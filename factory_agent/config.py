from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FSA_", env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./dev.db"
    redis_url: str = "redis://localhost:6379/0"
    # true면 Redis 대신 프로세스 내 fakeredis를 쓰고 API 프로세스 안에서 워커를 돌린다 (로컬 데모용).
    embedded: bool = False
    # 시작 시 테이블 자동 생성 (개발·테스트용). 운영은 `alembic upgrade head`로 스키마를 관리한다.
    create_schema: bool = False
    # 독립 워커의 Prometheus 지표 포트 (0이면 끔). API는 /metrics로 노출한다.
    metrics_port: int = 0

    jwt_secret: str = "dev-only-change-me"
    jwt_ttl_minutes: int = 60 * 12
    admin_emails: list[str] = []

    # 사용자별 기본 할당량
    max_running_per_user: int = 2
    max_queued_per_user: int = 20

    # 워커
    worker_slots: int = 2  # 워커 프로세스 하나가 동시에 돌리는 solve 수
    solver_threads: int = 4  # solve 하나가 쓰는 CP-SAT 스레드 수 (slots * threads <= 코어 수 권장)
    lease_seconds: int = 30  # heartbeat가 끊기고 이 시간이 지나면 다른 워커가 작업을 다시 가져간다
    max_attempts: int = 3
    poll_interval: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
