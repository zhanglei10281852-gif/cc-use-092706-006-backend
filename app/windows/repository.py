from __future__ import annotations

import json
import sqlite3
from typing import Any


class WindowRepository:
    """封装维护窗口编排领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 区域与作业类型 ----

    def zone_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM maintenance_zones WHERE code=?", (code,)).fetchone()

    def zone_by_id(self, zone_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM maintenance_zones WHERE id=?", (zone_id,)).fetchone()

    def list_zones(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM maintenance_zones ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    def create_zone(self, *, code: str, name: str, description: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO maintenance_zones(code,name,description,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (code, name, description, actor, now, now),
        )
        return dict(self.zone_by_id(cursor.lastrowid))

    def work_type_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM work_types WHERE code=?", (code,)).fetchone()

    def work_type_by_id(self, work_type_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM work_types WHERE id=?", (work_type_id,)).fetchone()

    def list_work_types(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM work_types ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    def create_work_type(self, *, code: str, name: str, description: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO work_types(code,name,description,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (code, name, description, actor, now, now),
        )
        return dict(self.work_type_by_id(cursor.lastrowid))

    # ---- 物种敏感期规则 ----

    def period_by_id(self, period_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM sensitive_periods WHERE id=?", (period_id,)).fetchone()

    def period_view(self, period_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT p.*,z.code AS zone_code FROM sensitive_periods p LEFT JOIN maintenance_zones z ON z.id=p.zone_id WHERE p.id=?",
            (period_id,),
        ).fetchone()
        return self._period_view(row) if row else None

    def create_period(self, *, zone_id: int | None, species: str, period_kind: str, start_month_day: str, end_month_day: str, blocked_work_types: list[str], note: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO sensitive_periods(zone_id,species,period_kind,start_month_day,end_month_day,blocked_work_types_json,note,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (zone_id, species, period_kind, start_month_day, end_month_day, json.dumps(sorted(blocked_work_types), ensure_ascii=False), note, actor, now, now),
        )
        return self.period_view(cursor.lastrowid)

    def list_periods(self, *, zone_id: int | None = None, include_inactive: bool = False) -> list[dict[str, Any]]:
        clauses = [] if include_inactive else ["p.active=1"]
        values: list[Any] = []
        if zone_id is not None:
            clauses.append("(p.zone_id IS NULL OR p.zone_id=?)")
            values.append(zone_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT p.*,z.code AS zone_code FROM sensitive_periods p LEFT JOIN maintenance_zones z ON z.id=p.zone_id" + where + " ORDER BY p.id",
            values,
        ).fetchall()
        return [self._period_view(row) for row in rows]

    def active_rules_for_zone(self, zone_id: int) -> list[dict[str, Any]]:
        return self.list_periods(zone_id=zone_id)

    def retire_period(self, period_id: int, *, now: str) -> dict[str, Any] | None:
        cursor = self.connection.execute("UPDATE sensitive_periods SET active=0,updated_at=? WHERE id=? AND active=1", (now, period_id))
        if cursor.rowcount != 1:
            return None
        return self.period_view(period_id)

    @staticmethod
    def _period_view(row: sqlite3.Row) -> dict[str, Any]:
        view = dict(row)
        view["blocked_work_types"] = json.loads(view.pop("blocked_work_types_json"))
        return view

    # ---- 维护计划 ----

    def plan_by_id(self, plan_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT p.*,z.code AS zone_code,w.code AS work_type_code FROM maintenance_plans p "
            "JOIN maintenance_zones z ON z.id=p.zone_id JOIN work_types w ON w.id=p.work_type_id WHERE p.id=?",
            (plan_id,),
        ).fetchone()

    def plan_by_idempotency(self, requested_by: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM maintenance_plans WHERE requested_by=? AND idempotency_key=?",
            (requested_by, key),
        ).fetchone()

    def create_plan(self, *, zone_id: int, work_type_id: int, requested_start: str, requested_end: str, duration_days: int, priority: int, status: str, scheduled_start: str | None, scheduled_end: str | None, evaluation: dict[str, Any], requested_by: str, idempotency_key: str, payload_hash: str, note: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO maintenance_plans(zone_id,work_type_id,requested_start,requested_end,duration_days,priority,status,scheduled_start,scheduled_end,evaluation_json,requested_by,idempotency_key,payload_hash,note,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (zone_id, work_type_id, requested_start, requested_end, duration_days, priority, status, scheduled_start, scheduled_end, json.dumps(evaluation, ensure_ascii=False, sort_keys=True), requested_by, idempotency_key, payload_hash, note, now, now),
        )
        return dict(self.plan_by_id(cursor.lastrowid))

    def apply_evaluation(self, plan_id: int, *, requested_start: str, requested_end: str, status: str, scheduled_start: str | None, scheduled_end: str | None, evaluation: dict[str, Any], now: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE maintenance_plans SET requested_start=?,requested_end=?,status=?,scheduled_start=?,scheduled_end=?,evaluation_json=?,version=version+1,updated_at=? WHERE id=?",
            (requested_start, requested_end, status, scheduled_start, scheduled_end, json.dumps(evaluation, ensure_ascii=False, sort_keys=True), now, plan_id),
        )
        return dict(self.plan_by_id(plan_id))

    def cancel_plan(self, plan_id: int, *, now: str) -> dict[str, Any]:
        self.connection.execute(
            "UPDATE maintenance_plans SET status='cancelled',version=version+1,updated_at=? WHERE id=?",
            (now, plan_id),
        )
        return dict(self.plan_by_id(plan_id))

    def list_plans(self, *, status: str | None, zone_id: int | None, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("p.status=?")
            values.append(status)
        if zone_id is not None:
            clauses.append("p.zone_id=?")
            values.append(zone_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT p.*,z.code AS zone_code,w.code AS work_type_code FROM maintenance_plans p "
            "JOIN maintenance_zones z ON z.id=p.zone_id JOIN work_types w ON w.id=p.work_type_id"
            + where + " ORDER BY p.priority DESC,p.created_at DESC,p.id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 版本化历史 ----

    def add_revision(self, *, plan_id: int, version: int, action: str, reason: str, cause: str, actor: str, request_key: str, request_hash: str, before: dict[str, Any], after: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO plan_revisions(plan_id,version,action,reason,cause,actor,request_key,request_hash,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, version, action, reason, cause, actor, request_key, request_hash, json.dumps(before, ensure_ascii=False, sort_keys=True), json.dumps(after, ensure_ascii=False, sort_keys=True), now),
        )

    def revision_by_key(self, plan_id: int, action: str, request_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM plan_revisions WHERE plan_id=? AND action=? AND request_key=?",
            (plan_id, action, request_key),
        ).fetchone()

    def revisions_for_plan(self, plan_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM plan_revisions WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
        return [dict(row) for row in rows]
