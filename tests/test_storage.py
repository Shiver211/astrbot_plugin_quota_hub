"""SQLite 存储层的集成测试（用临时库，asyncio.run 驱动）。"""

from __future__ import annotations

import asyncio
import sqlite3

from core.models import SCOPE_GROUP, SCOPE_MEMBER, SCOPE_PRIVATE, Override
from core.storage import Storage


def _run(coro_fn):
    return asyncio.run(coro_fn())


async def _fresh(tmp_path):
    storage = Storage(tmp_path / "quota.db")
    await storage.initialize()
    return storage


def test_initialize_creates_db_file(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.close()

    _run(lambda: scenario())
    assert (tmp_path / "quota.db").exists()


def test_insert_and_query_usage_across_scopes(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        # 一条群聊事件：group g1 / user u1
        await storage.insert_events(
            [(1, 1000, "aiocqhttp", "g1", "u1", 1, 0, "gpt")]
        )
        await storage.update_tokens([(120, 1)])
        group = await storage.query_usage(SCOPE_GROUP, "g1", "", 0)
        member = await storage.query_usage(SCOPE_MEMBER, "g1", "u1", 0)
        private = await storage.query_usage(SCOPE_PRIVATE, "", "u1", 0)
        await storage.close()
        return group, member, private

    group, member, private = _run(lambda: scenario())
    assert group == (1, 120)
    assert member == (1, 120)
    assert private == (0, 0)  # 群聊事件不计入私聊


def test_query_usage_respects_cutoff(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, 1000, "p", "", "u1", 1, 10, ""),
                (2, 5000, "p", "", "u1", 1, 20, ""),
            ]
        )
        recent = await storage.query_usage(SCOPE_PRIVATE, "", "u1", 4000)
        await storage.close()
        return recent

    assert _run(lambda: scenario()) == (1, 20)


def test_usage_since_markers_batches_per_entity_cutoffs(tmp_path):
    """一次调用取多个实体、各自不同起点的用量（等价于逐个 query_usage）。"""

    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, 1000, "p", "g1", "u1", 1, 10, ""),
                (2, 5000, "p", "g1", "u1", 1, 20, ""),
                (3, 5000, "p", "g1", "u2", 1, 30, ""),
                (4, 1000, "p", "", "u3", 1, 40, ""),
            ]
        )
        rows = await storage.usage_since_markers(
            [
                (SCOPE_GROUP, "g1", "", 4000.0),
                (SCOPE_MEMBER, "g1", "u1", 0.0),
                (SCOPE_MEMBER, "g1", "u2", 9000.0),
                (SCOPE_PRIVATE, "", "u3", 0.0),
            ]
        )
        single = await storage.query_usage(SCOPE_GROUP, "g1", "", 4000)
        await storage.close()
        return rows, single

    rows, single = _run(lambda: scenario())
    assert rows[(SCOPE_GROUP, "g1", "")] == (2, 50)  # 4000 之后的两条
    assert rows[(SCOPE_MEMBER, "g1", "u1")] == (2, 30)
    assert rows[(SCOPE_MEMBER, "g1", "u2")] == (0, 0)  # 起点晚于全部用量
    assert rows[(SCOPE_PRIVATE, "", "u3")] == (1, 40)
    assert rows[(SCOPE_GROUP, "g1", "")] == single  # 与单条查询口径一致


def test_usage_since_markers_empty_input_skips_query(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.close()  # 已关闭：若真去查库会抛 RuntimeError
        return await storage.usage_since_markers([])

    assert _run(lambda: scenario()) == {}


def test_load_recent_for_rebuild(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, 1000, "p", "g1", "u1", 1, 5, ""),
                (2, 2000, "p", "", "u2", 1, 7, ""),
            ]
        )
        rows = await storage.load_recent(0)
        await storage.close()
        return sorted(rows)

    rows = _run(lambda: scenario())
    assert ("", "u2", 2000, 7) in rows
    assert ("g1", "u1", 1000, 5) in rows


def test_delete_events_member_vs_group(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, 1000, "p", "g1", "u1", 1, 0, ""),
                (2, 1000, "p", "g1", "u2", 1, 0, ""),
            ]
        )
        await storage.delete_events(SCOPE_MEMBER, "g1", "u1")
        after_member = await storage.query_usage(SCOPE_GROUP, "g1", "", 0)
        await storage.delete_events(SCOPE_GROUP, "g1", "")
        after_group = await storage.query_usage(SCOPE_GROUP, "g1", "", 0)
        await storage.close()
        return after_member, after_group

    after_member, after_group = _run(lambda: scenario())
    assert after_member == (1, 0)  # 删了 u1，群里还剩 u2 的一次
    assert after_group == (0, 0)  # 整群清空


def test_purge_old_removes_aged_rows(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, 1000, "p", "", "u1", 1, 0, ""),
                (2, 9000, "p", "", "u1", 1, 0, ""),
            ]
        )
        deleted = await storage.purge_old(5000)
        remaining = await storage.query_usage(SCOPE_PRIVATE, "", "u1", 0)
        await storage.close()
        return deleted, remaining

    deleted, remaining = _run(lambda: scenario())
    assert deleted == 1
    assert remaining == (1, 0)


def test_override_upsert_list_get_delete(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        ov = Override(
            scope=SCOPE_MEMBER,
            group_id="g1",
            user_id="u1",
            call_limit=5,
            token_limit=None,
            window_hours=2.0,
            enabled=True,
        )
        await storage.upsert_override(ov)
        got = await storage.get_override(SCOPE_MEMBER, "g1", "u1")
        # 更新同一实体
        await storage.upsert_override(
            Override(scope=SCOPE_MEMBER, group_id="g1", user_id="u1", call_limit=9)
        )
        listed = await storage.list_overrides()
        updated = await storage.get_override(SCOPE_MEMBER, "g1", "u1")
        deleted = await storage.delete_override(SCOPE_MEMBER, "g1", "u1")
        gone = await storage.get_override(SCOPE_MEMBER, "g1", "u1")
        await storage.close()
        return got, listed, updated, deleted, gone

    got, listed, updated, deleted, gone = _run(lambda: scenario())
    assert got.call_limit == 5 and got.window_hours == 2.0
    assert len(listed) == 1
    assert updated.call_limit == 9
    assert deleted is True
    assert gone is None


def test_dashboard_aggregates(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, 1000, "p", "g1", "u1", 1, 100, ""),
                (2, 1000, "p", "g1", "u2", 1, 50, ""),
                (3, 1000, "p", "", "u1", 1, 30, ""),
            ]
        )
        groups = await storage.group_totals(0)
        members = await storage.member_totals("g1", 0)
        users = await storage.user_totals(0)
        overview = await storage.overview(0)
        await storage.close()
        return groups, members, users, overview

    groups, members, users, overview = _run(lambda: scenario())
    assert {g: (c, t) for g, c, t in groups} == {"g1": (2, 150)}
    assert {u: (c, t) for u, c, t in members} == {"u1": (1, 100), "u2": (1, 50)}
    # u1 跨私聊+群：2 次 130 token
    assert {u: (c, t) for u, c, t in users}["u1"] == (2, 130)
    assert overview["total_calls"] == 3
    assert overview["total_tokens"] == 180
    assert overview["active_groups"] == 1
    assert overview["active_users"] == 2


def test_entity_meta_name_and_remark_are_independent(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        # 先写名称，再写备注：两者互不覆盖
        await storage.upsert_names("group", [("g1", "测试群"), ("g2", "二群")])
        await storage.set_remark("group", "g1", "VIP 群")
        # 名称再次刷新不应抹掉备注
        await storage.upsert_names("group", [("g1", "测试群-改名")])
        # 备注更新不应抹掉名称
        await storage.set_remark("group", "g2", "备注2")
        meta = {(k, e): (n, r) for k, e, n, r in await storage.load_meta()}
        await storage.close()
        return meta

    meta = _run(lambda: scenario())
    assert meta[("group", "g1")] == ("测试群-改名", "VIP 群")
    assert meta[("group", "g2")] == ("二群", "备注2")


def test_remark_before_name_keeps_both(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        # 先设备注（实体尚无名称），再捕获到名称
        await storage.set_remark("group", "g9", "先备注")
        await storage.upsert_names("group", [("g9", "后名称")])
        meta = {(k, e): (n, r) for k, e, n, r in await storage.load_meta()}
        await storage.close()
        return meta

    meta = _run(lambda: scenario())
    assert meta[("group", "g9")] == ("后名称", "先备注")


def test_timeseries_buckets_by_interval(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.insert_events(
            [
                (1, 1000, "p", "", "u1", 1, 10, ""),
                (2, 1050, "p", "", "u1", 1, 20, ""),  # 同一个 100s 桶(1000)
                (3, 1250, "p", "", "u1", 1, 5, ""),  # 下一个桶(1200)
            ]
        )
        series = await storage.timeseries(0, bucket_seconds=100)
        await storage.close()
        return series

    series = _run(lambda: scenario())
    assert series == [(1000, 2, 30), (1200, 1, 5)]


def test_override_roundtrip_fallback_fields(tmp_path):
    async def scenario():
        storage = await _fresh(tmp_path)
        await storage.upsert_override(
            Override(
                scope=SCOPE_GROUP, group_id="g9", call_limit=10,
                fallback_provider_id="prov_b", fallback_call_limit=5,
                fallback_token_limit=100,
            )
        )
        got = await storage.get_override(SCOPE_GROUP, "g9", "")
        await storage.close()
        return got

    got = _run(lambda: scenario())
    assert got.fallback_provider_id == "prov_b"
    assert got.fallback_call_limit == 5
    assert got.fallback_token_limit == 100


def test_migrate_adds_fallback_columns_to_legacy_db(tmp_path):
    # 模拟无回退列的旧库：初始化时应自动补列，旧行新列为 NULL（= 继承默认）。
    db = tmp_path / "quota.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE overrides ("
        " scope TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',"
        " user_id TEXT NOT NULL DEFAULT '', call_limit INTEGER, token_limit INTEGER,"
        " window_hours REAL, enabled INTEGER NOT NULL DEFAULT 1,"
        " updated_at INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (scope, group_id, user_id));"
        " INSERT INTO overrides(scope, group_id, user_id, call_limit, enabled)"
        " VALUES('group', 'g1', '', 7, 1);"
    )
    conn.commit()
    conn.close()

    async def scenario():
        storage = Storage(db)
        await storage.initialize()  # 触发迁移
        got = await storage.get_override("group", "g1", "")
        await storage.close()
        return got

    got = _run(lambda: scenario())
    assert got.call_limit == 7
    assert got.fallback_provider_id is None
    assert got.fallback_call_limit is None
    assert got.fallback_token_limit is None
