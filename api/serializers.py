"""数据中台 API 的取值解析与序列化（纯函数，不 import astrbot / quart）。

从 ``api/routes.py`` 抽出，让处理器只留「取参 → 组装 → 信封」的骨架。
序列化统一为「原始字段 + 展示用附加字段」：前端不必自己维护中文标签与
名称映射，也不必猜测哪个 id 该显示成什么（见 docs/design/0.4.0-phase2-spec.md §2）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

try:  # 部署时作为 data.plugins.<name>.api.serializers 加载
    from ..core.cycles import (
        ACTOR_ADMIN,
        ACTOR_AUTO,
        ACTOR_DASHBOARD,
        ACTOR_SELF,
        CycleState,
    )
    from ..core.models import (
        WINDOW_LABELS,
        Override,
        Quota,
    )
    from ..core.requests import STATUS_LABELS, ResetRequest, entity_text
except ImportError:  # pragma: no cover - 插件根目录在 sys.path 时的兜底
    from core.cycles import (
        ACTOR_ADMIN,
        ACTOR_AUTO,
        ACTOR_DASHBOARD,
        ACTOR_SELF,
        CycleState,
    )
    from core.models import (
        WINDOW_LABELS,
        Override,
        Quota,
    )
    from core.requests import STATUS_LABELS, ResetRequest, entity_text

# 重置发起者的中文标签（日志表「发起」列）
ACTOR_LABELS = {
    ACTOR_SELF: "本人",
    ACTOR_ADMIN: "管理员",
    ACTOR_DASHBOARD: "中台",
    ACTOR_AUTO: "自动",
}


# --- 取值解析 -----------------------------------------------------------


def opt_int(value: Any) -> int | None:
    """空值/非整数返回 ``None``（视为「回落默认」）。"""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def opt_str(value: Any) -> str | None:
    """空串视为「回落默认」(None)，否则取去空白后的字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def clamp_limit(value: Any, default: int = 100, lo: int = 1, hi: int = 500) -> int:
    """列表接口的 ``limit``：缺省取 ``default``，并夹在 ``[lo, hi]`` 内。"""
    parsed = opt_int(value)
    if parsed is None:
        return default
    return min(max(parsed, lo), hi)


def positive_int(value: Any, field: str) -> int:
    """必须是正整数，否则抛 ``ValueError``（中文原因，可直接回给前端）。"""
    parsed = opt_int(value)
    if parsed is None or parsed < 1:
        raise ValueError(f"{field} 必须是正整数")
    return parsed


def optional_positive_int(value: Any, field: str) -> int | None:
    """可缺省的正整数：``None`` / 空串表示「按默认」，其余必须是正整数。"""
    if value is None or value == "":
        return None
    return positive_int(value, field)


def non_negative_int(value: Any, field: str, default: int = 0) -> int:
    """缺省为 ``default`` 的非负整数，否则抛 ``ValueError``。"""
    if value is None or value == "":
        return default
    parsed = opt_int(value)
    if parsed is None or parsed < 0:
        raise ValueError(f"{field} 必须是大于等于 0 的整数")
    return parsed


def parse_override_fields(
    data: Mapping[str, Any], provider_exists: Callable[[str], bool]
) -> dict[str, Any]:
    """把请求体里出现的覆盖字段解析成 ``Override`` 关键字参数（缺席的键不出现）。

    非法值抛 ``ValueError``，由调用方转成错误信封。``provider_exists`` 用于校验
    备用模型供应商是否真实存在（由主类注入，避免本模块依赖 astrbot）。
    """
    parsed: dict[str, Any] = {}
    if "enabled" in data:
        parsed["enabled"] = bool(data.get("enabled"))
    if "call_limit" in data:
        parsed["call_limit"] = opt_int(data.get("call_limit"))
    if "token_limit" in data:
        parsed["token_limit"] = opt_int(data.get("token_limit"))
    if "window_hours" in data:
        window_hours = opt_float(data.get("window_hours"))
        if window_hours is not None and window_hours <= 0:
            raise ValueError("window_hours 必须大于 0")
        parsed["window_hours"] = window_hours
    if "fallback_provider_id" in data:
        provider = opt_str(data.get("fallback_provider_id"))
        if provider and not provider_exists(provider):
            raise ValueError(f"未找到备用模型供应商：{provider}")
        parsed["fallback_provider_id"] = provider
    for key in ("fallback_call_limit", "fallback_token_limit"):
        if key in data:
            value = opt_int(data.get(key))
            if value is not None and value < 0:
                raise ValueError("备用模型额外额度不能为负")
            parsed[key] = value
    for key in ("long_calls", "long_tokens"):
        if key in data:
            value = opt_int(data.get(key))
            if value is not None and value < -1:
                raise ValueError("长窗口上限需大于等于 -1（-1 表示不限）")
            parsed[key] = value
    if "long_window_days" in data:
        days = opt_float(data.get("long_window_days"))
        if days is not None and days <= 0:
            raise ValueError("long_window_days 必须大于 0")
        parsed["long_window_days"] = days
    for key in ("self_reset_short", "self_reset_long"):
        if key in data:
            value = opt_int(data.get(key))
            if value is not None and value < 0:
                raise ValueError("自助重置次数不能为负")
            parsed[key] = value
    return parsed


# --- 序列化 -------------------------------------------------------------


def serialize_quota(q: Quota) -> dict:
    return {
        "call_limit": q.call_limit,
        "token_limit": q.token_limit,
        "window_hours": q.window_hours,
        "enabled": q.enabled,
        "fallback_provider_id": q.fallback_provider_id,
        "fallback_call_limit": q.fallback_call_limit,
        "fallback_token_limit": q.fallback_token_limit,
        "long_calls": q.long_calls,
        "long_tokens": q.long_tokens,
        "long_window_days": q.long_window_days,
        "self_reset_short": q.self_reset_short,
        "self_reset_long": q.self_reset_long,
    }


def serialize_override(ov: Override) -> dict:
    return {
        "scope": ov.scope,
        "group_id": ov.group_id,
        "user_id": ov.user_id,
        "call_limit": ov.call_limit,
        "token_limit": ov.token_limit,
        "window_hours": ov.window_hours,
        "enabled": ov.enabled,
        "fallback_provider_id": ov.fallback_provider_id,
        "fallback_call_limit": ov.fallback_call_limit,
        "fallback_token_limit": ov.fallback_token_limit,
        "long_calls": ov.long_calls,
        "long_tokens": ov.long_tokens,
        "long_window_days": ov.long_window_days,
        "self_reset_short": ov.self_reset_short,
        "self_reset_long": ov.self_reset_long,
    }


def serialize_pending(req: ResetRequest | None) -> dict | None:
    """行内的待审批申请摘要（列表页只需要窗口 / 数量 / 理由）。"""
    if req is None:
        return None
    return {
        "id": req.id,
        "window": req.window,
        "amount": req.amount,
        "reason": req.reason,
        "created_at": req.created_at,
    }


def serialize_cycle(
    state: CycleState,
    long_calls: int,
    long_tokens: int,
    long_end: float | None,
    pending: ResetRequest | None = None,
) -> dict:
    """某实体的周期快照：当前周期累计 + 到期时刻 + 剩余重置次数 + 待审批申请。"""
    return {
        "long_calls": long_calls,
        "long_tokens": long_tokens,
        "window_start": state.window_start,
        "long_end": long_end,
        "short_marker": state.short_marker,
        "short_allowance": state.short_allowance,
        "long_allowance": state.long_allowance,
        "pending_request": serialize_pending(pending),
    }


def serialize_request(
    req: ResetRequest,
    group_names: Mapping[str, str],
    group_remarks: Mapping[str, str],
    user_names: Mapping[str, str],
) -> dict:
    """一条申请：原始字段 + 中文标签 + 实体描述 + 群名/备注/昵称。"""
    return {
        "id": req.id,
        "scope": req.scope,
        "group_id": req.group_id,
        "user_id": req.user_id,
        "window": req.window,
        "amount": req.amount,
        "reason": req.reason,
        "status": req.status,
        "created_at": req.created_at,
        "cycle_start": req.cycle_start,
        "requester_origin": req.requester_origin,
        "decided_at": req.decided_at,
        "decided_by": req.decided_by,
        "granted": req.granted,
        "decision_note": req.decision_note,
        "notified": req.notified,
        "window_label": WINDOW_LABELS.get(req.window, req.window),
        "status_label": STATUS_LABELS.get(req.status, req.status),
        "entity_text": entity_text(req),
        "group_name": group_names.get(req.group_id, ""),
        "group_remark": group_remarks.get(req.group_id, ""),
        "user_name": user_names.get(req.user_id, ""),
    }


def serialize_log_entry(
    entry: Any,
    group_names: Mapping[str, str],
    group_remarks: Mapping[str, str],
    user_names: Mapping[str, str],
) -> dict:
    """一条重置日志：原始字段 + 窗口/发起者中文标签 + 群名/备注/昵称。

    ``actor_name`` 是发起者本人的昵称（自助重置时与 ``user_name`` 相同，
    管理员 / 中台重置时是操作者的昵称，未知则为空串）。
    """
    return {
        "id": entry.id,
        "ts": entry.ts,
        "scope": entry.scope,
        "group_id": entry.group_id,
        "user_id": entry.user_id,
        "window": entry.window,
        "actor": entry.actor,
        "actor_id": entry.actor_id,
        "consumed": entry.consumed,
        "window_label": WINDOW_LABELS.get(entry.window, entry.window),
        "actor_label": ACTOR_LABELS.get(entry.actor, entry.actor),
        "entity_text": entity_text(entry),
        "group_name": group_names.get(entry.group_id, ""),
        "group_remark": group_remarks.get(entry.group_id, ""),
        "user_name": user_names.get(entry.user_id, ""),
        "actor_name": user_names.get(entry.actor_id, ""),
    }
