"""小时级用量汇总（``usage_hourly``）：历史统计的存储层。

明细表 ``usage_events`` 受 ``retention_days`` 约束会被定期清理；本模块把「已关账」
的整点小时聚合固化到 ``usage_hourly`` 长期保留，并提供合并「已固化汇总 + 水位线
之后未固化明细」的历史统计查询，使统计口径覆盖到当前时刻且不重复计数。
以 Mixin 注入 ``Storage``（复用其 ``_run`` 串行化机制）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

HOUR = 3600
# 小时关账滞后：给 on_llm_response 的 token 回填与批量落盘留余量
# （聚合器 PENDING_TTL=600s，取 900s 覆盖）。
ROLLUP_LAG_SECONDS = 900.0

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_hourly (
    hour_ts INTEGER NOT NULL,            -- 整点小时起始时间戳
    platform TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (hour_ts, platform, group_id, user_id, model)
);
CREATE INDEX IF NOT EXISTS idx_qh_hourly_group ON usage_hourly(group_id, hour_ts);
CREATE INDEX IF NOT EXISTS idx_qh_hourly_user ON usage_hourly(user_id, hour_ts);
"""

# 合并查询的公共 CTE。参数（顺序固定）：
#   start_h            —— 范围起点（对齐到整点）
#   min(wm, end)       —— 汇总侧上界：不超过水位线也不超过范围终点
#   max(start_h, wm)   —— 明细侧下界：水位线之前的明细已固化，不再重复计入
#   end                —— 范围终点
_MERGED_CTE = (
    "WITH merged AS ("
    " SELECT hour_ts, platform, group_id, user_id, model, calls, tokens"
    "   FROM usage_hourly WHERE hour_ts >= ? AND hour_ts < ?"
    " UNION ALL"
    " SELECT (ts / 3600) * 3600 AS hour_ts, platform, group_id, user_id, model,"
    "        calls, tokens"
    "   FROM usage_events WHERE ts >= ? AND ts < ?"
    ") "
)


class HistoryStorageMixin:
    """依赖宿主类提供 ``_run(fn)``（见 ``Storage``）。"""

    async def rollup_hourly(
        self, now: float, lag_seconds: float = ROLLUP_LAG_SECONDS
    ) -> int:
        """把已关账的整点小时从明细聚合固化进 ``usage_hourly``。

        返回关账水位线（时间戳，不含）：早于该时刻的明细已全部固化，可安全清理。
        从上次水位小时（含，幂等重算以吸收固化后才落盘的 token 回填）滚动到
        ``now - lag`` 所在小时（不含）。
        """
        horizon = int((now - lag_seconds) // HOUR) * HOUR

        def op(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT MAX(hour_ts) FROM usage_hourly").fetchone()
            if row is not None and row[0] is not None:
                start = int(row[0])
            else:
                first = conn.execute("SELECT MIN(ts) FROM usage_events").fetchone()
                if first is None or first[0] is None:
                    return horizon
                start = int(first[0]) // HOUR * HOUR
            if start >= horizon:
                return horizon
            conn.execute(
                "INSERT INTO usage_hourly"
                " (hour_ts, platform, group_id, user_id, model, calls, tokens)"
                " SELECT (ts / 3600) * 3600 AS h, platform, group_id, user_id, model,"
                "        SUM(calls), SUM(tokens)"
                "   FROM usage_events WHERE ts >= ? AND ts < ?"
                "  GROUP BY h, platform, group_id, user_id, model"
                " ON CONFLICT(hour_ts, platform, group_id, user_id, model)"
                " DO UPDATE SET calls = excluded.calls, tokens = excluded.tokens",
                [start, horizon],
            )
            conn.commit()
            return horizon

        return await self._run(op)  # type: ignore[attr-defined]

    async def history_stats(
        self,
        start_ts: float,
        end_ts: float,
        top_limit: int = 10,
        with_hourly: bool = False,
    ) -> dict[str, Any]:
        """区间 ``[start_ts, end_ts)`` 的历史统计（起点向下对齐到整点）。

        日期与时段按服务器本地时区划分；``heatmap`` 的 weekday 取 SQLite ``%w``
        口径（0=周日 … 6=周六）。``with_hourly`` 额外返回按整点的 ``hourly``
        序列（供 24 小时视图画趋势）——长范围会产生大量行，仅短范围开启。
        """
        start_h = int(start_ts) // HOUR * HOUR
        end = int(end_ts)

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            row = conn.execute("SELECT MAX(hour_ts) FROM usage_hourly").fetchone()
            wm = int(row[0]) + HOUR if row is not None and row[0] is not None else 0
            params = [start_h, min(wm, end), max(start_h, wm), end]

            def q(sql: str, extra: tuple = ()) -> list[tuple]:
                return conn.execute(_MERGED_CTE + sql, [*params, *extra]).fetchall()

            t = q(
                "SELECT COALESCE(SUM(calls), 0), COALESCE(SUM(tokens), 0),"
                " COUNT(DISTINCT CASE WHEN group_id != '' THEN group_id END),"
                " COUNT(DISTINCT CASE WHEN user_id != '' THEN user_id END),"
                " COUNT(DISTINCT date(hour_ts, 'unixepoch', 'localtime'))"
                " FROM merged"
            )[0]
            top = (
                "SELECT {col}, SUM(calls), SUM(tokens) FROM merged"
                " WHERE {col} != '' GROUP BY {col}"
                " ORDER BY SUM(tokens) DESC, SUM(calls) DESC LIMIT ?"
            )
            dist = (
                "SELECT {col}, SUM(calls), SUM(tokens) FROM merged GROUP BY {col}"
                " ORDER BY SUM(tokens) DESC, SUM(calls) DESC"
            )
            hourly: list[tuple[int, int, int]] = []
            if with_hourly:
                hourly = [
                    (int(r[0]), int(r[1]), int(r[2]))
                    for r in q(
                        "SELECT hour_ts, SUM(calls), SUM(tokens) FROM merged"
                        " GROUP BY hour_ts ORDER BY hour_ts"
                    )
                ]
            return {
                "totals": {
                    "calls": int(t[0]),
                    "tokens": int(t[1]),
                    "active_groups": int(t[2] or 0),
                    "active_users": int(t[3] or 0),
                    "active_days": int(t[4] or 0),
                },
                "hourly": hourly,
                "daily": [
                    (r[0], int(r[1]), int(r[2]))
                    for r in q(
                        "SELECT date(hour_ts, 'unixepoch', 'localtime') AS d,"
                        " SUM(calls), SUM(tokens) FROM merged GROUP BY d ORDER BY d"
                    )
                ],
                "top_groups": [
                    (r[0], int(r[1]), int(r[2]))
                    for r in q(top.format(col="group_id"), (top_limit,))
                ],
                "top_users": [
                    (r[0], int(r[1]), int(r[2]))
                    for r in q(top.format(col="user_id"), (top_limit,))
                ],
                "models": [
                    (r[0], int(r[1]), int(r[2])) for r in q(dist.format(col="model"))
                ],
                "platforms": [
                    (r[0], int(r[1]), int(r[2])) for r in q(dist.format(col="platform"))
                ],
                "heatmap": [
                    (int(r[0]), int(r[1]), int(r[2]), int(r[3]))
                    for r in q(
                        "SELECT CAST(strftime('%w', hour_ts, 'unixepoch', 'localtime')"
                        "        AS INTEGER) AS w,"
                        " CAST(strftime('%H', hour_ts, 'unixepoch', 'localtime')"
                        "        AS INTEGER) AS h,"
                        " SUM(calls), SUM(tokens) FROM merged GROUP BY w, h"
                        " ORDER BY w, h"
                    )
                ],
            }

        return await self._run(op)  # type: ignore[attr-defined]

    async def purge_hourly(self, cutoff_ts: float) -> int:
        """清理早于 cutoff 的小时汇总（``history_retention_days`` > 0 时调用）。"""

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM usage_hourly WHERE hour_ts < ?", [int(cutoff_ts)]
            )
            conn.commit()
            return cur.rowcount

        return await self._run(op)  # type: ignore[attr-defined]
