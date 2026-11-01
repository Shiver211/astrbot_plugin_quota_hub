"""配额解析、拦截判定与文案格式化的单元测试。"""

from __future__ import annotations

from core import quota
from core.models import WINDOW_LONG, Decision, Override, Quota
from core.quota import ScopeUsage

# --- resolve_quota -------------------------------------------------------

DEFAULTS = Quota(call_limit=30, token_limit=1000, window_hours=5.0, enabled=True)


def test_resolve_quota_no_override_returns_defaults():
    assert quota.resolve_quota(DEFAULTS, None) == DEFAULTS


def test_resolve_quota_partial_override_merges_fields():
    ov = Override(scope="member", group_id="g", user_id="u", call_limit=5)
    merged = quota.resolve_quota(DEFAULTS, ov)
    assert merged.call_limit == 5  # 覆盖
    assert merged.token_limit == 1000  # 回落默认
    assert merged.window_hours == 5.0
    assert merged.enabled is True


def test_resolve_quota_override_can_set_unlimited_and_window():
    ov = Override(scope="group", group_id="g", call_limit=-1, window_hours=2.0)
    merged = quota.resolve_quota(DEFAULTS, ov)
    assert merged.call_limit == -1
    assert merged.window_hours == 2.0


def test_resolve_quota_disabled_override_disables_scope():
    ov = Override(scope="private", user_id="u", enabled=False)
    merged = quota.resolve_quota(DEFAULTS, ov)
    assert merged.enabled is False


# --- evaluate_scope ------------------------------------------------------


def test_unlimited_never_blocks():
    q = Quota(call_limit=-1, token_limit=-1)
    assert quota.evaluate_scope(q, used_calls=10_000, used_tokens=10_000) == (
        False,
        None,
        0,
        0,
    )


def test_calls_allowed_until_limit():
    q = Quota(call_limit=3, token_limit=-1)
    assert quota.evaluate_scope(q, 2, 0)[0] is False  # 第 3 次允许
    assert quota.evaluate_scope(q, 3, 0) == (True, "calls", 3, 3)  # 第 4 次拦截


def test_zero_call_limit_blocks_immediately():
    q = Quota(call_limit=0, token_limit=-1)
    assert quota.evaluate_scope(q, 0, 0) == (True, "calls", 0, 0)


def test_tokens_soft_limit_blocks_when_reached():
    q = Quota(call_limit=-1, token_limit=100)
    assert quota.evaluate_scope(q, 0, 99)[0] is False
    assert quota.evaluate_scope(q, 0, 100) == (True, "tokens", 100, 100)


def test_calls_checked_before_tokens():
    q = Quota(call_limit=1, token_limit=100)
    # 调用数已超 → 先报 calls
    assert quota.evaluate_scope(q, 1, 999)[1] == "calls"


def test_disabled_quota_never_blocks():
    q = Quota(call_limit=0, token_limit=0, enabled=False)
    assert quota.evaluate_scope(q, 100, 100)[0] is False


# --- format_block_message -----------------------------------------------


def test_format_block_message_substitutes_variables():
    d = Decision(
        blocked=True,
        scope="member",
        metric="calls",
        used=30,
        limit=30,
        window_hours=5.0,
        reset_in=3660,
    )
    msg = quota.format_block_message(
        "{scope_label}{metric_label} {used}/{limit} 窗口{window_hours}h 恢复{reset_in}",
        d,
    )
    assert "你在本群调用次数 30/30" in msg
    assert "窗口5h" in msg
    assert "1小时1分钟" in msg


def test_format_block_message_unknown_variable_falls_back():
    d = Decision(blocked=True, scope="group", metric="tokens", used=5, limit=5)
    msg = quota.format_block_message("{nonexistent_var}", d)
    assert "5/5" in msg  # 退回安全文案


# --- format_auto_reset_message ------------------------------------------


def test_format_auto_reset_message_substitutes_variables():
    d = Decision(
        blocked=True,
        scope="member",
        metric="calls",
        used=30,
        limit=30,
        window_hours=5.0,
        reset_in=3660,
    )
    msg = quota.format_auto_reset_message(
        "已达{window_label}上限，自动使用 1 次重置（剩余 {remaining} 次）"
        "，{scope_label}{metric_label} {used}/{limit}",
        d,
        2,
    )
    assert "已达短窗口(5小时)上限" in msg
    assert "剩余 2 次" in msg
    assert "你在本群调用次数 30/30" in msg


def test_format_auto_reset_message_renders_long_window():
    d = Decision(
        blocked=True,
        scope="private",
        metric="calls",
        used=50,
        limit=50,
        window=WINDOW_LONG,
        window_days=7.0,
        reset_in=86400,
    )
    msg = quota.format_auto_reset_message("{window_label}/{window_days}/{remaining}", d, 0)
    assert msg == "长窗口(7天)/7/0"


def test_format_auto_reset_message_unknown_variable_falls_back():
    d = Decision(blocked=True, scope="private", metric="calls", used=5, limit=5)
    msg = quota.format_auto_reset_message("{nonexistent_var}", d, 3)
    assert "短窗口" in msg  # 退回安全文案，含窗口标签
    assert "剩余 3 次" in msg


# --- decide_scopes（群聊双作用域同时生效）-------------------------------


def test_decide_scopes_all_clear():
    items = [
        ScopeUsage("group", Quota(10, -1, 5), 2, 0, 1000.0),
        ScopeUsage("member", Quota(5, -1, 5), 1, 0, 1000.0),
    ]
    assert quota.decide_scopes(items, 2000.0).blocked is False


def test_decide_scopes_group_blocks_first():
    items = [
        ScopeUsage("group", Quota(2, -1, 5), 2, 0, 1000.0),  # 群超限
        ScopeUsage("member", Quota(5, -1, 5), 0, 0, None),
    ]
    d = quota.decide_scopes(items, 2000.0)
    assert d.blocked and d.scope == "group" and d.metric == "calls"


def test_decide_scopes_member_blocks_when_group_ok():
    items = [
        ScopeUsage("group", Quota(100, -1, 5), 2, 0, 1000.0),  # 群未超
        ScopeUsage("member", Quota(2, -1, 5), 2, 0, 1500.0),  # 成员超限
    ]
    d = quota.decide_scopes(items, 2000.0)
    assert d.blocked and d.scope == "member" and d.metric == "calls"


def test_decide_scopes_skips_disabled_scope():
    items = [ScopeUsage("group", Quota(0, 0, 5, enabled=False), 99, 99, 1000.0)]
    assert quota.decide_scopes(items, 2000.0).blocked is False


def test_decide_scopes_computes_reset_in():
    # oldest=1000, now=2000, 5h 窗口=18000 → reset_in = 18000-(2000-1000)=17000
    items = [ScopeUsage("group", Quota(1, -1, 5), 1, 0, 1000.0)]
    d = quota.decide_scopes(items, 2000.0)
    assert d.blocked and d.reset_in == 17000.0


# --- 回退（备用模型）---------------------------------------------------


def test_resolve_quota_merges_fallback_fields():
    defaults = Quota(
        30, 1000, 5.0,
        fallback_provider_id="g", fallback_call_limit=10, fallback_token_limit=20,
    )
    ov = Override(
        scope="member", group_id="x", user_id="y",
        fallback_provider_id="p2", fallback_call_limit=5,
    )
    merged = quota.resolve_quota(defaults, ov)
    assert merged.fallback_provider_id == "p2"  # 覆盖
    assert merged.fallback_call_limit == 5  # 覆盖
    assert merged.fallback_token_limit == 20  # None → 回落默认


def test_hard_quota_adds_budget_and_keeps_unlimited():
    q = Quota(10, -1, 5, fallback_call_limit=5, fallback_token_limit=100)
    hard = quota.hard_quota(q)
    assert hard.call_limit == 15  # 10 + 5
    assert hard.token_limit == -1  # 不限保持不限


def test_decide_scopes_fallback_when_over_primary_within_hard():
    # call_limit=2，回退额度 +3 → 硬上限 5；用量=2，第 3 次落在回退区间
    q = Quota(2, -1, 5, fallback_provider_id="p2", fallback_call_limit=3)
    d = quota.decide_scopes([ScopeUsage("member", q, 2, 0, 1000.0)], 2000.0)
    assert d.blocked is False
    assert d.outcome == "fallback"
    assert d.fallback_provider_id == "p2"
    assert d.scope == "member" and d.metric == "calls" and d.limit == 2


def test_decide_scopes_block_when_over_hard_limit():
    q = Quota(2, -1, 5, fallback_provider_id="p2", fallback_call_limit=3)
    d = quota.decide_scopes([ScopeUsage("member", q, 5, 0, 1000.0)], 2000.0)  # 第 6 次 > 硬上限 5
    assert d.blocked is True and d.outcome == "block"
    assert d.metric == "calls" and d.limit == 5  # 硬上限


def test_decide_scopes_block_when_over_primary_without_provider():
    # 配了额度但无备用供应商 → 不能回退 → 超常规上限即拦截
    q = Quota(2, -1, 5, fallback_provider_id="", fallback_call_limit=3)
    d = quota.decide_scopes([ScopeUsage("member", q, 2, 0, 1000.0)], 2000.0)
    assert d.blocked is True and d.outcome == "block"


def test_decide_scopes_block_takes_priority_over_fallback():
    # 群在回退区间，但成员已超硬上限（额度 0）→ 整体拦截
    grp = Quota(2, -1, 5, fallback_provider_id="p", fallback_call_limit=5)
    mem = Quota(1, -1, 5, fallback_provider_id="p", fallback_call_limit=0)
    items = [ScopeUsage("group", grp, 3, 0, 1000.0), ScopeUsage("member", mem, 1, 0, 1000.0)]
    d = quota.decide_scopes(items, 2000.0)
    assert d.blocked is True and d.outcome == "block" and d.scope == "member"


def test_decide_scopes_token_fallback_zone():
    q = Quota(-1, 100, 5, fallback_provider_id="p", fallback_token_limit=50)  # 硬上限 150
    d = quota.decide_scopes([ScopeUsage("group", q, 0, 120, 1000.0)], 2000.0)
    assert d.outcome == "fallback" and d.metric == "tokens" and d.limit == 100


# --- merge_override ------------------------------------------------------


def test_merge_override_without_existing_returns_new():
    new = Override("group", "g1", "", call_limit=10, token_limit=100)
    assert quota.merge_override(None, new) is new


def test_merge_override_preserves_fallback_and_enabled():
    """指令只显式给出次数/Token 时，中台配置的回退与停用状态不能被清掉。"""
    existing = Override(
        "group", "g1", "",
        call_limit=5, token_limit=50, window_hours=8.0, enabled=False,
        fallback_provider_id="backup", fallback_call_limit=3, fallback_token_limit=300,
    )
    new = Override("group", "g1", "", call_limit=10, token_limit=100)
    merged = quota.merge_override(existing, new)
    assert merged.call_limit == 10  # 显式字段生效
    assert merged.token_limit == 100
    assert merged.window_hours == 8.0  # 未显式给出 → 保留原值
    assert merged.enabled is False
    assert merged.fallback_provider_id == "backup"
    assert merged.fallback_call_limit == 3
    assert merged.fallback_token_limit == 300


def test_merge_override_explicit_window_wins():
    existing = Override("private", "", "u1", window_hours=8.0)
    new = Override("private", "", "u1", call_limit=1, token_limit=2, window_hours=2.0)
    assert quota.merge_override(existing, new).window_hours == 2.0


# --- member_default 链式解析 ---------------------------------------------


def test_member_default_chain_composes_between_defaults_and_member_override():
    """全局成员默认 → 群成员默认覆盖 → 具体成员覆盖，逐层逐字段生效。"""
    md = Override("member_default", "g1", "", call_limit=10, window_hours=8.0)
    per_group = quota.resolve_quota(DEFAULTS, md)
    assert per_group.call_limit == 10
    assert per_group.token_limit == 1000  # 未覆盖 → 回落全局默认
    assert per_group.window_hours == 8.0
    member = quota.resolve_quota(
        per_group, Override("member", "g1", "u1", token_limit=50)
    )
    assert member.call_limit == 10  # 承接群成员默认
    assert member.token_limit == 50  # 具体成员覆盖优先
    assert member.window_hours == 8.0


def test_user_default_layers_above_group_member_default():
    """用户级成员默认压过群级成员默认，未覆盖字段逐层承接。"""
    md = Override("member_default", "g1", "", call_limit=10, token_limit=200)
    ud = Override("user_default", "", "u1", call_limit=99)
    step1 = quota.resolve_quota(DEFAULTS, md)
    step2 = quota.resolve_quota(step1, ud)
    assert step2.call_limit == 99  # 用户级默认更贴近个体，优先
    assert step2.token_limit == 200  # 未覆盖字段承接群级成员默认
    member = quota.resolve_quota(
        step2, Override("member", "g1", "u1", call_limit=1)
    )
    assert member.call_limit == 1  # 具体成员覆盖仍然最高
