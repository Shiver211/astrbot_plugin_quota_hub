"""重置次数申请（ResetRequest）状态机的单元测试。"""

from __future__ import annotations

import dataclasses

import pytest
from core import requests
from core.models import SCOPE_MEMBER, SCOPE_PRIVATE, WINDOW_LONG, WINDOW_SHORT
from core.requests import (
    REQUEST_APPROVED,
    REQUEST_DENIED,
    REQUEST_PENDING,
    REQUEST_VOID,
    ResetRequest,
)

T0 = 1_000_000.0


def _pending(**kwargs) -> ResetRequest:
    req = requests.new_request(
        SCOPE_MEMBER,
        "g1",
        "u2",
        WINDOW_LONG,
        2,
        "跑批需要",
        T0,
        T0 - 100,
        "aiocqhttp:GroupMessage:g1",
    )
    return dataclasses.replace(req, id=7, **kwargs)


# --- validate_new_request ------------------------------------------------

OK = {"allow": True, "max_count": 3, "cycle_active": True, "has_pending": False}


def test_validate_accepts_valid_request():
    assert requests.validate_new_request(WINDOW_SHORT, 1, **OK) is None
    assert requests.validate_new_request(WINDOW_LONG, 3, **OK) is None


def test_validate_disallowed_checked_first():
    """未开放优先于其余所有问题。"""
    reason = requests.validate_new_request(
        "bogus", 99, allow=False, max_count=3, cycle_active=False, has_pending=True
    )
    assert reason is not None
    assert "开放" in reason


def test_validate_rejects_unknown_window_before_cycle_check():
    reason = requests.validate_new_request(
        "weekly", 1, allow=True, max_count=3, cycle_active=False, has_pending=True
    )
    assert reason is not None
    assert "short" in reason and "long" in reason


def test_validate_requires_active_cycle_before_pending_check():
    reason = requests.validate_new_request(
        WINDOW_SHORT, 1, allow=True, max_count=3, cycle_active=False, has_pending=True
    )
    assert reason is not None
    assert "周期" in reason


def test_validate_rejects_duplicate_pending_before_amount_check():
    reason = requests.validate_new_request(
        WINDOW_SHORT, 99, allow=True, max_count=3, cycle_active=True, has_pending=True
    )
    assert reason is not None
    assert "待审批" in reason


def test_validate_amount_boundaries():
    assert requests.validate_new_request(WINDOW_SHORT, 1, **OK) is None  # 下界
    assert requests.validate_new_request(WINDOW_SHORT, 3, **OK) is None  # 上界
    assert requests.validate_new_request(WINDOW_SHORT, 0, **OK) is not None
    assert requests.validate_new_request(WINDOW_SHORT, -1, **OK) is not None
    assert requests.validate_new_request(WINDOW_SHORT, 4, **OK) is not None


def test_validate_amount_message_mentions_range():
    reason = requests.validate_new_request(WINDOW_SHORT, 4, **OK)
    assert reason is not None
    assert "1" in reason and "3" in reason


# --- new_request ---------------------------------------------------------


def test_new_request_starts_pending_with_cycle_start():
    req = requests.new_request(
        SCOPE_PRIVATE, "", "u1", WINDOW_SHORT, 2, "理由", T0, T0 - 50, "origin"
    )
    assert req.status == REQUEST_PENDING
    assert (req.scope, req.group_id, req.user_id) == (SCOPE_PRIVATE, "", "u1")
    assert (req.window, req.amount, req.reason) == (WINDOW_SHORT, 2, "理由")
    assert req.created_at == T0
    assert req.cycle_start == T0 - 50
    assert req.requester_origin == "origin"
    assert req.decided_at is None
    assert req.granted == 0
    assert req.notified is False
    assert req.id == 0  # id 由数据库分配


def test_new_request_defaults_requester_origin():
    req = requests.new_request(SCOPE_PRIVATE, "", "u1", WINDOW_SHORT, 1, "", T0, T0)
    assert req.requester_origin == ""


# --- approve -------------------------------------------------------------


def test_approve_defaults_to_requested_amount():
    req = requests.approve(_pending(), T0 + 10, "admin1")
    assert req.status == REQUEST_APPROVED
    assert req.granted == 2
    assert req.decided_at == T0 + 10
    assert req.decided_by == "admin1"
    assert req.notified is False


def test_approve_with_explicit_amount_can_exceed_request_max():
    req = requests.approve(_pending(), T0 + 10, "admin1", amount=9)
    assert req.granted == 9
    assert req.amount == 2  # 原申请数量保留


def test_approve_does_not_mutate_original():
    original = _pending()
    requests.approve(original, T0 + 10, "admin1")
    assert original.status == REQUEST_PENDING
    assert original.granted == 0


@pytest.mark.parametrize("status", [REQUEST_APPROVED, REQUEST_DENIED, REQUEST_VOID])
def test_approve_non_pending_raises(status):
    with pytest.raises(ValueError):
        requests.approve(_pending(status=status), T0, "admin1")


@pytest.mark.parametrize("amount", [0, -3])
def test_approve_amount_below_one_raises(amount):
    with pytest.raises(ValueError):
        requests.approve(_pending(), T0, "admin1", amount=amount)


# --- deny ----------------------------------------------------------------


def test_deny_records_note_and_zero_grant():
    req = requests.deny(_pending(), T0 + 5, "admin2", "额度紧张")
    assert req.status == REQUEST_DENIED
    assert req.granted == 0
    assert req.decision_note == "额度紧张"
    assert req.decided_at == T0 + 5
    assert req.decided_by == "admin2"


def test_deny_note_is_optional():
    assert requests.deny(_pending(), T0, "admin2").decision_note == ""


@pytest.mark.parametrize("status", [REQUEST_APPROVED, REQUEST_DENIED, REQUEST_VOID])
def test_deny_non_pending_raises(status):
    with pytest.raises(ValueError):
        requests.deny(_pending(status=status), T0, "admin2")


# --- void_if_stale -------------------------------------------------------


def test_void_if_stale_keeps_request_within_same_cycle():
    req = _pending()
    assert requests.void_if_stale(req, req.cycle_start) is req


def test_void_if_stale_voids_when_cycle_reopened():
    req = _pending()
    voided = requests.void_if_stale(req, T0 + 999)
    assert voided.status == REQUEST_VOID
    assert voided.granted == 0
    assert req.status == REQUEST_PENDING  # 原对象不变


def test_void_if_stale_voids_when_cycle_closed():
    """长窗口重置关掉周期后 window_start 为 None，待审批申请作废。"""
    voided = requests.void_if_stale(_pending(), None)
    assert voided.status == REQUEST_VOID


@pytest.mark.parametrize("status", [REQUEST_APPROVED, REQUEST_DENIED, REQUEST_VOID])
def test_void_if_stale_leaves_decided_requests_untouched(status):
    req = _pending(status=status, granted=2)
    assert requests.void_if_stale(req, T0 + 999) is req


# --- mark_notified -------------------------------------------------------


def test_mark_notified_sets_flag_without_mutating():
    req = _pending(status=REQUEST_APPROVED, granted=2)
    marked = requests.mark_notified(req)
    assert marked.notified is True
    assert req.notified is False


# --- describe / decision_text -------------------------------------------


def test_describe_contains_id_window_amount_and_entity():
    text = requests.describe(_pending())
    assert text.startswith("#7 ")
    assert "长窗口" in text
    assert "2" in text
    assert "g1" in text and "u2" in text
    assert "待审批" in text
    assert "跑批需要" in text
    assert "\n" not in text  # 单行


def test_describe_private_scope_shows_user_only():
    req = requests.new_request(SCOPE_PRIVATE, "", "u1", WINDOW_SHORT, 1, "", T0, T0)
    text = requests.describe(req)
    assert "短窗口" in text
    assert "u1" in text
    assert "理由" not in text  # 无理由时不带理由段


@pytest.mark.parametrize(
    "status,keyword",
    [
        (REQUEST_APPROVED, "批准"),
        (REQUEST_DENIED, "拒绝"),
        (REQUEST_VOID, "作废"),
        (REQUEST_PENDING, "待审批"),
    ],
)
def test_describe_shows_status_label(status, keyword):
    assert keyword in requests.describe(_pending(status=status))


def test_decision_text_approved_shows_granted_amount():
    req = requests.approve(_pending(), T0, "admin1", amount=5)
    text = requests.decision_text(req)
    assert "#7" in text
    assert "长窗口" in text
    assert "5" in text
    assert "批准" in text or "通过" in text


def test_decision_text_denied_includes_reason():
    req = requests.deny(_pending(), T0, "admin1", "额度紧张")
    text = requests.decision_text(req)
    assert "#7" in text
    assert "拒绝" in text
    assert "额度紧张" in text


def test_decision_text_denied_without_note():
    req = requests.deny(_pending(), T0, "admin1")
    assert "拒绝" in requests.decision_text(req)


def test_decision_text_void_explains_cycle_ended():
    text = requests.decision_text(requests.void_if_stale(_pending(), None))
    assert "作废" in text
    assert "周期" in text


def test_decision_text_pending_is_still_readable():
    assert "待审批" in requests.decision_text(_pending())


def test_describe_group_scope_shows_group_id():
    """群共享池的重置只有管理员能做，但描述函数仍需覆盖该作用域。"""
    from core.models import SCOPE_GROUP

    req = dataclasses.replace(_pending(), scope=SCOPE_GROUP, group_id="g9", user_id="")
    text = requests.describe(req)
    assert "g9" in text
    assert "群" in text
