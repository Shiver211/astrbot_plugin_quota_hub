"""``api/routes.py`` 中不依赖 quart ``request`` 对象的辅助方法的单元测试。

``QuotaApiMixin`` 上的 ``api_*`` 处理器都要读 quart 的 ``request``（取 query /
body），不在本测试范围内；本文件只测那些只依赖 ``self.service`` /
``self.storage`` / ``self.aggregator`` / ``self._overrides`` 等属性、可以脱离
真实 quart 请求上下文调用的辅助方法：``_markers_after``、``_pending_by_key``、
``_cycle_of``、``_refresh_rows``、``_log_row``、``_request_row``。

搭建方式与 tests/test_request_service.py / tests/test_cycle_service.py 一致：
真实 ``Storage``（临时库）+ 真实 ``Aggregator`` + 真实 ``CycleService``，只把
「当前时间」与「配额解析」注入为可控假件；``Host`` 只挂 ``QuotaApiMixin``
调用到的最小属性集合，不触碰 ``self.context`` / ``self.config`` 等主类属性。
"""

from __future__ import annotations

import asyncio
import dataclasses

from api.routes import QuotaApiMixin
from core.aggregator import Aggregator
from core.cycles import ACTOR_ADMIN, ACTOR_SELF, ResetLogEntry
from core.models import (
    SCOPE_MEMBER,
    SCOPE_PRIVATE,
    WINDOW_LONG,
    WINDOW_SHORT,
    Quota,
    ScopeKey,
)
from core.requests import REQUEST_APPROVED, REQUEST_PENDING, REQUEST_VOID, new_request
from core.storage import Storage
from core.windows import SECONDS_PER_DAY
from services.cycle_service import CycleService

DAY = float(SECONDS_PER_DAY)
T0 = 1_700_000_000.0
KEY = ScopeKey(SCOPE_MEMBER, "g1", "u1")


class _Clock:
    """可推进的假时钟，同其它服务层测试。"""

    def __init__(self, start: float = T0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


class _Logger:
    def warning(self, msg, *args) -> None:  # pragma: no cover - 仅吞掉告警
        pass

    def error(self, msg, *args, **kwargs) -> None:  # pragma: no cover
        pass

    def info(self, msg, *args) -> None:  # pragma: no cover
        pass


def _quota(**changes) -> Quota:
    """测试用配额：短窗口 5 小时，长窗口 7 天，短/长各 1 次自助重置。"""
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


class Host(QuotaApiMixin):
    """最小宿主：只挂 ``QuotaApiMixin`` 待测方法用到的属性。

    不实现 ``_register_web_apis`` 所需的 ``self.context``，也不实现任何
    ``api_*`` 端点用到的 ``self.config`` / ``self.writer`` 等，测试只调用
    不碰 quart ``request`` 的辅助方法。
    """

    def __init__(self, storage, aggregator, service, now, quota) -> None:
        self.storage = storage
        self.aggregator = aggregator
        self.service = service
        self._now = now
        self._quota = quota
        self._overrides: dict[ScopeKey, object] = {}
        self._group_names: dict[str, str] = {}
        self._group_remarks: dict[str, str] = {}
        self._user_names: dict[str, str] = {}

    def _resolve_quota(self, key: ScopeKey) -> Quota:
        return self._quota


async def _build(tmp_path, clock, quota: Quota | None = None):
    """构造 (storage, aggregator, service, host)。"""
    storage = Storage(tmp_path / "quota.db")
    await storage.initialize()
    aggregator = Aggregator()
    logger = _Logger()
    resolved = quota or _quota()
    service = CycleService(storage, aggregator, clock, lambda key: resolved, logger)
    host = Host(storage, aggregator, service, clock, resolved)
    return storage, aggregator, service, host


async def _pending_row(storage, service, clock, key=KEY, window=WINDOW_SHORT, amount=1):
    """开窗后提交一条待审批申请，返回已带 id 的申请（同 test_request_service）。"""
    await service.before_allowed_call([key], clock())
    state = service.state_of(key)
    req = new_request(
        key.scope,
        key.group_id,
        key.user_id,
        window,
        amount,
        "赶稿",
        clock(),
        state.window_start or 0.0,
        "origin:1",
    )
    return await storage.insert_request(req)


def _run(scenario):
    return asyncio.run(scenario())


# --- _markers_after ---------------------------------------------------------


def test_markers_after_only_returns_keys_marked_later_than_cutoff(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        key_a = ScopeKey(SCOPE_MEMBER, "g1", "a")
        key_b = ScopeKey(SCOPE_MEMBER, "g1", "b")
        # a 被短窗口重置（写下标记），b 从未被标记过
        await service.reset(key_a, WINDOW_SHORT, ACTOR_SELF, "a", consume=False)
        marked = host._markers_after([key_a, key_b], clock() - 1)
        await storage.close()
        return marked, key_a, key_b

    marked, key_a, key_b = _run(scenario)
    assert list(marked) == [key_a]
    assert marked[key_a] == T0
    assert key_b not in marked


def test_markers_after_excludes_marker_equal_to_cutoff(tmp_path):
    """标记必须**晚于**截止时刻才算数，等于截止时刻不算（边界排他）。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        await service.reset(KEY, WINDOW_SHORT, ACTOR_SELF, "u1", consume=False)
        marked = host._markers_after([KEY], clock())  # cutoff == marker
        await storage.close()
        return marked

    assert _run(scenario) == {}


def test_markers_after_empty_when_nothing_marked(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        marked = host._markers_after([KEY], clock() - 1)
        await storage.close()
        return marked

    assert _run(scenario) == {}


# --- _pending_by_key ---------------------------------------------------------


def test_pending_by_key_indexes_rows_by_scope_key(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        key_a = ScopeKey(SCOPE_MEMBER, "g1", "a")
        key_b = ScopeKey(SCOPE_PRIVATE, "", "b")
        req_a = await _pending_row(storage, service, clock, key=key_a)
        req_b = await _pending_row(storage, service, clock, key=key_b, window=WINDOW_LONG)

        pending = await host._pending_by_key()
        await storage.close()
        return pending, key_a, key_b, req_a.id, req_b.id

    pending, key_a, key_b, id_a, id_b = _run(scenario)
    assert set(pending) == {key_a, key_b}
    assert pending[key_a].id == id_a
    assert pending[key_b].id == id_b


def test_pending_by_key_empty_when_no_pending_requests(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, _service, host = await _build(tmp_path, clock)
        pending = await host._pending_by_key()
        await storage.close()
        return pending

    assert _run(scenario) == {}


def test_pending_by_key_returns_empty_dict_on_storage_failure(tmp_path):
    """读取失败只记日志、返回空表：申请徽标缺失不应让整张用量表打不开。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, _service, host = await _build(tmp_path, clock)

        async def boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        storage.list_requests = boom  # type: ignore[method-assign]
        pending = await host._pending_by_key()
        await storage.close()
        return pending

    assert _run(scenario) == {}


# --- _cycle_of ---------------------------------------------------------------


def test_cycle_of_without_cycle_or_pending(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, _service, host = await _build(tmp_path, clock)
        row = await host._cycle_of(KEY, host._quota, clock())
        await storage.close()
        return row

    row = _run(scenario)
    assert row["window_start"] is None
    assert row["long_calls"] == 0 and row["long_tokens"] == 0
    assert row["pending_request"] is None


def test_cycle_of_with_active_cycle_reports_long_usage(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, aggregator, service, host = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        aggregator.record_call([KEY], clock())
        row = await host._cycle_of(KEY, host._quota, clock())
        await storage.close()
        return row

    row = _run(scenario)
    assert row["window_start"] == T0
    assert row["long_calls"] == 1
    assert row["pending_request"] is None


def test_cycle_of_with_pending_index_hit_still_valid(tmp_path):
    """命中 pending 索引、周期仍在进行中：申请照常展示，周期状态不受影响。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        req = await _pending_row(storage, service, clock)
        pending = {KEY: req}
        row = await host._cycle_of(KEY, host._quota, clock(), pending)
        await storage.close()
        return row

    row = _run(scenario)
    assert row["window_start"] == T0
    assert row["pending_request"] is not None
    assert row["pending_request"]["reason"] == "赶稿"


def test_cycle_of_with_pending_index_hit_expired_cycle_voids_and_refreshes_state(
    tmp_path,
):
    """命中 pending 索引、周期已自然到期：申请随之作废，行内展示关窗后的最新状态。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        req = await _pending_row(storage, service, clock)
        pending = {KEY: req}
        clock.advance(8 * DAY)  # 超过 7 天长窗口，周期自然到期
        row = await host._cycle_of(KEY, host._quota, clock(), pending)
        stored = await storage.get_request(req.id)
        await storage.close()
        return row, stored

    row, stored = _run(scenario)
    assert row["window_start"] is None  # 关窗后的最新状态
    assert row["pending_request"] is None  # 申请已作废，行内不再展示
    assert stored.status == REQUEST_VOID


# --- _refresh_rows ------------------------------------------------------------


def test_refresh_rows_keeps_pending_row_when_cycle_still_active(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        saved = await _pending_row(storage, service, clock)
        rows = await host._refresh_rows([saved], keep_void=False)
        await storage.close()
        return rows

    rows = _run(scenario)
    assert len(rows) == 1
    assert rows[0]["status"] == REQUEST_PENDING
    assert rows[0]["id"] == 1


def test_refresh_rows_drops_stale_pending_row_when_keep_void_false(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        saved = await _pending_row(storage, service, clock)
        clock.advance(8 * DAY)  # 周期自然到期，惰性作废
        rows = await host._refresh_rows([saved], keep_void=False)
        await storage.close()
        return rows

    assert _run(scenario) == []


def test_refresh_rows_reads_back_voided_row_when_keep_void_true(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        saved = await _pending_row(storage, service, clock)
        clock.advance(8 * DAY)
        rows = await host._refresh_rows([saved], keep_void=True)
        await storage.close()
        return rows, saved.id

    rows, saved_id = _run(scenario)
    assert len(rows) == 1
    assert rows[0]["status"] == REQUEST_VOID
    assert rows[0]["id"] == saved_id


def test_refresh_rows_passes_through_already_decided_rows_untouched(tmp_path):
    """非待审批状态的行不再做惰性作废检查，直接序列化。"""

    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        saved = await _pending_row(storage, service, clock)
        decided = dataclasses.replace(
            saved, status=REQUEST_APPROVED, granted=1, decided_by="admin1"
        )
        rows = await host._refresh_rows([decided], keep_void=False)
        await storage.close()
        return rows

    rows = _run(scenario)
    assert len(rows) == 1
    assert rows[0]["status"] == REQUEST_APPROVED
    assert rows[0]["granted"] == 1


def test_refresh_rows_empty_input_returns_empty_list(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, _service, host = await _build(tmp_path, clock)
        rows = await host._refresh_rows([], keep_void=False)
        await storage.close()
        return rows

    assert _run(scenario) == []


# --- _log_row / _request_row ---------------------------------------------------


def test_log_row_uses_host_name_maps(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, _service, host = await _build(tmp_path, clock)
        host._group_names["g1"] = "测试群"
        host._user_names["u1"] = "小明"
        entry = ResetLogEntry(
            ts=clock(),
            scope=SCOPE_MEMBER,
            group_id="g1",
            user_id="u1",
            window=WINDOW_SHORT,
            actor=ACTOR_ADMIN,
            actor_id="u1",
            consumed=False,
            id=1,
        )
        row = host._log_row(entry)
        await storage.close()
        return row

    row = _run(scenario)
    assert row["group_name"] == "测试群"
    assert row["user_name"] == "小明"
    assert row["actor_label"] == "管理员"


def test_request_row_uses_host_name_maps(tmp_path):
    async def scenario():
        clock = _Clock()
        storage, _agg, service, host = await _build(tmp_path, clock)
        host._group_names["g1"] = "测试群"
        host._group_remarks["g1"] = "内部群"
        host._user_names["u1"] = "小明"
        saved = await _pending_row(storage, service, clock)
        row = host._request_row(saved)
        await storage.close()
        return row

    row = _run(scenario)
    assert row["group_name"] == "测试群"
    assert row["group_remark"] == "内部群"
    assert row["user_name"] == "小明"
    assert row["status_label"] == "待审批"
