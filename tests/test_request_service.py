"""``services/request_service.py`` 的集成测试。

用真实 ``Storage``（临时库）+ 真实 ``Aggregator`` + 真实 ``CycleService``，
只把「当前时间」与「配额解析」注入为可控的假件，验证审批 / 发放的落盘、
次数发放与失败时的中文原因。
"""

from __future__ import annotations

import asyncio
import dataclasses

from core.aggregator import Aggregator
from core.models import (
    SCOPE_MEMBER,
    SCOPE_PRIVATE,
    WINDOW_LONG,
    WINDOW_SHORT,
    Quota,
    ScopeKey,
)
from core.requests import (
    REQUEST_APPROVED,
    REQUEST_DENIED,
    REQUEST_PENDING,
    REQUEST_VOID,
    new_request,
)
from core.storage import Storage
from core.windows import SECONDS_PER_DAY
from services.cycle_service import CycleService
from services.request_service import RequestService

DAY = float(SECONDS_PER_DAY)
T0 = 1_700_000_000.0
KEY = ScopeKey(SCOPE_MEMBER, "g1", "u1")


class _Clock:
    def __init__(self, start: float = T0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> float:
        self.value += seconds
        return self.value


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def _record(self, msg, *args) -> None:
        self.messages.append(msg % args if args else msg)

    warning = _record
    error = _record

    def info(self, msg, *args) -> None:  # pragma: no cover - 服务当前不打 info
        pass


def _quota(**changes) -> Quota:
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
    """构造 (storage, aggregator, cycle_service, request_service)。"""
    storage = Storage(tmp_path / "quota.db")
    await storage.initialize()
    aggregator = Aggregator()
    logger = _Logger()
    service = CycleService(
        storage, aggregator, clock, lambda key: quota or _quota(), logger
    )
    requests_service = RequestService(storage, service, clock, logger)
    return storage, aggregator, service, requests_service


async def _submit(storage, service, clock, key=KEY, window=WINDOW_SHORT, amount=1):
    """开窗后提交一条待审批申请，返回已带 id 的申请。"""
    await service.before_allowed_call([key], clock())
    state = service.state_of(key)
    req = new_request(
        key.scope,
        key.group_id,
        key.user_id,
        window,
        amount,
        "写论文",
        clock(),
        state.window_start or 0.0,
        "origin:1",
    )
    return await storage.insert_request(req)


def test_approve_grants_and_persists(tmp_path):
    """批准：状态落盘为 approved，次数发到对应窗口。"""

    async def run() -> None:
        clock = _Clock()
        storage, _agg, service, reqs = await _build(tmp_path, clock)
        saved = await _submit(storage, service, clock, amount=2)
        before = service.state_of(KEY).short_allowance

        decided = await reqs.approve(saved.id, None, "admin1")

        assert decided.status == REQUEST_APPROVED
        assert decided.granted == 2
        assert decided.decided_by == "admin1"
        stored = await storage.get_request(saved.id)
        assert stored.status == REQUEST_APPROVED
        assert service.state_of(KEY).short_allowance == before + 2

    asyncio.run(run())


def test_approve_custom_amount_long_window(tmp_path):
    """批准时可覆盖数量，长窗口申请只加长窗口次数。"""

    async def run() -> None:
        clock = _Clock()
        storage, _agg, service, reqs = await _build(tmp_path, clock)
        saved = await _submit(storage, service, clock, window=WINDOW_LONG, amount=1)
        state = service.state_of(KEY)

        decided = await reqs.approve(saved.id, 5, "admin1")

        assert decided.granted == 5
        after = service.state_of(KEY)
        assert after.long_allowance == state.long_allowance + 5
        assert after.short_allowance == state.short_allowance

    asyncio.run(run())


def test_approve_rejects_zero_amount(tmp_path):
    """发放数量必须 ≥ 1，且失败时不改动申请状态。"""

    async def run() -> None:
        clock = _Clock()
        storage, _agg, service, reqs = await _build(tmp_path, clock)
        saved = await _submit(storage, service, clock)

        try:
            await reqs.approve(saved.id, 0, "admin1")
        except ValueError as exc:
            assert str(exc) == "发放数量必须大于等于 1"
        else:  # pragma: no cover - 断言失败路径
            raise AssertionError("应当抛出 ValueError")
        stored = await storage.get_request(saved.id)
        assert stored.status == REQUEST_PENDING

    asyncio.run(run())


def test_approve_twice_rejected(tmp_path):
    """已决的申请不能再次审批。"""

    async def run() -> None:
        clock = _Clock()
        storage, _agg, service, reqs = await _build(tmp_path, clock)
        saved = await _submit(storage, service, clock)
        await reqs.approve(saved.id, None, "admin1")

        try:
            await reqs.approve(saved.id, None, "admin2")
        except ValueError as exc:
            assert "已批准" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("应当抛出 ValueError")

    asyncio.run(run())


def test_deny_persists_note(tmp_path):
    """拒绝：理由落盘，未发放任何次数。"""

    async def run() -> None:
        clock = _Clock()
        storage, _agg, service, reqs = await _build(tmp_path, clock)
        saved = await _submit(storage, service, clock)
        before = service.state_of(KEY).short_allowance

        decided = await reqs.deny(saved.id, "用量正常", "admin1")

        assert decided.status == REQUEST_DENIED
        assert decided.decision_note == "用量正常"
        assert (await storage.get_request(saved.id)).status == REQUEST_DENIED
        assert service.state_of(KEY).short_allowance == before

    asyncio.run(run())


def test_load_missing_request(tmp_path):
    """不存在的 ID 给出中文原因。"""

    async def run() -> None:
        clock = _Clock()
        _storage, _agg, _service, reqs = await _build(tmp_path, clock)
        try:
            await reqs.load(404)
        except ValueError as exc:
            assert str(exc) == "未找到申请 #404"
        else:  # pragma: no cover
            raise AssertionError("应当抛出 ValueError")

    asyncio.run(run())


def test_approve_stale_request_voided(tmp_path):
    """周期自然到期后，待审批申请在审批时惰性作废并拒绝批准。"""

    async def run() -> None:
        clock = _Clock()
        storage, _agg, service, reqs = await _build(tmp_path, clock)
        saved = await _submit(storage, service, clock)
        clock.advance(8 * DAY)  # 超过 7 天长窗口

        try:
            await reqs.approve(saved.id, None, "admin1")
        except ValueError as exc:
            assert str(exc) == f"申请 #{saved.id} 所在周期已结束，已自动作废"
        else:  # pragma: no cover
            raise AssertionError("应当抛出 ValueError")
        assert (await storage.get_request(saved.id)).status == REQUEST_VOID

    asyncio.run(run())


def test_grant_to_requires_active_cycle(tmp_path):
    """没有进行中的周期时发放被拒绝，有周期时按窗口累加。"""

    async def run() -> None:
        clock = _Clock()
        _storage, _agg, service, reqs = await _build(tmp_path, clock)
        key = ScopeKey(SCOPE_PRIVATE, "", "u9")

        try:
            await reqs.grant_to(key, 1, 1)
        except ValueError as exc:
            assert str(exc) == "该实体当前没有进行中的周期，无法发放"
        else:  # pragma: no cover
            raise AssertionError("应当抛出 ValueError")

        await service.before_allowed_call([key], clock())
        before = service.state_of(key)
        state = await reqs.grant_to(key, 2, 3)
        assert state.short_allowance == before.short_allowance + 2
        assert state.long_allowance == before.long_allowance + 3

    asyncio.run(run())


def test_grant_to_touches_expired_cycle(tmp_path):
    """已自然到期的周期先被推进（关窗 + 补满），再拒绝发放。"""

    async def run() -> None:
        clock = _Clock()
        _storage, _agg, service, reqs = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        clock.advance(8 * DAY)

        try:
            await reqs.grant_to(KEY, 1, 0)
        except ValueError as exc:
            assert str(exc) == "该实体当前没有进行中的周期，无法发放"
        else:  # pragma: no cover
            raise AssertionError("应当抛出 ValueError")
        assert service.state_of(KEY).window_start is None

    asyncio.run(run())


def test_grant_amount_capped(tmp_path):
    """单次发放 / 批准超过 MAX_GRANT 时拒绝，避免误输入天文数字。"""
    from services.request_service import MAX_GRANT

    async def run() -> None:
        clock = _Clock()
        storage, _agg, service, reqs = await _build(tmp_path, clock)
        await service.before_allowed_call([KEY], clock())
        for short, long in ((MAX_GRANT + 1, 0), (0, MAX_GRANT + 1)):
            try:
                await reqs.grant_to(KEY, short, long)
            except ValueError as exc:
                assert str(MAX_GRANT) in str(exc)
            else:
                raise AssertionError("超上限发放未被拒绝")
        saved = await _submit(storage, service, clock, amount=1)
        try:
            await reqs.approve(saved.id, MAX_GRANT + 1, "admin1")
        except ValueError as exc:
            assert str(MAX_GRANT) in str(exc)
        else:
            raise AssertionError("超上限批准未被拒绝")
        await storage.close()

    asyncio.run(run())
