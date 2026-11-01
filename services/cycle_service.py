"""周期服务：长窗口周期、重置标记与重置次数的「缓存 + 落盘 + 聚合器」编排。

``core.cycles`` 只做纯状态转移，本模块把一次状态转移落到三处并保证一致：
1. 内存缓存 ``dict[ScopeKey, CycleState]``（热路径只读内存）；
2. SQLite ``entity_state`` / ``reset_log`` / ``reset_requests``；
3. 内存聚合器（短窗口重置标记、长窗口周期状态）。

不 import astrbot，可用真实 ``Storage`` + ``Aggregator`` 直接测试。
落盘失败只记日志、不抛出：限流热路径不能因为一次写库失败而中断 LLM 调用，
内存状态仍然正确，下一次状态转移会再次尝试落盘。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

try:  # 部署时作为 data.plugins.<name>.services.cycle_service 加载
    from ..core import cycles, requests
    from ..core.models import (
        DEFAULT_LONG_WINDOW_DAYS,
        WINDOW_SHORT,
        Quota,
        ScopeKey,
    )
except ImportError:  # pragma: no cover - 插件根目录在 sys.path 时的兜底
    from core import cycles, requests
    from core.models import (
        DEFAULT_LONG_WINDOW_DAYS,
        WINDOW_SHORT,
        Quota,
        ScopeKey,
    )


class CycleService:
    """某个插件实例的全部实体周期状态。

    Args:
        storage: ``core.storage.Storage``（须已 ``initialize``）。
        aggregator: ``core.aggregator.Aggregator``。
        now: 取当前时间戳的可调用对象（便于测试注入假时钟）。
        resolve_quota: ``ScopeKey`` → 解析后的 ``Quota``（提供长窗口天数与重置次数配置）。
        logger: 具备 ``warning`` / ``info`` 的日志对象。
    """

    def __init__(
        self,
        storage: Any,
        aggregator: Any,
        now: Callable[[], float],
        resolve_quota: Callable[[ScopeKey], Quota],
        logger: Any,
    ) -> None:
        self._storage = storage
        self._aggregator = aggregator
        self._now = now
        self._resolve_quota = resolve_quota
        self._logger = logger
        self._states: dict[ScopeKey, cycles.CycleState] = {}

    # --- 读 -------------------------------------------------------------

    def state_of(self, key: ScopeKey, quota: Quota | None = None) -> cycles.CycleState:
        """该实体的周期状态；从未出现过的实体返回「重置次数为配置值」的初始状态。

        读取不写缓存：未落盘的实体没必要占内存，且配置改动能立刻反映到初始次数上。
        ``quota`` 由热路径传入已解析的配额，省掉一次多层覆盖合并。
        """
        state = self._states.get(key)
        if state is not None:
            return state
        if quota is None:
            quota = self._resolve_quota(key)
        return cycles.initial_state(quota.self_reset_short, quota.self_reset_long)

    def known_keys(self) -> list[ScopeKey]:
        """已有缓存状态的实体。"""
        return list(self._states)

    def earliest_active_start(self) -> float | None:
        """所有进行中周期里最早的开窗时刻；没有进行中的周期返回 ``None``。

        供明细清理护栏使用：长窗口累计值重启时要从明细重算（见 docs/adr/0002），
        护栏必须覆盖到最早那个仍在进行中的周期起点。
        """
        starts = [
            state.window_start
            for state in self._states.values()
            if state.window_start is not None
        ]
        return min(starts) if starts else None

    def active_keys(self) -> list[ScopeKey]:
        """有进行中周期的实体（维护循环只需推进这些：无周期就没有到期可推）。"""
        return [key for key, state in self._states.items() if state.window_start is not None]

    def days_of(self, key: ScopeKey, quota: Quota | None = None) -> float:
        """该实体当前解析出的长窗口天数（防御非正值，避免周期立刻到期）。

        ``quota`` 由热路径传入已解析的配额，避免同一次判定里重复解析。
        """
        if quota is None:
            quota = self._resolve_quota(key)
        days = float(quota.long_window_days)
        return days if days > 0 else DEFAULT_LONG_WINDOW_DAYS

    def long_end(self, key: ScopeKey, quota: Quota | None = None) -> float | None:
        """当前周期的到期时刻；无进行中的周期返回 ``None``。"""
        return cycles.cycle_end(self.state_of(key, quota), self.days_of(key, quota))

    def is_active(self, key: ScopeKey, now: float) -> bool:
        """是否有进行中的周期（不推进到期，调用方应先 ``touch``）。"""
        return cycles.is_active(self.state_of(key), now, self.days_of(key))

    def allowance(self, key: ScopeKey, window: str) -> int:
        """该实体对应窗口的剩余自助重置次数。"""
        return cycles.allowance(self.state_of(key), window)

    # --- 惰性推进 -------------------------------------------------------

    async def touch(self, key: ScopeKey, now: float) -> cycles.CycleState:
        """推进到期：自然到期则关窗、补满重置次数、作废该实体的待审批申请。

        未到期时返回原对象（``expire_if_due`` 的约定），不落盘、不产生副作用。
        """
        state = self.state_of(key)
        quota = self._resolve_quota(key)
        days = float(quota.long_window_days)
        new, expired = cycles.expire_if_due(
            state,
            now,
            days if days > 0 else DEFAULT_LONG_WINDOW_DAYS,
            quota.self_reset_short,
            quota.self_reset_long,
        )
        if not expired:
            return state
        self._aggregator.close_long(key)
        await self._store(key, new)
        await self._void_pending(key, new.window_start)
        return new

    async def touch_all(self, keys: Iterable[ScopeKey], now: float) -> None:
        """对多个实体逐个 ``touch``（限流热路径在判定前调用）。"""
        for key in keys:
            await self.touch(key, now)

    def open_if_needed(
        self, keys: Iterable[ScopeKey], now: float
    ) -> list[tuple[ScopeKey, cycles.CycleState]]:
        """**同步**开窗：没有周期的实体在此开窗，返回待落盘的 ``(key, 新状态)``。

        只改内存（``_states`` 与 ``aggregator.open_long``），刻意不 await：限流的
        「判定 → 记账」之间一旦让出，并发请求会读到同一份旧用量而超发。落盘交给
        ``persist_states``，由调用方在 ``aggregator.record_call`` 之后再 await。

        必须在 ``record_call`` 之前调用，否则本次调用不计入长窗口。无论长窗口
        配额是否为 ``-1`` 都开窗，否则重置次数永远等不到自然到期补满。
        """
        opened: list[tuple[ScopeKey, cycles.CycleState]] = []
        for key in keys:
            state = self.state_of(key)
            if state.window_start is not None:
                continue
            new = cycles.open_cycle(state, now)
            self._states[key] = new
            self._aggregator.open_long(key, now)
            opened.append((key, new))
        return opened

    async def persist_states(
        self, pairs: Iterable[tuple[ScopeKey, cycles.CycleState]]
    ) -> None:
        """把 ``open_if_needed`` 开出的新周期落盘（热路径应放在记账之后）。

        以缓存里的最新状态为准落盘：这期间若有其它协程改过该实体（如管理员
        重置），不能拿手上的旧快照把它覆盖回去。
        """
        for key, state in pairs:
            await self._store(key, self._states.get(key, state))

    async def before_allowed_call(self, keys: Iterable[ScopeKey], now: float) -> None:
        """``touch_all`` + ``open_if_needed`` + ``persist_states`` 的组合。

        供非热路径与旧调用方使用；限流热路径请分开调用，把落盘挪到
        ``record_call`` 之后，以消除判定与记账之间的 await 间隙。
        """
        await self.touch_all(keys, now)
        await self.persist_states(self.open_if_needed(keys, now))

    async def reclaim(self, now: float, short_cutoff: float) -> int:
        """回收「已无信息量」的缓存状态，返回回收条数。

        ``_states`` 会随出现过的实体单调增长（群成员多时可观），而其中大量条目
        早已退化成「和从未出现过的实体完全等价」：没有进行中的周期、短窗口标记
        早于护栏（``short_cutoff`` 一般取最大限流窗口起点，早于它的标记不再影响
        任何统计）、重置次数恰好等于配置值（没有发放或消耗的痕迹）。这类状态由
        ``state_of`` 现算即可复现，内存与 DB 里都不必留。

        ``closed_by_reset`` 只作审计展示、不参与补满判定（见 ``core.cycles``），
        故不影响回收判断。
        """
        stale: list[ScopeKey] = []
        for key, state in self._states.items():
            if state.window_start is not None:
                continue
            # 时钟回拨等异常导致标记落在未来时保守跳过，避免删掉仍在生效的标记
            if state.short_marker >= short_cutoff or state.short_marker > now:
                continue
            quota = self._resolve_quota(key)
            if (state.short_allowance, state.long_allowance) != (
                quota.self_reset_short,
                quota.self_reset_long,
            ):
                continue
            stale.append(key)
        for key in stale:
            self._states.pop(key, None)
            try:
                await self._storage.delete_state(key)
            except Exception as exc:  # noqa: BLE001 - 回收失败只是留一行旧数据
                self._logger.warning("quota_hub 回收周期状态失败 key=%s: %s", key, exc)
        return len(stale)

    # --- 重置 / 发放 ----------------------------------------------------

    async def reset(
        self,
        key: ScopeKey,
        window: str,
        actor: str,
        actor_id: str = "",
        *,
        consume: bool,
        now: float | None = None,
    ) -> cycles.CycleState:
        """执行一次重置：短窗口写标记、长窗口关窗，并记一条审计日志。

        ``consume=True``（自助重置）时扣减一次对应窗口的重置次数；管理员 / 中台
        重置传 ``False``。调用方须先校验前置条件（见 ``core.cycles.check_self_reset``）。

        ``now`` 为重置生效时刻，``None``（手动 / 管理员 / 中台重置）时取当前时钟。
        限流热路径**必须显式传入判定用的 ``now``**：短窗口重置标记若晚于记账时刻，
        触发这次自动重置的调用会被自己写下的标记驱逐出窗口，等于每次自动重置白
        放行一次（标记是「只统计 ts >= marker 的用量」的护栏，见 docs/adr/0001）。
        """
        if now is None:
            now = self._now()
        state = cycles.apply_reset(self.state_of(key), window, now)
        if consume:
            state = cycles.consume_allowance(state, window)
        if window == WINDOW_SHORT:
            self._aggregator.set_marker(key, now)
        else:
            self._aggregator.close_long(key)
        await self._store(key, state)
        await self._append_log(
            cycles.ResetLogEntry(
                ts=now,
                scope=key.scope,
                group_id=key.group_id,
                user_id=key.user_id,
                window=window,
                actor=actor,
                actor_id=actor_id,
                consumed=consume,
            )
        )
        return state

    async def grant(self, key: ScopeKey, short: int, long: int) -> cycles.CycleState:
        """为当前周期追加重置次数（自然到期时会被「补满=赋值」清掉）。"""
        state = cycles.grant(self.state_of(key), short, long)
        await self._store(key, state)
        return state

    # --- 申请 -----------------------------------------------------------

    async def pending_request(self, key: ScopeKey) -> requests.ResetRequest | None:
        """该实体仍然有效的待审批申请；周期已结束的申请在此惰性作废并返回 ``None``。

        必须先 ``touch``：周期到期是惰性的，若不先推进，缓存里仍是旧的
        ``window_start``，已过期周期里的申请会被误判为「仍然有效」。
        """
        state = await self.touch(key, self._now())
        req = await self._storage.get_pending_request(key)
        if req is None:
            return None
        return await self._void_if_stale(req, state.window_start)

    async def refresh_request(
        self, req: requests.ResetRequest
    ) -> requests.ResetRequest | None:
        """审批前的惰性作废检查：仍待审批返回原对象，已作废返回 ``None``。

        同 ``pending_request``：先推进到期再取 ``window_start``，否则批准的次数
        会发放到一个早已自然到期的周期上（下次开窗即被补满覆盖，等于白发）。
        """
        key = ScopeKey(req.scope, req.group_id, req.user_id)
        state = await self.touch(key, self._now())
        return await self._void_if_stale(req, state.window_start)

    # --- 启动恢复 -------------------------------------------------------

    async def restore(self) -> None:
        """从 ``entity_state`` 恢复缓存、重置标记与长窗口累计值。

        必须在 ``aggregator.add_events`` **之前**调用：标记先就位，事件回填才会
        跳过标记之前的记录。长窗口累计值按 ADR 0002 从用量事件重算而非落库。
        """
        rows = await self._storage.load_states()
        self._states = dict(rows)
        now = self._now()
        for key, state in rows:
            if state.short_marker > 0:
                self._aggregator.set_marker(key, state.short_marker)
            if state.window_start is None:
                continue
            # 已自然到期的周期不必回查用量：那是一次全表扫描的聚合，而恢复出来的
            # 累计值随即会被首次 touch 关窗丢弃。状态原样留着，交给 touch 推进
            # （关窗 + 补满重置次数）。
            end = cycles.cycle_end(state, self.days_of(key))
            if end is not None and now >= end:
                continue
            calls, tokens = await self._storage.query_usage(
                key.scope, key.group_id, key.user_id, state.window_start
            )
            self._aggregator.restore_long(key, state.window_start, calls, tokens)

    # --- 内部 -----------------------------------------------------------

    async def _store(self, key: ScopeKey, state: cycles.CycleState) -> None:
        """更新缓存并落盘；落盘失败只记日志（内存仍是限流判定的真值）。"""
        self._states[key] = state
        try:
            await self._storage.save_state(key, state)
        except Exception as exc:  # noqa: BLE001 - 落盘失败不应中断限流热路径
            self._logger.warning("quota_hub 周期状态落盘失败 key=%s: %s", key, exc)

    async def _append_log(self, entry: cycles.ResetLogEntry) -> None:
        try:
            await self._storage.append_reset_log(entry)
        except Exception as exc:  # noqa: BLE001 - 审计日志失败不影响重置本身
            self._logger.warning("quota_hub 重置日志落盘失败: %s", exc)

    async def _void_pending(self, key: ScopeKey, cycle_start: float | None) -> None:
        req = await self._storage.get_pending_request(key)
        if req is not None:
            await self._void_if_stale(req, cycle_start)

    async def _void_if_stale(
        self, req: requests.ResetRequest, cycle_start: float | None
    ) -> requests.ResetRequest | None:
        """周期已结束则落盘作废并返回 ``None``；仍有效则原样返回。"""
        checked = requests.void_if_stale(req, cycle_start)
        if checked is req:
            return req
        try:
            await self._storage.update_request(checked)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("quota_hub 作废申请 #%s 失败: %s", req.id, exc)
        return None
