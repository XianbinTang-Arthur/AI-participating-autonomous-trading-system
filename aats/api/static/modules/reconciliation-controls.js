// #5 修复：原本 reconciliationActionCopy / renderReconciliationControls 两个
// helper 连同支撑它们的私有 should*/reconciliationNeedsAttention/resumeActionHint
// 全部定义在 modules/views/risk-view.js 里，但 modules/detail-drawers.js（属于
// "更底层的 drawer 组装模块"）需要同样的逻辑来画历史对账 drawer 的按钮列，只好
// 反向 import：
//     modules/detail-drawers.js  →  modules/views/risk-view.js
// views/* 的模块本意是"最上层的页面级组合"，drawer helper 反向依赖 view 会让
// 循环依赖/初始化顺序风险一直悬在头上，也让 drawer 看起来像是 risk 页面的附属。
//
// 这里把跟"对账/恢复按钮"相关的一整簇 helper 提到一个独立的底层模块，
// risk-view 和 detail-drawers 都从这里 import，依赖方向恢复成：
//     views/risk-view.js  →  modules/reconciliation-controls.js
//     modules/detail-drawers.js  →  modules/reconciliation-controls.js
// 未来如果还有别的页面（比如 overview）需要同样的按钮，也只加 import 就行。
//
// 命名约定（modules/ 下的"提取模块"后缀，配合 exit-execution-helpers.js 一起读）：
//   - `*-controls.js`：模块的核心职责是"渲染 / 决定一组动作按钮的可用性"，
//      也就是和"用户交互控件"高度耦合的逻辑。本文件的 renderReconciliationControls
//      / shouldShowResumeAction 这一簇就是典型——它们决定按钮显隐和文案。
//   - `*-helpers.js`：模块的核心职责是"无副作用的纯计算 / 数据归一化 /
//      字符串拼接"，不直接渲染按钮，只产生供视图使用的中间数据。
//      exit-execution-helpers.js 那边的 normalizedExitExecutionHistoryFilters
//      / mergedExitExecutionReviewItems 就是典型。
// 新模块加进来时按这个标准选后缀；如果一个模块同时干这两类事，那它太大、
// 应当先按职责拆开再分别命名。

import { actionButton } from "./components.js";
import { localizeList, textOrFallback } from "./copy.js";
import { operationalStatusCopy } from "./terms.js";

export function reconciliationNeedsAttention(reconciliation) {
  const severity = String(reconciliation?.severity || "").toUpperCase();
  return Boolean(
    reconciliation?.halt_required
    || reconciliation?.review_required
    || (severity && severity !== "CLEAN")
  );
}

export function actionSuggestsRebaseline(value) {
  return String(value || "").toLowerCase().includes("rebaseline");
}

export function shouldShowValidateAction({ reconciliation, recovery }) {
  return Boolean(
    reconciliationNeedsAttention(reconciliation)
    || reconciliation?.observational_only
    || recovery.review_required
    // 全新环境首次启动时 safe_to_trade=false 但无 reconciliation/review，
    // 仍需让 Operator 能触发对账来推进状态机。
    || !recovery.safe_to_trade
  );
}

export function shouldShowRebaselineAction({ reconciliation, recovery }) {
  return Boolean(
    recovery.rebaseline_available
    || reconciliation?.review_required
    || actionSuggestsRebaseline(reconciliation?.recommended_operator_action)
  );
}

export function shouldShowResumeAction({ recovery }) {
  // 原条件仅 halted || resume_eligible，导致全新环境下
  // safe_to_trade=false 但 halted=false、resume_eligible=false 时按钮消失。
  // 补充 !safe_to_trade 条件，让 Operator 能主动触发 resume 流程
  // （后端会做完整校验，不会绕过安全检查）。
  return Boolean(recovery.halted || recovery.resume_eligible || !recovery.safe_to_trade);
}

export function shouldShowInspectReconciliation({ reconciliation, recovery }) {
  return Boolean(
    reconciliation?.reconciliation_id
    && (reconciliationNeedsAttention(reconciliation) || recovery.review_required)
  );
}

export function resumeActionHint({ recovery, uiHints }) {
  if (recovery.resume_eligible) {
    return recovery.halted ? operationalStatusCopy({ recovery }) : "";
  }
  return operationalStatusCopy({
    recovery,
    recoveryReasonText: textOrFallback(
      uiHints?.recoveryReasonsText,
      localizeList(recovery.resume_blocked_reasons, "当前没有额外恢复说明")
    ),
  });
}

export function reconciliationActionCopy({ reconciliation = null, recovery = {}, isHistorical = false } = {}) {
  if (isHistorical) {
    return "这是历史对账记录。下面的操作会作用于当前运行态，请先确认最新对账结论是否仍然一致。";
  }
  if (reconciliation?.halt_required) {
    return "当前需先完成对账。请先核对差异原因；确认交易所当前状态才是正确状态后，再接受为新基线。";
  }
  if (reconciliation?.observational_only && !recovery.review_required) {
    return "当前只有轻度动态漂移，例如保证金或浮盈随行情波动。系统可继续运行，建议持续观察，不需要立即重设基线。";
  }
  if (reconciliation?.review_required || shouldShowRebaselineAction({ reconciliation, recovery })) {
    return "当前处于待人工确认状态。请先重新对账或核对交易所账单，确认状态符合预期后再接受为新基线。";
  }
  if (recovery.halted && recovery.resume_eligible) {
    return operationalStatusCopy({ recovery });
  }
  if (!recovery.safe_to_trade) {
    return operationalStatusCopy({ recovery });
  }
  return "当前状态稳定。如果想再次确认状态，可以手动重新对账（刷新交易所状态）。";
}

export function renderReconciliationControls({
  reconciliation = null,
  recovery = {},
  uiHints = {},
  includeInspect = false,
  compact = false,
} = {}) {
  const permissionMessage = textOrFallback(uiHints.controlPermissionMessage, "");
  const canWrite = !permissionMessage;
  const buttons = [];
  if (includeInspect && shouldShowInspectReconciliation({ reconciliation, recovery })) {
    buttons.push(actionButton("查看对账", "inspect-reconciliation", reconciliation.reconciliation_id, "ghost"));
  }
  if (shouldShowValidateAction({ reconciliation, recovery })) {
    buttons.push(
      actionButton("重新对账（刷新交易所状态）", "trigger-reconciliation-validate", "", "secondary", {
        disabled: !canWrite,
        title: permissionMessage,
      })
    );
  }
  if (shouldShowRebaselineAction({ reconciliation, recovery })) {
    buttons.push(
      actionButton("接受当前状态为新基线", "trigger-rebaseline", "", "warning", {
        disabled: !canWrite,
        title: permissionMessage,
      })
    );
  }
  if (shouldShowResumeAction({ recovery })) {
    // resume_eligible=false 时仍允许点击：后端 /system/resume 会做完整校验
    // （刷新账户快照 → 对账 → resume_check），不通过时返回具体 blockers，
    // 不会绕过安全检查。禁用按钮只在无写权限时生效。
    buttons.push(
      actionButton("恢复自动运行", "trigger-resume", "", "warning", {
        disabled: !canWrite,
        title: !canWrite ? permissionMessage : resumeActionHint({ recovery, uiHints }),
      })
    );
  }
  if (!buttons.length) return `<p class="meta-copy">${reconciliationActionCopy({ reconciliation, recovery })}</p>`;
  return `<div class="stack-actions ${compact ? "table-actions--compact" : ""}">${buttons.join("")}</div>`;
}
