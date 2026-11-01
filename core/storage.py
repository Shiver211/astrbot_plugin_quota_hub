"""SQLite 持久化层。

并发安全策略：单一连接（``check_same_thread=False``）+ WAL + ``busy_timeout``，
所有访问经 ``asyncio.Lock`` 串行化并在 ``asyncio.to_thread`` 中执行，避免阻塞事件
循环，也杜绝 ``database is locked``。仅用 Python 标准库，无外部依赖。
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .models import SCOPE_GROUP, SCOPE_MEMBER, SCOPE_PRIVATE, Override
from .storage_history import HISTORY_SCHEMA, HistoryStorageMixin
from .storage_state import STATE_SCHEMA, StateStorageMixin

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    eid INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    platform TEXT NOT NULL DEFAULT '',
    group_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_qh_events_ts ON usage_events(ts);
CREATE INDEX IF NOT EXISTS idx_qh_events_group_user ON usage_events(group_id, user_id, ts);
CREATE INDEX IF NOT EXISTS idx_qh_events_eid ON usage_events(eid);

CREATE TABLE IF NOT EXISTS overrides (
    scope TEXT NOT NULL,
    group_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    call_limit INTEGER,
    token_limit INTEGER,
    window_hours REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    fallback_provider_id TEXT,
    fallback_call_limit INTEGER,
    fallback_token_limit INTEGER,
    long_calls INTEGER,
    long_tokens INTEGER,
    long_window_days REAL,
    self_reset_short INTEGER,
    self_reset_long INTEGER,
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, group_id, user_id)
);

CREATE TABLE IF NOT EXISTS entity_meta (
    kind TEXT NOT NULL,                  -- 'group' | 'user'
    entity_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',       -- 平台名称/昵称（消息中自动捕获）
    remark TEXT NOT NULL DEFAULT '',     -- 用户备注（中台编辑）
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, entity_id)
);

CREATE TABLE IF NOT EXISTS banned_users (
    group_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL,
    banned_at INTEGER NOT NULL,
    expire_at INTEGER NOT NULL,          -- -1 表示永久封禁，否则为到期时间戳
    banned_by TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_qh_banned_user ON banned_users(user_id);
"""


def _scope_predicate(scope: str, group_id: str, user_id: str) -> tuple[str, list[Any]]:
    """返回某作用域实体的 SQL WHERE 片段与参数。"""
    if scope == SCOPE_PRIVATE:
        return "group_id = '' AND user_id = ?", [user_id]
    if scope == SCOPE_GROUP:
        return "group_id = ?", [group_id]
    if scope == SCOPE_MEMBER:
        return "group_id = ? AND user_id = ?", [group_id, user_id]
    raise ValueError(f"未知作用域: {scope}")


class Storage(HistoryStorageMixin, StateStorageMixin):
    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # --- 生命周期 -------------------------------------------------------

    async def initialize(self) -> None:
        await asyncio.to_thread(self._connect)

    def _connect(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        conn.executescript(HISTORY_SCHEMA)
        conn.executescript(STATE_SCHEMA)
        self._migrate(conn)
        conn.commit()
        self._conn = conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """为旧库补齐后续版本新增的列（ADD COLUMN 默认 NULL，语义=回落默认）。"""
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(overrides)").fetchall()
        }
        for column, decl in (
            ("fallback_provider_id", "TEXT"),
            ("fallback_call_limit", "INTEGER"),
            ("fallback_token_limit", "INTEGER"),
            ("long_calls", "INTEGER"),
            ("long_tokens", "INTEGER"),
            ("long_window_days", "REAL"),
            ("self_reset_short", "INTEGER"),
            ("self_reset_long", "INTEGER"),
        ):
            if column not in existing:
                conn.execute(f"ALTER TABLE overrides ADD COLUMN {column} {decl}")

    async def close(self) -> None:
        if self._conn is not None:
            conn = self._conn
            self._conn = None
            async with self._lock:
                await asyncio.to_thread(conn.close)

    async def _run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        async with self._lock:
            if self._conn is None:
                raise RuntimeError("Storage 未初始化或已关闭")
            return await asyncio.to_thread(fn, self._conn)

    # --- 写入 -----------------------------------------------------------

    async def insert_events(
        self, rows: Sequence[tuple[int, int, str, str, str, int, int, str]]
    ) -> None:
        if not rows:
            return

        def op(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT INTO usage_events"
                " (eid, ts, platform, group_id, user_id, calls, tokens, model)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()

        await self._run(op)

    async def update_tokens(self, updates: Sequence[tuple[int, int]]) -> None:
        """``updates`` 为 ``(tokens, eid)`` 序列，把对应事件的 token 设为该值。"""
        if not updates:
            return

        def op(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "UPDATE usage_events SET tokens = ? WHERE eid = ?", updates
            )
            conn.commit()

        await self._run(op)

    async def delete_events(self, scope: str, group_id: str, user_id: str) -> int:
        where, params = _scope_predicate(scope, group_id, user_id)

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(f"DELETE FROM usage_events WHERE {where}", params)
            conn.commit()
            return cur.rowcount

        return await self._run(op)

    async def purge_old(self, cutoff_ts: float) -> int:
        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "DELETE FROM usage_events WHERE ts < ?", [int(cutoff_ts)]
            )
            conn.commit()
            return cur.rowcount

        return await self._run(op)

    # --- 读取（限流回填 / 启动恢复） -----------------------------------

    async def query_usage(
        self, scope: str, group_id: str, user_id: str, cutoff_ts: float
    ) -> tuple[int, int]:
        where, params = _scope_predicate(scope, group_id, user_id)

        def op(conn: sqlite3.Connection) -> tuple[int, int]:
            row = conn.execute(
                "SELECT COALESCE(SUM(calls), 0), COALESCE(SUM(tokens), 0)"
                f" FROM usage_events WHERE {where} AND ts >= ?",
                [*params, int(cutoff_ts)],
            ).fetchone()
            return (int(row[0]), int(row[1]))

        return await self._run(op)

    async def usage_since_markers(
        self, items: Sequence[tuple[str, str, str, float]]
    ) -> dict[tuple[str, str, str], tuple[int, int]]:
        """批量版 ``query_usage``：一次事务内取多个实体各自起点之后的用量。

        ``items`` 为 ``(scope, group_id, user_id, since_ts)`` 序列，返回以
        ``(scope, group_id, user_id)`` 为键的 ``(calls, tokens)``。

        中台列表页每行都可能要按重置标记重新取数，逐行 await 会退化成 N+1 次
        「拿锁 + 切线程」往返；这里在同一次 ``_run`` 内循环 execute，锁与线程
        切换各只发生一次。
        """
        if not items:
            return {}

        def op(conn: sqlite3.Connection) -> dict[tuple[str, str, str], tuple[int, int]]:
            result: dict[tuple[str, str, str], tuple[int, int]] = {}
            for scope, group_id, user_id, since in items:
                where, params = _scope_predicate(scope, group_id, user_id)
                row = conn.execute(
                    "SELECT COALESCE(SUM(calls), 0), COALESCE(SUM(tokens), 0)"
                    f" FROM usage_events WHERE {where} AND ts >= ?",
                    [*params, int(since)],
                ).fetchone()
                result[(scope, group_id, user_id)] = (int(row[0]), int(row[1]))
            return result

        return await self._run(op)

    async def load_recent(self, cutoff_ts: float) -> list[tuple[str, str, int, int]]:
        def op(conn: sqlite3.Connection) -> list[tuple[str, str, int, int]]:
            return [
                (r[0], r[1], int(r[2]), int(r[3]))
                for r in conn.execute(
                    "SELECT group_id, user_id, ts, tokens FROM usage_events"
                    " WHERE ts >= ?",
                    [int(cutoff_ts)],
                ).fetchall()
            ]

        return await self._run(op)

    async def load_events_for_group(
        self, group_id: str, cutoff_ts: float
    ) -> list[tuple[str, str, int, int]]:
        def op(conn: sqlite3.Connection) -> list[tuple[str, str, int, int]]:
            return [
                (r[0], r[1], int(r[2]), int(r[3]))
                for r in conn.execute(
                    "SELECT group_id, user_id, ts, tokens FROM usage_events"
                    " WHERE group_id = ? AND ts >= ?",
                    [group_id, int(cutoff_ts)],
                ).fetchall()
            ]

        return await self._run(op)

    # --- 覆盖 -----------------------------------------------------------

    async def upsert_override(self, ov: Override) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO overrides"
                " (scope, group_id, user_id, call_limit, token_limit, window_hours,"
                "  enabled, fallback_provider_id, fallback_call_limit,"
                "  fallback_token_limit, long_calls, long_tokens, long_window_days,"
                "  self_reset_short, self_reset_long, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                "         strftime('%s','now'))"
                " ON CONFLICT(scope, group_id, user_id) DO UPDATE SET"
                "  call_limit=excluded.call_limit,"
                "  token_limit=excluded.token_limit,"
                "  window_hours=excluded.window_hours,"
                "  enabled=excluded.enabled,"
                "  fallback_provider_id=excluded.fallback_provider_id,"
                "  fallback_call_limit=excluded.fallback_call_limit,"
                "  fallback_token_limit=excluded.fallback_token_limit,"
                "  long_calls=excluded.long_calls,"
                "  long_tokens=excluded.long_tokens,"
                "  long_window_days=excluded.long_window_days,"
                "  self_reset_short=excluded.self_reset_short,"
                "  self_reset_long=excluded.self_reset_long,"
                "  updated_at=excluded.updated_at",
                [
                    ov.scope,
                    ov.group_id,
                    ov.user_id,
                    ov.call_limit,
                    ov.token_limit,
                    ov.window_hours,
                    1 if ov.enabled else 0,
                    ov.fallback_provider_id,
                    ov.fallback_call_limit,
                    ov.fallback_token_limit,
                    ov.long_calls,
                    ov.long_tokens,
                    ov.long_window_days,
                    ov.self_reset_short,
                    ov.self_reset_long,
                ],
            )
            conn.commit()

        await self._run(op)

    async def delete_override(self, scope: str, group_id: str, user_id: str) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "DELETE FROM overrides WHERE scope = ? AND group_id = ? AND user_id = ?",
                [scope, group_id, user_id],
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run(op)

    # 覆盖行的统一列序，供 get/list 复用。
    _OVERRIDE_COLUMNS = (
        "scope, group_id, user_id, call_limit, token_limit, window_hours, enabled,"
        " fallback_provider_id, fallback_call_limit, fallback_token_limit,"
        " long_calls, long_tokens, long_window_days, self_reset_short,"
        " self_reset_long"
    )

    @staticmethod
    def _row_to_override(row: tuple[Any, ...]) -> Override:
        return Override(
            scope=row[0],
            group_id=row[1],
            user_id=row[2],
            call_limit=row[3],
            token_limit=row[4],
            window_hours=row[5],
            enabled=bool(row[6]),
            fallback_provider_id=row[7],
            fallback_call_limit=row[8],
            fallback_token_limit=row[9],
            long_calls=row[10],
            long_tokens=row[11],
            long_window_days=row[12],
            self_reset_short=row[13],
            self_reset_long=row[14],
        )

    async def get_override(
        self, scope: str, group_id: str, user_id: str
    ) -> Override | None:
        def op(conn: sqlite3.Connection) -> Override | None:
            row = conn.execute(
                f"SELECT {self._OVERRIDE_COLUMNS} FROM overrides"
                " WHERE scope = ? AND group_id = ? AND user_id = ?",
                [scope, group_id, user_id],
            ).fetchone()
            return self._row_to_override(row) if row else None

        return await self._run(op)

    async def list_overrides(self) -> list[Override]:
        def op(conn: sqlite3.Connection) -> list[Override]:
            return [
                self._row_to_override(r)
                for r in conn.execute(
                    f"SELECT {self._OVERRIDE_COLUMNS} FROM overrides"
                ).fetchall()
            ]

        return await self._run(op)

    # --- 中台聚合 -------------------------------------------------------

    async def group_totals(self, cutoff_ts: float) -> list[tuple[str, int, int]]:
        def op(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
            return [
                (r[0], int(r[1]), int(r[2]))
                for r in conn.execute(
                    "SELECT group_id, SUM(calls), SUM(tokens) FROM usage_events"
                    " WHERE group_id != '' AND ts >= ? GROUP BY group_id"
                    " ORDER BY SUM(tokens) DESC, SUM(calls) DESC",
                    [int(cutoff_ts)],
                ).fetchall()
            ]

        return await self._run(op)

    async def member_totals(
        self, group_id: str, cutoff_ts: float
    ) -> list[tuple[str, int, int]]:
        def op(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
            return [
                (r[0], int(r[1]), int(r[2]))
                for r in conn.execute(
                    "SELECT user_id, SUM(calls), SUM(tokens) FROM usage_events"
                    " WHERE group_id = ? AND ts >= ? GROUP BY user_id"
                    " ORDER BY SUM(tokens) DESC, SUM(calls) DESC",
                    [group_id, int(cutoff_ts)],
                ).fetchall()
            ]

        return await self._run(op)

    async def private_totals(self, cutoff_ts: float) -> list[tuple[str, int, int]]:
        def op(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
            return [
                (r[0], int(r[1]), int(r[2]))
                for r in conn.execute(
                    "SELECT user_id, SUM(calls), SUM(tokens) FROM usage_events"
                    " WHERE group_id = '' AND ts >= ? GROUP BY user_id"
                    " ORDER BY SUM(tokens) DESC, SUM(calls) DESC",
                    [int(cutoff_ts)],
                ).fetchall()
            ]

        return await self._run(op)

    async def user_totals(self, cutoff_ts: float) -> list[tuple[str, int, int]]:
        """每个用户跨私聊与所有群的合计用量。"""

        def op(conn: sqlite3.Connection) -> list[tuple[str, int, int]]:
            return [
                (r[0], int(r[1]), int(r[2]))
                for r in conn.execute(
                    "SELECT user_id, SUM(calls), SUM(tokens) FROM usage_events"
                    " WHERE user_id != '' AND ts >= ? GROUP BY user_id"
                    " ORDER BY SUM(tokens) DESC, SUM(calls) DESC",
                    [int(cutoff_ts)],
                ).fetchall()
            ]

        return await self._run(op)

    async def overview(self, cutoff_ts: float) -> dict[str, int]:
        def op(conn: sqlite3.Connection) -> dict[str, int]:
            row = conn.execute(
                "SELECT COALESCE(SUM(calls), 0), COALESCE(SUM(tokens), 0),"
                " COUNT(DISTINCT CASE WHEN group_id != '' THEN group_id END),"
                " COUNT(DISTINCT CASE WHEN user_id != '' THEN user_id END)"
                " FROM usage_events WHERE ts >= ?",
                [int(cutoff_ts)],
            ).fetchone()
            return {
                "total_calls": int(row[0]),
                "total_tokens": int(row[1]),
                "active_groups": int(row[2] or 0),
                "active_users": int(row[3] or 0),
            }

        return await self._run(op)

    async def timeseries(
        self, cutoff_ts: float, bucket_seconds: int
    ) -> list[tuple[int, int, int]]:
        """按时间分桶的 ``(bucket_ts, calls, tokens)`` 序列，供中台画图。"""
        bucket = max(1, int(bucket_seconds))

        def op(conn: sqlite3.Connection) -> list[tuple[int, int, int]]:
            return [
                (int(r[0]), int(r[1]), int(r[2]))
                for r in conn.execute(
                    "SELECT (ts / ?) * ? AS b, SUM(calls), SUM(tokens)"
                    " FROM usage_events WHERE ts >= ? GROUP BY b ORDER BY b",
                    [bucket, bucket, int(cutoff_ts)],
                ).fetchall()
            ]

        return await self._run(op)

    # --- 名称 / 备注（entity_meta）-------------------------------------

    async def load_meta(self) -> list[tuple[str, str, str, str]]:
        """加载全部 ``(kind, entity_id, name, remark)``，供启动恢复缓存。"""

        def op(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
            return [
                (r[0], r[1], r[2], r[3])
                for r in conn.execute(
                    "SELECT kind, entity_id, name, remark FROM entity_meta"
                ).fetchall()
            ]

        return await self._run(op)

    async def upsert_names(self, kind: str, items: Sequence[tuple[str, str]]) -> None:
        """批量写入实体名称（保留已有 remark 不被覆盖）。``items`` 为 ``(entity_id, name)``。"""
        if not items:
            return

        def op(conn: sqlite3.Connection) -> None:
            conn.executemany(
                "INSERT INTO entity_meta(kind, entity_id, name, remark, updated_at)"
                " VALUES(?, ?, ?, '', strftime('%s','now'))"
                " ON CONFLICT(kind, entity_id) DO UPDATE SET"
                "  name=excluded.name, updated_at=excluded.updated_at",
                [(kind, eid, name) for eid, name in items],
            )
            conn.commit()

        await self._run(op)

    async def set_remark(self, kind: str, entity_id: str, remark: str) -> None:
        """设置实体备注（保留已捕获的 name 不被覆盖）。"""

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO entity_meta(kind, entity_id, name, remark, updated_at)"
                " VALUES(?, ?, '', ?, strftime('%s','now'))"
                " ON CONFLICT(kind, entity_id) DO UPDATE SET"
                "  remark=excluded.remark, updated_at=excluded.updated_at",
                [kind, entity_id, remark],
            )
            conn.commit()

        await self._run(op)

    # --- 封禁管理 -------------------------------------------------------

    async def set_ban(
        self,
        group_id: str,
        user_id: str,
        banned_at: int,
        expire_at: int,
        banned_by: str = "",
        reason: str = "",
    ) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO banned_users(group_id, user_id, banned_at, expire_at, banned_by, reason)"
                " VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(group_id, user_id) DO UPDATE SET"
                "  banned_at=excluded.banned_at,"
                "  expire_at=excluded.expire_at,"
                "  banned_by=excluded.banned_by,"
                "  reason=excluded.reason",
                [group_id, user_id, banned_at, expire_at, banned_by, reason],
            )
            conn.commit()

        await self._run(op)

    async def remove_ban(self, group_id: str, user_id: str) -> bool:
        def op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute(
                "DELETE FROM banned_users WHERE group_id = ? AND user_id = ?",
                [group_id, user_id],
            )
            conn.commit()
            return cur.rowcount > 0

        return await self._run(op)

    async def list_bans(
        self, now: int | None = None
    ) -> list[tuple[str, str, int, int, str, str]]:
        def op(conn: sqlite3.Connection) -> list[tuple[str, str, int, int, str, str]]:
            if now is not None:
                return conn.execute(
                    "SELECT group_id, user_id, banned_at, expire_at, banned_by, reason"
                    " FROM banned_users WHERE expire_at < 0 OR expire_at > ?"
                    " ORDER BY banned_at DESC",
                    [now],
                ).fetchall()
            return conn.execute(
                "SELECT group_id, user_id, banned_at, expire_at, banned_by, reason"
                " FROM banned_users ORDER BY banned_at DESC"
            ).fetchall()

        return await self._run(op)
