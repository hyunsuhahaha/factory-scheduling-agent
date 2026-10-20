"""agent 평가 셋.

케이스는 정답을 아는 문제(generator)에서 만든다: 문제를 한국어 지시문으로 풀어 쓰고,
agent가 tool에 넘긴 입력이 원래 문제와 같은지 프로그램으로 채점한다 (LLM 채점 없음).

카테고리
  create_absolute   날짜를 그대로 준 신규 계획
  create_relative   "수요일 18시"처럼 요일로 준 납기 → 현재 시각 기준 환산
  create_alias      설비를 "2호기"로 부름 → EXT-2 매핑
  breakdown         기존 계획에 설비 정지 반영 (reschedule + freeze)
  rush_order        긴급 주문 추가
  due_change        납기 변경
  cancel            주문 취소
  missing_info      필수 정보 누락 → 제출하지 말고 되물어야 함
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from ..solver.generator import generate
from ..solver.schema import ScheduleProblem
from .loop import Trace

WEEKDAYS = "월화수목금토일"
NOW = datetime(2026, 11, 2, 8, 0)  # 월요일


def fmt_abs(dt: datetime) -> str:
    return f"{dt.month}/{dt.day:02d}({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def fmt_rel(dt: datetime, now: datetime) -> str:
    days = (dt.date() - now.date()).days
    word = {0: "오늘", 1: "내일", 2: "모레"}.get(days, f"{'이번 주' if days < 7 else '다음 주'} {WEEKDAYS[dt.weekday()]}요일")
    return f"{word} {dt:%H:%M}"


def machine_name(machine_id: str, alias: bool) -> str:
    return f"{machine_id.split('-')[1]}호기" if alias else machine_id


def render_problem(p: ScheduleProblem, now: datetime, dates: str = "abs", alias: bool = False, drop: str | None = None) -> str:
    """drop: 'rates' | 'due' | 'orders' — 일부러 뺄 정보 (missing_info 케이스)."""
    fmt = (lambda d: fmt_abs(d)) if dates == "abs" else (lambda d: fmt_rel(d, now))
    lines = ["압출 계획 짜줘."]
    if alias:
        lines.append(f"설비는 {', '.join(machine_name(m.id, True) for m in p.machines)} (시스템 id는 EXT-번호).")
    else:
        lines.append(f"설비: {', '.join(m.id for m in p.machines)}.")
    init = [f"{machine_name(m.id, alias)}={m.initial_family}" for m in p.machines if m.initial_family]
    if init:
        lines.append("지금 세팅된 원료: " + ", ".join(init) + ".")
    lines.append("")
    lines.append("제품 (원료 계열 / 설비별 시간당 생산량):")
    for prod in p.products:
        if drop == "rates":
            lines.append(f"- {prod.id}: {prod.family}, 가능 설비 {', '.join(machine_name(m, alias) for m in prod.rates)}")
        else:
            rates = ", ".join(f"{machine_name(m, alias)} {r:g}개" for m, r in prod.rates.items())
            lines.append(f"- {prod.id}: {prod.family} / {rates}")
    lines.append("")
    rules = ", ".join(f"{r.from_family}→{r.to_family} {r.minutes}분" for r in p.setup_rules)
    lines.append(f"원료 계열이 바뀌면 교체 시간 기본 {p.default_setup_minutes}분. 예외: {rules}.")
    lines.append("")
    if drop == "orders":
        lines.append("주문은 이번 주 들어온 거 전부.")
    else:
        lines.append("주문:")
        for o in p.orders:
            parts = [f"- {o.id}: {o.product_id} {o.quantity}개"]
            if drop != "due":
                parts.append(f"납기 {fmt(o.due)}")
            if o.release and o.release > p.horizon_start:
                parts.append(f"자재 입고 {fmt(o.release)} 이후 시작 가능")
            if o.weight > 1:
                parts.append(f"중요도 {o.weight}")
            lines.append(", ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------- checks

Check = Callable[[Trace, "CaseContext"], tuple[bool, str]]


@dataclass
class CaseContext:
    parent_job_id: str | None = None
    parent_problem: ScheduleProblem | None = None


def _dt(s) -> datetime | None:
    if s is None:
        return None
    return s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", ""))


def _last_input(trace: Trace, tool: str) -> dict | None:
    calls = [c for c in trace.calls(tool) if not c.is_error]
    return calls[-1].input if calls else None


def problem_matches(expected: ScheduleProblem) -> Check:
    def check(trace: Trace, ctx: CaseContext) -> tuple[bool, str]:
        given = _last_input(trace, "submit_schedule")
        if given is None:
            return False, "no successful submit_schedule"
        try:
            got = ScheduleProblem.model_validate(given["problem"])
        except Exception as exc:  # noqa: BLE001
            return False, f"invalid problem: {exc}"
        errs = []
        if got.horizon_start != NOW:
            errs.append(f"horizon_start {got.horizon_start}")
        if {m.id for m in got.machines} != {m.id for m in expected.machines}:
            errs.append(f"machines {sorted(m.id for m in got.machines)}")
        init = {m.id: m.initial_family for m in got.machines}
        if any(init.get(m.id) != m.initial_family for m in expected.machines):
            errs.append("initial_family")
        gp = {p.id: p for p in got.products}
        for p in expected.products:
            q = gp.get(p.id)
            if q is None or q.family != p.family or set(q.rates) != set(p.rates) or any(
                abs(q.rates[m] - r) > 0.005 * r for m, r in p.rates.items()
            ):
                errs.append(f"product {p.id}")
        go = {o.id: o for o in got.orders}
        if set(go) != {o.id for o in expected.orders}:
            errs.append(f"orders {sorted(go)}")
        for o in expected.orders:
            g = go.get(o.id)
            if g is None:
                continue
            if (g.product_id, g.quantity, g.due) != (o.product_id, o.quantity, o.due):
                errs.append(f"order {o.id} {g.product_id} {g.quantity} {g.due}")
            if (g.release or NOW) != (o.release or NOW):
                errs.append(f"order {o.id} release {g.release}")
            if g.weight != o.weight:
                errs.append(f"order {o.id} weight {g.weight}")
        if got.default_setup_minutes != expected.default_setup_minutes:
            errs.append(f"default_setup {got.default_setup_minutes}")
        rules = lambda pr: {(r.from_family, r.to_family, r.minutes) for r in pr.setup_rules}  # noqa: E731
        if rules(got) != rules(expected):
            errs.append("setup_rules")
        return not errs, "; ".join(errs[:6])

    return check


def patch_check(fn: Callable[[dict], list[str]]) -> Check:
    def check(trace: Trace, ctx: CaseContext) -> tuple[bool, str]:
        given = _last_input(trace, "reschedule")
        if given is None:
            return False, "no successful reschedule"
        if given.get("job_id") != ctx.parent_job_id:
            return False, f"wrong job_id {given.get('job_id')}"
        patch = given.get("patch") or {}
        errs = fn(patch)
        if _dt(patch.get("freeze_until")) != NOW:
            errs.append(f"freeze_until {patch.get('freeze_until')}")
        return not errs, "; ".join(errs)

    return check


def finished_and_answered(trace: Trace, ctx: CaseContext) -> tuple[bool, str]:
    waits = [c for c in trace.calls("wait_for_job") if not c.is_error]
    if not waits:
        return False, "never waited for the job"
    if '"succeeded"' not in waits[-1].output:
        return False, "job not succeeded"
    if not trace.final_text:
        return False, f"no final answer ({trace.error})"
    return True, ""


def asks_instead_of_submitting(trace: Trace, ctx: CaseContext) -> tuple[bool, str]:
    if trace.calls("submit_schedule") or trace.calls("reschedule"):
        return False, "submitted despite missing info"
    if "?" not in trace.final_text and "알려" not in trace.final_text:
        return False, "did not ask a question"
    return True, ""


@dataclass
class Case:
    id: str
    category: str
    prompt: str
    checks: list[Check]
    parent: ScheduleProblem | None = None  # 있으면 하네스가 먼저 계획을 만들어 두고 job id를 알려준다
    oracle: tuple[str, dict] | None = None  # 정답 tool 호출 (reschedule이면 job_id는 실행 시 채움). 하네스 검증용
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------- case builders

def _base(seed: int, n: int, m: int) -> ScheduleProblem:
    return generate(n_orders=n, n_machines=m, seed=seed, horizon_start=NOW, time_limit_s=5)


def _create(cid: str, category: str, seed: int, n: int, m: int, **render) -> Case:
    p = _base(seed, n, m)
    prompt = f"현재 시각 {fmt_abs(NOW)}.\n" + render_problem(p, NOW, **render)
    return Case(cid, category, prompt, [problem_matches(p), finished_and_answered],
                oracle=("submit_schedule", {"problem": p.model_dump(mode="json")}))


def _replan(cid: str, category: str, seed: int, text: str, fn: Callable[[dict], list[str]], oracle_patch: dict) -> Case:
    parent = _base(seed, 8, 3)
    prompt = f"현재 시각 {fmt_abs(NOW)}. 기존 계획 job id: {{parent_job_id}}.\n{text}"
    patch = {"horizon_start": NOW.isoformat(), "freeze_until": NOW.isoformat(), **oracle_patch}
    return Case(cid, category, prompt, [patch_check(fn), finished_and_answered], parent=parent,
                oracle=("reschedule", {"patch": patch}))


def _downtime(machine: str, hours: float) -> Callable[[dict], list[str]]:
    def fn(patch: dict) -> list[str]:
        for d in patch.get("machine_downtime") or []:
            if d.get("machine_id") == machine and _dt(d["start"]) == NOW and _dt(d["end"]) == NOW + timedelta(hours=hours):
                return []
        return [f"downtime {machine} {hours}h not found in {patch.get('machine_downtime')}"]

    return fn


def build_cases() -> list[Case]:
    cases: list[Case] = []
    for k, (seed, n, m) in enumerate([(1, 4, 2), (2, 5, 2), (3, 6, 3), (4, 6, 3), (5, 8, 3), (6, 8, 4), (7, 10, 4), (8, 12, 4)]):
        cases.append(_create(f"create_abs_{k + 1}", "create_absolute", seed, n, m, dates="abs"))
    for k, (seed, n, m) in enumerate([(11, 4, 2), (12, 5, 3), (13, 6, 3), (14, 8, 3), (15, 8, 4)]):
        cases.append(_create(f"create_rel_{k + 1}", "create_relative", seed, n, m, dates="rel"))
    for k, (seed, n, m) in enumerate([(21, 4, 2), (22, 6, 3), (23, 8, 4), (24, 10, 4)]):
        cases.append(_create(f"create_alias_{k + 1}", "create_alias", seed, n, m, dates="abs", alias=True))

    for k, (seed, machine, hours, phrasing) in enumerate([
        (31, "EXT-2", 6, "EXT-2 지금 고장 났어. 6시간 정지야. 다시 짜줘."),
        (32, "EXT-1", 4, "1호기 스크류 교체 들어가. 지금부터 4시간 못 써."),
        (33, "EXT-3", 8, "3호기 금형 파손, 지금부터 8시간 정지. 이미 돌고 있는 건 건드리지 말고 다시 계획 잡아줘."),
        (34, "EXT-2", 2.5, "2호기 정기 점검 지금부터 2시간 30분. 반영해줘."),
        (35, "EXT-1", 12, "EXT-1 메인 모터 문제로 지금부터 12시간 정지됨."),
    ]):
        down = {"machine_id": machine, "start": NOW.isoformat(), "end": (NOW + timedelta(hours=hours)).isoformat()}
        cases.append(_replan(f"breakdown_{k + 1}", "breakdown", seed, phrasing, _downtime(machine, hours), {"machine_downtime": [down]}))

    for k, seed in enumerate([41, 42, 43, 44]):
        parent = _base(seed, 8, 3)
        prod = parent.products[k % len(parent.products)]
        qty, due = [1500, 3000, 2200, 800][k], NOW + timedelta(days=[1, 2, 1, 3][k], hours=[6, 2, 10, 0][k])

        def fn(patch: dict, prod=prod.id, qty=qty, due=due) -> list[str]:
            for o in patch.get("add_orders") or []:
                if o.get("product_id") == prod and o.get("quantity") == qty and _dt(o.get("due")) == due:
                    return [] if not patch.get("add_products") else ["added an existing product"]
            return [f"rush order {prod} {qty} {due} not found in {patch.get('add_orders')}"]

        text = f"긴급 주문 들어왔어. {prod} {qty}개, 납기 {fmt_abs(due)}. 기존 계획에 끼워 넣어줘."
        add = {"id": f"RUSH-{k + 1}", "product_id": prod.id, "quantity": qty, "due": due.isoformat(), "weight": 3}
        cases.append(_replan(f"rush_{k + 1}", "rush_order", seed, text, fn, {"add_orders": [add]}))

    for k, seed in enumerate([51, 52, 53]):
        parent = _base(seed, 8, 3)
        order = parent.orders[k + 1]
        new_due = order.due - timedelta(hours=[12, 24, 6][k])

        def fn(patch: dict, oid=order.id, new_due=new_due) -> list[str]:
            for u in patch.get("order_updates") or []:
                if u.get("id") == oid and _dt(u.get("due")) == new_due:
                    return []
            return [f"due update {oid} {new_due} not found in {patch.get('order_updates')}"]

        text = f"고객 요청으로 {order.id} 납기가 {fmt_abs(new_due)}로 당겨졌어. 다시 짜줘."
        upd = {"id": order.id, "due": new_due.isoformat()}
        cases.append(_replan(f"due_{k + 1}", "due_change", seed, text, fn, {"order_updates": [upd]}))

    for k, seed in enumerate([61, 62, 63]):
        parent = _base(seed, 8, 3)
        oid = parent.orders[-(k + 1)].id

        def fn(patch: dict, oid=oid) -> list[str]:
            return [] if oid in (patch.get("remove_order_ids") or []) else [f"{oid} not removed: {patch.get('remove_order_ids')}"]

        cases.append(_replan(f"cancel_{k + 1}", "cancel", seed, f"{oid} 주문 취소됐어. 빼고 다시 짜줘.", fn,
                             {"remove_order_ids": [oid]}))

    for k, (seed, drop) in enumerate([(71, "rates"), (72, "due"), (73, "orders"), (74, "rates")]):
        p = _base(seed, 5, 3)
        prompt = f"현재 시각 {fmt_abs(NOW)}.\n" + render_problem(p, NOW, dates="abs", drop=drop)
        cases.append(Case(f"missing_{k + 1}", "missing_info", prompt, [asks_instead_of_submitting], meta={"dropped": drop}))
    return cases
