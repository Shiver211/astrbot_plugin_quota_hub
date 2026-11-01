"""``api/serializers.py`` 的单元测试：锁定与前端约定的字段契约。

见 docs/design/0.4.0-phase2-spec.md §1、§2：列表行的 ``cycle``、``Request`` 与
``LogEntry`` 的附加标签字段是前端直接渲染的，字段名与取值口径不能漂移。
"""

from __future__ import annotations

import dataclasses

import pytest
from api.serializers import (
    clamp_limit,
    non_negative_int,
    opt_int,
    optional_positive_int,
    parse_override_fields,
    positive_int,
    serialize_cycle,
    serialize_log_entry,
    serialize_override,
    serialize_quota,
    serialize_request,
)
from core.cycles import (
    ACTOR_AUTO,
    ACTOR_DASHBOARD,
    ACTOR_SELF,
    CycleState,
    ResetLogEntry,
)
from core.models import SCOPE_MEMBER, WINDOW_LONG, WINDOW_SHORT, Override, Quota
from core.requests import REQUEST_APPROVED, new_request

NAMES = {"u1": "小明", "admin9": "管理员甲"}
GROUPS = {"g1": "测试群"}
REMARKS = {"g1": "内部群"}


def _request(**changes):
    req = new_request(
        SCOPE_MEMBER, "g1", "u1", WINDOW_SHORT, 2, "赶稿", 100.0, 50.0, "origin:1"
    )
    return dataclasses.replace(req, **changes)


def test_clamp_limit_bounds():
    assert clamp_limit(None) == 100
    assert clamp_limit("") == 100
    assert clamp_limit("7") == 7
    assert clamp_limit(0) == 1
    assert clamp_limit(9999) == 500
    assert clamp_limit("abc") == 100  # 非法值回落默认


def test_positive_int_validation():
    assert positive_int("3", "id") == 3
    with pytest.raises(ValueError, match="id 必须是正整数"):
        positive_int(0, "id")
    with pytest.raises(ValueError, match="id 必须是正整数"):
        positive_int("x", "id")


def test_optional_and_non_negative_int():
    assert optional_positive_int(None, "amount") is None
    assert optional_positive_int("", "amount") is None
    assert optional_positive_int(2, "amount") == 2
    with pytest.raises(ValueError):
        optional_positive_int(0, "amount")
    assert non_negative_int(None, "short") == 0
    assert non_negative_int("0", "short") == 0
    with pytest.raises(ValueError, match="short 必须是大于等于 0 的整数"):
        non_negative_int(-1, "short")


def test_serialize_cycle_fields():
    state = CycleState(
        window_start=100.0, short_marker=5.0, short_allowance=2, long_allowance=1
    )
    payload = serialize_cycle(state, 3, 400, 800.0, _request(id=7))
    assert payload == {
        "long_calls": 3,
        "long_tokens": 400,
        "window_start": 100.0,
        "long_end": 800.0,
        "short_marker": 5.0,
        "short_allowance": 2,
        "long_allowance": 1,
        "pending_request": {
            "id": 7,
            "window": WINDOW_SHORT,
            "amount": 2,
            "reason": "赶稿",
            "created_at": 100.0,
        },
    }


def test_serialize_cycle_without_cycle_or_pending():
    payload = serialize_cycle(CycleState(), 0, 0, None)
    assert payload["window_start"] is None
    assert payload["long_end"] is None
    assert payload["pending_request"] is None


def test_serialize_request_labels():
    req = _request(id=9, status=REQUEST_APPROVED, granted=3, decided_by="dashboard")
    row = serialize_request(req, GROUPS, REMARKS, NAMES)
    assert row["status_label"] == "已批准"
    assert row["window_label"] == "短窗口"
    assert row["entity_text"] == "群成员(g1/u1)"
    assert (row["group_name"], row["group_remark"], row["user_name"]) == (
        "测试群",
        "内部群",
        "小明",
    )
    # 原始字段一并返回，前端不必二次请求
    for field in ("id", "scope", "amount", "granted", "cycle_start", "notified"):
        assert field in row


def test_serialize_log_entry_actor_labels():
    entry = ResetLogEntry(
        ts=10.0,
        scope=SCOPE_MEMBER,
        group_id="g1",
        user_id="u1",
        window=WINDOW_LONG,
        actor=ACTOR_DASHBOARD,
        actor_id="admin9",
        consumed=False,
        id=3,
    )
    row = serialize_log_entry(entry, GROUPS, REMARKS, NAMES)
    assert row["window_label"] == "长窗口"
    assert row["actor_label"] == "中台"
    assert row["actor_name"] == "管理员甲"
    assert row["user_name"] == "小明"
    assert row["consumed"] is False

    self_row = serialize_log_entry(
        ResetLogEntry(
            ts=11.0,
            scope=SCOPE_MEMBER,
            group_id="g1",
            user_id="u1",
            window=WINDOW_SHORT,
            actor=ACTOR_SELF,
            actor_id="u1",
            consumed=True,
        ),
        GROUPS,
        REMARKS,
        NAMES,
    )
    assert self_row["actor_label"] == "本人"
    assert self_row["actor_name"] == "小明"

    # 触顶自动重置：发起方显示为「自动」，同样消耗次数
    auto_row = serialize_log_entry(
        ResetLogEntry(
            ts=12.0,
            scope=SCOPE_MEMBER,
            group_id="g1",
            user_id="u1",
            window=WINDOW_SHORT,
            actor=ACTOR_AUTO,
            actor_id="u1",
            consumed=True,
        ),
        GROUPS,
        REMARKS,
        NAMES,
    )
    assert auto_row["actor_label"] == "自动"
    assert auto_row["consumed"] is True


# --- opt_int 边界 -----------------------------------------------------------


def test_opt_int_rejects_non_integer_float():
    """``1.9`` 不是整数值的浮点数，视为非法（回落 None），不做四舍五入截断。"""
    assert opt_int(1.9) is None


def test_opt_int_accepts_integer_valued_float():
    assert opt_int(2.0) == 2


def test_opt_int_parses_numeric_string():
    assert opt_int("3") == 3


def test_opt_int_none_and_empty_string_are_none():
    assert opt_int(None) is None
    assert opt_int("") is None


# --- parse_override_fields：只解析出现过的键 ---------------------------------


def _always_exists(_provider_id: str) -> bool:
    return True


def test_parse_override_fields_only_parses_present_keys():
    """未出现在请求体里的键不应出现在结果字典中（否则会把另一张表单的字段清空）。"""
    result = parse_override_fields({"call_limit": 5}, _always_exists)
    assert result == {"call_limit": 5}


def test_parse_override_fields_parses_all_present_keys_together():
    data = {
        "enabled": True,
        "call_limit": 5,
        "token_limit": 200,
        "window_hours": 2.5,
        "fallback_provider_id": "p1",
        "fallback_call_limit": 1,
        "fallback_token_limit": 2,
        "long_calls": 10,
        "long_tokens": 20,
        "long_window_days": 3.0,
        "self_reset_short": 1,
        "self_reset_long": 2,
    }
    result = parse_override_fields(data, _always_exists)
    assert result == data


def test_parse_override_fields_empty_body_returns_empty_dict():
    assert parse_override_fields({}, _always_exists) == {}


# --- window_hours / long_window_days 必须为正 --------------------------------


@pytest.mark.parametrize("value", [0, -1, "0", -0.5])
def test_parse_override_fields_rejects_non_positive_window_hours(value):
    with pytest.raises(ValueError, match="window_hours 必须大于 0"):
        parse_override_fields({"window_hours": value}, _always_exists)


def test_parse_override_fields_accepts_positive_window_hours():
    assert parse_override_fields({"window_hours": 3.5}, _always_exists) == {
        "window_hours": 3.5
    }


def test_parse_override_fields_window_hours_empty_string_is_none():
    """空串表示「回落默认」，不触发 > 0 校验。"""
    assert parse_override_fields({"window_hours": ""}, _always_exists) == {
        "window_hours": None
    }


@pytest.mark.parametrize("value", [0, -1, "0", -3.5])
def test_parse_override_fields_rejects_non_positive_long_window_days(value):
    with pytest.raises(ValueError, match="long_window_days 必须大于 0"):
        parse_override_fields({"long_window_days": value}, _always_exists)


def test_parse_override_fields_long_window_days_empty_string_is_none():
    assert parse_override_fields({"long_window_days": ""}, _always_exists) == {
        "long_window_days": None
    }


# --- 备用模型额度 / 自助重置次数不能为负 --------------------------------------


@pytest.mark.parametrize("key", ["fallback_call_limit", "fallback_token_limit"])
def test_parse_override_fields_rejects_negative_fallback_limits(key):
    with pytest.raises(ValueError, match="备用模型额外额度不能为负"):
        parse_override_fields({key: -1}, _always_exists)


@pytest.mark.parametrize("key", ["fallback_call_limit", "fallback_token_limit"])
def test_parse_override_fields_accepts_zero_fallback_limit(key):
    assert parse_override_fields({key: 0}, _always_exists) == {key: 0}


@pytest.mark.parametrize("key", ["self_reset_short", "self_reset_long"])
def test_parse_override_fields_rejects_negative_self_reset(key):
    with pytest.raises(ValueError, match="自助重置次数不能为负"):
        parse_override_fields({key: -1}, _always_exists)


@pytest.mark.parametrize("key", ["self_reset_short", "self_reset_long"])
def test_parse_override_fields_accepts_zero_self_reset(key):
    assert parse_override_fields({key: 0}, _always_exists) == {key: 0}


# --- long_calls / long_tokens：-1 表示不限，< -1 非法 -------------------------


@pytest.mark.parametrize("key", ["long_calls", "long_tokens"])
def test_parse_override_fields_rejects_long_metric_below_negative_one(key):
    with pytest.raises(ValueError, match="长窗口上限需大于等于 -1"):
        parse_override_fields({key: -2}, _always_exists)


@pytest.mark.parametrize("key", ["long_calls", "long_tokens"])
def test_parse_override_fields_allows_long_metric_unlimited(key):
    assert parse_override_fields({key: -1}, _always_exists) == {key: -1}


# --- fallback_provider_id 走 provider_exists 回调 -----------------------------


def test_parse_override_fields_rejects_unknown_fallback_provider():
    with pytest.raises(ValueError, match="未找到备用模型供应商：gpt-x"):
        parse_override_fields(
            {"fallback_provider_id": "gpt-x"}, lambda pid: False
        )


def test_parse_override_fields_accepts_known_fallback_provider():
    result = parse_override_fields(
        {"fallback_provider_id": "gpt-x"}, lambda pid: pid == "gpt-x"
    )
    assert result == {"fallback_provider_id": "gpt-x"}


def test_parse_override_fields_fallback_provider_empty_string_skips_existence_check():
    """空串归一化为 None（回落默认），不应触发存在性回调。"""
    calls: list[str] = []

    def spy(pid: str) -> bool:
        calls.append(pid)
        return True

    result = parse_override_fields({"fallback_provider_id": ""}, spy)
    assert result == {"fallback_provider_id": None}
    assert calls == []


# --- enabled 布尔 -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), (False, False), (1, True), (0, False)],
)
def test_parse_override_fields_enabled_is_coerced_to_bool(value, expected):
    assert parse_override_fields({"enabled": value}, _always_exists) == {
        "enabled": expected
    }


# --- 空串统一回落为 None -------------------------------------------------------


def test_parse_override_fields_empty_strings_become_none():
    data = {
        "call_limit": "",
        "token_limit": "",
        "fallback_call_limit": "",
        "fallback_token_limit": "",
        "long_calls": "",
        "long_tokens": "",
        "self_reset_short": "",
        "self_reset_long": "",
    }
    result = parse_override_fields(data, _always_exists)
    assert result == dict.fromkeys(data)


# --- serialize_quota / serialize_override：附加字段齐全 -----------------------


def test_serialize_quota_includes_all_extended_fields():
    q = Quota(
        call_limit=10,
        token_limit=1000,
        window_hours=5.0,
        enabled=True,
        fallback_provider_id="p1",
        fallback_call_limit=2,
        fallback_token_limit=200,
        long_calls=50,
        long_tokens=5000,
        long_window_days=7.0,
        self_reset_short=1,
        self_reset_long=2,
    )
    assert serialize_quota(q) == {
        "call_limit": 10,
        "token_limit": 1000,
        "window_hours": 5.0,
        "enabled": True,
        "fallback_provider_id": "p1",
        "fallback_call_limit": 2,
        "fallback_token_limit": 200,
        "long_calls": 50,
        "long_tokens": 5000,
        "long_window_days": 7.0,
        "self_reset_short": 1,
        "self_reset_long": 2,
    }


def test_serialize_override_includes_all_extended_fields():
    ov = Override(
        scope=SCOPE_MEMBER,
        group_id="g1",
        user_id="u1",
        call_limit=10,
        token_limit=None,
        window_hours=None,
        enabled=False,
        fallback_provider_id=None,
        fallback_call_limit=None,
        fallback_token_limit=None,
        long_calls=-1,
        long_tokens=-1,
        long_window_days=3.0,
        self_reset_short=0,
        self_reset_long=0,
    )
    assert serialize_override(ov) == {
        "scope": SCOPE_MEMBER,
        "group_id": "g1",
        "user_id": "u1",
        "call_limit": 10,
        "token_limit": None,
        "window_hours": None,
        "enabled": False,
        "fallback_provider_id": None,
        "fallback_call_limit": None,
        "fallback_token_limit": None,
        "long_calls": -1,
        "long_tokens": -1,
        "long_window_days": 3.0,
        "self_reset_short": 0,
        "self_reset_long": 0,
    }
