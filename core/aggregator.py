"""内存滚动窗口聚合器：限流判定的热路径，零数据库访问。

设计要点：
- 每个作用域键（ScopeKey）一个桶，桶内 ``deque`` 按时间戳记录用量，并缓存
  ``calls`` / ``tokens`` 运行和，判定即读运行和，O(1) 摊还。
- 一次群聊请求会同时落到 ``group`` 与 ``member`` 两个桶；``add_tokens`` 通过
  进程内自增 ``eid`` 把同一请求的 token 回填到它涉及的所有桶。
- 短窗口**重置标记**与长窗口状态都独立于桶存放：桶会被 ``sweep`` 删除、被
  ``rebuild`` 清空，而标记与长窗口累计值必须存活下来（见 docs/adr/0001、0002）。
  短窗口的驱逐截止 = ``max(滚动窗口截止, 重置标记)``。
- 长窗口是天级**锚定**窗口：只保留「开窗时刻 + 累计计数」，不逐条驱逐；开窗由调用方
  在 ``record_call`` 之前调用 ``open_long`` 决定，重启后由 ``restore_long`` 从事件重算。
- 全部方法均为同步、无 ``await``，在事件循环上原子执行，无需加锁。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence

from .models import ScopeKey, scopes_for
from .windows import window_cutoff

# 一次请求记调用后，等待 on_llm_response 回填 token 的最大保留时长（秒）。
# 超时仍未回填则丢弃 pending（响应早已不可能到达），避免内存泄漏。
PENDING_TTL = 600.0


class _Record:
    __slots__ = ("ts", "tokens", "evicted")

    def __init__(self, ts: float) -> None:
        self.ts = ts
        self.tokens = 0
        self.evicted = False


class _Bucket:
    __slots__ = ("records", "calls", "tokens")

    def __init__(self) -> None:
        self.records: deque[_Record] = deque()
        self.calls = 0
        self.tokens = 0


class _LongState:
    """某实体进行中的长窗口周期：开窗时刻 + 本周期累计用量。"""

    __slots__ = ("start", "calls", "tokens")

    def __init__(self, start: float, calls: int = 0, tokens: int = 0) -> None:
        self.start = start
        self.calls = calls
        self.tokens = tokens


class Aggregator:
    def __init__(self, eid_base: int = 0) -> None:
        self._buckets: dict[ScopeKey, _Bucket] = {}
        self._pending: dict[int, tuple[float, list[tuple[ScopeKey, _Bucket, _Record]]]] = {}
        # 短窗口重置标记：早于标记的用量不计入短窗口（用量事件本身不删除）。
        self._markers: dict[ScopeKey, float] = {}
        # 进行中的长窗口周期；无条目表示该实体当前没有周期。
        self._long: dict[ScopeKey, _LongState] = {}
        # eid 用于关联「记调用」与「回填 token」。以 eid_base 作起点，使其跨进程重启
        # 全局唯一——否则重启后从 0 重新自增，token 回填的 UPDATE WHERE eid=? 会误伤
        # 保留期内上一进程写入的同 eid 历史行。主插件用 time.time_ns() 作 base。
        self._eid = eid_base

    # --- 内部 -----------------------------------------------------------

    @staticmethod
    def _evict(bucket: _Bucket, cutoff: float) -> None:
        records = bucket.records
        while records and records[0].ts < cutoff:
            old = records.popleft()
            old.evicted = True
            bucket.calls -= 1
            bucket.tokens -= old.tokens

    def _cutoff(self, key: ScopeKey, now: float, window_hours: float) -> float:
        """短窗口的驱逐截止：滚动窗口起点与重置标记取较晚者。"""
        return max(window_cutoff(now, window_hours), self._markers.get(key, 0.0))

    def _get_or_create(self, key: ScopeKey) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[key] = bucket
        return bucket

    def _forget(self, key: ScopeKey) -> None:
        """彻底遗忘一个键：桶、重置标记与长窗口状态一并清除。"""
        self._buckets.pop(key, None)
        self._markers.pop(key, None)
        self._long.pop(key, None)

    # --- 读 -------------------------------------------------------------

    def usage(
        self, key: ScopeKey, now: float, window_hours: float
    ) -> tuple[int, int, float | None]:
        """返回 ``(calls, tokens, oldest_ts)``；未知键返回 ``(0, 0, None)``，不创建桶。"""
        bucket = self._buckets.get(key)
        if bucket is None:
            return (0, 0, None)
        self._evict(bucket, self._cutoff(key, now, window_hours))
        if not bucket.records:
            return (0, 0, None)
        return (bucket.calls, bucket.tokens, bucket.records[0].ts)

    def marker(self, key: ScopeKey) -> float:
        """该键的短窗口重置标记；无标记返回 ``0.0``。"""
        return self._markers.get(key, 0.0)

    def long_usage(self, key: ScopeKey) -> tuple[int, int]:
        """当前长窗口周期内的 ``(calls, tokens)``；无周期返回 ``(0, 0)``。"""
        state = self._long.get(key)
        return (0, 0) if state is None else (state.calls, state.tokens)

    def long_start(self, key: ScopeKey) -> float | None:
        """当前长窗口周期的开窗时刻；无周期返回 ``None``。"""
        state = self._long.get(key)
        return None if state is None else state.start

    # --- 写 -------------------------------------------------------------

    def record_call(self, keys: Sequence[ScopeKey], now: float) -> int:
        """在给定的所有作用域桶各记一次调用，返回用于回填 token 的进程内 ``eid``。

        已有进行中周期的键同时累计长窗口调用数；开窗由调用方在此之前 ``open_long``。
        """
        self._eid += 1
        eid = self._eid
        pairs: list[tuple[ScopeKey, _Bucket, _Record]] = []
        for key in keys:
            bucket = self._get_or_create(key)
            record = _Record(now)
            bucket.records.append(record)
            bucket.calls += 1
            pairs.append((key, bucket, record))
            state = self._long.get(key)
            if state is not None:
                state.calls += 1
        self._pending[eid] = (now, pairs)
        return eid

    def add_tokens(self, eid: int, tokens: int) -> None:
        """把本次请求的 token 回填到它涉及的所有桶（按 eid 关联）。未知/超时 eid 为空操作。

        短窗口只回填未被驱逐（未过期、未被重置标记切走）的记录；长窗口则回填到
        ``record.ts >= 周期开窗时刻`` 的记录所属周期——短窗口重置不影响长窗口累计。
        """
        entry = self._pending.pop(eid, None)
        if entry is None:
            return
        _created, pairs = entry
        for key, bucket, record in pairs:
            record.tokens += tokens
            if not record.evicted:
                bucket.tokens += tokens
            state = self._long.get(key)
            if state is not None and record.ts >= state.start:
                state.tokens += tokens

    # --- 长窗口周期 ------------------------------------------------------

    def open_long(self, key: ScopeKey, start: float) -> None:
        """开启新周期：记录开窗时刻，累计值清零。"""
        self._long[key] = _LongState(start)

    def close_long(self, key: ScopeKey) -> None:
        """结束周期（到期或长窗口重置）：删除状态，其后的 token 回填不再计入。"""
        self._long.pop(key, None)

    def restore_long(self, key: ScopeKey, start: float, calls: int, tokens: int) -> None:
        """启动恢复：用持久化的开窗时刻与重算出的累计值重建周期状态。"""
        self._long[key] = _LongState(start, calls, tokens)

    def set_marker(self, key: ScopeKey, ts: float) -> None:
        """短窗口重置：记录标记并立即驱逐桶内早于标记的记录（迟到 token 不再回填）。"""
        self._markers[key] = ts
        bucket = self._buckets.get(key)
        if bucket is not None:
            self._evict(bucket, ts)

    # --- 重置 / 重建 ----------------------------------------------------

    def reset_key(self, key: ScopeKey) -> None:
        """丢弃某键的桶、标记与长窗口状态。

        **仅测试与旧路径使用**：0.4.0 起重置不再删内存用量，而是写重置标记 /
        关闭周期（见 docs/adr/0001），请走 ``CycleService.reset``。直接调用会
        丢掉重置标记，使标记之前的用量在下次 ``rebuild`` 时被重新加回来。
        """
        self._forget(key)

    def drop_private(self, user_id: str) -> None:
        """丢弃某私聊实体的全部内存状态。**仅测试与旧路径使用**，见 ``reset_key``。"""
        self._forget(ScopeKey("private", "", user_id))

    def drop_group(self, group_id: str) -> None:
        """删除某群的 ``group`` 键与其下所有 ``member`` 键（含标记与长窗口状态）。

        **仅测试与旧路径使用**：重置请走 ``CycleService.reset``（见 ``reset_key``）。
        """
        keys = {
            key
            for source in (self._buckets, self._markers, self._long)
            for key in source
            if key.group_id == group_id and key.scope in ("group", "member")
        }
        for key in keys:
            self._forget(key)

    def add_events(self, events: Iterable[tuple[str, str, float, int]]) -> None:
        """从持久化事件重建内存状态（启动恢复 / 窗口调大后回填）。

        ``events`` 为 ``(group_id, user_id, ts, tokens)`` 序列，按 ts 升序插入；
        早于该键重置标记的记录跳过。长窗口计数**不**在此处理（只经 ``restore_long``）。
        """
        for group_id, user_id, ts, tokens in sorted(events, key=lambda e: e[2]):
            for key in scopes_for(group_id, user_id):
                if ts < self._markers.get(key, 0.0):
                    continue
                bucket = self._get_or_create(key)
                record = _Record(ts)
                record.tokens = tokens
                bucket.records.append(record)
                bucket.calls += 1
                bucket.tokens += tokens

    def rebuild(self, events: Iterable[tuple[str, str, float, int]]) -> None:
        """清空全部桶并从持久化事件重建（窗口调大后回填已被驱逐的历史用量）。

        保留 ``_pending``：在途请求的 token 回填会落到旧桶对象上（已成孤儿，
        不影响新状态），对应的 DB 侧回填不受影响。
        重置标记与长窗口状态同样保留：它们不是从事件推导出来的。
        """
        self._buckets.clear()
        self.add_events(events)

    # --- 维护 -----------------------------------------------------------

    def sweep(self, now: float, max_window_hours: float) -> None:
        """驱逐空闲桶里的过期记录、删除空桶、丢弃已失效的标记与超时 pending。

        长窗口状态不受影响：周期的结束由调用方按到期时刻判断，与桶的生命周期无关。
        """
        cutoff = window_cutoff(now, max_window_hours)
        empty: list[ScopeKey] = []
        for key, bucket in self._buckets.items():
            self._evict(bucket, max(cutoff, self._markers.get(key, 0.0)))
            if not bucket.records:
                empty.append(key)
        for key in empty:
            del self._buckets[key]
        # 标记早于窗口截止后已无意义（窗口本身已经把这些记录驱逐干净）。
        for key in [k for k, ts in self._markers.items() if ts < cutoff]:
            del self._markers[key]
        stale = [
            eid
            for eid, (created, _pairs) in self._pending.items()
            if created < now - PENDING_TTL
        ]
        for eid in stale:
            self._pending.pop(eid, None)

    # --- 调试 / 测试 ----------------------------------------------------

    def bucket_count(self) -> int:
        return len(self._buckets)

    def pending_count(self) -> int:
        return len(self._pending)

    def long_count(self) -> int:
        return len(self._long)
