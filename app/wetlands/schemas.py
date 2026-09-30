from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

MONTH_DAY_PATTERN = r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$"
ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


class ZoneCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=100)
    zone_type: Literal["reed", "dryland_willow", "mudflat"]


class WorkTypeCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(..., min_length=1, max_length=100)


class SensitiveRuleCreate(BaseModel):
    zone_id: int = Field(..., ge=1)
    work_type_code: str | None = Field(default=None, min_length=2, max_length=40)
    species_name: str = Field(..., min_length=1, max_length=80)
    period_type: Literal["annual", "fixed"]
    # 周年规则：MM-DD，允许跨年（如 11-15 到 03-31 的越冬期）
    start_day: str | None = Field(default=None, pattern=MONTH_DAY_PATTERN)
    end_day: str | None = Field(default=None, pattern=MONTH_DAY_PATTERN)
    # 固定规则：YYYY-MM-DD
    start_date: str | None = Field(default=None, pattern=ISO_DATE_PATTERN)
    end_date: str | None = Field(default=None, pattern=ISO_DATE_PATTERN)
    severity: Literal["avoid", "forbid"] = "forbid"
    reason: str = Field(default="", max_length=300)


class WindowQuery(BaseModel):
    zone_id: int = Field(..., ge=1)
    work_type_code: str = Field(..., min_length=2, max_length=40)
    earliest_date: str = Field(..., pattern=ISO_DATE_PATTERN)
    latest_date: str = Field(..., pattern=ISO_DATE_PATTERN)
    duration_days: int = Field(..., ge=1, le=60)
    priority: int = Field(default=50, ge=0, le=100)


class PlanCreate(WindowQuery):
    title: str = Field(..., min_length=1, max_length=120)
    idempotency_key: str = Field(..., min_length=6, max_length=160)
    reason: str = Field(default="", max_length=300)


class PlanPostpone(BaseModel):
    # 注入的“今天”：临时降雨把作业推迟到该日期之后重排
    today: str = Field(..., pattern=ISO_DATE_PATTERN)
    reason: str = Field(..., min_length=1, max_length=300)
    force: bool = False


class PlanCancel(BaseModel):
    today: str = Field(..., pattern=ISO_DATE_PATTERN)
    reason: str = Field(..., min_length=1, max_length=300)


class PlanComplete(BaseModel):
    today: str = Field(..., pattern=ISO_DATE_PATTERN)
