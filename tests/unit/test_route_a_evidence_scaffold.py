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
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from aats.cli import main
from aats.data_platform.replay.backtest.route_a_evidence_scaffold import (
    OBSERVATION_WINDOW_REQUIRED_KEYS,
    SCORECARD_REQUIRED_KEYS,
    ScaffoldError,
    ScaffoldInputs,
    create_scaffold,
)


def _valid_scorecard_payload() -> dict:
    return {
        "meta": {"symbol": "BTC-USDT-SWAP"},
        "oos": {"train": {}, "test": {}},
        "cross_window": [],
        "cost_adjusted": {"fee_bps": 0.0},
        "regime_slice": {"vol": {"low": {}, "high": {}}},
    }


def _valid_observation_payload() -> dict:
    return {
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


def _rich_scorecard_payload() -> dict:
    """Scorecard 以真实形态提供 meta/oos/cross_window/cost_adjusted 字段,
    用于验证 proposal.md 的预填段落。
    """
    return {
        "meta": {
            "symbol": "BTC-USDT-SWAP",
            "timeframe": "15m",
            "dataset_version": "aats-research-20260420",
            "order_type": "ioc",
            "start_ts": "2026-03-01T00:00:00+00:00",
            "end_ts": "2026-04-20T00:00:00+00:00",
            "generated_at": "2026-04-25T12:00:00+00:00",
        },
        "oos": {
            "split_method": "explicit",
            "split_ts": "2026-04-01T00:00:00+00:00",
            "train": {
                "start": "2026-03-01T00:00:00+00:00",
                "end": "2026-04-01T00:00:00+00:00",
                "ir_annualized": 1.23,
                "sharpe_ratio": 1.23,
                "hit_rate": 0.54,
                "max_drawdown_bps": 80.0,
                "sample_n": 720,
            },
            "test": {
                "start": "2026-04-01T00:00:00+00:00",
                "end": "2026-04-20T00:00:00+00:00",
                "ir_annualized": 0.91,
                "sharpe_ratio": 0.91,
                "hit_rate": 0.51,
                "max_drawdown_bps": 45.0,
                "sample_n": 456,
            },
        },
        "cross_window": [
            {
                "start": "2026-04-01T00:00:00+00:00",
                "end": "2026-04-07T00:00:00+00:00",
                "ir_annualized": 0.88,
                "hit_rate": 0.52,
                "max_drawdown_bps": 30.0,
                "sample_n": 150,
            },
            {
                "start": "2026-04-07T00:00:00+00:00",
                "end": "2026-04-14T00:00:00+00:00",
                "ir_annualized": 0.95,
                "hit_rate": 0.50,
                "max_drawdown_bps": 40.0,
                "sample_n": 155,
            },
            {
                "start": "2026-04-14T00:00:00+00:00",
                "end": "2026-04-20T00:00:00+00:00",
                "ir_annualized": 0.90,
                "hit_rate": 0.51,
                "max_drawdown_bps": 35.0,
                "sample_n": 151,
            },
        ],
        "cost_adjusted": {
            "fee_bps": 1.0,
            "slip_bps": 0.5,
            "exec_buffer_bps": 0.2,
            "realized_edge_bps": 3.0,
            "net_edge_bps": 1.3,
            "train": {
                "realized_edge_bps": 3.2,
                "fee_bps": 1.0,
                "slip_bps": 0.5,
                "exec_buffer_bps": 0.2,
                "net_edge_bps": 1.5,
            },
            "test": {
                "realized_edge_bps": 2.8,
                "fee_bps": 1.0,
                "slip_bps": 0.5,
                "exec_buffer_bps": 0.2,
                "net_edge_bps": 1.1,
            },
            "sensitivity": {
                "overall": {
                    "net_edge_fee_up_20pct_bps": 1.1,
                    "net_edge_slip_plus_0_5bps_bps": 0.8,
                },
                "train": {
                    "net_edge_fee_up_20pct_bps": 1.3,
                    "net_edge_slip_plus_0_5bps_bps": 1.0,
                },
                "test": {
                    "net_edge_fee_up_20pct_bps": 0.9,
                    "net_edge_slip_plus_0_5bps_bps": 0.6,
                },
            },
        },
        "regime_slice": {"vol": {"low": {}, "high": {}}},
    }


class TestProposalMdPrefill(unittest.TestCase):
    """SoW 要求 proposal.md 预填 metadata + §4 + §6.1 + §6.2 + §6.3
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
            self.assertIn("BTC-USDT-SWAP", md_text)
            self.assertIn("aats-research-20260420", md_text)
            self.assertIn("ioc", md_text)
            self.assertIn("2026-03-01T00:00:00+00:00", md_text)

            # §4 train/test boundaries + split
            self.assertIn("train_start", md_text)
            self.assertIn("train_end", md_text)
            self.assertIn("test_start", md_text)
            self.assertIn("test_end", md_text)
            self.assertIn("split_method", md_text)
            self.assertIn("explicit", md_text)
            self.assertIn("2026-04-01T00:00:00+00:00", md_text)

            # §6.1 OOS train/test numeric cells present
            self.assertIn("§6.1 OOS", md_text)
            self.assertIn("1.23", md_text)
            self.assertIn("0.91", md_text)
            self.assertIn("0.54", md_text)
            self.assertIn("720", md_text)
            self.assertIn("456", md_text)

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
            self.assertIn("1.3", md_text)
            self.assertIn("0.9", md_text)
            self.assertIn("0.6", md_text)

            # observation-window summary
            self.assertIn("观察窗摘要", md_text)
            self.assertIn("overall", md_text)
            self.assertIn("window_start", md_text)
            self.assertIn("window_target", md_text)
            self.assertIn("warn_count", md_text)
            self.assertIn("fail_count", md_text)
            self.assertIn("2026-04-22T00:00:00Z", md_text)
            self.assertIn("2026-04-29T00:00:00Z", md_text)

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

    def test_minimal_scorecard_still_renders_with_tbd_placeholders(self) -> None:
        """当 scorecard 只带必需顶层键 (子字段缺失) 时, 预填段落仍可生成, 缺值用 <TBD>。"""
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
            self.assertIn("观察窗摘要", md_text)
            # 缺字段渲染为 <TBD>
            self.assertIn("<TBD>", md_text)


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
