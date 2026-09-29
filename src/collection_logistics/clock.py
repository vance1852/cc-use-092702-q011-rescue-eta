"""可注入的 UTC 时间源。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        if self.current.tzinfo is None:
            raise ValueError("冻结时钟必须带时区")
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str, field: str = "时间") -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def arrival_after_minutes(departed_at: datetime, response_minutes: int) -> datetime:
    """从带时区的实际出发时刻增加分钟，得到预计到达时刻。

    先归一到 UTC 再加分钟：救援时长是物理经过时间，必须按绝对时刻运算。
    若直接在本地墙上时间上加，夏令时回拨当天会多算一小时、拨快当天会少算。
    归一后跨日自然进位，夏令时切换结果唯一确定。
    入参分钟由 models.response_minutes_value 保证为正整数，这里做防御性校验。
    """
    if departed_at.tzinfo is None or departed_at.utcoffset() is None:
        raise ValueError("出发时刻必须带时区")
    if isinstance(response_minutes, bool) or not isinstance(response_minutes, int) or response_minutes <= 0:
        raise ValueError("响应时长必须是正整数分钟")
    return departed_at.astimezone(timezone.utc) + timedelta(minutes=response_minutes)
