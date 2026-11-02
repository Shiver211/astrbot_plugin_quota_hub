"""滚动窗口的纯时间计算（无副作用，便于单测）。"""

from __future__ import annotations

import re

SECONDS_PER_HOUR = 3600.0
SECONDS_PER_DAY = 86400

DURATION_UNITS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "秒": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "分": 60.0,
    "分钟": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "时": 3600.0,
    "小时": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "天": 86400.0,
    "w": 7 * 86400.0,
    "week": 7 * 86400.0,
    "weeks": 7 * 86400.0,
    "周": 7 * 86400.0,
    "星期": 7 * 86400.0,
}

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z\u4e00-\u9fa5]+)")


def parse_duration(text: Any) -> float | None:
    """把时长字符串解析为秒数。

    支持例如: '1d', '2h', '30m', '60s', '1w', '1.5d', '1天', '2小时', '30分钟', '1d12h' 等。
    若为 '0', '0s', '0d' 等返回 0.0（常用于解封）。
    若为 'perm', 'forever', '永久', '-1' 等返回 -1.0（永久封禁）。
    若纯数字（如 '60'）按秒处理。
    无法解析时返回 None。
    """
    if text is None:
        return None
    raw = str(text).strip().lower()
    if not raw:
        return None
    if raw in ("perm", "permanent", "forever", "inf", "infinity", "永久", "-1"):
        return -1.0
    if raw in ("0", "0s", "0m", "0h", "0d", "0秒", "0分", "0天"):
        return 0.0

    matches = _DURATION_RE.findall(raw)
    if not matches:
        if raw.isdigit():
            return float(raw)
        return None

    total_seconds = 0.0
    for val_str, unit_str in matches:
        unit = unit_str.lower()
        if unit not in DURATION_UNITS:
            return None
        total_seconds += float(val_str) * DURATION_UNITS[unit]

    return total_seconds


def window_cutoff(now: float, window_hours: float) -> float:
    """返回滚动窗口的起始时间戳：早于该时刻的用量应被驱逐。"""
    return now - window_hours * SECONDS_PER_HOUR


def reset_in_seconds(oldest_ts: float | None, now: float, window_hours: float) -> float:
    """窗口内最早一条用量再过多久离开窗口（即额度开始恢复的时间）。

    ``oldest_ts`` 为 ``None``（无用量）时返回 0。
    """
    if oldest_ts is None:
        return 0.0
    remaining = window_hours * SECONDS_PER_HOUR - (now - oldest_ts)
    return max(0.0, remaining)


def format_duration(seconds: float) -> str:
    """把秒数格式化为中文可读时长，如 ``2小时15分钟`` / ``7天``。

    单位随量级递减，只保留最大的两级：秒 → 分钟 → 小时(分钟) → 天(小时)。
    长窗口以天计，``168小时`` 这样的读数对用户没有意义，故 ≥24 小时改用「天」，
    并舍去分钟（天级精度下分钟无意义）。
    """
    total = int(max(0, round(seconds)))
    if total < 60:
        return f"{total}秒"
    minutes, _sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}小时{minutes}分钟" if minutes else f"{hours}小时"
    days, hours = divmod(hours, 24)
    return f"{days}天{hours}小时" if hours else f"{days}天"
