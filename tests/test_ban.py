"""测试管理员 /ban 与 /unban 封禁指令及 LLM 对话拦截机制。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from core.models import BanRecord
from core.storage import Storage
from core.windows import parse_duration
from main import ADMIN_ONLY_TIP, QuotaHub


def test_parse_duration():
    # 秒
    assert parse_duration("30s") == 30.0
    assert parse_duration("10秒") == 10.0
    assert parse_duration("120") == 120.0

    # 分钟
    assert parse_duration("15m") == 900.0
    assert parse_duration("30分") == 1800.0
    assert parse_duration("5min") == 300.0
    assert parse_duration("10分钟") == 600.0

    # 小时
    assert parse_duration("2h") == 7200.0
    assert parse_duration("1小时") == 3600.0
    assert parse_duration("3时") == 10800.0

    # 天
    assert parse_duration("1d") == 86400.0
    assert parse_duration("3天") == 259200.0
    assert parse_duration("0.5d") == 43200.0

    # 周
    assert parse_duration("1w") == 7 * 86400.0
    assert parse_duration("2周") == 14 * 86400.0

    # 复合
    assert parse_duration("1d12h") == 86400.0 + 43200.0
    assert parse_duration("2h30m") == 7200.0 + 1800.0
    assert parse_duration("1天2小时") == 86400.0 + 7200.0

    # 永久
    assert parse_duration("永久") == -1.0
    assert parse_duration("perm") == -1.0
    assert parse_duration("forever") == -1.0
    assert parse_duration("-1") == -1.0

    # 解封 (0)
    assert parse_duration("0") == 0.0
    assert parse_duration("0s") == 0.0
    assert parse_duration("0d") == 0.0

    # 异常输入
    assert parse_duration("invalid") is None
    assert parse_duration("") is None
    assert parse_duration(None) is None


@pytest.mark.asyncio
async def test_storage_ban_operations(tmp_path: Path):
    storage = Storage(tmp_path / "test_ban.db")
    await storage.initialize()
    try:
        now = 1000
        # 1. 写入群成员封禁与全局封禁
        await storage.set_ban("g1", "u1", 1000, 2000, "admin1", "违规发言")
        await storage.set_ban("", "u2", 1000, -1, "admin1", "恶意刷屏")
        await storage.set_ban("g2", "u3", 500, 800, "admin2", "已过期")

        # 2. 查询有效封禁 (now = 1000)
        active = await storage.list_bans(now=1000)
        active_uids = {row[1] for row in active}
        assert "u1" in active_uids
        assert "u2" in active_uids
        assert "u3" not in active_uids  # 800 <= 1000，已过期

        # 查询全量封禁
        all_bans = await storage.list_bans()
        assert len(all_bans) == 3

        # 3. 删除封禁
        deleted = await storage.remove_ban("g1", "u1")
        assert deleted is True
        deleted_again = await storage.remove_ban("g1", "u1")
        assert deleted_again is False

        remaining = await storage.list_bans()
        assert len(remaining) == 2
    finally:
        await storage.close()


class DummyEvent:
    def __init__(
        self,
        group_id: str = "g1",
        user_id: str = "u1",
        text: str = "hello",
        is_admin_user: bool = False,
    ) -> None:
        self.group_id = group_id
        self.user_id = user_id
        self.text = text
        self._is_admin = is_admin_user
        self.unified_msg_origin = f"test:{group_id}"
        self.extras: dict[str, Any] = {}
        self.stopped = False
        self.sent_messages: list[Any] = []
        self.message_obj = None
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.self_id = ""

    def is_private_chat(self) -> bool:
        return not bool(self.group_id)

    def get_group_id(self) -> str:
        return self.group_id

    def get_sender_id(self) -> str:
        return self.user_id

    def get_self_id(self) -> str:
        return getattr(self, "self_id", "")

    def get_sender_name(self) -> str:
        return f"User_{self.user_id}"

    def get_message_str(self) -> str:
        return self.text

    def set_extra(self, k: str, v: Any) -> None:
        self.extras[k] = v

    def get_extra(self, k: str, default: Any = None) -> Any:
        return self.extras.get(k, default)

    async def send(self, msg: Any) -> None:
        self.sent_messages.append(msg)

    def stop_event(self) -> None:
        self.stopped = True

    def is_admin(self) -> bool:
        return self._is_admin

    def plain_result(self, text: str) -> str:
        return text


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
        "default_group_tokens": 10000,
        "default_member_tokens": 5000,
        "default_group_calls": 50,
        "default_member_calls": 50,
        "window_hours": 5.0,
        "send_ban_message": True,
    }
    hub = QuotaHub(context=DummyContext(), config=config)
    asyncio.run(hub.initialize())
    try:
        yield hub
    finally:
        asyncio.run(hub.terminate())


@pytest.mark.asyncio
async def test_admin_permission_check(temp_hub: QuotaHub):
    non_admin_event = DummyEvent(group_id="g1", user_id="u1", is_admin_user=False)

    # 尝试 ban
    results = [
        res
        async for res in temp_hub.cmd_ban(non_admin_event, "123456", "1d")
    ]
    assert len(results) == 1
    assert ADMIN_ONLY_TIP in results[0]

    # 尝试 unban
    results = [
        res
        async for res in temp_hub.cmd_unban(non_admin_event, "123456")
    ]
    assert len(results) == 1
    assert ADMIN_ONLY_TIP in results[0]

    # 尝试 banlist
    results = [
        res
        async for res in temp_hub.quota_banlist(non_admin_event)
    ]
    assert len(results) == 1
    assert ADMIN_ONLY_TIP in results[0]


@pytest.mark.asyncio
async def test_ban_and_llm_interception_workflow(temp_hub: QuotaHub, monkeypatch: pytest.MonkeyPatch):
    current_time = 1000000.0
    monkeypatch.setattr(temp_hub, "_now", lambda: current_time)

    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)

    # 1. 管理员封禁群 g1 里的用户 123456 1 天（86400秒）
    ban_results = [
        res
        async for res in temp_hub.cmd_ban(admin_event, "123456", "1d", "滥用刷屏")
    ]
    assert len(ban_results) == 1
    assert "✅ 已禁止 群 g1 成员 123456 与 LLM 对话" in ban_results[0]
    assert "时长 1天" in ban_results[0]
    assert "滥用刷屏" in ban_results[0]

    # 2. 检查内存中已记录
    ban = temp_hub.is_banned("g1", "123456", current_time)
    assert ban is not None
    assert ban.user_id == "123456"
    assert ban.group_id == "g1"
    assert ban.reason == "滥用刷屏"
    assert ban.expire_at == int(current_time + 86400)

    # 3. 被封禁用户 123456 在群 g1 发送消息触发 LLM
    member_event = DummyEvent(group_id="g1", user_id="123456", text="你好，请作诗")
    req = type("Req", (), {"model": "gpt-4o"})()

    await temp_hub._preselect_fallback(member_event)
    await temp_hub._enforce(member_event, req)

    # 必须被拦截：stop_event 被调用，发送封禁提示，未调用 record_call / 记账
    assert member_event.stopped is True
    assert len(member_event.sent_messages) == 1
    msg_text = str(member_event.sent_messages[0].parts)
    assert "🚫 你已被管理员禁止与 LLM 对话" in msg_text
    assert "滥用刷屏" in msg_text

    # 4. 同群未被封禁的其他用户 654321 对话不受影响
    normal_event = DummyEvent(group_id="g1", user_id="654321", text="你好")
    await temp_hub._enforce(normal_event, req)
    assert normal_event.stopped is False

    # 5. 用户 123456 在另一个群 g2（未封禁该群）对话不受影响
    other_group_event = DummyEvent(group_id="g2", user_id="123456", text="你好")
    await temp_hub._enforce(other_group_event, req)
    assert other_group_event.stopped is False

    # 6. 查看封禁列表
    list_results = [
        res
        async for res in temp_hub.quota_banlist(admin_event)
    ]
    assert len(list_results) == 1
    assert "123456" in list_results[0]
    assert "群 g1 成员" in list_results[0]

    # 7. 用户 123456 查看 /quota me 包含封禁提示
    me_results = [
        res
        async for res in temp_hub.quota_me(member_event)
    ]
    assert len(me_results) == 1
    assert "🚫 封禁状态：已被禁止与 LLM 对话" in me_results[0]

    # 8. 管理员执行 /unban 123456
    unban_results = [
        res
        async for res in temp_hub.cmd_unban(admin_event, "123456")
    ]
    assert len(unban_results) == 1
    assert "✅ 已解除 123456 的封禁" in unban_results[0]

    # 解封后再次请求 LLM，正常放行
    member_event2 = DummyEvent(group_id="g1", user_id="123456", text="你好2")
    await temp_hub._enforce(member_event2, req)
    assert member_event2.stopped is False


@pytest.mark.asyncio
async def test_ban_automatic_expiration(temp_hub: QuotaHub, monkeypatch: pytest.MonkeyPatch):
    current_time = 1000000.0
    monkeypatch.setattr(temp_hub, "_now", lambda: current_time)

    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)

    # 封禁 60 秒
    async for _ in temp_hub.cmd_ban(admin_event, "user_exp", "60s"):
        pass

    assert temp_hub.is_banned("g1", "user_exp", current_time) is not None

    # 时间推进 30 秒：仍处于封禁期
    current_time += 30.0
    assert temp_hub.is_banned("g1", "user_exp", current_time) is not None

    # 时间推进到 70 秒（超过 60 秒）：自动过期
    current_time += 40.0
    assert temp_hub.is_banned("g1", "user_exp", current_time) is None

    # LLM 请求正常放行
    req = type("Req", (), {"model": "gpt-4o"})()
    event = DummyEvent(group_id="g1", user_id="user_exp", text="hello")
    await temp_hub._enforce(event, req)
    assert event.stopped is False


@pytest.mark.asyncio
async def test_ban_with_at_mention(temp_hub: QuotaHub):
    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)

    # 模拟 @QQ 形式或者带 At 元件的消息
    at_comp = type("AtComp", (), {"qq": 998877})()
    admin_event.message_obj = type("MsgObj", (), {"message": [at_comp]})()

    results = [
        res
        async for res in temp_hub.cmd_ban(admin_event, "@某人", "2h", "违规")
    ]
    assert len(results) == 1
    assert "998877" in results[0]
    assert temp_hub.is_banned("g1", "998877") is not None


@pytest.mark.asyncio
async def test_global_ban_and_unban_by_zero(temp_hub: QuotaHub):
    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)

    # 带 global 关键字实现全局封禁
    results = [
        res
        async for res in temp_hub.cmd_ban(admin_event, "user_glob", "1d", "global 严重违规")
    ]
    assert "用户（全局）" in results[0]

    # 全局封禁在任何群都生效
    assert temp_hub.is_banned("g1", "user_glob") is not None
    assert temp_hub.is_banned("g2", "user_glob") is not None
    assert temp_hub.is_banned("", "user_glob") is not None

    # 用时间 0 解封 (/ban user 0)
    unban_results = [
        res
        async for res in temp_hub.cmd_ban(admin_event, "user_glob", "0")
    ]
    assert "✅ 已解除 user_glob 的封禁" in unban_results[0]
    assert temp_hub.is_banned("g1", "user_glob") is None


@pytest.mark.asyncio
async def test_ban_with_int_target_and_duration(temp_hub: QuotaHub):
    """测试 target 和 duration 作为 int 传入时（AstrBot 指令参数自动类型解析场景）。"""
    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)

    # 1. 模拟 /ban 2863677073 1s（用户实际报错场景：target 为 int）
    results = [
        res
        async for res in temp_hub.cmd_ban(admin_event, 2863677073, "1s")
    ]
    assert len(results) == 1
    assert "✅ 已禁止 群 g1 成员 2863677073 与 LLM 对话" in results[0]
    assert "时长 1秒" in results[0]
    assert temp_hub.is_banned("g1", 2863677073) is not None
    assert temp_hub.is_banned("g1", "2863677073") is not None

    # 2. 模拟 duration 为 int（如 /ban 2863677073 60）
    results_dur_int = [
        res
        async for res in temp_hub.cmd_ban(admin_event, 2863677073, 60)
    ]
    assert len(results_dur_int) == 1
    assert "1分钟" in results_dur_int[0]

    # 3. 模拟 /unban 2863677073（target 为 int）
    unban_res = [
        res
        async for res in temp_hub.cmd_unban(admin_event, 2863677073)
    ]
    assert len(unban_res) == 1
    assert "✅ 已解除 2863677073 的封禁" in unban_res[0]
    assert temp_hub.is_banned("g1", 2863677073) is None

    # 4. 模拟 duration 为 0（int 类型的 0 解封）
    # 先封禁
    async for _ in temp_hub.cmd_ban(admin_event, 2863677073, "10s"):
        pass
    assert temp_hub.is_banned("g1", 2863677073) is not None
    # duration = 0 解封
    unban_zero_res = [
        res
        async for res in temp_hub.cmd_ban(admin_event, 2863677073, 0)
    ]
    assert len(unban_zero_res) == 1
    assert "✅ 已解除 2863677073 的封禁" in unban_zero_res[0]
    assert temp_hub.is_banned("g1", 2863677073) is None

    # 5. 模拟 target 直接是 At 元件对象
    at_obj = type("AtComp", (), {"qq": 2863677073})()
    at_results = [
        res
        async for res in temp_hub.cmd_ban(admin_event, at_obj, "5s")
    ]
    assert len(at_results) == 1
    assert "2863677073" in at_results[0]
    assert temp_hub.is_banned("g1", 2863677073) is not None

    # 6. 帮助与查看封禁列表
    help_res = [res async for res in temp_hub.cmd_ban(admin_event, "help")]
    assert "用法：" in help_res[0]

    list_res = [res async for res in temp_hub.cmd_ban(admin_event, "list")]
    assert "当前封禁名单" in list_res[0]


@pytest.mark.asyncio
async def test_ban_command_interception(temp_hub: QuotaHub, monkeypatch: pytest.MonkeyPatch):
    """测试被封禁用户的任何指令均被拦截，非封禁用户正常放行。"""
    current_time = 1000000.0
    monkeypatch.setattr(temp_hub, "_now", lambda: current_time)

    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)
    async for _ in temp_hub.cmd_ban(admin_event, "u_cmd", "1d", "禁止使用指令"):
        pass

    assert temp_hub.is_banned("g1", "u_cmd") is not None

    # 1. 尝试以 "/" 开头的各类指令
    for cmd_text in ("/quota me", "/help", "/pixiv", "/ban", "/custom_cmd"):
        event = DummyEvent(group_id="g1", user_id="u_cmd", text=cmd_text)
        await temp_hub._intercept_banned_message(event)
        assert event.stopped is True, f"指令 {cmd_text} 应被拦截"
        assert len(event.sent_messages) == 1
        msg = str(event.sent_messages[0].parts)
        assert "🚫 你已被管理员禁止与 LLM 对话及交互至" in msg
        assert "禁止使用指令" in msg

    # 2. 尝试以其他常见指令前缀开头的指令（如 !, #）
    for cmd_text in ("!help", "#sign"):
        event = DummyEvent(group_id="g1", user_id="u_cmd", text=cmd_text)
        await temp_hub._intercept_banned_message(event)
        assert event.stopped is True
        assert len(event.sent_messages) == 1

    # 3. 正常未封禁用户发送指令不被拦截
    normal_event = DummyEvent(group_id="g1", user_id="u_normal", text="/help")
    await temp_hub._intercept_banned_message(normal_event)
    assert normal_event.stopped is False
    assert len(normal_event.sent_messages) == 0


@pytest.mark.asyncio
async def test_ban_wake_up_interception(temp_hub: QuotaHub, monkeypatch: pytest.MonkeyPatch):
    """测试被封禁用户的唤醒行为被拦截（is_wake, @Bot, 回复Bot）。"""
    current_time = 1000000.0
    monkeypatch.setattr(temp_hub, "_now", lambda: current_time)

    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)
    async for _ in temp_hub.cmd_ban(admin_event, "u_wake", "1d", "禁止唤醒"):
        pass

    # 1. 唤醒前缀 / wake_prefix 唤醒
    temp_hub.context = type("MockCtx", (), {
        "get_config": lambda *a, **kw: {"wake_prefix": ["bot", "猫猫"]}
    })()
    event_wake_prefix = DummyEvent(group_id="g1", user_id="u_wake", text="bot 你好机器人")
    await temp_hub._intercept_banned_message(event_wake_prefix)
    assert event_wake_prefix.stopped is True
    assert len(event_wake_prefix.sent_messages) == 1
    assert "🚫 你已被管理员禁止与 LLM 对话及交互至" in str(event_wake_prefix.sent_messages[0].parts)

    # 2. event.is_at_or_wake_command = True
    event_wake_cmd = DummyEvent(group_id="g1", user_id="u_wake", text="查天气")
    event_wake_cmd.is_at_or_wake_command = True
    await temp_hub._intercept_banned_message(event_wake_cmd)
    assert event_wake_cmd.stopped is True
    assert len(event_wake_cmd.sent_messages) == 1

    # 3. @ 当前 Bot（通过 At 组件且目标等于 self_id）
    event_at = DummyEvent(group_id="g1", user_id="u_wake", text="你好")
    event_at.self_id = "bot_12345"
    at_comp = type("AtComp", (), {"qq": "bot_12345"})()
    event_at.message_obj = type("MsgObj", (), {"message": [at_comp]})()
    await temp_hub._intercept_banned_message(event_at)
    assert event_at.stopped is True
    assert len(event_at.sent_messages) == 1

    # 4. 引用回复 Bot 的消息（Reply 组件）
    event_reply = DummyEvent(group_id="g1", user_id="u_wake", text="接着说")
    event_reply.self_id = "bot_12345"
    reply_comp = type("ReplyComp", (), {"sender": "bot_12345"})()
    event_reply.message_obj = type("MsgObj", (), {"message": [reply_comp]})()
    await temp_hub._intercept_banned_message(event_reply)
    assert event_reply.stopped is True
    assert len(event_reply.sent_messages) == 1


@pytest.mark.asyncio
async def test_ban_private_chat_and_group_normal_chat(temp_hub: QuotaHub, monkeypatch: pytest.MonkeyPatch):
    """测试私聊拦截以及群聊普通发言（静默阻断后续，不刷屏提示）。"""
    current_time = 1000000.0
    monkeypatch.setattr(temp_hub, "_now", lambda: current_time)

    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)
    # 全局封禁
    async for _ in temp_hub.cmd_ban(admin_event, "u_glob_test", "1d", "global 违规发言"):
        pass

    # 1. 私聊场景：任何消息均被视为对 Bot 交互，拦截并发送提示
    private_event = DummyEvent(group_id="", user_id="u_glob_test", text="私聊你一个问题")
    await temp_hub._intercept_banned_message(private_event)
    assert private_event.stopped is True
    assert len(private_event.sent_messages) == 1
    assert "🚫 你已被管理员禁止与 LLM 对话及交互至" in str(private_event.sent_messages[0].parts)

    # 2. 群聊普通闲聊场景：既非指令也未唤醒 Bot
    # 不阻断群内闲聊，更不发送封禁提示，避免打扰群内正常交流或产生误触发
    chat_event = DummyEvent(group_id="g1", user_id="u_glob_test", text="明天中午大家一起吃肯德基吗？")
    await temp_hub._intercept_banned_message(chat_event)
    assert chat_event.stopped is False, "群聊普通闲聊不应被拦截"
    assert len(chat_event.sent_messages) == 0, "群聊普通闲聊不应发送封禁提醒刷屏"

    # 3. 群聊表情包 / 纯图片场景（text 为空，图片组件）：静默放行，不触发封禁提示
    sticker_event = DummyEvent(group_id="g1", user_id="u_glob_test", text="")
    img_comp = type("ImageComp", (), {})()
    sticker_event.message_obj = type("MsgObj", (), {"message": [img_comp]})()
    await temp_hub._intercept_banned_message(sticker_event)
    assert sticker_event.stopped is False, "表情包/纯图片不应被拦截"
    assert len(sticker_event.sent_messages) == 0, "表情包/纯图片不应发送封禁提醒"

    # 4. 单独发送问号 "?" 或普通文本（无唤醒词、无指令前缀）：不触发封禁提示
    for normal_text in ("?", "？", "还有主动回复", "今天天气真好"):
        txt_event = DummyEvent(group_id="g1", user_id="u_glob_test", text=normal_text)
        await temp_hub._intercept_banned_message(txt_event)
        assert txt_event.stopped is False, f"普通文本 {normal_text} 不应被拦截"
        assert len(txt_event.sent_messages) == 0, f"普通文本 {normal_text} 不应发送封禁提醒"


@pytest.mark.asyncio
async def test_ban_silent_mode_and_direct_calls(temp_hub: QuotaHub, monkeypatch: pytest.MonkeyPatch):
    """测试静默模式（send_ban_message=False）以及直接调用组件方法时的防御。"""
    current_time = 1000000.0
    monkeypatch.setattr(temp_hub, "_now", lambda: current_time)

    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)
    async for _ in temp_hub.cmd_ban(admin_event, "u_direct", "1d", "受限用户"):
        pass

    # 1. 开启 send_ban_message = False 时，指令仍被阻断，但无提示发送
    temp_hub.config["send_ban_message"] = False
    cmd_event = DummyEvent(group_id="g1", user_id="u_direct", text="/quota me")
    await temp_hub._intercept_banned_message(cmd_event)
    assert cmd_event.stopped is True
    assert len(cmd_event.sent_messages) == 0
    temp_hub.config["send_ban_message"] = True

    # 2. 直接调用 _self_reset：被封禁直接拦截返回提示
    reset_event = DummyEvent(group_id="g1", user_id="u_direct")
    res_list = [r async for r in temp_hub._self_reset(reset_event, "short")]
    assert len(res_list) == 1
    assert "🚫 你已被管理员禁止与 LLM 对话及交互至" in res_list[0]
    assert reset_event.stopped is True

    # 3. 直接调用 quota_request：被封禁直接拦截返回提示
    temp_hub.config["allow_reset_request"] = True
    req_event = DummyEvent(group_id="g1", user_id="u_direct")
    req_res = [r async for r in temp_hub.quota_request(req_event, "short", "1")]
    assert len(req_res) == 1
    assert "🚫 你已被管理员禁止与 LLM 对话及交互至" in req_res[0]
    assert req_event.stopped is True


@pytest.mark.asyncio
async def test_ban_expiration_and_deduplication_and_isolation(temp_hub: QuotaHub, monkeypatch: pytest.MonkeyPatch):
    """测试封禁过期、重复提示去重、群隔离与全局封禁。"""
    current_time = 1000000.0
    monkeypatch.setattr(temp_hub, "_now", lambda: current_time)

    admin_event = DummyEvent(group_id="g1", user_id="admin1", is_admin_user=True)

    # 1. 在 g1 封禁 u_iso 60 秒
    async for _ in temp_hub.cmd_ban(admin_event, "u_iso", "60s", "测试隔离"):
        pass

    # 在 g1 发指令：被拦截
    ev_g1 = DummyEvent(group_id="g1", user_id="u_iso", text="/help")
    await temp_hub._intercept_banned_message(ev_g1)
    assert ev_g1.stopped is True
    assert len(ev_g1.sent_messages) == 1

    # 在 g2 发指令：群隔离，不被拦截
    ev_g2 = DummyEvent(group_id="g2", user_id="u_iso", text="/help")
    await temp_hub._intercept_banned_message(ev_g2)
    assert ev_g2.stopped is False
    assert len(ev_g2.sent_messages) == 0

    # 2. 消息去重测试：_intercept_banned_message 与 _enforce 连续调用同一 event 时，提示不重复发送
    ev_dup = DummyEvent(group_id="g1", user_id="u_iso", text="/help")
    req = type("Req", (), {"model": "gpt-4o"})()
    await temp_hub._intercept_banned_message(ev_dup)
    assert len(ev_dup.sent_messages) == 1
    await temp_hub._enforce(ev_dup, req)
    assert len(ev_dup.sent_messages) == 1, "同一事件中不应重复发送封禁提示"

    # 3. 时间推进 70 秒（超过 60 秒），自动过期
    current_time += 70.0
    ev_expired = DummyEvent(group_id="g1", user_id="u_iso", text="/help")
    await temp_hub._intercept_banned_message(ev_expired)
    assert ev_expired.stopped is False
    assert len(ev_expired.sent_messages) == 0

    # 4. 插件总开关关闭时（enabled=False），不执行拦截
    async for _ in temp_hub.cmd_ban(admin_event, "u_iso", "1d"):
        pass
    temp_hub.config["enabled"] = False
    ev_disabled = DummyEvent(group_id="g1", user_id="u_iso", text="/help")
    await temp_hub._intercept_banned_message(ev_disabled)
    assert ev_disabled.stopped is False
    temp_hub.config["enabled"] = True


