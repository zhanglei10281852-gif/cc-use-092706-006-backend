from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.windows.calendar import month_day_covers, parse_month_day
from app.windows.repository import WindowRepository

SCHEMA = """
CREATE TABLE IF NOT EXISTS maintenance_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sensitive_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER REFERENCES maintenance_zones(id) ON DELETE CASCADE,
    species TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK(period_kind IN ('breeding','migration','other')),
    start_month_day TEXT NOT NULL,
    end_month_day TEXT NOT NULL,
    blocked_work_types_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sensitive_periods_zone ON sensitive_periods(zone_id, active);
CREATE TABLE IF NOT EXISTS maintenance_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES maintenance_zones(id) ON DELETE RESTRICT,
    work_type_id INTEGER NOT NULL REFERENCES work_types(id) ON DELETE RESTRICT,
    requested_start TEXT NOT NULL,
    requested_end TEXT NOT NULL,
    duration_days INTEGER NOT NULL CHECK(duration_days > 0),
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','blocked','cancelled')),
    scheduled_start TEXT,
    scheduled_end TEXT,
    evaluation_json TEXT NOT NULL DEFAULT '{}',
    requested_by TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_plans_status ON maintenance_plans(status, priority DESC);
CREATE TABLE IF NOT EXISTS plan_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES maintenance_plans(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('create','postpone','cancel')),
    reason TEXT NOT NULL,
    cause TEXT NOT NULL DEFAULT 'other',
    actor TEXT NOT NULL,
    request_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, action, request_key)
);
CREATE INDEX IF NOT EXISTS idx_plan_revisions_plan ON plan_revisions(plan_id, id);
"""

ONE_DAY = timedelta(days=1)
HORIZON_DAYS = 370
MAX_CANDIDATES = 3
KIND_LABELS = {"breeding": "繁殖", "migration": "迁徙", "other": "敏感"}


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _rule_blocks(rule: dict[str, Any], work_type_code: str, day: date) -> bool:
    blocked = rule["blocked_work_types"]
    if blocked and work_type_code not in blocked:
        return False
    return month_day_covers(rule["_start_md"], rule["_end_md"], day)


def _prepare_rules(periods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for period in periods:
        rule = dict(period)
        rule["_start_md"] = parse_month_day(period["start_month_day"])
        rule["_end_md"] = parse_month_day(period["end_month_day"])
        rules.append(rule)
    return rules


def _free_runs(rules: list[dict[str, Any]], work_type_code: str, start: date, end: date) -> list[tuple[date, date]]:
    runs: list[tuple[date, date]] = []
    run_start: date | None = None
    day = start
    while day <= end:
        if any(_rule_blocks(rule, work_type_code, day) for rule in rules):
            if run_start is not None:
                runs.append((run_start, day - ONE_DAY))
                run_start = None
        elif run_start is None:
            run_start = day
        day += ONE_DAY
    if run_start is not None:
        runs.append((run_start, end))
    return runs


def _conflict_segments(rules: list[dict[str, Any]], work_type_code: str, start: date, end: date) -> list[dict[str, Any]]:
    blocked_days: dict[int, list[date]] = {}
    day = start
    while day <= end:
        for rule in rules:
            if _rule_blocks(rule, work_type_code, day):
                blocked_days.setdefault(rule["id"], []).append(day)
        day += ONE_DAY
    conflicts: list[dict[str, Any]] = []
    by_id = {rule["id"]: rule for rule in rules}
    for rule_id in sorted(blocked_days):
        rule = by_id[rule_id]
        segments: list[dict[str, str]] = []
        for day in sorted(blocked_days[rule_id]):
            if segments and (day - date.fromisoformat(segments[-1]["end"])).days == 1:
                segments[-1]["end"] = day.isoformat()
            else:
                segments.append({"start": day.isoformat(), "end": day.isoformat()})
        blocked = rule["blocked_work_types"]
        target = "全部作业" if not blocked else f"作业「{work_type_code}」"
        conflicts.append({
            "rule_id": rule["id"],
            "species": rule["species"],
            "period_kind": rule["period_kind"],
            "period": {"start": rule["start_month_day"], "end": rule["end_month_day"]},
            "zone_code": rule["zone_code"],
            "blocked_work_types": blocked,
            "segments": segments,
            "reason": f"{rule['species']}{KIND_LABELS[rule['period_kind']]}敏感期（{rule['start_month_day']}至{rule['end_month_day']}）内禁止{target}",
        })
    return conflicts


def evaluate_windows(periods: list[dict[str, Any]], *, work_type_code: str, start: date, end: date, duration_days: int, as_of: date) -> dict[str, Any]:
    """按注入基准日计算可执行窗口、冲突原因与候选方案（纯函数，便于复核）。"""
    rules = _prepare_rules(periods)
    effective_start = max(start, as_of)
    windows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    if effective_start <= end:
        for run_start, run_end in _free_runs(rules, work_type_code, effective_start, end):
            days = (run_end - run_start).days + 1
            windows.append({
                "start": run_start.isoformat(),
                "end": run_end.isoformat(),
                "days": days,
                "fits": days >= duration_days,
            })
        conflicts = _conflict_segments(rules, work_type_code, effective_start, end)
    feasible = any(window["fits"] for window in windows)
    candidates: list[dict[str, Any]] = []
    if not feasible:
        scan_from = max(end + ONE_DAY, as_of)
        for run_start, run_end in _free_runs(rules, work_type_code, scan_from, scan_from + timedelta(days=HORIZON_DAYS)):
            if (run_end - run_start).days + 1 < duration_days:
                continue
            candidates.append({
                "start": run_start.isoformat(),
                "end": (run_start + timedelta(days=duration_days - 1)).isoformat(),
                "days": duration_days,
                "available_until": run_end.isoformat(),
            })
            if len(candidates) >= MAX_CANDIDATES:
                break
    return {
        "as_of": as_of.isoformat(),
        "effective_start": effective_start.isoformat() if effective_start <= end else None,
        "requested": {"start": start.isoformat(), "end": end.isoformat(), "duration_days": duration_days},
        "feasible": feasible,
        "windows": windows,
        "conflicts": conflicts,
        "candidates": candidates,
    }


def _schedule_from(evaluation: dict[str, Any]) -> tuple[str, str | None, str | None]:
    """从评估结果中挑选最早可容纳窗口作为排班结果。"""
    for window in evaluation["windows"]:
        if window["fits"]:
            start = date.fromisoformat(window["start"])
            end = start + timedelta(days=evaluation["requested"]["duration_days"] - 1)
            return "scheduled", start.isoformat(), end.isoformat()
    return "blocked", None, None


class WindowPlanningService:
    """维护区域、敏感期规则与维护计划，计算可执行窗口并保存版本化历史。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_schema()
        self.repository = WindowRepository(self.connection)

    # ---- 登记：区域 / 作业类型 / 敏感期 ----

    def create_zone(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = WindowRepository(connection)
            if repository.zone_by_code(payload["code"]):
                raise ConflictError("区域编码已存在")
            return repository.create_zone(code=payload["code"], name=payload["name"], description=payload["description"], actor=actor, now=now)

    def list_zones(self) -> list[dict[str, Any]]:
        return self.repository.list_zones()

    def create_work_type(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = WindowRepository(connection)
            if repository.work_type_by_code(payload["code"]):
                raise ConflictError("作业类型编码已存在")
            return repository.create_work_type(code=payload["code"], name=payload["name"], description=payload["description"], actor=actor, now=now)

    def list_work_types(self) -> list[dict[str, Any]]:
        return self.repository.list_work_types()

    def create_period(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        parse_month_day(payload["start_month_day"])
        parse_month_day(payload["end_month_day"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = WindowRepository(connection)
            zone_id = self._zone_id(repository, payload.get("zone_code"), required=False)
            blocked = sorted(set(payload["blocked_work_types"]))
            for code in blocked:
                work_type = repository.work_type_by_code(code)
                if work_type is None or not work_type["active"]:
                    raise ValidationError(f"禁作规则引用了未知作业类型：{code}")
            return repository.create_period(
                zone_id=zone_id, species=payload["species"], period_kind=payload["period_kind"],
                start_month_day=payload["start_month_day"], end_month_day=payload["end_month_day"],
                blocked_work_types=blocked, note=payload["note"], actor=actor, now=now,
            )

    def list_periods(self, zone_code: str | None = None) -> list[dict[str, Any]]:
        zone_id = None
        if zone_code:
            zone = self.repository.zone_by_code(zone_code)
            if zone is None:
                raise NotFoundError("区域不存在")
            zone_id = zone["id"]
        return self.repository.list_periods(zone_id=zone_id)

    def retire_period(self, period_id: int, actor: str) -> dict[str, Any]:
        del actor
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            retired = WindowRepository(connection).retire_period(period_id, now=now)
            if retired is None:
                raise NotFoundError("敏感期规则不存在或已停用")
            return retired

    # ---- 窗口计算 ----

    def compute(self, payload: dict[str, Any]) -> dict[str, Any]:
        as_of = payload.get("as_of") or self.clock.now().date()
        with transaction() as connection:
            repository = WindowRepository(connection)
            zone_id = self._zone_id(repository, payload["zone_code"])
            work_type = self._work_type(repository, payload["work_type_code"])
            evaluation = evaluate_windows(
                repository.active_rules_for_zone(zone_id),
                work_type_code=work_type["code"],
                start=payload["start"], end=payload["end"],
                duration_days=payload["duration_days"], as_of=as_of,
            )
        return {"zone_code": payload["zone_code"], "work_type_code": work_type["code"], **evaluation}

    # ---- 计划生命周期 ----

    def create_plan(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        as_of = payload.get("as_of") or self.clock.now().date()
        now = to_storage(self.clock.now())
        payload_hash = request_fingerprint(self._plan_fingerprint(payload, as_of))
        with transaction(immediate=True) as connection:
            repository = WindowRepository(connection)
            existing = repository.plan_by_idempotency(actor, payload["idempotency_key"])
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise ConflictError("同一幂等键对应了不同的计划内容")
                return self._plan_view(repository, existing["id"]) | {"replayed": True}
            zone_id = self._zone_id(repository, payload["zone_code"])
            work_type = self._work_type(repository, payload["work_type_code"])
            evaluation = evaluate_windows(
                repository.active_rules_for_zone(zone_id),
                work_type_code=work_type["code"],
                start=payload["requested_start"], end=payload["requested_end"],
                duration_days=payload["duration_days"], as_of=as_of,
            )
            status, scheduled_start, scheduled_end = _schedule_from(evaluation)
            plan = repository.create_plan(
                zone_id=zone_id, work_type_id=work_type["id"],
                requested_start=payload["requested_start"].isoformat(), requested_end=payload["requested_end"].isoformat(),
                duration_days=payload["duration_days"], priority=payload["priority"],
                status=status, scheduled_start=scheduled_start, scheduled_end=scheduled_end,
                evaluation=evaluation, requested_by=actor, idempotency_key=payload["idempotency_key"],
                payload_hash=payload_hash, note=payload["note"], now=now,
            )
            repository.add_revision(
                plan_id=plan["id"], version=plan["version"], action="create", reason="登记维护计划",
                cause="other", actor=actor, request_key=payload["idempotency_key"], request_hash=payload_hash,
                before={}, after=plan, now=now,
            )
            return self._plan_view(repository, plan["id"]) | {"replayed": False}

    def list_plans(self, *, status: str | None = None, zone_code: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        zone_id = None
        if zone_code:
            zone = self.repository.zone_by_code(zone_code)
            if zone is None:
                raise NotFoundError("区域不存在")
            zone_id = zone["id"]
        rows = self.repository.list_plans(status=status, zone_id=zone_id, limit=max(1, min(limit, 500)))
        return [self._view(row) for row in rows]

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        row = self.repository.plan_by_id(plan_id)
        if row is None:
            raise NotFoundError("维护计划不存在")
        view = self._view(row)
        view["revisions"] = self.repository.revisions_for_plan(plan_id)
        return view

    def postpone_plan(self, plan_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        as_of = payload.get("as_of") or self.clock.now().date()
        now = to_storage(self.clock.now())
        request_hash = request_fingerprint(self._postpone_fingerprint(payload, as_of))
        with transaction(immediate=True) as connection:
            repository = WindowRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("维护计划不存在")
            replay = repository.revision_by_key(plan_id, "postpone", payload["idempotency_key"])
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise ConflictError("同一幂等键对应了不同的延期请求")
                return self._plan_view(repository, plan_id) | {"replayed": True}
            if plan["status"] == "cancelled":
                raise ConflictError("计划已取消，不能延期")
            if payload.get("shift_days") is not None:
                shift = timedelta(days=payload["shift_days"])
                start = date.fromisoformat(plan["requested_start"]) + shift
                end = date.fromisoformat(plan["requested_end"]) + shift
            else:
                start, end = payload["new_start"], payload["new_end"]
            evaluation = evaluate_windows(
                repository.active_rules_for_zone(plan["zone_id"]),
                work_type_code=plan["work_type_code"],
                start=start, end=end, duration_days=int(plan["duration_days"]), as_of=as_of,
            )
            status, scheduled_start, scheduled_end = _schedule_from(evaluation)
            before = self._view(plan)
            updated = repository.apply_evaluation(
                plan_id, requested_start=start.isoformat(), requested_end=end.isoformat(),
                status=status, scheduled_start=scheduled_start, scheduled_end=scheduled_end,
                evaluation=evaluation, now=now,
            )
            after = self._view(updated)
            repository.add_revision(
                plan_id=plan_id, version=after["version"], action="postpone", reason=payload["reason"],
                cause=payload["cause"], actor=actor, request_key=payload["idempotency_key"], request_hash=request_hash,
                before=before, after=after, now=now,
            )
            return self._plan_view(repository, plan_id) | {"replayed": False}

    def cancel_plan(self, plan_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        request_hash = request_fingerprint({"reason": payload["reason"]})
        with transaction(immediate=True) as connection:
            repository = WindowRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("维护计划不存在")
            replay = repository.revision_by_key(plan_id, "cancel", payload["idempotency_key"])
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    raise ConflictError("同一幂等键对应了不同的取消请求")
                return self._plan_view(repository, plan_id) | {"replayed": True}
            if plan["status"] == "cancelled":
                raise ConflictError("计划已取消")
            before = self._view(plan)
            after = self._view(repository.cancel_plan(plan_id, now=now))
            repository.add_revision(
                plan_id=plan_id, version=after["version"], action="cancel", reason=payload["reason"],
                cause="other", actor=actor, request_key=payload["idempotency_key"], request_hash=request_hash,
                before=before, after=after, now=now,
            )
            return self._plan_view(repository, plan_id) | {"replayed": False}

    # ---- 内部工具 ----

    @staticmethod
    def _zone_id(repository: WindowRepository, code: str | None, *, required: bool = True) -> int | None:
        if code is None:
            if required:
                raise ValidationError("必须指定区域")
            return None
        zone = repository.zone_by_code(code)
        if zone is None or not zone["active"]:
            raise NotFoundError(f"区域不存在或已停用：{code}")
        return int(zone["id"])

    @staticmethod
    def _work_type(repository: WindowRepository, code: str) -> sqlite3.Row:
        work_type = repository.work_type_by_code(code)
        if work_type is None or not work_type["active"]:
            raise NotFoundError(f"作业类型不存在或已停用：{code}")
        return work_type

    @staticmethod
    def _plan_fingerprint(payload: dict[str, Any], as_of: date) -> dict[str, Any]:
        return {
            "zone_code": payload["zone_code"], "work_type_code": payload["work_type_code"],
            "requested_start": payload["requested_start"].isoformat(), "requested_end": payload["requested_end"].isoformat(),
            "duration_days": payload["duration_days"], "priority": payload["priority"],
            "as_of": as_of.isoformat(), "note": payload["note"],
        }

    @staticmethod
    def _postpone_fingerprint(payload: dict[str, Any], as_of: date) -> dict[str, Any]:
        return {
            "reason": payload["reason"], "cause": payload["cause"],
            "shift_days": payload.get("shift_days"),
            "new_start": payload["new_start"].isoformat() if payload.get("new_start") else None,
            "new_end": payload["new_end"].isoformat() if payload.get("new_end") else None,
            "as_of": as_of.isoformat(),
        }

    def _plan_view(self, repository: WindowRepository, plan_id: int) -> dict[str, Any]:
        row = repository.plan_by_id(plan_id)
        if row is None:
            raise NotFoundError("维护计划不存在")
        return self._view(row)

    @staticmethod
    def _view(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        view = dict(row)
        evaluation = view.pop("evaluation_json")
        view["evaluation"] = json.loads(evaluation) if evaluation else {}
        return view
