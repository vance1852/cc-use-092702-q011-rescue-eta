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


def arrival_after_minutes(departed_at: datetime, duration_minutes: int) -> datetime:
    """从带时区的实际出发时间增加以分钟为单位的通行时长，返回 UTC 到达时刻。

    先把出发时间归一到 UTC 再做绝对时间加法：跨日自然推进；夏令时切换（含
    春跳空档与秋令重叠）时按真实流逝的分钟数落在唯一的 UTC 瞬间上，再转回
    任何 IANA 时区都确定，绝不依赖本地墙上时间的归一化。
    """
    if departed_at.tzinfo is None or departed_at.utcoffset() is None:
        raise ValueError("出发时间必须带时区")
    if not isinstance(duration_minutes, int) or isinstance(duration_minutes, bool) or duration_minutes <= 0:
        raise ValueError("通行时长必须是以分钟为单位的正整数")
    departed_utc = departed_at.astimezone(timezone.utc)
    return departed_utc + timedelta(minutes=duration_minutes)
