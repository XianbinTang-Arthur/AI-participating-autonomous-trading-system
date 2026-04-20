"""Regression: order submission 埋点必须带 mode label, 供 P0-b alert 使用.

2026-04-20 P0-b Task 2.3 alert rule (deploy/wsl2-dev/grafana/provisioning/alerting/rules.yml)
依赖 Prometheus metric ``aats_orders_submitted_total{mode="baseline_only"}`` 才能 fire:

  - sev2-runtime-baseline-has-orders: baseline_only 下有订单提交 (authority_map 被绕过)
  - sev3-runtime-ai-decision-no-orders: ai_decision_maker 下 24h 零订单 (alpha/cost 阻断)

若有人改 aats/bootstrap/config.py 把 metrics.increment_labeled("orders_submitted", ...)
移除或改掉 mode label, 下游 alert 永不 fire, 违反观测层契约.

本测试用 inspect.getsource 静态扫描, 不需要启整条 execution 栈.
"""

from __future__ import annotations

import inspect
import re


def test_order_intents_generated_has_sibling_labeled_metric() -> None:
    """两处 metrics.increment("order_intents_generated") 必须伴随 increment_labeled.

    契约:
      1. `order_intents_generated` 保留 (向后兼容, 用于 /api routes 的 JSON)
      2. 紧跟 `increment_labeled("orders_submitted", labels={"mode": ...})`
      3. mode 值必须来自 settings.canonical_ai_operating_mode (不能硬编码)
    """
    from aats.bootstrap import config as config_mod

    src = inspect.getsource(config_mod)

    # 两处 order_intents_generated 都要有对应的 orders_submitted labeled metric
    order_intents_count = src.count('metrics.increment("order_intents_generated")')
    assert order_intents_count >= 2, (
        f"order_intents_generated 埋点数 = {order_intents_count}, 预期至少 2 "
        f"(execution leg intent 路径 + single-leg position target 路径)"
    )

    orders_submitted_labeled_count = len(
        re.findall(
            r'metrics\.increment_labeled\(\s*"orders_submitted"',
            src,
        )
    )
    assert orders_submitted_labeled_count >= 2, (
        f"orders_submitted labeled metric 埋点数 = {orders_submitted_labeled_count}, "
        f"预期至少 2 (与 order_intents_generated 一一对应). "
        f"若移除会导致 P0-b sev2/sev3 runtime governance alert 永不 fire."
    )


def test_orders_submitted_mode_label_comes_from_settings() -> None:
    """mode label 的值必须从 settings.canonical_ai_operating_mode 读, 不能硬编码.

    反模式: labels={"mode": "baseline_only"} — 硬编码导致 mode 切换时 label 不更新,
    authority_map 绕过的 attack 检测失效.
    """
    from aats.bootstrap import config as config_mod

    src = inspect.getsource(config_mod)

    # 找所有 orders_submitted labeled 调用, 确认 labels 引用 settings
    # 简化: 搜 increment_labeled("orders_submitted" 到下一个 ) 之间的段落,
    # 要求含 settings.canonical_ai_operating_mode
    matches = list(re.finditer(
        r'metrics\.increment_labeled\(\s*"orders_submitted"',
        src,
    ))
    assert len(matches) >= 2, "至少 2 处 orders_submitted 调用"

    # 检查每个 match 后 200 字符内是否含 settings.canonical_ai_operating_mode
    for m in matches:
        segment = src[m.start():m.start() + 400]
        assert "settings.canonical_ai_operating_mode" in segment, (
            f"orders_submitted @ char {m.start()} 附近没引用 "
            f"settings.canonical_ai_operating_mode; 禁止硬编码 mode label."
            f"\n片段: {segment[:300]!r}"
        )


def test_orders_submitted_increment_wrapped_in_try_except() -> None:
    """metrics.increment_labeled 必须 wrap 在 try/except, 异常不得阻断订单流.

    P0-b Task 2.4 设计原则: metrics 异常永不阻断业务逻辑. order submission
    是关键路径, 指标上报失败绝不能让订单本身失败.
    """
    from aats.bootstrap import config as config_mod

    src = inspect.getsource(config_mod)

    # 简化: 搜 increment_labeled("orders_submitted" 前面 100 字符内应有 "try:"
    for m in re.finditer(r'metrics\.increment_labeled\(\s*"orders_submitted"', src):
        # 往前看 200 字符找 "try:"
        preceding = src[max(0, m.start() - 200):m.start()]
        assert "try:" in preceding, (
            f"orders_submitted @ char {m.start()} 前 200 字符没 try:, "
            f"违反 'metrics 异常不阻断订单流' 契约"
        )


# ─────────────────────────────────────────────────────────────────────
# 行为级单测 (2026-04-20 code review A-H3 fix)
# 上面 3 个静态扫描 test 只能抓"字符出现与否", 抓不到:
#   - MetricsRegistry.increment_labeled signature mismatch (调用不传 labels)
#   - labels dict 实际不含 mode 键 (str(enum) 失效)
#   - labeled counter 真没递增 (调用写错)
# 本组测试直接跑 MetricsRegistry API, 锁死行为契约.
# ─────────────────────────────────────────────────────────────────────


def test_metrics_registry_increment_labeled_accepts_mode_label() -> None:
    """MetricsRegistry.increment_labeled 必须接受 labels={"mode": <str>} 参数.

    这是 config.py 两处 orders_submitted 调用的 call signature; 若 MetricsRegistry
    API 改了名或改了参数签名, 两处调用会 AttributeError/TypeError, 但被
    `except Exception: pass` 吞掉 → metric 永不 emit 仍看起来"工作正常".
    """
    from aats.bootstrap.metrics import MetricsRegistry

    registry = MetricsRegistry()

    # 三种 canonical mode 分别增一次
    for mode in ("baseline_only", "ai_assisted", "ai_decision_maker"):
        registry.increment_labeled(
            "orders_submitted",
            labels={"mode": mode},
            value=1,
        )

    snap = registry.labeled_snapshot()
    # labeled_snapshot 格式: {(metric_name, tuple(sorted(labels.items()))): count}
    assert snap.get(("orders_submitted", (("mode", "baseline_only"),))) == 1
    assert snap.get(("orders_submitted", (("mode", "ai_assisted"),))) == 1
    assert snap.get(("orders_submitted", (("mode", "ai_decision_maker"),))) == 1


def test_metrics_registry_labels_isolated_per_mode() -> None:
    """不同 mode label 之间 counter 独立 (不会被意外合并).

    这是 Prometheus sum(rate(...{mode="baseline_only"}[24h])) 正确 dispatching
    的前提. 若 label 隔离失败, 三个 mode 的 counter 会汇总到同一 series,
    P0-b Task 2.3 sev2/sev3 alert 会误触发或永不触发.
    """
    from aats.bootstrap.metrics import MetricsRegistry

    registry = MetricsRegistry()

    registry.increment_labeled("orders_submitted", labels={"mode": "baseline_only"}, value=3)
    registry.increment_labeled("orders_submitted", labels={"mode": "ai_assisted"}, value=7)

    snap = registry.labeled_snapshot()
    assert snap.get(("orders_submitted", (("mode", "baseline_only"),))) == 3
    assert snap.get(("orders_submitted", (("mode", "ai_assisted"),))) == 7
    # ai_decision_maker 没调过 → 不应有 entry
    assert ("orders_submitted", (("mode", "ai_decision_maker"),)) not in snap


def test_metrics_registry_plain_vs_labeled_separated() -> None:
    """plain `increment("order_intents_generated")` 和 `increment_labeled(...)` 互不污染.

    config.py 两处都保留了 plain 版本供 /api routes JSON 使用; 若 MetricsRegistry
    把 labeled snapshot 归到 plain snapshot, legacy /api 会报"order_intents_generated=
    0 但 orders_submitted_total=N", 暴露不一致.
    """
    from aats.bootstrap.metrics import MetricsRegistry

    registry = MetricsRegistry()

    registry.increment("order_intents_generated", value=5)
    registry.increment_labeled("orders_submitted", labels={"mode": "baseline_only"}, value=5)

    # plain snapshot 只含 order_intents_generated (不被 labeled 污染)
    plain = registry.snapshot()
    assert plain == {"order_intents_generated": 5}

    # labeled snapshot 只含 labeled (不被 plain 污染)
    labeled = registry.labeled_snapshot()
    assert labeled == {("orders_submitted", (("mode", "baseline_only"),)): 5}
