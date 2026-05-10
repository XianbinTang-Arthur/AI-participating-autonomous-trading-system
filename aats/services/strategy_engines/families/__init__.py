from __future__ import annotations

from importlib import import_module

__all__ = [
    "DirectionalFamilyAdapter",
    "ExistingCandidateFamilyAdapter",
    "IndependentFamilyEngine",
    "StrategyFamilyRegistry",
]


def __getattr__(name: str) -> object:
    if name == "IndependentFamilyEngine":
        return getattr(import_module(".independent_family", __name__), name)
    if name == "DirectionalFamilyAdapter":
        return getattr(import_module(".legacy_adapters", __name__), name)
    if name == "ExistingCandidateFamilyAdapter":
        return getattr(import_module(".legacy_adapters", __name__), name)
    if name == "StrategyFamilyRegistry":
        return getattr(import_module(".registry", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
