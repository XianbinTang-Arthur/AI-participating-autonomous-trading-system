#!/usr/bin/env python3
"""Write a no-secret, non-overwriting simulation deployment evidence packet."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CONTAINER_RE = re.compile(r"^aats-[a-z0-9-]+$")
_CONTAINER_PORT_RE = re.compile(r"^[1-9][0-9]{0,4}/(?:tcp|udp)$")
_READINESS_GENERATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SIMULATION_PROFILES = frozenset({"spot", "derivatives"})
_LOOPBACK_HOST_IPS = frozenset({"127.0.0.1", "::1"})
_COLLECTOR_HEARTBEATS = {
    "aats-liquidations-daemon": "/tmp/aats_liquidations_heartbeat",
    "aats-microstructure-collector": "/tmp/aats_microstructure_heartbeat",
}
_COLLECTOR_MAX_HEARTBEAT_AGE_SECONDS = 60.0
_COLLECTOR_MAX_FUTURE_SKEW_SECONDS = 5.0


CommandRunner = Callable[[Sequence[str], Path | None], str]
Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_command(args: Sequence[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return completed.stdout.strip()


def _container_fact(name: str, run: CommandRunner) -> dict[str, str]:
    if not _CONTAINER_RE.fullmatch(name):
        raise ValueError("invalid_required_container_name")
    status = run(("docker", "inspect", "--format", "{{.State.Status}}", name), None)
    health = run(
        (
            "docker",
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            name,
        ),
        None,
    )
    image_id = run(("docker", "inspect", "--format", "{{.Image}}", name), None)
    if status != "running" or health != "healthy":
        raise RuntimeError(f"required_container_not_healthy:{name}:{status}:{health}")
    if not _IMAGE_RE.fullmatch(image_id):
        raise RuntimeError(f"invalid_container_image_id:{name}")
    return {"name": name, "status": status, "health": health, "image_id": image_id}


def _collector_heartbeat_fact(
    name: str,
    *,
    run: CommandRunner,
    now: datetime | None = None,
    clock: Clock = _utc_now,
) -> dict[str, object]:
    heartbeat_path = _COLLECTOR_HEARTBEATS[name]
    raw_epoch = run(("docker", "exec", name, "stat", "-c", "%Y", heartbeat_path), None)
    if not raw_epoch.isdigit():
        raise RuntimeError(f"invalid_collector_heartbeat_epoch:{name}")
    try:
        heartbeat_at = datetime.fromtimestamp(int(raw_epoch), tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeError(f"invalid_collector_heartbeat_epoch:{name}") from exc
    observed_at = now or clock()
    raw_age_seconds = (observed_at - heartbeat_at).total_seconds()
    if raw_age_seconds < -_COLLECTOR_MAX_FUTURE_SKEW_SECONDS:
        raise RuntimeError(f"collector_heartbeat_in_future:{name}:{raw_age_seconds:.3f}")
    age_seconds = max(0.0, raw_age_seconds)
    if age_seconds >= _COLLECTOR_MAX_HEARTBEAT_AGE_SECONDS:
        raise RuntimeError(f"collector_heartbeat_stale:{name}:{age_seconds:.3f}")
    return {
        "name": name,
        "heartbeat_path": heartbeat_path,
        "heartbeat_at": heartbeat_at.isoformat(),
        "heartbeat_age_seconds": round(age_seconds, 3),
        "fresh": True,
    }


def _gateway_published_bindings(run: CommandRunner) -> list[dict[str, str]]:
    raw = run(
        (
            "docker",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Ports}}",
            "aats-gateway",
        ),
        None,
    )
    try:
        published_ports = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid_gateway_published_ports_json") from exc
    if not isinstance(published_ports, dict):
        raise RuntimeError("invalid_gateway_published_ports_shape")

    result: list[dict[str, str]] = []
    for container_port, bindings in published_ports.items():
        if not isinstance(container_port, str) or not _CONTAINER_PORT_RE.fullmatch(container_port):
            raise RuntimeError("invalid_gateway_container_port")
        if int(container_port.split("/", maxsplit=1)[0]) > 65535:
            raise RuntimeError("invalid_gateway_container_port")
        if not isinstance(bindings, list) or not bindings:
            continue
        for binding in bindings:
            if not isinstance(binding, dict):
                raise RuntimeError("invalid_gateway_published_binding")
            host_ip = binding.get("HostIp")
            host_port = binding.get("HostPort")
            if not isinstance(host_ip, str) or host_ip not in _LOOPBACK_HOST_IPS:
                display_host = host_ip if isinstance(host_ip, str) and host_ip else "all_interfaces"
                raise RuntimeError(f"gateway_published_on_non_loopback:{display_host}")
            if not isinstance(host_port, str) or not host_port.isdigit():
                raise RuntimeError("invalid_gateway_host_port")
            port_number = int(host_port)
            if not 1 <= port_number <= 65535:
                raise RuntimeError("invalid_gateway_host_port")
            result.append(
                {
                    "container_port": container_port,
                    "host_ip": host_ip,
                    "host_port": host_port,
                }
            )
    if not result:
        raise RuntimeError("gateway_has_no_published_loopback_binding")
    return sorted(result, key=lambda item: (item["container_port"], item["host_ip"], item["host_port"]))


def build_evidence(
    *,
    repo_root: Path,
    profile: str,
    overlay: str,
    schema_job_status: str,
    runtime_readiness_generation: str,
    required_containers: Sequence[str],
    run: CommandRunner = _run_command,
    generated_at: datetime | None = None,
    clock: Clock = _utc_now,
) -> dict[str, object]:
    if profile not in _SIMULATION_PROFILES:
        raise ValueError("deployment_evidence_requires_simulation_profile")
    if schema_job_status != "passed":
        raise ValueError("schema_job_not_passed")
    if not _READINESS_GENERATION_RE.fullmatch(runtime_readiness_generation):
        raise ValueError("invalid_runtime_readiness_generation")
    if not required_containers:
        raise ValueError("required_container_list_empty")
    if "aats-gateway" not in required_containers:
        raise ValueError("required_gateway_missing")

    commit = run(("git", "rev-parse", "HEAD"), repo_root)
    base_image_id = run(
        ("docker", "image", "inspect", "aats-base:dev", "--format", "{{.Id}}"),
        None,
    )
    if not _COMMIT_RE.fullmatch(commit):
        raise RuntimeError("invalid_deployed_commit")
    if not _IMAGE_RE.fullmatch(base_image_id):
        raise RuntimeError("invalid_base_image_id")

    now = generated_at or clock()
    container_facts = [_container_fact(name, run) for name in required_containers]
    collector_freshness = [
        _collector_heartbeat_fact(
            name,
            run=run,
            now=generated_at,
            clock=clock,
        )
        for name in required_containers
        if name in _COLLECTOR_HEARTBEATS
    ]
    gateway_bindings = _gateway_published_bindings(run)
    return {
        "format_version": 1,
        "generated_at": now.isoformat(),
        "status": "simulation_stack_healthy",
        "production_ready": False,
        "trading_ready": False,
        "deployed_commit": commit,
        "base_image_id": base_image_id,
        "profile": profile,
        "compose_overlay": overlay,
        "runtime_readiness_generation": runtime_readiness_generation,
        "schema_contract": {
            "job_status": schema_job_status,
            "clone_manifest_verified": False,
            "consistent_rollback_verified": False,
        },
        "required_containers": container_facts,
        "collector_freshness": collector_freshness,
        "gateway_published_bindings": gateway_bindings,
        "runtime_unknowns": [
            "production_account_and_exchange_not_verified",
            "production_schema_manifest_not_verified",
            "app_schema_parameter_rollback_not_verified",
            "trading_readiness_packet_not_verified",
        ],
    }


def write_evidence(*, repo_root: Path, payload: dict[str, object]) -> Path:
    evidence_dir = repo_root / "deploy" / "wsl2-dev" / "runtime" / "deployment-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.fromisoformat(str(payload["generated_at"]))
    timestamp = generated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    commit = str(payload["deployed_commit"])
    profile = str(payload["profile"])
    target = evidence_dir / f"{timestamp}-{profile}-{commit[:12]}.json"
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    target.chmod(0o444)
    return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--overlay", required=True)
    parser.add_argument("--schema-job-status", required=True)
    parser.add_argument("--runtime-readiness-generation", required=True)
    parser.add_argument("--required-container", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root.resolve()
    payload = build_evidence(
        repo_root=repo_root,
        profile=args.profile,
        overlay=args.overlay,
        schema_job_status=args.schema_job_status,
        runtime_readiness_generation=args.runtime_readiness_generation,
        required_containers=args.required_container,
    )
    target = write_evidence(repo_root=repo_root, payload=payload)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
