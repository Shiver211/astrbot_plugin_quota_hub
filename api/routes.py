"""数据中台 Web API 处理器（以 Mixin 形式注入主插件类）。

统一返回信封：``{"success": bool, "data": ..., "error": str | None}``。
所有读取走 SQLite（落盘延迟 ≤1s，对统计可接受）；动态调整写 SQLite/配置并同步内存。
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from quart import request

from astrbot.api import logger

try:  # 部署时作为 data.plugins.<name>.api.routes 加载
    from ..core.cycles import ACTOR_DASHBOARD, CycleState
    from ..core.models import (
        OVERRIDE_SCOPES,
        SCOPE_GROUP,
        SCOPE_MEMBER,
        SCOPE_MEMBER_DEFAULT,
        SCOPE_PRIVATE,
        SCOPE_USER_DEFAULT,
        VALID_SCOPES,
        VALID_WINDOWS,
        WINDOW_SHORT,
        Override,
        Quota,
        ScopeKey,
        normalize_id,
    )
    from ..core.quota import merge_fields
    from ..core.requests import REQUEST_PENDING, ResetRequest
    from ..core.windows import SECONDS_PER_DAY, reset_in_seconds
except ImportError:  # 插件根目录在 sys.path 时的兜底
    from core.cycles import ACTOR_DASHBOARD, CycleState
    from core.models import (
        OVERRIDE_SCOPES,
        SCOPE_GROUP,
        SCOPE_MEMBER,
        SCOPE_MEMBER_DEFAULT,
        SCOPE_PRIVATE,
        SCOPE_USER_DEFAULT,
        VALID_SCOPES,
        VALID_WINDOWS,
        WINDOW_SHORT,
        Override,
        Quota,
        ScopeKey,
        normalize_id,
    )
    from core.quota import merge_fields
    from core.requests import REQUEST_PENDING, ResetRequest
    from core.windows import SECONDS_PER_DAY, reset_in_seconds

# 取值解析与序列化都在 serializers（纯函数，无 astrbot / quart 依赖）
from .serializers import (
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

# 一次取全量待审批申请的上限：每个实体同时只能有一条待审批，故足以覆盖列表页
PENDING_SCAN_LIMIT = 1000

# AstrBot 标准响应信封：{status, message, data}。Dashboard bridge 在 status==ok 时把
# data 字段直接 resolve 给前端，status==error 时以 message reject（见 plugin_page_bridge.js）。
def _ok(data: Any = None) -> dict:
    return {"status": "ok", "message": None, "data": data if data is not None else {}}


def _err(message: str) -> dict:
    return {"status": "error", "message": message, "data": None}


def _fail(message: str, exc: Exception) -> dict:
    """记录完整异常到服务端日志，仅向前端返回通用文案（避免泄露 db 路径等内部细节）。"""
    logger.error("quota_hub API 异常: %s", exc, exc_info=True)
    return _err(f"{message}，详情见服务端日志")


class QuotaApiMixin:
    """Web API 处理器集合。依赖主类提供的属性/方法见各处 ``self.*`` 调用。"""

    # 由主类在 __init__ 中调用，集中注册所有 Web API。
    def _register_web_apis(self, plugin_name: str) -> None:
        api = (
            ("overview", self.api_overview, ["GET"], "概览统计"),
            ("history", self.api_history, ["GET"], "历史统计"),
            ("groups", self.api_groups, ["GET"], "各群用量"),
            ("users", self.api_users, ["GET"], "各用户用量"),
            ("members", self.api_members, ["GET"], "群内成员用量"),
            ("overrides", self.api_overrides, ["GET"], "覆盖列表"),
            ("override", self.api_set_override, ["POST"], "设置覆盖"),
            ("override/delete", self.api_delete_override, ["POST"], "删除覆盖"),
            ("reset", self.api_reset, ["POST"], "重置用量"),
            ("reset_log", self.api_reset_log, ["GET"], "重置日志"),
            ("requests", self.api_requests, ["GET"], "重置次数申请列表"),
            ("request/approve", self.api_approve_request, ["POST"], "批准申请"),
            ("request/deny", self.api_deny_request, ["POST"], "拒绝申请"),
            ("grant", self.api_grant, ["POST"], "发放重置次数"),
            ("remark", self.api_set_remark, ["POST"], "设置备注"),
            ("lists", self.api_get_lists, ["GET"], "监控/白名单"),
            ("lists", self.api_save_lists, ["POST"], "保存名单"),
            ("providers", self.api_providers, ["GET"], "可用模型供应商"),
            ("config", self.api_get_config, ["GET"], "获取配置"),
            ("config", self.api_save_config, ["POST"], "保存配置"),
        )
        for endpoint, handler, methods, desc in api:
            self.context.register_web_api(
                f"/{plugin_name}/{endpoint}", handler, methods, desc
            )

    # --- 中台读取 -------------------------------------------------------

    def _markers_after(
        self, keys: Iterable[ScopeKey], cutoff: float
    ) -> dict[ScopeKey, float]:
        """筛出重置标记晚于窗口起点的实体及其标记时刻（其余无需重新取数）。"""
        marked: dict[ScopeKey, float] = {}
        for key in keys:
            marker = self.service.state_of(key).short_marker
            if marker > cutoff:
                marked[key] = marker
        return marked

    async def _usage_since_markers(
        self, keys: Iterable[ScopeKey], cutoff: float
    ) -> dict[ScopeKey, tuple[int, int]]:
        """有标记的实体改按「标记之后」的口径取数，一次批量查询完成。

        SQL 的 SUM 只按窗口截止过滤，而限流判定只统计重置标记之后的用量
        （见 docs/adr/0001）；两者不一致会让中台显示的用量高于实际生效值。
        列表页每行单独 await 会退化成 N+1 次查询，故一次取齐。
        """
        marked = self._markers_after(keys, cutoff)
        if not marked:
            return {}
        rows = await self.storage.usage_since_markers(
            [(k.scope, k.group_id, k.user_id, ts) for k, ts in marked.items()]
        )
        return {key: rows[(key.scope, key.group_id, key.user_id)] for key in marked}

    async def _marked_deltas(
        self, keys: Iterable[ScopeKey], cutoff: float
    ) -> dict[ScopeKey, tuple[int, int]]:
        """有标记的实体在「窗口起点 ~ 标记」之间的用量，供跨作用域合计扣减。

        合计（如「按人」表）不能直接用单实体标记后口径替换（会把其它作用域的
        用量一并丢掉），只减去该实体被标记切走的这一段。两次批量查询即可，
        不随行数增长。
        """
        marked = self._markers_after(keys, cutoff)
        if not marked:
            return {}
        before = await self.storage.usage_since_markers(
            [(k.scope, k.group_id, k.user_id, cutoff) for k in marked]
        )
        after = await self.storage.usage_since_markers(
            [(k.scope, k.group_id, k.user_id, ts) for k, ts in marked.items()]
        )
        deltas: dict[ScopeKey, tuple[int, int]] = {}
        for key in marked:
            ident = (key.scope, key.group_id, key.user_id)
            deltas[key] = (
                before[ident][0] - after[ident][0],
                before[ident][1] - after[ident][1],
            )
        return deltas

    async def _pending_by_key(self) -> dict[ScopeKey, ResetRequest]:
        """一次取全量待审批申请并按实体建索引（列表页避免逐行 SELECT）。每个实体
        同时只能有一条待审批申请，故按键覆盖不丢数据；查询失败只记日志并返回空表
        （申请徽标缺失不应让整张用量表打不开）。
        """
        try:
            rows = await self.storage.list_requests(REQUEST_PENDING, PENDING_SCAN_LIMIT)
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota_hub 读取待审批申请失败: %s", exc)
            return {}
        return {ScopeKey(r.scope, r.group_id, r.user_id): r for r in rows}

    async def _cycle_of(
        self,
        key: ScopeKey,
        quota: Quota,
        now: float,
        pending: dict[ScopeKey, ResetRequest] | None = None,
    ) -> dict:
        """某实体的周期快照。取数前先 ``touch``（到期是惰性的）；``pending`` 是一次
        取齐的待审批索引，只有命中的行才做 ``refresh_request``（内部再次推进到期并
        落盘作废），未命中的行不额外查库。
        """
        state = await self.service.touch(key, now)
        req = (pending or {}).get(key)
        if req is not None:
            req = await self.service.refresh_request(req)
            state = self.service.state_of(key, quota)
        long_calls, long_tokens = self.aggregator.long_usage(key)
        return serialize_cycle(
            state, long_calls, long_tokens, self.service.long_end(key, quota), req
        )

    async def _entity_cycle(
        self, key: ScopeKey, quota: Quota, state: CycleState
    ) -> dict:
        """已知最新状态的实体周期快照（写操作的返回值）；待审批申请单独查一次。"""
        pending = await self.service.pending_request(key)
        long_calls, long_tokens = self.aggregator.long_usage(key)
        return serialize_cycle(
            state, long_calls, long_tokens, self.service.long_end(key, quota), pending
        )

    async def api_overview(self) -> dict:
        try:
            window_hours = self._window_hours()
            cutoff = self._now() - window_hours * 3600
            overview = await self.storage.overview(cutoff)
            bucket = max(60, int(window_hours * 3600 / 48))
            series = await self.storage.timeseries(cutoff, bucket)
            top_groups = (await self.storage.group_totals(cutoff))[:10]
            top_users = (await self.storage.user_totals(cutoff))[:10]
            return _ok(
                {
                    "window_hours": window_hours,
                    "enforcement_mode": str(self.config.get("enforcement_mode", "allowlist")),
                    "enabled": bool(self.config.get("enabled", True)),
                    "dropped": self.writer.dropped_count(),
                    "overview": overview,
                    "timeseries": [
                        {"ts": ts, "calls": c, "tokens": t} for ts, c, t in series
                    ],
                    "top_groups": [
                        {"group_id": g, "calls": c, "tokens": t}
                        for g, c, t in top_groups
                    ],
                    "top_users": [
                        {"user_id": u, "calls": c, "tokens": t}
                        for u, c, t in top_users
                    ],
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("获取概览失败", exc)

    async def api_history(self) -> dict:
        """长周期历史统计：小时级汇总（长期保留）+ 未固化明细合并口径。"""
        try:
            # 上限 3650 有意宽于 UI 暴露的 24h/7/30/90/365，供脚本直连取更长范围
            raw_days = opt_int(request.args.get("days"))
            days = 30 if raw_days is None else raw_days
            days = min(max(days, 1), 3650)
            now = self._now()
            # 短范围（≤2 天）额外带按小时序列，供 24 小时视图画趋势
            stats = await self.storage.history_stats(
                now - days * SECONDS_PER_DAY, now, with_hourly=days <= 2
            )
            return _ok(
                {
                    "days": days,
                    # 服务端本地时区的「今天」与当前时刻：前端补零日历/小时轴的锚点
                    "end_day": time.strftime("%Y-%m-%d", time.localtime(now)),
                    "end_ts": int(now),
                    "totals": stats["totals"],
                    "daily": [
                        {"day": d, "calls": c, "tokens": t}
                        for d, c, t in stats["daily"]
                    ],
                    "hourly": [
                        {"ts": ts, "calls": c, "tokens": t}
                        for ts, c, t in stats["hourly"]
                    ],
                    "top_groups": [
                        {
                            "group_id": g,
                            "name": self._group_names.get(g, ""),
                            "remark": self._group_remarks.get(g, ""),
                            "calls": c,
                            "tokens": t,
                        }
                        for g, c, t in stats["top_groups"]
                    ],
                    "top_users": [
                        {
                            "user_id": u,
                            "name": self._user_names.get(u, ""),
                            "calls": c,
                            "tokens": t,
                        }
                        for u, c, t in stats["top_users"]
                    ],
                    "models": [
                        {"model": m, "calls": c, "tokens": t}
                        for m, c, t in stats["models"]
                    ],
                    "platforms": [
                        {"platform": p, "calls": c, "tokens": t}
                        for p, c, t in stats["platforms"]
                    ],
                    # weekday 为 SQLite %w 口径：0=周日 … 6=周六
                    "heatmap": [
                        {"weekday": w, "hour": h, "calls": c, "tokens": t}
                        for w, h, c, t in stats["heatmap"]
                    ],
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("获取历史统计失败", exc)

    async def api_groups(self) -> dict:
        try:
            await self._refresh_group_names()  # 节流：经协议批量刷新群名
            window_hours = self._window_hours()
            now = self._now()
            cutoff = now - window_hours * 3600
            totals = {g: (c, t) for g, c, t in await self.storage.group_totals(cutoff)}
            # 并入仅有覆盖（含群成员默认覆盖）、暂无用量的群
            for key in self._overrides:
                if (
                    key.scope in (SCOPE_GROUP, SCOPE_MEMBER_DEFAULT)
                    and key.group_id not in totals
                ):
                    totals[key.group_id] = (0, 0)
            # 先一次性取齐「被重置标记切过」的实体用量，避免逐行查库（N+1）
            since_marker = await self._usage_since_markers(
                [ScopeKey(SCOPE_GROUP, gid, "") for gid in totals], cutoff
            )
            pending = await self._pending_by_key()
            rows = []
            for gid, (calls, tokens) in totals.items():
                key = ScopeKey(SCOPE_GROUP, gid, "")
                calls, tokens = since_marker.get(key, (calls, tokens))
                ov = self._overrides.get(key)
                quota = self._resolve_quota(key)
                rows.append(
                    {
                        "group_id": gid,
                        "name": self._group_names.get(gid, ""),
                        "remark": self._group_remarks.get(gid, ""),
                        "calls": calls,
                        "tokens": tokens,
                        "quota": serialize_quota(quota),
                        "override": serialize_override(ov) if ov else None,
                        "has_override": ov is not None,
                        "has_member_default": (
                            ScopeKey(SCOPE_MEMBER_DEFAULT, gid, "") in self._overrides
                        ),
                        "cycle": await self._cycle_of(key, quota, now, pending),
                    }
                )
            rows.sort(key=lambda r: (r["tokens"], r["calls"]), reverse=True)
            return _ok(
                {
                    "now": now,
                    "window_hours": window_hours,
                    "defaults": serialize_quota(self._defaults_for(SCOPE_GROUP)),
                    "groups": rows,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("获取群用量失败", exc)

    async def api_users(self) -> dict:
        try:
            window_hours = self._window_hours()
            now = self._now()
            cutoff = now - window_hours * 3600
            totals = {u: (c, t) for u, c, t in await self.storage.user_totals(cutoff)}
            # 并入仅有覆盖（含用户级成员默认覆盖）、暂无用量的用户
            for key in self._overrides:
                if (
                    key.scope in (SCOPE_PRIVATE, SCOPE_USER_DEFAULT)
                    and key.user_id not in totals
                ):
                    totals[key.user_id] = (0, 0)
            # 「按人」是私聊 + 所有群的合计，只扣私聊实体标记前的那一段；批量取齐
            deltas = await self._marked_deltas(
                [ScopeKey(SCOPE_PRIVATE, "", uid) for uid in totals], cutoff
            )
            pending = await self._pending_by_key()
            rows = []
            for uid, (calls, tokens) in totals.items():
                key = ScopeKey(SCOPE_PRIVATE, "", uid)
                drop_calls, drop_tokens = deltas.get(key, (0, 0))
                calls = max(0, calls - drop_calls)
                tokens = max(0, tokens - drop_tokens)
                ov = self._overrides.get(key)
                ud = self._overrides.get(ScopeKey(SCOPE_USER_DEFAULT, "", uid))
                quota = self._resolve_quota(key)
                rows.append(
                    {
                        "user_id": uid,
                        "name": self._user_names.get(uid, ""),
                        "calls": calls,
                        "tokens": tokens,
                        "private_quota": serialize_quota(quota),
                        "cycle": await self._cycle_of(key, quota, now, pending),
                        "override": serialize_override(ov) if ov else None,
                        "has_override": ov is not None,
                        # 用户级成员默认（该用户在所有群的成员配额默认值）
                        "user_default_override": (
                            serialize_override(ud) if ud else None
                        ),
                        "has_user_default": ud is not None,
                    }
                )
            rows.sort(key=lambda r: (r["tokens"], r["calls"]), reverse=True)
            return _ok(
                {
                    "now": now,
                    "window_hours": window_hours,
                    "defaults": serialize_quota(self._defaults_for(SCOPE_PRIVATE)),
                    # 「群内默认」编辑弹窗的占位提示（全局成员默认；具体群还可能
                    # 有 member_default 层，占位仅供参考）
                    "member_defaults": serialize_quota(
                        self._defaults_for(SCOPE_MEMBER)
                    ),
                    "users": rows,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("获取用户用量失败", exc)

    async def api_members(self) -> dict:
        try:
            group_id = normalize_id(request.args.get("group_id"))
            if not group_id:
                return _err("缺少 group_id")
            now = self._now()
            window_hours = self._window_hours()
            cutoff = now - window_hours * 3600
            totals = {
                u: (c, t)
                for u, c, t in await self.storage.member_totals(group_id, cutoff)
            }
            for key in self._overrides:
                if (
                    key.scope == SCOPE_MEMBER
                    and key.group_id == group_id
                    and key.user_id not in totals
                ):
                    totals[key.user_id] = (0, 0)
            # 经协议拉取群成员名片/昵称（TTL 缓存），优先于消息捕获的昵称
            member_names = await self._fetch_member_names(group_id)
            since_marker = await self._usage_since_markers(
                [ScopeKey(SCOPE_MEMBER, group_id, uid) for uid in totals], cutoff
            )
            pending = await self._pending_by_key()
            rows = []
            for uid, (calls, tokens) in totals.items():
                key = ScopeKey(SCOPE_MEMBER, group_id, uid)
                calls, tokens = since_marker.get(key, (calls, tokens))
                ov = self._overrides.get(key)
                quota = self._resolve_quota(key)
                # 重置时间取内存聚合器（限流真值）中最早一条用量离开窗口的剩余秒数，
                # 与拦截提示 / /quota me 中的「约 X 后恢复」口径一致。
                _c, _t, oldest = self.aggregator.usage(key, now, quota.window_hours)
                name = member_names.get(uid) or self._user_names.get(uid, "")
                if member_names.get(uid):
                    self._remember_user_name(uid, member_names[uid])
                rows.append(
                    {
                        "user_id": uid,
                        "name": name,
                        "calls": calls,
                        "tokens": tokens,
                        "quota": serialize_quota(quota),
                        "override": serialize_override(ov) if ov else None,
                        "has_override": ov is not None,
                        "reset_in": reset_in_seconds(oldest, now, quota.window_hours),
                        "cycle": await self._cycle_of(key, quota, now, pending),
                    }
                )
            rows.sort(key=lambda r: (r["tokens"], r["calls"]), reverse=True)
            group_key = ScopeKey(SCOPE_GROUP, group_id, "")
            md_ov = self._overrides.get(ScopeKey(SCOPE_MEMBER_DEFAULT, group_id, ""))
            return _ok(
                {
                    "now": now,
                    "window_hours": window_hours,
                    "group_id": group_id,
                    "group_name": self._group_names.get(group_id, ""),
                    "group_remark": self._group_remarks.get(group_id, ""),
                    "group_quota": serialize_quota(self._resolve_quota(group_key)),
                    # 成员编辑弹窗的占位默认：全局成员默认 + 本群成员默认覆盖
                    "defaults": serialize_quota(
                        self._member_defaults_for_group(group_id)
                    ),
                    # 「成员默认配额」编辑弹窗用：现有覆盖 + 其占位（全局成员默认）
                    "member_default_override": (
                        serialize_override(md_ov) if md_ov else None
                    ),
                    "global_member_defaults": serialize_quota(
                        self._defaults_for(SCOPE_MEMBER)
                    ),
                    "members": rows,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("获取群成员用量失败", exc)

    async def api_overrides(self) -> dict:
        try:
            return _ok(
                {"overrides": [serialize_override(ov) for ov in self._overrides.values()]}
            )
        except Exception as exc:  # noqa: BLE001
            return _fail("获取覆盖失败", exc)

    # --- 动态调整 -------------------------------------------------------

    async def api_set_override(self) -> dict:
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        scope = str(data.get("scope") or "").strip()
        if scope not in OVERRIDE_SCOPES:
            return _err(f"scope 必须是 {OVERRIDE_SCOPES} 之一")
        group_id = normalize_id(data.get("group_id"))
        user_id = normalize_id(data.get("user_id"))
        if scope in (SCOPE_PRIVATE, SCOPE_USER_DEFAULT) and not user_id:
            return _err(f"{scope} 覆盖需要 user_id")
        if scope in (SCOPE_GROUP, SCOPE_MEMBER_DEFAULT) and not group_id:
            return _err(f"{scope} 覆盖需要 group_id")
        if scope == SCOPE_MEMBER and not (group_id and user_id):
            return _err("member 覆盖需要 group_id 与 user_id")
        # 清掉与作用域无关的键分量，避免杂散 id 生成永远匹配不上的覆盖键
        if scope in (SCOPE_PRIVATE, SCOPE_USER_DEFAULT):
            group_id = ""
        if scope in (SCOPE_GROUP, SCOPE_MEMBER_DEFAULT):
            user_id = ""
        try:
            parsed = parse_override_fields(data, self._fallback_provider_exists)
        except ValueError as exc:
            return _err(str(exc))
        # 只用请求体里**出现过**的键：中台的配额弹窗与长窗口 / 重置次数是两套表单，
        # 整行覆盖会把对方的字段清成「回落默认」。
        ov = Override(scope=scope, group_id=group_id, user_id=user_id, **parsed)
        merged = merge_fields(self._overrides.get(ov.key), ov, list(parsed))
        try:
            await self._set_override(merged)
        except Exception as exc:  # noqa: BLE001
            return _fail("保存覆盖失败", exc)
        return _ok(serialize_override(merged))

    async def api_delete_override(self) -> dict:
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        scope = str(data.get("scope") or "").strip()
        if scope not in OVERRIDE_SCOPES:
            return _err("无效 scope")
        group_id = normalize_id(data.get("group_id"))
        user_id = normalize_id(data.get("user_id"))
        if scope in (SCOPE_GROUP, SCOPE_MEMBER_DEFAULT):
            user_id = ""
        if scope in (SCOPE_PRIVATE, SCOPE_USER_DEFAULT):
            group_id = ""
        deleted = await self._clear_override(scope, group_id, user_id)
        return _ok({"deleted": deleted})

    async def api_reset(self) -> dict:
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        scope = str(data.get("scope") or "").strip()
        if scope not in VALID_SCOPES:
            return _err("无效 scope")
        group_id = normalize_id(data.get("group_id"))
        user_id = normalize_id(data.get("user_id"))
        # 与 override / grant 端点一致：清掉与作用域无关的 id 分量并校验必填，
        # 避免对幻影实体写状态与日志
        if scope == SCOPE_PRIVATE:
            group_id = ""
        if scope == SCOPE_GROUP:
            user_id = ""
        if scope in (SCOPE_PRIVATE, SCOPE_MEMBER) and not user_id:
            return _err("重置需要 user_id")
        if scope in (SCOPE_GROUP, SCOPE_MEMBER) and not group_id:
            return _err("重置需要 group_id")
        window = str(data.get("window") or WINDOW_SHORT).strip().lower()
        if window not in VALID_WINDOWS:
            return _err("window 必须是 short 或 long")
        try:
            # 中台重置与管理员指令同一机制：只记标记 / 关周期，不删用量事件，
            # 也不消耗自助重置次数（见 docs/adr/0001）。
            await self._reset_entity(scope, group_id, user_id, window, ACTOR_DASHBOARD)
        except Exception as exc:  # noqa: BLE001
            return _fail("重置失败", exc)
        return _ok({"reset": True, "window": window})

    async def api_reset_log(self) -> dict:
        """最近的重置日志（默认 100 条，最多 500）。"""
        try:
            limit = clamp_limit(request.args.get("limit"))
            entries = await self.storage.list_reset_log(limit)
            rows = [self._log_row(entry) for entry in entries]
            return _ok({"now": self._now(), "entries": rows})
        except Exception as exc:  # noqa: BLE001
            return _fail("获取重置日志失败", exc)

    # --- 重置次数申请 ---------------------------------------------------

    async def api_requests(self) -> dict:
        """申请列表：默认只看待审批（``status=all`` 看全部，含已决与已作废）。"""
        status = str(request.args.get("status") or REQUEST_PENDING).strip().lower()
        if status not in (REQUEST_PENDING, "all"):
            return _err("status 只能是 pending 或 all")
        try:
            rows = await self.storage.list_requests(
                None if status == "all" else REQUEST_PENDING,
                clamp_limit(request.args.get("limit")),
            )
            fresh = await self._refresh_rows(rows, keep_void=status == "all")
            return _ok({"now": self._now(), "requests": fresh})
        except Exception as exc:  # noqa: BLE001
            return _fail("获取申请列表失败", exc)

    async def _refresh_rows(self, rows: Iterable[Any], *, keep_void: bool) -> list[dict]:
        """对待审批行逐条做惰性作废检查，再序列化。``refresh_request`` 返回
        ``None`` 表示这条已被落盘作废：待审批视图剔除，全部视图重新读回该行。
        """
        result: list[dict] = []
        for row in rows:
            req = row
            if req.status == REQUEST_PENDING:
                fresh = await self.service.refresh_request(req)
                if fresh is None:
                    if not keep_void:
                        continue
                    fresh = await self.storage.get_request(req.id) or req
                req = fresh
            result.append(self._request_row(req))
        return result

    async def api_approve_request(self) -> dict:
        """批准一条申请（``amount`` 为空表示按申请数量发放）。"""
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        try:
            request_id = positive_int(data.get("id"), "id")
            amount = optional_positive_int(data.get("amount"), "amount")
        except ValueError as exc:
            return _err(str(exc))
        try:
            # 与 /quota approve 共用同一业务方法；中台操作的 admin_id 用 dashboard
            decided = await self._approve_request(request_id, amount, ACTOR_DASHBOARD)
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail("批准申请失败", exc)
        return _ok({"request": self._request_row(decided)})

    async def api_deny_request(self) -> dict:
        """拒绝一条申请（``note`` 为可选理由，会展示给申请者）。"""
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        try:
            request_id = positive_int(data.get("id"), "id")
        except ValueError as exc:
            return _err(str(exc))
        note = str(data.get("note") or "").strip()[:100]
        try:
            decided = await self._deny_request(request_id, note, ACTOR_DASHBOARD)
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail("拒绝申请失败", exc)
        return _ok({"request": self._request_row(decided)})

    async def api_grant(self) -> dict:
        """为私聊 / 群成员实体的当前周期发放重置次数。"""
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        scope = str(data.get("scope") or "").strip()
        if scope not in (SCOPE_PRIVATE, SCOPE_MEMBER):
            return _err("scope 必须是 private 或 member")
        group_id = normalize_id(data.get("group_id"))
        user_id = normalize_id(data.get("user_id"))
        if not user_id:
            return _err("发放需要 user_id")
        if scope == SCOPE_MEMBER and not group_id:
            return _err("member 发放需要 group_id")
        if scope == SCOPE_PRIVATE:  # 清掉与作用域无关的键分量
            group_id = ""
        try:
            short = non_negative_int(data.get("short"), "short")
            long = non_negative_int(data.get("long"), "long")
        except ValueError as exc:
            return _err(str(exc))
        key = ScopeKey(scope, group_id, user_id)
        try:
            # 与 /quota grant 共用同一业务方法（没有进行中的周期时抛 ValueError）
            state = await self._grant_to(key, short, long)
            quota = self._resolve_quota(key)
            return _ok({"cycle": await self._entity_cycle(key, quota, state)})
        except ValueError as exc:
            return _err(str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail("发放失败", exc)

    def _request_row(self, req: ResetRequest) -> dict:
        return serialize_request(
            req, self._group_names, self._group_remarks, self._user_names
        )

    def _log_row(self, entry: Any) -> dict:
        return serialize_log_entry(
            entry, self._group_names, self._group_remarks, self._user_names
        )

    async def api_set_remark(self) -> dict:
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        kind = str(data.get("kind") or "group").strip()
        if kind != "group":  # 目前仅支持群备注
            return _err("目前仅支持群备注（kind=group）")
        entity_id = normalize_id(data.get("entity_id"))
        if not entity_id:
            return _err("缺少 entity_id")
        remark = str(data.get("remark") or "").strip()[:64]
        try:
            await self._set_remark(kind, entity_id, remark)
        except Exception as exc:  # noqa: BLE001
            return _fail("保存备注失败", exc)
        return _ok({"kind": kind, "entity_id": entity_id, "remark": remark})

    # --- 名单 -----------------------------------------------------------

    _LIST_KEYS = (
        "monitored_groups",
        "monitored_users",
        "exempt_groups",
        "exempt_users",
    )

    async def api_get_lists(self) -> dict:
        return _ok(
            {
                "enforcement_mode": str(self.config.get("enforcement_mode", "allowlist")),
                **{
                    key: [normalize_id(x) for x in (self.config.get(key) or [])]
                    for key in self._LIST_KEYS
                },
            }
        )

    async def api_save_lists(self) -> dict:
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        mode = str(data.get("enforcement_mode", "")).strip()
        if mode in ("allowlist", "all"):
            self.config["enforcement_mode"] = mode
        for key in self._LIST_KEYS:
            if key in data and isinstance(data[key], list):
                self.config[key] = [normalize_id(x) for x in data[key] if normalize_id(x)]
        self._save_config()
        return await self.api_get_lists()

    # --- 配置 -----------------------------------------------------------

    async def api_providers(self) -> dict:
        try:
            return _ok({"providers": self._provider_options()})
        except Exception as exc:  # noqa: BLE001
            return _fail("获取模型供应商失败", exc)

    async def api_get_config(self) -> dict:
        return _ok(
            {"config": self._serialize_config(), "schema": self._config_schema_for_page()}
        )

    async def api_save_config(self) -> dict:
        data = await request.get_json(silent=True)
        if not isinstance(data, dict):
            return _err("请求体必须是 JSON 对象")
        raw = data.get("config", data)
        if not isinstance(raw, dict):
            return _err("config 必须是 JSON 对象")
        try:
            cfg = self._sanitize_config(raw)
        except ValueError as exc:
            return _err(str(exc))
        old_window = self._window_hours()
        self._apply_config(cfg)
        # 全局窗口变更后从 DB 重建内存用量：内存驱逐不可逆，调大窗口须回填
        if self._window_hours() != old_window:
            await self._rebuild_usage()
        return _ok(
            {"config": self._serialize_config(), "schema": self._config_schema_for_page()}
        )
