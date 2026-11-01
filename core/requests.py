"""追加重置次数的申请（ResetRequest）状态机。

数据类骨架由集成方预先定义；校验与转移函数见本模块下方（由实现者补充）。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import (
    SCOPE_GROUP,
    SCOPE_MEMBER,
    SCOPE_PRIVATE,
    VALID_WINDOWS,
    WINDOW_LABELS,
)

REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_DENIED = "denied"
REQUEST_VOID = "void"
REQUEST_STATUSES = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DENIED, REQUEST_VOID)


@dataclass(frozen=True)
class ResetRequest:
    """一条申请。``id`` 由数据库自增分配，插入前为 ``0``。

    - ``cycle_start``：提出申请时实体的开窗时刻；周期结束后申请作废。
    - ``requester_origin``：申请者会话的 ``unified_msg_origin``，用于主动通知。
    - ``granted``：批准时实际发放的次数（拒绝 / 作废为 0）。
    - ``notified``：审批结果是否已送达申请者（主动推送成功或已在 /quota me 展示）。
    """

    scope: str
    group_id: str
    user_id: str
    window: str
    amount: int
    reason: str = ""
    status: str = REQUEST_PENDING
    created_at: float = 0.0
    cycle_start: float = 0.0
    requester_origin: str = ""
    decided_at: float | None = None
    decided_by: str = ""
    granted: int = 0
    decision_note: str = ""
    notified: bool = False
    id: int = 0


# 申请状态的中文标签（用于列表与结果通知）
STATUS_LABELS = {
    REQUEST_PENDING: "待审批",
    REQUEST_APPROVED: "已批准",
    REQUEST_DENIED: "已拒绝",
    REQUEST_VOID: "已作废",
}

# 申请列表面向管理员，作用域用中性说法（models 里的标签是面向当事人的）
SCOPE_TEXTS = {
    SCOPE_PRIVATE: "私聊",
    SCOPE_GROUP: "群",
    SCOPE_MEMBER: "群成员",
}


def validate_new_request(
    window: str,
    amount: int,
    *,
    allow: bool,
    max_count: int,
    cycle_active: bool,
    has_pending: bool,
) -> str | None:
    """校验一条新申请，合法返回 ``None``，否则返回可直接回复用户的中文原因。

    顺序固定：是否开放 → 窗口是否合法 → 是否有进行中的周期 → 是否已有待审批 → 数量区间。
    """
    if not allow:
        return "管理员未开放重置次数申请"
    if window not in VALID_WINDOWS:
        return "窗口只能是 short 或 long"
    if not cycle_active:
        return "当前没有进行中的周期，先正常使用后再申请"
    if has_pending:
        return "你已有一条待审批的申请，请等待管理员处理"
    if amount < 1 or amount > max_count:
        return f"申请数量需在 1 ~ {max_count} 之间"
    return None


def new_request(
    scope: str,
    group_id: str,
    user_id: str,
    window: str,
    amount: int,
    reason: str,
    now: float,
    cycle_start: float,
    requester_origin: str = "",
) -> ResetRequest:
    """构造一条待审批申请（``id`` 由数据库插入时分配）。

    ``cycle_start`` 记录提出时实体的开窗时刻，用于之后判断申请是否已随周期作废。
    """
    return ResetRequest(
        scope=scope,
        group_id=group_id,
        user_id=user_id,
        window=window,
        amount=amount,
        reason=reason,
        status=REQUEST_PENDING,
        created_at=now,
        cycle_start=cycle_start,
        requester_origin=requester_origin,
    )


def _require_pending(req: ResetRequest, action: str) -> None:
    """非待审批状态不允许再做审批动作。"""
    if req.status != REQUEST_PENDING:
        label = STATUS_LABELS.get(req.status, req.status)
        raise ValueError(f"申请 #{req.id} 状态为「{label}」，无法{action}")


def approve(
    req: ResetRequest, now: float, admin_id: str, amount: int | None = None
) -> ResetRequest:
    """批准：``amount`` 缺省为申请数量；管理员不受 ``request_max_count`` 限制但必须 ≥ 1。"""
    _require_pending(req, "批准")
    granted = req.amount if amount is None else amount
    if granted < 1:
        raise ValueError("发放数量必须大于等于 1")
    return replace(
        req,
        status=REQUEST_APPROVED,
        decided_at=now,
        decided_by=admin_id,
        granted=granted,
        notified=False,
    )


def deny(req: ResetRequest, now: float, admin_id: str, note: str = "") -> ResetRequest:
    """拒绝：``note`` 为可选的拒绝理由，会展示给申请者。"""
    _require_pending(req, "拒绝")
    return replace(
        req,
        status=REQUEST_DENIED,
        decided_at=now,
        decided_by=admin_id,
        granted=0,
        decision_note=note,
        notified=False,
    )


def void_if_stale(req: ResetRequest, current_cycle_start: float | None) -> ResetRequest:
    """惰性作废：待审批申请所属周期已结束（到期或被重置关闭）时置为作废。

    已决（批准 / 拒绝 / 作废）的申请原样返回。
    """
    if req.status != REQUEST_PENDING:
        return req
    if current_cycle_start == req.cycle_start:
        return req
    return replace(req, status=REQUEST_VOID, granted=0)


def mark_notified(req: ResetRequest) -> ResetRequest:
    """标记审批结果已送达申请者（主动推送成功或已在 /quota me 展示过）。"""
    return replace(req, notified=True)


def entity_text(req: ResetRequest) -> str:
    """申请实体的可读描述，如 ``私聊(u1)`` / ``群成员(g1/u2)``。"""
    scope_text = SCOPE_TEXTS.get(req.scope, req.scope)
    if req.scope == SCOPE_GROUP:
        target = req.group_id
    elif req.scope == SCOPE_PRIVATE:
        target = req.user_id
    else:
        target = f"{req.group_id}/{req.user_id}"
    return f"{scope_text}({target})"


def _window_text(req: ResetRequest) -> str:
    return WINDOW_LABELS.get(req.window, req.window)


def describe(req: ResetRequest) -> str:
    """单行摘要：``#id 实体 窗口 数量 状态 [理由]``，供 /quota requests 与管理员通知。"""
    parts = [
        f"#{req.id}",
        entity_text(req),
        _window_text(req),
        f"{req.amount}次",
        STATUS_LABELS.get(req.status, req.status),
    ]
    if req.status == REQUEST_APPROVED and req.granted != req.amount:
        parts.append(f"实发{req.granted}次")
    if req.reason:
        parts.append(f"理由：{req.reason}")
    return " ".join(parts)


def decision_text(req: ResetRequest) -> str:
    """面向申请者的一句话结果。"""
    head = f"重置次数申请 #{req.id}（{_window_text(req)} {req.amount}次）"
    if req.status == REQUEST_APPROVED:
        return f"{head}已批准，已发放 {req.granted} 次。"
    if req.status == REQUEST_DENIED:
        tail = f"，理由：{req.decision_note}" if req.decision_note else "。"
        return f"{head}已被拒绝{tail}"
    if req.status == REQUEST_VOID:
        return f"{head}已作废：所在周期已结束。"
    return f"{head}待审批，请等待管理员处理。"
