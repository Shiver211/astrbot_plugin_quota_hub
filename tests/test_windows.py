"""滚动窗口时间计算的单元测试。"""

from __future__ import annotations

import pytest
from core import windows


def test_window_cutoff_subtracts_window():
    assert windows.window_cutoff(1000.0, 5) == 1000.0 - 5 * 3600


def test_window_cutoff_fractional_hours():
    assert windows.window_cutoff(0.0, 0.5) == -1800.0


def test_reset_in_none_oldest_is_zero():
    assert windows.reset_in_seconds(None, 1000.0, 5) == 0.0


def test_reset_in_partial_window():
    now = 10_000.0
    oldest = now - 3600  # 进入窗口 1 小时
    # 5 小时窗口 → 还剩 4 小时 = 14400 秒
    assert windows.reset_in_seconds(oldest, now, 5) == pytest.approx(14400.0)


def test_reset_in_clamped_to_zero_when_aged_out():
    now = 10_000.0
    oldest = now - 6 * 3600  # 已超过 5 小时窗口
    assert windows.reset_in_seconds(oldest, now, 5) == 0.0


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0秒"),
        (-5, "0秒"),
        (30, "30秒"),
        (59, "59秒"),
        (60, "1分钟"),
        (90, "1分钟"),
        (3599, "59分钟"),
        (3600, "1小时"),
        (3660, "1小时1分钟"),
        (7200, "2小时"),
        (23 * 3600 + 59 * 60, "23小时59分钟"),
        (86400, "1天"),
        (86400 + 3600, "1天1小时"),
        # 天级精度下舍去分钟：23小时59分 仍不足一天，1天0小时59分 显示为「1天」
        (86400 + 59 * 60, "1天"),
        (2 * 86400, "2天"),
        (7 * 86400, "7天"),
        (7 * 86400 + 2 * 3600 + 30 * 60, "7天2小时"),
    ],
)
def test_format_duration(seconds, expected):
    assert windows.format_duration(seconds) == expected
