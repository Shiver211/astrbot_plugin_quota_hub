"""作用域工具与 ID 归一化的单元测试。"""

from __future__ import annotations

from core.models import (
    SCOPE_GROUP,
    SCOPE_MEMBER,
    SCOPE_PRIVATE,
    ScopeKey,
    normalize_id,
    scopes_for,
)


def test_normalize_id_handles_various_types():
    assert normalize_id(None) == ""
    assert normalize_id(12345) == "12345"
    assert normalize_id(123.0) == "123"  # 整数浮点去掉小数
    assert normalize_id("  abc  ") == "abc"
    assert normalize_id(True) == ""  # 布尔不当作 ID


def test_scopes_for_group_message():
    keys = scopes_for("g1", "u1")
    assert keys == [
        ScopeKey(SCOPE_GROUP, "g1", ""),
        ScopeKey(SCOPE_MEMBER, "g1", "u1"),
    ]


def test_scopes_for_private_message():
    assert scopes_for("", "u1") == [ScopeKey(SCOPE_PRIVATE, "", "u1")]


def test_scope_key_label():
    assert ScopeKey(SCOPE_PRIVATE, "", "u").label() == "个人"
    assert ScopeKey(SCOPE_GROUP, "g", "").label() == "本群"
    assert ScopeKey(SCOPE_MEMBER, "g", "u").label() == "你在本群"
