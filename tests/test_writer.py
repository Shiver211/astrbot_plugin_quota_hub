"""异步批量落盘 writer 的单元测试（用假存储）。"""

from __future__ import annotations

import asyncio

from core.writer import UsageWriter

ROW = (1, 1000, "p", "", "u1", 1, 0, "")


class FakeStorage:
    def __init__(self) -> None:
        self.inserts: list = []
        self.updates: list = []

    async def insert_events(self, rows) -> None:
        self.inserts.extend(rows)

    async def update_tokens(self, updates) -> None:
        self.updates.extend(updates)


def test_background_loop_writes_inserts_then_updates():
    async def scenario():
        storage = FakeStorage()
        writer = UsageWriter(storage, max_batch=10)
        await writer.start()
        writer.enqueue_insert(ROW)
        writer.enqueue_token_update(1, 55)
        await asyncio.sleep(0.05)
        await writer.stop()
        return storage

    storage = asyncio.run(scenario())
    assert storage.inserts == [ROW]
    assert storage.updates == [(55, 1)]


def test_flush_writes_without_a_running_consumer():
    async def scenario():
        storage = FakeStorage()
        writer = UsageWriter(storage)
        writer.enqueue_insert(ROW)
        writer.enqueue_token_update(1, 7)
        await writer.flush()
        return storage

    storage = asyncio.run(scenario())
    assert storage.inserts == [ROW]
    assert storage.updates == [(7, 1)]


def test_stop_flushes_all_queued_records_without_loss():
    async def scenario():
        storage = FakeStorage()
        writer = UsageWriter(storage, max_batch=2)
        await writer.start()
        for i in range(5):
            writer.enqueue_insert((i, 1000 + i, "p", "", "u", 1, 0, ""))
        await writer.stop()  # 优雅停止：5 条都应落盘，不丢失
        return storage

    storage = asyncio.run(scenario())
    assert len(storage.inserts) == 5


def test_queue_full_drops_and_counts():
    async def scenario():
        storage = FakeStorage()
        writer = UsageWriter(storage, max_queue=2)  # 不启动消费者 → 队列会满
        writer.enqueue_insert(ROW)
        writer.enqueue_insert(ROW)
        writer.enqueue_insert(ROW)  # 第三条被丢弃
        return writer

    writer = asyncio.run(scenario())
    assert writer.dropped_count() == 1
