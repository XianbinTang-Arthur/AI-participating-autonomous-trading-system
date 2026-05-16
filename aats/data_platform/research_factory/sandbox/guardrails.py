"""Guardrails for research-only sandbox proposals."""

from __future__ import annotations

import ast
import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aats.data_platform.research_factory.sandbox.proposal import (
    SandboxProposal,
    normalize_sandbox_path,
)

DEFAULT_ALLOWED_WRITE_ROOTS = (
    "artifacts/research/research_factory/tmp/",
    "artifacts/research/research_factory/generated/",
    "configs/research_factory/generated/",
    "aats/data_platform/research_factory/generated/",
)
DEFAULT_DENIED_ENV_PATTERNS = (".env", "OKX", "SECRET", "TOKEN", "PASSWORD", "KEY")
DEFAULT_DENIED_PATH_PATTERNS = (
    ".env*",
    "**/.env*",
    "deploy/*",
    "deploy/**",
    "configs/templates/*live*",
    "configs/templates/**/*live*",
    "configs/templates/*credential*",
    "configs/templates/**/*credential*",
    "*credential*",
    "**/*credential*",
    "*credentials*",
    "**/*credentials*",
    "*live*template*",
    "**/*live*template*",
    "*template*live*",
    "**/*template*live*",
    "*.live",
    "**/*.live",
    "aats/data_platform/research_factory/sandbox/*",
    "aats/data_platform/research_factory/sandbox/**",
    "aats/data_platform/research_factory/metrics/*",
    "aats/data_platform/research_factory/metrics/**",
    "aats/data_platform/research_factory/experiments/*",
    "aats/data_platform/research_factory/experiments/**",
    "aats/data_platform/research_factory/datasets/*",
    "aats/data_platform/research_factory/datasets/**",
)
DEFAULT_FORBIDDEN_OUTPUT_TERMS = (
    "active_parameter",
    "active_parameters",
    "apply",
    "live_order",
    "okx_write",
    "operator_write",
    "production_config",
)
FORBIDDEN_IMPORT_ROOTS = (
    "aiohttp",
    "boto3",
    "ccxt",
    "ftplib",
    "httpx",
    "okx",
    "os",
    "paramiko",
    "requests",
    "socket",
    "subprocess",
    "urllib",
)
NETWORK_CALL_HINTS = (
    "requests.",
    "httpx.",
    "urllib.request",
    "aiohttp.",
    "socket.",
    "http://",
    "https://",
)
ENV_ACCESS_HINTS = (
    "os.environ",
    "os.getenv",
    "getenv(",
    "environ[",
)
KEY_SECRET_PATTERN = re.compile(
    r"\b(?:api|secret|private|access|okx)[_-]?key\b|\bkey\s*=",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Static policy for validating untrusted research sandbox proposals."""

    allowed_write_roots: Sequence[str] = field(default_factory=lambda: DEFAULT_ALLOWED_WRITE_ROOTS)
    denied_env_patterns: Sequence[str] = field(default_factory=lambda: DEFAULT_DENIED_ENV_PATTERNS)
    denied_path_patterns: Sequence[str] = field(default_factory=lambda: DEFAULT_DENIED_PATH_PATTERNS)
    forbidden_output_terms: Sequence[str] = field(default_factory=lambda: DEFAULT_FORBIDDEN_OUTPUT_TERMS)

    def __post_init__(self) -> None:
        allowed_write_roots = tuple(
            normalize_sandbox_path(root, "allowed_write_roots").rstrip("/")
            for root in _require_string_sequence(
                self.allowed_write_roots,
                "allowed_write_roots",
                allow_empty=False,
            )
        )
        denied_env_patterns = _require_string_sequence(
            self.denied_env_patterns,
            "denied_env_patterns",
            allow_empty=False,
        )
        denied_path_patterns = _require_string_sequence(
            self.denied_path_patterns,
            "denied_path_patterns",
            allow_empty=False,
        )
        forbidden_output_terms = _require_string_sequence(
            self.forbidden_output_terms,
            "forbidden_output_terms",
            allow_empty=False,
        )
        object.__setattr__(self, "allowed_write_roots", allowed_write_roots)
        object.__setattr__(self, "denied_env_patterns", denied_env_patterns)
        object.__setattr__(self, "denied_path_patterns", denied_path_patterns)
        object.__setattr__(self, "forbidden_output_terms", forbidden_output_terms)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SandboxPolicy":
        """Build a policy from JSON-compatible mapping data."""
        if not isinstance(value, Mapping):
            raise ValueError("sandbox policy must be a mapping")
        return cls(
            allowed_write_roots=value.get("allowed_write_roots", DEFAULT_ALLOWED_WRITE_ROOTS),
            denied_env_patterns=value.get("denied_env_patterns", DEFAULT_DENIED_ENV_PATTERNS),
            denied_path_patterns=value.get("denied_path_patterns", DEFAULT_DENIED_PATH_PATTERNS),
            forbidden_output_terms=value.get("forbidden_output_terms", DEFAULT_FORBIDDEN_OUTPUT_TERMS),
        )


def validate_sandbox_proposal(
    proposal: SandboxProposal,
    policy: SandboxPolicy | None = None,
) -> SandboxProposal:
    """Validate a proposal without executing generated code or touching live systems."""
    if not isinstance(proposal, SandboxProposal):
        raise ValueError("proposal must be a SandboxProposal")
    policy = policy or SandboxPolicy()
    if not isinstance(policy, SandboxPolicy):
        raise ValueError("policy must be a SandboxPolicy")

    for path in proposal.read_paths:
        _reject_denied_path(path, policy, "read path")

    for path in proposal.write_paths:
        _reject_denied_path(path, policy, "write path")
        if not _is_allowed_write_path(path, policy.allowed_write_roots):
            allowed = ", ".join(policy.allowed_write_roots)
            raise ValueError(f"write path {path!r} is outside allowed sandbox roots: {allowed}")

    _reject_forbidden_output_terms(proposal.outputs, policy, "proposal outputs")
    _reject_forbidden_output_terms(proposal.metadata, policy, "proposal metadata")
    return proposal


def scan_candidate_patch(
    changed_paths: Sequence[str],
    text_blobs: Mapping[str, str] | Sequence[str],
    policy: SandboxPolicy | None = None,
) -> tuple[str, ...]:
    """Fail-closed static scan for candidate sandbox code patches."""
    policy = policy or SandboxPolicy()
    if not isinstance(policy, SandboxPolicy):
        raise ValueError("policy must be a SandboxPolicy")
    if not isinstance(changed_paths, Sequence) or isinstance(changed_paths, str | bytes | bytearray):
        raise ValueError("changed_paths must be a sequence of paths")
    normalized_paths = tuple(
        normalize_sandbox_path(path, "changed path")
        for path in changed_paths
    )
    if not normalized_paths:
        raise ValueError("changed_paths must not be empty")

    for path in normalized_paths:
        _reject_denied_path(path, policy, "changed path")
        if not _is_allowed_write_path(path, policy.allowed_write_roots):
            allowed = ", ".join(policy.allowed_write_roots)
            raise ValueError(f"changed path {path!r} is outside allowed sandbox roots: {allowed}")

    for context, text in _normalize_text_blobs(text_blobs):
        _scan_text_blob(context, text, policy)

    return normalized_paths


def _reject_denied_path(path: str, policy: SandboxPolicy, context: str) -> None:
    lowered = path.lower()
    for pattern in policy.denied_path_patterns:
        if fnmatch.fnmatchcase(lowered, pattern.lower()):
            raise ValueError(f"{context} {path!r} matches denied path pattern {pattern!r}")
    _reject_denied_env_text(path, policy, context)


def _is_allowed_write_path(path: str, allowed_roots: Sequence[str]) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in allowed_roots)


def _reject_forbidden_output_terms(value: Any, policy: SandboxPolicy, context: str) -> None:
    for text in _iter_text(value):
        lowered = text.lower()
        for term in policy.forbidden_output_terms:
            if term.lower() in lowered:
                raise ValueError(f"{context} contains forbidden output term: {term}")
        _reject_denied_env_text(text, policy, context)


def _normalize_text_blobs(text_blobs: Mapping[str, str] | Sequence[str]) -> tuple[tuple[str, str], ...]:
    if isinstance(text_blobs, Mapping):
        result: list[tuple[str, str]] = []
        for path, text in text_blobs.items():
            context = normalize_sandbox_path(str(path), "text blob path")
            if not isinstance(text, str):
                raise ValueError(f"text blob {context!r} must be a string")
            result.append((context, text))
        return tuple(result)
    if not isinstance(text_blobs, Sequence) or isinstance(text_blobs, str | bytes | bytearray):
        raise ValueError("text_blobs must be a mapping or sequence of strings")
    result = []
    for index, text in enumerate(text_blobs):
        if not isinstance(text, str):
            raise ValueError(f"text blob {index} must be a string")
        result.append((f"text blob {index}", text))
    return tuple(result)


def _scan_text_blob(context: str, text: str, policy: SandboxPolicy) -> None:
    _reject_forbidden_imports(context, text)
    _reject_network_hints(context, text)
    _reject_env_access_hints(context, text)
    _reject_secret_text(context, text, policy)


def _reject_forbidden_imports(context: str, text: str) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None

    if tree is not None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    _reject_forbidden_import_root(context, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                _reject_forbidden_import_root(context, node.module)

    for module_name in FORBIDDEN_IMPORT_ROOTS:
        pattern = rf"(?m)^\s*(?:from\s+{re.escape(module_name)}\b|import\s+{re.escape(module_name)}\b)"
        if re.search(pattern, text):
            raise ValueError(f"{context} imports forbidden module: {module_name}")


def _reject_forbidden_import_root(context: str, module_name: str) -> None:
    root = module_name.split(".", 1)[0]
    if root in FORBIDDEN_IMPORT_ROOTS:
        raise ValueError(f"{context} imports forbidden module: {root}")


def _reject_network_hints(context: str, text: str) -> None:
    lowered = text.lower()
    for hint in NETWORK_CALL_HINTS:
        if hint in lowered:
            raise ValueError(f"{context} contains network call hint: {hint}")


def _reject_env_access_hints(context: str, text: str) -> None:
    lowered = text.lower()
    for hint in ENV_ACCESS_HINTS:
        if hint in lowered:
            raise ValueError(f"{context} contains environment access hint: {hint}")


def _reject_secret_text(context: str, text: str, policy: SandboxPolicy) -> None:
    lowered = text.lower()
    for pattern in policy.denied_env_patterns:
        pattern_lower = pattern.lower()
        if pattern_lower == "key":
            if KEY_SECRET_PATTERN.search(text):
                raise ValueError(f"{context} contains secret pattern: {pattern}")
        elif pattern_lower in lowered:
            raise ValueError(f"{context} contains secret pattern: {pattern}")


def _reject_denied_env_text(value: str, policy: SandboxPolicy, context: str) -> None:
    lowered = value.lower()
    for pattern in policy.denied_env_patterns:
        if pattern.lower() in lowered:
            raise ValueError(f"{context} contains denied environment pattern: {pattern}")


def _iter_text(value: Any) -> tuple[str, ...]:
    text_values: list[str] = []
    _collect_text(value, text_values)
    return tuple(text_values)


def _collect_text(value: Any, output: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            output.append(str(key))
            _collect_text(item, output)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for item in value:
            _collect_text(item, output)
        return
    if isinstance(value, str):
        output.append(value)


def _require_string_sequence(
    values: Sequence[str],
    field_name: str,
    *,
    allow_empty: bool,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes | bytearray):
        raise ValueError(f"{field_name} must be a sequence of strings")
    result = tuple(values)
    if not allow_empty and not result:
        raise ValueError(f"{field_name} must not be empty")
    if not all(isinstance(value, str) and value.strip() for value in result):
        raise ValueError(f"{field_name} must contain non-empty strings")
    return result
