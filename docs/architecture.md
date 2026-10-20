# 설계 결정

## 1. 작업 생명주기

```mermaid
stateDiagram-v2
  [*] --> queued: POST /jobs (DB 커밋 후 enqueue)
  queued --> running: worker claim (Lua, 원자적)
  queued --> cancelled: POST /cancel (ZREM)
  running --> succeeded: solve 완료, 조건부 UPDATE
  running --> failed: 입력·모델 오류 (재시도 무의미)
  running --> queued: lease 만료 → reaper 재등록
  running --> failed: lease 만료 max_attempts회
  running --> cancelled: POST /cancel (워커 결과는 폐기)
```

| 결정 | 이유 |
|---|---|
| DB가 상태 원본, Redis는 스케줄링만 | Redis가 날아가도 작업 기록·결과는 남는다. Redis는 "누가 다음에 무엇을 돌릴지"만 안다. |
| DB 커밋 → enqueue 순서 | 반대 순서면 워커가 아직 커밋 안 된 작업을 집을 수 있다. enqueue 실패 시 작업을 `failed`로 표시해 유령 작업을 남기지 않는다. |
| 워커의 DB 갱신은 `WHERE id AND attempt AND status='running'` | lease가 만료돼 다른 워커가 같은 작업을 다시 가져갔거나 사용자가 취소했으면, 늦게 끝난 워커의 결과는 0행 갱신으로 자연히 버려진다. 분산 락 없이 멱등성 확보. |

## 2. 큐

| 결정 | 대안 | 이유 |
|---|---|---|
| Redis ZSET + Lua 직접 구현 | Celery, RQ, arq | 사용자별 동시 실행 한도와 fair queuing을 claim 시점에 원자적으로 판단해야 한다. 범용 큐는 "꺼낸 뒤 한도 초과면 되돌리기"가 되어 경합 시 순서가 깨지고 불필요한 왕복이 생긴다. 스크립트 4개(enqueue, claim, release, reap)라 유지 부담이 작다. |
| lease + heartbeat | ack 기반 visibility timeout | 최적화 작업은 수 초~2분. heartbeat가 lease를 연장하므로 긴 작업도 회수되지 않고, 워커가 죽으면 `lease_seconds` 안에 다른 워커가 회수한다. |
| 시도 횟수 상한 (`max_attempts`) | 무한 재시도 | 특정 입력이 solve 프로세스를 죽이는 경우(OOM 등) 워커를 계속 쓰러뜨리는 독약 작업을 막는다. |
| 입력·모델 오류는 즉시 `failed` | 재시도 | 같은 입력은 같은 결과. 재시도는 프로세스 비정상 종료(`BrokenProcessPool`)에만 적용한다. |

### fair queuing

pending score = `(9 - priority) × 10^13 + 가상 시작 시각`.

사용자마다 가상 시각 `vt`를 두고, 작업을 넣을 때 `start = max(now, vt_user)`, `vt_user = start + 예상 실행 시간(time limit)`.

- 한 사용자가 작업 20개를 몰아 넣으면 그 작업들의 가상 시작 시각이 time limit 간격으로 벌어진다.
- 나중에 온 사용자의 첫 작업은 `now`에 놓여 그 사이로 들어간다.
- 무거운 작업(긴 time limit)을 넣는 사용자는 그만큼 뒤로 밀린다 → 자원 사용량 기준 공정성.
- 우선순위 구간이 먼저 비교되므로 긴급(9) 작업은 공정성보다 앞선다.

부하 테스트에서 사용자별 첫 작업 최대 대기가 19.0s → 5.4s로 줄었다.

### 사용자별 동시 실행 한도

claim 스크립트가 pending 앞쪽부터 훑으며 `user_running < limit`인 첫 작업을 꺼낸다. 한도에 걸린 사용자의 작업은 건너뛰므로 한 사용자가 워커 슬롯을 독점하지 못한다.
한도는 `user_limit` HASH(관리자 API로 설정)에 있고, 없으면 기본값.

## 3. 워커와 CPU 자원

| 결정 | 이유 |
|---|---|
| solve는 `ProcessPoolExecutor` | CP-SAT은 CPU 바운드. 이벤트 루프 스레드에서 돌리면 heartbeat가 멈춰 lease가 만료된다. 프로세스 분리로 solve 크래시가 워커를 죽이지 않는다. |
| `worker_slots × solver_threads ≤ 코어 수` | CP-SAT은 멀티스레드 portfolio 탐색. 슬롯과 스레드를 곱한 값이 코어를 넘으면 모든 작업이 같이 느려진다. 처리량(슬롯↑) vs 개별 품질(스레드↑) 조절 지점. |
| 수평 확장 = 워커 컨테이너 수 | 워커는 상태가 없다. compose `replicas`, Kubernetes Deployment 그대로 늘리면 된다. |
| graceful shutdown | SIGTERM 시 새 claim을 멈추고 실행 중 작업을 마친다 (`stop_grace_period: 2m`). 배포 중 작업 유실 없음. |

## 4. 인증과 MCP

| 결정 | 이유 |
|---|---|
| MCP 서버는 상태 없는 API 클라이언트 | 인증·할당량·큐를 한 곳(백엔드)에서만 관리. MCP 서버 장애가 작업 상태에 영향을 주지 않는다. |
| HTTP 모드에서 호출자 토큰 전달 | 공용 서비스 계정으로 붙이면 모든 사용자가 한 할당량을 나눠 쓰고 작업 소유권이 사라진다. MCP 서버는 호출자 Bearer 토큰을 백엔드 `/auth/me`로 검증하고, tool 호출에도 그 토큰을 그대로 쓴다. JWT 비밀키를 MCP 서버와 공유하지 않는다. |
| 타인 작업은 404 | 403이면 작업 id 존재 여부가 드러난다. |
| PBKDF2 해시는 스레드에서 | 24만 회 반복은 수백 ms CPU. 이벤트 루프에서 돌리면 동시 요청 전체가 멈춘다 (부하 테스트에서 제출 p95 4.4s의 원인). |
| 운영 모드에서 기본 JWT 키 거부 | 기본값으로 배포되는 사고 방지. 32자 미만이면 기동 실패. |

## 5. 최적화 모델

| 결정 | 이유 |
|---|---|
| CP-SAT (제약 프로그래밍) | 세팅 시간이 순서에 따라 달라지는 스케줄링은 MIP보다 CP가 강하다. interval·no-overlap·circuit을 직접 표현할 수 있다. |
| 세팅 시간은 설비별 circuit | 연속한 두 작업 사이 간격을 강제하려면 "누가 바로 다음인지"를 알아야 한다. 세팅이 필요 없는 설비는 circuit을 생략해 모델을 줄인다. |
| 기준 휴리스틱 해를 hint로 | 초기 해를 즉시 확보해 time limit이 짧아도 최소한 규칙 수준 이상을 보장. |
| 결과 독립 검증 | 모델 버그로 제약을 어긴 일정이 현장에 나가는 것을 막는 마지막 방어선. 테스트에서도 같은 검증기를 쓴다. |
| 착수 작업 고정, 충돌 시 해제 | 재계산 때 이미 돌고 있는 작업을 옮기면 현장에서 쓸 수 없다. 단, 고정 작업이 새 고장 구간과 겹치면 물리적으로 불가능하므로 고정을 풀고 `released_frozen`으로 알린다. |
| 목적함수 가중합 | 납기 지연(가중) 우선, 동률이면 빨리 끝나는 쪽. 가중치는 요청마다 조절 가능. |
