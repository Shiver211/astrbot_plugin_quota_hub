// 配额中枢数据中台前端。通过 window.AstrBotPluginPage bridge 与后端通信。

const bridge = window.AstrBotPluginPage;

const state = {
  view: "groups", // groups | users
  windowHours: 5,
  groups: [],
  users: [],
  members: [], // 当前打开的成员弹窗的成员列表
  membersGroupId: null, // 当前打开成员弹窗的群（null=未打开）
  filter: "",
  schema: {},
  config: {},
  // 各作用域的默认配额（用作配额编辑弹窗的占位提示），随对应列表接口返回刷新
  defaults: { group: {}, private: {}, member: {}, member_default: {}, user_default: {} },
  memberDefault: null, // 当前打开的群的「成员默认配额」覆盖（null=未设置）
  providers: [], // 可用聊天模型供应商 [{id,label}]，供回退模型下拉
  // 服务端当前时刻（列表接口返回），用于算「距到期」，避免受本机时钟偏差影响
  now: Date.now() / 1000,
  // 历史统计页：时间范围 / 指标 / 排行与分布的视图切换 / 最近一次接口数据
  history: { days: 30, metric: "tokens", top: "groups", share: "models", data: null },
  // 申请与重置页：申请列表的筛选状态 / 两块数据
  requests: { status: "pending", rows: [], log: [] },
};

// ---------------------------------------------------------------- 工具

const $ = (sel) => document.querySelector(sel);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

const fmt = (n) => Number(n || 0).toLocaleString("en-US");

// 百万级以上缩写为 k/M 防止卡片/表格溢出，精确值由调用方放 title 悬停提示
const numDisp = (n) => (Math.abs(Number(n) || 0) >= 1e6 ? fmtCompact(n) : fmt(n));

// 秒数 → 中文可读时长，与后端 windows.format_duration 口径一致
// （如 2小时15分钟、3天5小时）。长窗口以天计，故 24 小时以上进位到天级。
function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}秒`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes}分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    const rem = minutes % 60;
    return rem ? `${hours}小时${rem}分钟` : `${hours}小时`;
  }
  const days = Math.floor(hours / 24);
  const remHours = hours % 24;
  return remHours ? `${days}天${remHours}小时` : `${days}天`;
}

// 时间戳（秒）→ MM-DD HH:mm，按浏览器本地时区展示
function fmtTime(ts) {
  const n = Number(ts);
  if (!n) return "—";
  const d = new Date(n * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// 时间戳（秒）→ 完整本地时间，用作 title 悬停提示
const fmtTimeFull = (ts) => (Number(ts) ? new Date(Number(ts) * 1000).toLocaleString() : "—");

const WINDOW_LABELS = { short: "短窗口", long: "长窗口" };
const windowLabel = (w) => WINDOW_LABELS[w] || String(w || "");

let toastTimer = null;
function toast(message, isError) {
  const el = $("#toast");
  clearTimeout(toastTimer); // 连续 toast 时取消上一个定时器，避免新提示被旧定时器提前隐藏
  el.textContent = message;
  el.className = "toast" + (isError ? " err" : "");
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

// bridge.apiGet/apiPost 已在 status==ok 时直接 resolve 后端 data 字段，error 时 reject。
async function call(method, endpoint, payload) {
  return method === "GET"
    ? await bridge.apiGet(endpoint, payload || {})
    : await bridge.apiPost(endpoint, payload || {});
}

function meter(used, limit) {
  if (Number(limit) < 0) {
    return `<div class="meter"><span class="num" title="${fmt(used)}">${numDisp(used)} · <span class="inf">∞</span></span></div>`;
  }
  const lim = Number(limit);
  const pct = lim === 0 ? 100 : Math.min(100, (used / lim) * 100);
  const cls = pct >= 100 ? "full" : pct >= 80 ? "warn" : "";
  return `<div class="meter">
    <div class="track"><div class="fill ${cls}" style="width:${pct}%"></div></div>
    <span class="num" title="${fmt(used)}/${fmt(lim)}">${numDisp(used)}/${numDisp(lim)}</span>
  </div>`;
}

// QQ 头像：数字 id 才走腾讯 CDN（群用 p.qlogo，用户用 q1.qlogo）；非数字 id 仅显示首字母。
// 头像作背景图覆盖在首字母之上，加载失败时自动露出首字母，无需 onerror。
function avatarChip(name, id, kind) {
  const sid = String(id ?? "");
  const initial = esc((name || sid || "?").trim().charAt(0).toUpperCase() || "?");
  let bg = "";
  if (/^\d+$/.test(sid)) {
    const url =
      kind === "group"
        ? `https://p.qlogo.cn/gh/${sid}/${sid}/100`
        : `https://q1.qlogo.cn/g?b=qq&nk=${sid}&s=100`;
    bg = ` style="background-image:url('${url}')"`;
  }
  return `<span class="avatar"${bg}>${initial}</span>`;
}

function entityCell(name, id, remark, kind, hasOverride, extraBadge) {
  const title = name
    ? `<span>${esc(name)}</span>`
    : `<span class="unnamed">(未命名)</span>`;
  const tag = remark ? `<span class="remark-tag">${esc(remark)}</span>` : "";
  const ov = hasOverride ? `<span class="badge">覆盖</span>` : "";
  const extra = extraBadge ? `<span class="badge">${esc(extraBadge)}</span>` : "";
  return `<div class="entity">
    ${avatarChip(name, id, kind)}
    <div class="entity-info">
      <div class="entity-name">${title}${tag}${ov}${extra}</div>
      <div class="entity-id">${esc(id)}</div>
    </div>
  </div>`;
}

// 通用确认弹窗（沙箱禁用了原生 confirm，故自建，返回 Promise<boolean>）
let confirmResolve = null;
function confirmDialog(message) {
  if (confirmResolve) settleConfirm(false); // 结束上一个未决确认，避免 resolver 被覆盖而永挂
  return new Promise((resolve) => {
    confirmResolve = resolve;
    $("#confirm-msg").textContent = message;
    $("#confirm-modal").classList.remove("hidden");
  });
}
function settleConfirm(value) {
  $("#confirm-modal").classList.add("hidden");
  const r = confirmResolve;
  confirmResolve = null;
  if (r) r(value);
}
$("#confirm-ok").addEventListener("click", () => settleConfirm(true));
$("#confirm-cancel").addEventListener("click", () => settleConfirm(false));
$("#confirm-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "confirm-modal") settleConfirm(false);
});

// 通用输入弹窗（沙箱同样禁用了原生 prompt）。取消返回 null，确定返回输入字符串。
// opts：{ type:"text"|"number", min, step, placeholder }
let promptResolve = null;
function promptDialog(title, label, defaultValue, opts = {}) {
  if (promptResolve) settlePrompt(null); // 结束上一个未决输入，避免 resolver 被覆盖而永挂
  return new Promise((resolve) => {
    promptResolve = resolve;
    $("#prompt-title").textContent = title;
    $("#prompt-label").textContent = label;
    const input = $("#prompt-input");
    input.type = opts.type === "number" ? "number" : "text";
    input.min = opts.min != null ? String(opts.min) : "";
    input.step = opts.step != null ? String(opts.step) : "1";
    input.placeholder = opts.placeholder || "";
    input.title = label;
    input.value = defaultValue == null ? "" : String(defaultValue);
    $("#prompt-modal").classList.remove("hidden");
    input.focus();
    input.select();
  });
}
function settlePrompt(value) {
  $("#prompt-modal").classList.add("hidden");
  const r = promptResolve;
  promptResolve = null;
  if (r) r(value);
}
$("#prompt-ok").addEventListener("click", () => settlePrompt($("#prompt-input").value));
$("#prompt-cancel").addEventListener("click", () => settlePrompt(null));
$("#prompt-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "prompt-modal") settlePrompt(null);
});
$("#prompt-input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") settlePrompt($("#prompt-input").value);
  else if (ev.key === "Escape") settlePrompt(null);
});

// ---------------------------------------------------------------- 周期相关单元格

// 长窗口到期列：无进行中周期显示「—」，否则 MM-DD HH:mm + 「约 X 后到期」提示
function expireCell(cycle) {
  const end = Number(cycle && cycle.long_end);
  if (!end) return `<span class="muted" title="尚未开窗，首次计入用量时开始新周期">—</span>`;
  const left = end - Number(state.now || 0);
  const tip =
    left > 0
      ? `约 ${fmtDuration(left)}后到期（${fmtTimeFull(end)}）`
      : `已到期（${fmtTimeFull(end)}），下次调用开新周期`;
  return `<span title="${esc(tip)}">${esc(fmtTime(end))}</span>`;
}

// 剩余自助重置次数列：「短 N · 长 M」，有待审批申请时追加「申请中」徽标
function allowanceCell(cycle) {
  if (!cycle) return `<span class="muted">—</span>`;
  const short = Number(cycle.short_allowance) || 0;
  const long = Number(cycle.long_allowance) || 0;
  const base = `<span title="本周期内剩余的自助重置次数（短窗口 · 长窗口）">短 ${short} · 长 ${long}</span>`;
  const req = cycle.pending_request;
  if (!req) return base;
  const tip = `待审批申请：${windowLabel(req.window)} +${Number(req.amount) || 0}${
    req.reason ? `　理由：${req.reason}` : "　（无理由）"
  }`;
  return `${base}<span class="badge warn" title="${esc(tip)}">申请中</span>`;
}

// 成员弹窗的长窗口合并列：「调用 a/b · Token c/d」，不限用 ∞
function longPairCell(cycle, quota) {
  const c = cycle || {};
  const q = quota || {};
  // 合并列位置紧张，统一用 k/M 缩写，精确值放 title
  const lim = (v) => (Number(v) < 0 ? "∞" : fmtCompact(v));
  const exact = (v) => (Number(v) < 0 ? "不限" : fmt(v));
  const calls = Number(c.long_calls) || 0;
  const tokens = Number(c.long_tokens) || 0;
  const tip = `长窗口累计：调用 ${fmt(calls)}/${exact(q.long_calls)}，Token ${fmt(tokens)}/${exact(q.long_tokens)}`;
  return `<span class="long-pair" title="${esc(tip)}">调用 ${fmtCompact(calls)}/${lim(
    q.long_calls,
  )} · Token ${fmtCompact(tokens)}/${lim(q.long_tokens)}</span>`;
}

// ---------------------------------------------------------------- 概览 + 图表

async function loadDashboard() {
  try {
    const data = await call("GET", "overview");
    state.windowHours = data.window_hours;
    renderCards(data);
    renderTrend(data.timeseries || []);
  } catch (e) {
    toast(e.message, true);
  }
  await loadTable();
}

function card(label, value, opts = {}) {
  const help = opts.help ? `<span class="help" title="${esc(opts.help)}">?</span>` : "";
  const unit = opts.unit ? `<span class="unit">${esc(opts.unit)}</span>` : "";
  const foot = opts.foot ? `<div class="foot">${esc(opts.foot)}</div>` : "";
  const cls = ["v", opts.small ? "small" : "", opts.tone || ""].filter(Boolean).join(" ");
  const title = opts.title ? ` title="${esc(opts.title)}"` : "";
  return `<div class="card">
    <div class="k">${esc(label)}${help}</div>
    <div class="${cls}"${title}>${esc(value)}${unit}</div>
    ${foot}
  </div>`;
}

// 数值卡片：百万级以上缩写展示（如 45.6M），悬停 title 提示精确值
function numCard(label, n, opts = {}) {
  const num = Number(n) || 0;
  if (Math.abs(num) >= 1e6) {
    return card(label, fmtCompact(num), { ...opts, title: fmt(num) });
  }
  return card(label, fmt(num), opts);
}

function renderCards(data) {
  const o = data.overview || {};
  const wh = data.window_hours;
  const cards = [
    numCard(`近 ${wh}h 总调用`, o.total_calls, {
      unit: "次",
      help: `最近 ${wh} 小时（滚动窗口）内所有作用域的 LLM 调用次数合计。`,
    }),
    numCard(`近 ${wh}h 总 Token`, o.total_tokens, {
      unit: "tk",
      help: `最近 ${wh} 小时内累计消耗的 Token（输入 + 输出）。`,
    }),
    card("活跃群", fmt(o.active_groups), { unit: "个", foot: "窗口内有用量的群" }),
    card("活跃用户", fmt(o.active_users), { unit: "人", foot: "窗口内有用量的人" }),
    card("生效模式", data.enforcement_mode === "all" ? "全员限流" : "监控列表", {
      small: true,
      help: "allowlist：仅监控名单内实体生效；all：默认全员、白名单豁免。",
    }),
    card("状态", data.enabled ? "已启用" : "已停用", {
      small: true,
      tone: data.enabled ? "ok" : "off",
    }),
  ];
  if (data.dropped > 0) {
    cards.push(
      card("丢弃落盘", fmt(data.dropped), {
        small: true,
        tone: "off",
        help: "落盘队列溢出被丢弃的事件数（仅影响统计，不影响实时限流）。",
      }),
    );
  }
  $("#cards").innerHTML = cards.join("");
}

// ECharts：本页恒为深色，统一用 CSS 变量同色系，避免依赖 prefers-color-scheme。
const CC = {
  accent: "#5b8def",
  accent2: "#43c59e",
  text: "#e6e8ec",
  muted: "#8a92a3",
  border: "#2a2f3a",
  panel2: "#1f232c",
  panel: "#181b22",
};
const DIST_PALETTE = [
  "#5b8def", "#43c59e", "#e0a528", "#e5594e", "#9b7bff",
  "#13b5b1", "#e8694a", "#3aa0ff", "#f2a93b", "#6c7686",
];

const CHART_SELECTORS = [
  "#chart-trend",
  "#chart-dist",
  "#chart-hist-trend",
  "#chart-hist-top",
  "#chart-hist-share",
  "#chart-hist-heat",
];
const chartReady = () => typeof window.echarts !== "undefined";

// 复用同容器已有实例（getInstanceByDom），避免每次刷新都 dispose+init 整块 canvas；
// 渲染时统一 setOption(opt, true)（notMerge）整张替换，不残留旧配置。
function getChart(sel) {
  const el = $(sel);
  return window.echarts.getInstanceByDom(el) || window.echarts.init(el, null, { renderer: "canvas" });
}

function showChart(sel) {
  $(sel).classList.remove("is-hidden");
  $(`${sel}-empty`).classList.add("is-hidden");
}

function setChartEmpty(sel, msg) {
  const el = $(sel);
  if (chartReady()) {
    const inst = window.echarts.getInstanceByDom(el);
    if (inst) inst.dispose();
  }
  el.classList.add("is-hidden");
  const empty = $(`${sel}-empty`);
  empty.classList.remove("is-hidden");
  empty.textContent = msg;
}

function resizeCharts() {
  if (!chartReady()) return;
  for (const sel of CHART_SELECTORS) {
    const el = $(sel);
    const inst = el && window.echarts.getInstanceByDom(el);
    if (inst && el.offsetWidth > 0) {
      try { inst.resize(); } catch (e) { /* noop */ }
    }
  }
}
window.addEventListener("resize", resizeCharts);

function fmtCompact(n) {
  n = Number(n || 0);
  const abs = Math.abs(n);
  if (abs >= 1e6) return (n / 1e6).toFixed(abs % 1e6 ? 1 : 0) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(abs % 1e3 ? 1 : 0) + "k";
  return String(n);
}

function tsLabel(ts, multiDay) {
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  return multiDay ? `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}` : hm;
}

// 趋势：Token 柱 + 调用折线，双 Y 轴
function renderTrend(series) {
  const legend = $("#series-legend");
  if (!chartReady()) { setChartEmpty("#chart-trend", "图表库未加载"); legend.textContent = ""; return; }
  if (!series.length) { setChartEmpty("#chart-trend", "当前窗口暂无用量数据"); legend.textContent = ""; return; }
  showChart("#chart-trend");
  const span = series[series.length - 1].ts - series[0].ts;
  const multiDay = span > 86400;
  const cats = series.map((p) => tsLabel(p.ts, multiDay));
  const fullTime = series.map((p) => new Date(p.ts * 1000).toLocaleString());
  const tokens = series.map((p) => p.tokens);
  const calls = series.map((p) => p.calls);
  const peak = Math.max(0, ...tokens);
  legend.textContent = `单时段峰值 ${fmt(peak)} token`;
  getChart("#chart-trend").setOption(
    {
      backgroundColor: "transparent",
      textStyle: { fontFamily: "inherit" },
      grid: { left: 8, right: 8, top: 36, bottom: 6, containLabel: true },
      legend: {
        data: ["Token", "调用"], top: 2, right: 4, icon: "roundRect",
        itemWidth: 12, itemHeight: 8, textStyle: { color: CC.muted, fontSize: 11 },
      },
      tooltip: {
        trigger: "axis", confine: true, backgroundColor: CC.panel2,
        borderColor: CC.border, textStyle: { color: CC.text, fontSize: 12 },
        formatter: (ps) => {
          const i = ps[0].dataIndex;
          return `${esc(fullTime[i])}<br/>Token ${fmt(tokens[i])}<br/>调用 ${fmt(calls[i])}`;
        },
      },
      xAxis: {
        type: "category", data: cats, boundaryGap: true,
        axisLine: { lineStyle: { color: CC.border } }, axisTick: { show: false },
        axisLabel: { color: CC.muted, fontSize: 11, hideOverlap: true },
      },
      // 轴名（Token / 调用）与右上角图例重复且会与图例文字重叠，故省略，统一由图例标识。
      yAxis: [
        {
          type: "value",
          splitLine: { lineStyle: { color: CC.border, type: "dashed" } },
          axisLabel: { color: CC.muted, fontSize: 11, formatter: fmtCompact },
        },
        {
          type: "value",
          splitLine: { show: false },
          axisLabel: { color: CC.muted, fontSize: 11, formatter: fmtCompact },
        },
      ],
      series: [
        {
          name: "Token", type: "bar", yAxisIndex: 0, data: tokens, barMaxWidth: 18,
          itemStyle: { color: CC.accent, borderRadius: [3, 3, 0, 0] },
        },
        {
          name: "调用", type: "line", yAxisIndex: 1, data: calls, smooth: true,
          symbol: "circle", symbolSize: 4, lineStyle: { color: CC.accent2, width: 2 },
          itemStyle: { color: CC.accent2 }, areaStyle: { color: "rgba(67,197,158,.08)" },
        },
      ],
    },
    true,
  );
}

// 占比：当前视图（群/人）Top 8 用量环形图，其余并入「其他」
function renderDist(rows, view) {
  const legend = $("#dist-legend");
  if (!chartReady()) { setChartEmpty("#chart-dist", "图表库未加载"); legend.textContent = ""; return; }
  const idField = view === "groups" ? "group_id" : "user_id";
  let metric = "tokens";
  let total = rows.reduce((s, r) => s + Number(r.tokens || 0), 0);
  if (total <= 0) { metric = "calls"; total = rows.reduce((s, r) => s + Number(r.calls || 0), 0); }
  const metricLabel = metric === "tokens" ? "Token" : "调用";
  legend.textContent = `${view === "groups" ? "按群" : "按人"} · ${metricLabel}占比`;
  const ranked = rows
    .filter((r) => Number(r[metric]) > 0)
    .sort((a, b) => Number(b[metric]) - Number(a[metric]));
  if (!ranked.length) { setChartEmpty("#chart-dist", "当前窗口暂无用量数据"); return; }
  showChart("#chart-dist");
  const TOP = 8;
  const top = ranked.slice(0, TOP);
  const restSum = ranked.slice(TOP).reduce((s, r) => s + Number(r[metric]), 0);
  const data = top.map((r, i) => ({
    name: r.name || String(r[idField]),
    value: Number(r[metric]),
    itemStyle: { color: DIST_PALETTE[i % DIST_PALETTE.length] },
  }));
  if (restSum > 0) data.push({ name: "其他", value: restSum, itemStyle: { color: CC.muted } });
  const clip = (s) => { s = String(s || ""); return s.length > 10 ? s.slice(0, 10) + "…" : s; };
  getChart("#chart-dist").setOption(
    {
      backgroundColor: "transparent",
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        trigger: "item", confine: true, backgroundColor: CC.panel2,
        borderColor: CC.border, textStyle: { color: CC.text, fontSize: 12 },
        formatter: (p) => `${esc(p.name)}<br/>${metricLabel} ${fmt(p.value)} (${p.percent}%)`,
      },
      legend: {
        type: "scroll", orient: "horizontal", left: "center", bottom: 0, icon: "circle",
        itemWidth: 9, itemHeight: 9, textStyle: { color: CC.muted, fontSize: 11 },
        pageTextStyle: { color: CC.muted }, pageIconColor: CC.accent,
        pageIconInactiveColor: CC.border, formatter: clip,
      },
      title: {
        text: fmtCompact(total), subtext: `总${metricLabel}`,
        left: "center", top: "40%", textAlign: "center",
        textStyle: { fontSize: 20, color: CC.text },
        subtextStyle: { fontSize: 11, color: CC.muted },
      },
      series: [
        {
          type: "pie", radius: ["48%", "70%"], center: ["50%", "45%"], avoidLabelOverlap: true,
          itemStyle: { borderColor: CC.panel, borderWidth: 2, borderRadius: 4 },
          label: { show: false }, labelLine: { show: false }, data,
        },
      ],
    },
    true,
  );
}

// ---------------------------------------------------------------- 历史统计

const WEEK_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
// 后端 weekday 为 SQLite %w 口径（0=周日…6=周六）→ 显示行序（周一在上）
const rowOfW = (w) => (w + 6) % 7;
// 时段热力的单色系深浅渐变（暗底：由暗到亮表示由少到多）
const HEAT_RAMP = ["#1d2637", "#31518f", "#5b8def"];

let histReqSeq = 0; // 递增序号：丢弃过期响应，防止快速切换范围时旧响应覆盖新数据

async function loadHistory(days = state.history.days) {
  const seq = ++histReqSeq;
  try {
    const data = await call("GET", "history", { days });
    if (seq !== histReqSeq) return; // 已有更新的请求在途/完成，本响应作废
    state.history.days = days;
    state.history.data = data;
    renderHistory();
  } catch (e) {
    if (seq === histReqSeq) syncHistRange(); // 失败回滚分段控件到当前生效范围
    toast(e.message, true);
  }
}

// 让 #hist-range 的选中态与 state.history.days 保持一致（请求失败时回滚用）
function syncHistRange() {
  document.querySelectorAll("#hist-range .seg-btn").forEach((b) => {
    b.classList.toggle("active", Number(b.dataset.days) === state.history.days);
  });
}

function renderHistory() {
  const data = state.history.data;
  if (!data) return;
  renderHistCards(data);
  renderHistTrend(data);
  renderHistTop(data);
  renderHistShare(data);
  renderHistHeat(data);
}

// 24 小时视图（days=1）标签用「24 小时」，其余用「N 天」
const rangeLabel = (days) => (Number(days) === 1 ? "24 小时" : `${days} 天`);

function renderHistCards(data) {
  const t = data.totals || {};
  const actDays = Number(t.active_days) || 0;
  const avg = (v) => (actDays ? Math.round(Number(v || 0) / actDays) : 0);
  const label = rangeLabel(data.days);
  const cards = [
    numCard(`近 ${label}总调用`, t.calls, { unit: "次" }),
    numCard(`近 ${label}总 Token`, t.tokens, { unit: "tk" }),
  ];
  if (Number(data.days) > 1) {
    // 24 小时视图跨 1-2 个自然日，「日均」无意义，仅多日范围展示
    cards.push(
      numCard("日均调用", avg(t.calls), { unit: "次", foot: `按 ${actDays} 个活跃日计` }),
      numCard("日均 Token", avg(t.tokens), { unit: "tk", foot: `按 ${actDays} 个活跃日计` }),
    );
  }
  cards.push(
    card("活跃群", fmt(t.active_groups), { unit: "个", foot: "时段内有用量的群" }),
    card("活跃用户", fmt(t.active_users), { unit: "人", foot: "时段内有用量的人" }),
  );
  $("#hist-cards").innerHTML = cards.join("");
  $("#hist-note").textContent = "小时级汇总 + 实时明细，日期按服务端本地时区";
}

// 把稀疏的按日序列补零成连续 N 天。以服务端下发的 end_day 为锚点做纯日历运算
// （正午 UTC 步进，与浏览器时区无关），保证生成的键与服务端本地时区的日期串对齐。
function fillDaily(daily, days, endDay) {
  const rows = daily || [];
  const map = new Map(rows.map((d) => [d.day, d]));
  const anchor = endDay || (rows.length ? rows[rows.length - 1].day : null);
  if (!anchor) return [];
  const end = new Date(`${anchor}T12:00:00Z`);
  const out = [];
  for (let i = days - 1; i >= 0; i--) {
    const key = new Date(end.getTime() - i * 86400000).toISOString().slice(0, 10);
    const row = map.get(key);
    out.push({ day: key, calls: row ? row.calls : 0, tokens: row ? row.tokens : 0 });
  }
  return out;
}

// 24 小时视图：按整点补零成连续 24 桶。小时桶是绝对时间戳（与时区无关），
// 标签按浏览器本地时区展示；锚点用服务端下发的 end_ts。
function fillHourly(hourly, endTs) {
  const rows = hourly || [];
  const map = new Map(rows.map((h) => [Number(h.ts), h]));
  const endHour = Math.floor((Number(endTs) || Date.now() / 1000) / 3600) * 3600;
  const out = [];
  for (let i = 23; i >= 0; i--) {
    const ts = endHour - i * 3600;
    const row = map.get(ts);
    out.push({
      label: tsLabel(ts, false),
      tip: new Date(ts * 1000).toLocaleString(),
      calls: row ? row.calls : 0,
      tokens: row ? row.tokens : 0,
    });
  }
  return out;
}

// 用量趋势：单轴柱状，指标由顶部 Token/调用 切换，tooltip 恒给双指标；
// 24 小时范围（days=1）按整点渲染，多日范围按自然日渲染
function renderHistTrend(data) {
  const legend = $("#hist-trend-legend");
  const isHourView = Number(data.days) === 1;
  $("#hist-trend-title").textContent = isHourView ? "24 小时用量趋势" : "每日用量趋势";
  if (!chartReady()) { setChartEmpty("#chart-hist-trend", "图表库未加载"); legend.textContent = ""; return; }
  const points = isHourView
    ? fillHourly(data.hourly, data.end_ts)
    : fillDaily(data.daily, data.days, data.end_day).map((d) => ({
        label: d.day.slice(5), tip: d.day, calls: d.calls, tokens: d.tokens,
      }));
  if (!points.some((d) => d.calls > 0 || d.tokens > 0)) {
    setChartEmpty("#chart-hist-trend", "该时段暂无用量数据");
    legend.textContent = "";
    return;
  }
  showChart("#chart-hist-trend");
  const metric = state.history.metric;
  const values = points.map((d) => Number(d[metric] || 0));
  const peakUnit = isHourView ? "单小时峰值" : "单日峰值";
  legend.textContent = `${peakUnit} ${fmt(Math.max(...values))}${metric === "tokens" ? " token" : " 次"}`;
  getChart("#chart-hist-trend").setOption(
    {
      backgroundColor: "transparent",
      textStyle: { fontFamily: "inherit" },
      grid: { left: 8, right: 8, top: 18, bottom: 6, containLabel: true },
      tooltip: {
        trigger: "axis", confine: true, backgroundColor: CC.panel2,
        borderColor: CC.border, textStyle: { color: CC.text, fontSize: 12 },
        formatter: (ps) => {
          const d = points[ps[0].dataIndex];
          return `${esc(d.tip)}<br/>调用 ${fmt(d.calls)}<br/>Token ${fmt(d.tokens)}`;
        },
      },
      xAxis: {
        type: "category", data: points.map((d) => d.label), boundaryGap: true,
        axisLine: { lineStyle: { color: CC.border } }, axisTick: { show: false },
        axisLabel: { color: CC.muted, fontSize: 11, hideOverlap: true },
      },
      yAxis: {
        type: "value",
        splitLine: { lineStyle: { color: CC.border, type: "dashed" } },
        axisLabel: { color: CC.muted, fontSize: 11, formatter: fmtCompact },
      },
      series: [
        {
          name: metric === "tokens" ? "Token" : "调用",
          type: "bar", data: values, barMaxWidth: 16,
          itemStyle: {
            color: metric === "tokens" ? CC.accent : CC.accent2,
            borderRadius: [3, 3, 0, 0],
          },
        },
      ],
    },
    true,
  );
}

// 用量排行：Top 10 横向条形，群/用户切换，数值直接标注在条尾
function renderHistTop(data) {
  if (!chartReady()) { setChartEmpty("#chart-hist-top", "图表库未加载"); return; }
  const view = state.history.top;
  const rows = (view === "groups" ? data.top_groups : data.top_users) || [];
  const idField = view === "groups" ? "group_id" : "user_id";
  const metric = state.history.metric;
  const ranked = rows
    .filter((r) => Number(r[metric]) > 0)
    .sort((a, b) => Number(b[metric]) - Number(a[metric]))
    .slice(0, 10);
  if (!ranked.length) { setChartEmpty("#chart-hist-top", "该时段暂无用量数据"); return; }
  showChart("#chart-hist-top");
  const clip = (s) => { s = String(s || ""); return s.length > 8 ? s.slice(0, 8) + "…" : s; };
  getChart("#chart-hist-top").setOption(
    {
      backgroundColor: "transparent",
      textStyle: { fontFamily: "inherit" },
      grid: { left: 8, right: 46, top: 8, bottom: 6, containLabel: true },
      tooltip: {
        trigger: "item", confine: true, backgroundColor: CC.panel2,
        borderColor: CC.border, textStyle: { color: CC.text, fontSize: 12 },
        formatter: (p) => {
          const r = ranked[p.dataIndex];
          const nm = r.remark || r.name || r[idField];
          return `${esc(nm)}（${esc(r[idField])}）<br/>调用 ${fmt(r.calls)}<br/>Token ${fmt(r.tokens)}`;
        },
      },
      xAxis: {
        type: "value",
        splitLine: { lineStyle: { color: CC.border, type: "dashed" } },
        axisLabel: { color: CC.muted, fontSize: 11, formatter: fmtCompact },
      },
      yAxis: {
        type: "category", inverse: true,
        data: ranked.map((r) => clip(r.remark || r.name || r[idField])),
        axisLine: { lineStyle: { color: CC.border } }, axisTick: { show: false },
        axisLabel: { color: CC.muted, fontSize: 11 },
      },
      series: [
        {
          name: metric === "tokens" ? "Token" : "调用",
          type: "bar", data: ranked.map((r) => Number(r[metric])), barMaxWidth: 14,
          itemStyle: { color: CC.accent, borderRadius: [0, 3, 3, 0] },
          label: {
            show: true, position: "right", color: CC.muted, fontSize: 11,
            formatter: (p) => fmtCompact(p.value),
          },
        },
      ],
    },
    true,
  );
}

// 用量分布：模型/平台 Top 8 环形图（与中台占比图同一套样式），其余并入「其他」
function renderHistShare(data) {
  if (!chartReady()) { setChartEmpty("#chart-hist-share", "图表库未加载"); return; }
  const view = state.history.share;
  const rows = (view === "models" ? data.models : data.platforms) || [];
  const idField = view === "models" ? "model" : "platform";
  const metric = state.history.metric;
  const metricLabel = metric === "tokens" ? "Token" : "调用";
  const ranked = rows
    .map((r) => ({ name: String(r[idField] || "（未知）"), value: Number(r[metric] || 0) }))
    .filter((r) => r.value > 0)
    .sort((a, b) => b.value - a.value);
  if (!ranked.length) { setChartEmpty("#chart-hist-share", "该时段暂无用量数据"); return; }
  showChart("#chart-hist-share");
  const TOP = 8;
  const items = ranked.slice(0, TOP).map((r, i) => ({
    ...r,
    itemStyle: { color: DIST_PALETTE[i % DIST_PALETTE.length] },
  }));
  const rest = ranked.slice(TOP).reduce((s, r) => s + r.value, 0);
  if (rest > 0) items.push({ name: "其他", value: rest, itemStyle: { color: CC.muted } });
  const total = ranked.reduce((s, r) => s + r.value, 0);
  const clip = (s) => { s = String(s || ""); return s.length > 14 ? s.slice(0, 14) + "…" : s; };
  getChart("#chart-hist-share").setOption(
    {
      backgroundColor: "transparent",
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        trigger: "item", confine: true, backgroundColor: CC.panel2,
        borderColor: CC.border, textStyle: { color: CC.text, fontSize: 12 },
        formatter: (p) => `${esc(p.name)}<br/>${metricLabel} ${fmt(p.value)} (${p.percent}%)`,
      },
      legend: {
        type: "scroll", orient: "horizontal", left: "center", bottom: 0, icon: "circle",
        itemWidth: 9, itemHeight: 9, textStyle: { color: CC.muted, fontSize: 11 },
        pageTextStyle: { color: CC.muted }, pageIconColor: CC.accent,
        pageIconInactiveColor: CC.border, formatter: clip,
      },
      title: {
        text: fmtCompact(total), subtext: `总${metricLabel}`,
        left: "center", top: "40%", textAlign: "center",
        textStyle: { fontSize: 20, color: CC.text },
        subtextStyle: { fontSize: 11, color: CC.muted },
      },
      series: [
        {
          type: "pie", radius: ["48%", "70%"], center: ["50%", "45%"], avoidLabelOverlap: true,
          itemStyle: { borderColor: CC.panel, borderWidth: 2, borderRadius: 4 },
          label: { show: false }, labelLine: { show: false }, data: items,
        },
      ],
    },
    true,
  );
}

// 时段热力：周几 × 小时的调用分布，7×24 补零成完整网格
function renderHistHeat(data) {
  if (!chartReady()) { setChartEmpty("#chart-hist-heat", "图表库未加载"); return; }
  const cells = data.heatmap || [];
  if (!cells.some((c) => Number(c.calls) > 0)) {
    setChartEmpty("#chart-hist-heat", "该时段暂无用量数据");
    return;
  }
  showChart("#chart-hist-heat");
  const byKey = new Map(cells.map((c) => [`${c.weekday}-${c.hour}`, c]));
  const seriesData = [];
  const tokensAt = new Map();
  for (let w = 0; w < 7; w++) {
    for (let h = 0; h < 24; h++) {
      const c = byKey.get(`${w}-${h}`);
      seriesData.push([h, rowOfW(w), c ? Number(c.calls) : 0]);
      tokensAt.set(`${rowOfW(w)}-${h}`, c ? Number(c.tokens) : 0);
    }
  }
  const maxCalls = Math.max(1, ...cells.map((c) => Number(c.calls) || 0));
  getChart("#chart-hist-heat").setOption(
    {
      backgroundColor: "transparent",
      textStyle: { fontFamily: "inherit" },
      grid: { left: 8, right: 8, top: 8, bottom: 44, containLabel: true },
      tooltip: {
        confine: true, backgroundColor: CC.panel2, borderColor: CC.border,
        textStyle: { color: CC.text, fontSize: 12 },
        formatter: (p) => {
          const [h, row, calls] = p.value;
          const tk = tokensAt.get(`${row}-${h}`) || 0;
          return `${WEEK_LABELS[row]} ${String(h).padStart(2, "0")}:00<br/>调用 ${fmt(calls)}<br/>Token ${fmt(tk)}`;
        },
      },
      xAxis: {
        type: "category", data: Array.from({ length: 24 }, (_, i) => i),
        axisLine: { lineStyle: { color: CC.border } }, axisTick: { show: false },
        axisLabel: { color: CC.muted, fontSize: 10, interval: 2 },
      },
      yAxis: {
        type: "category", data: WEEK_LABELS,
        axisLine: { lineStyle: { color: CC.border } }, axisTick: { show: false },
        axisLabel: { color: CC.muted, fontSize: 11 },
      },
      visualMap: {
        min: 0, max: maxCalls, calculable: false, orient: "horizontal",
        left: "center", bottom: 0, itemWidth: 10, itemHeight: 90,
        textStyle: { color: CC.muted, fontSize: 10 },
        inRange: { color: HEAT_RAMP },
      },
      series: [
        {
          type: "heatmap", data: seriesData,
          itemStyle: { borderColor: CC.panel, borderWidth: 2, borderRadius: 3 },
          emphasis: { itemStyle: { borderColor: CC.accent } },
        },
      ],
    },
    true,
  );
}

// 历史页的四组切换：时间范围重新拉取；指标 / 排行视图 / 分布视图仅本地重绘
function bindSeg(sel, onPick) {
  $(sel).addEventListener("click", (ev) => {
    const btn = ev.target.closest(".seg-btn");
    if (!btn) return;
    $(sel).querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    onPick(btn.dataset);
  });
}
bindSeg("#hist-range", (d) => {
  // 不提前写 state：请求成功才提交 days，失败由 syncHistRange 回滚选中态
  loadHistory(Number(d.days) || 30);
});
bindSeg("#hist-metric", (d) => {
  state.history.metric = d.metric === "calls" ? "calls" : "tokens";
  renderHistory();
});
bindSeg("#hist-top-view", (d) => {
  state.history.top = d.top === "users" ? "users" : "groups";
  if (state.history.data) renderHistTop(state.history.data);
});
bindSeg("#hist-share-view", (d) => {
  state.history.share = d.share === "platforms" ? "platforms" : "models";
  if (state.history.data) renderHistShare(state.history.data);
});

// ---------------------------------------------------------------- 主表格（群 / 人）

async function loadTable() {
  try {
    if (state.view === "groups") {
      const data = await call("GET", "groups");
      state.groups = data.groups || [];
      state.defaults.group = data.defaults || {};
      if (data.now) state.now = data.now;
    } else {
      const data = await call("GET", "users");
      state.users = data.users || [];
      state.defaults.private = data.defaults || {};
      state.defaults.user_default = data.member_defaults || {};
      if (data.now) state.now = data.now;
    }
    renderTable();
    // 占比图用全量数据（不受筛选影响），故在此渲染而非 renderTable 内
    renderDist(state.view === "groups" ? state.groups : state.users, state.view);
  } catch (e) {
    toast(e.message, true);
  }
}

function applyFilter(rows, idField) {
  const f = state.filter.trim();
  if (!f) return rows;
  return rows.filter(
    (r) => String(r[idField]).includes(f) || String(r.name || "").includes(f),
  );
}

function renderTable() {
  if (state.view === "groups") return renderGroups();
  return renderUsers();
}

function renderGroups() {
  const wrap = $("#table-wrap");
  const rows = applyFilter(state.groups, "group_id");
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty">暂无群用量</div>`;
    return;
  }
  wrap.innerHTML = `<table><thead><tr>
      <th>群</th><th>短窗口调用</th><th>短窗口 Token</th>
      <th>长窗口调用</th><th>长窗口 Token</th><th>长窗口到期</th><th>操作</th>
    </tr></thead><tbody>${rows
      .map(
        (r) => `<tr>
        <td>${entityCell(r.name, r.group_id, r.remark, "group", r.has_override,
          r.has_member_default ? "成员默认" : "")}</td>
        <td>${meter(r.calls, r.quota.call_limit)}</td>
        <td>${meter(r.tokens, r.quota.token_limit)}</td>
        <td>${meter((r.cycle || {}).long_calls, r.quota.long_calls)}</td>
        <td>${meter((r.cycle || {}).long_tokens, r.quota.long_tokens)}</td>
        <td>${expireCell(r.cycle)}</td>
        <td>${[
          `<button type="button" class="mini-btn" data-act="members" data-g="${esc(r.group_id)}">成员</button>`,
          `<button type="button" class="mini-btn" data-act="edit" data-scope="group" data-g="${esc(r.group_id)}">配额</button>`,
          `<button type="button" class="mini-btn" data-act="remark" data-g="${esc(r.group_id)}">备注</button>`,
          `<button type="button" class="mini-btn danger" data-act="reset" data-scope="group" data-g="${esc(r.group_id)}">重置</button>`,
        ].join("")}</td>
      </tr>`,
      )
      .join("")}</tbody></table>`;
}

function renderUsers() {
  const wrap = $("#table-wrap");
  const rows = applyFilter(state.users, "user_id");
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty">暂无用户用量</div>`;
    return;
  }
  wrap.innerHTML = `<table><thead><tr>
      <th>用户</th><th>私聊调用</th><th>私聊 Token</th>
      <th>长窗口调用</th><th>长窗口 Token</th><th>到期</th><th>剩余重置</th><th>操作</th>
    </tr></thead><tbody>${rows
      .map(
        (r) => `<tr>
        <td>${entityCell(r.name, r.user_id, "", "user", r.has_override,
          r.has_user_default ? "群内默认" : "")}</td>
        <td>${meter(r.calls, r.private_quota.call_limit)}</td>
        <td>${meter(r.tokens, r.private_quota.token_limit)}</td>
        <td>${meter((r.cycle || {}).long_calls, r.private_quota.long_calls)}</td>
        <td>${meter((r.cycle || {}).long_tokens, r.private_quota.long_tokens)}</td>
        <td>${expireCell(r.cycle)}</td>
        <td>${allowanceCell(r.cycle)}</td>
        <td>${[
          `<button type="button" class="mini-btn" data-act="edit" data-scope="private" data-u="${esc(r.user_id)}">配额</button>`,
          `<button type="button" class="mini-btn" data-act="edit" data-scope="user_default" data-u="${esc(r.user_id)}" title="该用户在所有群的成员默认配额">群内默认</button>`,
          `<button type="button" class="mini-btn" data-act="grant" data-scope="private" data-u="${esc(r.user_id)}" title="为该用户私聊追加自助重置次数">发放</button>`,
          `<button type="button" class="mini-btn danger" data-act="reset" data-scope="private" data-u="${esc(r.user_id)}">重置</button>`,
        ].join("")}</td>
      </tr>`,
      )
      .join("")}</tbody></table>`;
}

// 主表事件委托
$("#table-wrap").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  handleAction(btn.dataset.act, btn.dataset.scope, btn.dataset.g || "", btn.dataset.u || "");
});

function handleAction(act, scope, g, u) {
  if (act === "members") return openMembers(g);
  if (act === "remark") return openRemark(g);
  if (act === "edit") return openQuotaModal(scope, g, u);
  if (act === "reset") return openResetModal(scope, g, u);
  if (act === "grant") return openGrantModal(scope, g, u);
}

// 弹窗副标题用的实体描述，尽量带上已知的群名 / 昵称
function entityLabel(scope, g, u) {
  const groupRow = state.groups.find((r) => r.group_id === g);
  const groupText = groupRow && groupRow.name ? `${groupRow.name}（${g}）` : `群 ${g}`;
  if (scope === "group") return groupText;
  if (scope === "private") {
    const row = state.users.find((r) => r.user_id === u);
    return row && row.name ? `私聊 · ${row.name}（${u}）` : `私聊 · 用户 ${u}`;
  }
  const member = state.members.find((r) => r.user_id === u);
  const who = member && member.name ? `${member.name}（${u}）` : `用户 ${u}`;
  return `${groupText} · 成员 ${who}`;
}

// ---------------------------------------------------------------- 群成员弹窗

async function openMembers(groupId) {
  state.membersGroupId = groupId;
  await reloadMembers();
  $("#members-modal").classList.remove("hidden");
}

async function reloadMembers() {
  try {
    const data = await call("GET", "members", { group_id: state.membersGroupId });
    state.members = data.members || [];
    if (data.now) state.now = data.now;
    state.defaults.member = data.defaults || {};
    state.memberDefault = data.member_default_override || null;
    state.defaults.member_default = data.global_member_defaults || {};
    $("#members-default-btn").classList.toggle("has-md", !!state.memberDefault);
    const label = data.group_name
      ? `${data.group_name}（${data.group_id}）`
      : data.group_id;
    $("#members-title").textContent = "群成员";
    $("#members-sub").textContent = `${label}　共 ${state.members.length} 名活跃成员`;
    renderMembersBody();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderMembersBody() {
  const body = $("#members-body");
  if (!state.members.length) {
    body.innerHTML = `<div class="empty">该群暂无成员用量</div>`;
    return;
  }
  body.innerHTML = `<table><thead><tr>
      <th>成员</th><th>调用</th><th>Token</th><th>额度恢复</th>
      <th>长窗口</th><th>到期</th><th>剩余重置</th><th>操作</th>
    </tr></thead><tbody>${state.members
      .map(
        (r) => `<tr>
        <td>${entityCell(r.name, r.user_id, "", "user", r.has_override)}</td>
        <td>${meter(r.calls, r.quota.call_limit)}</td>
        <td>${meter(r.tokens, r.quota.token_limit)}</td>
        <td>${resetCell(r.reset_in)}</td>
        <td>${longPairCell(r.cycle, r.quota)}</td>
        <td>${expireCell(r.cycle)}</td>
        <td>${allowanceCell(r.cycle)}</td>
        <td>${[
          `<button type="button" class="mini-btn" data-mact="edit" data-u="${esc(r.user_id)}">配额</button>`,
          `<button type="button" class="mini-btn" data-mact="grant" data-u="${esc(r.user_id)}" title="为该成员追加自助重置次数">发放</button>`,
          `<button type="button" class="mini-btn danger" data-mact="reset" data-u="${esc(r.user_id)}">重置</button>`,
        ].join("")}</td>
      </tr>`,
      )
      .join("")}</tbody></table>`;
}

// 额度恢复列：窗口内最早一条用量离开窗口的剩余时长（届时开始恢复额度）。
// 无用量（reset_in<=0）显示「—」。
function resetCell(resetIn) {
  const secs = Number(resetIn) || 0;
  if (secs <= 0) return `<span class="muted">—</span>`;
  return `<span title="窗口内最早一次用量离开窗口的剩余时间，届时开始恢复额度">${fmtDuration(secs)}后</span>`;
}

$("#members-body").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-mact]");
  if (!btn) return;
  const u = btn.dataset.u;
  const g = state.membersGroupId;
  const act = btn.dataset.mact;
  if (act === "edit") openQuotaModal("member", g, u);
  else if (act === "reset") openResetModal("member", g, u);
  else if (act === "grant") openGrantModal("member", g, u);
});

$("#members-default-btn").addEventListener("click", () => {
  if (state.membersGroupId) openQuotaModal("member_default", state.membersGroupId, "");
});

function closeMembers() {
  state.membersGroupId = null;
  $("#members-modal").classList.add("hidden");
}
$("#members-close").addEventListener("click", closeMembers);
$("#members-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "members-modal") closeMembers();
});

// ---------------------------------------------------------------- 群备注弹窗

function openRemark(groupId) {
  const g = state.groups.find((r) => r.group_id === groupId);
  $("#remark-modal").dataset.g = groupId;
  $("#remark-sub").textContent = g && g.name ? `${g.name}（${groupId}）` : `群 ${groupId}`;
  $("#remark-input").value = (g && g.remark) || "";
  $("#remark-status").textContent = "";
  $("#remark-modal").classList.remove("hidden");
}
function closeRemark() {
  $("#remark-modal").classList.add("hidden");
}
$("#remark-cancel").addEventListener("click", closeRemark);
$("#remark-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "remark-modal") closeRemark();
});
$("#remark-save").addEventListener("click", async () => {
  const groupId = $("#remark-modal").dataset.g;
  try {
    await call("POST", "remark", {
      kind: "group",
      entity_id: groupId,
      remark: $("#remark-input").value.trim(),
    });
    closeRemark();
    toast("备注已保存");
    await loadDashboard();
  } catch (e) {
    $("#remark-status").textContent = e.message;
    $("#remark-status").className = "status err";
  }
});

// ---------------------------------------------------------------- 重置用量弹窗

// 重置的目标实体（scope / group_id / user_id），由 openResetModal 写入
function openResetModal(scope, g, u) {
  const m = $("#reset-modal");
  m.dataset.scope = scope;
  m.dataset.g = g || "";
  m.dataset.u = u || "";
  $("#reset-sub").textContent = entityLabel(scope, g, u);
  const short = m.querySelector('input[name="reset-window"][value="short"]');
  if (short) short.checked = true;
  $("#reset-status").textContent = "";
  $("#reset-status").className = "status";
  m.classList.remove("hidden");
}

function closeResetModal() {
  $("#reset-modal").classList.add("hidden");
}
$("#reset-cancel").addEventListener("click", closeResetModal);
$("#reset-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "reset-modal") closeResetModal();
});

$("#reset-ok").addEventListener("click", async () => {
  const m = $("#reset-modal");
  const picked = m.querySelector('input[name="reset-window"]:checked');
  const window_ = picked ? picked.value : "short";
  try {
    await call("POST", "reset", {
      scope: m.dataset.scope,
      group_id: m.dataset.g,
      user_id: m.dataset.u,
      window: window_,
    });
    closeResetModal();
    toast(`已重置${windowLabel(window_)}用量`);
    await afterQuotaChange();
  } catch (e) {
    $("#reset-status").textContent = e.message;
    $("#reset-status").className = "status err";
  }
});

// ---------------------------------------------------------------- 发放重置次数弹窗

function openGrantModal(scope, g, u) {
  const m = $("#grant-modal");
  m.dataset.scope = scope;
  m.dataset.g = g || "";
  m.dataset.u = u || "";
  $("#grant-sub").textContent = entityLabel(scope, g, u);
  $("#grant-short").value = "0";
  $("#grant-long").value = "0";
  $("#grant-status").textContent = "";
  $("#grant-status").className = "status";
  m.classList.remove("hidden");
}

function closeGrantModal() {
  $("#grant-modal").classList.add("hidden");
}
$("#grant-cancel").addEventListener("click", closeGrantModal);
$("#grant-modal").addEventListener("click", (ev) => {
  if (ev.target.id === "grant-modal") closeGrantModal();
});

$("#grant-ok").addEventListener("click", async () => {
  const m = $("#grant-modal");
  const setStatus = (msg) => {
    $("#grant-status").textContent = msg;
    $("#grant-status").className = "status err";
  };
  const short = Math.floor(Number($("#grant-short").value || 0));
  const long = Math.floor(Number($("#grant-long").value || 0));
  if (!Number.isFinite(short) || !Number.isFinite(long) || short < 0 || long < 0) {
    setStatus("发放数量必须是不小于 0 的整数");
    return;
  }
  if (short === 0 && long === 0) {
    setStatus("短窗口与长窗口至少要发放 1 次");
    return;
  }
  try {
    await call("POST", "grant", {
      scope: m.dataset.scope,
      group_id: m.dataset.g,
      user_id: m.dataset.u,
      short,
      long,
    });
    closeGrantModal();
    toast("已发放");
    await afterQuotaChange();
  } catch (e) {
    setStatus(e.message);
  }
});

// ---------------------------------------------------------------- 配额编辑弹窗

function findRow(scope, g, u) {
  if (scope === "group") return state.groups.find((r) => r.group_id === g);
  if (scope === "private") return state.users.find((r) => r.user_id === u);
  if (scope === "member") return state.members.find((r) => r.user_id === u);
  return null;
}

// 各作用域覆盖的取值来源：两层默认覆盖不走行数据的 override 字段
function findOverride(scope, g, u) {
  if (scope === "member_default") return state.memberDefault;
  if (scope === "user_default") {
    return state.users.find((r) => r.user_id === u)?.user_default_override || null;
  }
  return findRow(scope, g, u)?.override || null;
}

// 输入框占位：把某作用域的默认值提示给用户，留空即沿用该默认。
function defaultHint(value) {
  if (value === undefined || value === null) return "留空用默认";
  return Number(value) < 0 ? "默认：不限" : `默认：${value}`;
}

// 备用模型下拉：首项「继承默认」（值为空，提交时后端按 null 处理 = 继承全局），
// 其后为当前可用供应商；若覆盖里的供应商已不在列表（被删），仍补一项保留其值。
function buildProviderOptions(currentId, defaultId) {
  const inherit = `继承默认（${defaultId ? esc(defaultId) : "无"}）`;
  const opts = [`<option value="" ${currentId == null ? "selected" : ""}>${inherit}</option>`];
  let found = false;
  for (const p of state.providers || []) {
    if (currentId === p.id) found = true;
    opts.push(
      `<option value="${esc(p.id)}" ${currentId === p.id ? "selected" : ""}>${esc(p.label || p.id)}</option>`,
    );
  }
  if (currentId != null && currentId !== "" && !found) {
    opts.push(`<option value="${esc(currentId)}" selected>${esc(currentId)}（不在当前列表）</option>`);
  }
  return opts.join("");
}

function openQuotaModal(scope, g, u) {
  // 预填「单实体覆盖」原始值（无覆盖则留空），默认值改以占位符提示——
  // 这样「留空=用默认」语义成立，也能为成员设置区别于默认的专属上限。
  const ov = findOverride(scope, g, u);
  const def = state.defaults[scope] || {};
  const m = $("#modal");
  m.dataset.scope = scope;
  m.dataset.g = g;
  m.dataset.u = u;
  const label =
    scope === "group" ? `群 ${g}`
      : scope === "private" ? `用户 ${u}`
      : scope === "member_default" ? `群 ${g} · 全体成员默认`
      : scope === "user_default" ? `用户 ${u} · 所有群成员默认`
      : `群 ${g} · 成员 ${u}`;
  $("#modal-title").textContent = "编辑配额";
  $("#modal-sub").textContent = label;
  const calls = $("#m-calls");
  const tokens = $("#m-tokens");
  const hours = $("#m-hours");
  calls.value = ov?.call_limit ?? "";
  calls.placeholder = defaultHint(def.call_limit);
  tokens.value = ov?.token_limit ?? "";
  tokens.placeholder = defaultHint(def.token_limit);
  hours.value = ov?.window_hours ?? "";
  hours.placeholder = def.window_hours != null ? `默认：${def.window_hours}` : "留空用默认";
  $("#m-fb-provider").innerHTML = buildProviderOptions(
    ov?.fallback_provider_id ?? null,
    def.fallback_provider_id,
  );
  const fbCalls = $("#m-fb-calls");
  const fbTokens = $("#m-fb-tokens");
  fbCalls.value = ov?.fallback_call_limit ?? "";
  fbCalls.placeholder = defaultHint(def.fallback_call_limit);
  fbTokens.value = ov?.fallback_token_limit ?? "";
  fbTokens.placeholder = defaultHint(def.fallback_token_limit);
  // 长窗口
  const longCalls = $("#m-long-calls");
  const longTokens = $("#m-long-tokens");
  const longDays = $("#m-long-days");
  longCalls.value = ov?.long_calls ?? "";
  longCalls.placeholder = defaultHint(def.long_calls);
  longTokens.value = ov?.long_tokens ?? "";
  longTokens.placeholder = defaultHint(def.long_tokens);
  longDays.value = ov?.long_window_days ?? "";
  longDays.placeholder = def.long_window_days != null ? `默认：${def.long_window_days}` : "留空用默认";
  // 自助重置次数：群共享池由全体成员共用，不面向个人，故 group 作用域隐藏该分隔
  const srBlock = $("#m-selfreset-block");
  srBlock.classList.toggle("is-hidden", scope === "group");
  const srShort = $("#m-sr-short");
  const srLong = $("#m-sr-long");
  srShort.value = ov?.self_reset_short ?? "";
  srShort.placeholder = defaultHint(def.self_reset_short);
  srLong.value = ov?.self_reset_long ?? "";
  srLong.placeholder = defaultHint(def.self_reset_long);
  $("#m-enabled").checked = ov ? ov.enabled !== false : true;
  $("#modal-status").textContent = "";
  m.classList.remove("hidden");
}

function closeQuotaModal() {
  $("#modal").classList.add("hidden");
}

async function afterQuotaChange() {
  await loadDashboard();
  if (state.membersGroupId) await reloadMembers();
}

$("#m-cancel").addEventListener("click", closeQuotaModal);
$("#modal").addEventListener("click", (ev) => {
  if (ev.target.id === "modal") closeQuotaModal();
});

$("#m-save").addEventListener("click", async () => {
  const m = $("#modal");
  const numOrNull = (v) => (v === "" ? null : Number(v));
  const payload = {
    scope: m.dataset.scope,
    group_id: m.dataset.g,
    user_id: m.dataset.u,
    call_limit: numOrNull($("#m-calls").value),
    token_limit: numOrNull($("#m-tokens").value),
    window_hours: numOrNull($("#m-hours").value),
    enabled: $("#m-enabled").checked,
    // 空字符串 = 继承默认（后端按 null 处理）
    fallback_provider_id: $("#m-fb-provider").value,
    fallback_call_limit: numOrNull($("#m-fb-calls").value),
    fallback_token_limit: numOrNull($("#m-fb-tokens").value),
    long_calls: numOrNull($("#m-long-calls").value),
    long_tokens: numOrNull($("#m-long-tokens").value),
    long_window_days: numOrNull($("#m-long-days").value),
  };
  // 群共享池没有自助重置次数，对应字段不提交（后端只处理请求体里出现过的键）
  if (m.dataset.scope !== "group") {
    payload.self_reset_short = numOrNull($("#m-sr-short").value);
    payload.self_reset_long = numOrNull($("#m-sr-long").value);
  }
  try {
    await call("POST", "override", payload);
    closeQuotaModal();
    toast("配额已更新");
    await afterQuotaChange();
  } catch (e) {
    $("#modal-status").textContent = e.message;
    $("#modal-status").className = "status err";
  }
});

$("#m-clear").addEventListener("click", async () => {
  const m = $("#modal");
  // 清除覆盖不可撤销（该实体将整体回落到上一层默认），先二次确认
  if (!(await confirmDialog("确定清除该实体的配额覆盖？清除后将回落到上一层默认。"))) return;
  try {
    await call("POST", "override/delete", {
      scope: m.dataset.scope,
      group_id: m.dataset.g,
      user_id: m.dataset.u,
    });
    closeQuotaModal();
    toast("已清除覆盖");
    await afterQuotaChange();
  } catch (e) {
    $("#modal-status").textContent = e.message;
    $("#modal-status").className = "status err";
  }
});

// ---------------------------------------------------------------- 申请与重置页

// 申请状态 → 徽标配色（作废与未知状态用中性色）
const STATUS_TONE = { pending: "warn", approved: "ok", denied: "danger", void: "mute" };

// 审批 / 发起人：中台操作的 id 固定为 dashboard，其余是管理员 QQ 号
const actorText = (id) => (id === "dashboard" ? "中台" : String(id || "—"));

async function loadRequestsTab() {
  await Promise.all([loadRequestList(), loadResetLog()]);
}

let reqListSeq = 0; // 递增序号：快速切换「待审批 / 全部」时丢弃过期响应
async function loadRequestList() {
  const seq = ++reqListSeq;
  try {
    const data = await call("GET", "requests", { status: state.requests.status, limit: 100 });
    if (seq !== reqListSeq) return; // 已有更新的请求在途/完成，本响应作废
    state.requests.rows = data.requests || [];
    if (data.now) state.now = data.now;
    renderRequestList();
  } catch (e) {
    toast(e.message, true);
  }
}

async function loadResetLog() {
  try {
    const data = await call("GET", "reset_log", { limit: 100 });
    state.requests.log = data.entries || [];
    if (data.now) state.now = data.now;
    renderResetLog();
  } catch (e) {
    toast(e.message, true);
  }
}

// 申请 / 日志共用的实体列：主行为人名（群作用域为群名），副行为后端给的 entity_text
function recordEntityCell(r) {
  const who =
    r.scope === "group"
      ? r.group_name || `群 ${r.group_id}`
      : r.user_name || `用户 ${r.user_id}`;
  const inGroup =
    r.scope === "member"
      ? `<span class="remark-tag">${esc(r.group_remark || r.group_name || r.group_id)}</span>`
      : "";
  return `<div class="entity-info">
    <div class="entity-name">${esc(who)}${inGroup}</div>
    <div class="entity-id">${esc(r.entity_text || "")}</div>
  </div>`;
}

// 理由 / 备注等自由文本：单行截断，完整内容放 title
function clipCell(text) {
  const s = String(text || "").trim();
  if (!s) return `<span class="muted">—</span>`;
  return `<span class="clip" title="${esc(s)}">${esc(s)}</span>`;
}

function renderRequestList() {
  const wrap = $("#requests-body");
  const rows = state.requests.rows;
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty">${
      state.requests.status === "pending" ? "没有待审批的申请" : "暂无重置申请"
    }</div>`;
    return;
  }
  wrap.innerHTML = `<table class="flex-2nd"><thead><tr>
      <th>#</th><th>实体</th><th>窗口</th><th>数量</th><th>理由</th>
      <th>提交时间</th><th>状态</th><th>操作</th>
    </tr></thead><tbody>${rows.map(requestRow).join("")}</tbody></table>`;
}

function requestRow(r) {
  const tone = STATUS_TONE[r.status] || "";
  const amount = Number(r.amount) || 0;
  const granted = Number(r.granted) || 0;
  const amountText =
    r.status === "approved"
      ? `<span title="申请 ${amount} 次，实发 ${granted} 次">+${granted}</span>`
      : `<span title="申请 ${amount} 次">+${amount}</span>`;
  return `<tr>
    <td class="id">#${esc(r.id)}</td>
    <td>${recordEntityCell(r)}</td>
    <td>${esc(r.window_label || windowLabel(r.window))}</td>
    <td>${amountText}</td>
    <td>${clipCell(r.reason)}</td>
    <td title="${esc(fmtTimeFull(r.created_at))}">${esc(fmtTime(r.created_at))}</td>
    <td><span class="badge ${tone}">${esc(r.status_label || r.status)}</span></td>
    <td>${requestActionCell(r)}</td>
  </tr>`;
}

function requestActionCell(r) {
  if (r.status === "pending") {
    return [
      `<button type="button" class="mini-btn" data-ract="approve" data-id="${esc(r.id)}">批准</button>`,
      `<button type="button" class="mini-btn danger" data-ract="deny" data-id="${esc(r.id)}">拒绝</button>`,
    ].join("");
  }
  if (r.status === "void") {
    return `<span class="muted" title="所属周期已结束，申请自动作废">周期已结束</span>`;
  }
  const note = r.decision_note ? `　备注：${r.decision_note}` : "";
  const tip = `${actorText(r.decided_by)} 于 ${fmtTimeFull(r.decided_at)} 处理${note}`;
  return `<span class="muted" title="${esc(tip)}">${esc(actorText(r.decided_by))} · ${esc(
    fmtTime(r.decided_at),
  )}</span>`;
}

// 申请列表事件委托：批准要输入实发数量，拒绝要输入理由（可留空）
$("#requests-body").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-ract]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const row = state.requests.rows.find((r) => Number(r.id) === id);
  if (!row) return;
  if (btn.dataset.ract === "approve") await approveRequest(row);
  else await denyRequest(row);
});

async function approveRequest(row) {
  const input = await promptDialog(
    `批准申请 #${row.id}`,
    `实际发放的${windowLabel(row.window)}重置次数（≥1）`,
    String(Number(row.amount) || 1),
    { type: "number", min: 1 },
  );
  if (input === null) return;
  const amount = Math.floor(Number(String(input).trim()));
  if (!Number.isFinite(amount) || amount < 1) {
    toast("发放数量必须是不小于 1 的整数", true);
    return;
  }
  try {
    await call("POST", "request/approve", { id: row.id, amount });
    toast("已批准");
    await refreshAfterDecision();
  } catch (e) {
    toast(e.message, true);
  }
}

async function denyRequest(row) {
  const note = await promptDialog(
    `拒绝申请 #${row.id}`,
    "拒绝理由（可留空，会展示给申请者）",
    "",
    { placeholder: "例如：本周期已发放过" },
  );
  if (note === null) return;
  try {
    await call("POST", "request/deny", { id: row.id, note: String(note).trim() });
    toast("已拒绝");
    await refreshAfterDecision();
  } catch (e) {
    toast(e.message, true);
  }
}

// 审批后申请列表与重置日志都会变；主表的「申请中」徽标也要跟着更新
async function refreshAfterDecision() {
  await loadRequestsTab();
  await loadTable();
}

function renderResetLog() {
  const wrap = $("#resetlog-body");
  const rows = state.requests.log;
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty">暂无重置记录</div>`;
    return;
  }
  wrap.innerHTML = `<table class="flex-2nd"><thead><tr>
      <th>时间</th><th>实体</th><th>窗口</th><th>发起</th><th>消耗次数</th>
    </tr></thead><tbody>${rows
      .map(
        (r) => `<tr>
        <td title="${esc(fmtTimeFull(r.ts))}">${esc(fmtTime(r.ts))}</td>
        <td>${recordEntityCell(r)}</td>
        <td>${esc(r.window_label || windowLabel(r.window))}</td>
        <td>${logActorCell(r)}</td>
        <td>${
          r.consumed
            ? `<span title="消耗了该实体一次自助重置次数">1 次</span>`
            : `<span class="muted" title="管理员 / 中台重置不消耗自助重置次数">—</span>`
        }</td>
      </tr>`,
      )
      .join("")}</tbody></table>`;
}

// 发起列：来源标签（本人 / 管理员 / 中台）+ 已知的发起人昵称或 id
function logActorCell(r) {
  const label = r.actor_label || r.actor || "—";
  const who = r.actor_name || (r.actor_id ? String(r.actor_id) : "");
  if (!who) return `<span>${esc(label)}</span>`;
  return `<span title="${esc(r.actor_id || "")}">${esc(label)}（${esc(who)}）</span>`;
}

bindSeg("#req-status-seg", (d) => {
  state.requests.status = d.status === "all" ? "all" : "pending";
  loadRequestList();
});

// ---------------------------------------------------------------- 配置页

async function loadConfig() {
  try {
    const data = await call("GET", "config");
    state.schema = data.schema || {};
    state.config = data.config || {};
    renderConfigForm();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderConfigForm() {
  const form = $("#config-form");
  const entries = Object.entries(state.schema);
  form.innerHTML = entries
    .map(([key, spec]) => {
      const val = state.config[key];
      const type = spec.type || "string";
      const id = `cf-${esc(key)}`;
      const full = type === "text" || type === "list" ? "full" : "";
      let control = "";
      if (type === "bool") {
        control = `<div class="switch"><input type="checkbox" id="${id}" ${val ? "checked" : ""}/><span>${val ? "开" : "关"}</span></div>`;
      } else if (type === "int" || type === "float") {
        control = `<input type="number" id="${id}" value="${esc(val)}" ${type === "float" ? 'step="0.5"' : ""}/>`;
      } else if (Array.isArray(spec.options)) {
        const labels = Array.isArray(spec.option_labels) ? spec.option_labels : null;
        control = `<select id="${id}">${spec.options
          .map(
            (o, i) =>
              `<option value="${esc(o)}" ${o === val ? "selected" : ""}>${esc(
                labels && labels[i] != null ? labels[i] : o,
              )}</option>`,
          )
          .join("")}</select>`;
      } else if (type === "list") {
        control = `<textarea id="${id}" placeholder="每行一个">${esc((val || []).join("\n"))}</textarea>`;
      } else if (type === "text") {
        control = `<textarea id="${id}">${esc(val ?? "")}</textarea>`;
      } else {
        control = `<input type="text" id="${id}" value="${esc(val ?? "")}"/>`;
      }
      return `<div class="field ${full}">
        <label class="head" for="${id}">${esc(spec.description || key)}</label>
        ${control}
        ${spec.hint ? `<span class="hint">${esc(spec.hint)}</span>` : ""}
      </div>`;
    })
    .join("");
}

function collectConfig() {
  const out = {};
  for (const [key, spec] of Object.entries(state.schema)) {
    const el = document.getElementById(`cf-${key}`);
    if (!el) continue;
    const type = spec.type || "string";
    if (type === "bool") out[key] = el.checked;
    else if (type === "int") out[key] = parseInt(el.value, 10);
    else if (type === "float") out[key] = parseFloat(el.value);
    else if (type === "list")
      out[key] = el.value
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
    else out[key] = el.value;
  }
  return out;
}

$("#save-config").addEventListener("click", async () => {
  const status = $("#config-status");
  try {
    const data = await call("POST", "config", { config: collectConfig() });
    state.config = data.config || {};
    renderConfigForm();
    status.textContent = "✅ 配置已保存";
    status.className = "status ok";
    toast("配置已保存");
  } catch (e) {
    status.textContent = e.message;
    status.className = "status err";
    toast(e.message, true);
  }
});

// ---------------------------------------------------------------- 外壳：标签 / 刷新 / 筛选

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    const name = tab.dataset.tab;
    $(`#tab-${name}`).classList.add("active");
    if (name === "config") loadConfig();
    // 历史页在切入时拉取/重绘；已存在的实例在隐藏期间可能错过窗口 resize，补一次
    else if (name === "history") { loadHistory(); requestAnimationFrame(resizeCharts); }
    else if (name === "requests") loadRequestsTab();
    // 图表在隐藏面板里 init 会得到 0 宽，切回中台时重新 resize 以撑满容器
    else requestAnimationFrame(resizeCharts);
  });
});

// 限定作用域在数据中台的群/人切换容器内，避免影响历史统计页的分段控件
bindSeg("#view-seg", (d) => {
  state.view = d.view === "users" ? "users" : "groups";
  loadTable();
});

$("#filter").addEventListener("input", (ev) => {
  state.filter = ev.target.value;
  renderTable();
});

$("#refresh").addEventListener("click", async () => {
  if ($("#tab-config").classList.contains("active")) {
    await loadConfig();
  } else if ($("#tab-history").classList.contains("active")) {
    await loadHistory();
  } else if ($("#tab-requests").classList.contains("active")) {
    await loadRequestsTab();
  } else {
    await loadDashboard();
  }
  toast("已刷新");
});

// ---------------------------------------------------------------- 启动

async function main() {
  try {
    await bridge.ready();
  } catch (e) {
    document.body.innerHTML =
      '<p style="padding:24px;color:#8a92a3">请在 AstrBot WebUI 的插件页面内打开本页面。</p>';
    return;
  }
  loadProviders();
  loadDashboard();
}

// 备用模型供应商列表：失败不阻塞主流程，仅置空（下拉只剩「继承默认」）。
async function loadProviders() {
  try {
    const data = await call("GET", "providers");
    state.providers = data.providers || [];
  } catch (e) {
    state.providers = [];
  }
}

main();
