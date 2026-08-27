"""Unit tests for Route A phase 0 evidence bundle scaffold.

覆盖 ``aats.data_platform.replay.backtest.route_a_evidence_scaffold`` helper
以及 ``aats.cli route-a-evidence-scaffold`` 子命令，纯本地 FS, 无 DB / 网络。

SoW §11 要求覆盖:
    1. 正常 scaffold 创建 + manifest/copy/proposal.md 内容
    2. scorecard 缺关键键时报错
    3. observation-window 缺关键键时报错
    4. 输出目录已存在时报错
"""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from aats.cli import main
from aats.data_platform.replay.backtest.evidence_scorecard import (
    SCORECARD_ARTIFACT_KIND,
    SCORECARD_SCHEMA_VERSION,
)
from aats.data_platform.replay.backtest.route_a_evidence_scaffold import (
    BUNDLE_MANIFEST_ARTIFACT_KIND,
    BUNDLE_MANIFEST_SCHEMA_VERSION,
    OBSERVATION_WINDOW_ARTIFACT_KIND,
    OBSERVATION_WINDOW_REQUIRED_KEYS,
    OBSERVATION_WINDOW_SCHEMA_VERSION,
    SCORECARD_REQUIRED_KEYS,
    ScaffoldError,
    ScaffoldInputs,
    create_scaffold,
)
from aats.domain.instrument_contract import INSTRUMENT_ARITHMETIC_POLICY_ID
from aats.data_platform.replay.core.replay_context import ReplayParameterOverrides
from tests.unit.replay_contract_fixtures import SPOT_CONTRACT


def _scorecard_contract_meta() -> dict:
    contract = SPOT_CONTRACT
    return {
        "symbol": contract.symbol,
        "execution_model_version": "next_bar_event_v2",
        "fill_model_version": "ohlcv_participation_cap_contract_v3",
        "spot_buy_fee_asset": "quote",
        "instrument_arithmetic_policy_id": INSTRUMENT_ARITHMETIC_POLICY_ID,
        "contract_lineage_status": "calculation_contract_only_unverified",
        "instrument_contract_fingerprint": contract.fingerprint,
        "settlement_currency": contract.settle_currency,
        "timeframe": "15m",
        "dataset_version": "aats-research-test",
        "family": "independent",
        "order_type": "ioc",
        "market_data_granularity": "ohlcv",
        "execution_realism_limitations": [
            "no_l2_depth",
            "no_spread_or_queue_position",
            "no_market_impact_calibration",
            "fixed_slippage_bps",
            "volume_participation_proxy_only",
        ],
        "start_ts": "2026-04-01T00:00:00+00:00",
        "end_ts": "2026-04-02T01:00:00+00:00",
        "generated_at": "2026-04-25T12:00:00+00:00",
        "total_bars": 100,
        "total_fills": 10,
        "total_decisions": 100,
        "resolved_parameters": asdict(
            replace(
                ReplayParameterOverrides.for_family("independent"),
                strategy_short_bias_enabled=False,
            )
        ),
        "adapter_identity": (
            "aats.data_platform.replay.adapters.independent_adapter."
            "IndependentReplayAdapter"
        ),
        "adapter_algorithm_version": "independent-replay/v2",
        "fill_attribution_status": "explicit_v1",
        "cadence_gap_count": 0,
        "risk_metric_policy_id": "calendar-365.25-bar-pnl-increment/v1",
        "instrument_contract": {
            "symbol": contract.symbol,
            "instrument_type": contract.instrument_type,
            "contract_type": contract.contract_type,
            "base_currency": contract.base_currency,
            "quote_currency": contract.quote_currency,
            "settle_currency": contract.settle_currency,
            "contract_value": str(contract.contract_value),
            "contract_multiplier": str(contract.contract_multiplier),
            "contract_value_currency": contract.contract_value_currency,
            "lot_size": str(contract.lot_size),
            "min_size": str(contract.min_size),
            "tick_size": str(contract.tick_size),
        },
    }


def _valid_scorecard_payload() -> dict:
    def _slice(start: str, end: str, *, fills: int, sample_n: int) -> dict:
        return {
            "start": start,
            "end": end,
            "ir": 0.0,
            "ir_annualized": 0.0,
            "sharpe_ratio": 0.0,
            "hit_rate": 0.5,
            "fills": fills,
            "sample_n": sample_n,
            "max_drawdown_bps": 10.0,
        }

    def _cost_bucket() -> dict:
        return {
            "realized_edge_bps": 8.0,
            "fee_bps": 5.0,
            "slip_bps": 1.0,
            "exec_buffer_bps": 0.0,
            "net_edge_bps": 2.0,
        }

    sensitivity_bucket = {
        "net_edge_fee_up_20pct_bps": 1.0,
        "net_edge_slip_plus_0_5bps_bps": 1.5,
    }
    return {
        "artifact_kind": SCORECARD_ARTIFACT_KIND,
        "artifact_schema_version": SCORECARD_SCHEMA_VERSION,
        "meta": _scorecard_contract_meta(),
        "oos": {
            "split_method": "explicit",
            "split_ts": "2026-04-01T12:45:00+00:00",
            "train": _slice(
                "2026-04-01T00:15:00+00:00",
                "2026-04-01T12:30:00+00:00",
                fills=4,
                sample_n=49,
            ),
            "test": _slice(
                "2026-04-01T12:45:00+00:00",
                "2026-04-02T01:00:00+00:00",
                fills=6,
                sample_n=50,
            ),
        },
        "cross_window": [
            _slice(
                "2026-04-01T12:45:00+00:00",
                "2026-04-01T16:30:00+00:00",
                fills=2,
                sample_n=16,
            ),
            _slice(
                "2026-04-01T16:45:00+00:00",
                "2026-04-01T20:45:00+00:00",
                fills=2,
                sample_n=17,
            ),
            _slice(
                "2026-04-01T21:00:00+00:00",
                "2026-04-02T01:00:00+00:00",
                fills=2,
                sample_n=17,
            ),
        ],
        "cost_adjusted": {
            **_cost_bucket(),
            "train": _cost_bucket(),
            "test": _cost_bucket(),
            "sensitivity": {
                "overall": dict(sensitivity_bucket),
                "train": dict(sensitivity_bucket),
                "test": dict(sensitivity_bucket),
            },
        },
        "regime_slice": {
            "vol": {
                "low": {"ir": 0.1, "fills": 5, "sample_n": 50},
                "high": {"ir": 0.2, "fills": 5, "sample_n": 49},
            }
        },
    }


def _valid_observation_payload() -> dict:
    return {
        "artifact_kind": OBSERVATION_WINDOW_ARTIFACT_KIND,
        "artifact_schema_version": OBSERVATION_WINDOW_SCHEMA_VERSION,
        "generated_at": "2026-04-24T14:02:11Z",
        "window_start": "2026-04-22T00:00:00Z",
        "window_target": "2026-04-29T00:00:00Z",
        "overall": "pass",
        "exit_code": 0,
        "warn_count": 0,
        "fail_count": 0,
        "checks": [
            {
                "section": "[1/7] Container health",
                "status": "pass",
                "message": "ok",
            }
        ],
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class TestCreateScaffoldHappyPath(unittest.TestCase):
    def test_creates_bundle_files_and_manifest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "scorecard.json"
            observation_src = root / "observation.json"
            scorecard_payload = _valid_scorecard_payload()
            observation_payload = _valid_observation_payload()
            _write_json(scorecard_src, scorecard_payload)
            _write_json(observation_src, observation_payload)

            generated_at = datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)
            inputs = ScaffoldInputs(
                proposal_id="route-a-phase0-ofi-5s-20260430",
                feature="OFI",
                horizon="5s",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                proposer="alice",
                output_root=root / "bundle_root",
            )
            result = create_scaffold(inputs, generated_at=generated_at)

            # proposal_dir path
            self.assertEqual(
                result.proposal_dir,
                root / "bundle_root" / "route-a-phase0-ofi-5s-20260430",
            )
            self.assertTrue(result.proposal_dir.is_dir())

            # all four artifacts exist
            self.assertTrue(result.manifest_path.is_file())
            self.assertTrue(result.scorecard_path.is_file())
            self.assertTrue(result.observation_window_summary_path.is_file())
            self.assertTrue(result.proposal_md_path.is_file())

            # artifact names match SoW
            self.assertEqual(result.manifest_path.name, "manifest.json")
            self.assertEqual(result.scorecard_path.name, "scorecard.json")
            self.assertEqual(
                result.observation_window_summary_path.name,
                "observation_window_summary.json",
            )
            self.assertEqual(result.proposal_md_path.name, "proposal.md")

            # copies are byte-identical to sources
            self.assertEqual(
                json.loads(result.scorecard_path.read_text(encoding="utf-8")),
                scorecard_payload,
            )
            self.assertEqual(
                json.loads(
                    result.observation_window_summary_path.read_text(
                        encoding="utf-8"
                    )
                ),
                observation_payload,
            )

            # manifest metadata
            manifest = json.loads(
                result.manifest_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["artifact_kind"],
                BUNDLE_MANIFEST_ARTIFACT_KIND,
            )
            self.assertEqual(
                manifest["artifact_schema_version"],
                BUNDLE_MANIFEST_SCHEMA_VERSION,
            )
            self.assertIs(manifest["artifact_set_complete"], False)
            self.assertEqual(
                manifest["observation_completion_status"],
                "incomplete_single_snapshot",
            )
            self.assertNotIn("complete", manifest)
            self.assertEqual(
                manifest["proposal_id"], "route-a-phase0-ofi-5s-20260430"
            )
            self.assertEqual(manifest["feature"], "OFI")
            self.assertEqual(manifest["horizon"], "5s")
            self.assertEqual(manifest["proposer"], "alice")
            self.assertEqual(
                manifest["generated_at"], "2026-04-30T12:00:00+00:00"
            )
            self.assertEqual(
                manifest["source_paths"]["scorecard_json"],
                str(scorecard_src),
            )
            self.assertEqual(
                manifest["source_paths"]["observation_window_json"],
                str(observation_src),
            )

            expected_scorecard_sha = hashlib.sha256(
                scorecard_src.read_bytes()
            ).hexdigest()
            expected_observation_sha = hashlib.sha256(
                observation_src.read_bytes()
            ).hexdigest()
            self.assertEqual(
                manifest["source_sha256"]["scorecard_json"],
                expected_scorecard_sha,
            )
            self.assertEqual(
                manifest["source_sha256"]["observation_window_json"],
                expected_observation_sha,
            )

            # proposal.md contains prefilled metadata + references
            md_text = result.proposal_md_path.read_text(encoding="utf-8")
            self.assertIn("route-a-phase0-ofi-5s-20260430", md_text)
            self.assertIn("OFI", md_text)
            self.assertIn("5s", md_text)
            self.assertIn("alice", md_text)
            self.assertIn("scorecard.json", md_text)
            self.assertIn("observation_window_summary.json", md_text)
            self.assertIn("manifest.json", md_text)

            # manifest has no verdict / go-no-go / archive fields (SoW boundary)
            for forbidden in ("verdict", "go", "archive", "status"):
                self.assertNotIn(forbidden, manifest.keys())

    def test_missing_proposer_renders_tbd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            _write_json(scorecard_src, _valid_scorecard_payload())
            _write_json(observation_src, _valid_observation_payload())

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="TFI",
                horizon="15min",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                output_root=root / "bundle",
            )
            result = create_scaffold(inputs)

            manifest = json.loads(
                result.manifest_path.read_text(encoding="utf-8")
            )
            self.assertIsNone(manifest["proposer"])
            md_text = result.proposal_md_path.read_text(encoding="utf-8")
            self.assertIn("<TBD>", md_text)

    def test_time_midpoint_and_directional_adapter_contract_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            payload = _valid_scorecard_payload()
            payload["meta"].update(
                {
                    "family": "directional",
                    "resolved_parameters": asdict(
                        replace(
                            ReplayParameterOverrides.for_family("directional"),
                            strategy_short_bias_enabled=False,
                        )
                    ),
                    "adapter_identity": (
                        "aats.data_platform.replay.adapters.directional_adapter."
                        "DirectionalReplayAdapter"
                    ),
                    "adapter_algorithm_version": "directional-replay/v2",
                }
            )
            payload["oos"].update(
                {
                    "split_method": "time_midpoint",
                    "split_ts": "2026-04-01T12:37:30+00:00",
                }
            )
            _write_json(scorecard_src, payload)
            _write_json(observation_src, _valid_observation_payload())

            result = create_scaffold(
                ScaffoldInputs(
                    proposal_id="p1",
                    feature="TFI",
                    horizon="15min",
                    scorecard_json=scorecard_src,
                    observation_window_json=observation_src,
                    output_root=root / "bundle",
                )
            )

            self.assertTrue(result.manifest_path.is_file())

    def test_contiguous_curve_may_cover_only_part_of_requested_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            payload = _valid_scorecard_payload()
            payload["meta"].update(
                {
                    "start_ts": "2026-03-31T23:30:00+00:00",
                    "end_ts": "2026-04-02T02:00:00+00:00",
                }
            )
            _write_json(scorecard_src, payload)
            _write_json(observation_src, _valid_observation_payload())

            result = create_scaffold(
                ScaffoldInputs(
                    proposal_id="p1",
                    feature="OFI",
                    horizon="5s",
                    scorecard_json=scorecard_src,
                    observation_window_json=observation_src,
                    output_root=root / "bundle",
                )
            )

            self.assertTrue(result.manifest_path.is_file())


class TestCreateScaffoldBomTolerance(unittest.TestCase):
    """PowerShell's default UTF-8 output prepends a BOM; scaffold must accept it."""

    def _write_json_with_bom(self, path: Path, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))

    def test_bom_prefixed_scorecard_and_observation_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "scorecard.json"
            observation_src = root / "observation.json"
            self._write_json_with_bom(scorecard_src, _valid_scorecard_payload())
            self._write_json_with_bom(
                observation_src, _valid_observation_payload()
            )
            # sanity: files really start with a BOM
            self.assertTrue(scorecard_src.read_bytes().startswith(b"\xef\xbb\xbf"))
            self.assertTrue(
                observation_src.read_bytes().startswith(b"\xef\xbb\xbf")
            )

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="OFI",
                horizon="5s",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                output_root=root / "bundle",
            )
            result = create_scaffold(inputs)
            self.assertTrue(result.manifest_path.is_file())


class TestCreateScaffoldErrors(unittest.TestCase):
    def test_standard_json_exponent_overflow_is_rejected_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            raw_scorecard = json.dumps(
                _valid_scorecard_payload(),
                ensure_ascii=False,
            ).replace('"total_bars": 100', '"total_bars": 1e999')
            scorecard_src.write_text(raw_scorecard, encoding="utf-8")
            _write_json(observation_src, _valid_observation_payload())

            with self.assertRaisesRegex(ScaffoldError, "JSON 含非有限数值"):
                create_scaffold(
                    ScaffoldInputs(
                        proposal_id="p1",
                        feature="OFI",
                        horizon="5s",
                        scorecard_json=scorecard_src,
                        observation_window_json=observation_src,
                        output_root=root / "bundle",
                    )
                )

            self.assertFalse((root / "bundle").exists())

    def test_scorecard_missing_key_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            bad_scorecard = _valid_scorecard_payload()
            # Drop one required top-level key.
            dropped_key = next(iter(SCORECARD_REQUIRED_KEYS))
            bad_scorecard.pop(dropped_key)
            _write_json(scorecard_src, bad_scorecard)
            _write_json(observation_src, _valid_observation_payload())

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="OFI",
                horizon="5s",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                output_root=root / "bundle",
            )
            with self.assertRaises(ScaffoldError) as ctx:
                create_scaffold(inputs)
            self.assertIn("scorecard", str(ctx.exception))
            self.assertIn(dropped_key, str(ctx.exception))
            # nothing should have been created on error
            self.assertFalse((root / "bundle").exists())

    def test_observation_window_missing_key_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            bad_observation = _valid_observation_payload()
            dropped_key = next(iter(OBSERVATION_WINDOW_REQUIRED_KEYS))
            bad_observation.pop(dropped_key)
            _write_json(scorecard_src, _valid_scorecard_payload())
            _write_json(observation_src, bad_observation)

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="OFI",
                horizon="5s",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                output_root=root / "bundle",
            )
            with self.assertRaises(ScaffoldError) as ctx:
                create_scaffold(inputs)
            self.assertIn("observation-window", str(ctx.exception))
            self.assertIn(dropped_key, str(ctx.exception))
            self.assertFalse((root / "bundle").exists())

    def test_missing_input_file_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            observation_src = root / "o.json"
            _write_json(observation_src, _valid_observation_payload())

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="OFI",
                horizon="5s",
                scorecard_json=root / "does_not_exist.json",
                observation_window_json=observation_src,
                output_root=root / "bundle",
            )
            with self.assertRaises(ScaffoldError) as ctx:
                create_scaffold(inputs)
            self.assertIn("scorecard", str(ctx.exception))
            self.assertIn("does_not_exist.json", str(ctx.exception))

    def test_existing_output_dir_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            _write_json(scorecard_src, _valid_scorecard_payload())
            _write_json(observation_src, _valid_observation_payload())

            output_root = root / "bundle"
            existing = output_root / "p1"
            existing.mkdir(parents=True)
            (existing / "marker.txt").write_text("pre-existing", encoding="utf-8")

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="OFI",
                horizon="5s",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                output_root=output_root,
            )
            with self.assertRaises(ScaffoldError) as ctx:
                create_scaffold(inputs)
            self.assertIn("已存在", str(ctx.exception))
            # pre-existing content untouched, no new artifacts written
            self.assertEqual(
                (existing / "marker.txt").read_text(encoding="utf-8"),
                "pre-existing",
            )
            self.assertFalse((existing / "manifest.json").exists())

    def test_unknown_or_decision_scorecard_fields_fail_before_output(self) -> None:
        payloads: list[dict] = []
        unknown_top = _valid_scorecard_payload()
        unknown_top["production_ready"] = True
        payloads.append(unknown_top)
        unknown_meta = _valid_scorecard_payload()
        unknown_meta["meta"]["unexpected"] = "value"
        payloads.append(unknown_meta)
        nested_decision = _valid_scorecard_payload()
        nested_decision["oos"]["production_ready"] = True
        payloads.append(nested_decision)
        composite_decision = _valid_scorecard_payload()
        composite_decision["oos"]["is_approved"] = True
        payloads.append(composite_decision)
        wrong_kind = _valid_scorecard_payload()
        wrong_kind["artifact_kind"] = "legacy"
        payloads.append(wrong_kind)
        wrong_version = _valid_scorecard_payload()
        wrong_version["artifact_schema_version"] = "legacy"
        payloads.append(wrong_version)
        wrong_fingerprint = _valid_scorecard_payload()
        wrong_fingerprint["meta"]["instrument_contract_fingerprint"] = "0" * 64
        payloads.append(wrong_fingerprint)
        malformed_metric = _valid_scorecard_payload()
        malformed_metric["oos"]["train"]["ir_annualized"] = "approved for live"
        payloads.append(malformed_metric)
        missing_cross_windows = _valid_scorecard_payload()
        missing_cross_windows["cross_window"] = []
        payloads.append(missing_cross_windows)
        ambiguous_attribution = _valid_scorecard_payload()
        ambiguous_attribution["meta"]["fill_attribution_status"] = (
            "legacy_ambiguous"
        )
        payloads.append(ambiguous_attribution)

        for index, payload in enumerate(payloads):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scorecard_src = root / "s.json"
                observation_src = root / "o.json"
                _write_json(scorecard_src, payload)
                _write_json(observation_src, _valid_observation_payload())
                output_root = root / "bundle"

                with self.assertRaises(ScaffoldError):
                    create_scaffold(
                        ScaffoldInputs(
                            proposal_id="p1",
                            feature="OFI",
                            horizon="5s",
                            scorecard_json=scorecard_src,
                            observation_window_json=observation_src,
                            output_root=output_root,
                        )
                    )
                self.assertFalse(output_root.exists())

    def test_scorecard_meta_semantics_fail_closed_before_output(self) -> None:
        payloads: list[tuple[str, dict]] = []

        unsupported_family = _valid_scorecard_payload()
        unsupported_family["meta"]["family"] = "custom"
        payloads.append(("unsupported_family", unsupported_family))

        wrong_adapter = _valid_scorecard_payload()
        wrong_adapter["meta"]["adapter_identity"] = (
            "aats.data_platform.replay.adapters.directional_adapter."
            "DirectionalReplayAdapter"
        )
        payloads.append(("wrong_adapter", wrong_adapter))

        wrong_adapter_version = _valid_scorecard_payload()
        wrong_adapter_version["meta"]["adapter_algorithm_version"] = (
            "independent-replay/v1"
        )
        payloads.append(("wrong_adapter_version", wrong_adapter_version))

        wrong_order_type = _valid_scorecard_payload()
        wrong_order_type["meta"]["order_type"] = "market"
        payloads.append(("wrong_order_type", wrong_order_type))

        malformed_timeframe = _valid_scorecard_payload()
        malformed_timeframe["meta"]["timeframe"] = "15min"
        payloads.append(("malformed_timeframe", malformed_timeframe))

        empty_dataset_version = _valid_scorecard_payload()
        empty_dataset_version["meta"]["dataset_version"] = "  "
        payloads.append(("empty_dataset_version", empty_dataset_version))

        wrong_granularity = _valid_scorecard_payload()
        wrong_granularity["meta"]["market_data_granularity"] = "l2"
        payloads.append(("wrong_granularity", wrong_granularity))

        hidden_realism_change = _valid_scorecard_payload()
        hidden_realism_change["meta"]["execution_realism_limitations"] = list(
            reversed(
                hidden_realism_change["meta"]["execution_realism_limitations"]
            )
        )
        payloads.append(("hidden_realism_change", hidden_realism_change))

        generated_before_window_end = _valid_scorecard_payload()
        generated_before_window_end["meta"]["generated_at"] = (
            "2026-04-01T23:59:59+00:00"
        )
        payloads.append(
            ("generated_before_window_end", generated_before_window_end)
        )

        too_many_fills = _valid_scorecard_payload()
        too_many_fills["meta"]["total_fills"] = 100
        payloads.append(("too_many_fills", too_many_fills))

        for label, payload in payloads:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scorecard_src = root / "s.json"
                observation_src = root / "o.json"
                _write_json(scorecard_src, payload)
                _write_json(observation_src, _valid_observation_payload())
                output_root = root / "bundle"

                with self.assertRaises(ScaffoldError):
                    create_scaffold(
                        ScaffoldInputs(
                            proposal_id="p1",
                            feature="OFI",
                            horizon="5s",
                            scorecard_json=scorecard_src,
                            observation_window_json=observation_src,
                            output_root=output_root,
                        )
                    )
                self.assertFalse(output_root.exists())

    def test_scorecard_window_and_sample_semantics_fail_closed(self) -> None:
        payloads: list[tuple[str, dict]] = []

        wrong_annualization = _valid_scorecard_payload()
        wrong_annualization["oos"]["train"]["ir"] = 0.1
        payloads.append(("wrong_annualization", wrong_annualization))

        wrong_sample_n = _valid_scorecard_payload()
        wrong_sample_n["oos"]["train"]["sample_n"] = 48
        payloads.append(("wrong_sample_n", wrong_sample_n))

        split_outside_partition = _valid_scorecard_payload()
        split_outside_partition["oos"]["split_ts"] = (
            "2026-04-01T12:46:00+00:00"
        )
        payloads.append(("split_outside_partition", split_outside_partition))

        cross_gap = _valid_scorecard_payload()
        cross_gap["cross_window"][1].update(
            {
                "start": "2026-04-01T17:00:00+00:00",
                "sample_n": 15,
            }
        )
        payloads.append(("cross_gap", cross_gap))

        nonzero_ir_with_one_return = _valid_scorecard_payload()
        annualization = math.sqrt(365.25 * 24 * 60 / 15)
        nonzero_ir_with_one_return["cross_window"][0].update(
            {
                "end": "2026-04-01T13:00:00+00:00",
                "ir": 0.1,
                "ir_annualized": 0.1 * annualization,
                "sharpe_ratio": 0.1 * annualization,
                "fills": 0,
                "sample_n": 1,
            }
        )
        payloads.append(("nonzero_ir_with_one_return", nonzero_ir_with_one_return))

        empty_cross_slice = _valid_scorecard_payload()
        empty_cross_slice["cross_window"][0] = {
            "start": None,
            "end": None,
            "ir": 0.0,
            "ir_annualized": 0.0,
            "sharpe_ratio": 0.0,
            "hit_rate": 0.0,
            "fills": 0,
            "sample_n": 0,
            "max_drawdown_bps": 0.0,
        }
        payloads.append(("empty_cross_slice", empty_cross_slice))

        wrong_regime_sample_count = _valid_scorecard_payload()
        wrong_regime_sample_count["regime_slice"]["vol"]["low"][
            "sample_n"
        ] = 49
        payloads.append(("wrong_regime_sample_count", wrong_regime_sample_count))

        for label, payload in payloads:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scorecard_src = root / "s.json"
                observation_src = root / "o.json"
                _write_json(scorecard_src, payload)
                _write_json(observation_src, _valid_observation_payload())
                output_root = root / "bundle"

                with self.assertRaises(ScaffoldError):
                    create_scaffold(
                        ScaffoldInputs(
                            proposal_id="p1",
                            feature="OFI",
                            horizon="5s",
                            scorecard_json=scorecard_src,
                            observation_window_json=observation_src,
                            output_root=output_root,
                        )
                    )
                self.assertFalse(output_root.exists())

    def test_scorecard_cost_and_sensitivity_semantics_fail_closed(self) -> None:
        payloads: list[tuple[str, dict]] = []

        wrong_net_edge = _valid_scorecard_payload()
        wrong_net_edge["cost_adjusted"]["test"]["net_edge_bps"] = 2.1
        payloads.append(("wrong_net_edge", wrong_net_edge))

        wrong_weighted_overall = _valid_scorecard_payload()
        wrong_weighted_overall["cost_adjusted"].update(
            {"realized_edge_bps": 8.5, "net_edge_bps": 2.5}
        )
        wrong_weighted_overall["cost_adjusted"]["sensitivity"]["overall"] = {
            "net_edge_fee_up_20pct_bps": 1.5,
            "net_edge_slip_plus_0_5bps_bps": 2.0,
        }
        payloads.append(("wrong_weighted_overall", wrong_weighted_overall))

        wrong_sensitivity = _valid_scorecard_payload()
        wrong_sensitivity["cost_adjusted"]["sensitivity"]["test"][
            "net_edge_fee_up_20pct_bps"
        ] = 99.0
        payloads.append(("wrong_sensitivity", wrong_sensitivity))

        nonzero_empty_cost_bucket = _valid_scorecard_payload()
        nonzero_empty_cost_bucket["oos"]["train"]["fills"] = 0
        nonzero_empty_cost_bucket["oos"]["test"]["fills"] = 10
        payloads.append(("nonzero_empty_cost_bucket", nonzero_empty_cost_bucket))

        for label, payload in payloads:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scorecard_src = root / "s.json"
                observation_src = root / "o.json"
                _write_json(scorecard_src, payload)
                _write_json(observation_src, _valid_observation_payload())
                output_root = root / "bundle"

                with self.assertRaises(ScaffoldError):
                    create_scaffold(
                        ScaffoldInputs(
                            proposal_id="p1",
                            feature="OFI",
                            horizon="5s",
                            scorecard_json=scorecard_src,
                            observation_window_json=observation_src,
                            output_root=output_root,
                        )
                    )
                self.assertFalse(output_root.exists())

    def test_invalid_observation_contract_fails_before_output(self) -> None:
        payloads: list[dict] = []
        wrong_schema = _valid_observation_payload()
        wrong_schema["artifact_schema_version"] = "legacy"
        payloads.append(wrong_schema)
        wrong_kind = _valid_observation_payload()
        wrong_kind["artifact_kind"] = "legacy"
        payloads.append(wrong_kind)
        inconsistent = _valid_observation_payload()
        inconsistent["warn_count"] = 1
        inconsistent["checks"].append(
            {"section": "x", "status": "warn", "message": "warn"}
        )
        payloads.append(inconsistent)
        non_finite = _valid_observation_payload()
        non_finite["warn_count"] = float("nan")
        payloads.append(non_finite)
        before_window = _valid_observation_payload()
        before_window["generated_at"] = "2026-04-21T23:59:59Z"
        payloads.append(before_window)

        for index, payload in enumerate(payloads):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                scorecard_src = root / "s.json"
                observation_src = root / "o.json"
                _write_json(scorecard_src, _valid_scorecard_payload())
                _write_json(observation_src, payload)
                output_root = root / "bundle"

                with self.assertRaises(ScaffoldError):
                    create_scaffold(
                        ScaffoldInputs(
                            proposal_id="p1",
                            feature="OFI",
                            horizon="5s",
                            scorecard_json=scorecard_src,
                            observation_window_json=observation_src,
                            output_root=output_root,
                        )
                    )
                self.assertFalse(output_root.exists())

    def test_proposal_id_cannot_escape_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            _write_json(scorecard_src, _valid_scorecard_payload())
            _write_json(observation_src, _valid_observation_payload())
            output_root = root / "bundle"
            unsafe_ids = (
                "../escape",
                "sub/dir",
                "sub\\dir",
                str((root / "absolute-escape").resolve()),
            )

            for proposal_id in unsafe_ids:
                with self.subTest(proposal_id=proposal_id):
                    with self.assertRaisesRegex(
                        ScaffoldError,
                        "proposal_id",
                    ):
                        create_scaffold(
                            ScaffoldInputs(
                                proposal_id=proposal_id,
                                feature="OFI",
                                horizon="5s",
                                scorecard_json=scorecard_src,
                                observation_window_json=observation_src,
                                output_root=output_root,
                            )
                        )
            self.assertFalse(output_root.exists())
            self.assertFalse((root / "escape").exists())
            self.assertFalse((root / "absolute-escape").exists())


def _rich_scorecard_payload() -> dict:
    """Scorecard 以真实形态提供 meta/oos/cross_window/cost_adjusted 字段,
    用于验证 proposal.md 的预填段落。
    """
    payload = _valid_scorecard_payload()
    payload["meta"]["dataset_version"] = "aats-research-20260420"
    annualization = math.sqrt(365.25 * 24 * 60 / 15)
    payload["oos"]["train"].update(
        {
            "ir": 1.23 / annualization,
            "ir_annualized": 1.23,
            "sharpe_ratio": 1.23,
            "hit_rate": 0.54,
        }
    )
    payload["oos"]["test"].update(
        {
            "ir": 0.91 / annualization,
            "ir_annualized": 0.91,
            "sharpe_ratio": 0.91,
            "hit_rate": 0.51,
        }
    )
    for window, annualized in zip(
        payload["cross_window"],
        (0.88, 0.95, 0.90),
        strict=True,
    ):
        window["ir"] = annualized / annualization
        window["ir_annualized"] = annualized
        window["sharpe_ratio"] = annualized
    payload["cost_adjusted"].update(
        {
            "realized_edge_bps": 7.96,
            "fee_bps": 5.0,
            "slip_bps": 1.0,
            "exec_buffer_bps": 0.0,
            "net_edge_bps": 1.96,
        }
    )
    payload["cost_adjusted"]["train"].update(
        {"realized_edge_bps": 8.2, "net_edge_bps": 2.2}
    )
    payload["cost_adjusted"]["test"].update(
        {"realized_edge_bps": 7.8, "net_edge_bps": 1.8}
    )
    payload["cost_adjusted"]["sensitivity"].update(
        {
            "overall": {
                "net_edge_fee_up_20pct_bps": 0.96,
                "net_edge_slip_plus_0_5bps_bps": 1.46,
            },
            "train": {
                "net_edge_fee_up_20pct_bps": 1.2,
                "net_edge_slip_plus_0_5bps_bps": 1.7,
            },
            "test": {
                "net_edge_fee_up_20pct_bps": 0.8,
                "net_edge_slip_plus_0_5bps_bps": 1.3,
            },
        }
    )
    payload["regime_slice"]["vol"]["low"]["ir"] = 0.72
    payload["regime_slice"]["vol"]["high"]["ir"] = 0.48
    return payload


class TestProposalMdPrefill(unittest.TestCase):
    """SoW 要求 proposal.md 预填 metadata + §4 + §6.1 + §6.2 + §6.3 + §6.4
    + observation-window 摘要, 且不出现 verdict / 裁决文案。
    """

    def _build_bundle(self, tmp: Path) -> Path:
        scorecard_src = tmp / "scorecard.json"
        observation_src = tmp / "observation.json"
        _write_json(scorecard_src, _rich_scorecard_payload())
        _write_json(observation_src, _valid_observation_payload())

        inputs = ScaffoldInputs(
            proposal_id="route-a-phase0-ofi-5s-20260430",
            feature="OFI",
            horizon="5s",
            scorecard_json=scorecard_src,
            observation_window_json=observation_src,
            proposer="alice",
            output_root=tmp / "bundle",
        )
        result = create_scaffold(inputs)
        return result.proposal_md_path

    def test_prefilled_sections_contain_expected_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            md_text = self._build_bundle(Path(tmp)).read_text(encoding="utf-8")

            # metadata
            self.assertIn("BTC-USDT", md_text)
            self.assertIn("aats-research-20260420", md_text)
            self.assertIn("ioc", md_text)
            self.assertIn("2026-04-01T00:00:00+00:00", md_text)

            # §4 train/test boundaries + split
            self.assertIn("train_start", md_text)
            self.assertIn("train_end", md_text)
            self.assertIn("test_start", md_text)
            self.assertIn("test_end", md_text)
            self.assertIn("split_method", md_text)
            self.assertIn("explicit", md_text)
            self.assertIn("2026-04-01T12:45:00+00:00", md_text)

            # §6.1 OOS train/test numeric cells present
            self.assertIn("§6.1 OOS", md_text)
            self.assertIn("1.23", md_text)
            self.assertIn("0.91", md_text)
            self.assertIn("0.54", md_text)
            self.assertIn("49", md_text)

            # §6.2 cross-window: S1/S2/S3 labels + values
            self.assertIn("§6.2 Cross-window", md_text)
            self.assertIn("| S1 |", md_text)
            self.assertIn("| S2 |", md_text)
            self.assertIn("| S3 |", md_text)
            self.assertIn("0.88", md_text)
            self.assertIn("0.95", md_text)
            self.assertIn("0.9", md_text)  # last slice ir_annualized 0.90

            # §6.3 cost-adjusted + sensitivity
            self.assertIn("§6.3 Cost-adjusted", md_text)
            self.assertIn("realized_edge_bps", md_text)
            self.assertIn("net_edge_bps", md_text)
            self.assertIn("exec_buffer_bps", md_text)
            self.assertIn("fee 上调 20%", md_text)
            self.assertIn("slip +0.5 bps", md_text)
            # sensitivity numeric values
            self.assertIn("1.2", md_text)
            self.assertIn("0.8", md_text)
            self.assertIn("1.3", md_text)

            # §6.4 regime-slice vol low/high 预填
            self.assertIn("§6.4 Regime-slice", md_text)
            self.assertIn("| low_vol |", md_text)
            self.assertIn("| high_vol |", md_text)
            self.assertIn("0.72", md_text)
            self.assertIn("0.48", md_text)
            self.assertIn("| low_vol | 0.72 | 5 | 50 |", md_text)
            self.assertIn("| high_vol | 0.48 | 5 | 49 |", md_text)
            # §6.4 明确点出 funding 方向 / 2×2 heatmap 仍待手工
            self.assertIn("funding 方向", md_text)

            # observation-window summary
            self.assertIn("观察窗摘要", md_text)
            self.assertIn("overall", md_text)
            self.assertIn("window_start", md_text)
            self.assertIn("window_target", md_text)
            self.assertIn("warn_count", md_text)
            self.assertIn("fail_count", md_text)
            self.assertIn("2026-04-22T00:00:00Z", md_text)
            self.assertIn("2026-04-29T00:00:00Z", md_text)
            self.assertIn("incomplete_single_snapshot", md_text)
            self.assertIn("2026-04-24T14:02:11Z", md_text)
            self.assertIn("不能证明连续 7 天完成", md_text)
            self.assertIn("资本资格", md_text)

            # 仍保留待填提示
            self.assertIn("待人工填写", md_text)

    def test_no_verdict_or_gate_ruling_words_present(self) -> None:
        """SoW 硬边界: proposal.md 不得输出 verdict / 归档 / 过关裁决文案。"""
        with tempfile.TemporaryDirectory() as tmp:
            md_text = self._build_bundle(Path(tmp)).read_text(encoding="utf-8")
            for forbidden in ("PASS", "FAIL", "Archive", "Go", "verdict"):
                self.assertNotIn(
                    forbidden,
                    md_text,
                    f"proposal.md 不应出现裁决文案: {forbidden!r}",
                )

    def test_complete_scorecard_preserves_manual_tbd_sections(self) -> None:
        """完整 scorecard 只预填机械证据，人工提案字段仍保留 <TBD>。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            _write_json(scorecard_src, _valid_scorecard_payload())
            _write_json(observation_src, _valid_observation_payload())

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="OFI",
                horizon="5s",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                output_root=root / "bundle",
            )
            result = create_scaffold(inputs)
            md_text = result.proposal_md_path.read_text(encoding="utf-8")

            # 所有关键段落仍写出
            self.assertIn("§6.1 OOS", md_text)
            self.assertIn("§6.2 Cross-window", md_text)
            self.assertIn("§6.3 Cost-adjusted", md_text)
            self.assertIn("§6.4 Regime-slice", md_text)
            self.assertIn("| low_vol |", md_text)
            self.assertIn("| high_vol |", md_text)
            self.assertIn("观察窗摘要", md_text)
            # 没有机械来源的人工字段仍渲染为 <TBD>。
            self.assertIn("<TBD>", md_text)

    def test_regime_slice_missing_vol_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            payload = _valid_scorecard_payload()
            payload["regime_slice"] = {}  # keep top-level key, drop vol/low/high
            _write_json(scorecard_src, payload)
            _write_json(observation_src, _valid_observation_payload())

            inputs = ScaffoldInputs(
                proposal_id="p1",
                feature="OFI",
                horizon="5s",
                scorecard_json=scorecard_src,
                observation_window_json=observation_src,
                output_root=root / "bundle",
            )
            with self.assertRaisesRegex(ScaffoldError, "regime_slice"):
                create_scaffold(inputs)
            self.assertFalse((root / "bundle").exists())


class TestCLIRouteAScaffold(unittest.TestCase):
    def test_cli_end_to_end_creates_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "scorecard.json"
            observation_src = root / "observation.json"
            _write_json(scorecard_src, _valid_scorecard_payload())
            _write_json(observation_src, _valid_observation_payload())

            output_root = root / "bundle"
            rc = main(
                [
                    "route-a-evidence-scaffold",
                    "--proposal-id",
                    "route-a-phase0-ofi-5s-20260430",
                    "--feature",
                    "OFI",
                    "--horizon",
                    "5s",
                    "--scorecard-json",
                    str(scorecard_src),
                    "--observation-window-json",
                    str(observation_src),
                    "--proposer",
                    "alice",
                    "--output-root",
                    str(output_root),
                ]
            )
            self.assertEqual(rc, 0)

            proposal_dir = output_root / "route-a-phase0-ofi-5s-20260430"
            self.assertTrue((proposal_dir / "manifest.json").is_file())
            self.assertTrue((proposal_dir / "scorecard.json").is_file())
            self.assertTrue(
                (proposal_dir / "observation_window_summary.json").is_file()
            )
            self.assertTrue((proposal_dir / "proposal.md").is_file())

    def test_cli_refuses_overwrite_with_systemexit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scorecard_src = root / "s.json"
            observation_src = root / "o.json"
            _write_json(scorecard_src, _valid_scorecard_payload())
            _write_json(observation_src, _valid_observation_payload())

            output_root = root / "bundle"
            (output_root / "p1").mkdir(parents=True)

            argv = [
                "route-a-evidence-scaffold",
                "--proposal-id",
                "p1",
                "--feature",
                "OFI",
                "--horizon",
                "5s",
                "--scorecard-json",
                str(scorecard_src),
                "--observation-window-json",
                str(observation_src),
                "--output-root",
                str(output_root),
            ]
            with self.assertRaises(SystemExit):
                main(argv)


if __name__ == "__main__":
    unittest.main()
