from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from typing import Any

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction

# 区域类型：芦苇 / 旱柳 / 滩涂
ZONE_TYPES = ("reed", "dryland_willow", "mudflat")
# 敏感级别：avoid=建议避让（可强制覆盖），forbid=严禁作业
SEVERITIES = ("avoid", "forbid")
# 计划状态
PLAN_STATUSES = ("scheduled", "postponed", "cancelled", "completed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS wetland_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    zone_type TEXT NOT NULL CHECK(zone_type IN ('reed','dryland_willow','mudflat')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wetland_work_types (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wetland_sensitive_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES wetland_zones(id) ON DELETE CASCADE,
    work_type_code TEXT,
    species_name TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK(period_type IN ('annual','fixed')),
    start_day TEXT,
    end_day TEXT,
    start_date TEXT,
    end_date TEXT,
    severity TEXT NOT NULL DEFAULT 'forbid' CHECK(severity IN ('avoid','forbid')),
    reason TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wetland_rules_zone ON wetland_sensitive_rules(zone_id, active);
CREATE TABLE IF NOT EXISTS wetland_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id INTEGER NOT NULL REFERENCES wetland_zones(id),
    work_type_code TEXT NOT NULL,
    title TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    earliest_date TEXT NOT NULL,
    latest_date TEXT NOT NULL,
    duration_days INTEGER NOT NULL CHECK(duration_days >= 1),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','postponed','cancelled','completed')),
    window_start TEXT,
    window_end TEXT,
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(created_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_wetland_plans_zone ON wetland_plans(zone_id, status);
CREATE TABLE IF NOT EXISTS wetland_plan_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES wetland_plans(id) ON DELETE CASCADE,
    plan_version INTEGER NOT NULL,
    attempt_label TEXT NOT NULL DEFAULT '',
    rank INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    feasible INTEGER NOT NULL CHECK(feasible IN (0,1)),
    reason TEXT NOT NULL DEFAULT '',
    conflict_rules_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, plan_version, attempt_label, rank)
);
CREATE INDEX IF NOT EXISTS idx_wetland_candidates_plan ON wetland_plan_candidates(plan_id, plan_version);
CREATE TABLE IF NOT EXISTS wetland_plan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES wetland_plans(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('schedule','postpone','cancel','complete','recompute')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    from_status TEXT,
    to_status TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, version)
);
CREATE INDEX IF NOT EXISTS idx_wetland_history_plan ON wetland_plan_history(plan_id, version);
CREATE TABLE IF NOT EXISTS wetland_intervention_keys (
    plan_id INTEGER NOT NULL REFERENCES wetland_plans(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK(action IN ('postpone','cancel','complete')),
    idempotency_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(plan_id, action, idempotency_key)
);
"""

DEFAULT_WORK_TYPES = (
    ("reed_cutting", "芦苇收割"),
    ("willow_thinning", "旱柳疏伐"),
    ("mudflat_cleanup", "滩涂清理"),
    ("patrol", "巡查监测"),
)


def _today() -> date:
    return date.today()


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    now = _today().isoformat()
    for code, name in DEFAULT_WORK_TYPES:
        connection.execute(
            "INSERT OR IGNORE INTO wetland_work_types(code,name,active,created_at,updated_at) VALUES(?,?,'1',?,?)",
            (code, name, now, now),
        )


def parse_day(value: str) -> tuple[int, int]:
    try:
        month, day = (int(part) for part in value.split("-"))
    except ValueError as exc:
        raise ValidationError("日期必须为 MM-DD 格式") from exc
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise ValidationError("MM-DD 日期超出合法范围")
    return month, day


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"日期不合法：{value}（应为 YYYY-MM-DD）") from exc


def day_in_window(day: tuple[int, int], start: tuple[int, int], end: tuple[int, int]) -> bool:
    """判断 2 月 29 日这类周年规则点是否落在（可能跨年的）周年窗口内。"""
    if start <= end:
        return start <= day <= end
    return day >= start or day <= end


class WetlandsService:
    """维护窗口编排：登记区域/敏感期/作业类型，按注入日期计算可执行窗口并版本化干预。"""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_schema()

    # ------------------------------------------------------------------ 基础资料

    def create_zone(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["zone_type"] not in ZONE_TYPES:
            raise ValidationError("zone_type 不合法")
        now = _today().isoformat()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO wetland_zones(code,name,zone_type,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (payload["code"], payload["name"], payload["zone_type"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("区域编码已存在") from exc
            return self._zone(connection, cursor.lastrowid)

    def list_zones(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM wetland_zones"
        if not include_inactive:
            sql += " WHERE active=1"
        return [dict(row) for row in self.connection.execute(sql + " ORDER BY code").fetchall()]

    def create_work_type(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = _today().isoformat()
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO wetland_work_types(code,name,created_at,updated_at) VALUES(?,?,?,?)",
                    (payload["code"], payload["name"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("作业类型编码已存在") from exc
            return dict(connection.execute("SELECT * FROM wetland_work_types WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_work_types(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM wetland_work_types WHERE active=1 ORDER BY code").fetchall()]

    def create_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        zone = self.connection.execute("SELECT id FROM wetland_zones WHERE id=? AND active=1", (payload["zone_id"],)).fetchone()
        if zone is None:
            raise NotFoundError("区域不存在或已停用")
        work_type_code = payload.get("work_type_code")
        if work_type_code is not None:
            row = self.connection.execute("SELECT 1 FROM wetland_work_types WHERE code=? AND active=1", (work_type_code,)).fetchone()
            if row is None:
                raise ValidationError("作业类型不存在或已停用")
        severity = payload.get("severity", "forbid")
        if severity not in SEVERITIES:
            raise ValidationError("severity 必须为 avoid 或 forbid")
        now = _today().isoformat()
        if payload["period_type"] == "annual":
            start_day = payload.get("start_day")
            end_day = payload.get("end_day")
            if not start_day or not end_day:
                raise ValidationError("周年规则必须提供 start_day 与 end_day (MM-DD)")
            parse_day(start_day)
            parse_day(end_day)
            start_date = end_date = None
        else:
            start_date = payload.get("start_date")
            end_date = payload.get("end_date")
            if not start_date or not end_date:
                raise ValidationError("固定规则必须提供 start_date 与 end_date (YYYY-MM-DD)")
            parsed_start = parse_iso_date(start_date)
            parsed_end = parse_iso_date(end_date)
            if parsed_end < parsed_start:
                raise ValidationError("敏感期结束日期不能早于开始日期")
            start_day = end_day = None
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO wetland_sensitive_rules(zone_id,work_type_code,species_name,period_type,start_day,end_day,start_date,end_date,severity,reason,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (payload["zone_id"], work_type_code, payload["species_name"], payload["period_type"], start_day, end_day,
                 start_date, end_date, severity, payload.get("reason", ""), now, now),
            )
            return self._rule(connection, cursor.lastrowid)

    def list_rules(self, zone_id: int | None = None) -> list[dict[str, Any]]:
        if zone_id is not None:
            rows = self.connection.execute("SELECT * FROM wetland_sensitive_rules WHERE zone_id=? ORDER BY id", (zone_id,)).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM wetland_sensitive_rules ORDER BY zone_id,id").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 试算窗口

    def preview(self, payload: dict[str, Any], today_value: date) -> dict[str, Any]:
        zone = self._require_zone(payload["zone_id"])
        self._require_work_type(payload["work_type_code"])
        earliest = parse_iso_date(payload["earliest_date"])
        latest = parse_iso_date(payload["latest_date"])
        duration = int(payload["duration_days"])
        self._validate_span(earliest, latest, duration)
        candidates = self._candidates(zone, payload["work_type_code"], earliest, latest, duration, payload.get("priority", 50))
        return {
            "zone_id": zone["id"],
            "work_type_code": payload["work_type_code"],
            "earliest_date": earliest.isoformat(),
            "latest_date": latest.isoformat(),
            "duration_days": duration,
            "evaluated_at": today_value.isoformat(),
            "candidates": [self._candidate_dict(item) for item in candidates],
        }

    # ------------------------------------------------------------------ 计划编排

    def schedule_plan(self, payload: dict[str, Any], actor: str, today_value: date) -> dict[str, Any]:
        zone = self._require_zone(payload["zone_id"])
        self._require_work_type(payload["work_type_code"])
        earliest = parse_iso_date(payload["earliest_date"])
        latest = parse_iso_date(payload["latest_date"])
        duration = int(payload["duration_days"])
        self._validate_span(earliest, latest, duration)
        digest = self._request_digest(payload)
        now = today_value.isoformat()
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM wetland_plans WHERE created_by=? AND idempotency_key=?",
                (actor, payload["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != digest:
                    raise ConflictError("同一幂等键对应了不同的编排请求")
                return self._plan_detail(connection, existing["id"])
            candidates = self._candidates(zone, payload["work_type_code"], earliest, latest, duration, payload.get("priority", 50))
            chosen = next((item for item in candidates if item["feasible"] and item["within_requested_range"]), None)
            if chosen is None:
                raise ConflictWithCandidates(
                    "注入日期范围内没有可执行窗口，候选方案（含范围外备选）已保留",
                    candidates=[self._candidate_dict(item) for item in candidates],
                )
            cursor = connection.execute(
                "INSERT INTO wetland_plans(zone_id,work_type_code,title,priority,earliest_date,latest_date,duration_days,"
                "status,window_start,window_end,idempotency_key,request_digest,conflict_count,version,created_by,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,'scheduled',?,?,?,?,?,1,?,?,?)",
                (zone["id"], payload["work_type_code"], payload["title"], payload.get("priority", 50),
                 earliest.isoformat(), latest.isoformat(), duration,
                 chosen["start"].isoformat(), chosen["end"].isoformat(),
                 payload["idempotency_key"], digest, len(chosen["conflicts"]), actor, now, now),
            )
            plan_id = cursor.lastrowid
            self._persist_candidates(connection, plan_id, 1, candidates, now)
            self._write_history(connection, plan_id, 1, "schedule", actor, payload.get("reason", "首次编排"),
                                None, "scheduled", chosen["start"], chosen["end"], now)
            return self._plan_detail(connection, plan_id)

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        return self._plan_detail(self.connection, plan_id)

    def list_plans(self, *, zone_id: int | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if zone_id is not None:
            clauses.append("zone_id=?")
            values.append(zone_id)
        if status:
            clauses.append("status=?")
            values.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        values.append(max(1, min(limit, 500)))
        rows = self.connection.execute(
            "SELECT * FROM wetland_plans" + where + " ORDER BY priority DESC,window_start,id DESC LIMIT ?", values
        ).fetchall()
        return [dict(row) for row in rows]

    def postpone_plan(self, plan_id: int, actor: str, reason: str, today_value: date, *, force: bool = False, idempotency_key: str | None = None) -> dict[str, Any]:
        """因临时降雨等原因延期：以注入日期为新起点重新计算，生成新版本历史。重复请求幂等。

        冲突时先把候选方案持久化保留（不回滚），再返回 409 与候选明细。
        """
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            plan = self._require_plan(connection, plan_id)
            if idempotency_key is not None:
                replayed = self._replayed_intervention(connection, plan_id, "postpone", idempotency_key)
                if replayed is not None:
                    connection.commit()
                    return replayed
            if plan["status"] in {"cancelled", "completed"}:
                raise ValidationError("已取消或已完成的计划不能延期")
            zone = self._require_zone(plan["zone_id"])
            # 降雨当天无法进场作业，重排起点不早于注入日期的次日
            earliest = max(parse_iso_date(plan["earliest_date"]), today_value + timedelta(days=1))
            latest = parse_iso_date(plan["latest_date"])
            duration = int(plan["duration_days"])
            if earliest + timedelta(days=duration - 1) > latest:
                raise ValidationError("延期起点已超出计划最晚完成日期，无法在原范围内重排")
            candidates = self._candidates(zone, plan["work_type_code"], earliest, latest, duration, int(plan["priority"]))
            chosen = next((item for item in candidates if item["feasible"] and item["within_requested_range"]), None)
            if chosen is None:
                forced = next((item for item in candidates if item["feasible"]), None)
                if not force or forced is None:
                    # 保留本轮候选（版本号沿用当前版本，标记为延期尝试），再提交并报冲突
                    self._persist_candidates(connection, plan_id, int(plan["version"]), candidates,
                                             today_value.isoformat(), attempt_label=f"postpone:{today_value.isoformat()}")
                    payload = [self._candidate_dict(item) for item in candidates]
                    connection.commit()
                    raise ConflictWithCandidates("延期后在请求范围内没有可执行窗口，候选方案已保留", candidates=payload)
                chosen = forced
            version = int(plan["version"]) + 1
            now = today_value.isoformat()
            connection.execute(
                "UPDATE wetland_plans SET status='postponed',window_start=?,window_end=?,conflict_count=?,version=?,updated_at=? WHERE id=?",
                (chosen["start"].isoformat(), chosen["end"].isoformat(), len(chosen["conflicts"]), version, now, plan_id),
            )
            self._persist_candidates(connection, plan_id, version, candidates, now)
            self._write_history(connection, plan_id, version, "postpone", actor, reason,
                                plan["status"], "postponed", chosen["start"], chosen["end"], now)
            if idempotency_key is not None:
                self._record_intervention_key(connection, plan_id, "postpone", idempotency_key, version, now)
            result = self._plan_detail(connection, plan_id)
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise

    def cancel_plan(self, plan_id: int, actor: str, reason: str, today_value: date, *, idempotency_key: str | None = None) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if idempotency_key is not None:
                replayed = self._replayed_intervention(connection, plan_id, "cancel", idempotency_key)
                if replayed is not None:
                    return replayed
            if plan["status"] == "cancelled":
                # 无幂等键的重复取消同样幂等：直接返回当前状态
                return self._plan_detail(connection, plan_id)
            if plan["status"] == "completed":
                raise ValidationError("已完成的计划不能取消")
            version = int(plan["version"]) + 1
            now = today_value.isoformat()
            connection.execute(
                "UPDATE wetland_plans SET status='cancelled',version=?,updated_at=? WHERE id=?",
                (version, now, plan_id),
            )
            self._write_history(connection, plan_id, version, "cancel", actor, reason,
                                plan["status"], "cancelled", None, None, now)
            if idempotency_key is not None:
                self._record_intervention_key(connection, plan_id, "cancel", idempotency_key, version, now)
            return self._plan_detail(connection, plan_id)

    def complete_plan(self, plan_id: int, actor: str, today_value: date, *, idempotency_key: str | None = None) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            plan = self._require_plan(connection, plan_id)
            if idempotency_key is not None:
                replayed = self._replayed_intervention(connection, plan_id, "complete", idempotency_key)
                if replayed is not None:
                    return replayed
            if plan["status"] == "completed":
                return self._plan_detail(connection, plan_id)
            if plan["status"] == "cancelled":
                raise ValidationError("已取消的计划不能标记完成")
            version = int(plan["version"]) + 1
            now = today_value.isoformat()
            connection.execute(
                "UPDATE wetland_plans SET status='completed',version=?,updated_at=? WHERE id=?",
                (version, now, plan_id),
            )
            self._write_history(connection, plan_id, version, "complete", actor, "作业完成",
                                plan["status"], "completed", plan["window_start"], plan["window_end"], now)
            if idempotency_key is not None:
                self._record_intervention_key(connection, plan_id, "complete", idempotency_key, version, now)
            return self._plan_detail(connection, plan_id)

    def history(self, plan_id: int) -> list[dict[str, Any]]:
        self._require_plan(self.connection, plan_id)
        rows = self.connection.execute("SELECT * FROM wetland_plan_history WHERE plan_id=? ORDER BY version", (plan_id,)).fetchall()
        return [dict(row) for row in rows]

    def recover(self, today_value: date) -> dict[str, Any]:
        """服务重启后恢复：扫描计划并报告持久化状态，无需内存调度。"""
        rows = self.connection.execute("SELECT id,status,window_start,window_end,version FROM wetland_plans ORDER BY id").fetchall()
        plans = [dict(row) for row in rows]
        overdue = [p["id"] for p in plans if p["status"] in {"scheduled", "postponed"} and p["window_end"] < today_value.isoformat()]
        return {
            "recovered_at": today_value.isoformat(),
            "total_plans": len(plans),
            "by_status": {status: sum(1 for p in plans if p["status"] == status) for status in PLAN_STATUSES},
            "overdue_open_plans": overdue,
            "plans": plans,
        }

    # ------------------------------------------------------------------ 计算引擎

    def _candidates(self, zone: sqlite3.Row, work_type_code: str, earliest: date, latest: date, duration: int, priority: int) -> list[dict[str, Any]]:
        rules = [dict(row) for row in self.connection.execute(
            "SELECT * FROM wetland_sensitive_rules WHERE active=1 AND zone_id=? AND (work_type_code IS NULL OR work_type_code=?) ORDER BY id",
            (zone["id"], work_type_code),
        ).fetchall()]
        horizon_end = self._extend_horizon(earliest, latest, duration)
        max_starts = 60
        candidates: list[dict[str, Any]] = []
        start = earliest
        seen: set[date] = set()
        while start + timedelta(days=duration - 1) <= horizon_end and len(seen) < max_starts:
            if start not in seen:
                seen.add(start)
                end = start + timedelta(days=duration - 1)
                conflicts = self._conflicts_for_window(rules, work_type_code, start, end)
                blocked = any(item["severity"] == "forbid" for item in conflicts)
                within = end <= latest
                candidates.append({
                    "start": start,
                    "end": end,
                    "within_requested_range": within,
                    # 可执行 = 未命中严禁敏感期；是否落在请求日期范围是独立维度（force 延期可采用范围外候选）
                    "feasible": not blocked,
                    "severity_blocked": blocked,
                    "priority": priority,
                    "conflicts": conflicts,
                })
            start += timedelta(days=1)
        # 请求范围内可执行优先，其次范围外可执行，再按冲突少、开始早排序；不可执行候选保留在后
        candidates.sort(key=lambda item: (
            item["severity_blocked"],
            not item["within_requested_range"],
            len(item["conflicts"]),
            item["start"],
        ))
        for rank, item in enumerate(candidates, start=1):
            item["rank"] = rank
        return candidates

    @staticmethod
    def _extend_horizon(earliest: date, latest: date, duration: int) -> date:
        # 范围内无解时额外向后探测一周，保留“范围外但可执行”的候选
        return latest + timedelta(days=max(7, duration))

    def _conflicts_for_window(self, rules: list[dict[str, Any]], work_type_code: str, start: date, end: date) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        cursor_date = start
        while cursor_date <= end:
            for rule in rules:
                if self._rule_active_on(rule, cursor_date):
                    conflicts.append({
                        "rule_id": rule["id"],
                        "species_name": rule["species_name"],
                        "severity": rule["severity"],
                        "date": cursor_date.isoformat(),
                        "reason": rule["reason"] or f"{rule['species_name']}敏感期",
                    })
            cursor_date += timedelta(days=1)
        # 同一规则在窗口内可能命中多天，去重后保留命中日期
        merged: dict[tuple[int, str], dict[str, Any]] = {}
        for item in conflicts:
            key = (item["rule_id"], item["severity"])
            if key not in merged:
                merged[key] = {**item, "dates": [item["date"]]}
            else:
                merged[key]["dates"].append(item["date"])
        result = list(merged.values())
        for item in result:
            item.pop("date", None)
        result.sort(key=lambda item: (item["rule_id"], item["dates"][0]))
        return result

    @staticmethod
    def _rule_active_on(rule: dict[str, Any], day_value: date) -> bool:
        if rule["period_type"] == "annual":
            start = parse_day(rule["start_day"])
            end = parse_day(rule["end_day"])
            day_tuple = (day_value.month, day_value.day)
            return day_in_window(day_tuple, start, end)
        start_date = date.fromisoformat(rule["start_date"])
        end_date = date.fromisoformat(rule["end_date"])
        return start_date <= day_value <= end_date

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _validate_span(earliest: date, latest: date, duration: int) -> None:
        if duration < 1:
            raise ValidationError("作业持续天数至少为 1")
        if latest < earliest:
            raise ValidationError("最晚完成日期不能早于最早开始日期")
        if earliest + timedelta(days=duration - 1) > latest:
            raise ValidationError("作业持续天数超出允许的日期范围")

    def _require_zone(self, zone_id: int) -> sqlite3.Row:
        zone = self.connection.execute("SELECT * FROM wetland_zones WHERE id=? AND active=1", (zone_id,)).fetchone()
        if zone is None:
            raise NotFoundError("区域不存在或已停用")
        return zone

    def _require_work_type(self, code: str) -> None:
        row = self.connection.execute("SELECT 1 FROM wetland_work_types WHERE code=? AND active=1", (code,)).fetchone()
        if row is None:
            raise ValidationError("作业类型不存在或已停用")

    def _require_plan(self, connection: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        plan = connection.execute("SELECT * FROM wetland_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("维护计划不存在")
        return plan

    @staticmethod
    def _zone(connection: sqlite3.Connection, zone_id: int) -> dict[str, Any]:
        return dict(connection.execute("SELECT * FROM wetland_zones WHERE id=?", (zone_id,)).fetchone())

    @staticmethod
    def _rule(connection: sqlite3.Connection, rule_id: int) -> dict[str, Any]:
        return dict(connection.execute("SELECT * FROM wetland_sensitive_rules WHERE id=?", (rule_id,)).fetchone())

    @staticmethod
    def _request_digest(payload: dict[str, Any]) -> str:
        material = {key: payload[key] for key in sorted(payload) if key != "idempotency_key"}
        text = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def _persist_candidates(self, connection: sqlite3.Connection, plan_id: int, version: int, candidates: list[dict[str, Any]], now: str, *, attempt_label: str = "") -> None:
        connection.execute(
            "DELETE FROM wetland_plan_candidates WHERE plan_id=? AND plan_version=? AND attempt_label=?",
            (plan_id, version, attempt_label),
        )
        for item in candidates:
            connection.execute(
                "INSERT INTO wetland_plan_candidates(plan_id,plan_version,attempt_label,rank,start_date,end_date,feasible,reason,conflict_rules_json,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plan_id, version, attempt_label, item["rank"], item["start"].isoformat(), item["end"].isoformat(),
                 1 if item["feasible"] else 0, "" if item["feasible"] else self._candidate_reason(item),
                 json.dumps(item["conflicts"], ensure_ascii=False), now),
            )

    @staticmethod
    def _candidate_reason(item: dict[str, Any]) -> str:
        if item["severity_blocked"]:
            forbidden = [c["species_name"] for c in item["conflicts"] if c["severity"] == "forbid"]
            return "命中严禁作业敏感期：" + "、".join(dict.fromkeys(forbidden))
        if not item["within_requested_range"]:
            return "可执行，但候选窗口落在请求日期范围之外"
        return "存在避让级别冲突"

    @staticmethod
    def _candidate_dict(item: dict[str, Any]) -> dict[str, Any]:
        note = ""
        if item["severity_blocked"]:
            note = WetlandsService._candidate_reason(item)
        elif not item["within_requested_range"]:
            note = "可执行，但候选窗口落在请求日期范围之外"
        elif item["conflicts"]:
            note = "存在避让级别冲突，作业前需复核"
        return {
            "rank": item["rank"],
            "start_date": item["start"].isoformat(),
            "end_date": item["end"].isoformat(),
            "feasible": item["feasible"],
            "within_requested_range": item["within_requested_range"],
            "conflict_count": len(item["conflicts"]),
            "conflicts": item["conflicts"],
            "reason": note,
        }

    def _replayed_intervention(self, connection: sqlite3.Connection, plan_id: int, action: str, key: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT version FROM wetland_intervention_keys WHERE plan_id=? AND action=? AND idempotency_key=?",
            (plan_id, action, key),
        ).fetchone()
        if row is None:
            return None
        return self._plan_detail(connection, plan_id)

    def _record_intervention_key(self, connection: sqlite3.Connection, plan_id: int, action: str, key: str, version: int, now: str) -> None:
        connection.execute(
            "INSERT INTO wetland_intervention_keys(plan_id,action,idempotency_key,version,created_at) VALUES(?,?,?,?,?)",
            (plan_id, action, key, version, now),
        )

    def _write_history(self, connection: sqlite3.Connection, plan_id: int, version: int, action: str, actor: str,
                       reason: str, from_status: str | None, to_status: str,
                       window_start: date | None, window_end: date | None, now: str) -> None:
        snapshot = {
            "status": to_status,
            "window_start": window_start.isoformat() if window_start else None,
            "window_end": window_end.isoformat() if window_end else None,
        }
        connection.execute(
            "INSERT INTO wetland_plan_history(plan_id,version,action,actor,reason,from_status,to_status,window_start,window_end,snapshot_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, version, action, actor, reason, from_status, to_status,
             snapshot["window_start"], snapshot["window_end"], json.dumps(snapshot, ensure_ascii=False), now),
        )

    def _plan_detail(self, connection: sqlite3.Connection, plan_id: int) -> dict[str, Any]:
        plan = connection.execute("SELECT * FROM wetland_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            raise NotFoundError("维护计划不存在")
        result = dict(plan)
        result["candidates"] = [
            dict(row) for row in connection.execute(
                "SELECT * FROM wetland_plan_candidates WHERE plan_id=? ORDER BY plan_version DESC, attempt_label, rank", (plan_id,)
            ).fetchall()
        ]
        for candidate in result["candidates"]:
            candidate["conflict_rules"] = json.loads(candidate.pop("conflict_rules_json"))
        result["history"] = [
            dict(row) for row in connection.execute(
                "SELECT * FROM wetland_plan_history WHERE plan_id=? ORDER BY version", (plan_id,)
            ).fetchall()
        ]
        return result


class ConflictWithCandidates(ConflictError):
    """编排冲突：携带保留下来的候选方案与原因。"""

    def __init__(self, message: str, *, candidates: list[dict[str, Any]]) -> None:
        super().__init__(message, context={"candidates": candidates})
        self.candidates = candidates
