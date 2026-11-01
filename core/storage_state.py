"""周期状态 / 重置日志 / 重置次数申请的存储层。

三张表：
- ``entity_state``：每个限流实体的周期状态（长窗口开窗时刻、短窗口重置标记、
  剩余自助重置次数……），与 ``core.cycles.CycleState`` 一一对应。
- ``reset_log``：每次重置（self / admin / dashboard / auto）的审计记录。
- ``reset_requests``：追加重置次数的申请及其审批结果。

以 Mixin 注入 ``Storage``（复用其 ``_run`` 串行化机制），风格对齐
``core/storage_history.py``。
"""

from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any

from .cycles import CycleState, ResetLogEntry
from .models import ScopeKey
from .requests import ResetRequest

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_state (
    scope TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
    window_start REAL,                       -- NULL = 无周期
    short_marker REAL NOT NULL DEFAULT 0,
    short_allowance INTEGER NOT NULL DEFAULT 0,
    long_allowance INTEGER NOT NULL DEFAULT 0,
    closed_by_reset INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, group_id, user_id)
);
CREATE TABLE IF NOT EXISTS reset_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,
    scope TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
    window TEXT NOT NULL, actor TEXT NOT NULL, actor_id TEXT NOT NULL DEFAULT '',
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qh_reset_log_ts ON reset_log(ts);
CREATE TABLE IF NOT EXISTS reset_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL DEFAULT '',
    window TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, created_at INTEGER NOT NULL, cycle_start REAL NOT NULL DEFAULT 0,
    requester_origin TEXT NOT NULL DEFAULT '',
    decided_at INTEGER, decided_by TEXT NOT NULL DEFAULT '', granted INTEGER NOT NULL DEFAULT 0,
    decision_note TEXT NOT NULL DEFAULT '', notified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qh_requests_status ON reset_requests(status);
"""

# 与 SQL 列序对应，供 _row_to_* 复用。
_STATE_COLUMNS = (
    "scope, group_id, user_id, window_start, short_marker, short_allowance,"
    " long_allowance, closed_by_reset"
)
_REQUEST_COLUMNS = (
    "id, scope, group_id, user_id, window, amount, reason, status, created_at,"
    " cycle_start, requester_origin, decided_at, decided_by, granted,"
    " decision_note, notified"
)


class StateStorageMixin:
    """依赖宿主类提供 ``_run(fn)``（见 ``Storage``）。"""

    # --- 行 <-> 数据类 转换 -------------------------------------------------

    @staticmethod
    def _row_to_state(row: tuple[Any, ...]) -> tuple[ScopeKey, CycleState]:
        key = ScopeKey(scope=row[0], group_id=row[1], user_id=row[2])
        state = CycleState(
            window_start=row[3],
            short_marker=row[4],
            short_allowance=row[5],
            long_allowance=row[6],
            closed_by_reset=bool(row[7]),
        )
        return key, state

    @staticmethod
    def _row_to_log(row: tuple[Any, ...]) -> ResetLogEntry:
        return ResetLogEntry(
            id=row[0],
            ts=row[1],
            scope=row[2],
            group_id=row[3],
            user_id=row[4],
            window=row[5],
            actor=row[6],
            actor_id=row[7],
            consumed=bool(row[8]),
        )

    @staticmethod
    def _row_to_request(row: tuple[Any, ...]) -> ResetRequest:
        return ResetRequest(
            id=row[0],
            scope=row[1],
            group_id=row[2],
            user_id=row[3],
            window=row[4],
            amount=row[5],
            reason=row[6],
            status=row[7],
            created_at=row[8],
            cycle_start=row[9],
            requester_origin=row[10],
            decided_at=row[11],
            decided_by=row[12],
            granted=row[13],
            decision_note=row[14],
            notified=bool(row[15]),
        )

    # --- entity_state -------------------------------------------------------

    async def load_states(self) -> list[tuple[ScopeKey, CycleState]]:
        def op(conn: sqlite3.Connection) -> list[tuple[ScopeKey, CycleState]]:
            return [
                self._row_to_state(r)
                for r in conn.execute(
                    f"SELECT {_STATE_COLUMNS} FROM entity_state"
                ).fetchall()
            ]

        return await self._run(op)  # type: ignore[attr-defined]

    async def save_state(self, key: ScopeKey, state: CycleState) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO entity_state"
                " (scope, group_id, user_id, window_start, short_marker,"
                "  short_allowance, long_allowance, closed_by_reset, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%s','now'))"
                " ON CONFLICT(scope, group_id, user_id) DO UPDATE SET"
                "  window_start=excluded.window_start,"
                "  short_marker=excluded.short_marker,"
                "  short_allowance=excluded.short_allowance,"
                "  long_allowance=excluded.long_allowance,"
                "  closed_by_reset=excluded.closed_by_reset,"
                "  updated_at=excluded.updated_at",
                [
                    key.scope,
                    key.group_id,
                    key.user_id,
                    state.window_start,
                    state.short_marker,
                    state.short_allowance,
                    state.long_allowance,
                    1 if state.closed_by_reset else 0,
                ],
            )
            conn.commit()

        await self._run(op)  # type: ignore[attr-defined]

    async def delete_state(self, key: ScopeKey) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "DELETE FROM entity_state WHERE scope = ? AND group_id = ? AND user_id = ?",
                [key.scope, key.group_id, key.user_id],
            )
            conn.commit()

        await self._run(op)  # type: ignore[attr-defined]

    # --- reset_log ------------------------------------------------------------

    async def append_reset_log(self, entry: ResetLogEntry) -> int:
        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO reset_log"
                " (ts, scope, group_id, user_id, window, actor, actor_id, consumed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    int(entry.ts),
                    entry.scope,
                    entry.group_id,
                    entry.user_id,
                    entry.window,
                    entry.actor,
                    entry.actor_id,
                    1 if entry.consumed else 0,
                ],
            )
            conn.commit()
            return int(cur.lastrowid)

        return await self._run(op)  # type: ignore[attr-defined]

    async def list_reset_log(self, limit: int = 100) -> list[ResetLogEntry]:
        def op(conn: sqlite3.Connection) -> list[ResetLogEntry]:
            return [
                self._row_to_log(r)
                for r in conn.execute(
                    "SELECT id, ts, scope, group_id, user_id, window, actor,"
                    " actor_id, consumed FROM reset_log"
                    " ORDER BY ts DESC, id DESC LIMIT ?",
                    [limit],
                ).fetchall()
            ]

        return await self._run(op)  # type: ignore[attr-defined]

    async def purge_reset_log(self, cutoff_ts: float) -> int:
        """删除 ``ts < cutoff_ts`` 的重置审计记录，返回删除条数。

        审计日志只增不减，长期运行会无界增长；与用量明细共用 ``retention_days``
        保留期。删掉旧日志不影响任何判定：限流真值在 ``entity_state`` 与聚合器里，
        ``reset_log`` 纯作展示与审计。
        """

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute("DELETE FROM reset_log WHERE ts < ?", [int(cutoff_ts)])
            conn.commit()
            return cur.rowcount

        return await self._run(op)  # type: ignore[attr-defined]

    # --- reset_requests ---------------------------------------------------------

    async def insert_request(self, req: ResetRequest) -> ResetRequest:
        def op(conn: sqlite3.Connection) -> ResetRequest:
            cur = conn.execute(
                "INSERT INTO reset_requests"
                " (scope, group_id, user_id, window, amount, reason, status,"
                "  created_at, cycle_start, requester_origin, decided_at,"
                "  decided_by, granted, decision_note, notified)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    req.scope,
                    req.group_id,
                    req.user_id,
                    req.window,
                    req.amount,
                    req.reason,
                    req.status,
                    int(req.created_at),
                    req.cycle_start,
                    req.requester_origin,
                    None if req.decided_at is None else int(req.decided_at),
                    req.decided_by,
                    req.granted,
                    req.decision_note,
                    1 if req.notified else 0,
                ],
            )
            conn.commit()
            return dataclasses.replace(req, id=int(cur.lastrowid))

        return await self._run(op)  # type: ignore[attr-defined]

    async def update_request(self, req: ResetRequest) -> None:
        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE reset_requests SET"
                "  scope=?, group_id=?, user_id=?, window=?, amount=?, reason=?,"
                "  status=?, created_at=?, cycle_start=?, requester_origin=?,"
                "  decided_at=?, decided_by=?, granted=?, decision_note=?,"
                "  notified=?"
                " WHERE id=?",
                [
                    req.scope,
                    req.group_id,
                    req.user_id,
                    req.window,
                    req.amount,
                    req.reason,
                    req.status,
                    int(req.created_at),
                    req.cycle_start,
                    req.requester_origin,
                    None if req.decided_at is None else int(req.decided_at),
                    req.decided_by,
                    req.granted,
                    req.decision_note,
                    1 if req.notified else 0,
                    req.id,
                ],
            )
            conn.commit()

        await self._run(op)  # type: ignore[attr-defined]

    async def get_request(self, request_id: int) -> ResetRequest | None:
        def op(conn: sqlite3.Connection) -> ResetRequest | None:
            row = conn.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM reset_requests WHERE id = ?",
                [request_id],
            ).fetchone()
            return self._row_to_request(row) if row else None

        return await self._run(op)  # type: ignore[attr-defined]

    async def get_pending_request(self, key: ScopeKey) -> ResetRequest | None:
        def op(conn: sqlite3.Connection) -> ResetRequest | None:
            row = conn.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM reset_requests"
                " WHERE scope = ? AND group_id = ? AND user_id = ? AND status = 'pending'"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                [key.scope, key.group_id, key.user_id],
            ).fetchone()
            return self._row_to_request(row) if row else None

        return await self._run(op)  # type: ignore[attr-defined]

    async def list_requests(
        self, status: str | None = None, limit: int = 100
    ) -> list[ResetRequest]:
        def op(conn: sqlite3.Connection) -> list[ResetRequest]:
            if status is None:
                rows = conn.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM reset_requests"
                    " ORDER BY created_at DESC, id DESC LIMIT ?",
                    [limit],
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {_REQUEST_COLUMNS} FROM reset_requests"
                    " WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                    [status, limit],
                ).fetchall()
            return [self._row_to_request(r) for r in rows]

        return await self._run(op)  # type: ignore[attr-defined]

    async def list_undelivered(self, key: ScopeKey) -> list[ResetRequest]:
        def op(conn: sqlite3.Connection) -> list[ResetRequest]:
            rows = conn.execute(
                f"SELECT {_REQUEST_COLUMNS} FROM reset_requests"
                " WHERE scope = ? AND group_id = ? AND user_id = ?"
                " AND status IN ('approved', 'denied', 'void') AND notified = 0"
                " ORDER BY created_at DESC, id DESC",
                [key.scope, key.group_id, key.user_id],
            ).fetchall()
            return [self._row_to_request(r) for r in rows]

        return await self._run(op)  # type: ignore[attr-defined]
