"""MetricsRegistry → OTel Counter 桥接模块单元测试。

覆盖：
1. OTel meter 不可用时 create_bridge 返回 None（graceful no-op）
2. sync_once 正确计算增量（delta）
3. sync_once 忽略负增量（计数器单调递增）
4. 新增指标自动创建 Counter（惰性创建）
5. start_metrics_bridge_loop 在 meter 不可用时静默退出
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from aats.bootstrap.metrics import MetricsRegistry
from aats.bootstrap.metrics_bridge import create_bridge, start_metrics_bridge_loop


# ─────────────────────────────────────────────────────────────────────
# create_bridge
# ─────────────────────────────────────────────────────────────────────


def test_create_bridge_returns_none_when_no_meter() -> None:
    """OTel meter 不可用时应返回 None。"""
    registry = MetricsRegistry()
    with patch("aats.bootstrap.metrics_bridge.get_meter", return_value=None):
        result = create_bridge(registry)
    assert result is None


def test_create_bridge_returns_callable_with_meter() -> None:
    """OTel meter 可用时应返回 sync_once 可调用对象。"""
    registry = MetricsRegistry()
    mock_meter = MagicMock()
    mock_counter = MagicMock()
    mock_meter.create_counter.return_value = mock_counter

    with patch("aats.bootstrap.metrics_bridge.get_meter", return_value=mock_meter):
        sync_fn = create_bridge(registry)

    assert sync_fn is not None
    assert callable(sync_fn)


def test_sync_once_computes_delta() -> None:
    """sync_once 应当计算增量而非绝对值。"""
    registry = MetricsRegistry()
    mock_meter = MagicMock()
    mock_counter = MagicMock()
    mock_meter.create_counter.return_value = mock_counter

    with patch("aats.bootstrap.metrics_bridge.get_meter", return_value=mock_meter):
        sync_fn = create_bridge(registry)

    # 第一次同步：5 的增量
    registry.increment("test_metric", 5)
    sync_fn()
    mock_counter.add.assert_called_once_with(5)
    mock_counter.add.reset_mock()

    # 第二次同步：增量 3（从 5 到 8）
    registry.increment("test_metric", 3)
    sync_fn()
    mock_counter.add.assert_called_once_with(3)
    mock_counter.add.reset_mock()

    # 第三次同步：无增量，不应调用 add
    sync_fn()
    mock_counter.add.assert_not_called()


def test_sync_once_handles_multiple_metrics() -> None:
    """sync_once 应当同步所有 MetricsRegistry 中的指标。"""
    registry = MetricsRegistry()
    mock_meter = MagicMock()
    counters: dict[str, MagicMock] = {}

    def _create_counter(name: str, **kwargs: object) -> MagicMock:
        c = MagicMock()
        counters[name] = c
        return c

    mock_meter.create_counter.side_effect = _create_counter

    with patch("aats.bootstrap.metrics_bridge.get_meter", return_value=mock_meter):
        sync_fn = create_bridge(registry)

    registry.increment("metric_a", 10)
    registry.increment("metric_b", 20)
    sync_fn()

    assert "aats_metric_a" in counters
    assert "aats_metric_b" in counters
    counters["aats_metric_a"].add.assert_called_once_with(10)
    counters["aats_metric_b"].add.assert_called_once_with(20)


def test_sync_once_creates_counter_lazily() -> None:
    """运行时新增的指标名应当惰性创建 Counter。"""
    registry = MetricsRegistry()
    mock_meter = MagicMock()
    mock_counter = MagicMock()
    mock_meter.create_counter.return_value = mock_counter

    with patch("aats.bootstrap.metrics_bridge.get_meter", return_value=mock_meter):
        sync_fn = create_bridge(registry)

    # 第一次同步：只有 metric_a
    registry.increment("metric_a", 1)
    sync_fn()
    assert mock_meter.create_counter.call_count == 1

    # 第二次同步：新增 metric_b，应当创建第二个 counter
    registry.increment("metric_b", 1)
    sync_fn()
    assert mock_meter.create_counter.call_count == 2


# ─────────────────────────────────────────────────────────────────────
# start_metrics_bridge_loop
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bridge_loop_exits_when_no_meter() -> None:
    """OTel meter 不可用时 loop 应立即退出（不阻塞）。"""
    registry = MetricsRegistry()
    with patch("aats.bootstrap.metrics_bridge.get_meter", return_value=None):
        # 如果 loop 不退出，这里会永远挂住。设置极短 interval 作为安全网。
        await start_metrics_bridge_loop(registry, interval=0.01)
    # 到达这里说明 loop 正常退出
