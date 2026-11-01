"""异步批量落盘：把用量事件从限流热路径解耦到后台任务。

热路径只做 ``enqueue_*``（非阻塞）。后台任务把队列里的 INSERT/UPDATE 批量写入
SQLite。队列满时丢弃落盘（仅影响中台统计与重启恢复，**不影响实时限流**，因为
限流只依赖内存聚合器）。
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

_STOP_KIND = "__stop__"


class _StorageLike(Protocol):
    async def insert_events(self, rows: Any) -> None: ...
    async def update_tokens(self, updates: Any) -> None: ...


class UsageWriter:
    def __init__(
        self,
        storage: _StorageLike,
        *,
        max_batch: int = 200,
        max_queue: int = 10000,
        logger: Any = None,
    ) -> None:
        self._storage = storage
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=max_queue)
        self._max_batch = max_batch
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._logger = logger
        self._dropped = 0

    # --- 入队（热路径，非阻塞）-----------------------------------------

    def enqueue_insert(
        self, row: tuple[int, int, str, str, str, int, int, str]
    ) -> None:
        self._put(("insert", row))

    def enqueue_token_update(self, eid: int, tokens: int) -> None:
        self._put(("token", (int(tokens), int(eid))))

    def _put(self, item: tuple[str, Any]) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._logger is not None and self._dropped % 100 == 1:
                self._logger.warning(
                    "quota_hub 落盘队列已满，已丢弃 %d 条用量记录（实时限流不受影响）。",
                    self._dropped,
                )

    def dropped_count(self) -> int:
        return self._dropped

    # --- 生命周期 -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            # 优雅停止：投递哨兵唤醒循环，让它写完手头批次后自然退出，避免在 DB 写入
            # 中途被 cancel 而丢数据。仅当队列已满（极端情况）才回退到强制 cancel。
            try:
                self._queue.put_nowait((_STOP_KIND, None))
            except asyncio.QueueFull:
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 后台任务退出不应影响关闭
                pass
        await self.flush()

    async def flush(self) -> None:
        """把当前队列里的所有内容写盘（用于 reset 前与关闭时）。"""
        await self._write_batch(self._drain_all())

    # --- 内部 -----------------------------------------------------------

    def _drain_all(self) -> list[tuple[str, Any]]:
        items: list[tuple[str, Any]] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    async def _write_batch(self, items: list[tuple[str, Any]]) -> None:
        inserts = [payload for kind, payload in items if kind == "insert"]
        updates = [payload for kind, payload in items if kind == "token"]
        # 先 INSERT 再 UPDATE，保证同一 eid 的 token 回填能命中已插入的行。
        if inserts:
            await self._storage.insert_events(inserts)
        if updates:
            await self._storage.update_tokens(updates)

    async def _run(self) -> None:
        while True:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                break
            batch: list[tuple[str, Any]] = [first]
            while len(batch) < self._max_batch:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            stop_seen = any(kind == _STOP_KIND for kind, _ in batch)
            real = [item for item in batch if item[0] != _STOP_KIND]
            try:
                await self._write_batch(real)
            except Exception as exc:  # noqa: BLE001 - 落盘失败不应中断后台任务
                if self._logger is not None:
                    self._logger.error("quota_hub 用量落盘失败: %s", exc)
            if stop_seen and not self._running:
                break
