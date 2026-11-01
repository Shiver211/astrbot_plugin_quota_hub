"""``core/storage_state.py``（周期状态 / 重置日志 / 申请）的集成测试。

用临时库 + ``asyncio.run`` 驱动，风格对齐 ``tests/test_storage.py``。
"""

from __future__ import annotations

import asyncio

from core.cycles import ACTOR_ADMIN, ACTOR_SELF, CycleState, ResetLogEntry
from core.models import SCOPE_GROUP, SCOPE_MEMBER, SCOPE_PRIVATE, ScopeKey
from core.requests import (
    REQUEST_APPROVED,
    REQUEST_DENIED,
    REQUEST_PENDING,
    ResetRequest,
)
from core.storage import Storage


def _run(coro_fn):
    return asyncio.run(coro_fn())


async def _fresh(tmp_path):
    storage = Storage(tmp_path / "quota.db")
    await storage.initialize()
    return storage


# --- entity_state ---------------------------------------------------------


def test_load_states_empty_by_default(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        states = await storage.load_states()
        await storage.close()
        return states

    assert _run(lambda: scenario()) == []


def test_save_and_load_state_roundtrip(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        key = ScopeKey(SCOPE_MEMBER, "g1", "u1")
        state = CycleState(
            window_start=1000.0,
            short_marker=500.0,
            short_allowance=2,
            long_allowance=1,
            closed_by_reset=False,
        )
        await storage.save_state(key, state)
        states = await storage.load_states()
        await storage.close()
        return states

    states = _run(lambda: scenario())
    assert len(states) == 1
    got_key, got_state = states[0]
    assert got_key == ScopeKey(SCOPE_MEMBER, "g1", "u1")
    assert got_state == CycleState(
        window_start=1000.0,
        short_marker=500.0,
        short_allowance=2,
        long_allowance=1,
        closed_by_reset=False,
    )


def test_save_state_upserts(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        key = ScopeKey(SCOPE_PRIVATE, "", "u1")
        await storage.save_state(key, CycleState(window_start=100.0))
        await storage.save_state(key, CycleState(window_start=200.0, short_allowance=3))
        states = await storage.load_states()
        await storage.close()
        return states

    states = _run(lambda: scenario())
    assert len(states) == 1
    got_key, got_state = states[0]
    assert got_key == ScopeKey(SCOPE_PRIVATE, "", "u1")
    assert got_state.window_start == 200.0
    assert got_state.short_allowance == 3


def test_save_state_window_start_none_persists_as_null(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        key = ScopeKey(SCOPE_GROUP, "g1", "")
        await storage.save_state(
            key, CycleState(window_start=None, closed_by_reset=True)
        )
        states = await storage.load_states()
        await storage.close()
        return states

    states = _run(lambda: scenario())
    assert len(states) == 1
    _, state = states[0]
    assert state.window_start is None
    assert state.closed_by_reset is True


def test_delete_state_removes_row(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        key = ScopeKey(SCOPE_PRIVATE, "", "u1")
        await storage.save_state(key, CycleState(window_start=100.0))
        await storage.delete_state(key)
        states = await storage.load_states()
        await storage.close()
        return states

    assert _run(lambda: scenario()) == []


def test_delete_state_missing_key_is_noop(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.delete_state(ScopeKey(SCOPE_PRIVATE, "", "u_missing"))
        states = await storage.load_states()
        await storage.close()
        return states

    assert _run(lambda: scenario()) == []


def test_load_states_multiple_scopes(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.save_state(
            ScopeKey(SCOPE_PRIVATE, "", "u1"), CycleState(window_start=1.0)
        )
        await storage.save_state(
            ScopeKey(SCOPE_GROUP, "g1", ""), CycleState(window_start=2.0)
        )
        await storage.save_state(
            ScopeKey(SCOPE_MEMBER, "g1", "u1"), CycleState(window_start=3.0)
        )
        states = await storage.load_states()
        await storage.close()
        return states

    states = _run(lambda: scenario())
    keys = {k for k, _ in states}
    assert keys == {
        ScopeKey(SCOPE_PRIVATE, "", "u1"),
        ScopeKey(SCOPE_GROUP, "g1", ""),
        ScopeKey(SCOPE_MEMBER, "g1", "u1"),
    }


# --- reset_log --------------------------------------------------------------


def test_append_reset_log_returns_id(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        entry = ResetLogEntry(
            ts=1000, scope=SCOPE_PRIVATE, group_id="", user_id="u1",
            window="short", actor=ACTOR_SELF, actor_id="u1", consumed=True,
        )
        first_id = await storage.append_reset_log(entry)
        second_id = await storage.append_reset_log(entry)
        await storage.close()
        return first_id, second_id

    first_id, second_id = _run(lambda: scenario())
    assert first_id >= 1
    assert second_id == first_id + 1


def test_list_reset_log_orders_by_ts_desc(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.append_reset_log(
            ResetLogEntry(
                ts=1000, scope=SCOPE_PRIVATE, group_id="", user_id="u1",
                window="short", actor=ACTOR_SELF,
            )
        )
        await storage.append_reset_log(
            ResetLogEntry(
                ts=3000, scope=SCOPE_GROUP, group_id="g1", user_id="",
                window="long", actor=ACTOR_ADMIN, actor_id="admin1", consumed=False,
            )
        )
        await storage.append_reset_log(
            ResetLogEntry(
                ts=2000, scope=SCOPE_MEMBER, group_id="g1", user_id="u2",
                window="short", actor=ACTOR_SELF, consumed=True,
            )
        )
        entries = await storage.list_reset_log()
        await storage.close()
        return entries

    entries = _run(lambda: scenario())
    assert [e.ts for e in entries] == [3000, 2000, 1000]
    assert entries[0].scope == SCOPE_GROUP
    assert entries[0].actor == ACTOR_ADMIN
    assert entries[0].actor_id == "admin1"
    assert entries[0].consumed is False
    assert entries[1].consumed is True
    assert all(e.id for e in entries)


def test_list_reset_log_respects_limit(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        for i in range(5):
            await storage.append_reset_log(
                ResetLogEntry(
                    ts=1000 + i, scope=SCOPE_PRIVATE, group_id="", user_id="u1",
                    window="short", actor=ACTOR_SELF,
                )
            )
        entries = await storage.list_reset_log(limit=2)
        await storage.close()
        return entries

    entries = _run(lambda: scenario())
    assert len(entries) == 2
    assert [e.ts for e in entries] == [1004, 1003]


def test_purge_reset_log_drops_only_entries_before_cutoff(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        for i in range(5):
            await storage.append_reset_log(
                ResetLogEntry(
                    ts=1000 + i, scope=SCOPE_PRIVATE, group_id="", user_id="u1",
                    window="short", actor=ACTOR_SELF,
                )
            )
        removed = await storage.purge_reset_log(1003)
        entries = await storage.list_reset_log()
        await storage.close()
        return removed, entries

    removed, entries = _run(lambda: scenario())
    assert removed == 3  # 1000 / 1001 / 1002
    assert [e.ts for e in entries] == [1004, 1003]  # 恰在 cutoff 上的保留


def test_purge_reset_log_on_empty_table_is_noop(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        removed = await storage.purge_reset_log(9999)
        await storage.close()
        return removed

    assert _run(lambda: scenario()) == 0


# --- reset_requests ----------------------------------------------------------


def _make_request(**overrides):
    base = {
        "scope": SCOPE_MEMBER,
        "group_id": "g1",
        "user_id": "u1",
        "window": "short",
        "amount": 1,
        "reason": "需要更多次数",
        "status": REQUEST_PENDING,
        "created_at": 1000.0,
        "cycle_start": 500.0,
        "requester_origin": "aiocqhttp:GroupMessage:g1_u1",
    }
    base.update(overrides)
    return ResetRequest(**base)


def test_insert_request_assigns_incrementing_id(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        first = await storage.insert_request(_make_request())
        second = await storage.insert_request(_make_request(user_id="u2"))
        await storage.close()
        return first, second

    first, second = _run(lambda: scenario())
    assert first.id >= 1
    assert second.id == first.id + 1
    # 返回值是带 id 的新副本，其余字段保持一致
    assert first.scope == SCOPE_MEMBER
    assert first.amount == 1


def test_get_request_roundtrip(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        inserted = await storage.insert_request(_make_request())
        got = await storage.get_request(inserted.id)
        await storage.close()
        return inserted, got

    inserted, got = _run(lambda: scenario())
    assert got == inserted


def test_get_request_missing_returns_none(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        got = await storage.get_request(999)
        await storage.close()
        return got

    assert _run(lambda: scenario()) is None


def test_update_request_persists_all_fields(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        inserted = await storage.insert_request(_make_request())
        import dataclasses

        approved = dataclasses.replace(
            inserted,
            status=REQUEST_APPROVED,
            decided_at=2000.0,
            decided_by="admin1",
            granted=2,
            decision_note="",
            notified=True,
        )
        await storage.update_request(approved)
        got = await storage.get_request(inserted.id)
        await storage.close()
        return got

    got = _run(lambda: scenario())
    assert got.status == REQUEST_APPROVED
    assert got.decided_at == 2000.0
    assert got.decided_by == "admin1"
    assert got.granted == 2
    assert got.notified is True


def test_get_pending_request_returns_only_pending(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        key = ScopeKey(SCOPE_MEMBER, "g1", "u1")
        pending = await storage.insert_request(_make_request())
        # 决议后不再是 pending
        import dataclasses

        decided = await storage.insert_request(_make_request(user_id="u1"))
        await storage.update_request(
            dataclasses.replace(decided, status=REQUEST_DENIED, decided_at=1500.0)
        )
        got_before = await storage.get_pending_request(key)
        await storage.close()
        return pending, got_before

    pending, got_before = _run(lambda: scenario())
    assert got_before is not None
    assert got_before.id == pending.id
    assert got_before.status == REQUEST_PENDING


def test_get_pending_request_none_when_absent(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        got = await storage.get_pending_request(ScopeKey(SCOPE_PRIVATE, "", "u_none"))
        await storage.close()
        return got

    assert _run(lambda: scenario()) is None


def test_list_requests_filters_by_status_and_orders_desc(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        import dataclasses

        r1 = await storage.insert_request(_make_request(created_at=1000.0))
        r2 = await storage.insert_request(
            _make_request(user_id="u2", created_at=2000.0)
        )
        await storage.update_request(
            dataclasses.replace(r2, status=REQUEST_APPROVED, decided_at=2500.0)
        )
        all_requests = await storage.list_requests()
        pending_only = await storage.list_requests(status=REQUEST_PENDING)
        await storage.close()
        return r1, all_requests, pending_only

    r1, all_requests, pending_only = _run(lambda: scenario())
    assert [r.created_at for r in all_requests] == [2000.0, 1000.0]
    assert len(pending_only) == 1
    assert pending_only[0].id == r1.id


def test_list_requests_respects_limit(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        for i in range(5):
            await storage.insert_request(
                _make_request(user_id=f"u{i}", created_at=1000.0 + i)
            )
        limited = await storage.list_requests(limit=2)
        await storage.close()
        return limited

    limited = _run(lambda: scenario())
    assert len(limited) == 2
    assert [r.created_at for r in limited] == [1004.0, 1003.0]


def test_list_undelivered_filters_decided_and_not_notified(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        import dataclasses

        key = ScopeKey(SCOPE_MEMBER, "g1", "u1")
        pending = await storage.insert_request(_make_request())
        approved_unnotified = await storage.insert_request(
            _make_request(user_id="u1", created_at=1100.0)
        )
        await storage.update_request(
            dataclasses.replace(
                approved_unnotified,
                status=REQUEST_APPROVED,
                decided_at=1500.0,
                granted=1,
                notified=False,
            )
        )
        approved_notified = await storage.insert_request(
            _make_request(user_id="u1", created_at=1200.0)
        )
        await storage.update_request(
            dataclasses.replace(
                approved_notified,
                status=REQUEST_APPROVED,
                decided_at=1600.0,
                granted=1,
                notified=True,
            )
        )
        undelivered = await storage.list_undelivered(key)
        await storage.close()
        return pending, approved_unnotified, undelivered

    pending, approved_unnotified, undelivered = _run(lambda: scenario())
    ids = {r.id for r in undelivered}
    assert ids == {approved_unnotified.id}
    assert pending.id not in ids


def test_list_undelivered_scoped_to_key(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        import dataclasses

        req = await storage.insert_request(_make_request(user_id="u1"))
        await storage.update_request(
            dataclasses.replace(
                req, status=REQUEST_DENIED, decided_at=1500.0, notified=False
            )
        )
        other_key = ScopeKey(SCOPE_MEMBER, "g1", "u2")
        undelivered_other = await storage.list_undelivered(other_key)
        undelivered_self = await storage.list_undelivered(
            ScopeKey(SCOPE_MEMBER, "g1", "u1")
        )
        await storage.close()
        return undelivered_other, undelivered_self

    undelivered_other, undelivered_self = _run(lambda: scenario())
    assert undelivered_other == []
    assert len(undelivered_self) == 1


def test_request_group_scope_user_id_empty(tmp_path):
    """group 作用域请求 user_id 为空串，仍能正确落库/查询。"""

    async def scenario():
        storage = await _fresh(tmp_path)
        req = await storage.insert_request(
            _make_request(scope=SCOPE_GROUP, group_id="g1", user_id="")
        )
        got = await storage.get_pending_request(ScopeKey(SCOPE_GROUP, "g1", ""))
        await storage.close()
        return req, got

    req, got = _run(lambda: scenario())
    assert got is not None
    assert got.id == req.id


# --- overrides 五个新字段（迁移 + 往返） ------------------------------------


def test_migrate_adds_long_window_columns_to_legacy_db(tmp_path):
    import sqlite3

    db = tmp_path / "quota.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE overrides ("
        " scope TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',"
        " user_id TEXT NOT NULL DEFAULT '', call_limit INTEGER, token_limit INTEGER,"
        " window_hours REAL, enabled INTEGER NOT NULL DEFAULT 1,"
        " fallback_provider_id TEXT, fallback_call_limit INTEGER,"
        " fallback_token_limit INTEGER,"
        " updated_at INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (scope, group_id, user_id));"
        " INSERT INTO overrides(scope, group_id, user_id, call_limit, enabled)"
        " VALUES('group', 'g1', '', 7, 1);"
    )
    conn.commit()
    conn.close()

    async def scenario():
        storage = await _fresh_existing(db)
        columns = await _table_columns(storage, "overrides")
        got = await storage.get_override("group", "g1", "")
        await storage.close()
        return columns, got

    async def _fresh_existing(path):
        s = Storage(path)
        await s.initialize()
        return s

    async def _table_columns(storage, table):
        def op(conn):
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

        return await storage._run(op)

    columns, got = _run(lambda: scenario())
    assert {
        "long_calls",
        "long_tokens",
        "long_window_days",
        "self_reset_short",
        "self_reset_long",
    } <= columns
    assert got.call_limit == 7
    assert got.long_calls is None
    assert got.long_tokens is None
    assert got.long_window_days is None
    assert got.self_reset_short is None
    assert got.self_reset_long is None


def test_override_long_window_fields_roundtrip(tmp_path):
    from core.models import SCOPE_MEMBER, Override

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.upsert_override(
            Override(
                scope=SCOPE_MEMBER,
                group_id="g1",
                user_id="u1",
                long_calls=100,
                long_tokens=20000,
                long_window_days=14.0,
                self_reset_short=2,
                self_reset_long=1,
            )
        )
        got = await storage.get_override(SCOPE_MEMBER, "g1", "u1")
        await storage.close()
        return got

    got = _run(lambda: scenario())
    assert got.long_calls == 100
    assert got.long_tokens == 20000
    assert got.long_window_days == 14.0
    assert got.self_reset_short == 2
    assert got.self_reset_long == 1


def test_override_long_window_fields_none_stay_none(tmp_path):
    from core.models import SCOPE_PRIVATE, Override

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.upsert_override(Override(scope=SCOPE_PRIVATE, user_id="u1"))
        got = await storage.get_override(SCOPE_PRIVATE, "", "u1")
        await storage.close()
        return got

    got = _run(lambda: scenario())
    assert got.long_calls is None
    assert got.long_tokens is None
    assert got.long_window_days is None
    assert got.self_reset_short is None
    assert got.self_reset_long is None
