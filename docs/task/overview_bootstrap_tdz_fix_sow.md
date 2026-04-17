# Overview Bootstrap TDZ 修复 SOW

## 业务目标与边界
- 修复 `/ui/overview` 在已登录场景下只显示页面壳子、主体空白的问题。
- 不改动 overview 的业务数据来源与接口契约，只修复前端启动顺序和回归测试缺口。

## 当前行为
- `app.js` 在模块中段调用 `init()`。
- `renderShell()` 首轮执行会进入 `renderActiveView()`，并调用 `isProtectedViewAuthBlocked()`。
- 该路径依赖模块后半段才初始化的 `PROTECTED_DASHBOARD_VIEWS` / `DASHBOARD_AUTH_ERROR_CODES`。
- 结果是命中 `const` TDZ，浏览器 console 抛 `ReferenceError`，页面只剩导航与标题。

## 方案
- 将 `init()` 移动到模块末尾，保证所有 `const` 和 helper 都初始化完成后再启动前端。
- 增加浏览器级回归测试：
  - HTTPS 登录建立 secure session。
  - 打开 `/ui/overview`。
  - 断言 overview 主体有内容，且 browser console 无 `SEVERE` 错误。

## 输入/输出接口
- 不修改任何 API 路由或返回字段。
- 不修改数据库。

## 一致性/并发
- 仅前端模块初始化顺序调整，无状态迁移风险。

## 安全与权限
- 浏览器回归测试继续走 HTTPS + secure cookie。
- 不放松任何 live profile 的认证约束。

## 错误处理
- 修复目标是消除模块启动期异常。
- 后续 auth-blocked / data loading 逻辑保持原有分支，不在本次范围内改动。

## 测试策略
- 相关 Windows 单测/集成测试。
- WSL2 最窄集成测试沿用 operator auth 浏览器链。

## 部署与验收
- 修复后重新部署。
- 验收标准：
  - `/ui/overview` 不再只显示壳子。
  - browser console 无启动期 `ReferenceError`。
