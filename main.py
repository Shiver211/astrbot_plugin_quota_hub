"""配额中枢 —— AstrBot 插件入口。

对个人私聊 / 整个群共享 / 群内每个成员三种作用域，按滚动时间窗口做调用次数与
Token 双重限额。限流判定走内存聚合器（零数据库访问），用量异步批量落盘到 SQLite，
内置数据中台可视化与动态调参。
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter

try:
    from astrbot.api.event.filter import EventMessageType, PermissionType
except ImportError:
    from astrbot.api.event.filter import PermissionType  # type: ignore[no-redef]

    EventMessageType = getattr(filter, "EventMessageType", None)  # type: ignore[assignment]
    if EventMessageType is None:
        class EventMessageType:  # type: ignore[no-redef]
            ALL = "ALL"
            GROUP_MESSAGE = "GROUP_MESSAGE"
            PRIVATE_MESSAGE = "PRIVATE_MESSAGE"
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult

try:
    from astrbot.api.star import Context, Star, StarTools
except ImportError:  # pragma: no cover - 兼容旧版本 AstrBot
    from astrbot.api.star import Context, Star

    StarTools = None  # type: ignore[assignment]

try:  # 部署时作为 data.plugins.<name>.main 加载
    from .api.routes import QuotaApiMixin
    from .core.aggregator import Aggregator
    from .core.cycles import (
        ACTOR_ADMIN,
        ACTOR_AUTO,
        ACTOR_SELF,
        REFUSAL_MESSAGES,
        allowance,
        check_self_reset,
    )
    from .core.models import (
        DEFAULT_LONG_WINDOW_DAYS,
        DEFAULT_SELF_RESET_LONG,
        DEFAULT_SELF_RESET_SHORT,
        OUTCOME_BLOCK,
        OUTCOME_FALLBACK,
        SCOPE_GROUP,
        SCOPE_MEMBER,
        SCOPE_MEMBER_DEFAULT,
        SCOPE_PRIVATE,
        SCOPE_USER_DEFAULT,
        VALID_WINDOWS,
        WINDOW_LABELS,
        WINDOW_SHORT,
        BanRecord,
        Decision,
        Override,
        Quota,
        ScopeKey,
        normalize_id,
        scopes_for,
    )
    from .core.quota import (
        ScopeUsage,
        decide_scopes,
        format_auto_reset_message,
        format_block_message,
        merge_fields,
        merge_override,
        resolve_quota,
    )
    from .core.requests import (
        REQUEST_PENDING,
        decision_text,
        describe,
        mark_notified,
        new_request,
        validate_new_request,
    )
    from .core.storage import Storage
    from .core.windows import (
        SECONDS_PER_DAY,
        format_duration,
        parse_duration,
        reset_in_seconds,
        window_cutoff,
    )
    from .core.writer import UsageWriter
    from .services.cycle_service import CycleService
    from .services.request_service import RequestService
except ImportError:  # pragma: no cover - 插件根目录在 sys.path 时的兜底
    from api.routes import QuotaApiMixin
    from core.aggregator import Aggregator
    from core.cycles import (
        ACTOR_ADMIN,
        ACTOR_AUTO,
        ACTOR_SELF,
        REFUSAL_MESSAGES,
        allowance,
        check_self_reset,
    )
    from core.models import (
        DEFAULT_LONG_WINDOW_DAYS,
        DEFAULT_SELF_RESET_LONG,
        DEFAULT_SELF_RESET_SHORT,
        OUTCOME_BLOCK,
        OUTCOME_FALLBACK,
        SCOPE_GROUP,
        SCOPE_MEMBER,
        SCOPE_MEMBER_DEFAULT,
        SCOPE_PRIVATE,
        SCOPE_USER_DEFAULT,
        VALID_WINDOWS,
        WINDOW_LABELS,
        WINDOW_SHORT,
        BanRecord,
        Decision,
        Override,
        Quota,
        ScopeKey,
        normalize_id,
        scopes_for,
    )
    from core.quota import (
        ScopeUsage,
        decide_scopes,
        format_auto_reset_message,
        format_block_message,
        merge_fields,
        merge_override,
        resolve_quota,
    )
    from core.requests import (
        REQUEST_PENDING,
        decision_text,
        describe,
        mark_notified,
        new_request,
        validate_new_request,
    )
    from core.storage import Storage
    from core.windows import (
        SECONDS_PER_DAY,
        format_duration,
        parse_duration,
        reset_in_seconds,
        window_cutoff,
    )
    from core.writer import UsageWriter
    from services.cycle_service import CycleService
    from services.request_service import RequestService

PLUGIN_NAME = "astrbot_plugin_quota_hub"
ADMIN_ONLY_TIP = "⚠️ 该指令仅管理员可用"
MAINTENANCE_INTERVAL = 300.0  # 后台扫描/清理周期（秒）
GROUP_NAME_REFRESH_INTERVAL = 600.0  # 经协议刷新群名的最小间隔（秒）
MEMBER_NAME_TTL = 300.0  # 群成员昵称缓存有效期（秒）
PROTOCOL_TIMEOUT = 10.0  # 协议调用超时（秒）

# 触顶自动重置的默认提示文案（与 _conf_schema.json 的默认值保持一致）
DEFAULT_AUTO_RESET_MESSAGE = (
    "♻️ 已达{window_label}上限，自动为你使用 1 次重置"
    "（本周期剩余 {remaining} 次），本次请求继续。"
)
# 拦截提示默认文案（与 _conf_schema.json 的默认值保持一致）
DEFAULT_BLOCK_MESSAGE = (
    "⚠️ 已达使用上限：{scope_label} {window_label} {metric_label}：{used}/{limit}，"
    "约 {reset_in} 后恢复。如需提高额度请联系管理员。"
)
# 自动重置后仍被其它限额拦截时的提示：次数确实扣了，但不能声称「本次请求继续」，
# 故不套用户模板（模板默认文案带继续语），改用这句固定文案 + 随后的拦截提示。
AUTO_RESET_PARTIAL_MESSAGE = (
    "♻️ 已自动为你使用 1 次{window_label}重置（本周期剩余 {remaining} 次），"
    "但本次请求仍超出其它限额。"
)

# 超限措施：直接拦截 / 回退到备用模型继续。
OVER_LIMIT_BLOCK = "block"
OVER_LIMIT_FALLBACK = "fallback"
VALID_OVER_LIMIT_ACTIONS = (OVER_LIMIT_BLOCK, OVER_LIMIT_FALLBACK)


def _on_waiting_llm_request(priority: int):
    """``on_waiting_llm_request`` 钩子装饰器；旧版 AstrBot 缺失时退化为普通方法。

    回退切换依赖该钩子（须在供应商选择前写入 ``selected_provider``）。缺失时不注册
    钩子、不在类定义期抛 ``AttributeError`` 拖垮整个插件加载，仅令回退切换失效。
    """
    deco = getattr(filter, "on_waiting_llm_request", None)
    if deco is None:
        return lambda fn: fn
    return deco(priority=priority)


def _event_message_type(msg_type: Any, priority: int = 0):
    """``event_message_type`` 装饰器；旧版 AstrBot 缺失时退化为普通方法。"""
    deco = getattr(filter, "event_message_type", None)
    if deco is None:
        return lambda fn: fn
    return deco(msg_type, priority=priority)

HELP_TEXT = (
    "📊 配额中枢指令：\n"
    "/quota me —— 查询自己的用量、剩余重置次数与申请状态\n"
    "/quota reset me [long] —— 自助重置自己的短 / 长窗口（消耗重置次数）\n"
    "/quota request short|long [数量] [理由] —— 申请追加重置次数\n"
    "（以下需管理员）\n"
    "/ban <QQ号或@成员> <时间（如1d）> [理由] —— 禁止成员使用指令、唤醒与 LLM 对话\n"
    "/unban <QQ号或@成员> —— 解除成员的封禁\n"
    "/ban list —— 查看当前处于封禁中的成员列表\n"
    "/quota set user|group|member|members|userdef <...> <次数> <Token> [小时]\n"
    "  members=某群全体成员默认，userdef=该用户在所有群的成员默认\n"
    "/quota setlong user|group|member|members|userdef <...> <次数> <Token> [天数]\n"
    "  只改长窗口字段；次数/Token 填 -1 表示不限\n"
    "/quota setreset user|member|members|userdef <...> <短窗次数> <长窗次数>\n"
    "/quota clear user|group|member|members|userdef <...> —— 删除覆盖\n"
    "/quota reset user|group|member <...> [long] —— 重置某实体（不消耗次数）\n"
    "  （重置只是记标记 / 关周期，不删除用量记录，历史统计不受影响）\n"
    "/quota grant user <用户ID> <短> <长> —— 为私聊实体发放重置次数\n"
    "/quota grant member <群号> <用户ID> <短> <长>\n"
    "/quota requests —— 列出待审批申请\n"
    "/quota approve <id> [数量] —— 批准申请\n"
    "/quota deny <id> [理由] —— 拒绝申请\n"
    "/quota session —— 打印当前会话标识（填入「申请通知会话列表」）\n"
    "/quota remark <群号> [备注] —— 设置/清除群备注\n"
    "/quota monitor add|del group|user <ID> —— 维护监控名单\n"
    "/quota exempt add|del group|user <ID> —— 维护白名单\n"
    "/quota stats [天数] —— 用量概览；带天数（1-365）时查看历史统计"
)


def _load_schema() -> dict[str, Any]:
    schema_path = Path(__file__).parent / "_conf_schema.json"
    try:
        return json.loads(schema_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("quota_hub 读取 _conf_schema.json 失败: %s", exc)
        return {}


class QuotaHub(QuotaApiMixin, Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self._schema = _load_schema()

        data_dir = (
            StarTools.get_data_dir(PLUGIN_NAME)
            if StarTools is not None
            else Path(__file__).parent / "data"
        )
        self.storage = Storage(Path(data_dir) / "quota.db")
        # 用纳秒时戳作 eid 起点，保证 eid 跨重启全局唯一（见 Aggregator 注释）
        self.aggregator = Aggregator(eid_base=time.time_ns())
        self.writer = UsageWriter(self.storage, logger=logger)
        # 周期 / 重置标记 / 重置次数的编排（缓存 + 落盘 + 聚合器同步）
        self.service = CycleService(
            self.storage, self.aggregator, self._now, self._resolve_quota, logger
        )
        # 申请审批 / 次数发放的编排（指令与中台共用；通知留在本类，依赖 context）
        self.request_service = RequestService(
            self.storage, self.service, self._now, logger
        )
        self._overrides: dict[ScopeKey, Override] = {}
        # 解析后配额的记忆化缓存：热路径每次判定要解析 2~3 个实体的多层覆盖，
        # 结果只随「覆盖」与「配置」变化，故在这两处变更时整体清空。
        self._quota_cache: dict[ScopeKey, Quota] = {}
        self._maintenance_task: asyncio.Task[None] | None = None
        # 名称/备注缓存（热路径只读写内存；名称定期落盘，备注写入即落盘）
        self._group_names: dict[str, str] = {}
        self._user_names: dict[str, str] = {}
        self._group_remarks: dict[str, str] = {}
        self._dirty_names: set[tuple[str, str]] = set()
        self._banned_users: dict[tuple[str, str], BanRecord] = {}
        self._group_names_refreshed_at = 0.0
        self._member_name_cache: dict[str, tuple[float, dict[str, str]]] = {}
        # 会话 → 上次回退提示时戳，用于「同一会话一个窗口内只提示一次」
        self._fallback_notified: dict[str, float] = {}

        self._register_web_apis(PLUGIN_NAME)

    # --- 生命周期 -------------------------------------------------------

    async def initialize(self) -> None:
        await self.storage.initialize()
        self._overrides = {ov.key: ov for ov in await self.storage.list_overrides()}
        self._invalidate_quota_cache()
        await self._load_bans()
        self._load_meta_cache(await self.storage.load_meta())
        # 顺序不可颠倒：先恢复周期状态与重置标记，再回填用量事件，
        # 否则标记之前的记录会被重新加回短窗口（见 docs/adr/0001）。
        await self.service.restore()
        cutoff = window_cutoff(self._now(), self._max_window_hours())
        self.aggregator.add_events(await self.storage.load_recent(cutoff))
        await self.writer.start()
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())
        logger.info(
            "quota_hub 已就绪：恢复 %d 个桶、%d 条覆盖、%d 个周期状态。",
            self.aggregator.bucket_count(),
            len(self._overrides),
            len(self.service.known_keys()),
        )

    async def _load_bans(self) -> None:
        try:
            now = int(self._now())
            rows = await self.storage.list_bans(now)
            self._banned_users = {
                (gid, uid): BanRecord(gid, uid, bat, exp, by, rsn)
                for gid, uid, bat, exp, by, rsn in rows
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 加载封禁列表失败: %s", exc)

    def is_banned(
        self, group_id: Any, user_id: Any, now: float | None = None
    ) -> BanRecord | None:
        """检查用户是否处于封禁状态。先查群成员封禁，再查全局封禁。"""
        user_id = normalize_id(user_id)
        group_id = normalize_id(group_id)
        if not user_id:
            return None
        if now is None:
            now = self._now()
        # 1. 检查当前群成员封禁
        if group_id:
            ban = self._banned_users.get((group_id, user_id))
            if ban is not None:
                if ban.expire_at < 0 or ban.expire_at > now:
                    return ban
                self._banned_users.pop((group_id, user_id), None)
                asyncio.create_task(self._safe_remove_ban(group_id, user_id))
        # 2. 检查全局封禁
        ban = self._banned_users.get(("", user_id))
        if ban is not None:
            if ban.expire_at < 0 or ban.expire_at > now:
                return ban
            self._banned_users.pop(("", user_id), None)
            asyncio.create_task(self._safe_remove_ban("", user_id))
        return None

    async def _safe_remove_ban(self, group_id: str, user_id: str) -> None:
        try:
            await self.storage.remove_ban(group_id, user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 移除过期封禁失败: %s", exc)

    @staticmethod
    def _fmt_time_full(ts: float) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))

    def _load_meta_cache(self, rows: list[tuple[str, str, str, str]]) -> None:
        for kind, entity_id, name, remark in rows:
            if kind == "group":
                if name:
                    self._group_names[entity_id] = name
                if remark:
                    self._group_remarks[entity_id] = remark
            elif kind == "user" and name:
                self._user_names[entity_id] = name

    async def _persist_names(self) -> None:
        """把脏掉的名称批量落盘（在维护周期与关闭时调用，不在热路径）。"""
        if not self._dirty_names:
            return
        dirty = self._dirty_names
        self._dirty_names = set()
        groups = [(eid, self._group_names.get(eid, "")) for k, eid in dirty if k == "group"]
        users = [(eid, self._user_names.get(eid, "")) for k, eid in dirty if k == "user"]
        # 两类独立落盘：失败时只把失败那一类重新标脏，避免重复落盘已成功的条目
        try:
            await self.storage.upsert_names("group", groups)
        except Exception as exc:  # noqa: BLE001 - 名称落盘失败仅影响重启后显示
            logger.warning("quota_hub 群名落盘失败: %s", exc)
            self._dirty_names |= {("group", eid) for eid, _ in groups}
        try:
            await self.storage.upsert_names("user", users)
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 昵称落盘失败: %s", exc)
            self._dirty_names |= {("user", eid) for eid, _ in users}

    async def terminate(self) -> None:
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                pass
            self._maintenance_task = None
        await self._persist_names()
        await self.writer.stop()
        await self.storage.close()

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(MAINTENANCE_INTERVAL)
                now = self._now()
                # 惰性推进：把有周期的实体结账（补满次数、作废过期申请）；
                # 无周期的实体没有到期可推，跳过以免每轮空跑全部历史实体
                await self.service.touch_all(self.service.active_keys(), now)
                # 回收退化成「等价于从未出现」的缓存状态，避免 _states 无界增长
                await self.service.reclaim(
                    now, window_cutoff(now, self._max_window_hours())
                )
                # 配额缓存同样按实体增长，定期整体丢弃（下次判定按需重算）
                self._invalidate_quota_cache()
                self.aggregator.sweep(now, self._max_window_hours())
                # 回退提示去重表：丢弃早于最大窗口的记录，避免无界增长
                notice_cutoff = now - self._max_window_hours() * 3600
                self._fallback_notified = {
                    k: v for k, v in self._fallback_notified.items() if v >= notice_cutoff
                }
                # 清理过期封禁
                for bk, bv in list(self._banned_users.items()):
                    if bv.is_expired(now):
                        self._banned_users.pop(bk, None)
                        await self.storage.remove_ban(bk[0], bk[1])
                await self._refresh_group_names()
                await self._persist_names()
                # 先把已关账的小时固化进历史汇总，再清理明细；purge 以水位线为
                # 护栏，保证未固化的明细不会先于固化被清掉。
                horizon = await self.storage.rollup_hourly(now)
                retention = int(self.config.get("retention_days", 30) or 0)
                if retention > 0:
                    # 三护栏：不清仍在最大限流窗口内的明细（误配 retention < 窗口
                    # 时保护重启恢复）、不清最长长窗口天数内的明细（长窗口累计值
                    # 启动时要从明细重算，见 docs/adr/0002），也不清尚未固化的明细
                    keep_days = max(retention, self._max_long_days())
                    await self.storage.purge_old(
                        min(
                            now - keep_days * SECONDS_PER_DAY,
                            window_cutoff(now, self._max_window_hours()),
                            horizon,
                        )
                    )
                    # 重置审计日志同样按保留期清理（只增不减会无界增长）。它不参与
                    # 任何判定，故只需保留期这一条护栏，无须对齐明细的三护栏。
                    await self.storage.purge_reset_log(
                        now - retention * SECONDS_PER_DAY
                    )
                history_retention = self._config_int("history_retention_days", 0)
                if history_retention > 0:
                    await self.storage.purge_hourly(
                        now - history_retention * SECONDS_PER_DAY
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("quota_hub 维护任务异常: %s", exc)

    # --- 配置助手 -------------------------------------------------------

    @staticmethod
    def _now() -> float:
        return time.time()

    def _window_hours(self) -> float:
        try:
            value = float(self.config.get("window_hours", 5) or 5)
        except (TypeError, ValueError):
            return 5.0
        return value if value > 0 else 5.0  # 窗口 <=0 会驱逐全部用量、变相停用限流

    def _max_window_hours(self) -> float:
        base = self._window_hours()
        for ov in self._overrides.values():
            if ov.window_hours:
                base = max(base, float(ov.window_hours))
        return base

    def _config_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _config_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return default if value is None else bool(value)

    def _long_window_days(self) -> float:
        """全局长窗口天数；非正值（误配）回落默认，避免周期开窗即到期。"""
        try:
            value = float(self.config.get("long_window_days", DEFAULT_LONG_WINDOW_DAYS))
        except (TypeError, ValueError):
            return DEFAULT_LONG_WINDOW_DAYS
        return value if value > 0 else DEFAULT_LONG_WINDOW_DAYS

    def _max_long_days(self) -> float:
        """明细清理护栏用的最长长窗口天数。

        取三者最大：全局配置、所有覆盖里的天数、**仍在进行中的最早周期已跨过的
        天数**。最后一项不可省：天数被调小（或覆盖被删）后，此前开的老周期仍按
        原跨度运行，只看配置值会把它重启后重算长窗口所需的明细提前清掉
        （见 docs/adr/0002），表现为重启后长窗口用量凭空归零。
        """
        base = self._long_window_days()
        for ov in self._overrides.values():
            if ov.long_window_days:
                base = max(base, float(ov.long_window_days))
        earliest = self.service.earliest_active_start()
        if earliest is not None:
            base = max(base, (self._now() - earliest) / SECONDS_PER_DAY)
        return base

    def _request_max_count(self) -> int:
        return max(1, self._config_int("request_max_count", 3))

    def _auto_reset_on_block(self) -> bool:
        """触顶时是否自动消耗一次重置次数继续放行。"""
        return self._config_bool("auto_reset_on_block", False)

    def _auto_reset_silent(self) -> bool:
        """自动重置后是否不在会话内提示。"""
        return self._config_bool("auto_reset_silent", False)

    def _auto_reset_template(self) -> str:
        return str(
            self.config.get("auto_reset_message", "") or DEFAULT_AUTO_RESET_MESSAGE
        )

    def _query_denied_message(self) -> str:
        return str(self.config.get("query_denied_message", "") or "管理员未开放配额查询")

    def _id_set(self, key: str) -> set[str]:
        return {normalize_id(x) for x in (self.config.get(key) or []) if normalize_id(x)}

    def _defaults_for(self, scope: str) -> Quota:
        # 回退默认与长窗口 / 重置次数默认对三作用域统一（可被逐实体覆盖），
        # 随 quota 一并参与解析。group 作用域的 self_reset_* 没有意义（群池不可
        # 自助重置），保持默认值即可。
        common = {
            "window_hours": self._window_hours(),
            "fallback_provider_id": self._fallback_provider_id(),
            "fallback_call_limit": self._fallback_call_limit(),
            "fallback_token_limit": self._fallback_token_limit(),
            "long_window_days": self._long_window_days(),
            "self_reset_short": max(
                0, self._config_int("self_reset_short_count", DEFAULT_SELF_RESET_SHORT)
            ),
            "self_reset_long": max(
                0, self._config_int("self_reset_long_count", DEFAULT_SELF_RESET_LONG)
            ),
        }
        if scope == SCOPE_PRIVATE:
            return Quota(
                call_limit=self._config_int("default_private_calls", 50),
                token_limit=self._config_int("default_private_tokens", -1),
                long_calls=self._config_int("default_private_long_calls", -1),
                long_tokens=self._config_int("default_private_long_tokens", -1),
                **common,
            )
        if scope == SCOPE_GROUP:
            return Quota(
                call_limit=self._config_int("default_group_calls", 200),
                token_limit=self._config_int("default_group_tokens", 1000000),
                long_calls=self._config_int("default_group_long_calls", -1),
                long_tokens=self._config_int("default_group_long_tokens", -1),
                **common,
            )
        return Quota(
            call_limit=self._config_int("default_member_calls", 30),
            token_limit=self._config_int("default_member_tokens", -1),
            long_calls=self._config_int("default_member_long_calls", -1),
            long_tokens=self._config_int("default_member_long_tokens", -1),
            **common,
        )

    # --- 回退（备用模型）配置 -------------------------------------------

    def _over_limit_action(self) -> str:
        action = str(self.config.get("over_limit_action", OVER_LIMIT_BLOCK) or "").strip()
        return action if action in VALID_OVER_LIMIT_ACTIONS else OVER_LIMIT_BLOCK

    def _fallback_provider_id(self) -> str:
        return str(self.config.get("fallback_provider_id", "") or "").strip()

    def _fallback_call_limit(self) -> int:
        return max(0, self._config_int("fallback_call_limit", 0))

    def _fallback_token_limit(self) -> int:
        return max(0, self._config_int("fallback_token_limit", 0))

    def _fallback_provider_exists(self, provider_id: str) -> bool:
        """供应商存在且支持对话（text_chat）才可作为备用模型。"""
        if not provider_id:
            return False
        getter = getattr(self.context, "get_provider_by_id", None)
        if not callable(getter):
            return False
        try:
            provider = getter(provider_id)
        except Exception:  # noqa: BLE001
            return False
        return provider is not None and hasattr(provider, "text_chat")

    def _provider_options(self) -> list[dict[str, str]]:
        """当前已加载、支持对话的供应商列表，供中台下拉选择备用模型。"""
        options: list[dict[str, str]] = []
        seen: set[str] = set()
        getter = getattr(self.context, "get_all_providers", None)
        providers = getter() if callable(getter) else []
        for provider in providers or []:
            if not hasattr(provider, "text_chat"):
                continue
            try:
                meta = provider.meta()
            except Exception:  # noqa: BLE001
                continue
            pid = str(getattr(meta, "id", "") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            model = str(getattr(meta, "model", "") or "").strip()
            ptype = str(getattr(meta, "type", "") or "").strip()
            detail = " / ".join(x for x in (ptype, model) if x)
            options.append({"id": pid, "label": f"{pid} - {detail}" if detail else pid})
        return options

    def _config_schema_for_page(self) -> dict[str, Any]:
        """向页面 schema 注入备用供应商下拉选项（静态 schema 无法预知供应商）。"""
        schema = copy.deepcopy(self._schema)
        spec = schema.get("fallback_provider_id")
        if isinstance(spec, dict):
            options = self._provider_options()
            spec["options"] = [""] + [o["id"] for o in options]
            spec["option_labels"] = ["（不设置）"] + [o["label"] for o in options]
        return schema

    def _member_defaults_for_group(self, group_id: str) -> Quota:
        """某群的成员默认配额：全局成员默认之上叠加该群的 member_default 覆盖。"""
        md = self._overrides.get(ScopeKey(SCOPE_MEMBER_DEFAULT, group_id, ""))
        return resolve_quota(self._defaults_for(SCOPE_MEMBER), md)

    def _resolve_quota(self, key: ScopeKey) -> Quota:
        """解析后的配额（带记忆化）；覆盖或配置变更时须调用 ``_invalidate_quota_cache``。"""
        cached = self._quota_cache.get(key)
        if cached is not None:
            return cached
        quota = self._compute_quota(key)
        self._quota_cache[key] = quota
        return quota

    def _invalidate_quota_cache(self) -> None:
        """清空配额缓存：覆盖增删改、配置保存后都必须调用（默认值也参与解析）。"""
        self._quota_cache.clear()

    def _compute_quota(self, key: ScopeKey) -> Quota:
        # member 走四层链（后者优先）：
        # 全局成员默认 → 群成员默认覆盖 → 用户级成员默认覆盖 → 具体成员覆盖
        if key.scope == SCOPE_MEMBER:
            defaults = self._member_defaults_for_group(key.group_id)
            ud = self._overrides.get(ScopeKey(SCOPE_USER_DEFAULT, "", key.user_id))
            defaults = resolve_quota(defaults, ud)
        else:
            defaults = self._defaults_for(key.scope)
        quota = resolve_quota(defaults, self._overrides.get(key))
        if quota.window_hours <= 0:  # 防御：窗口 <=0 会驱逐全部用量、变相停用限流
            quota = replace(quota, window_hours=self._window_hours())
        if quota.long_window_days <= 0:  # 防御：天数 <=0 会让周期开窗即到期
            quota = replace(quota, long_window_days=self._long_window_days())
        return quota

    @staticmethod
    def _strip_fallback(quota: Quota) -> Quota:
        """清零回退字段，使硬上限=常规上限（超限即拦截）。"""
        if not (
            quota.fallback_provider_id
            or quota.fallback_call_limit
            or quota.fallback_token_limit
        ):
            return quota
        return replace(
            quota,
            fallback_provider_id="",
            fallback_call_limit=0,
            fallback_token_limit=0,
        )

    def _save_config(self) -> None:
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()

    def _serialize_config(self) -> dict[str, Any]:
        return {
            key: self.config.get(key, spec.get("default"))
            for key, spec in self._schema.items()
        }

    @staticmethod
    def _coerce(value: Any, type_str: str) -> Any:
        """把提交值转换为 schema 声明类型；不合法时抛 ValueError/TypeError。

        快速失败而非静默降级：坏类型静默通过会把垃圾值存进配置，list 类型
        收到字符串若静默变 ``[]`` 甚至可能清空监控/白名单。
        """
        if type_str == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if type_str == "int":
            return int(value)
        if type_str == "float":
            return float(value)
        if type_str == "list":
            if isinstance(value, (list, tuple)):
                return list(value)
            raise ValueError("必须是数组")
        return str(value)

    def _sanitize_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, spec in self._schema.items():
            if key in raw:
                type_str = spec.get("type", "string")
                try:
                    result[key] = self._coerce(raw[key], type_str)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"配置项「{key}」的值不合法（应为 {type_str} 类型）"
                    ) from None
        window = result.get("window_hours")
        if window is not None and window <= 0:
            raise ValueError("window_hours 必须大于 0")
        mode = result.get("enforcement_mode")
        if mode is not None and mode not in ("allowlist", "all"):
            raise ValueError("enforcement_mode 必须是 allowlist 或 all")
        action = result.get("over_limit_action")
        if action is not None and action not in VALID_OVER_LIMIT_ACTIONS:
            raise ValueError("over_limit_action 必须是 block 或 fallback")
        days = result.get("long_window_days")
        if days is not None and days <= 0:
            raise ValueError("long_window_days 必须大于 0")
        for key in ("self_reset_short_count", "self_reset_long_count"):
            value = result.get(key)
            if value is not None and value < 0:
                raise ValueError(f"{key} 不能为负数")
        max_count = result.get("request_max_count")
        if max_count is not None and max_count < 1:
            raise ValueError("request_max_count 必须大于等于 1")
        sessions = result.get("request_notify_sessions")
        if sessions is not None:
            result["request_notify_sessions"] = [
                str(x).strip() for x in sessions if str(x).strip()
            ]
        # 选择回退时校验备用供应商存在；部分更新时回落到已存配置值。
        effective_action = action if action is not None else self._over_limit_action()
        if effective_action == OVER_LIMIT_FALLBACK:
            pid = str(
                result.get("fallback_provider_id", self.config.get("fallback_provider_id", ""))
                or ""
            ).strip()
            if not pid:
                raise ValueError("选择「回退到备用模型」时必须指定备用模型供应商")
            if not self._fallback_provider_exists(pid):
                raise ValueError(f"未找到备用模型供应商：{pid}")
        return result

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        for key, value in cfg.items():
            self.config[key] = value
        self._save_config()
        self._invalidate_quota_cache()  # 默认值参与解析，改配置即改所有实体的配额

    # --- 覆盖 / 重置 ----------------------------------------------------

    async def _set_override(self, ov: Override) -> None:
        old = self._overrides.get(ov.key)
        await self.storage.upsert_override(ov)
        self._overrides[ov.key] = ov
        # 覆盖可能是 member_default / user_default，会影响其它键，故整体清空
        self._invalidate_quota_cache()
        # 专属窗口变更会改变该实体的有效窗口；内存驱逐不可逆，须从 DB 重建回填
        if (old.window_hours if old else None) != ov.window_hours:
            await self._rebuild_usage()

    async def _rebuild_usage(self) -> None:
        """从 DB 重建内存聚合器（窗口变更时调用，非热路径）。

        聚合器的驱逐是破坏性的：窗口调大后，已按旧小窗口逐出的记录只在 DB 里
        还有，不重建会导致限流偏松直到重启。先 flush 落盘队列避免漏掉在途事件。
        """
        await self.writer.flush()
        cutoff = window_cutoff(self._now(), self._max_window_hours())
        self.aggregator.rebuild(await self.storage.load_recent(cutoff))

    async def _set_remark(self, kind: str, entity_id: str, remark: str) -> None:
        await self.storage.set_remark(kind, entity_id, remark)
        if kind == "group":
            if remark:
                self._group_remarks[entity_id] = remark
            else:
                self._group_remarks.pop(entity_id, None)

    def _capture_names(self, event: AstrMessageEvent, group_id: str, user_id: str) -> None:
        """从消息对象捕获群名/昵称到内存缓存（零额外平台调用），变更才标脏。"""
        if group_id:
            group = getattr(getattr(event, "message_obj", None), "group", None)
            name = (getattr(group, "group_name", None) or "").strip()[:64]
            if name and self._group_names.get(group_id) != name:
                self._group_names[group_id] = name
                self._dirty_names.add(("group", group_id))
        if user_id:
            try:
                nick = (event.get_sender_name() or "").strip()[:64]
            except Exception:  # noqa: BLE001
                nick = ""
            if nick and self._user_names.get(user_id) != nick:
                self._user_names[user_id] = nick
                self._dirty_names.add(("user", user_id))

    # --- 经协议获取群名 / 成员昵称（aiocqhttp）-------------------------

    def _resolve_aiocqhttp_client(self) -> Any:
        try:
            instances = self.context.platform_manager.get_insts()
        except Exception:  # noqa: BLE001
            return None
        for platform in instances:
            try:
                if platform.meta().name == "aiocqhttp":
                    return platform.get_client()
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _refresh_group_names(self, force: bool = False) -> None:
        """用一次 get_group_list 批量刷新所有群名（节流，best-effort）。"""
        now = self._now()
        if not force and now - self._group_names_refreshed_at < GROUP_NAME_REFRESH_INTERVAL:
            return
        client = self._resolve_aiocqhttp_client()
        if client is None:
            return  # 非 aiocqhttp / 未就绪：不消耗节流，下次再试
        self._group_names_refreshed_at = now
        try:
            groups = await asyncio.wait_for(
                client.call_action("get_group_list"), timeout=PROTOCOL_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 获取群列表失败: %s", exc)
            return
        if not isinstance(groups, list):
            return
        for g in groups:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("group_id") or "")
            name = str(g.get("group_name") or "").strip()[:64]
            if gid and name and self._group_names.get(gid) != name:
                self._group_names[gid] = name
                self._dirty_names.add(("group", gid))

    async def _fetch_member_names(self, group_id: str) -> dict[str, str]:
        """获取某群成员 user_id→显示名（群名片优先）。按群 TTL 缓存，保护协议端。"""
        now = self._now()
        cached = self._member_name_cache.get(group_id)
        if cached and now - cached[0] < MEMBER_NAME_TTL:
            return cached[1]
        if not group_id.isdigit():  # 非数字群号（如 qq_official openid）走不了 OneBot
            return cached[1] if cached else {}
        client = self._resolve_aiocqhttp_client()
        if client is None:
            return cached[1] if cached else {}
        try:
            raw = await asyncio.wait_for(
                client.call_action("get_group_member_list", group_id=int(group_id)),
                timeout=PROTOCOL_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 获取群 %s 成员失败: %s", group_id, exc)
            return cached[1] if cached else {}
        names: dict[str, str] = {}
        if isinstance(raw, list):
            for m in raw:
                if not isinstance(m, dict):
                    continue
                uid = str(m.get("user_id") or "")
                nm = str(m.get("card") or m.get("nickname") or "").strip()[:64]
                if uid and nm:
                    names[uid] = nm
        self._member_name_cache[group_id] = (now, names)
        return names

    def _remember_user_name(self, user_id: str, name: str) -> None:
        """把显示名写入持久昵称缓存（供「按人」表显示），变更才标脏。"""
        if name and self._user_names.get(user_id) != name:
            self._user_names[user_id] = name
            self._dirty_names.add(("user", user_id))

    async def _clear_override(self, scope: str, group_id: str, user_id: str) -> bool:
        deleted = await self.storage.delete_override(scope, group_id, user_id)
        removed = self._overrides.pop(ScopeKey(scope, group_id, user_id), None)
        self._invalidate_quota_cache()
        # 删除带专属窗口的覆盖后该实体回落默认窗口，重建以对齐内存用量
        if removed is not None and removed.window_hours is not None:
            await self._rebuild_usage()
        return deleted

    async def _reset_entity(
        self,
        scope: str,
        group_id: str,
        user_id: str,
        window: str = WINDOW_SHORT,
        actor: str = ACTOR_ADMIN,
        actor_id: str = "",
    ) -> None:
        """管理员 / 中台重置：写重置标记或关闭周期，**不**删除用量事件。

        见 docs/adr/0001：重置只改变限流的统计起点，历史统计与群共享池的其他
        成员都不受影响（旧的「reset member 连带扣减群池」行为已移除）。
        """
        key = ScopeKey(scope, group_id, user_id)
        await self.service.touch(key, self._now())
        await self.service.reset(key, window, actor, actor_id, consume=False)

    # --- 生效范围判定 ---------------------------------------------------

    def _is_subject_to_limit(self, group_id: str, user_id: str) -> bool:
        mode = str(self.config.get("enforcement_mode", "allowlist"))
        if group_id:
            if mode == "all":
                return not (
                    group_id in self._id_set("exempt_groups")
                    or user_id in self._id_set("exempt_users")
                )
            return group_id in self._id_set("monitored_groups")
        if mode == "all":
            return user_id not in self._id_set("exempt_users")
        return user_id in self._id_set("monitored_users")

    def _is_skipped(self, event: AstrMessageEvent) -> bool:
        text = (event.get_message_str() or "").strip()
        if not text:
            return False
        prefixes = [str(p) for p in (self.config.get("skip_prefixes") or [])]
        return any(prefix and text.startswith(prefix) for prefix in prefixes)

    def _scope_items(
        self,
        keys: list[ScopeKey],
        now: float,
        *,
        zeroed: tuple[ScopeKey, str] | None = None,
    ) -> list[ScopeUsage]:
        """构造 ``decide_scopes`` 的输入（纯同步，无 await）。

        ``zeroed=(key, window)`` 表示「假设该实体该窗口刚被重置」：短窗口把
        ``calls/tokens`` 归零、``oldest`` 置空，长窗口把 ``long_calls/long_tokens``
        归零、``long_end`` 置空（重置即关窗）。供自动重置的干跑使用——先算一遍
        「重置之后还会不会被拦」，避免白扣次数。
        """
        action_fallback = self._over_limit_action() == OVER_LIMIT_FALLBACK
        provider_ok: dict[str, bool] = {}  # 单次判定内缓存供应商有效性
        items = []
        for key in keys:
            quota = self._resolve_quota(key)
            # 回退额度仅在「全局开启回退 + 该实体备用供应商有效」时生效；否则清零→超限即拦截。
            usable = action_fallback and bool(quota.fallback_provider_id)
            if usable:
                pid = quota.fallback_provider_id
                if pid not in provider_ok:
                    provider_ok[pid] = self._fallback_provider_exists(pid)
                usable = provider_ok[pid]
            if not usable:
                quota = self._strip_fallback(quota)
            calls, tokens, oldest = self.aggregator.usage(key, now, quota.window_hours)
            long_calls, long_tokens = self.aggregator.long_usage(key)
            # 传入已解析的配额，避免在 long_end 内再解析一次
            long_end = self.service.long_end(key, quota)
            if zeroed is not None and zeroed[0] == key:
                if zeroed[1] == WINDOW_SHORT:
                    calls, tokens, oldest = 0, 0, None
                else:
                    long_calls, long_tokens, long_end = 0, 0, None
            items.append(
                ScopeUsage(
                    scope=key.scope,
                    quota=quota,
                    calls=calls,
                    tokens=tokens,
                    oldest=oldest,
                    long_calls=long_calls,
                    long_tokens=long_tokens,
                    long_end=long_end,
                )
            )
        return items

    def _decide(self, keys: list[ScopeKey], now: float) -> Decision:
        """同步判定：长窗口到期须由调用方先 ``await service.touch_all(keys, now)``。"""
        return decide_scopes(self._scope_items(keys, now), now)

    @staticmethod
    def _platform_of(event: AstrMessageEvent) -> str:
        origin = getattr(event, "unified_msg_origin", "") or ""
        return origin.split(":", 1)[0] if ":" in origin else origin

    # --- 运行期拦截 -----------------------------------------------------

    def _format_ban_tip(self, ban: BanRecord, now: float) -> str:
        expire_str = "永久" if ban.is_permanent else self._fmt_time_full(ban.expire_at)
        remaining_str = ""
        if not ban.is_permanent:
            remaining_str = f"（剩余约 {format_duration(ban.expire_at - now)}）"
        reason_str = f"，理由：{ban.reason}" if ban.reason else ""
        return (
            f"🚫 你已被管理员禁止与 LLM 对话及交互至 "
            f"{expire_str}{remaining_str}{reason_str}"
        )

    def _get_self_id(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            try:
                sid = getter()
                if sid:
                    return normalize_id(sid)
            except Exception:  # noqa: BLE001
                pass
        sid = getattr(event, "self_id", None)
        if sid:
            return normalize_id(sid)
        try:
            platform_mgr = getattr(self.context, "platform_manager", None)
            if platform_mgr and hasattr(platform_mgr, "get_insts"):
                for inst in platform_mgr.get_insts():
                    bid = getattr(inst, "bot_id", None) or getattr(inst, "self_id", None)
                    if bid:
                        return normalize_id(bid)
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _is_at_or_reply_to_bot(self, event: AstrMessageEvent) -> bool:
        self_id = self._get_self_id(event)
        msg_obj = getattr(event, "message_obj", None)
        if msg_obj and hasattr(msg_obj, "message"):
            for comp in getattr(msg_obj, "message", []):
                cname = comp.__class__.__name__
                if cname == "At" or cname.startswith("At"):
                    target = (
                        getattr(comp, "qq", None)
                        or getattr(comp, "target", None)
                        or getattr(comp, "user_id", None)
                    )
                    target_id = normalize_id(target)
                    if self_id and target_id == self_id:
                        return True
                    if str(target).lower() in ("all", "-1") or getattr(comp, "is_at_all", False):
                        return True
                elif cname in ("Reply", "Quote") or cname.startswith(("Reply", "Quote")):
                    reply_sender = (
                        getattr(comp, "sender", None)
                        or getattr(comp, "user_id", None)
                        or getattr(comp, "sender_id", None)
                    )
                    if self_id and normalize_id(reply_sender) == self_id:
                        return True
        return False

    def _is_command_or_wake(self, event: AstrMessageEvent) -> bool:
        """检查消息是否属于指令、唤醒或对 Bot 的主动交互。"""
        # 1. 私聊：发送给 Bot 的任何消息均视为交互
        group_id = normalize_id(event.get_group_id())
        if not group_id:
            return True

        # 2. 框架已认定的唤醒状态
        if getattr(event, "is_wake", False):
            return True
        if getattr(event, "is_at_or_wake_command", False):
            return True

        # 3. 常见指令前缀或唤醒前缀
        text = (event.get_message_str() or "").strip()
        if text:
            prefixes = ["/", "!", "！", ".", "。", "#", "\\"]
            for sp in self.config.get("skip_prefixes") or []:
                if sp and sp not in prefixes:
                    prefixes.append(str(sp))
            try:
                cfg_getter = getattr(self.context, "get_config", None)
                if callable(cfg_getter):
                    umo = getattr(event, "unified_msg_origin", None)
                    global_cfg = cfg_getter(umo=umo) if umo else cfg_getter()
                    if isinstance(global_cfg, dict):
                        wake_prefixes = global_cfg.get("wake_prefix")
                        if isinstance(wake_prefixes, list):
                            for wp in wake_prefixes:
                                if wp and wp not in prefixes:
                                    prefixes.append(str(wp))
                        elif (
                            isinstance(wake_prefixes, str)
                            and wake_prefixes
                            and wake_prefixes not in prefixes
                        ):
                            prefixes.append(wake_prefixes)
            except Exception:  # noqa: BLE001
                pass

            if any(text.startswith(p) for p in prefixes):
                return True

        # 4. @Bot 或回复 Bot
        if self._is_at_or_reply_to_bot(event):
            return True

        return False

    @_event_message_type(getattr(EventMessageType, "ALL", "ALL"), priority=1000)
    async def _intercept_banned_message(self, event: AstrMessageEvent) -> None:
        """在所有指令与消息处理器之前拦截被封禁用户的消息（禁用任何指令或唤醒）。"""
        if not bool(self.config.get("enabled", True)):
            return
        group_id = normalize_id(event.get_group_id())
        user_id = normalize_id(event.get_sender_id())
        if not user_id:
            return

        now = self._now()
        ban = self.is_banned(group_id, user_id, now)
        if ban is None:
            return

        # 无论是否显式唤醒，只要被封禁，立即停止该事件继续向后传播（阻断任何指令与插件触发）
        event.stop_event()

        # 仅当检测到主动交互（指令、唤醒、私聊）时发送封禁提示，避免在群普通闲聊中刷屏
        if bool(self.config.get("send_ban_message", True)) and self._is_command_or_wake(event):
            if not getattr(event, "_quota_hub_ban_notified", False):
                setattr(event, "_quota_hub_ban_notified", True)
                ban_tip = self._format_ban_tip(ban, now)
                try:
                    await event.send(MessageChain().message(ban_tip))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("quota_hub 发送封禁提示失败: %s", exc)

        logger.info(
            "quota_hub 封禁拦截(指令/唤醒) group=%s user=%s expire_at=%s reason=%s",
            group_id,
            user_id,
            ban.expire_at,
            ban.reason,
        )

    @_on_waiting_llm_request(priority=900)
    async def _preselect_fallback(self, event: AstrMessageEvent) -> None:
        """在 AstrBot 选择供应商之前预切换备用模型。

        该钩子早于供应商选择触发（见 pipeline），是写入 ``selected_provider`` 唯一
        生效的时机；``on_llm_request`` 时供应商已选定，改写已无效。
        """
        if not bool(self.config.get("enabled", True)):
            return
        if self._over_limit_action() != OVER_LIMIT_FALLBACK:
            return
        if self._is_skipped(event):
            return
        group_id = normalize_id(event.get_group_id())
        user_id = normalize_id(event.get_sender_id())
        if not user_id or self.is_banned(group_id, user_id):
            return
        if not self._is_subject_to_limit(group_id, user_id):
            return
        keys = scopes_for(group_id, user_id)
        now = self._now()
        await self.service.touch_all(keys, now)
        decision = self._decide(keys, now)
        if decision.outcome == OUTCOME_FALLBACK and decision.fallback_provider_id:
            event.set_extra("selected_provider", decision.fallback_provider_id)
            # 记录预选结果，供 _enforce 对账：只有真正切换了供应商才发回退提示
            event.set_extra("_quota_hub_preselected", decision.fallback_provider_id)

    @filter.on_llm_request(priority=900)
    async def _enforce(self, event: AstrMessageEvent, req: Any) -> None:
        if not bool(self.config.get("enabled", True)):
            return
        if self._is_skipped(event):
            return
        group_id = normalize_id(event.get_group_id())
        user_id = normalize_id(event.get_sender_id())
        if not user_id:
            return

        now = self._now()
        ban = self.is_banned(group_id, user_id, now)
        if ban is not None:
            if bool(self.config.get("send_ban_message", True)):
                if not getattr(event, "_quota_hub_ban_notified", False):
                    setattr(event, "_quota_hub_ban_notified", True)
                    ban_tip = self._format_ban_tip(ban, now)
                    try:
                        await event.send(MessageChain().message(ban_tip))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("quota_hub 发送封禁提示失败: %s", exc)
            event.stop_event()
            logger.info(
                "quota_hub 封禁拦截 group=%s user=%s expire_at=%s reason=%s",
                group_id, user_id, ban.expire_at, ban.reason,
            )
            return

        if not self._is_subject_to_limit(group_id, user_id):
            return

        # 在判定之前捕获名称：被拦截的实体也应能在中台显示群名/昵称
        self._capture_names(event, group_id, user_id)
        keys = scopes_for(group_id, user_id)
        now = self._now()
        # 判定前推进长窗口到期（惰性），否则已到期的周期会继续拦截
        await self.service.touch_all(keys, now)
        # 此处重新判定（而非复用 _preselect_fallback 的结果）：本钩子是放行/拦截/记账的
        # 权威判定点，以判定时刻的最新用量为准；预选钩子仅负责更早地切换供应商。
        decision = self._decide(keys, now)
        # 触发自动重置的那次判定：提示文案要用它的窗口/指标，故在重判前留存
        blocked = decision
        auto_remaining: int | None = None
        if decision.outcome == OUTCOME_BLOCK:
            # 触顶：先尝试自动消耗一次重置次数并重新判定（每次请求最多一次；
            # 重置后仍会被拦截的情形由内部干跑提前挡掉，不白扣次数）。
            # 其中的 await 全部发生在第二次 _decide 之前，故「判定 → 记账」
            # 之间无 await 的不变量继续成立。
            decision, auto_remaining = await self._try_auto_reset(keys, decision, now)
        if decision.outcome == OUTCOME_BLOCK:
            # 扣了次数却仍被拦截：先说明次数已消耗，再发拦截提示（此分支不记账，
            # 可自由 await）。干跑（见 _auto_reset）已经挡掉了确定性的白扣，这里
            # 只兜底并发场景：重置落盘的 await 期间别的请求把额度又用满了。
            if auto_remaining is not None:
                await self._send_auto_reset_notice(
                    event, blocked, auto_remaining, AUTO_RESET_PARTIAL_MESSAGE
                )
            if bool(self.config.get("send_block_message", True)):
                template = str(
                    self.config.get("block_message", "") or DEFAULT_BLOCK_MESSAGE
                )
                try:
                    await event.send(
                        MessageChain().message(format_block_message(template, decision))
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("quota_hub 发送拦截提示失败: %s", exc)
            event.stop_event()
            logger.info(
                "quota_hub 拦截 scope=%s metric=%s used=%s/%s group=%s user=%s",
                decision.scope, decision.metric, decision.used, decision.limit,
                group_id, user_id,
            )
            return

        # 放行且计入。判定与记账之间不得有 await：一旦让出，并发请求会读到同一份
        # 旧用量而超发。因此先同步完成「开窗 + 计数 + 入队」，再 await 落盘。
        # 开窗必须早于 record_call，否则本次调用不计入长窗口；无论长窗口配额
        # 是否为 -1 都开窗。
        opened = self.service.open_if_needed(keys, now)
        eid = self.aggregator.record_call(keys, now)
        model = str(getattr(req, "model", "") or "")
        self.writer.enqueue_insert(
            (eid, int(now), self._platform_of(event), group_id, user_id, 1, 0, model)
        )
        # 维护待回填的 eid 队列（支持同一消息触发多次/并发 LLM 请求时按序匹配）
        pending = getattr(event, "_quota_hub_pending_eids", None)
        if pending is None:
            pending = []
            setattr(event, "_quota_hub_pending_eids", pending)
        pending.append(eid)
        setattr(event, "_quota_hub_eid", eid)
        await self.service.persist_states(opened)

        # 自动重置提示放在记账之后：它是 await，留在判定与记账之间会重新引入让出间隙
        if auto_remaining is not None:
            await self._send_auto_reset_notice(
                event, blocked, auto_remaining, self._auto_reset_template()
            )

        # 回退区间：供应商已在 on_waiting_llm_request 预切换，此处仅按需提示一次。
        # 放在记账之后：提示是 await，留在判定与记账之间会重新引入让出间隙。
        if decision.outcome == OUTCOME_FALLBACK and decision.fallback_provider_id:
            preselected = str(event.get_extra("_quota_hub_preselected") or "")
            if preselected == decision.fallback_provider_id:
                await self._maybe_send_fallback_notice(event, decision)
                logger.info(
                    "quota_hub 超限回退 scope=%s metric=%s used=%s/%s provider=%s"
                    " group=%s user=%s",
                    decision.scope, decision.metric, decision.used, decision.limit,
                    decision.fallback_provider_id, group_id, user_id,
                )
            else:
                # 预选未切换：旧版 AstrBot 缺钩子、并发下两次判定间越过了边界，或
                # 自动重置把拦截翻成了回退（预选钩子早已跑完，见 0.4.1 规格「已知
                # 后果」）。本次实际仍用主供应商，既不发「已切换」提示，日志也不能
                # 打 provider= 让人以为切了。
                logger.info(
                    "quota_hub 超限回退判定但未切换供应商，本次仍用主模型"
                    " scope=%s metric=%s used=%s/%s expected_provider=%s"
                    " preselected=%s group=%s user=%s",
                    decision.scope, decision.metric, decision.used, decision.limit,
                    decision.fallback_provider_id, preselected or "-",
                    group_id, user_id,
                )

    async def _try_auto_reset(
        self,
        keys: list[ScopeKey],
        decision: Decision,
        now: float,
    ) -> tuple[Decision, int | None]:
        """``_auto_reset`` 的兜底外壳：任何异常都降级为「不重置、保持原判定」。

        本钩子既负责拦截也负责记账，异常一旦逃逸就会变成「既不拦截也不记账」的
        fail-open（额度形同虚设）。故这里把整段自动重置吞掉，返回调用方传进来的
        原始拦截判定，让请求按未开启自动重置处理。
        """
        try:
            return await self._auto_reset(keys, decision, now)
        except Exception as exc:  # noqa: BLE001 - 绝不让异常穿透限流钩子
            logger.warning(
                "quota_hub 自动重置异常，降级为照常拦截 scope=%s window=%s: %s",
                decision.scope, decision.window, exc,
            )
            return decision, None

    async def _auto_reset(
        self,
        keys: list[ScopeKey],
        decision: Decision,
        now: float,
    ) -> tuple[Decision, int | None]:
        """触顶时自动消耗一次重置次数，返回 ``(重判后的判定, 剩余次数)``。

        未重置时返回 ``(decision, None)``；重置了则返回重置后该窗口的剩余次数，
        由调用方决定何时发提示（放行路径要等记账之后）。群共享池永不自动重置（与
        「群池只有管理员可重置」一致）；其余前置条件完全复用 ``check_self_reset``。
        作用的实体固定为 ``keys[-1]``（群聊=成员实体、私聊=私聊实体），窗口取触发
        窗口（短窗口触顶消耗短窗口次数，长窗口同理）。

        落盘之前先做一次**同步干跑**：按「该实体该窗口用量已清零」重算一遍判定，
        仍是拦截（长短窗口同时超限、群池已满……）就不重置，免得白扣次数。

        **顺序不变量**：唯一的 ``await``（重置落盘）排在第二次 ``_decide`` 之前，
        调用方拿到新判定后即可同步记账，中间不会再让出事件循环。
        """
        if not self._auto_reset_on_block():
            return decision, None
        if decision.scope not in (SCOPE_PRIVATE, SCOPE_MEMBER):
            return decision, None
        key = keys[-1]
        # 不变量守卫：拦截判定的作用域必须就是要重置的那个实体，否则等于拿别人的
        # 超限去扣本实体的次数
        if key.scope != decision.scope:
            return decision, None
        window = decision.window
        quota = self._resolve_quota(key)
        # 到期已由 _enforce 的 touch_all 推进过，此处只读状态与用量
        calls, tokens, _oldest = self.aggregator.usage(key, now, quota.window_hours)
        refusal = check_self_reset(
            self.service.state_of(key, quota),
            window,
            limited=self._entity_limited(key, quota, window),
            has_short_usage=calls > 0 or tokens > 0,
            days=quota.long_window_days,
            now=now,
        )
        if refusal is not None:
            return decision, None
        # 干跑：纯同步计算，不落盘、不 await；重置也救不回来就别扣次数
        dry = decide_scopes(self._scope_items(keys, now, zeroed=(key, window)), now)
        if dry.outcome == OUTCOME_BLOCK:
            logger.info(
                "quota_hub 自动重置干跑仍被拦截，跳过重置 scope=%s window=%s"
                " blocking_scope=%s group=%s user=%s",
                decision.scope, window, dry.scope, key.group_id, key.user_id,
            )
            return decision, None
        # 必须传入判定用的 now：让重置标记与本次记账时刻一致，否则本次调用会被
        # 自己写下的标记驱逐出短窗口（等于每次自动重置多放行一次）
        state = await self.service.reset(
            key, window, ACTOR_AUTO, key.user_id, consume=True, now=now
        )
        remaining = allowance(state, window)
        logger.info(
            "quota_hub 自动重置 scope=%s window=%s group=%s user=%s remaining=%s",
            decision.scope, window, key.group_id, key.user_id, remaining,
        )
        # 重置后状态已变，重新取一次真实判定（不复用干跑结果）
        return self._decide(keys, now), remaining

    async def _send_auto_reset_notice(
        self,
        event: AstrMessageEvent,
        decision: Decision,
        remaining: int,
        template: str,
    ) -> None:
        """自动重置后在会话内提示；发送失败只记 warning，不影响放行。

        ``decision`` 是触发自动重置的那次拦截判定（提供窗口/指标文案），
        ``template`` 由调用方按「已放行 / 仍被拦截」选择。
        """
        if self._auto_reset_silent():
            return
        try:
            await event.send(
                MessageChain().message(
                    format_auto_reset_message(template, decision, remaining)
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 发送自动重置提示失败: %s", exc)

    async def _maybe_send_fallback_notice(
        self, event: AstrMessageEvent, decision: Decision
    ) -> None:
        """切换到备用模型时在会话内提示——同一会话一个窗口内只提示一次。"""
        if not bool(self.config.get("send_fallback_message", True)):
            return
        template = str(self.config.get("fallback_message", "") or "")
        if not template:
            return
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        now = self._now()
        # 冷却用触发作用域的实际窗口（可能被实体覆盖），而非全局默认窗口
        window = (decision.window_hours or self._window_hours()) * 3600
        if origin and now - self._fallback_notified.get(origin, 0.0) < window:
            return
        if origin:
            self._fallback_notified[origin] = now
        try:
            await event.send(
                MessageChain().message(format_block_message(template, decision))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 发送回退提示失败: %s", exc)

    @staticmethod
    def _extract_tokens(response: Any) -> int:
        """从各类型 Provider 响应对象中稳健提取消耗的 Token 总数。"""
        if response is None:
            return 0
        usage = getattr(response, "usage", None)
        if usage is None:
            raw = getattr(response, "raw_completion", None) or getattr(
                response, "raw_response", None
            )
            if raw is not None:
                usage = getattr(raw, "usage", None)
        if usage is None:
            return 0

        # 字典结构兼容（部分自定义 Provider 返回 dict）
        if isinstance(usage, dict):
            for key in ("total_tokens", "total"):
                if key in usage and usage[key] is not None:
                    try:
                        return max(0, int(usage[key]))
                    except (TypeError, ValueError):
                        pass
            inp = (
                usage.get("prompt_tokens")
                or usage.get("input_tokens")
                or usage.get("input")
                or 0
            )
            out = (
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or usage.get("output")
                or 0
            )
            try:
                return max(0, int(inp or 0) + int(out or 0))
            except (TypeError, ValueError):
                return 0

        # 对象属性兼容（如 OpenAI CompletionUsage / AstrBot TokenUsage）
        for attr in ("total_tokens", "total"):
            val = getattr(usage, attr, None)
            if val is not None:
                try:
                    return max(0, int(val))
                except (TypeError, ValueError):
                    pass

        inp = (
            getattr(usage, "prompt_tokens", None)
            or getattr(usage, "input_tokens", None)
            or getattr(usage, "input", None)
            or 0
        )
        out = (
            getattr(usage, "completion_tokens", None)
            or getattr(usage, "output_tokens", None)
            or getattr(usage, "output", None)
            or 0
        )
        try:
            return max(0, int(inp or 0) + int(out or 0))
        except (TypeError, ValueError):
            return 0

    @filter.on_llm_response()
    async def _record_tokens(self, event: AstrMessageEvent, response: Any) -> None:
        pending: list[int] | None = getattr(event, "_quota_hub_pending_eids", None)
        if pending:
            eid = pending.pop(0)
        else:
            eid = getattr(event, "_quota_hub_eid", None)
        if eid is None:
            return

        total = self._extract_tokens(response)
        if total <= 0:
            return
        self.aggregator.add_tokens(int(eid), total)
        self.writer.enqueue_token_update(int(eid), total)

    # --- 指令 -----------------------------------------------------------

    @filter.command_group("quota")
    def quota_root(self):  # noqa: ANN201 - 命令组容器
        """配额中枢指令组。"""

    @quota_root.command("help")
    async def quota_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)

    @quota_root.command("me")
    async def quota_me(self, event: AstrMessageEvent):
        if not self._config_bool("allow_user_query", True) and not event.is_admin():
            yield event.plain_result(f"⚠️ {self._query_denied_message()}")
            return
        group_id = normalize_id(event.get_group_id())
        user_id = normalize_id(event.get_sender_id())
        keys = scopes_for(group_id, user_id)
        now = self._now()
        await self.service.touch_all(keys, now)  # 查询前推进到期
        lines = ["📊 你的配额用量："]
        ban = self.is_banned(group_id, user_id, now)
        if ban is not None:
            expire_str = "永久" if ban.is_permanent else self._fmt_time_full(ban.expire_at)
            rem = f"（剩余约 {format_duration(ban.expire_at - now)}）" if not ban.is_permanent else ""
            rsn = f"，理由：{ban.reason}" if ban.reason else ""
            lines.append(f"· 🚫 封禁状态：已被禁止与 LLM 对话及交互至 {expire_str}{rem}{rsn}")
        for key in keys:
            lines.extend(self._status_lines(key, now))
        # 自助重置次数与申请状态只针对「自己」的实体：群里是成员实体，私聊是私聊实体
        lines.extend(await self._self_service_lines(keys[-1]))
        yield event.plain_result("\n".join(lines))

    def _status_lines(self, key: ScopeKey, now: float) -> list[str]:
        """某作用域的两行状态：短窗口一行（含恢复时间）、长窗口一行（含到期时刻）。"""
        quota = self._resolve_quota(key)
        label = key.label()
        if not quota.enabled:
            return [f"· {label}：不限（该实体已停用限制）"]
        calls, tokens, oldest = self.aggregator.usage(key, now, quota.window_hours)
        short = (
            f"· {label} 短窗口({self._fmt_hours(quota.window_hours)}小时)："
            f"调用 {self._fmt_used(calls, quota.call_limit)}，"
            f"Token {self._fmt_used(tokens, quota.token_limit)}"
        )
        if oldest is not None:
            recover = reset_in_seconds(oldest, now, quota.window_hours)
            short += f"，约 {format_duration(recover)} 后开始恢复"
        long_calls, long_tokens = self.aggregator.long_usage(key)
        end = self.service.long_end(key, quota)  # 复用上面解析出的配额
        long = (
            f"· {label} 长窗口({self._fmt_hours(quota.long_window_days)}天)："
            f"调用 {self._fmt_used(long_calls, quota.long_calls)}，"
            f"Token {self._fmt_used(long_tokens, quota.long_tokens)}"
        )
        long += f"，{self._fmt_time(end)} 到期" if end is not None else "，当前无进行中的周期"
        return [short, long]

    async def _self_service_lines(self, key: ScopeKey) -> list[str]:
        """剩余重置次数 + 待审批申请 + 未送达的审批结果（展示后置 notified）。"""
        state = self.service.state_of(key)
        lines = [
            f"· 剩余自助重置次数：短窗口 {state.short_allowance} 次，"
            f"长窗口 {state.long_allowance} 次"
        ]
        try:
            pending = await self.service.pending_request(key)
            undelivered = await self.storage.list_undelivered(key)
        except Exception as exc:  # noqa: BLE001 - 查询失败不应让整条回复失败
            logger.warning("quota_hub 读取申请状态失败: %s", exc)
            return lines
        if pending is not None:
            lines.append(f"· 待审批申请：{describe(pending)}")
        for req in undelivered:
            lines.append(f"· {decision_text(req)}")
            await self._mark_request_notified(req)
        return lines

    async def _mark_request_notified(self, req: Any) -> None:
        """把审批结果标记为「已送达」（主动推送成功或已在 /quota me 展示）。"""
        try:
            await self.storage.update_request(mark_notified(req))
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 标记申请 #%s 已通知失败: %s", req.id, exc)

    @staticmethod
    def _fmt_used(used: int, limit: int) -> str:
        return f"{used}/不限" if limit < 0 else f"{used}/{limit}"

    @staticmethod
    def _fmt_time(ts: float) -> str:
        """长窗口到期时刻按服务端本地时区展示。"""
        return time.strftime("%m-%d %H:%M", time.localtime(ts))

    @staticmethod
    def _fmt_hours(hours: float) -> str:
        return str(int(hours)) if float(hours).is_integer() else str(hours)

    @staticmethod
    def _parse_limit(value: str | None) -> int | None:
        if value is None or value == "":
            return None
        return int(value)

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("set")
    async def quota_set(
        self,
        event: AstrMessageEvent,
        scope: str | None = None,
        a: str | None = None,
        b: str | None = None,
        c: str | None = None,
        d: str | None = None,
        e: str | None = None,
    ):
        scope = str(scope or "").lower()
        try:
            if scope == "user":
                if a is None or b is None or c is None:
                    raise ValueError
                ov = Override(SCOPE_PRIVATE, "", normalize_id(a),
                              self._parse_limit(b), self._parse_limit(c),
                              float(d) if d else None, True)
            elif scope == "group":
                if a is None or b is None or c is None:
                    raise ValueError
                ov = Override(SCOPE_GROUP, normalize_id(a), "",
                              self._parse_limit(b), self._parse_limit(c),
                              float(d) if d else None, True)
            elif scope == "members":  # 某群「群内每个成员」的默认配额
                if a is None or b is None or c is None:
                    raise ValueError
                ov = Override(SCOPE_MEMBER_DEFAULT, normalize_id(a), "",
                              self._parse_limit(b), self._parse_limit(c),
                              float(d) if d else None, True)
            elif scope == "userdef":  # 该用户在所有群的成员默认配额
                if a is None or b is None or c is None:
                    raise ValueError
                ov = Override(SCOPE_USER_DEFAULT, "", normalize_id(a),
                              self._parse_limit(b), self._parse_limit(c),
                              float(d) if d else None, True)
            elif scope == "member":
                if a is None or b is None or c is None or d is None:
                    raise ValueError
                ov = Override(SCOPE_MEMBER, normalize_id(a), normalize_id(b),
                              self._parse_limit(c), self._parse_limit(d),
                              float(e) if e else None, True)
            else:
                raise ValueError
        except ValueError:
            yield event.plain_result(
                "用法：/quota set user|group|member|members|userdef <...>"
                " <次数> <Token> [小时]\n详见 /quota help"
            )
            return
        if ov.window_hours is not None and ov.window_hours <= 0:
            yield event.plain_result("小时必须大于 0")
            return
        # 合并既有覆盖：只更新指令显式给出的字段，保留中台配置的回退与停用状态
        ov = merge_override(self._overrides.get(ov.key), ov)
        await self._set_override(ov)
        note = "" if ov.enabled else "\n⚠️ 该实体当前为「停用限制」状态（不限流），可在中台恢复"
        yield event.plain_result(
            f"✅ 已设置 {ov.scope} 覆盖：次数={ov.call_limit} Token={ov.token_limit}"
            f" 窗口={ov.window_hours or '默认'}{note}"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("clear")
    async def quota_clear(
        self,
        event: AstrMessageEvent,
        scope: str | None = None,
        a: str | None = None,
        b: str | None = None,
    ):
        target = self._scope_target(scope, a, b, allow_defaults=True)
        if target is None:
            yield event.plain_result(
                "用法：/quota clear user|group|member|members|userdef <...>"
            )
            return
        deleted = await self._clear_override(*target)
        yield event.plain_result("✅ 已删除覆盖" if deleted else "⚠️ 未找到对应覆盖")

    @quota_root.command("reset")
    async def quota_reset(
        self,
        event: AstrMessageEvent,
        scope: str | None = None,
        a: str | None = None,
        b: str | None = None,
        c: str | None = None,
    ):
        """``/quota reset me [long]`` 自助；其余分支需管理员（故不加权限装饰器）。"""
        word = str(scope or "").lower()
        args, window = self._split_window([a, b, c])
        if word == "me":
            async for result in self._self_reset(event, window):
                yield result
            return
        if not event.is_admin():
            yield event.plain_result("⚠️ 该指令仅管理员可用；重置自己请用 /quota reset me")
            return
        args = args + [None, None]
        target = self._scope_target(word, args[0], args[1])
        if target is None:
            yield event.plain_result(
                "用法：/quota reset me|user|group|member <...> [long]\n"
                "省略 long 时重置短窗口；带 long 时结束当前长窗口周期"
            )
            return
        await self._reset_entity(
            *target, window, ACTOR_ADMIN, normalize_id(event.get_sender_id())
        )
        yield event.plain_result(
            f"✅ 已重置该实体的{WINDOW_LABELS.get(window, window)}用量（不消耗自助次数，"
            "用量记录与历史统计不受影响）"
        )

    async def _self_reset(self, event: AstrMessageEvent, window: str):
        """自助重置：受 allow_self_reset 与前置条件约束，消耗一次重置次数。"""
        group_id = normalize_id(event.get_group_id())
        user_id = normalize_id(event.get_sender_id())
        if user_id:
            now = self._now()
            ban = self.is_banned(group_id, user_id, now)
            if ban is not None:
                event.stop_event()
                if bool(self.config.get("send_ban_message", True)):
                    yield event.plain_result(self._format_ban_tip(ban, now))
                return

        if not self._config_bool("allow_self_reset", False):
            yield event.plain_result("⚠️ 管理员未开放自助重置")
            return
        if not user_id:
            yield event.plain_result("⚠️ 无法识别你的用户 ID")
            return
        # 群共享池不可自助重置：群里作用于「你在本群」，私聊作用于「个人」
        key = scopes_for(group_id, user_id)[-1]
        now = self._now()
        await self.service.touch(key, now)
        quota = self._resolve_quota(key)
        calls, tokens, _oldest = self.aggregator.usage(key, now, quota.window_hours)
        refusal = check_self_reset(
            self.service.state_of(key),
            window,
            limited=self._entity_limited(key, quota, window),
            has_short_usage=calls > 0 or tokens > 0,
            days=quota.long_window_days,
            now=now,
        )
        if refusal is not None:
            yield event.plain_result(f"⚠️ {REFUSAL_MESSAGES[refusal]}")
            return
        state = await self.service.reset(key, window, ACTOR_SELF, user_id, consume=True)
        yield event.plain_result(
            f"✅ 已重置你的{WINDOW_LABELS.get(window, window)}用量，本周期还剩 "
            f"{allowance(state, window)} 次自助重置"
        )

    def _entity_limited(self, key: ScopeKey, quota: Quota, window: str) -> bool:
        """该实体在对应窗口上是否真的受限流约束（否则重置没有意义）。"""
        if not quota.enabled:
            return False
        if not self._is_subject_to_limit(key.group_id, key.user_id):
            return False
        if window == WINDOW_SHORT:
            return quota.call_limit >= 0 or quota.token_limit >= 0
        return quota.long_calls >= 0 or quota.long_tokens >= 0

    @staticmethod
    def _split_window(values: list[Any]) -> tuple[list[str], str]:
        """从指令尾部切出可选的窗口词（``short`` / ``long``），缺省为短窗口。"""
        args = [str(v) for v in values if v is not None and str(v) != ""]
        window = WINDOW_SHORT
        if args and args[-1].lower() in VALID_WINDOWS:
            window = args.pop().lower()
        return args, window

    @staticmethod
    def _scope_target(
        scope: str | None,
        a: str | None,
        b: str | None,
        *,
        allow_defaults: bool = False,
    ) -> tuple[str, str, str] | None:
        """把指令作用域词解析为覆盖键。

        ``members``（群内成员默认）与 ``userdef``（用户级成员默认）只是覆盖层、
        没有自己的用量事件，故仅 clear（``allow_defaults=True``）接受，reset 不接受。
        """
        scope = str(scope or "").lower()
        if scope == "user" and a:
            return (SCOPE_PRIVATE, "", normalize_id(a))
        if scope == "group" and a:
            return (SCOPE_GROUP, normalize_id(a), "")
        if scope == "member" and a and b:
            return (SCOPE_MEMBER, normalize_id(a), normalize_id(b))
        if allow_defaults and scope == "members" and a:
            return (SCOPE_MEMBER_DEFAULT, normalize_id(a), "")
        if allow_defaults and scope == "userdef" and a:
            return (SCOPE_USER_DEFAULT, "", normalize_id(a))
        return None

    # --- 指令：申请 / 发放 / 长窗口与重置次数设置 ------------------------

    @quota_root.command("request")
    async def quota_request(
        self,
        event: AstrMessageEvent,
        window: str | None = None,
        amount: str | None = None,
        r1: str | None = None,
        r2: str | None = None,
        r3: str | None = None,
        r4: str | None = None,
    ):
        """``/quota request short|long [数量] [理由]``：向管理员申请追加重置次数。"""
        usage = "用法：/quota request short|long [数量] [理由]"
        user_id = normalize_id(event.get_sender_id())
        group_id = normalize_id(event.get_group_id())
        if user_id:
            now = self._now()
            ban = self.is_banned(group_id, user_id, now)
            if ban is not None:
                event.stop_event()
                if bool(self.config.get("send_ban_message", True)):
                    yield event.plain_result(self._format_ban_tip(ban, now))
                return

        # 开关关闭时立即回复：不推进周期、不查库，避免被随手刷指令拖累
        if not self._config_bool("allow_reset_request", False):
            yield event.plain_result("⚠️ 管理员未开放重置次数申请")
            return
        try:
            count = 1 if amount is None else int(amount)
        except (TypeError, ValueError):
            yield event.plain_result(usage)
            return
        if not user_id:
            yield event.plain_result("⚠️ 无法识别你的用户 ID")
            return
        # 群池不可自助申请：群里作用于成员实体，私聊作用于私聊实体
        key = scopes_for(normalize_id(event.get_group_id()), user_id)[-1]
        now = self._now()
        await self.service.touch(key, now)
        try:
            pending = await self.service.pending_request(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 读取待审批申请失败: %s", exc)
            yield event.plain_result("⚠️ 读取申请状态失败，请稍后再试")
            return
        refusal = validate_new_request(
            str(window or "").lower(),
            count,
            allow=True,  # 开关已在入口处校验
            max_count=self._request_max_count(),
            cycle_active=self.service.is_active(key, now),
            has_pending=pending is not None,
        )
        if refusal is not None:
            yield event.plain_result(f"⚠️ {refusal}")
            return
        req = new_request(
            key.scope,
            key.group_id,
            key.user_id,
            str(window).lower(),
            count,
            self._join_words([r1, r2, r3, r4]),
            now,
            self.service.state_of(key).window_start or 0.0,
            str(getattr(event, "unified_msg_origin", "") or ""),
        )
        try:
            saved = await self.storage.insert_request(req)
        except Exception as exc:  # noqa: BLE001
            logger.error("quota_hub 提交申请失败: %s", exc)
            yield event.plain_result("⚠️ 提交申请失败，请稍后再试")
            return
        sent = await self._notify_admins(
            f"📥 新的重置次数申请\n{describe(saved)}\n"
            f"批准：/quota approve {saved.id} [数量]\n"
            f"拒绝：/quota deny {saved.id} [理由]"
        )
        # 未配置通知会话或推送失败时提示申请者，避免误以为管理员已经收到
        tip = "" if sent else "\n（未能通知到管理员，可提醒管理员用 /quota requests 查看）"
        yield event.plain_result(f"✅ 申请已提交：#{saved.id}，请等待管理员审批{tip}")

    @quota_root.command("requests")
    async def quota_requests(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return
        try:
            rows = await self.storage.list_requests(REQUEST_PENDING, 20)
        except Exception as exc:  # noqa: BLE001
            logger.error("quota_hub 读取申请列表失败: %s", exc)
            yield event.plain_result("⚠️ 读取申请列表失败，详情见服务端日志")
            return
        alive = []
        for req in rows:
            fresh = await self.service.refresh_request(req)  # 惰性作废已过期的申请
            if fresh is not None:
                alive.append(fresh)
        if not alive:
            yield event.plain_result("✅ 当前没有待审批的申请")
            return
        lines = ["📥 待审批申请："] + [f"· {describe(req)}" for req in alive]
        lines.append("批准：/quota approve <id> [数量]；拒绝：/quota deny <id> [理由]")
        yield event.plain_result("\n".join(lines))

    @quota_root.command("approve")
    async def quota_approve(
        self,
        event: AstrMessageEvent,
        request_id: str | None = None,
        amount: str | None = None,
    ):
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return
        rid = self._parse_request_id(request_id)
        if rid is None:
            yield event.plain_result("用法：<申请ID> 必须是整数，用 /quota requests 查看")
            return
        count: int | None = None
        if amount is not None:
            try:
                count = int(amount)
            except (TypeError, ValueError):
                yield event.plain_result("用法：/quota approve <id> [数量]")
                return
        try:
            decided = await self._approve_request(
                rid, count, normalize_id(event.get_sender_id())
            )
        except ValueError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        key = ScopeKey(decided.scope, decided.group_id, decided.user_id)
        state = self.service.state_of(key)
        yield event.plain_result(
            f"✅ 已批准 #{decided.id}：发放 {decided.granted} 次"
            f"{WINDOW_LABELS.get(decided.window, decided.window)}重置；"
            f"该实体现有短 {state.short_allowance} / 长 {state.long_allowance} 次"
        )

    @quota_root.command("deny")
    async def quota_deny(
        self,
        event: AstrMessageEvent,
        request_id: str | None = None,
        r1: str | None = None,
        r2: str | None = None,
        r3: str | None = None,
        r4: str | None = None,
    ):
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return
        rid = self._parse_request_id(request_id)
        if rid is None:
            yield event.plain_result("用法：<申请ID> 必须是整数，用 /quota requests 查看")
            return
        try:
            decided = await self._deny_request(
                rid,
                self._join_words([r1, r2, r3, r4]),
                normalize_id(event.get_sender_id()),
            )
        except ValueError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        yield event.plain_result(f"✅ 已拒绝 #{decided.id}")

    @quota_root.command("grant")
    async def quota_grant(
        self,
        event: AstrMessageEvent,
        scope: str | None = None,
        a: str | None = None,
        b: str | None = None,
        c: str | None = None,
        d: str | None = None,
    ):
        """``/quota grant user|member <...> <短窗次数> <长窗次数>``：为当前周期发放。"""
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return
        usage = (
            "用法：/quota grant user <用户ID> <短窗次数> <长窗次数>\n"
            "　　　/quota grant member <群号> <用户ID> <短窗次数> <长窗次数>"
        )
        word = str(scope or "").lower()
        try:
            if word == "user" and a is not None and b is not None and c is not None:
                key = ScopeKey(SCOPE_PRIVATE, "", normalize_id(a))
                short, long = int(b), int(c)
            elif word == "member" and None not in (a, b, c, d):
                key = ScopeKey(SCOPE_MEMBER, normalize_id(a), normalize_id(b))
                short, long = int(c), int(d)
            else:
                raise ValueError
            if short < 0 or long < 0:
                raise ValueError
        except (TypeError, ValueError):
            yield event.plain_result(usage)
            return
        try:
            # 开窗不补满、到期才补满：没有周期时发放的次数会在到期补满时被抹掉，
            # 故 _grant_to 在没有进行中的周期时直接抛 ValueError
            state = await self._grant_to(key, short, long)
        except ValueError as exc:
            yield event.plain_result(f"⚠️ {exc}")
            return
        yield event.plain_result(
            f"✅ 已发放：短窗口 +{short}、长窗口 +{long}；该实体现有短 "
            f"{state.short_allowance} / 长 {state.long_allowance} 次"
        )

    @quota_root.command("setlong")
    async def quota_setlong(
        self,
        event: AstrMessageEvent,
        scope: str | None = None,
        a: str | None = None,
        b: str | None = None,
        c: str | None = None,
        d: str | None = None,
        e: str | None = None,
    ):
        """只写长窗口三个字段，其余覆盖字段（含短窗口、回退）保持不变。"""
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return
        usage = (
            "用法：/quota setlong user|group|members|userdef <ID> <次数> <Token> [天数]\n"
            "　　　/quota setlong member <群号> <用户ID> <次数> <Token> [天数]\n"
            "　　　次数/Token 填 -1 表示不限，天数省略则沿用当前设置"
        )
        word = str(scope or "").lower()
        if word == "member":
            target, values = self._scope_target(word, a, b), (c, d, e)
        else:
            target, values = (
                self._scope_target(word, a, None, allow_defaults=True),
                (b, c, d),
            )
        calls, tokens, days = values
        if target is None or calls is None or tokens is None:
            yield event.plain_result(usage)
            return
        try:
            call_limit, token_limit = int(calls), int(tokens)
            day_value = None if days is None or str(days) == "" else float(days)
        except (TypeError, ValueError):
            yield event.plain_result(usage)
            return
        if call_limit < -1 or token_limit < -1:
            yield event.plain_result("次数 / Token 需大于等于 -1（-1 表示不限）")
            return
        if day_value is not None and day_value <= 0:
            yield event.plain_result("天数必须大于 0")
            return
        ov = Override(
            *target,
            long_calls=call_limit,
            long_tokens=token_limit,
            long_window_days=day_value,
        )
        fields = ["long_calls", "long_tokens"]
        if day_value is not None:
            fields.append("long_window_days")
        merged = merge_fields(self._overrides.get(ov.key), ov, fields)
        await self._set_override(merged)
        yield event.plain_result(
            f"✅ 已设置 {merged.scope} 长窗口：次数={merged.long_calls}"
            f" Token={merged.long_tokens} 天数={merged.long_window_days or '默认'}"
        )

    @quota_root.command("setreset")
    async def quota_setreset(
        self,
        event: AstrMessageEvent,
        scope: str | None = None,
        a: str | None = None,
        b: str | None = None,
        c: str | None = None,
        d: str | None = None,
    ):
        """只写「每周期自助重置次数」两个字段（群作用域没有意义，不接受）。"""
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return
        usage = (
            "用法：/quota setreset user|members|userdef <ID> <短窗次数> <长窗次数>\n"
            "　　　/quota setreset member <群号> <用户ID> <短窗次数> <长窗次数>"
        )
        word = str(scope or "").lower()
        if word == "member":
            target, values = self._scope_target(word, a, b), (c, d)
        elif word in ("user", "members", "userdef"):
            target, values = (
                self._scope_target(word, a, None, allow_defaults=True),
                (b, c),
            )
        else:
            target, values = None, (None, None)
        short_raw, long_raw = values
        if target is None or short_raw is None or long_raw is None:
            yield event.plain_result(usage)
            return
        try:
            short, long = int(short_raw), int(long_raw)
        except (TypeError, ValueError):
            yield event.plain_result(usage)
            return
        if short < 0 or long < 0:
            yield event.plain_result("重置次数不能为负数")
            return
        ov = Override(*target, self_reset_short=short, self_reset_long=long)
        merged = merge_fields(
            self._overrides.get(ov.key), ov, ["self_reset_short", "self_reset_long"]
        )
        await self._set_override(merged)
        yield event.plain_result(
            f"✅ 已设置 {merged.scope} 每周期自助重置次数：短窗口 {short} 次、"
            f"长窗口 {long} 次（下个自然周期生效）"
        )

    @quota_root.command("session")
    async def quota_session(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        yield event.plain_result(
            f"📎 当前会话标识：\n{origin}\n"
            "把它填进配置项「申请通知会话列表」即可在此会话接收申请通知。"
        )

    # --- 申请辅助 / 通知 ------------------------------------------------

    @staticmethod
    def _parse_request_id(value: Any) -> int | None:
        """指令里的 ``<申请ID>``：非整数返回 ``None``，由调用方回用法提示。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    # --- 共用业务方法（指令与数据中台走同一条路径）----------------------
    # 失败一律抛 ``ValueError(中文原因)``：指令层转 ``⚠️ {exc}``，API 层转 ``_err``。

    async def _approve_request(
        self, request_id: int, amount: int | None, admin_id: str
    ) -> Any:
        """批准申请：发放次数到申请实体的当前周期，并把结果推给申请者。"""
        decided = await self.request_service.approve(request_id, amount, admin_id)
        await self._notify_requester(decided)
        return decided

    async def _deny_request(self, request_id: int, note: str, admin_id: str) -> Any:
        """拒绝申请，并把结果推给申请者。"""
        decided = await self.request_service.deny(request_id, note, admin_id)
        await self._notify_requester(decided)
        return decided

    async def _grant_to(self, key: ScopeKey, short: int, long: int) -> Any:
        """为实体的当前周期发放重置次数（没有进行中的周期时抛 ``ValueError``）。"""
        return await self.request_service.grant_to(key, short, long)

    @staticmethod
    def _join_words(values: list[Any], limit: int = 100) -> str:
        """把指令尾部的若干可选词拼成一句（理由 / 备注）。"""
        text = " ".join(str(v) for v in values if v is not None and str(v) != "")
        return text.strip()[:limit]

    async def _push(self, origin: str, text: str) -> bool:
        """主动推送一条文本消息；失败只记 warning（部分平台不支持主动推送）。

        ``context.send_message`` 在找不到匹配平台时**返回 False 而不抛异常**，
        因此不能只靠 try/except 判断送达：返回假值同样视为未送达，否则会把
        「没送出去」误记成已通知（申请者再也看不到审批结果）。
        """
        if not origin:
            return False
        try:
            delivered = await self.context.send_message(origin, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 推送到会话 %s 失败: %s", origin, exc)
            return False
        if not delivered:
            logger.warning(
                "quota_hub 推送到会话 %s 未送达：未找到匹配的平台（请检查会话标识）",
                origin,
            )
            return False
        return True

    async def _notify_admins(self, text: str) -> int:
        """逐个通知配置的管理员会话，返回真正送达的数量（每个目标独立 try/except）。"""
        sessions = self.config.get("request_notify_sessions") or []
        if not isinstance(sessions, list):
            logger.warning(
                "quota_hub 配置项 request_notify_sessions 不是数组（实际 %s），已忽略",
                type(sessions).__name__,
            )
            return 0
        sent = 0
        for origin in sessions:
            if await self._push(str(origin).strip(), text):
                sent += 1
        return sent

    async def _notify_requester(self, req: Any) -> None:
        """把审批结果推给申请者；推送失败则保持未送达，留待 /quota me 展示。"""
        if await self._push(req.requester_origin, decision_text(req)):
            await self._mark_request_notified(req)

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("remark")
    async def quota_remark(
        self,
        event: AstrMessageEvent,
        group_id: Any = None,
        remark: Any = None,
    ):
        if group_id is None or not str(group_id).strip():
            yield event.plain_result(
                "用法：/quota remark <群号> [备注]\n"
                "备注留空则清除；含空格的备注请在数据中台设置。"
            )
            return
        text = "" if remark is None else str(remark).strip()[:64]
        await self._set_remark("group", normalize_id(group_id), text)
        action = "设置" if text else "清除"
        suffix = f"：{text}" if text else ""
        yield event.plain_result(f"✅ 已{action}群 {group_id} 的备注{suffix}")

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("monitor")
    async def quota_monitor(
        self,
        event: AstrMessageEvent,
        action: str | None = None,
        kind: str | None = None,
        ident: str | None = None,
    ):
        async for result in self._mutate_list(
            event, action, kind, ident, "monitored_groups", "monitored_users"
        ):
            yield result


    # --- 封禁指令 -------------------------------------------------------

    @staticmethod
    def _extract_target_id(event: AstrMessageEvent, candidate: Any) -> str:
        """从参数或消息中的 At 元件提取 QQ 号 / 用户 ID。"""
        if candidate is not None:
            at_qq = (
                getattr(candidate, "qq", None)
                or getattr(candidate, "target", None)
                or getattr(candidate, "user_id", None)
            )
            if at_qq:
                return normalize_id(at_qq)

            raw = str(candidate).strip()
            m = re.search(r"qq=(\d+)", raw)
            if m:
                return m.group(1)
            if raw.startswith("@") and raw[1:].isdigit():
                return raw[1:]
            if raw.isdigit():
                return raw

        msg_obj = getattr(event, "message_obj", None)
        components = getattr(msg_obj, "message", None)
        if components and isinstance(components, (list, tuple)):
            for comp in components:
                qq = (
                    getattr(comp, "qq", None)
                    or getattr(comp, "target", None)
                    or getattr(comp, "user_id", None)
                )
                if qq:
                    return normalize_id(qq)

        return normalize_id(candidate)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("ban")
    async def cmd_ban(
        self,
        event: AstrMessageEvent,
        target: Any = None,
        duration: Any = None,
        r1: Any = None,
        r2: Any = None,
        r3: Any = None,
        r4: Any = None,
    ):
        """/ban <QQ号或@成员> <时间（如1d）> [理由] —— 禁止成员与 LLM 对话。"""
        async for res in self._handle_ban(event, target, duration, [r1, r2, r3, r4]):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("unban")
    async def cmd_unban(
        self,
        event: AstrMessageEvent,
        target: Any = None,
        r1: Any = None,
    ):
        """/unban <QQ号或@成员> —— 解除成员的 LLM 对话封禁。"""
        async for res in self._handle_unban(event, target, r1):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("ban")
    async def quota_ban(
        self,
        event: AstrMessageEvent,
        target: Any = None,
        duration: Any = None,
        r1: Any = None,
        r2: Any = None,
        r3: Any = None,
        r4: Any = None,
    ):
        async for res in self._handle_ban(event, target, duration, [r1, r2, r3, r4]):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("unban")
    async def quota_unban(
        self,
        event: AstrMessageEvent,
        target: Any = None,
        r1: Any = None,
    ):
        async for res in self._handle_unban(event, target, r1):
            yield res

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("banlist")
    async def quota_banlist(self, event: AstrMessageEvent):
        async for res in self._handle_banlist(event):
            yield res

    async def _handle_ban(
        self,
        event: AstrMessageEvent,
        target: Any = None,
        duration: Any = None,
        extra_args: list[Any] | None = None,
    ):
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return

        target_str = str(target).strip() if target is not None else ""
        if not target_str or target_str.lower() in ("help", "-h", "--help"):
            yield event.plain_result(
                "用法：/ban <QQ号或@成员> <时间（如1d）> [理由]\n"
                "示例：\n"
                "  /ban 123456789 1d\n"
                "  /ban 123456789 2h 违规发言\n"
                "  /ban @成员 30m\n"
                "时间单位：s(秒), m(分), h(时), d(天), w(周), 0(解封), 永久\n"
                "查看封禁：/ban list  |  解封：/unban <QQ号>"
            )
            return

        if target_str.lower() == "list":
            async for res in self._handle_banlist(event):
                yield res
            return

        target_id = ""
        dur_seconds: float | None = None
        group_id = normalize_id(event.get_group_id())
        reason = ""
        extra_args = extra_args or []

        test_dur = parse_duration(duration)
        if test_dur is not None:
            target_id = self._extract_target_id(event, target)
            dur_seconds = test_dur
            reason = self._join_words(extra_args)
        elif extra_args and extra_args[0] is not None:
            test_dur_2 = parse_duration(extra_args[0])
            if test_dur_2 is not None:
                group_id = normalize_id(target)
                target_id = self._extract_target_id(event, duration)
                dur_seconds = test_dur_2
                reason = self._join_words(extra_args[1:])

        if dur_seconds is None or not target_id:
            yield event.plain_result(
                "⚠️ 时间格式不正确或未识别到 QQ 号。\n"
                "用法：/ban <QQ号或@成员> <时间（如1d）> [理由]\n"
                "示例：/ban 123456789 1d，支持单位：s(秒), m(分), h(时), d(天), w(周), 永久"
            )
            return

        if dur_seconds == 0.0:
            async for res in self._handle_unban(event, target_id):
                yield res
            return

        admin_id = normalize_id(event.get_sender_id())
        if target_id == admin_id:
            yield event.plain_result("⚠️ 不能封禁自己")
            return

        is_global = False
        if "global" in reason.lower() or "全局" in reason:
            is_global = True
            group_id = ""
            reason = reason.replace("global", "").replace("全局", "").strip()

        now = int(self._now())
        if dur_seconds < 0:
            expire_at = -1
            time_desc = "永久"
        else:
            expire_at = int(now + dur_seconds)
            time_desc = f"至 {self._fmt_time_full(expire_at)}（时长 {format_duration(dur_seconds)}）"

        ban_rec = BanRecord(
            group_id=group_id,
            user_id=target_id,
            banned_at=now,
            expire_at=expire_at,
            banned_by=admin_id,
            reason=reason,
        )

        self._banned_users[(group_id, target_id)] = ban_rec
        try:
            await self.storage.set_ban(
                group_id, target_id, now, expire_at, admin_id, reason
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("quota_hub 保存封禁记录失败: %s", exc)
            yield event.plain_result(f"⚠️ 保存封禁记录失败: {exc}")
            return

        scope_desc = f"群 {group_id} 成员" if group_id else "用户（全局）"
        reason_desc = f"，理由：{reason}" if reason else ""
        yield event.plain_result(
            f"✅ 已禁止 {scope_desc} {target_id} 与 LLM 对话及使用任何指令、唤醒 {time_desc}{reason_desc}"
        )

    async def _handle_unban(
        self,
        event: AstrMessageEvent,
        target: Any = None,
        extra_arg: Any = None,
    ):
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return

        target_str = str(target).strip() if target is not None else ""
        if not target_str or target_str.lower() in ("help", "-h", "--help"):
            yield event.plain_result("用法：/unban <QQ号或@成员>\n　　　/unban <群号> <QQ号或@成员>")
            return

        group_id = normalize_id(event.get_group_id())
        if extra_arg and str(extra_arg).strip():
            group_id = normalize_id(target)
            target_id = self._extract_target_id(event, extra_arg)
        else:
            target_id = self._extract_target_id(event, target)

        if not target_id:
            yield event.plain_result("⚠️ 无法识别目标 QQ 号或成员")
            return

        removed_any = False
        if group_id:
            if (group_id, target_id) in self._banned_users:
                self._banned_users.pop((group_id, target_id), None)
                removed_any = True
            db_rem = await self.storage.remove_ban(group_id, target_id)
            if db_rem:
                removed_any = True

        if ("", target_id) in self._banned_users:
            self._banned_users.pop(("", target_id), None)
            removed_any = True
        db_rem_global = await self.storage.remove_ban("", target_id)
        if db_rem_global:
            removed_any = True

        if removed_any:
            yield event.plain_result(f"✅ 已解除 {target_id} 的 LLM 对话封禁（已恢复指令与唤醒）")
        else:
            yield event.plain_result(f"⚠️ 未找到 {target_id} 的有效封禁记录")

    async def _handle_banlist(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result(ADMIN_ONLY_TIP)
            return

        now = self._now()
        active_bans: list[BanRecord] = []
        for k, b in list(self._banned_users.items()):
            if b.is_expired(now):
                self._banned_users.pop(k, None)
                asyncio.create_task(self._safe_remove_ban(k[0], k[1]))
            else:
                active_bans.append(b)

        if not active_bans:
            yield event.plain_result("✅ 当前没有被禁止对话的成员")
            return

        lines = ["📋 当前 LLM 对话封禁名单（含指令与唤醒）："]
        for b in active_bans:
            scope_desc = f"群 {b.group_id} 成员" if b.group_id else "全局"
            if b.is_permanent:
                dur_str = "永久封禁"
            else:
                dur_str = f"至 {self._fmt_time_full(b.expire_at)}（剩余约 {format_duration(b.expire_at - now)}）"
            reason_str = f"，理由：{b.reason}" if b.reason else ""
            lines.append(f"· {b.user_id}（{scope_desc}）：{dur_str}{reason_str}")

        lines.append("解封指令：/unban <QQ号>")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("exempt")
    async def quota_exempt(
        self,
        event: AstrMessageEvent,
        action: str | None = None,
        kind: str | None = None,
        ident: str | None = None,
    ):
        async for result in self._mutate_list(
            event, action, kind, ident, "exempt_groups", "exempt_users"
        ):
            yield result

    async def _mutate_list(
        self,
        event: AstrMessageEvent,
        action: Any,
        kind: Any,
        ident: Any,
        group_key: str,
        user_key: str,
    ):
        action = str(action or "").lower()
        kind = str(kind or "").lower()
        ident = normalize_id(ident)
        if action not in ("add", "del") or kind not in ("group", "user") or not ident:
            yield event.plain_result("用法：add|del group|user <ID>")
            return
        key = group_key if kind == "group" else user_key
        items = self._id_set(key)
        if action == "add":
            items.add(ident)
        else:
            items.discard(ident)
        self.config[key] = sorted(items)
        self._save_config()
        yield event.plain_result(f"✅ 已更新 {key}：{sorted(items)}")

    @filter.permission_type(PermissionType.ADMIN)
    @quota_root.command("stats")
    async def quota_stats(self, event: AstrMessageEvent, days: str | None = None):
        if days is not None:
            yield await self._history_stats_result(event, days)
            return
        window_hours = self._window_hours()
        cutoff = self._now() - window_hours * 3600
        overview = await self.storage.overview(cutoff)
        top_groups = (await self.storage.group_totals(cutoff))[:5]
        lines = [
            f"📊 最近 {self._fmt_hours(window_hours)} 小时概览：",
            f"· 总调用 {overview['total_calls']}，总 Token {overview['total_tokens']}",
            f"· 活跃群 {overview['active_groups']}，活跃用户 {overview['active_users']}",
        ]
        if top_groups:
            lines.append("· Top 群（Token）：")
            for gid, calls, tokens in top_groups:
                lines.append(f"   - {gid}: {calls} 次 / {tokens} token")
        yield event.plain_result("\n".join(lines))

    async def _history_stats_result(
        self, event: AstrMessageEvent, days: str
    ) -> MessageEventResult:
        """``/quota stats <天数>`` 的历史统计文本（数据来自小时级汇总+未固化明细）。"""
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= 365:
            return event.plain_result("用法：/quota stats [天数]，天数为 1-365 的整数")
        now = self._now()
        stats = await self.storage.history_stats(now - n * SECONDS_PER_DAY, now)
        totals = stats["totals"]
        active_days = max(1, totals["active_days"])
        lines = [
            f"📊 近 {n} 天历史统计：",
            f"· 总调用 {totals['calls']}，总 Token {totals['tokens']}",
            f"· 日均调用 {totals['calls'] // active_days}，"
            f"日均 Token {totals['tokens'] // active_days}"
            f"（按 {totals['active_days']} 个活跃日计）",
            f"· 活跃群 {totals['active_groups']}，活跃用户 {totals['active_users']}",
        ]
        top_groups = stats["top_groups"][:5]
        if top_groups:
            lines.append("· Top 群（Token）：")
            for gid, calls, tokens in top_groups:
                label = self._group_remarks.get(gid) or self._group_names.get(gid)
                shown = f"{label}({gid})" if label else gid
                lines.append(f"   - {shown}: {calls} 次 / {tokens} token")
        top_models = [m for m in stats["models"] if m[0]][:3]
        if top_models:
            lines.append("· Top 模型（Token）：")
            for model, calls, tokens in top_models:
                lines.append(f"   - {model}: {calls} 次 / {tokens} token")
        return event.plain_result("\n".join(lines))
