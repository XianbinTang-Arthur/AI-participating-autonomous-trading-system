import { actionButton, callout, kvList, pill, statGrid, surfaceCard, timeline } from "../components.js";
import { booleanWord, formatMaybeTimestamp, formatNumber, formatRelativeAge, formatSigned, listOrDash } from "../formatters.js";
import { localizeError, readableState, toneForRuntimeState } from "../terms.js";

export function renderRiskView(data) {
  const account = data.accountState || {};
  const portfolio = data.portfolio?.portfolio || {};
  const blockers = data.blockers?.blockers || [];
  const reconciliation = data.reconciliationLatest?.reconciliation || null;
  const mismatchSummary = data.reconciliationLatest?.mismatch_summary || {};
  const recovery = data.systemRecovery?.recovery || {};
  const replay = data.replayStatus || {};
  const metrics = data.metrics || {};

  return `
    <div class="panel-grid">
      <div class="span-12">
        ${surfaceCard({
          title: "风险总控",
          kicker: "是否仍然可信",
          copy: "这个页面回答系统是否还能继续自动运行，以及是什么因素在阻止它继续交易。",
          classes: "hero-card",
          actions: reconciliation?.reconciliation_id ? actionButton("查看对账详情", "inspect-reconciliation", reconciliation.reconciliation_id) : "",
          content: `
            ${callout({
              title: riskHeadline({ blockers, reconciliation, recovery }),
              copy: riskNarrative({ blockers, reconciliation, recovery }),
              pills: [
                pill(`运行状态：${readableState(data.health?.runtime_state || data.health?.overall_status)}`, toneForRuntimeState(data.health?.runtime_state || data.health?.overall_status)),
                pill(`可继续交易：${booleanWord(recovery.safe_to_trade)}`, recovery.safe_to_trade ? "positive" : "danger"),
                pill(`需要人工确认：${booleanWord(recovery.review_required)}`, recovery.review_required ? "warning" : "outline"),
              ],
            })}
            ${statGrid([
              { label: "当前 blocker 数", value: formatNumber(blockers.length), meta: blockers[0] ? localizeError(blockers[0].blocker) : "当前没有 blocker" },
              { label: "对账状态", value: readableState(reconciliation?.severity || "unknown"), meta: reconciliation?.reconciliation_id || "-" },
              { label: "恢复状态", value: readableState(recovery.recovery_state), meta: listOrDash(recovery.resume_blocked_reasons) },
              { label: "账户快照新鲜度", value: booleanWord(account.fresh), meta: formatMaybeTimestamp(account.last_refresh_timestamp) },
            ])}
          `,
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "账户与权益",
          kicker: "资金安全",
          copy: "这里显示最接近交易员理解方式的账户和仓位摘要。",
          content: kvList([
            ["总权益", formatNumber(portfolio.total_equity), "账户当前总价值"],
            ["已实现收益", formatSigned(portfolio.realized_pnl), "已确认收益"],
            ["未实现收益", formatSigned(portfolio.unrealized_pnl), "浮动盈亏"],
            ["总敞口", formatNumber(portfolio.gross_exposure), `净敞口 ${formatSigned(portfolio.net_exposure)}`],
            ["保证金使用", formatNumber(portfolio.margin_usage), "当前 snapshot 记录值"],
            ["账户快照就绪", booleanWord(account.ready), listOrDash(account.blockers)],
          ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "对账状态",
          kicker: "本地记录 vs 交易所",
          copy: "当本地记录和交易所状态不一致时，应优先在这里发现，而不是等到异常扩散。",
          content: kvList([
            ["对账级别", readableState(reconciliation?.severity || "-"), reconciliation?.reconciliation_id || "-"],
            ["是否要求暂停", booleanWord(reconciliation?.halt_required), reconciliation?.exchange_comparison_enabled ? "已对比交易所" : "仅本地校验"],
            ["差异原因", listOrDash(mismatchSummary.mismatch_reasons), listOrDash(mismatchSummary.mismatch_categories)],
            ["建议动作", localizeError(mismatchSummary.recommended_operator_action || "-"), listOrDash(mismatchSummary.safety_impacts)],
          ]),
        })}
      </div>

      <div class="span-4">
        ${surfaceCard({
          title: "恢复与回放",
          kicker: "可信恢复",
          copy: "恢复和回放决定了系统在异常后是否还能被信任。",
          content: kvList([
            ["恢复状态", readableState(recovery.recovery_state), recovery.safe_to_trade ? "当前可继续交易" : "当前不可继续交易"],
            ["是否可恢复运行", booleanWord(recovery.resume_eligible), listOrDash(recovery.resume_blocked_reasons)],
            ["是否需要人工确认", booleanWord(recovery.review_required), recovery.rebaseline_available ? "允许重建基线" : "当前不允许重建基线"],
            ["回放健康度", booleanWord(replay.healthy), replay.last_validation?.decision_id || "最近没有回放验证"],
          ]),
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "当前 blocker 明细",
          kicker: "阻断列表",
          copy: blockers.length ? "下面这些 blocker 正在影响交易资格。" : "当前没有 blocker，说明系统没有被明确阻断。",
          content: timeline(
            blockers.map((item) => ({
              title: localizeError(item.blocker),
              subtitle: item.subsystem ? `来源：${readableState(item.subsystem)}` : "系统阻断",
              detail: localizeError(item.recommended_action || item.blocker),
              pill: pill(item.submit_only ? "仅阻断发单" : "阻断执行", item.affects_execution ? "danger" : "warning"),
            })),
            "当前没有 blocker。"
          ),
        })}
      </div>

      <div class="span-6">
        ${surfaceCard({
          title: "风险观察指标",
          kicker: "辅助判断",
          copy: "这些指标不是为了好看，而是帮助判断系统是不是开始失真。",
          content: statGrid([
            { label: "拒单数", value: formatNumber(metrics.rejection_count), meta: "越高越要检查门禁与执行条件" },
            { label: "当前活动订单数", value: formatNumber(metrics.current_open_order_count), meta: "与 blocker 和 obligation 一起看" },
            { label: "对账异常数", value: formatNumber(metrics.reconciliation_mismatch_count), meta: "持续非零说明状态没有完全收敛" },
            { label: "最近回放时间", value: formatMaybeTimestamp(replay.last_validation?.validated_at), meta: formatRelativeAge(replay.last_validation?.validated_at) },
          ]),
        })}
      </div>
    </div>
  `;
}

function riskHeadline({ blockers, reconciliation, recovery }) {
  if (blockers.length > 0) return "当前存在明确阻断，系统不应继续盲目发单";
  if (reconciliation?.halt_required) return "最新对账要求暂停交易";
  if (!recovery.safe_to_trade) return "当前恢复状态仍不允许继续交易";
  return "系统当前没有明显阻断，但仍应持续观察账户与对账状态";
}

function riskNarrative({ blockers, reconciliation, recovery }) {
  if (blockers.length > 0) {
    return `当前最直接的风险来自 ${localizeError(blockers[0].blocker)}，需要先处理 blocker，再判断是否继续交易。`;
  }
  if (reconciliation?.halt_required) {
    return `最新对账结果为 ${readableState(reconciliation.severity)}，而且已经要求暂停交易，说明系统对本地状态与交易所状态的信任还没有恢复。`;
  }
  if (!recovery.safe_to_trade) {
    return `当前恢复状态为 ${readableState(recovery.recovery_state)}，系统仍不满足继续自动交易的条件：${listOrDash(recovery.resume_blocked_reasons)}。`;
  }
  return "当前没有明确 blocker，对账与恢复状态也没有显示出强制暂停条件，但仍要继续关注账户新鲜度和后续异常。";
}
