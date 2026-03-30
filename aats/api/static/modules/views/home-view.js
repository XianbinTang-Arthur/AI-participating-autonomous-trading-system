import { alertQueue, pill, summaryStrip, surfaceCard } from "../components.js";
import { localizeList } from "../copy.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned } from "../formatters.js";
import {
  localizeError,
  operationalStatusCopy,
  permissionStatusLabel,
  readableFamilyExecutionSummary,
  readableState,
  reconciliationStatusLabel,
  reviewStatusLabel,
  statusHeadline,
  toneForReconciliationSeverity,
} from "../terms.js";

export function renderHomeView(data) {
  const health = data.health || {};
  const mode = data.mode || {};
  const runtime = data.runtime || {};
  const recovery = data.systemRecovery?.recovery || {};
  const blockers = data.blockers?.blockers || [];
  const portfolio = data.portfolio?.portfolio || {};
  const latestDecision = data.latestDecision || {};
  const latestOrder = data.executionLatest?.latest_order || null;
  const latestFill = data.executionLatest?.latest_fill || null;
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const account = data.accountState || {};
  const metrics = data.metrics || {};
  const uiHints = data.uiHints || {};
  const executionRoute = runtime.environment_capabilities?.exchange_submission_target || mode.execution_route || "unknown";

  return `
    <div class="panel-grid">
      <div class="span-4">
        ${surfaceCard({
          title: "首要问题",
          kicker: "问题处置",
          classes: "home-context-card",
          content: alertQueue(primaryIssues({ blockers, reconciliation, recovery, account }), "当前暂无需要立刻人工处理的问题。"),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "操作概览",
          kicker: "操作前提",
          classes: "home-context-card",
          content: summaryStrip([
            { label: "执行线路", value: displayExecutionRoute(executionRoute), meta: displayExecutionRouteMeta(executionRoute), tone: "info" },
            { label: "是否暂停", value: booleanWord(health.halted), meta: health.halted ? "建议先确认暂停原因" : "当前没有触发手动暂停", tone: health.halted ? "danger" : "positive" },
            { label: "控制权限", value: permissionStatusLabel(!uiHints.controlPermissionMessage), meta: uiHints.controlPermissionMessage || "当前账号可以执行人工控制", tone: uiHints.controlPermissionMessage ? "warning" : "positive" },
            { label: "活动委托", value: formatNumber(metrics.current_open_order_count, 0), meta: metrics.current_open_order_count > 0 ? "恢复前建议先确认委托是否收敛" : "当前没有活动委托", tone: metrics.current_open_order_count > 0 ? "warning" : "positive" },
          ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "账户概览",
          kicker: "资金状态",
          classes: "home-context-card",
          content: summaryStrip([
            { label: "总权益", value: formatNumber(portfolio.total_equity), meta: `未实现 ${formatSigned(portfolio.unrealized_pnl)}`, tone: "info" },
            { label: "总敞口", value: formatNumber(portfolio.gross_exposure), meta: `净敞口 ${formatSigned(portfolio.net_exposure)}`, tone: Number(portfolio.gross_exposure || 0) > 0 ? "info" : "neutral" },
            { label: "交易所连接", value: booleanWord(account.connected), meta: account.connected ? "连接正常" : "需要先恢复连接", tone: account.connected ? "positive" : "danger" },
            { label: "快照新鲜度", value: booleanWord(account.fresh), meta: formatMaybeTimestamp(account.last_refresh_timestamp), tone: account.fresh ? "positive" : "warning" },
          ]),
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "最新动作",
          kicker: "动作摘要",
          copy: "把最新决策、委托、成交和对账压缩成一条值班视角摘要。",
          classes: "hero-card",
          content: `
            <div class="callout">
              <div class="panel-head">
                <h3>${latestDecision.decision_id ? "最新动作已生成" : "当前暂无新的交易动作"}</h3>
                <div class="inline-pills">
                  ${pill(`决策 ${latestDecision.decision_id ? "已更新" : "暂无"}`, latestDecision.decision_id ? "info" : "outline")}
                  ${pill(`委托 ${readableState(latestOrder?.status || "unknown")}`, latestOrder ? "info" : "outline")}
                </div>
              </div>
              <p>${latestActionNarrative({ latestDecision, latestOrder, latestFill, reconciliation })}</p>
            </div>
          `,
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "次级提醒",
          kicker: "提醒信息",
          copy: "这些提示不会替代首页主判断，但适合切页前快速扫一遍。",
          content: alertQueue(secondaryAlerts({ recovery, blockers, reconciliation, account, uiHints }), "当前暂无新的次级提醒。"),
        })}
      </div>
    </div>
  `;
}

function displayExecutionRoute(value) {
  const route = String(value || "").toLowerCase();
  if (!route || route === "unknown") return "线路待确认";
  if (route.includes("demo") && route.includes("derivatives")) return "演示合约";
  if (route.includes("demo") && route.includes("spot")) return "演示现货";
  if (route.includes("derivatives")) return "实盘合约";
  if (route.includes("spot")) return "实盘现货";
  return readableState(value);
}

function displayExecutionRouteMeta(value) {
  const route = String(value || "").toLowerCase();
  if (!route || route === "unknown") return "当前暂无线路说明";
  if (route.includes("guarded")) return "保护模式";
  if (route.includes("demo")) return "演示环境";
  return "标准执行路径";
}

function latestActionNarrative({ latestDecision, latestOrder, latestFill, reconciliation }) {
  if (!latestDecision.decision_id) {
    return "系统最近没有形成新的策略决策，当前更适合先确认账户同步、对账状态和风控条件。";
  }
  const symbol = latestDecision.decision_context?.symbol || "当前标的";
  const decisionText = readableFamilyExecutionSummary(latestDecision.position_target || {}, "保持当前仓位");
  return `${formatRelativeAge(latestDecision.decision_time || latestDecision.decision_context?.as_of_ts)}，系统针对 ${symbol} 形成了 ${decisionText} 的判断。`
    + `${latestOrder ? ` 最近一笔委托状态为 ${readableState(latestOrder.status)}。` : " 本轮暂未生成新委托。"}` 
    + `${latestFill ? ` 最新一笔成交已落库，数量 ${formatNumber(latestFill.fill_qty)}。` : " 当前暂无新的成交落库。"}` 
    + `${reconciliation ? ` 最近对账结论为 ${readableState(reconciliation.severity)}。` : " 当前暂无新的对账结论。"}`;
}

function primaryIssues({ blockers, reconciliation, recovery, account }) {
  if (blockers.length > 0) {
    return blockers.slice(0, 3).map((item) => ({
      title: localizeError(item.blocker),
      copy: localizeError(item.recommended_action || item.blocker),
      meta: item.subsystem ? `来源：${readableState(item.subsystem)}` : "系统阻断",
      tone: item.affects_execution ? "danger" : "warning",
      pill: pill(item.affects_execution ? "阻断执行" : "人工关注", item.affects_execution ? "danger" : "warning"),
    }));
  }
  if (reconciliation?.halt_required) {
    return [{
      title: statusHeadline("需先完成对账"),
      copy: `当前需先完成对账。最新对账结论为 ${readableState(reconciliation.severity)}，请先完成状态核对，再考虑恢复自动运行。`,
      meta: reconciliation.reconciliation_id || "最近对账",
      tone: "danger",
      pill: pill("要求停机", "danger"),
    }];
  }
  if (recovery.halted && recovery.resume_eligible) {
    return [{
      title: statusHeadline("待恢复"),
      copy: operationalStatusCopy({ recovery }),
      meta: readableState(recovery.recovery_state),
      tone: "warning",
      pill: pill("待恢复", "warning"),
    }];
  }
  if (!recovery.safe_to_trade) {
    return [{
      title: statusHeadline("恢复受限"),
      copy: operationalStatusCopy({
        recovery,
        recoveryReasonText: localizeList(recovery.resume_blocked_reasons, "当前没有给出额外恢复限制说明"),
      }),
      meta: readableState(recovery.recovery_state),
      tone: "warning",
      pill: pill("恢复受限", "warning"),
    }];
  }
  if (!account.ready) {
    return [{
      title: "先恢复账户同步",
      copy: localizeList(account.blockers, "当前账户状态还没同步完整。"),
      meta: formatMaybeTimestamp(account.last_refresh_timestamp),
      tone: "warning",
      pill: pill("账户未就绪", "warning"),
    }];
  }
  return [];
}

function secondaryAlerts({ recovery, blockers, reconciliation, account, uiHints }) {
  const items = [];
  if (recovery.review_required) {
    items.push({
      title: statusHeadline("待人工确认"),
      copy: operationalStatusCopy({
        recovery,
        recoveryReasonText: uiHints.recoveryReasonsText || localizeList(recovery.resume_blocked_reasons, "当前没有额外复核说明"),
      }),
      meta: readableState(recovery.recovery_state),
      tone: "warning",
      pill: pill(reviewStatusLabel(true), "warning"),
    });
  }
  if (blockers.length === 0 && reconciliation?.severity) {
      items.push({
        title: "关注最新对账结论",
        copy: `最近对账级别为 ${readableState(reconciliation.severity)}。`,
        meta: reconciliation.reconciliation_id || formatMaybeTimestamp(reconciliation.as_of_ts),
        tone: reconciliation.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation.severity),
        pill: pill(reconciliationStatusLabel(reconciliation), reconciliation.halt_required ? "danger" : toneForReconciliationSeverity(reconciliation.severity)),
      });
  }
  if (!account.fresh) {
    items.push({
      title: "账户快照不够新鲜",
      copy: "建议在执行人工恢复前先刷新账户状态。",
      meta: formatMaybeTimestamp(account.last_refresh_timestamp),
      tone: "warning",
      pill: pill("快照过期", "warning"),
    });
  }
  if (uiHints.controlPermissionMessage) {
    items.push({
      title: "当前账号无法直接执行高风险操作",
      copy: uiHints.controlPermissionMessage,
      meta: "权限限制",
      tone: "info",
      pill: pill(permissionStatusLabel(false), "info"),
    });
  }
  return items;
}
