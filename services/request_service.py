"""重置申请的审批与发放编排（指令与数据中台共用同一份业务逻辑）。

``core.requests`` 只做纯状态转移，本模块负责把一次审批落到三处：
1. ``reset_requests`` 表（状态转移后整行更新）；
2. ``CycleService``（批准后把次数发放到申请实体的当前周期）；
3. 惰性作废检查（审批前先推进到期，避免把次数发到已结束的周期上）。

不 import astrbot：可用真实 ``Storage`` + ``Aggregator`` + ``CycleService`` 直接测试。
业务失败一律抛 ``ValueError(中文原因)``，由调用方转成用户可见提示
（指令层 ``⚠️ {exc}``、API 层 ``_err(str(exc))``）；申请者通知留在 ``main.py``
（依赖 astrbot 的 ``context.send_message``）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:  # 部署时作为 data.plugins.<name>.services.request_service 加载
    from ..core import cycles, requests
    from ..core.models import WINDOW_SHORT, ScopeKey
except ImportError:  # pragma: no cover - 插件根目录在 sys.path 时的兜底
    from core import cycles, requests
    from core.models import WINDOW_SHORT, ScopeKey


# 单次发放 / 批准的次数上限：防止误输入把重置次数发成天文数字。
MAX_GRANT = 1000


def _check_grant_amount(value: int, label: str) -> None:
    if value > MAX_GRANT:
        raise ValueError(f"{label}单次最多 {MAX_GRANT} 次")


class RequestService:
    """申请审批 / 次数发放的编排。

    Args:
        storage: ``core.storage.Storage``（须已 ``initialize``）。
        service: ``services.cycle_service.CycleService``。
        now: 取当前时间戳的可调用对象（便于测试注入假时钟）。
        logger: 具备 ``warning`` / ``error`` 的日志对象。
    """

    def __init__(
        self,
        storage: Any,
        service: Any,
        now: Callable[[], float],
        logger: Any,
    ) -> None:
        self._storage = storage
        self._service = service
        self._now = now
        self._logger = logger

    # --- 读取 -----------------------------------------------------------

    async def load(self, request_id: int) -> requests.ResetRequest:
        """按 ID 取申请并做惰性作废检查；任何失败抛 ``ValueError``（中文原因）。"""
        try:
            req = await self._storage.get_request(request_id)
        except Exception as exc:  # noqa: BLE001 - 转成用户可见提示，细节进日志
            self._logger.error("quota_hub 读取申请 #%s 失败: %s", request_id, exc)
            raise ValueError("读取申请失败，详情见服务端日志") from exc
        if req is None:
            raise ValueError(f"未找到申请 #{request_id}")
        fresh = await self._service.refresh_request(req)
        if fresh is None:
            raise ValueError(f"申请 #{request_id} 所在周期已结束，已自动作废")
        return fresh

    # --- 审批 -----------------------------------------------------------

    async def approve(
        self, request_id: int, amount: int | None, admin_id: str
    ) -> requests.ResetRequest:
        """批准申请并把次数发放到申请实体的当前周期，返回已决的申请。

        ``amount`` 为 ``None`` 时取申请数量；管理员不受 ``request_max_count``
        限制，但必须 ≥ 1（由 ``core.requests.approve`` 校验）。
        """
        req = await self.load(request_id)
        decided = requests.approve(req, self._now(), admin_id, amount)
        _check_grant_amount(decided.granted, "批准数量")
        await self._save(decided)
        key = ScopeKey(decided.scope, decided.group_id, decided.user_id)
        short = decided.granted if decided.window == WINDOW_SHORT else 0
        await self._service.grant(key, short, decided.granted - short)
        return decided

    async def deny(
        self, request_id: int, note: str, admin_id: str
    ) -> requests.ResetRequest:
        """拒绝申请，``note`` 为可选理由（会展示给申请者）。"""
        req = await self.load(request_id)
        decided = requests.deny(req, self._now(), admin_id, note)
        await self._save(decided)
        return decided

    # --- 发放 -----------------------------------------------------------

    async def grant_to(self, key: ScopeKey, short: int, long: int) -> cycles.CycleState:
        """为实体的当前周期发放重置次数；没有进行中的周期时拒绝。

        开窗不补满、自然到期才补满：没有周期时发放的次数会在下次到期补满时被
        抹掉，等于白发，故直接拒绝（见 docs/design/0.4.0-phase1-spec.md §1.3）。
        """
        _check_grant_amount(short, "短窗口发放")
        _check_grant_amount(long, "长窗口发放")
        now = self._now()
        await self._service.touch(key, now)
        if not self._service.is_active(key, now):
            raise ValueError("该实体当前没有进行中的周期，无法发放")
        return await self._service.grant(key, short, long)

    # --- 内部 -----------------------------------------------------------

    async def _save(self, req: requests.ResetRequest) -> None:
        """落盘审批结果；失败必须让调用方知道（否则次数发了、状态还留在待审批）。"""
        try:
            await self._storage.update_request(req)
        except Exception as exc:  # noqa: BLE001
            self._logger.error("quota_hub 更新申请 #%s 失败: %s", req.id, exc)
            raise ValueError("保存审批结果失败，详情见服务端日志") from exc
