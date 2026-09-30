from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header

from app.api.dependencies import current_principal
from app.core.errors import ValidationError
from app.core.security import Principal
from app.wetlands.schemas import (
    PlanCancel,
    PlanComplete,
    PlanCreate,
    PlanPostpone,
    SensitiveRuleCreate,
    WindowQuery,
    WorkTypeCreate,
    ZoneCreate,
)
from app.wetlands.service import WetlandsService

router = APIRouter(prefix="/api/wetlands", tags=["奥森湿地维护窗口编排"])


def service() -> WetlandsService:
    return WetlandsService()


def parse_today(raw: str | None) -> date:
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("注入日期必须为 YYYY-MM-DD") from exc


# ---------------------------------------------------------------------- 区域与作业类型

@router.post("/zones", status_code=201)
def create_zone(payload: ZoneCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.write")
    return service().create_zone(payload.model_dump())


@router.get("/zones")
def list_zones(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return {"items": service().list_zones()}


@router.post("/work-types", status_code=201)
def create_work_type(payload: WorkTypeCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.write")
    return service().create_work_type(payload.model_dump())


@router.get("/work-types")
def list_work_types(principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return {"items": service().list_work_types()}


# ---------------------------------------------------------------------- 物种敏感期

@router.post("/rules", status_code=201)
def create_rule(payload: SensitiveRuleCreate, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.write")
    return service().create_rule(payload.model_dump(exclude_none=True))


@router.get("/rules")
def list_rules(zone_id: int | None = None, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return {"items": service().list_rules(zone_id)}


# ---------------------------------------------------------------------- 窗口试算

@router.post("/windows/preview")
def preview_windows(payload: WindowQuery, as_of_date: str | None = Header(default=None, alias="X-As-Of-Date"),
                    principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return service().preview(payload.model_dump(), parse_today(as_of_date))


# ---------------------------------------------------------------------- 维护计划

@router.post("/plans", status_code=201)
def schedule_plan(payload: PlanCreate, as_of_date: str | None = Header(default=None, alias="X-As-Of-Date"),
                  principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.write")
    return service().schedule_plan(payload.model_dump(), principal.username, parse_today(as_of_date))


@router.get("/plans")
def list_plans(zone_id: int | None = None, status: str | None = None,
               principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return {"items": service().list_plans(zone_id=zone_id, status=status)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return service().get_plan(plan_id)


@router.get("/plans/{plan_id}/history")
def plan_history(plan_id: int, principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return {"items": service().history(plan_id)}


@router.post("/plans/{plan_id}/postpone")
def postpone_plan(plan_id: int, payload: PlanPostpone,
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                  principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.write")
    return service().postpone_plan(
        plan_id, principal.username, payload.reason, parse_today(payload.today),
        force=payload.force, idempotency_key=idempotency_key,
    )


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: int, payload: PlanCancel,
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.write")
    return service().cancel_plan(
        plan_id, principal.username, payload.reason, parse_today(payload.today),
        idempotency_key=idempotency_key,
    )


@router.post("/plans/{plan_id}/complete")
def complete_plan(plan_id: int, payload: PlanComplete,
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                  principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.write")
    return service().complete_plan(
        plan_id, principal.username, parse_today(payload.today), idempotency_key=idempotency_key,
    )


# ---------------------------------------------------------------------- 重启恢复

@router.post("/recovery")
def recover_plans(as_of_date: str | None = Header(default=None, alias="X-As-Of-Date"),
                  principal: Principal = Depends(current_principal)) -> dict:
    principal.require("wetlands.read")
    return service().recover(parse_today(as_of_date))
