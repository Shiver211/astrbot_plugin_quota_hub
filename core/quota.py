"""配额解析（覆盖优先）、拦截判定与拦截文案格式化。

判定顺序（见 CONTEXT.md 与 docs/adr/0002）：
先长窗口后短窗口，同一窗口内按传入顺序（群共享池先于成员），只报告第一个违规。
长窗口是预算上限，耗尽即拦截，不参与回退到备用模型。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .models import (
    METRIC_LABELS,
    OUTCOME_BLOCK,
    OUTCOME_FALLBACK,
    OUTCOME_NORMAL,
    SCOPE_LABELS,
    WINDOW_LABELS,
    WINDOW_LONG,
    WINDOW_SHORT,
    Decision,
    Override,
    Quota,
)
from .windows import format_duration, reset_in_seconds

# 覆盖层里「``None`` 表示回落上一层」的字段；``enabled`` 不在其列（它总是显式生效）。
OVERRIDABLE_FIELDS = (
    "call_limit",
    "token_limit",
    "window_hours",
    "fallback_provider_id",
    "fallback_call_limit",
    "fallback_token_limit",
    "long_calls",
    "long_tokens",
    "long_window_days",
    "self_reset_short",
    "self_reset_long",
)


@dataclass(frozen=True)
class ScopeUsage:
    """``decide_scopes`` 的单个作用域输入：配额 + 短窗口用量 + 长窗口用量。

    ``oldest`` 为短窗口内最早一条用量的时间戳（无用量为 ``None``）；
    ``long_end`` 为当前长窗口周期的到期时刻（无进行中的周期为 ``None``）。
    """

    scope: str
    quota: Quota
    calls: int
    tokens: int
    oldest: float | None
    long_calls: int = 0
    long_tokens: int = 0
    long_end: float | None = None


def resolve_quota(defaults: Quota, override: Override | None) -> Quota:
    """把单实体覆盖合并到默认配额之上，逐字段生效（``None`` 字段回落默认）。"""
    if override is None:
        return defaults
    changes = {
        name: value
        for name in OVERRIDABLE_FIELDS
        if (value := getattr(override, name)) is not None
    }
    return replace(defaults, enabled=override.enabled, **changes)


def merge_fields(
    existing: Override | None, new: Override, fields: Sequence[str]
) -> Override:
    """以 ``existing`` 为底，只把 ``fields`` 列出的字段从 ``new`` 复制过去。

    覆盖表是整行 upsert：指令 / 中台只提供自己那几个字段，直接写入会把其余字段
    （回退配置、长窗口、重置次数、停用状态）清成默认，故写入前须逐字段合并。
    ``existing`` 为 ``None`` 时以 ``new`` 为底，此时结果就是 ``new`` 本身。
    """
    if existing is None:
        return new
    return replace(existing, **{name: getattr(new, name) for name in fields})


def merge_override(existing: Override | None, new: Override) -> Override:
    """``/quota set`` 的合并：次数 / Token 始终以 new 为准，窗口仅在显式给出时覆盖。"""
    fields = ["call_limit", "token_limit"]
    if new.window_hours is not None:
        fields.append("window_hours")
    return merge_fields(existing, new, fields)


def _add_budget(limit: int, budget: int) -> int:
    """常规上限叠加回退额度得到硬上限；``-1``（不限）或额度 <=0 时保持常规上限。"""
    if limit < 0 or budget <= 0:
        return limit
    return limit + budget


def hard_quota(quota: Quota) -> Quota:
    """把回退额度叠加进上限，得到「硬上限」配额，用于区分回退区间与真正拦截。"""
    return replace(
        quota,
        call_limit=_add_budget(quota.call_limit, quota.fallback_call_limit),
        token_limit=_add_budget(quota.token_limit, quota.fallback_token_limit),
    )


def _evaluate(
    enabled: bool, call_limit: int, token_limit: int, used_calls: int, used_tokens: int
) -> tuple[bool, str | None, int, int]:
    """长短窗口共用的超限判定：调用数硬限、Token 软限、``-1`` 不限、停用则跳过。"""
    if not enabled:
        return (False, None, 0, 0)
    if call_limit >= 0 and used_calls + 1 > call_limit:
        return (True, "calls", used_calls, call_limit)
    if token_limit >= 0 and used_tokens >= token_limit:
        return (True, "tokens", used_tokens, token_limit)
    return (False, None, 0, 0)


def evaluate_scope(
    quota: Quota, used_calls: int, used_tokens: int
) -> tuple[bool, str | None, int, int]:
    """判断「再来一次请求」在**短窗口**上是否会超限。

    Returns:
        ``(blocked, metric, used, limit)``。``metric`` 为 ``"calls"`` 或 ``"tokens"``。

    规则：调用数为硬限（``used + 1 > limit``）；Token 为软限（窗口内累计 ``>= limit``，
    因为请求前无法预知本次消耗）。``-1`` 表示该指标不限，调用数先于 Token 检查。
    """
    return _evaluate(
        quota.enabled, quota.call_limit, quota.token_limit, used_calls, used_tokens
    )


def evaluate_long(
    quota: Quota, long_calls: int, long_tokens: int
) -> tuple[bool, str | None, int, int]:
    """同 ``evaluate_scope``，但针对**长窗口**的上限与用量。"""
    return _evaluate(
        quota.enabled, quota.long_calls, quota.long_tokens, long_calls, long_tokens
    )


def _long_decision(item: ScopeUsage, now: float) -> Decision | None:
    """长窗口判定：命中即拦截（不参与回退），未命中返回 ``None``。"""
    blocked, metric, used, limit = evaluate_long(
        item.quota, item.long_calls, item.long_tokens
    )
    if not blocked:
        return None
    remaining = 0.0 if item.long_end is None else max(0.0, item.long_end - now)
    return Decision(
        blocked=True,
        outcome=OUTCOME_BLOCK,
        scope=item.scope,
        metric=metric,
        used=used,
        limit=limit,
        window_hours=item.quota.window_hours,
        reset_in=remaining,
        window=WINDOW_LONG,
        window_days=item.quota.long_window_days,
    )


def _short_decision(item: ScopeUsage, now: float) -> Decision | None:
    """短窗口判定：返回拦截或回退判定，未超限返回 ``None``。"""
    quota = item.quota
    blocked, metric, used, limit = evaluate_scope(quota, item.calls, item.tokens)
    if not blocked:
        return None
    # 已超常规上限：用「硬上限」配额再判一次，区分回退区间与真正拦截。
    hard_blocked, h_metric, h_used, h_limit = evaluate_scope(
        hard_quota(quota), item.calls, item.tokens
    )
    reset_in = reset_in_seconds(item.oldest, now, quota.window_hours)
    if hard_blocked or not quota.fallback_provider_id:
        return Decision(
            blocked=True,
            outcome=OUTCOME_BLOCK,
            scope=item.scope,
            metric=h_metric if hard_blocked else metric,
            used=h_used if hard_blocked else used,
            limit=h_limit if hard_blocked else limit,
            window_hours=quota.window_hours,
            reset_in=reset_in,
        )
    return Decision(
        blocked=False,
        outcome=OUTCOME_FALLBACK,
        scope=item.scope,
        metric=metric,
        used=used,
        limit=limit,
        window_hours=quota.window_hours,
        reset_in=reset_in,
        fallback_provider_id=quota.fallback_provider_id,
    )


def decide_scopes(items: Sequence[ScopeUsage], now: float) -> Decision:
    """对多个作用域依次判定，返回三态结果（正常 / 回退 / 拦截）。

    用于群聊「全群 + 群内成员」双作用域同时生效。先整体过一遍**长窗口**：任一作用域
    长窗口耗尽即拦截（长窗口不参与回退）。再过**短窗口**：任一作用域达到硬上限即
    拦截；否则若有作用域处于「常规上限 < 用量 <= 硬上限」的回退区间，则返回第一个
    回退判定（携带该作用域的备用供应商）；都未超限则正常。

    调用方须保证：仅当全局开启回退、且某实体备用供应商有效时，传入 quota 的
    ``fallback_*_limit`` 才为正；否则应清零，使硬上限=常规上限，从而超限即拦截。
    """
    enabled = [item for item in items if item.quota.enabled]
    for item in enabled:
        decision = _long_decision(item, now)
        if decision is not None:
            return decision
    fallback: Decision | None = None
    for item in enabled:
        decision = _short_decision(item, now)
        if decision is None:
            continue
        if decision.blocked:
            return decision
        if fallback is None:
            fallback = decision
    if fallback is not None:
        return fallback
    return Decision(blocked=False, outcome=OUTCOME_NORMAL)


def _format_number(value: float) -> str:
    """去掉整数的小数尾巴：``5.0`` → ``"5"``，``1.5`` → ``"1.5"``。"""
    return str(int(value)) if float(value).is_integer() else str(value)


def _window_label(decision: Decision) -> str:
    """``短窗口(5小时)`` / ``长窗口(7天)``。"""
    name = WINDOW_LABELS.get(decision.window, WINDOW_LABELS[WINDOW_SHORT])
    if decision.window == WINDOW_LONG:
        return f"{name}({_format_number(decision.window_days)}天)"
    return f"{name}({_format_number(decision.window_hours)}小时)"


def _message_values(decision: Decision) -> dict[str, object]:
    """拦截提示与自动重置提示共用的模板变量表。"""
    return {
        "scope": decision.scope or "",
        "scope_label": SCOPE_LABELS.get(decision.scope or "", decision.scope or ""),
        "metric": decision.metric or "",
        "metric_label": METRIC_LABELS.get(decision.metric or "", decision.metric or ""),
        "used": decision.used,
        "limit": decision.limit,
        "window_hours": _format_number(decision.window_hours),
        "window_days": _format_number(decision.window_days),
        "window_label": _window_label(decision),
        "reset_in": format_duration(decision.reset_in),
    }


def format_block_message(template: str, decision: Decision) -> str:
    """用判定结果渲染拦截提示模板，遇到未知变量时退回安全文案。"""
    values = _message_values(decision)
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return (
            f"已达使用上限（{values['scope_label']}{values['window_label']}"
            f"{values['metric_label']}："
            f"{decision.used}/{decision.limit}），约 "
            f"{format_duration(decision.reset_in)} 后恢复。"
        )


def format_auto_reset_message(
    template: str, decision: Decision, remaining: int
) -> str:
    """渲染「触顶自动使用一次重置次数」的提示模板。

    变量与 ``format_block_message`` 同一套，另加 ``{remaining}``（重置后该窗口的
    剩余重置次数）。模板含未知变量时退回安全中文文案（含窗口标签与剩余次数）。
    """
    values = _message_values(decision)
    values["remaining"] = remaining
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return (
            f"已达{values['window_label']}上限，已自动为你使用 1 次重置"
            f"（本周期剩余 {remaining} 次），本次请求继续。"
        )
