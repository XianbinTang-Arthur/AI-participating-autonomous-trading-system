from __future__ import annotations

__all__ = ["StrategyCoordinatorService"]


def __getattr__(name: str) -> object:
    if name == "StrategyCoordinatorService":
        from .coordinator import StrategyCoordinatorService

        return StrategyCoordinatorService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
