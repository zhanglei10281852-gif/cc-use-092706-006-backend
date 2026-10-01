from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient


def register_zone(client, headers, code="reed-bed", name="芦苇荡"):
    response = client.post("/api/windows/zones", headers=headers, json={"code": code, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def register_work_type(client, headers, code="harvest", name="收割"):
    response = client.post("/api/windows/work-types", headers=headers, json={"code": code, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def register_period(client, headers, **overrides):
    payload = {
        "zone_code": "reed-bed",
        "species": "震旦鸦雀",
        "period_kind": "breeding",
        "start_month_day": "04-01",
        "end_month_day": "05-15",
        "blocked_work_types": ["harvest"],
    }
    payload.update(overrides)
    response = client.post("/api/windows/sensitive-periods", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def plan_payload(**overrides):
    payload = {
        "zone_code": "reed-bed",
        "work_type_code": "harvest",
        "requested_start": "2026-03-28",
        "requested_end": "2026-04-10",
        "duration_days": 3,
        "priority": 50,
        "idempotency_key": "plan-000001",
        "as_of": "2026-03-01",
    }
    payload.update(overrides)
    return payload


def create_user(client, admin, *, username: str, role_code: str, permissions: list[str]) -> dict:
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": role_code, "name": role_code, "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": "Viewer!23456", "display_name": username, "role_codes": [role_code]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Viewer!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def test_cross_month_window_schedules_before_breeding_period(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])
    register_period(client, admin["headers"])

    created = client.post("/api/windows/plans", headers=admin["headers"], json=plan_payload())
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["replayed"] is False
    assert plan["status"] == "scheduled"
    # 跨月：3 月 28 日至 4 月 10 日的期望区间被 4 月繁殖期截断
    assert plan["scheduled_start"] == "2026-03-28"
    assert plan["scheduled_end"] == "2026-03-30"
    evaluation = plan["evaluation"]
    assert evaluation["feasible"] is True
    assert evaluation["windows"] == [{"start": "2026-03-28", "end": "2026-03-31", "days": 4, "fits": True}]
    assert evaluation["candidates"] == []
    assert len(evaluation["conflicts"]) == 1
    conflict = evaluation["conflicts"][0]
    assert conflict["species"] == "震旦鸦雀"
    assert conflict["period_kind"] == "breeding"
    assert conflict["segments"] == [{"start": "2026-04-01", "end": "2026-04-10"}]
    assert "繁殖" in conflict["reason"]


def test_overlapping_rules_fully_block_and_keep_candidates(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])
    register_period(client, admin["headers"])  # 繁殖期 04-01~05-15 禁收割
    register_period(
        client,
        admin["headers"],
        species="东方白鹳",
        period_kind="migration",
        start_month_day="04-15",
        end_month_day="05-31",
        blocked_work_types=[],
    )  # 迁徙期 04-15~05-31 禁一切作业

    created = client.post(
        "/api/windows/plans",
        headers=admin["headers"],
        json=plan_payload(requested_start="2026-04-10", requested_end="2026-05-05", duration_days=2, as_of="2026-04-01"),
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["status"] == "blocked"
    assert plan["scheduled_start"] is None
    evaluation = plan["evaluation"]
    assert evaluation["feasible"] is False
    assert evaluation["windows"] == []
    # 两条重叠规则分别给出冲突原因与被禁日期段
    assert [item["species"] for item in evaluation["conflicts"]] == ["震旦鸦雀", "东方白鹳"]
    assert evaluation["conflicts"][0]["segments"] == [{"start": "2026-04-10", "end": "2026-05-05"}]
    assert evaluation["conflicts"][1]["segments"] == [{"start": "2026-04-15", "end": "2026-05-05"}]
    assert "迁徙" in evaluation["conflicts"][1]["reason"]
    # 重叠规则取并集后，最早候选窗口为 6 月 1 日起
    assert evaluation["candidates"][0]["start"] == "2026-06-01"
    assert evaluation["candidates"][0]["end"] == "2026-06-02"
    assert evaluation["candidates"][0]["days"] == 2


def test_year_wrap_global_rule_and_retire(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])
    period = register_period(
        client,
        admin["headers"],
        zone_code=None,
        species="雁鸭类",
        period_kind="migration",
        start_month_day="11-01",
        end_month_day="02-15",
        blocked_work_types=[],
    )
    assert period["zone_code"] is None

    blocked = client.post(
        "/api/windows/compute",
        headers=admin["headers"],
        json={"zone_code": "reed-bed", "work_type_code": "harvest", "start": "2026-01-10", "end": "2026-01-20", "duration_days": 2, "as_of": "2026-01-05"},
    )
    assert blocked.status_code == 200, blocked.text
    evaluation = blocked.json()
    assert evaluation["feasible"] is False
    assert evaluation["conflicts"][0]["zone_code"] is None
    assert evaluation["candidates"][0]["start"] == "2026-02-16"

    free = client.post(
        "/api/windows/compute",
        headers=admin["headers"],
        json={"zone_code": "reed-bed", "work_type_code": "harvest", "start": "2026-02-20", "end": "2026-02-25", "duration_days": 2, "as_of": "2026-02-18"},
    )
    assert free.json()["feasible"] is True
    assert free.json()["conflicts"] == []

    retired = client.delete(f"/api/windows/sensitive-periods/{period['id']}", headers=admin["headers"])
    assert retired.status_code == 200 and retired.json()["active"] == 0
    unblocked = client.post(
        "/api/windows/compute",
        headers=admin["headers"],
        json={"zone_code": "reed-bed", "work_type_code": "harvest", "start": "2026-01-10", "end": "2026-01-20", "duration_days": 2, "as_of": "2026-01-05"},
    )
    assert unblocked.json()["feasible"] is True


def test_rain_postpone_is_versioned_and_idempotent(client, admin):
    register_zone(client, admin["headers"], code="mudflat", name="滩涂")
    register_work_type(client, admin["headers"], code="dredging", name="清淤")
    created = client.post(
        "/api/windows/plans",
        headers=admin["headers"],
        json=plan_payload(zone_code="mudflat", work_type_code="dredging", requested_start="2026-06-01", requested_end="2026-06-10", as_of="2026-05-20"),
    ).json()
    assert created["status"] == "scheduled" and created["version"] == 1

    postpone = {
        "reason": "强降雨导致滩涂积水",
        "cause": "rain",
        "shift_days": 5,
        "idempotency_key": "rain-delay-0001",
        "as_of": "2026-06-02",
    }
    moved = client.post(f"/api/windows/plans/{created['id']}/postpone", headers=admin["headers"], json=postpone)
    assert moved.status_code == 200, moved.text
    assert moved.json()["replayed"] is False
    assert moved.json()["version"] == 2
    assert moved.json()["requested_start"] == "2026-06-06"
    assert moved.json()["scheduled_start"] == "2026-06-06"
    assert moved.json()["scheduled_end"] == "2026-06-08"

    # 重复请求幂等：版本与历史不变
    replay = client.post(f"/api/windows/plans/{created['id']}/postpone", headers=admin["headers"], json=postpone)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["version"] == 2

    # 同一幂等键不能对应不同请求
    conflict = client.post(
        f"/api/windows/plans/{created['id']}/postpone",
        headers=admin["headers"],
        json=postpone | {"shift_days": 7},
    )
    assert conflict.status_code == 409

    detail = client.get(f"/api/windows/plans/{created['id']}", headers=admin["headers"]).json()
    assert [item["action"] for item in detail["revisions"]] == ["create", "postpone"]
    assert detail["revisions"][1]["cause"] == "rain"
    assert detail["revisions"][1]["reason"] == "强降雨导致滩涂积水"
    assert detail["revisions"][1]["version"] == 2


def test_postpone_into_conflict_keeps_candidates(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])
    register_period(client, admin["headers"])
    created = client.post(
        "/api/windows/plans",
        headers=admin["headers"],
        json=plan_payload(requested_start="2026-03-20", requested_end="2026-03-31", duration_days=2),
    ).json()
    assert created["status"] == "scheduled"

    moved = client.post(
        f"/api/windows/plans/{created['id']}/postpone",
        headers=admin["headers"],
        json={
            "reason": "设备检修，改期",
            "cause": "operational",
            "new_start": "2026-04-05",
            "new_end": "2026-04-12",
            "idempotency_key": "postpone-into-0001",
            "as_of": "2026-04-01",
        },
    )
    assert moved.status_code == 200, moved.text
    plan = moved.json()
    assert plan["status"] == "blocked"
    assert plan["scheduled_start"] is None
    assert plan["evaluation"]["conflicts"][0]["species"] == "震旦鸦雀"
    assert plan["evaluation"]["candidates"][0]["start"] == "2026-05-16"


def test_cancel_is_versioned_idempotent_and_guards_state(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])
    created = client.post("/api/windows/plans", headers=admin["headers"], json=plan_payload()).json()

    cancel = {"reason": "年度预算调整", "idempotency_key": "cancel-0001"}
    cancelled = client.post(f"/api/windows/plans/{created['id']}/cancel", headers=admin["headers"], json=cancel)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["version"] == 2

    replay = client.post(f"/api/windows/plans/{created['id']}/cancel", headers=admin["headers"], json=cancel)
    assert replay.status_code == 200 and replay.json()["replayed"] is True

    again = client.post(
        f"/api/windows/plans/{created['id']}/cancel",
        headers=admin["headers"],
        json={"reason": "重复取消", "idempotency_key": "cancel-0002"},
    )
    assert again.status_code == 409

    postponed = client.post(
        f"/api/windows/plans/{created['id']}/postpone",
        headers=admin["headers"],
        json={"reason": "尝试改期", "shift_days": 3, "idempotency_key": "postpone-cancelled-1"},
    )
    assert postponed.status_code == 409

    detail = client.get(f"/api/windows/plans/{created['id']}", headers=admin["headers"]).json()
    assert [item["action"] for item in detail["revisions"]] == ["create", "cancel"]


def test_create_plan_idempotency_and_priority_ordering(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])
    first = client.post("/api/windows/plans", headers=admin["headers"], json=plan_payload(priority=30))
    second = client.post("/api/windows/plans", headers=admin["headers"], json=plan_payload(priority=30))
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert second.json()["replayed"] is True

    changed = client.post(
        "/api/windows/plans",
        headers=admin["headers"],
        json=plan_payload(priority=30, requested_end="2026-04-12"),
    )
    assert changed.status_code == 409

    urgent = client.post(
        "/api/windows/plans",
        headers=admin["headers"],
        json=plan_payload(idempotency_key="plan-000002", priority=90),
    )
    assert urgent.status_code == 201
    listing = client.get("/api/windows/plans", headers=admin["headers"])
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert [item["priority"] for item in items] == [90, 30]


def test_permission_boundaries(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])

    anonymous = client.get("/api/windows/zones")
    assert anonymous.status_code == 401

    viewer = create_user(client, admin, username="window.viewer", role_code="windows.viewer", permissions=["windows.read"])
    outsider = create_user(client, admin, username="plain.user", role_code="no.windows", permissions=["residents.read"])

    assert client.get("/api/windows/zones", headers=viewer).status_code == 200
    compute = client.post(
        "/api/windows/compute",
        headers=viewer,
        json={"zone_code": "reed-bed", "work_type_code": "harvest", "start": "2026-03-01", "end": "2026-03-10", "duration_days": 1, "as_of": "2026-02-20"},
    )
    assert compute.status_code == 200
    assert client.post("/api/windows/zones", headers=viewer, json={"code": "dry-willow", "name": "旱柳林"}).status_code == 403
    assert client.post("/api/windows/plans", headers=viewer, json=plan_payload(idempotency_key="viewer-000001")).status_code == 403

    assert client.get("/api/windows/zones", headers=outsider).status_code == 403
    assert client.get("/api/windows/plans", headers=outsider).status_code == 403


def test_plan_survives_service_restart(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])
    register_period(client, admin["headers"])
    created = client.post("/api/windows/plans", headers=admin["headers"], json=plan_payload()).json()
    moved = client.post(
        f"/api/windows/plans/{created['id']}/postpone",
        headers=admin["headers"],
        json={"reason": "降雨延期", "cause": "rain", "shift_days": 2, "idempotency_key": "restart-postpone-1", "as_of": "2026-03-01"},
    )
    assert moved.status_code == 200

    # 模拟服务重启：关闭连接后在同一数据库文件上重新装配应用
    from app.database import close_connection
    from app.main import app

    close_connection()
    with TestClient(app) as restarted:
        detail = restarted.get(f"/api/windows/plans/{created['id']}", headers=admin["headers"])
        assert detail.status_code == 200, detail.text
        plan = detail.json()
        assert plan["version"] == 2
        assert plan["requested_start"] == "2026-03-30"
        assert [item["action"] for item in plan["revisions"]] == ["create", "postpone"]
        # 重启后窗口计算与排班仍然可用
        recomputed = restarted.post(
            "/api/windows/compute",
            headers=admin["headers"],
            json={"zone_code": "reed-bed", "work_type_code": "harvest", "start": "2026-03-28", "end": "2026-04-10", "duration_days": 3, "as_of": "2026-03-01"},
        )
        assert recomputed.status_code == 200
        assert recomputed.json()["feasible"] is True
        follow_up = restarted.post(
            "/api/windows/plans",
            headers=admin["headers"],
            json=plan_payload(idempotency_key="plan-after-restart", requested_start="2026-06-01", requested_end="2026-06-10"),
        )
        assert follow_up.status_code == 201
        assert follow_up.json()["status"] == "scheduled"


def test_injected_clock_drives_default_as_of(client):
    from app.core.clock import FrozenClock
    from app.database import get_connection
    from app.windows.service import WindowPlanningService

    clock = FrozenClock(datetime(2026, 3, 1, 8, 0, tzinfo=UTC))
    service = WindowPlanningService(get_connection(), clock)
    service.create_zone({"code": "reed-bed", "name": "芦苇荡", "description": ""}, "tester")
    service.create_work_type({"code": "harvest", "name": "收割", "description": ""}, "tester")
    service.create_period(
        {"zone_code": "reed-bed", "species": "震旦鸦雀", "period_kind": "breeding", "start_month_day": "04-01", "end_month_day": "05-15", "blocked_work_types": ["harvest"], "note": ""},
        "tester",
    )
    plan = service.create_plan(
        {
            "zone_code": "reed-bed",
            "work_type_code": "harvest",
            "requested_start": date(2026, 3, 28),
            "requested_end": date(2026, 4, 10),
            "duration_days": 3,
            "priority": 50,
            "idempotency_key": "clock-test-0001",
            "note": "",
        },
        "tester",
    )
    assert plan["evaluation"]["as_of"] == "2026-03-01"
    assert plan["scheduled_start"] == "2026-03-28"

    clock.advance(days=40)
    moved = service.postpone_plan(
        plan["id"],
        {"reason": "降雨", "cause": "rain", "shift_days": 60, "new_start": None, "new_end": None, "idempotency_key": "clock-postpone-1"},
        "tester",
    )
    assert moved["evaluation"]["as_of"] == "2026-04-10"
    assert moved["status"] == "scheduled"
    assert moved["scheduled_start"] == "2026-05-27"


def test_registration_validation(client, admin):
    register_zone(client, admin["headers"])
    register_work_type(client, admin["headers"])

    duplicate = client.post("/api/windows/zones", headers=admin["headers"], json={"code": "reed-bed", "name": "重复区域"})
    assert duplicate.status_code == 409

    bad_month = client.post(
        "/api/windows/sensitive-periods",
        headers=admin["headers"],
        json={"zone_code": "reed-bed", "species": "x", "period_kind": "other", "start_month_day": "13-01", "end_month_day": "12-01"},
    )
    assert bad_month.status_code == 422

    bad_day = client.post(
        "/api/windows/sensitive-periods",
        headers=admin["headers"],
        json={"zone_code": "reed-bed", "species": "x", "period_kind": "other", "start_month_day": "02-30", "end_month_day": "03-01"},
    )
    assert bad_day.status_code == 422

    unknown_work = client.post(
        "/api/windows/sensitive-periods",
        headers=admin["headers"],
        json={"zone_code": "reed-bed", "species": "x", "period_kind": "other", "start_month_day": "02-01", "end_month_day": "03-01", "blocked_work_types": ["unknown"]},
    )
    assert unknown_work.status_code == 422

    unknown_zone = client.post("/api/windows/plans", headers=admin["headers"], json=plan_payload(zone_code="nowhere"))
    assert unknown_zone.status_code == 404

    inverted = client.post(
        "/api/windows/compute",
        headers=admin["headers"],
        json={"zone_code": "reed-bed", "work_type_code": "harvest", "start": "2026-04-10", "end": "2026-04-01", "duration_days": 1},
    )
    assert inverted.status_code == 422

    missing = client.get("/api/windows/plans/9999", headers=admin["headers"])
    assert missing.status_code == 404
