"""测试单条消息触发多次 LLM 请求时的 Token 记录与提取功能。"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from core.models import ScopeKey, scopes_for
from core.storage import Storage
from core.writer import UsageWriter
from main import QuotaHub


@dataclass
class DummyCompletionUsage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    total_tokens: int = 150


@dataclass
class DummyResponse:
    usage: Any = None
    raw_completion: Any = None
    raw_response: Any = None


def test_extract_tokens_openai_completion_usage():
    resp = DummyResponse(usage=DummyCompletionUsage(120, 80, 200))
    assert QuotaHub._extract_tokens(resp) == 200


def test_extract_tokens_dict_formats():
    # total_tokens in dict
    assert QuotaHub._extract_tokens(DummyResponse(usage={"total_tokens": 350})) == 350
    # total in dict
    assert QuotaHub._extract_tokens(DummyResponse(usage={"total": 420})) == 420
    # prompt + completion in dict
    assert (
        QuotaHub._extract_tokens(
            DummyResponse(usage={"prompt_tokens": 150, "completion_tokens": 250})
        )
        == 400
    )
    # anthropic input_tokens + output_tokens in dict
    assert (
        QuotaHub._extract_tokens(
            DummyResponse(usage={"input_tokens": 80, "output_tokens": 70})
        )
        == 150
    )
    # input + output in dict
    assert QuotaHub._extract_tokens(DummyResponse(usage={"input": 30, "output": 40})) == 70


def test_extract_tokens_raw_completion_fallback():
    # usage 在 raw_completion 上
    resp = DummyResponse(
        usage=None,
        raw_completion=DummyResponse(usage=DummyCompletionUsage(50, 60, 110)),
    )
    assert QuotaHub._extract_tokens(resp) == 110


def test_extract_tokens_raw_response_fallback():
    # usage 在 raw_response 上
    resp = DummyResponse(
        usage=None,
        raw_response=DummyResponse(usage={"total_tokens": 99}),
    )
    assert QuotaHub._extract_tokens(resp) == 99


def test_extract_tokens_none_and_invalid():
    assert QuotaHub._extract_tokens(None) == 0
    assert QuotaHub._extract_tokens(DummyResponse(usage=None)) == 0
    assert QuotaHub._extract_tokens(DummyResponse(usage={})) == 0
    assert QuotaHub._extract_tokens(DummyResponse(usage={"total_tokens": "invalid"})) == 0


class DummyEvent:
    def __init__(self, group_id: str = "g1", user_id: str = "u1") -> None:
        self.group_id = group_id
        self.user_id = user_id
        self.unified_msg_origin = f"test:{group_id}"
        self.extras: dict[str, Any] = {}
        self.stopped = False
        self.sent_messages: list[Any] = []

    def get_group_id(self) -> str:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.user_id

    def get_sender_name(self) -> str:
        return "TestUser"

    def get_message_str(self) -> str:
        return "hello"

    def set_extra(self, k: str, v: Any) -> None:
        self.extras[k] = v

    def get_extra(self, k: str, default: Any = None) -> Any:
        return self.extras.get(k, default)

    async def send(self, msg: Any) -> None:
        self.sent_messages.append(msg)

    def stop_event(self) -> None:
        self.stopped = True


class DummyContext:
    def register_web_api(self, *args, **kwargs) -> None:
        pass


@pytest.fixture
def temp_hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import astrbot.api.star as star_mod

    monkeypatch.setattr(star_mod.StarTools, "get_data_dir", lambda name: str(tmp_path))
    config = {
        "enabled": True,
        "enforcement_mode": "all",
        "default_group_tokens": 1000,
        "default_member_tokens": 1000,
        "default_group_calls": 50,
        "default_member_calls": 50,
        "window_hours": 5.0,
    }
    hub = QuotaHub(context=DummyContext(), config=config)
    asyncio.run(hub.initialize())
    try:
        yield hub
    finally:
        asyncio.run(hub.terminate())


def test_single_message_multiple_sequential_llm_requests(temp_hub: QuotaHub):
    async def scenario():
        event = DummyEvent(group_id="g1", user_id="u1")
        req1 = type("Req", (), {"model": "gpt-4o"})()
        req2 = type("Req", (), {"model": "gpt-4o"})()

        # 第 1 次 LLM 请求
        await temp_hub._enforce(event, req1)
        assert hasattr(event, "_quota_hub_pending_eids")
        assert len(event._quota_hub_pending_eids) == 1
        eid1 = event._quota_hub_pending_eids[0]

        # 第 1 次 LLM 响应 (消耗 300 tokens)
        resp1 = DummyResponse(usage=DummyCompletionUsage(200, 100, 300))
        await temp_hub._record_tokens(event, resp1)
        assert len(event._quota_hub_pending_eids) == 0

        # 第 2 次 LLM 请求 (同一条用户消息中由于工具调用再次触发)
        await temp_hub._enforce(event, req2)
        assert len(event._quota_hub_pending_eids) == 1
        eid2 = event._quota_hub_pending_eids[0]
        assert eid1 != eid2

        # 第 2 次 LLM 响应 (消耗 450 tokens)
        resp2 = DummyResponse(usage=DummyCompletionUsage(300, 150, 450))
        await temp_hub._record_tokens(event, resp2)
        assert len(event._quota_hub_pending_eids) == 0

        # 验证内存聚合器中累加了两次调用的全部 Token (300 + 450 = 750)
        member_key = ScopeKey("member", "g1", "u1")
        calls, tokens, _ = temp_hub.aggregator.usage(member_key, temp_hub._now(), 5.0)
        assert tokens == 750

    asyncio.run(scenario())


def test_single_message_multiple_concurrent_llm_requests(temp_hub: QuotaHub):
    async def scenario():
        event = DummyEvent(group_id="g1", user_id="u1")
        req1 = type("Req", (), {"model": "gpt-4o"})()
        req2 = type("Req", (), {"model": "gpt-4o"})()

        # 请求 1 和 请求 2 重叠/并发触发 _enforce
        await temp_hub._enforce(event, req1)
        await temp_hub._enforce(event, req2)
        assert len(event._quota_hub_pending_eids) == 2
        eid1, eid2 = event._quota_hub_pending_eids

        # 响应 1 返回并记录
        resp1 = DummyResponse(usage=DummyCompletionUsage(100, 100, 200))
        await temp_hub._record_tokens(event, resp1)
        assert event._quota_hub_pending_eids == [eid2]

        # 响应 2 返回并记录
        resp2 = DummyResponse(usage=DummyCompletionUsage(200, 200, 400))
        await temp_hub._record_tokens(event, resp2)
        assert len(event._quota_hub_pending_eids) == 0

        # 验证两个请求的 Token 均被正确记录，无覆盖无丢弃 (200 + 400 = 600)
        member_key = ScopeKey("member", "g1", "u1")
        calls, tokens, _ = temp_hub.aggregator.usage(member_key, temp_hub._now(), 5.0)
        assert tokens == 600

    asyncio.run(scenario())


def test_tokens_accumulate_and_trigger_block_on_third_request(temp_hub: QuotaHub):
    async def scenario():
        event = DummyEvent(group_id="g1", user_id="u1")
        req = type("Req", (), {"model": "gpt-4o"})()

        # 额度上限为 1000 tokens。第 1 次用 600
        await temp_hub._enforce(event, req)
        await temp_hub._record_tokens(
            event, DummyResponse(usage=DummyCompletionUsage(400, 200, 600))
        )

        # 第 2 次用 500 (总计 1100 > 1000)
        await temp_hub._enforce(event, req)
        await temp_hub._record_tokens(
            event, DummyResponse(usage=DummyCompletionUsage(300, 200, 500))
        )

        # 第 3 次请求应该被拦截（Token 超额）
        event3 = DummyEvent(group_id="g1", user_id="u1")
        await temp_hub._enforce(event3, req)
        assert event3.stopped is True
        assert any("Token" in str(msg.parts) for msg in event3.sent_messages)

    asyncio.run(scenario())


def test_backward_compatibility_single_eid_attr(temp_hub: QuotaHub):
    async def scenario():
        # 如果外部没有 pending_eids，仅设置了 _quota_hub_eid
        event = DummyEvent(group_id="g1", user_id="u1")
        now = temp_hub._now()
        eid = temp_hub.aggregator.record_call(scopes_for("g1", "u1"), now)
        setattr(event, "_quota_hub_eid", eid)

        resp = DummyResponse(usage=DummyCompletionUsage(50, 50, 100))
        await temp_hub._record_tokens(event, resp)

        member_key = ScopeKey("member", "g1", "u1")
        calls, tokens, _ = temp_hub.aggregator.usage(member_key, now, 5.0)
        assert tokens == 100

    asyncio.run(scenario())
