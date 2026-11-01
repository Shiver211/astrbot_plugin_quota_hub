"""usage_hourly 固化与历史统计查询的集成测试（用临时库，asyncio.run 驱动）。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime

from core.storage import Storage
from core.storage_history import ROLLUP_LAG_SECONDS

HOUR = 3600
BASE = 1_700_000_000 // HOUR * HOUR  # 对齐到整点的历史时刻


def _run(coro_fn):
    return asyncio.run(coro_fn())


async def _fresh(tmp_path):
    storage = Storage(tmp_path / "quota.db")
    await storage.initialize()
    return storage


def _hourly_rows(tmp_path):
    conn = sqlite3.connect(tmp_path / "quota.db")
    try:
        return conn.execute(
            "SELECT hour_ts, platform, group_id, user_id, model, calls, tokens"
            " FROM usage_hourly ORDER BY hour_ts, model"
        ).fetchall()
    finally:
        conn.close()


def test_rollup_aggregates_closed_hours(tmp_path):
    now = BASE + 2 * HOUR + ROLLUP_LAG_SECONDS + 1  # 小时 0/1 已关账，小时 2 未关账

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, BASE + 10, "aiocqhttp", "g1", "u1", 1, 100, "gpt"),
                (2, BASE + 20, "aiocqhttp", "g1", "u1", 1, 50, "gpt"),
                (3, BASE + HOUR + 5, "aiocqhttp", "g1", "u2", 1, 30, "claude"),
                (4, BASE + 2 * HOUR + 5, "aiocqhttp", "g1", "u1", 1, 7, "gpt"),
            ]
        )
        horizon = await storage.rollup_hourly(now)
        await storage.close()
        return horizon

    assert _run(lambda: scenario()) == BASE + 2 * HOUR
    assert _hourly_rows(tmp_path) == [
        (BASE, "aiocqhttp", "g1", "u1", "gpt", 2, 150),
        (BASE + HOUR, "aiocqhttp", "g1", "u2", "claude", 1, 30),
    ]


def test_rollup_rerolls_watermark_hour(tmp_path):
    """固化后才到的 token 回填，应被下一轮对水位小时的重算吸收。"""
    now = BASE + HOUR + ROLLUP_LAG_SECONDS + 1

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events([(1, BASE + 10, "p", "g1", "u1", 1, 0, "m")])
        await storage.rollup_hourly(now)
        await storage.update_tokens([(88, 1)])
        await storage.rollup_hourly(now)
        await storage.close()

    _run(lambda: scenario())
    assert _hourly_rows(tmp_path) == [(BASE, "p", "g1", "u1", "m", 1, 88)]


def test_rollup_idempotent(tmp_path):
    now = BASE + HOUR + ROLLUP_LAG_SECONDS + 1

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events([(1, BASE + 10, "p", "g1", "u1", 1, 5, "m")])
        await storage.rollup_hourly(now)
        await storage.rollup_hourly(now)
        await storage.close()

    _run(lambda: scenario())
    assert _hourly_rows(tmp_path) == [(BASE, "p", "g1", "u1", "m", 1, 5)]


def test_rollup_without_events_is_noop(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        horizon = await storage.rollup_hourly(BASE)
        await storage.close()
        return horizon

    assert _run(lambda: scenario()) == BASE - HOUR
    assert _hourly_rows(tmp_path) == []


def test_history_stats_merges_rolled_and_raw(tmp_path):
    """已固化的小时与水位线之后的明细应合并统计、互不重复计数。"""
    now = BASE + 2 * HOUR + ROLLUP_LAG_SECONDS + 1

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, BASE + 10, "p", "g1", "u1", 1, 100, "gpt"),
                (2, BASE + 2 * HOUR + 30, "p", "g1", "u1", 1, 40, "gpt"),  # 未固化
            ]
        )
        await storage.rollup_hourly(now)
        stats = await storage.history_stats(BASE - HOUR, now)
        await storage.close()
        return stats

    stats = _run(lambda: scenario())
    assert stats["totals"]["calls"] == 2
    assert stats["totals"]["tokens"] == 140
    assert stats["top_groups"] == [("g1", 2, 140)]
    assert stats["models"] == [("gpt", 2, 140)]


def test_history_stats_seam_at_watermark(tmp_path):
    """紧贴水位线两侧的事件恰好各计一次：固化侧上界与明细侧下界在 wm 处严丝合缝。"""
    now = BASE + HOUR + ROLLUP_LAG_SECONDS + 1  # 水位线 wm = BASE + HOUR

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, BASE + HOUR - 1, "p", "g1", "u1", 1, 10, "m"),  # 已固化小时的最后一秒
                (2, BASE + HOUR + 1, "p", "g1", "u1", 1, 20, "m"),  # 水位线后的首个未固化小时
            ]
        )
        await storage.rollup_hourly(now)
        stats = await storage.history_stats(BASE, now)
        await storage.close()
        return stats

    stats = _run(lambda: scenario())
    assert stats["totals"]["calls"] == 2  # 若明细下界写错为 wm±1h，则漏计或重复
    assert stats["totals"]["tokens"] == 30


def test_history_stats_without_rollup_reads_raw(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events([(1, BASE + 10, "p", "", "u1", 1, 30, "m")])
        stats = await storage.history_stats(BASE - HOUR, BASE + HOUR)
        await storage.close()
        return stats

    stats = _run(lambda: scenario())
    assert stats["totals"]["calls"] == 1
    assert stats["totals"]["tokens"] == 30
    assert stats["top_users"] == [("u1", 1, 30)]


def test_history_stats_dimensions(tmp_path):
    now = BASE + HOUR + ROLLUP_LAG_SECONDS + 1

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, BASE + 10, "aiocqhttp", "g1", "u1", 1, 100, "gpt"),
                (2, BASE + 20, "aiocqhttp", "g2", "u2", 1, 300, "claude"),
                (3, BASE + 30, "webchat", "", "u3", 1, 50, "gpt"),
            ]
        )
        await storage.rollup_hourly(now)
        stats = await storage.history_stats(BASE - HOUR, now)
        await storage.close()
        return stats

    stats = _run(lambda: scenario())
    assert stats["totals"]["active_groups"] == 2
    assert stats["totals"]["active_users"] == 3
    assert stats["totals"]["active_days"] == 1
    assert stats["top_groups"][0] == ("g2", 1, 300)  # 按 Token 降序
    assert stats["top_users"][0] == ("u2", 1, 300)
    assert {m: (c, t) for m, c, t in stats["models"]} == {
        "gpt": (2, 150),
        "claude": (1, 300),
    }
    assert {p: (c, t) for p, c, t in stats["platforms"]} == {
        "aiocqhttp": (2, 400),
        "webchat": (1, 50),
    }
    dt = datetime.fromtimestamp(BASE)
    assert stats["heatmap"] == [((dt.weekday() + 1) % 7, dt.hour, 3, 450)]
    assert stats["daily"] == [(dt.strftime("%Y-%m-%d"), 3, 450)]


def test_history_stats_hourly_series(tmp_path):
    """with_hourly=True 返回按整点的合并序列（固化 + 未固化各占一桶），默认关闭。"""
    now = BASE + 2 * HOUR + ROLLUP_LAG_SECONDS + 1  # 水位线 wm = BASE + 2H

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, BASE + 10, "p", "g1", "u1", 1, 10, "m"),            # 将被固化
                (2, BASE + 20, "p", "g1", "u2", 1, 5, "m"),             # 同小时并桶
                (3, BASE + 2 * HOUR + 30, "p", "g1", "u1", 1, 20, "m"),  # 未固化
            ]
        )
        await storage.rollup_hourly(now)
        with_hourly = await storage.history_stats(BASE, now, with_hourly=True)
        without = await storage.history_stats(BASE, now)
        await storage.close()
        return with_hourly, without

    with_hourly, without = _run(lambda: scenario())
    assert with_hourly["hourly"] == [
        (BASE, 2, 15),
        (BASE + 2 * HOUR, 1, 20),
    ]
    assert without["hourly"] == []


def test_purge_hourly(tmp_path):
    now = BASE + 2 * HOUR + ROLLUP_LAG_SECONDS + 1

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, BASE + 10, "p", "g1", "u1", 1, 1, "m"),
                (2, BASE + HOUR + 10, "p", "g1", "u1", 1, 2, "m"),
            ]
        )
        await storage.rollup_hourly(now)
        deleted = await storage.purge_hourly(BASE + HOUR)
        await storage.close()
        return deleted

    assert _run(lambda: scenario()) == 1
    assert [r[0] for r in _hourly_rows(tmp_path)] == [BASE + HOUR]


def test_history_survives_raw_purge(tmp_path):
    """明细按 retention 清理后，已固化的历史统计不受影响。"""
    now = BASE + HOUR + ROLLUP_LAG_SECONDS + 1

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events([(1, BASE + 10, "p", "g1", "u1", 1, 9, "m")])
        horizon = await storage.rollup_hourly(now)
        await storage.purge_old(min(now, horizon))  # 与维护循环的护栏同口径
        stats = await storage.history_stats(BASE - HOUR, now)
        await storage.close()
        return stats

    stats = _run(lambda: scenario())
    assert stats["totals"] == {
        "calls": 1,
        "tokens": 9,
        "active_groups": 1,
        "active_users": 1,
        "active_days": 1,
    }
