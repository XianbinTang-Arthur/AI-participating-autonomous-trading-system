import { actionButton, callout, kvList, pill, statGrid, surfaceCard, table } from "../components.js";
import { formatMaybeTimestamp, formatNumber, formatRelativeAge, listOrDash } from "../formatters.js";
import { localizeError, readableState } from "../terms.js";

const AI_STATE_MAP = {
  baseline_only: "仅运行 baseline",
  ai_advisory: "AI 参与评估",
  ai_blended: "AI 一致性确认",
  ai_primary: "AI 主导方向",
  ai_primary_shadow: "AI 影子主导",
  healthy: "正常",
  degraded: "已降级",
  trend: "趋势",
  breakout: "突破",
  range: "震荡",
  uncertain: "不确定",
  same_as_baseline: "与 baseline 一致",
  hold_instead: "改为继续持有/观望",
  entry_override: "改为开仓",
  exit_override: "改为退出",
  reverse_override: "改为反手",
  hold: "继续持有",
  flat: "继续观望",
  long: "偏多",
  short: "偏空",
  normal: "正常",
};

const AI_ERROR_MAP = {
  ai_degraded_requires_manual_review: "AI 已降级且未开启自动回退，需要人工确认后才能恢复 AI 主链。",
  ai_auto_downgraded: "AI 已自动降级，当前只保留 baseline 主链。",
  output_rejected: "AI 输出通过了结构校验，但未通过交易语义校验。",
};

function humanState(value) {
  if (value === null || value === undefined || value === "") return "-";
  const key = String(value).trim().toLowerCase();
  return AI_STATE_MAP[key] || readableState(value);
}

function humanError(value) {
  if (!value) return "-";
  const key = String(value).trim();
  return AI_ERROR_MAP[key] || localizeError(key);
}

export function renderAISections(data) {
  const runtime = data.aiRuntime || {};
  const latest = data.aiLatest || {};
  const recentPayload = data.aiRecent || {};
  const recentAssessments = recentPayload.assessments || [];
  const shadowLatest = data.aiShadowLatest?.shadow_decision || null;
  const shadowRecentPayload = data.aiShadowRecent || {};
  const shadowRecent = shadowRecentPayload.shadow_decisions || [];
  const evaluationsPayload = data.aiShadowEvaluations || {};
  const evaluations = evaluationsPayload.evaluations || [];
  const latestAssessment = latest.assessment || null;
  const latestBrief = latest.brief || null;
  const latestTakeover = latest.takeover || null;

  return {
    aiHero: surfaceCard({
      title: "AI 运行状态",
      kicker: "先确认 AI 现在是否真的在主链里",
      copy: "这张卡只回答三个问题：AI 当前配置是什么、当前是否已降级、系统是否还会继续尝试恢复 AI 主链。",
      actions: actionButton("立即生成 shadow 收益回放", "evaluate-ai-shadow", "", "secondary"),
      classes: "hero-card",
      content: `
        ${callout({
          title:
            runtime.effective_operating_mode === "baseline_only"
              ? "AI 当前没有进入真实交易主链"
              : `AI 当前有效模式：${humanState(runtime.effective_operating_mode || runtime.configured_operating_mode || "unknown")}`,
          copy: aiRuntimeNarrative(runtime),
          pills: [
            pill(`配置模式 ${humanState(runtime.configured_operating_mode || "unknown")}`, "info"),
            pill(
              `当前有效模式 ${humanState(runtime.effective_operating_mode || "unknown")}`,
              runtime.effective_operating_mode === "baseline_only" ? "warning" : "positive",
            ),
            pill(
              `自动降级 ${runtime.auto_downgrade_active ? "已触发" : "未触发"}`,
              runtime.auto_downgrade_active ? "danger" : "outline",
            ),
          ],
        })}
        ${statGrid([
          {
            label: "连续失败次数",
            value: formatNumber(runtime.consecutive_failures ?? 0, 0),
            meta: `连续成功 ${formatNumber(runtime.consecutive_successes ?? 0, 0)} 次`,
          },
          {
            label: "近期 fallback 比例",
            value: formatNumber(runtime.recent_fallback_ratio ?? 0, 3),
            meta: `timeout ${formatNumber(runtime.recent_timeout_count ?? 0, 0)} 次 / 无效输出 ${formatNumber(runtime.recent_invalid_output_count ?? 0, 0)} 次`,
          },
          {
            label: "恢复探测时间",
            value: runtime.recovery_probe_after ? formatRelativeAge(runtime.recovery_probe_after) : "-",
            meta: runtime.recovery_probe_after ? formatMaybeTimestamp(runtime.recovery_probe_after) : "当前不在降级恢复窗口内",
          },
          {
            label: "Shadow 模式",
            value: runtime.shadow_mode_enabled ? "已开启" : "未开启",
            meta: "开启后会记录 AI 假设路径，但不会改动真实下单。",
          },
        ])}
      `,
    }),
    aiLatest: surfaceCard({
      title: "最新 AI 判断",
      kicker: "最近一次 AI 看到了什么、想做什么",
      copy: "如果这里没有数据，通常表示当前就是 baseline_only，或者 AI 还没有真正进入一轮决策。",
      content: latestAssessment
        ? kvList([
            ["判断时间", formatMaybeTimestamp(latestAssessment.created_at), formatRelativeAge(latestAssessment.created_at)],
            ["市场结论", humanState(latestAssessment.regime || latestBrief?.regime_indicator || "unknown"), `方向 edge ${formatNumber(latestAssessment.directional_edge ?? 0, 2)}`],
            ["是否建议覆盖 baseline", latestAssessment.baseline_override_recommended ? "建议覆盖" : "不建议覆盖", listOrDash(latestAssessment.override_reason_codes)],
            ["交易经济性", latestAssessment.economically_actionable ? "满足交易条件" : "净边际不足", `净边际 ${formatNumber(latestAssessment.estimated_net_edge_bps ?? 0, 2)} bps`],
            ["接管结果", latestTakeover?.ai_takeover_applied ? "AI 已接管方向" : "AI 未接管", listOrDash((latestTakeover?.ai_takeover_blockers || latestAssessment.rejection_flags || []).map(humanError))],
            ["最新 shadow 动作", humanState(shadowLatest?.ai_shadow_action || "unknown"), shadowLatest ? `相对 baseline 的差异：${humanState(shadowLatest.shadow_action_type)}` : "最近还没有 shadow 动作"],
          ])
        : callout({
            title: "最近没有新的 AI 判断",
            copy: "当前如果是 baseline_only，AI 工作台只保留运行态说明，不会混入历史 AI 判断。",
            pills: [pill(`当前有效模式 ${humanState(runtime.effective_operating_mode || "unknown")}`, "outline")],
          }),
    }),
    aiHistory: surfaceCard({
      title: "AI 历史与 shadow 回放",
      kicker: "看最近判断、动作分歧和 shadow 收益比较",
      copy: "推荐的查看顺序是：先看最近 AI 判断，再看 shadow 是否会改动作，最后看 shadow 回放是否真的优于 baseline。",
      content: `
        <div class="panel-grid">
          <div class="span-12">
            ${table(
              ["时间", "市场判断", "是否建议覆盖", "交易经济性", "当前结论"],
              recentAssessments.map((item) => [
                `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
                `<div><strong>${humanState(item.regime || "unknown")}</strong><div class="table-meta">edge ${formatNumber(item.directional_edge ?? 0, 2)}</div></div>`,
                `<div><strong>${item.baseline_override_recommended ? "建议覆盖 baseline" : "不建议覆盖"}</strong><div class="table-meta">${listOrDash(item.override_reason_codes)}</div></div>`,
                `<div><strong>${item.economically_actionable ? "可交易" : "不建议交易"}</strong><div class="table-meta">净边际 ${formatNumber(item.estimated_net_edge_bps ?? 0, 2)} bps</div></div>`,
                `<div><strong>${item.fallback_used ? "fallback" : "provider"}</strong><div class="table-meta">${listOrDash((item.rejection_flags || item.validation_flags || []).map(humanError))}</div></div>`,
              ]),
              "最近还没有 AI 判断记录。",
            )}
            ${renderPaginationFooter(recentPayload, {
              key: "AI 判断记录",
              loadAction: "load-more-ai-assessments",
              collapseAction: "collapse-ai-assessments",
            })}
          </div>

          <div class="span-12">
            ${table(
              ["时间", "baseline 动作", "shadow 动作", "是否会覆盖", "动作差异"],
              shadowRecent.map((item) => [
                `<div><strong>${formatRelativeAge(item.created_at)}</strong><div class="table-meta">${formatMaybeTimestamp(item.created_at)}</div></div>`,
                `<div><strong>${humanState(item.baseline_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.baseline_target_qty ?? 0)}</div></div>`,
                `<div><strong>${humanState(item.ai_shadow_action || "unknown")}</strong><div class="table-meta">目标 ${formatNumber(item.ai_shadow_target_qty ?? 0)}</div></div>`,
                item.would_override_baseline ? pill("会改动 baseline 动作", "warning") : pill("与 baseline 一致", "positive"),
                `<div><strong>${humanState(item.shadow_action_type || "unknown")}</strong><div class="table-meta">${listOrDash(item.reason_codes)}</div></div>`,
              ]),
              "最近还没有 shadow 动作记录。",
            )}
            ${renderPaginationFooter(shadowRecentPayload, {
              key: "shadow 动作",
              loadAction: "load-more-ai-shadow-decisions",
              collapseAction: "collapse-ai-shadow-decisions",
            })}
          </div>

          <div class="span-12">
            ${table(
              ["评估窗口", "baseline 回放", "shadow 回放", "手续费压力", "结论"],
              evaluations.map((item) => [
                `<div><strong>${formatMaybeTimestamp(item.window_end)}</strong><div class="table-meta">${formatMaybeTimestamp(item.window_start)} ~ ${formatMaybeTimestamp(item.window_end)}</div></div>`,
                `<div><strong>净收益 ${formatNumber(item.baseline_net_pnl ?? 0)}</strong><div class="table-meta">毛收益 ${formatNumber(item.baseline_gross_pnl ?? 0)} / 交易 ${formatNumber(item.baseline_trade_count ?? 0, 0)}</div></div>`,
                `<div><strong>净收益 ${formatNumber(item.shadow_net_pnl ?? 0)}</strong><div class="table-meta">毛收益 ${formatNumber(item.shadow_gross_pnl ?? 0)} / 交易 ${formatNumber(item.shadow_trade_count ?? 0, 0)}</div></div>`,
                `<div><strong>baseline 费率 ${formatNumber(item.baseline_fee_ratio ?? 0, 3)}</strong><div class="table-meta">shadow 费率 ${formatNumber(item.shadow_fee_ratio ?? 0, 3)}</div></div>`,
                item.shadow_outperformed === null
                  ? pill("尚未得出结论", "outline")
                  : item.shadow_outperformed
                    ? pill("shadow 更优", "positive")
                    : pill("baseline 更优", "warning"),
              ]),
              "最近还没有 shadow 收益评估。",
            )}
            ${renderPaginationFooter(evaluationsPayload, {
              key: "shadow 收益评估",
              loadAction: "load-more-ai-shadow-evaluations",
              collapseAction: "collapse-ai-shadow-evaluations",
            })}
          </div>
        </div>
      `,
    }),
  };
}

export function renderAIView(data) {
  const sections = renderAISections(data);
  return `
    <div class="panel-grid">
      <div class="span-7">${sections.aiHero}</div>
      <div class="span-5">${sections.aiLatest}</div>
      <div class="span-12">${sections.aiHistory}</div>
    </div>
  `;
}

function aiRuntimeNarrative(runtime) {
  if (!runtime || Object.keys(runtime).length === 0) {
    return "当前还没有拿到 AI 主链运行状态。通常表示页面刚加载完成，或当前运行档位根本没有启用 AI。";
  }
  if (runtime.effective_operating_mode === "baseline_only" && runtime.configured_operating_mode !== "baseline_only") {
    return `AI 已进入自动降级状态。原因：${humanError(runtime.degradation_reason)}。当前真实交易只保留 baseline 主链，等恢复探测通过后才会重新尝试 AI。`;
  }
  if (runtime.effective_operating_mode === "baseline_only") {
    return "当前运行模式本来就是 baseline_only，所以 AI 不会参与真实交易，也不会接管方向。";
  }
  return "AI 当前仍在主链里，但是否真的接管方向，还要同时满足 override 建议、交易经济性、风控和执行状态等门禁。";
}

function renderPaginationFooter(payload, { key, loadAction, collapseAction }) {
  const shown = Array.isArray(payload?.assessments)
    ? payload.assessments.length
    : Array.isArray(payload?.shadow_decisions)
      ? payload.shadow_decisions.length
      : Array.isArray(payload?.evaluations)
        ? payload.evaluations.length
        : 0;
  const total = Number(payload?.total_available || shown);
  const hasMore = Boolean(payload?.has_more);
  const limit = Number(payload?.limit || shown);
  if (!shown) return "";
  return `
    <div class="history-footer">
      <p class="meta-copy">当前显示 ${shown} / ${total} 条${key}。</p>
      <div class="stack-actions">
        ${hasMore ? actionButton(`加载更多${key}`, loadAction, "", "secondary") : ""}
        ${limit > 8 ? actionButton("收起到最新 8 条", collapseAction, "", "ghost") : ""}
      </div>
    </div>
  `;
}
