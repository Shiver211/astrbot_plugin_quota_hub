"""滚动窗口的纯时间计算（无副作用，便于单测）。"""

from __future__ import annotations

SECONDS_PER_HOUR = 3600.0
SECONDS_PER_DAY = 86400


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
