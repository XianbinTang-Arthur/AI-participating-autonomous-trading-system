"""Base adapter interface for strategy family replay.

Phase 2 设计决策 §9：Strategy Adapter Layer 负责把不同 family 包装成统一接口。
不允许把 replay runner 写死成只支持某一个 family。
"""

from __future__ import annotations

import abc

from aats.data_platform.replay.core.replay_context import (
    ReplayBarContext,
    ReplayDecision,
    ReplayState,
)


class BaseReplayAdapter(abc.ABC):
    """所有策略 family replay adapter 的抽象基类。

    每个 family adapter 必须实现：
    - family_name    策略家族名称
    - evaluate_bar   逐 bar 评估，返回 ReplayDecision
    - reset_state    重置内部状态（新实验开始时调用）
    """

    @property
    @abc.abstractmethod
    def family_name(self) -> str:
        """返回策略家族名称，如 'independent' 或 'directional'。"""

    @property
    def algorithm_version(self) -> str:
        """Stable behavior version persisted in replay evidence artifacts.

        The empty compatibility sentinel keeps third-party legacy subclasses
        instantiable.  Versioned backtest preflight rejects it before I/O, so a
        subclass must override this property before it can emit trusted evidence.
        """

        return ""

    @property
    def accepted_parameter_keys(self) -> frozenset[str]:
        """Explicit override keys that this adapter behaviorally consumes.

        An empty default is migration-safe and fail-closed: legacy adapters may
        run their old read-only path, but cannot silently consume overrides in
        the versioned harness.
        """

        return frozenset()

    @abc.abstractmethod
    def evaluate_bar(self, ctx: ReplayBarContext) -> ReplayDecision:
        """对单根 bar 做策略评估。

        参数:
            ctx: 包含当前 bar 数据、累积状态、参数覆盖的完整上下文

        返回:
            ReplayDecision，包含该 bar 的评估结果和决策
        """

    @abc.abstractmethod
    def reset_state(self) -> ReplayState:
        """重置 adapter 内部状态，返回全新的 ReplayState。

        在每次新实验开始前调用。
        """
