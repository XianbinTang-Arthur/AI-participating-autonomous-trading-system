"""Deterministic research allocation policy for the Research Factory."""

from aats.data_platform.research_factory.allocation.policy import (
    ALLOCATION_ARMS,
    DEFAULT_REWARD_WEIGHTS,
    AllocationDecision,
    ResearchAllocationInput,
    choose_next_research_action,
)

__all__ = [
    "ALLOCATION_ARMS",
    "DEFAULT_REWARD_WEIGHTS",
    "AllocationDecision",
    "ResearchAllocationInput",
    "choose_next_research_action",
]
