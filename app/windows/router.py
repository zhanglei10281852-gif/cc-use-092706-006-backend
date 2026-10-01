from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.windows.schemas import PlanCancel, PlanCreate, PlanPostpone, SensitivePeriodCreate, WindowComputeRequest, WorkTypeCreate, ZoneCreate
from app.windows.service import WindowPlanningService

router = APIRouter(prefix="/api/windows", tags=["维护窗口编排"])


def service() -> WindowPlanningService:
    return WindowPlanningService()


@router.get("/zones")
def list_zones(principal: Principal = Depends(current_principal)):
    principal.require("windows.read")
    return {"items": service().list_zones()}


@router.post("/zones", status_code=201)
def create_zone(payload: ZoneCreate, principal: Principal = Depends(current_principal)):
    principal.require("windows.write")
    return service().create_zone(payload.model_dump(), principal.username)


@router.get("/work-types")
def list_work_types(principal: Principal = Depends(current_principal)):
    principal.require("windows.read")
    return {"items": service().list_work_types()}


@router.post("/work-types", status_code=201)
def create_work_type(payload: WorkTypeCreate, principal: Principal = Depends(current_principal)):
    principal.require("windows.write")
    return service().create_work_type(payload.model_dump(), principal.username)


@router.get("/sensitive-periods")
def list_periods(zone_code: str | None = None, principal: Principal = Depends(current_principal)):
    principal.require("windows.read")
    return {"items": service().list_periods(zone_code)}


@router.post("/sensitive-periods", status_code=201)
def create_period(payload: SensitivePeriodCreate, principal: Principal = Depends(current_principal)):
    principal.require("windows.write")
    return service().create_period(payload.model_dump(), principal.username)


@router.delete("/sensitive-periods/{period_id}")
def retire_period(period_id: int, principal: Principal = Depends(current_principal)):
    principal.require("windows.write")
    return service().retire_period(period_id, principal.username)


@router.post("/compute")
def compute_windows(payload: WindowComputeRequest, principal: Principal = Depends(current_principal)):
    principal.require("windows.read")
    return service().compute(payload.model_dump())


@router.post("/plans", status_code=201)
def create_plan(payload: PlanCreate, principal: Principal = Depends(current_principal)):
    principal.require("windows.write")
    return service().create_plan(payload.model_dump(), principal.username)


@router.get("/plans")
def list_plans(
    status: str | None = None,
    zone_code: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    principal: Principal = Depends(current_principal),
):
    principal.require("windows.read")
    return {"items": service().list_plans(status=status, zone_code=zone_code, limit=limit)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int, principal: Principal = Depends(current_principal)):
    principal.require("windows.read")
    return service().get_plan(plan_id)


@router.post("/plans/{plan_id}/postpone")
def postpone_plan(plan_id: int, payload: PlanPostpone, principal: Principal = Depends(current_principal)):
    principal.require("windows.write")
    return service().postpone_plan(plan_id, payload.model_dump(), principal.username)


@router.post("/plans/{plan_id}/cancel")
def cancel_plan(plan_id: int, payload: PlanCancel, principal: Principal = Depends(current_principal)):
    principal.require("windows.write")
    return service().cancel_plan(plan_id, payload.model_dump(), principal.username)
