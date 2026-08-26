"""Resumable execution of a capacity-approved historical campaign."""

from __future__ import annotations

import contextlib
import io
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import httpx
from sqlalchemy import text

from aats.data_platform.data_governance.coverage import git_commit
from aats.data_platform.data_governance.historical_campaign import (
    download_verified_file,
    finish_campaign,
    observe_capacity,
    start_campaign,
    update_campaign_checkpoint,
    validate_campaign_manifest,
)
from aats.data_platform.data_governance.historical_gold import (
    execute_historical_gold,
    fail_historical_gold,
    plan_historical_gold,
    start_historical_gold,
)
from aats.data_platform.data_governance.historical_rebuild import (
    execute_historical_rebuild,
    fail_historical_rebuild,
    plan_historical_rebuild,
    start_historical_rebuild,
)
from aats.data_platform.db import get_session


def run_historical_campaign(
    *,
    campaign_id: str,
    storage_root: Path,
    project_root: Path,
    resume_running: bool = False,
) -> dict[str, Any]:
    """Execute public-data work only; never touches account/private/live APIs."""

    storage_root = storage_root.expanduser().resolve()
    project_root = project_root.expanduser().resolve()
    with get_session() as session:
        state = start_campaign(session, campaign_id, resume_running=resume_running)
    if state["status"] == "already_succeeded":
        return {"campaign_id": campaign_id, "status": "already_succeeded"}
    try:
        manifest = state["manifest"]
        validate_campaign_manifest(manifest)
        start = datetime.fromisoformat(str(manifest["coverage_start"]))
        end = datetime.fromisoformat(str(manifest["coverage_end"]))
        days = int(manifest["requested_days"])
        with get_session() as session:
            current_capacity = observe_capacity(
                session,
                storage_root,
                requested_days=days,
            )
        if not current_capacity.approved:
            raise RuntimeError(current_capacity.reason_code)

        download_dir = storage_root / "downloads"
        raw_dir = storage_root / "raw"
        download_dir.mkdir(parents=True, exist_ok=True)
        raw_dir.mkdir(parents=True, exist_ok=True)
        bundle_ids = _run_campaign_inputs(
            campaign_id=campaign_id,
            manifest=manifest,
            start=start,
            end=end,
            download_dir=download_dir,
            raw_dir=raw_dir,
            project_root=project_root,
            checkpoint=state.get("checkpoint") or {},
        )
        artifacts: dict[str, Any] = {}
        for timeframe in ("15m", "1H"):
            with get_session() as session:
                plan = plan_historical_gold(
                    session,
                    symbol=str(manifest["symbol"]),
                    timeframe=timeframe,
                    candle_bundle_id=bundle_ids[f"candle:{timeframe}"],
                    funding_bundle_id=bundle_ids["funding"],
                    auxiliary_bundle_ids=(
                        bundle_ids[f"mark:{timeframe}"],
                        *bundle_ids["trade"],
                        *bundle_ids["l2"],
                    ),
                    coverage_start=start,
                    coverage_end=end,
                    git_commit=git_commit(str(project_root)),
                )
                status, artifact_id = start_historical_gold(session, plan)
            if status == "already_succeeded":
                artifacts[timeframe] = {"artifact_id": artifact_id, "status": status}
            else:
                try:
                    with get_session() as session:
                        result = execute_historical_gold(
                            session,
                            plan,
                            artifact_id=artifact_id,
                        )
                except Exception as exc:
                    try:
                        with get_session() as session:
                            fail_historical_gold(
                                session,
                                artifact_id,
                                type(exc).__name__,
                            )
                    except Exception as state_exc:
                        raise ExceptionGroup(
                            "historical Gold execution and terminal-state update failed",
                            [exc, state_exc],
                        ) from exc
                    raise
                artifacts[timeframe] = asdict(result)
            _checkpoint(
                campaign_id,
                f"gold:{timeframe}",
                {"status": "succeeded", "artifact_id": artifact_id},
            )
        with get_session() as session:
            finish_campaign(session, campaign_id, succeeded=True)
        return {
            "campaign_id": campaign_id,
            "status": "succeeded",
            "bundle_ids": bundle_ids,
            "artifacts": artifacts,
            "capacity_report": asdict(current_capacity),
        }
    except Exception as exc:
        try:
            with get_session() as session:
                finish_campaign(
                    session,
                    campaign_id,
                    succeeded=False,
                    error_type=type(exc).__name__,
                )
        except Exception:
            pass
        raise


def _run_campaign_inputs(
    *,
    campaign_id: str,
    manifest: Mapping[str, Any],
    start: datetime,
    end: datetime,
    download_dir: Path,
    raw_dir: Path,
    project_root: Path,
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    from scripts.rdp_deep_backfill_api import deep_backfill_one
    from scripts.rdp_deep_backfill_funding import deep_backfill_funding

    symbol = str(manifest["symbol"])
    bundles: dict[str, Any] = {"trade": [], "l2": []}
    for timeframe in ("15m", "1H"):
        checkpoint_key = f"candle:{timeframe}"
        existing = _checkpoint_bundle_id(checkpoint, checkpoint_key)
        if existing is not None:
            bundles[checkpoint_key] = existing
            continue
        stats = deep_backfill_one(
            symbol=symbol,
            timeframe=timeframe,
            target_start=start,
            rate_limit_sleep=0.15,
            dry_run=False,
            build_gold=False,
            merge_every_n_pages=50,
            refresh_existing=True,
            refresh_end=end,
            raw_archive_dir=raw_dir,
        )
        bundle = _eligible_bundle(stats, f"candle:{timeframe}")
        bundles[checkpoint_key] = bundle
        _checkpoint(campaign_id, checkpoint_key, {"status": "succeeded", "bundle_id": bundle})

    funding_bundle = _checkpoint_bundle_id(checkpoint, "funding")
    if funding_bundle is None:
        funding_stats = deep_backfill_funding(
            symbol=symbol,
            target_start=start,
            rate_limit_sleep=0.15,
            dry_run=False,
            merge_every_n_pages=30,
            raw_archive_dir=raw_dir,
            refresh_existing=True,
            refresh_end=end,
        )
        funding_bundle = _eligible_bundle(funding_stats, "funding")
        _checkpoint(campaign_id, "funding", {"status": "succeeded", "bundle_id": funding_bundle})
    bundles["funding"] = funding_bundle

    from scripts.rdp_import_official_history import main as import_official

    for timeframe in ("15m", "1H"):
        checkpoint_key = f"mark:{timeframe}"
        existing = _checkpoint_bundle_id(checkpoint, checkpoint_key)
        if existing is not None:
            bundles[checkpoint_key] = existing
            continue
        mark_result = _invoke_import(
            import_official,
            [
                "mark-rest",
                "--symbol",
                symbol,
                "--timeframe",
                timeframe,
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--raw-archive-dir",
                str(raw_dir),
                "--apply",
                "--confirm",
            ],
        )
        bundle = _eligible_bundle(mark_result, f"mark:{timeframe}")
        bundles[checkpoint_key] = bundle
        _checkpoint(campaign_id, checkpoint_key, {"status": "succeeded", "bundle_id": bundle})

    with httpx.Client(headers={"User-Agent": "AATS-RDP-Historical-Recovery/1.0"}) as client:
        for index, partition in enumerate(manifest["partitions"]):
            partition_start = datetime.fromisoformat(str(partition["coverage_start"]))
            partition_end = datetime.fromisoformat(str(partition["coverage_end"]))
            trade_checkpoint_key = f"trade:{index}"
            l2_checkpoint_key = f"l2:{index}"
            existing_trade = _checkpoint_bundle_id(checkpoint, trade_checkpoint_key)
            existing_l2 = _checkpoint_bundle_id(checkpoint, l2_checkpoint_key)
            if existing_trade is not None:
                _rebuild_bundle(existing_trade, project_root)
            if existing_l2 is not None:
                _rebuild_bundle(existing_l2, project_root)
            if existing_trade is not None and existing_l2 is not None:
                bundles["trade"].append(existing_trade)
                bundles["l2"].append(existing_l2)
                continue

            trade_paths = []
            for item in partition["trade_files"]:
                downloaded = download_verified_file(
                    client,
                    url=str(item["url"]),
                    target=download_dir / str(item["filename"]),
                )
                trade_paths.append(downloaded.path)
                _checkpoint(
                    campaign_id,
                    f"download:{downloaded.filename}",
                    {
                        "status": "succeeded",
                        "sha256": downloaded.sha256,
                        "size_bytes": downloaded.size_bytes,
                    },
                )
            l2_item = partition["l2_file"]
            l2_download = download_verified_file(
                client,
                url=str(l2_item["url"]),
                target=download_dir / str(l2_item["filename"]),
            )
            _checkpoint(
                campaign_id,
                f"download:{l2_download.filename}",
                {
                    "status": "succeeded",
                    "sha256": l2_download.sha256,
                    "size_bytes": l2_download.size_bytes,
                },
            )

            trade_bundle = existing_trade
            if trade_bundle is None:
                trade_args = [
                    "trade-file",
                    "--symbol",
                    symbol,
                    "--start",
                    partition_start.isoformat(),
                    "--end",
                    partition_end.isoformat(),
                    "--input",
                    trade_paths[0],
                    "--additional-input",
                    trade_paths[1],
                    "--raw-archive-dir",
                    str(raw_dir),
                    "--apply",
                    "--confirm",
                ]
                trade_result = _invoke_import(import_official, trade_args)
                trade_bundle = _eligible_bundle(trade_result, trade_checkpoint_key)
                _rebuild_bundle(trade_bundle, project_root)
                _checkpoint(campaign_id, trade_checkpoint_key, {"status": "succeeded", "bundle_id": trade_bundle})
            bundles["trade"].append(trade_bundle)

            l2_bundle = existing_l2
            if l2_bundle is None:
                l2_result = _invoke_import(
                    import_official,
                    [
                        "l2-file",
                        "--symbol",
                        symbol,
                        "--start",
                        partition_start.isoformat(),
                        "--end",
                        partition_end.isoformat(),
                        "--input",
                        l2_download.path,
                        "--raw-archive-dir",
                        str(raw_dir),
                        "--apply",
                        "--confirm",
                    ],
                )
                l2_bundle = _eligible_bundle(l2_result, l2_checkpoint_key)
                _rebuild_bundle(l2_bundle, project_root)
                _checkpoint(campaign_id, l2_checkpoint_key, {"status": "succeeded", "bundle_id": l2_bundle})
            bundles["l2"].append(l2_bundle)
    return bundles


def _invoke_import(main, argv: list[str]) -> dict[str, Any]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(argv)
    if code != 0:
        raise RuntimeError(f"historical_campaign_import_failed:{code}")
    try:
        result = json.loads(stdout.getvalue())
    except json.JSONDecodeError as exc:
        raise RuntimeError("historical_campaign_import_output_invalid") from exc
    if not isinstance(result, dict):
        raise RuntimeError("historical_campaign_import_output_invalid")
    return result


def _eligible_bundle(result: Mapping[str, Any], step: str) -> str:
    bundle = result.get("bundle")
    if not isinstance(bundle, Mapping) or bundle.get("eligible") is not True:
        raise RuntimeError(f"historical_campaign_bundle_ineligible:{step}")
    bundle_id = str(bundle.get("bundle_id", ""))
    if not bundle_id:
        raise RuntimeError(f"historical_campaign_bundle_missing:{step}")
    return bundle_id


def _rebuild_bundle(bundle_id: str, project_root: Path) -> None:
    with get_session() as session:
        plan = plan_historical_rebuild(
            session,
            bundle_id=bundle_id,
            git_commit=git_commit(str(project_root)),
        )
        status = start_historical_rebuild(session, plan)
    if status == "already_succeeded":
        return
    try:
        with get_session() as session:
            execute_historical_rebuild(session, plan)
    except Exception as exc:
        try:
            with get_session() as session:
                fail_historical_rebuild(
                    session,
                    plan.operation_key,
                    type(exc).__name__,
                )
        except Exception as state_exc:
            raise ExceptionGroup(
                "historical Silver rebuild and terminal-state update failed",
                [exc, state_exc],
            ) from exc
        raise


def _checkpoint_bundle_id(
    checkpoint: Mapping[str, Any],
    key: str,
) -> str | None:
    value = checkpoint.get(key)
    if not isinstance(value, Mapping) or value.get("status") != "succeeded":
        return None
    bundle_id = str(value.get("bundle_id") or "")
    if not bundle_id:
        return None
    with get_session() as session:
        valid = session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM meta.dataset_bundles "
                "WHERE bundle_id = CAST(:bundle_id AS UUID) "
                "AND status = 'ELIGIBLE' "
                "AND eligibility_mode = 'historical_research')"
            ),
            {"bundle_id": bundle_id},
        ).scalar_one()
    return bundle_id if bool(valid) else None


def _checkpoint(campaign_id: str, key: str, payload: Mapping[str, Any]) -> None:
    with get_session() as session:
        update_campaign_checkpoint(
            session,
            campaign_id,
            checkpoint_key=key,
            payload=payload,
        )


__all__ = ["run_historical_campaign"]
