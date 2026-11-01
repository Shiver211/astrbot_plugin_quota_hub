"""``services/cycle_service.py`` 的集成测试。

用真实 ``Storage``（临时库）+ 真实 ``Aggregator``，只把「当前时间」与「配额解析」
注入为可控的假件，验证「缓存 / 落盘 / 聚合器」三者的一致性。
"""

from __future__ import annotations

import asyncio
import dataclasses

from core.aggregator import Aggregator
from core.cycles import ACTOR_ADMIN, ACTOR_SELF
from core.models import (
    SCOPE_GROUP,
    SCOPE_MEMBER,
    SCOPE_PRIVATE,
    WINDOW_LONG,
    WINDOW_SHORT,
    Quota,
    ScopeKey,
)
from core.requests import REQUEST_PENDING, REQUEST_VOID, new_request
from core.storage import Storage
from core.windows import SECONDS_PER_DAY
from services.cycle_service import CycleService

DAY = float(SECONDS_PER_DAY)
T0 = 1_700_000_000.0
KEY = ScopeKey(SCOPE_MEMBER, "g1", "u1")


class _Clock:
    """可推进的假时钟。"""

    def __init__(self, start: float = T0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg, *args) -> None:
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args) -> None:  # pragma: no cover - 服务当前不打 info
        pass


def _quota(**changes) -> Quota:
    """测试用配额：长窗口 7 天、短 1 次 / 长 1 次自助重置。"""
    base = Quota(
        call_limit=10,
        token_limit=-1,
        window_hours=5.0,
        long_calls=-1,
        long_tokens=-1,
        long_window_days=7.0,
        self_reset_short=1,
        self_reset_long=1,
    )
    return dataclasses.replace(base, **changes)


async def _build(tmp_path, clock, quota: Quota | None = None):
    storage = Storage(tmp_path / "quota.db")
    await storage.initialize()
    aggregator = Aggregator()
    logger = _Logger()
    service = CycleService(
        storage, aggregator, clock, lambda key: quota or _quota(), logger
    )
    return storage, aggregator, service, logger


def _run(scenario):
    return asyncio.run(scenario())


# --- 开窗 -----------------------------------------------------------------


def test_first_allowed_call_opens_cycle_and_persists(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        states = await storage.load_states()
        await storage.close()
        return service.state_of(KEY), aggregator, states

    state, aggregator, states = _run(scenario)
    assert state.window_start == T0
    assert aggregator.long_start(KEY) == T0
    assert aggregator.long_usage(KEY) == (1, 0)
    # 落盘：重启后能按同一开窗时刻恢复
    assert [(k, s.window_start) for k, s in states] == [(KEY, T0)]


def test_second_call_in_cycle_keeps_window_start(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        clock.advance(3600)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        await storage.close()
        return service.state_of(KEY), aggregator.long_usage(KEY)

    state, usage = _run(scenario)
    assert state.window_start == T0
    assert usage == (2, 0)


def test_open_if_needed_is_memory_only_and_persist_writes_db(tmp_path):
    """开窗只改内存（不落盘），落盘由 persist_states 在记账之后完成。"""

    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        opened = service.open_if_needed([KEY], clock())
        before = await storage.load_states()
        aggregator.record_call([KEY], clock())
        await service.persist_states(opened)
        after = await storage.load_states()
        await storage.close()
        return opened, before, after, service.state_of(KEY), aggregator.long_start(KEY)

    opened, before, after, state, long_start = _run(scenario)
    assert [k for k, _ in opened] == [KEY]
    assert before == []  # 同步阶段不碰 DB
    assert [(k, s.window_start) for k, s in after] == [(KEY, T0)]
    assert state.window_start == T0 and long_start == T0


def test_open_if_needed_skips_entities_with_active_cycle(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        clock.advance(3600)
        opened = service.open_if_needed([KEY], clock())
        await storage.close()
        return opened, service.state_of(KEY)

    opened, state = _run(scenario)
    assert opened == []  # 已有周期不重复开窗
    assert state.window_start == T0


def test_persist_states_keeps_newer_cached_state(tmp_path):
    """落盘期间该实体被改动（如管理员重置）时，不能用旧快照覆盖回去。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        opened = service.open_if_needed([KEY], clock())
        await service.reset(KEY, WINDOW_LONG, ACTOR_ADMIN, "a", consume=False)
        await service.persist_states(opened)
        states = dict(await storage.load_states())
        await storage.close()
        return states[KEY]

    stored = _run(scenario)
    assert stored.window_start is None and stored.closed_by_reset is True


def test_group_and_member_open_independently(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        group = ScopeKey(SCOPE_GROUP, "g1", "")
        await service.before_allowed_call([group, KEY], clock())
        aggregator.record_call([group, KEY], clock())
        await storage.close()
        return aggregator.long_usage(group), aggregator.long_usage(KEY)

    assert _run(scenario) == ((1, 0), (1, 0))


# --- 自然到期 -------------------------------------------------------------


def test_touch_expires_cycle_refills_allowance_and_voids_request(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        # 本周期已用掉一次短窗口自助重置，并提了一条申请
        await service.reset(KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=True)
        req = await storage.insert_request(
            new_request(KEY.scope, KEY.group_id, KEY.user_id, WINDOW_SHORT, 2,
                        "写论文", clock(), T0, "aiocqhttp:GroupMessage:g1")
        )
        assert service.allowance(KEY, WINDOW_SHORT) == 0
        clock.advance(7 * DAY)
        state = await service.touch(KEY, clock())
        stored = dict(await storage.load_states())[KEY]
        after = await storage.get_request(req.id)
        await storage.close()
        return state, stored, aggregator, after

    state, stored, aggregator, after = _run(scenario)
    assert state.window_start is None and state.closed_by_reset is False
    # 补满到配置值（本周期消耗的次数不带到下一周期）
    assert (state.short_allowance, state.long_allowance) == (1, 1)
    assert stored.window_start is None and stored.short_allowance == 1
    assert aggregator.long_start(KEY) is None
    assert after.status == REQUEST_VOID


def test_touch_before_due_is_noop(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        before = service.state_of(KEY)
        clock.advance(6 * DAY)
        after = await service.touch(KEY, clock())
        await storage.close()
        return before, after, aggregator.long_start(KEY)

    before, after, long_start = _run(scenario)
    assert after is before  # 未到期返回同一对象，可跳过落盘
    assert long_start == T0


def test_expired_cycle_reopens_and_refilled_allowance_survives(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        clock.advance(8 * DAY)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        await storage.close()
        return service.state_of(KEY), aggregator.long_usage(KEY), clock()

    state, usage, now = _run(scenario)
    assert state.window_start == now  # 到期后的第一次放行调用开新窗
    assert usage == (1, 0)  # 新周期计数从零开始
    assert (state.short_allowance, state.long_allowance) == (1, 1)


def test_touch_all_advances_every_key(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        group = ScopeKey(SCOPE_GROUP, "g1", "")
        await service.before_allowed_call([group, KEY], clock())
        clock.advance(7 * DAY)
        await service.touch_all([group, KEY], clock())
        await storage.close()
        return service.state_of(group), service.state_of(KEY)

    group_state, member_state = _run(scenario)
    assert group_state.window_start is None and member_state.window_start is None


# --- 重置 -----------------------------------------------------------------


def test_short_reset_writes_marker_consumes_allowance_and_logs(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        clock.advance(60)
        state = await service.reset(KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=True)
        logs = await storage.list_reset_log()
        usage = aggregator.usage(KEY, clock(), 5.0)
        await storage.close()
        return state, logs, usage, aggregator

    state, logs, usage, aggregator = _run(scenario)
    assert state.short_marker == T0 + 60
    assert state.short_allowance == 0
    assert usage == (0, 0, None)  # 标记之前的用量不再计入短窗口
    assert aggregator.long_start(KEY) == T0  # 短窗口重置不影响周期
    assert len(logs) == 1
    assert (logs[0].window, logs[0].actor, logs[0].consumed) == (
        WINDOW_SHORT, ACTOR_SELF, True,
    )


def test_long_reset_closes_cycle_and_reopen_does_not_refill(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        clock.advance(60)
        closed = await service.reset(KEY, WINDOW_LONG, ACTOR_SELF, "u1", consume=True)
        clock.advance(60)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        await storage.close()
        return closed, service.state_of(KEY), aggregator.long_usage(KEY), clock()

    closed, reopened, usage, now = _run(scenario)
    assert closed.window_start is None and closed.closed_by_reset is True
    assert closed.long_allowance == 0
    assert reopened.window_start == now and reopened.closed_by_reset is False
    assert usage == (1, 0)  # 新周期计数清零
    # 由重置结束的周期再开窗**不**补满次数
    assert reopened.long_allowance == 0


def test_admin_reset_does_not_consume_allowance(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        state = await service.reset(KEY, WINDOW_SHORT, ACTOR_ADMIN, "admin1",
                                    consume=False)
        logs = await storage.list_reset_log()
        await storage.close()
        return state, logs

    state, logs = _run(scenario)
    assert state.short_allowance == 1
    assert logs[0].actor == ACTOR_ADMIN and logs[0].consumed is False


def test_reset_with_explicit_now_uses_it_for_marker_and_log(tmp_path):
    """显式传 ``now`` 时标记与审计日志都用它，而不是内部再取一次时钟。

    限流热路径（触顶自动重置）依赖这一点：标记晚于记账时刻会把触发这次重置的
    调用挤出短窗口，等于每次自动重置多放行一次。
    """

    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        clock.advance(60)  # 时钟已经走到 T0+60，但重置要按 T0+30 生效
        state = await service.reset(
            KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=True, now=T0 + 30
        )
        # 标记时刻记一次账：标记是 ``ts < marker`` 才驱逐，同刻的用量应保留
        aggregator.record_call([KEY], T0 + 30)
        logs = await storage.list_reset_log()
        usage = aggregator.usage(KEY, clock(), 5.0)
        await storage.close()
        return state, logs, usage, aggregator.marker(KEY)

    state, logs, usage, marker = _run(scenario)
    assert state.short_marker == T0 + 30
    assert marker == T0 + 30
    assert logs[0].ts == T0 + 30
    assert usage[:2] == (1, 0)  # 标记之前的那次被驱逐，标记同刻的那次保留


def test_reset_without_now_falls_back_to_clock(tmp_path):
    """不传 ``now``（手动 / 管理员 / 中台重置）时行为不变：取当前时钟。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        clock.advance(90)
        state = await service.reset(KEY, WINDOW_SHORT, ACTOR_ADMIN, "a", consume=False)
        logs = await storage.list_reset_log()
        await storage.close()
        return state, logs

    state, logs = _run(scenario)
    assert state.short_marker == T0 + 90
    assert logs[0].ts == T0 + 90


def test_reset_of_unknown_entity_starts_from_configured_allowance(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        state = await service.reset(KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=True)
        await storage.close()
        return state

    assert _run(scenario).short_allowance == 0


# --- 发放 -----------------------------------------------------------------


def test_grant_adds_allowance_and_persists(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        state = await service.grant(KEY, 2, 3)
        stored = dict(await storage.load_states())[KEY]
        await storage.close()
        return state, stored

    state, stored = _run(scenario)
    assert (state.short_allowance, state.long_allowance) == (3, 4)
    assert (stored.short_allowance, stored.long_allowance) == (3, 4)


# --- 申请的惰性作废 -------------------------------------------------------


def test_pending_request_survives_within_cycle(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        await storage.insert_request(
            new_request(KEY.scope, KEY.group_id, KEY.user_id, WINDOW_LONG, 1, "", clock(), T0)
        )
        pending = await service.pending_request(KEY)
        await storage.close()
        return pending

    pending = _run(scenario)
    assert pending is not None and pending.status == REQUEST_PENDING


def test_pending_request_voided_after_long_reset(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        req = await storage.insert_request(
            new_request(KEY.scope, KEY.group_id, KEY.user_id, WINDOW_SHORT, 1, "", clock(), T0)
        )
        await service.reset(KEY, WINDOW_LONG, ACTOR_ADMIN, "admin1", consume=False)
        pending = await service.pending_request(KEY)
        stored = await storage.get_request(req.id)
        refreshed = await service.refresh_request(req)
        await storage.close()
        return pending, stored, refreshed

    pending, stored, refreshed = _run(scenario)
    assert pending is None and refreshed is None
    assert stored.status == REQUEST_VOID


def test_refresh_request_voids_after_natural_expiry_without_other_touch(tmp_path):
    """周期自然到期后（未经其它路径 touch），审批前的检查必须自行推进并作废。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        req = await storage.insert_request(
            new_request(
                KEY.scope, KEY.group_id, KEY.user_id, WINDOW_LONG, 1, "", clock(), T0
            )
        )
        clock.advance(7 * DAY + 1)  # 自然到期，且不调用 touch / touch_all
        refreshed = await service.refresh_request(req)
        stored = await storage.get_request(req.id)
        state = service.state_of(KEY)
        await storage.close()
        return refreshed, stored, state

    refreshed, stored, state = _run(scenario)
    assert refreshed is None
    assert stored.status == REQUEST_VOID
    assert state.window_start is None  # refresh 自己推进了到期


def test_pending_request_voids_after_natural_expiry_without_other_touch(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        req = await storage.insert_request(
            new_request(
                KEY.scope, KEY.group_id, KEY.user_id, WINDOW_SHORT, 1, "", clock(), T0
            )
        )
        clock.advance(7 * DAY + 1)
        pending = await service.pending_request(KEY)
        stored = await storage.get_request(req.id)
        await storage.close()
        return pending, stored

    pending, stored = _run(scenario)
    assert pending is None
    assert stored.status == REQUEST_VOID


def test_grant_after_expiry_does_not_land_in_stale_cycle(tmp_path):
    """批准流程：refresh 已作废 → 不再发放；即便发放也只落在补满后的新次数上。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(
            tmp_path, clock, quota=_quota(self_reset_short=1, self_reset_long=1)
        )
        await service.before_allowed_call([KEY], clock())
        # 本周期内先把次数用掉，模拟「申请追加」的场景
        await service.reset(KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=True)
        req = await storage.insert_request(
            new_request(
                KEY.scope, KEY.group_id, KEY.user_id, WINDOW_SHORT, 2, "", clock(), T0
            )
        )
        clock.advance(7 * DAY + 1)
        refreshed = await service.refresh_request(req)
        state = service.state_of(KEY)
        await storage.close()
        return refreshed, state

    refreshed, state = _run(scenario)
    assert refreshed is None  # 审批被拦下，不会走到 grant
    # 到期已补满到配置值，过期周期里的发放不会残留
    assert (state.short_allowance, state.long_allowance) == (1, 1)


# --- 启动恢复 -------------------------------------------------------------


def test_restore_rebuilds_long_usage_and_markers(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        # 两条落盘用量：一条在标记之前，一条在标记之后
        await storage.insert_events([
            (1, int(T0), "aiocqhttp", "g1", "u1", 1, 100, "m"),
            (2, int(T0 + 120), "aiocqhttp", "g1", "u1", 1, 200, "m"),
        ])
        clock.advance(60)
        await service.reset(KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=True)
        clock.advance(60)

        # 模拟重启：新的聚合器与服务，先 restore 再 add_events
        fresh = Aggregator()
        revived = CycleService(storage, fresh, clock, lambda key: _quota(), _Logger())
        await revived.restore()
        fresh.add_events(await storage.load_recent(T0 - DAY))
        short_usage = fresh.usage(KEY, clock(), 5.0)
        long_usage = fresh.long_usage(KEY)
        await storage.close()
        return revived.state_of(KEY), fresh.marker(KEY), short_usage, long_usage

    state, marker, short_usage, long_usage = _run(scenario)
    assert state.window_start == T0 and state.short_allowance == 0
    assert marker == T0 + 60
    # 短窗口只剩标记之后的那一条；长窗口按开窗时刻重算，两条都在周期内
    assert short_usage[:2] == (1, 200)
    assert long_usage == (2, 300)


def test_restore_skips_long_usage_without_cycle(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.reset(KEY, WINDOW_LONG, ACTOR_ADMIN, "a", consume=False)
        fresh = Aggregator()
        revived = CycleService(storage, fresh, clock, lambda key: _quota(), _Logger())
        await revived.restore()
        await storage.close()
        return revived.state_of(KEY), fresh.long_start(KEY), revived.known_keys()

    state, long_start, keys = _run(scenario)
    assert state.closed_by_reset is True
    assert long_start is None
    assert keys == [KEY]


# --- 状态回收 -------------------------------------------------------------


def test_reclaim_drops_idle_states_from_memory_and_db(tmp_path):
    """无周期 + 标记已过护栏 + 次数等于配置值的状态可回收。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        clock.advance(7 * DAY + 1)
        await service.touch(KEY, clock())  # 自然到期：关窗并补满次数
        clock.advance(DAY)
        dropped = await service.reclaim(clock(), clock() - 5 * 3600)
        states = await storage.load_states()
        await storage.close()
        return dropped, service.known_keys(), states, service.state_of(KEY)

    dropped, keys, states, state = _run(scenario)
    assert dropped == 1
    assert keys == [] and states == []
    # 回收后由 state_of 现算，结果与回收前等价
    assert (state.window_start, state.short_allowance, state.long_allowance) == (None, 1, 1)


def test_reclaim_keeps_active_marked_and_granted_states(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        active = ScopeKey(SCOPE_MEMBER, "g1", "active")
        marked = ScopeKey(SCOPE_MEMBER, "g1", "marked")
        granted = ScopeKey(SCOPE_MEMBER, "g1", "granted")
        await service.before_allowed_call([active], clock())  # 有进行中的周期
        await service.reset(marked, WINDOW_SHORT, ACTOR_ADMIN, "a", consume=False)
        await service.grant(granted, 2, 0)  # 次数不等于配置值
        clock.advance(3600)
        dropped = await service.reclaim(clock(), clock() - 5 * 3600)
        keys = set(service.known_keys())
        await storage.close()
        return dropped, keys, {active, marked, granted}

    dropped, keys, expected = _run(scenario)
    assert dropped == 0 and keys == expected


def test_earliest_active_start_picks_oldest_running_cycle(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        empty = service.earliest_active_start()
        await service.before_allowed_call([KEY], clock())
        clock.advance(DAY)
        later = ScopeKey(SCOPE_MEMBER, "g1", "u2")
        await service.before_allowed_call([later], clock())
        running = service.earliest_active_start()
        # 最早那个周期到期关窗后（第二个还差半天），护栏应回落到剩下那个
        clock.advance(6.5 * DAY)
        await service.touch_all(service.active_keys(), clock())
        after = service.earliest_active_start()
        await storage.close()
        return empty, running, after

    empty, running, after = _run(scenario)
    assert empty is None
    assert running == T0
    assert after == T0 + DAY


def test_active_keys_only_lists_entities_with_cycle(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        other = ScopeKey(SCOPE_MEMBER, "g1", "u2")
        await service.before_allowed_call([KEY], clock())
        await service.reset(other, WINDOW_SHORT, ACTOR_ADMIN, "a", consume=False)
        await storage.close()
        return service.active_keys(), sorted(service.known_keys(), key=lambda k: k.user_id)

    active, known = _run(scenario)
    assert active == [KEY]
    assert len(known) == 2


def test_reclaim_survives_storage_failure(tmp_path):
    class _BrokenDelete:
        async def delete_state(self, key):
            raise RuntimeError("db down")

    async def scenario():
        clock = _Clock()
        logger = _Logger()
        service = CycleService(
            _BrokenDelete(), Aggregator(), clock, lambda key: _quota(), logger
        )
        service._states[KEY] = dataclasses.replace(
            service.state_of(KEY), short_allowance=1, long_allowance=1
        )
        dropped = await service.reclaim(clock(), clock() - 3600)
        return dropped, service.known_keys(), logger.warnings

    dropped, keys, warnings = _run(scenario)
    assert dropped == 1 and keys == []  # 内存照样回收
    assert len(warnings) == 1


# --- 只读辅助 -------------------------------------------------------------


def test_restore_skips_query_for_expired_cycle(tmp_path):
    """已到期的周期不回查用量，也不恢复长窗口累计值（留给首次 touch 推进）。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        await storage.insert_events(
            [(1, int(T0), "aiocqhttp", "g1", "u1", 1, 100, "m")]
        )
        clock.advance(7 * DAY + 1)  # 周期已自然到期

        queried: list[float] = []
        original = storage.query_usage

        async def spy(scope, group_id, user_id, cutoff):
            queried.append(cutoff)
            return await original(scope, group_id, user_id, cutoff)

        storage.query_usage = spy  # type: ignore[method-assign]
        fresh = Aggregator()
        revived = CycleService(storage, fresh, clock, lambda key: _quota(), _Logger())
        await revived.restore()
        before_touch = revived.state_of(KEY).window_start
        await revived.touch(KEY, clock())
        after = revived.state_of(KEY)
        storage.query_usage = original  # type: ignore[method-assign]
        await storage.close()
        return queried, fresh.long_usage(KEY), before_touch, after

    queried, long_usage, before_touch, after = _run(scenario)
    assert queried == []  # 没有发生回查
    assert long_usage == (0, 0)
    assert before_touch == T0  # 状态原样保留
    assert after.window_start is None and after.short_allowance == 1  # touch 后关窗补满


def test_restore_still_queries_for_live_cycle(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        await storage.insert_events(
            [(1, int(T0), "aiocqhttp", "g1", "u1", 1, 100, "m")]
        )
        clock.advance(DAY)  # 周期仍在进行中
        fresh = Aggregator()
        revived = CycleService(storage, fresh, clock, lambda key: _quota(), _Logger())
        await revived.restore()
        await storage.close()
        return fresh.long_usage(KEY), fresh.long_start(KEY)

    long_usage, long_start = _run(scenario)
    assert long_usage == (1, 100) and long_start == T0


def test_long_end_accepts_preresolved_quota(tmp_path):
    """传入 quota 时不再解析：用一个会抛异常的解析器验证确实没有走解析。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())

        def boom(key):  # pragma: no cover - 被调用即测试失败
            raise AssertionError("不应再解析配额")

        service._resolve_quota = boom
        end = service.long_end(KEY, _quota(long_window_days=2.0))
        days = service.days_of(KEY, _quota(long_window_days=2.0))
        await storage.close()
        return end, days

    end, days = _run(scenario)
    assert end == T0 + 2 * DAY and days == 2.0


def test_long_end_and_is_active(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(tmp_path, clock)
        before = service.long_end(KEY)
        await service.before_allowed_call([KEY], clock())
        end = service.long_end(KEY)
        active_now = service.is_active(KEY, clock())
        active_late = service.is_active(KEY, clock() + 8 * DAY)
        await storage.close()
        return before, end, active_now, active_late, service.days_of(KEY)

    before, end, active_now, active_late, days = _run(scenario)
    assert before is None
    assert end == T0 + 7 * DAY
    assert active_now is True and active_late is False
    assert days == 7.0


def test_days_of_falls_back_when_configured_non_positive(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(
            tmp_path, clock, quota=_quota(long_window_days=0)
        )
        await service.before_allowed_call([KEY], clock())
        await storage.close()
        return service.days_of(KEY), service.long_end(KEY)

    days, end = _run(scenario)
    assert days == 7.0
    assert end == T0 + 7 * DAY  # 非正天数不会让周期立刻到期


def test_state_of_unknown_key_uses_configured_allowance(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, _log = await _build(
            tmp_path, clock, quota=_quota(self_reset_short=3, self_reset_long=2)
        )
        state = service.state_of(ScopeKey(SCOPE_PRIVATE, "", "u9"))
        await storage.close()
        return state

    state = _run(scenario)
    assert (state.short_allowance, state.long_allowance) == (3, 2)
    assert state.window_start is None


# --- 落盘失败的降级 -------------------------------------------------------


def test_storage_failure_keeps_memory_state_and_logs(tmp_path):
    class _BrokenStorage:
        async def save_state(self, key, state):
            raise RuntimeError("db down")

        async def append_reset_log(self, entry):
            raise RuntimeError("db down")

        async def get_pending_request(self, key):
            return None

    async def scenario():
        clock = _Clock()
        logger = _Logger()
        aggregator = Aggregator()
        service = CycleService(
            _BrokenStorage(), aggregator, clock, lambda key: _quota(), logger
        )
        await service.reset(KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=True)
        return service.state_of(KEY), aggregator.marker(KEY), logger.warnings

    state, marker, warnings = _run(scenario)
    assert state.short_marker == T0 and marker == T0
    assert len(warnings) == 2  # 状态与日志各记一条告警
