export const VIEW_ROUTES = {
  home: "/ui",
  overview: "/ui/overview",
  strategy: "/ui/strategy",
  execution: "/ui/execution",
  risk: "/ui/risk",
  exitExecution: "/ui/exit-execution",
  replay: "/ui/replay",
  aiAnalysis: "/ui/ai-analysis",
  aiConfig: "/ui/ai-config",
  admin: "/ui/settings",
};

export const VIEW_META = {
  home: {
    docTitle: "AATS 自动交易监控台 | 主页",
    eyebrow: "主页",
    heading: "系统主控台",
    copy: "这里集中查看状态、执行路径和最新风险提示。",
    hidePageHead: true,
  },
  overview: {
    docTitle: "AATS 自动交易监控台 | 交易总览",
    eyebrow: "交易总览",
    heading: "交易总览",
    copy: "",
    hidePageHead: false,
  },
  strategy: {
    docTitle: "AATS 自动交易监控台 | 策略判断",
    eyebrow: "策略解释",
    heading: "为什么现在不做或要做这笔交易",
    copy: "先看策略结论，再看门禁和阻断原因。",
    hidePageHead: false,
  },
  execution: {
    docTitle: "AATS 自动交易监控台 | 委托与成交",
    eyebrow: "委托与成交",
    heading: "",
    copy: "查看最近委托、成交、报错和卡单，确认执行链路有没有真正闭环。",
    hidePageHead: false,
  },
  risk: {
    docTitle: "AATS 自动交易监控台 | 风险与恢复",
    eyebrow: "风险、对账、恢复",
    heading: "系统现在为什么能交易或不能交易",
    copy: "关注阻断原因、对账结论、恢复状态、账户快照和是否需要人工确认。",
    hidePageHead: false,
  },
  exitExecution: {
    docTitle: "AATS 自动交易监控台 | 退出任务工作台",
    eyebrow: "退出任务工作台",
    heading: "独立排查 parent-exit 的处理历史与剩余阻断",
    copy: "这里专门查看 parent-exit 的长历史、分页和可恢复人工动作，不再和风险页的其他卡片混在一起。",
    hidePageHead: false,
  },
  replay: {
    docTitle: "AATS 自动交易监控台 | 回放与复盘",
    eyebrow: "回放与复盘",
    heading: "Replay 工作区",
    copy: "这里专门对读 replay 父腿复盘、历史校验和腿级对账异常。",
    hidePageHead: false,
  },
  aiAnalysis: {
    docTitle: "AATS 自动交易监控台 | AI 分析",
    eyebrow: "AI 分析",
    heading: "先看 AI 怎么运行，再看它有没有价值",
    copy: "这里集中展示 AI 当前状态、决策解释、策略层 shadow、执行层 shadow 和长期表现。",
    hidePageHead: false,
  },
  aiConfig: {
    docTitle: "AATS 自动交易监控台 | AI 配置",
    eyebrow: "AI 配置",
    heading: "这里管理 AI 决策模式与策略换档方式",
    copy: "左侧决定 AI 在交易里扮演什么角色，右侧决定 6 个策略档位是自动切换还是手动固定。",
    hidePageHead: false,
  },
  admin: {
    docTitle: "AATS 自动交易控制台 | 账户与权限",
    eyebrow: "控制面",
    heading: "账户与权限工作区",
    copy: "这里专门处理登录、角色、账号启停和控制台访问权限。",
    hidePageHead: false,
  },
};

export const VIEW_LABELS = {
  home: "主页",
  overview: "交易总览",
  strategy: "策略判断",
  execution: "委托与成交",
  risk: "风险与恢复",
  exitExecution: "退出任务工作台",
  replay: "回放与复盘",
  aiAnalysis: "AI 分析",
  aiConfig: "AI 配置",
  admin: "账户与权限",
};

const VIEW_ROUTE_ALIASES = { "/ui/ai": "aiAnalysis" };

export function resolveKnownView(view, fallback = "home") {
  return Object.prototype.hasOwnProperty.call(VIEW_ROUTES, view) ? view : fallback;
}

export function resolveViewFromLocation(location = window.location) {
  return VIEW_ROUTE_ALIASES[location.pathname] || Object.entries(VIEW_ROUTES).find(([, path]) => path === location.pathname)?.[0] || "home";
}

