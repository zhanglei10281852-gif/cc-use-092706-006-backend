from __future__ import annotations

from datetime import date

from app.core.errors import ValidationError

# 以闰年为参照，允许 02-29 这类合法月日。
_REFERENCE_YEAR = 2024


def parse_month_day(value: str) -> tuple[int, int]:
    """把 ``MM-DD`` 形式的年度月日解析为 (月, 日)，并校验真实存在。"""
    text = (value or "").strip()
    try:
        month = int(text[0:2])
        day = int(text[3:5])
        if len(text) != 5 or text[2] != "-":
            raise ValueError
        date(_REFERENCE_YEAR, month, day)
    except (ValueError, IndexError):
        raise ValidationError(f"月日格式必须是合法的 MM-DD：{value!r}") from None
    return month, day


def month_day_covers(start: tuple[int, int], end: tuple[int, int], day: date) -> bool:
    """判断年度区间 [start, end] 是否覆盖某一天，支持跨年（如 11-01 至 02-15）。"""
    current = (day.month, day.day)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end
