from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import close_connection
from app.main import app


# ---------------------------------------------------------------------- 辅助函数

def make_zone(client, headers, code="reed-north", name="北区芦苇荡", zone_type="reed"):
    resp = client.post("/api/wetlands/zones", json={"code": code, "name": name, "zone_type": zone_type}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def add_rule(client, headers, zone_id, **rule):
    payload = {"zone_id": zone_id, "species_name": "东方大苇莺", "period_type": "annual",
               "start_day": "04-01", "end_day": "04-30", "severity": "forbid"}
    payload.update(rule)
    resp = client.post("/api/wetlands/rules", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def schedule(client, headers, zone_id, key, *, earliest="2026-04-24", latest="2026-05-31",
             duration=3, work="reed_cutting", title="芦苇收割", priority=50, as_of=None):
    extra = {"X-As-Of-Date": as_of} if as_of else {}
    return client.post(
        "/api/wetlands/plans",
        json={"zone_id": zone_id, "work_type_code": work, "title": title, "priority": priority,
              "earliest_date": earliest, "latest_date": latest, "duration_days": duration,
              "idempotency_key": key},
        headers={**headers, **extra},
    )


# ---------------------------------------------------------------------- 基础资料

def test_register_zone_worktype_and_rules(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h)
    assert zone["zone_type"] == "reed"
    duplicate = client.post("/api/wetlands/zones", json={"code": "reed-north", "name": "重名", "zone_type": "reed"}, headers=h)
    assert duplicate.status_code == 409

    work_types = client.get("/api/wetlands/work-types", headers=h).json()["items"]
    assert {w["code"] for w in work_types} >= {"reed_cutting", "willow_thinning", "mudflat_cleanup", "patrol"}

    rule = add_rule(client, h, zone["id"], species_name="震旦鸦雀", start_day="04-01", end_day="04-30")
    assert rule["severity"] == "forbid"
    rules = client.get(f"/api/wetlands/rules?zone_id={zone['id']}", headers=h).json()["items"]
    assert len(rules) == 1

    bad = client.post("/api/wetlands/rules", json={
        "zone_id": 9999, "species_name": "不存在区域", "period_type": "annual",
        "start_day": "04-01", "end_day": "04-30",
    }, headers=h)
    assert bad.status_code == 404


# ---------------------------------------------------------------------- 跨月窗口

def test_cross_month_window_is_chosen(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="willow-west", name="西堤旱柳", zone_type="dryland_willow")
    # 越冬鸟类严禁期覆盖 1-2 月，到 02-27 结束
    add_rule(client, h, zone["id"], species_name="越冬雁鸭", work_type_code="willow_thinning",
             start_day="01-01", end_day="02-27")
    resp = schedule(client, h, zone["id"], "plan-cross-month", work="willow_thinning",
                    earliest="2026-02-20", latest="2026-03-31", duration=5)
    assert resp.status_code == 201, resp.text
    plan = resp.json()
    # 最早可执行窗口 02-28 开始，持续 5 天，跨越 2/3 月
    assert plan["window_start"] == "2026-02-28"
    assert plan["window_end"] == "2026-03-04"
    assert plan["status"] == "scheduled"


# ---------------------------------------------------------------------- 重叠敏感期规则

def test_overlapping_rules_are_all_reported_and_forbid_dominates(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="mud-east", name="东滩涂", zone_type="mudflat")
    # 规则一：周年 4 月全月严禁（夏候鸟繁殖）
    add_rule(client, h, zone["id"], species_name="繁殖候鸟群", work_type_code="mudflat_cleanup",
             start_day="04-01", end_day="04-30", severity="forbid")
    # 规则二：固定窗口 04-10~04-20 仅建议避让，与规则一重叠
    add_rule(client, h, zone["id"], species_name="过境鸻鹬", work_type_code="mudflat_cleanup",
             period_type="fixed", start_day=None, end_day=None,
             start_date="2026-04-10", end_date="2026-04-20", severity="avoid")
    # 规则三：跨年周年规则 11-01 ~ 02-28 越冬期
    add_rule(client, h, zone["id"], species_name="越冬鹤类", work_type_code="mudflat_cleanup",
             start_day="11-01", end_day="02-28", severity="forbid")

    preview = client.post("/api/wetlands/windows/preview", json={
        "zone_id": zone["id"], "work_type_code": "mudflat_cleanup",
        "earliest_date": "2026-04-01", "latest_date": "2026-04-20", "duration_days": 2,
    }, headers=h)
    assert preview.status_code == 200
    blocked = [c for c in preview.json()["candidates"] if not c["feasible"]]
    assert blocked, "4 月中的窗口应全部被严禁规则阻断"
    mid = next(c for c in blocked if c["start_date"] == "2026-04-15")
    species = {rule["species_name"] for rule in mid["conflicts"]}
    # 重叠的两条规则都要出现在原因里，严禁与避让同时列出
    assert {"繁殖候鸟群", "过境鸻鹬"} <= species
    severities = {rule["severity"] for rule in mid["conflicts"]}
    assert severities == {"forbid", "avoid"}

    # 跨年规则：1 月阻断，10 月放行
    jan = client.post("/api/wetlands/windows/preview", json={
        "zone_id": zone["id"], "work_type_code": "mudflat_cleanup",
        "earliest_date": "2026-01-10", "latest_date": "2026-01-20", "duration_days": 2,
    }, headers=h).json()
    assert all(not c["feasible"] for c in jan["candidates"])
    october = client.post("/api/wetlands/windows/preview", json={
        "zone_id": zone["id"], "work_type_code": "mudflat_cleanup",
        "earliest_date": "2026-10-10", "latest_date": "2026-10-20", "duration_days": 2,
    }, headers=h).json()
    assert any(c["feasible"] for c in october["candidates"])


def test_conflict_returns_reason_and_preserves_candidates(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="reed-south", name="南芦苇区")
    # 严禁期 04-01~04-22，请求范围 04-01~04-20：范围内无窗口，04-23 起范围外可执行
    add_rule(client, h, zone["id"], start_day="04-01", end_day="04-22")
    resp = schedule(client, h, zone["id"], "plan-conflict", earliest="2026-04-01",
                    latest="2026-04-20", duration=3)
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert "可执行窗口" in body["message"]
    candidates = body["context"]["candidates"]
    assert candidates, "冲突时必须保留候选方案"
    assert any(c["feasible"] and not c["within_requested_range"] and c["start_date"] >= "2026-04-23"
               for c in candidates)
    blocked = next(c for c in candidates if not c["feasible"])
    assert "敏感期" in blocked["reason"]


# ---------------------------------------------------------------------- 降雨延期、版本历史与幂等

def test_rainfall_postpone_versions_history_and_idempotency(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="reed-rain", name="雨后芦苇区")
    add_rule(client, h, zone["id"], start_day="04-01", end_day="04-20")
    created = schedule(client, h, zone["id"], "plan-rain", earliest="2026-04-21",
                       latest="2026-05-31", duration=3, as_of="2026-04-15")
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["window_start"] == "2026-04-21"
    assert plan["version"] == 1

    # 降雨当天延期：以 04-21 为新起点重排
    first = client.post(f"/api/wetlands/plans/{plan['id']}/postpone",
                        json={"today": "2026-04-21", "reason": "临时降雨，作业面泥泞"}, headers=h)
    assert first.status_code == 200, first.text
    postponed = first.json()
    assert postponed["status"] == "postponed"
    assert postponed["window_start"] == "2026-04-22"
    assert postponed["version"] == 2
    versions = [item["version"] for item in postponed["history"]]
    assert versions == [1, 2]
    assert postponed["history"][1]["action"] == "postpone"
    assert "降雨" in postponed["history"][1]["reason"]
    # 两个版本各自保留候选方案
    candidate_versions = {c["plan_version"] for c in postponed["candidates"]}
    assert candidate_versions == {1, 2}

    # 重复延期请求（带幂等键）必须幂等：不再产生新版本
    repeat1 = client.post(f"/api/wetlands/plans/{plan['id']}/postpone",
                          json={"today": "2026-04-23", "reason": "再次降雨"},
                          headers={**h, "Idempotency-Key": "rain-delay-001"})
    repeat2 = client.post(f"/api/wetlands/plans/{plan['id']}/postpone",
                          json={"today": "2026-04-23", "reason": "再次降雨"},
                          headers={**h, "Idempotency-Key": "rain-delay-001"})
    assert repeat1.status_code == repeat2.status_code == 200
    assert repeat1.json()["version"] == repeat2.json()["version"] == 3
    detail = client.get(f"/api/wetlands/plans/{plan['id']}", headers=h).json()
    assert [item["version"] for item in detail["history"]] == [1, 2, 3]

    # 历史接口可独立查询
    history = client.get(f"/api/wetlands/plans/{plan['id']}/history", headers=h).json()["items"]
    assert [item["action"] for item in history] == ["schedule", "postpone", "postpone"]


def test_schedule_idempotency_rejects_same_key_different_body(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="reed-idem", name="幂等芦苇区")
    first = schedule(client, h, zone["id"], "same-key-0001", earliest="2026-06-01", latest="2026-06-30")
    second = schedule(client, h, zone["id"], "same-key-0001", earliest="2026-06-01", latest="2026-06-30")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    conflict = schedule(client, h, zone["id"], "same-key-0001", earliest="2026-07-01", latest="2026-07-31")
    assert conflict.status_code == 409


def test_cancel_is_versioned_idempotent_and_blocks_completion(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="reed-cancel", name="取消芦苇区")
    plan = schedule(client, h, zone["id"], "plan-cancel-1", earliest="2026-06-01",
                    latest="2026-06-30").json()

    first = client.post(f"/api/wetlands/plans/{plan['id']}/cancel",
                        json={"today": "2026-05-30", "reason": "保护区整体管控"}, headers=h)
    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert first.json()["version"] == 2

    # 带幂等键的重复取消返回同一状态，不新增历史；换键重复取消同样幂等
    for key in ("cancel-001", "cancel-001", "cancel-002"):
        resp = client.post(f"/api/wetlands/plans/{plan['id']}/cancel",
                           json={"today": "2026-05-30", "reason": "保护区整体管控"},
                           headers={**h, "Idempotency-Key": key})
        assert resp.status_code == 200
        assert resp.json()["version"] == 2
    detail = client.get(f"/api/wetlands/plans/{plan['id']}", headers=h).json()
    assert [item["action"] for item in detail["history"]] == ["schedule", "cancel"]

    # 已取消不能完成；已取消不能延期
    done = client.post(f"/api/wetlands/plans/{plan['id']}/complete", json={"today": "2026-06-05"}, headers=h)
    assert done.status_code == 422
    delayed = client.post(f"/api/wetlands/plans/{plan['id']}/postpone",
                          json={"today": "2026-06-05", "reason": "降雨"}, headers=h)
    assert delayed.status_code == 422


def test_force_postpone_adopts_out_of_range_candidate(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="reed-force", name="强制延期区")
    # 计划窗口在 05-01~05-31；降雨推迟到 05-20，而 05-20~05-31 仍处严禁期
    add_rule(client, h, zone["id"], start_day="05-01", end_day="05-31")
    plan = schedule(client, h, zone["id"], "plan-force-1", earliest="2026-04-25",
                    latest="2026-05-31", duration=3).json()
    assert plan["window_start"] == "2026-04-25"

    blocked = client.post(f"/api/wetlands/plans/{plan['id']}/postpone",
                          json={"today": "2026-05-20", "reason": "持续降雨"}, headers=h)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["context"]["candidates"]

    forced = client.post(f"/api/wetlands/plans/{plan['id']}/postpone",
                         json={"today": "2026-05-20", "reason": "持续降雨，强制顺延至禁期后", "force": True},
                         headers={**h, "Idempotency-Key": "force-delay-1"})
    assert forced.status_code == 200, forced.text
    assert forced.json()["window_start"] >= "2026-06-01"
    assert forced.json()["window_end"] > "2026-05-31"


# ---------------------------------------------------------------------- 优先级

def test_priority_is_stored_and_reflected_in_listing(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="reed-prio", name="优先级区")
    low = schedule(client, h, zone["id"], "prio-low", priority=10,
                   earliest="2026-07-01", latest="2026-07-31").json()
    high = schedule(client, h, zone["id"], "prio-high", priority=90,
                    earliest="2026-07-01", latest="2026-07-31").json()
    assert low["priority"] == 10 and high["priority"] == 90
    items = client.get("/api/wetlands/plans", headers=h).json()["items"]
    assert items[0]["id"] == high["id"]
    filtered = client.get(f"/api/wetlands/plans?zone_id={zone['id']}&status=scheduled", headers=h).json()["items"]
    assert len(filtered) == 2


# ---------------------------------------------------------------------- 权限边界

def test_permission_boundaries(client, admin):
    # 未认证请求被拒绝
    assert client.get("/api/wetlands/zones").status_code == 401
    assert client.post("/api/wetlands/zones", json={"code": "x", "name": "x", "zone_type": "reed"}).status_code == 401

    h = admin["headers"]
    # 管理员创建只读角色与用户
    role = client.post("/api/roles", json={
        "code": "wetland_viewer", "name": "湿地只读员", "permission_codes": ["wetlands.read"],
    }, headers=h)
    assert role.status_code == 201, role.text
    user = client.post("/api/users", json={
        "username": "viewer1", "password": "Viewer!23456", "display_name": "只读员",
        "role_codes": ["wetland_viewer"],
    }, headers=h)
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": "viewer1", "password": "Viewer!23456"})
    viewer = {"Authorization": f"Bearer {login.json()['token']}"}

    assert client.get("/api/wetlands/zones", headers=viewer).status_code == 200
    denied = client.post("/api/wetlands/zones",
                         json={"code": "zone-by-viewer", "name": "越权", "zone_type": "reed"}, headers=viewer)
    assert denied.status_code == 403
    denied_plan = client.post("/api/wetlands/plans", json={
        "zone_id": 1, "work_type_code": "patrol", "title": "越权计划",
        "earliest_date": "2026-08-01", "latest_date": "2026-08-10", "duration_days": 2,
        "idempotency_key": "denied-000001",
    }, headers=viewer)
    assert denied_plan.status_code == 403


# ---------------------------------------------------------------------- 服务重启恢复

def test_plans_survive_service_restart(client, admin):
    h = admin["headers"]
    zone = make_zone(client, h, code="reed-restart", name="重启恢复区")
    add_rule(client, h, zone["id"], start_day="04-01", end_day="04-20")
    open_plan = schedule(client, h, zone["id"], "restart-open", earliest="2026-04-21",
                         latest="2026-06-30", duration=3).json()
    cancelled = schedule(client, h, zone["id"], "restart-cancel", earliest="2026-07-01",
                         latest="2026-07-31", duration=3).json()
    client.post(f"/api/wetlands/plans/{cancelled['id']}/cancel",
                json={"today": "2026-03-01", "reason": "调整规划"}, headers=h)
    client.post(f"/api/wetlands/plans/{open_plan['id']}/postpone",
                json={"today": "2026-04-21", "reason": "降雨"}, headers={**h, "Idempotency-Key": "rp-1"})

    # 模拟服务重启：关闭线程局部连接，重新拉起应用生命周期（同一 SQLite 文件）
    close_connection()
    with TestClient(app) as restarted:
        login = restarted.post("/api/auth/login", json={"username": "admin", "password": "Admin!23456"})
        assert login.status_code == 200, login.text
        rh = {"Authorization": f"Bearer {login.json()['token']}"}

        recovery = restarted.post(
            "/api/wetlands/recovery", headers={**rh, "X-As-Of-Date": "2026-05-15"})
        assert recovery.status_code == 200, recovery.text
        report = recovery.json()
        assert report["total_plans"] == 2
        assert report["by_status"]["postponed"] == 1
        assert report["by_status"]["cancelled"] == 1

        detail = restarted.get(f"/api/wetlands/plans/{open_plan['id']}", headers=rh).json()
        assert detail["version"] == 2
        assert detail["status"] == "postponed"
        assert [item["action"] for item in detail["history"]] == ["schedule", "postpone"]
        # 候选与版本化历史同样持久保留
        assert {c["plan_version"] for c in detail["candidates"]} == {1, 2}

        # 重启后幂等键仍然生效
        replay = restarted.post(f"/api/wetlands/plans/{open_plan['id']}/postpone",
                                json={"today": "2026-04-21", "reason": "降雨"},
                                headers={**rh, "Idempotency-Key": "rp-1"})
        assert replay.status_code == 200
        assert replay.json()["version"] == 2

        # 逾期开放计划被恢复报告标出
        later = restarted.post("/api/wetlands/recovery", headers={**rh, "X-As-Of-Date": "2027-01-01"})
        assert open_plan["id"] in later.json()["overdue_open_plans"]
