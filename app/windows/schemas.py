from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.errors import ValidationError
from app.windows.calendar import parse_month_day

CODE_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,63}$"
MAX_RANGE_DAYS = 370


class ZoneCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class WorkTypeCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)


class SensitivePeriodCreate(BaseModel):
    zone_code: str | None = Field(default=None, max_length=64, description="为空表示适用于全部区域")
    species: str = Field(min_length=1, max_length=120)
    period_kind: Literal["breeding", "migration", "other"]
    start_month_day: str = Field(min_length=5, max_length=5)
    end_month_day: str = Field(min_length=5, max_length=5)
    blocked_work_types: list[str] = Field(default_factory=list, max_length=50, description="为空表示禁止全部作业类型")
    note: str = Field(default="", max_length=500)

    @field_validator("start_month_day", "end_month_day")
    @classmethod
    def valid_month_day(cls, value: str) -> str:
        try:
            parse_month_day(value)
        except ValidationError as exc:
            raise ValueError(exc.message) from exc
        return value


class WindowComputeRequest(BaseModel):
    zone_code: str = Field(min_length=2, max_length=64)
    work_type_code: str = Field(min_length=2, max_length=64)
    start: date
    end: date
    duration_days: int = Field(default=1, ge=1, le=60)
    as_of: date | None = Field(default=None, description="注入的计算基准日，缺省取系统当天")

    @model_validator(mode="after")
    def validate_range(self) -> "WindowComputeRequest":
        if self.end < self.start:
            raise ValueError("结束日期不能早于开始日期")
        if (self.end - self.start).days > MAX_RANGE_DAYS:
            raise ValueError(f"计算区间不能超过 {MAX_RANGE_DAYS} 天")
        return self


class PlanCreate(BaseModel):
    zone_code: str = Field(min_length=2, max_length=64)
    work_type_code: str = Field(min_length=2, max_length=64)
    requested_start: date
    requested_end: date
    duration_days: int = Field(default=1, ge=1, le=60)
    priority: int = Field(default=50, ge=0, le=100)
    idempotency_key: str = Field(min_length=6, max_length=160)
    as_of: date | None = Field(default=None, description="注入的计算基准日，缺省取系统当天")
    note: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_range(self) -> "PlanCreate":
        if self.requested_end < self.requested_start:
            raise ValueError("期望结束日期不能早于开始日期")
        if (self.requested_end - self.requested_start).days > MAX_RANGE_DAYS:
            raise ValueError(f"计划区间不能超过 {MAX_RANGE_DAYS} 天")
        return self


class PlanPostpone(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    cause: Literal["rain", "weather", "operational", "ecology", "other"] = "other"
    shift_days: int | None = Field(default=None, ge=1, le=365)
    new_start: date | None = None
    new_end: date | None = None
    idempotency_key: str = Field(min_length=6, max_length=160)
    as_of: date | None = Field(default=None, description="注入的计算基准日，缺省取系统当天")

    @model_validator(mode="after")
    def validate_target(self) -> "PlanPostpone":
        explicit = self.new_start is not None or self.new_end is not None
        if self.shift_days is not None and explicit:
            raise ValueError("shift_days 与 new_start/new_end 只能二选一")
        if self.shift_days is None and not explicit:
            raise ValueError("必须提供 shift_days 或 new_start/new_end")
        if explicit and (self.new_start is None or self.new_end is None):
            raise ValueError("new_start 与 new_end 必须同时提供")
        if explicit and self.new_end < self.new_start:
            raise ValueError("新结束日期不能早于新开始日期")
        if explicit and (self.new_end - self.new_start).days > MAX_RANGE_DAYS:
            raise ValueError(f"计划区间不能超过 {MAX_RANGE_DAYS} 天")
        return self


class PlanCancel(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    idempotency_key: str = Field(min_length=6, max_length=160)
