"""不可变数据模型与作用域工具。

三种作用域（scope）：
- ``private``：私聊，键为 ``user_id``（``group_id`` 为空串）。
- ``group``：整个群共享，键为 ``group_id``（``user_id`` 为空串）。
- ``member``：群内单个成员，键为 ``group_id`` + ``user_id``。
"""

from __future__ import annotations

from dataclasses import dataclass

SCOPE_PRIVATE = "private"
SCOPE_GROUP = "group"
SCOPE_MEMBER = "member"
# 仅用于覆盖表的两层「默认值覆盖」，不会出现在用量事件与限流判定结果里。
# member_default：某群「群内每个成员」的默认配额（键为 group_id）；
# user_default：某用户在**所有群**的成员默认配额（键为 user_id）。
# member 作用域的解析链（后者优先）：
#   全局 default_member_* → member_default(群) → user_default(用户) → member(具体成员)
SCOPE_MEMBER_DEFAULT = "member_default"
SCOPE_USER_DEFAULT = "user_default"

# 用量事件 / 限流判定中出现的作用域
VALID_SCOPES = (SCOPE_PRIVATE, SCOPE_GROUP, SCOPE_MEMBER)
# 可设置覆盖的作用域（多两层默认值覆盖）
OVERRIDE_SCOPES = (*VALID_SCOPES, SCOPE_MEMBER_DEFAULT, SCOPE_USER_DEFAULT)

UNLIMITED = -1

# 两种窗口：短窗口（小时级滑动）与长窗口（天级锚定）。
WINDOW_SHORT = "short"
WINDOW_LONG = "long"
VALID_WINDOWS = (WINDOW_SHORT, WINDOW_LONG)
WINDOW_LABELS = {WINDOW_SHORT: "短窗口", WINDOW_LONG: "长窗口"}
DEFAULT_LONG_WINDOW_DAYS = 7.0
DEFAULT_SELF_RESET_SHORT = 1
DEFAULT_SELF_RESET_LONG = 0

SCOPE_LABELS = {
    SCOPE_PRIVATE: "个人",
    SCOPE_GROUP: "本群",
    SCOPE_MEMBER: "你在本群",
    SCOPE_MEMBER_DEFAULT: "本群成员默认",
    SCOPE_USER_DEFAULT: "该用户群内默认",
}

METRIC_LABELS = {
    "calls": "调用次数",
    "tokens": "Token",
}


@dataclass(frozen=True)
class ScopeKey:
    """聚合器/存储层中标识一个限额实体的不可变键。"""

    scope: str
    group_id: str = ""
    user_id: str = ""

    def label(self) -> str:
        return SCOPE_LABELS.get(self.scope, self.scope)


@dataclass(frozen=True)
class Quota:
    """某作用域解析后的有效配额。``-1`` 表示该指标不限。

    回退（备用模型）字段：达到常规上限后，若启用回退且备用供应商有效，则在
    ``常规上限 + 回退额度`` 的「硬上限」之内改用备用模型继续放行；超过硬上限才拦截。
    ``fallback_*_limit`` 为 ``0`` 表示该指标不启用回退（即硬上限=常规上限）。
    """

    call_limit: int = UNLIMITED
    token_limit: int = UNLIMITED
    window_hours: float = 5.0
    enabled: bool = True
    fallback_provider_id: str = ""
    fallback_call_limit: int = 0
    fallback_token_limit: int = 0
    # 长窗口（天级锚定）：耗尽即拦截，不参与回退。
    long_calls: int = UNLIMITED
    long_tokens: int = UNLIMITED
    long_window_days: float = DEFAULT_LONG_WINDOW_DAYS
    # 每个周期可自助重置的次数（短 / 长分别计）。
    self_reset_short: int = DEFAULT_SELF_RESET_SHORT
    self_reset_long: int = DEFAULT_SELF_RESET_LONG


@dataclass(frozen=True)
class Override:
    """针对单个实体的配额覆盖。``None`` 字段表示回落到默认值。"""

    scope: str
    group_id: str = ""
    user_id: str = ""
    call_limit: int | None = None
    token_limit: int | None = None
    window_hours: float | None = None
    enabled: bool = True
    fallback_provider_id: str | None = None
    fallback_call_limit: int | None = None
    fallback_token_limit: int | None = None
    long_calls: int | None = None
    long_tokens: int | None = None
    long_window_days: float | None = None
    self_reset_short: int | None = None
    self_reset_long: int | None = None

    @property
    def key(self) -> ScopeKey:
        return ScopeKey(self.scope, self.group_id, self.user_id)


# 判定结果三态：正常放行 / 回退到备用模型 / 拦截。
OUTCOME_NORMAL = "normal"
OUTCOME_FALLBACK = "fallback"
OUTCOME_BLOCK = "block"


@dataclass(frozen=True)
class Decision:
    """一次拦截判定的结果。

    ``blocked`` 与 ``outcome == OUTCOME_BLOCK`` 等价（保留 ``blocked`` 以兼容旧用法）。
    ``outcome == OUTCOME_FALLBACK`` 时 ``fallback_provider_id`` 给出应切换到的备用供应商。
    ``window`` 标明触发的是短窗口还是长窗口；长窗口时 ``reset_in`` 为距到期的秒数，
    ``window_days`` 为该长窗口的天数。
    """

    blocked: bool
    scope: str | None = None
    metric: str | None = None
    used: int = 0
    limit: int = 0
    window_hours: float = 0.0
    reset_in: float = 0.0
    outcome: str = OUTCOME_NORMAL
    fallback_provider_id: str = ""
    window: str = WINDOW_SHORT
    window_days: float = 0.0


@dataclass(frozen=True)
class BanRecord:
    """封禁记录：禁止某成员或用户与 LLM 对话。"""

    group_id: str
    user_id: str
    banned_at: int
    expire_at: int  # -1 表示永久封禁，否则为到期 Unix 时间戳（秒）
    banned_by: str = ""
    reason: str = ""

    @property
    def is_permanent(self) -> bool:
        return self.expire_at < 0

    def is_expired(self, now: float) -> bool:
        if self.is_permanent:
            return False
        return now >= self.expire_at


def normalize_id(value: object) -> str:
    """把可能为 int/float/str/None 的 ID 归一化为字符串。"""
    if value is None:
        return ""
    if isinstance(value, bool):  # 避免 True/False 被当成 1/0
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return str(value).strip()


def scopes_for(group_id: str, user_id: str) -> list[ScopeKey]:
    """根据消息来源推导需要检查/记录的作用域键。

    - 私聊（无 group_id）→ ``[private(user_id)]``
    - 群聊 → ``[group(group_id), member(group_id, user_id)]``
    """
    group_id = normalize_id(group_id)
    user_id = normalize_id(user_id)
    if group_id:
        return [
            ScopeKey(SCOPE_GROUP, group_id, ""),
            ScopeKey(SCOPE_MEMBER, group_id, user_id),
        ]
    return [ScopeKey(SCOPE_PRIVATE, "", user_id)]
