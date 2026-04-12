#!/usr/bin/env python3
"""从治理层 registry 选取参数并写入 active parameter set.

阶段 B 交付物：将 Phase 2/5/6 的治理结论以受控方式回灌主交易系统。

用法:
    # 显示当前 registry 中的 frozen/candidate 参数
    python scripts/apply_active_parameter_set.py --action show

    # 应用指定 parameter set 为 active
    python scripts/apply_active_parameter_set.py --action apply --ps-id ps_20260404_072612_a5cc10

    # 从 frozen 参数自动生成全部 active sets
    python scripts/apply_active_parameter_set.py --action apply-frozen

    # 查看当前 active parameter sets
    python scripts/apply_active_parameter_set.py --action show-active

    # 清除指定 combo 的 active set
    python scripts/apply_active_parameter_set.py --action clear --combo independent_15m

退出码:
    0 = 成功
    1 = 错误
    2 = 无可应用的参数
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="管理 active parameter sets")
    p.add_argument(
        "--action",
        choices=("show", "show-active", "apply", "apply-frozen", "clear", "seed-db"),
        required=True,
    )
    p.add_argument(
        "--ps-id",
        default=None,
        help="要应用的 parameter_set_id（用于 --action apply）",
    )
    p.add_argument(
        "--combo",
        default=None,
        help="family_timeframe combo key（用于 --action clear）",
    )
    p.add_argument(
        "--recommendation-id",
        default=None,
        help="关联的 recommendation_id（用于审计追踪）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="仅展示将要执行的操作，不实际写入",
    )
    return p.parse_args()


def _get_db_engine():
    """尝试创建 DB engine，返回 (engine, True) 或 (None, False)."""
    db_url = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if not db_url:
        return None, False
    try:
        from sqlalchemy import create_engine, text as sa_text

        engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
        with engine.connect() as conn:
            conn.execute(sa_text("SELECT 1 FROM governance.active_parameter_sets LIMIT 0"))
        return engine, True
    except Exception as exc:
        print(f"[WARN] DB 不可用: {exc}")
        return None, False


def _try_db_write_active(
    *,
    family: str,
    timeframe: str,
    parameter_set_id: str,
    values: dict,
    source_round_id: str | None = None,
    recommendation_id: str | None = None,
    applied_by: str = "apply_active_parameter_set.py",
) -> bool:
    """尝试写入 governance.active_parameter_sets DB，成功返回 True."""
    engine, ok = _get_db_engine()
    if not ok:
        return False
    try:
        from sqlalchemy import text as sa_text
        from sqlalchemy.orm import Session

        from aats.data_platform.governance.active_params_db import (
            db_append_history,
            db_upsert_active_set,
        )

        with Session(engine) as session, session.begin():
            existing = session.execute(
                sa_text(
                    "SELECT parameter_set_id FROM governance.active_parameter_sets "
                    "WHERE family = :f AND timeframe = :t"
                ),
                {"f": family, "t": timeframe.lower()},
            ).fetchone()
            from_ps_id = existing.parameter_set_id if existing else None

            db_upsert_active_set(
                session,
                family=family, timeframe=timeframe,
                parameter_set_id=parameter_set_id, values=values,
                source_round_id=source_round_id,
                approval_recommendation_id=recommendation_id,
                applied_by=applied_by,
            )

            op_id = f"op_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
            db_append_history(
                session,
                operation_id=op_id, operation_type="apply",
                family=family, timeframe=timeframe,
                from_parameter_set_id=from_ps_id,
                to_parameter_set_id=parameter_set_id,
                recommendation_id=recommendation_id,
                actor=applied_by,
            )
        return True
    except Exception as exc:
        print(f"[WARN] DB 写入失败: {exc}")
        return False
    finally:
        if engine is not None:
            engine.dispose()


def action_show(project_root: Path) -> int:
    """展示 registry 中可用的参数."""
    from aats.data_platform.governance.parameter_registry import (
        find_parameter_sets,
        load_registry,
    )

    reg_path = project_root / "artifacts/governance/current_parameter_registry.json"
    registry = load_registry(reg_path)
    all_sets = registry.get("parameter_sets", [])

    print(f"Parameter Registry: {reg_path}")
    print(f"Version: {registry.get('version', 'N/A')}")
    print(f"Total parameter sets: {len(all_sets)}")
    print()

    for status in ("frozen", "candidate", "draft", "deprecated"):
        sets = find_parameter_sets(registry, status=status)
        if not sets:
            continue
        print(f"  [{status.upper()}] ({len(sets)} sets)")
        for ps in sets:
            print(f"    {ps['parameter_set_id']}: {ps['family']}/{ps['timeframe']}")
            vals = ps.get("values", {})
            for k, v in sorted(vals.items()):
                print(f"      {k}: {v}")
            if ps.get("frozen_at"):
                print(f"      frozen_at: {ps['frozen_at']}")
            print()

    return 0


def action_show_active(project_root: Path) -> int:
    """展示当前 active parameter sets."""
    from aats.bootstrap.active_parameters import get_active_parameter_summary

    summary = get_active_parameter_summary(project_root=project_root)

    print("Active Parameter Sets")
    print("=" * 50)
    print(f"Total: {summary['total_active_sets']}")
    print(f"Known combos: {', '.join(summary['known_combos'])}")
    print(f"Active combos: {', '.join(summary['active_combos']) or '(none)'}")
    print(f"Missing combos: {', '.join(summary['missing_combos']) or '(none)'}")
    print()

    for ps in summary["parameter_sets"]:
        print(f"  [{ps['combo_key']}]")
        print(f"    parameter_set_id: {ps['parameter_set_id']}")
        print(f"    status: {ps['status']}")
        print(f"    applied_at: {ps['applied_at']}")
        print(f"    applied_by: {ps['applied_by']}")
        if ps.get("approval_recommendation_id"):
            print(f"    recommendation: {ps['approval_recommendation_id']}")
        for k, v in sorted(ps.get("values", {}).items()):
            print(f"    {k}: {v}")
        print()

    return 0


def action_apply(project_root: Path, args: argparse.Namespace) -> int:
    """应用指定 parameter set."""
    from aats.bootstrap.active_parameters import upsert_active_registry, write_active_parameter_set
    from aats.data_platform.governance.parameter_registry import load_registry

    if not args.ps_id:
        print("[ERROR] --ps-id 必须指定")
        return 1

    reg_path = project_root / "artifacts/governance/current_parameter_registry.json"
    registry = load_registry(reg_path)

    # 查找 parameter set
    target = None
    for ps in registry.get("parameter_sets", []):
        if ps["parameter_set_id"] == args.ps_id:
            target = ps
            break

    if target is None:
        print(f"[ERROR] 未找到 parameter set: {args.ps_id}")
        return 1

    # 检查状态
    if target["status"] not in ("frozen", "candidate"):
        print(f"[WARNING] parameter set 状态为 '{target['status']}'，建议只应用 frozen/candidate")
        print("  继续? 使用 --dry-run 预览")

    combo_key = f"{target['family']}_{target['timeframe'].lower()}"
    print(f"将要应用: {combo_key}")
    print(f"  parameter_set_id: {target['parameter_set_id']}")
    print(f"  status: {target['status']}")
    print(f"  values: {json.dumps(target['values'], ensure_ascii=False)}")

    if args.dry_run:
        print("\n[DRY RUN] 未实际写入")
        return 0

    # 写入 per-file 和 registry 两种格式
    path = write_active_parameter_set(
        family=target["family"],
        timeframe=target["timeframe"],
        parameter_set_id=target["parameter_set_id"],
        values=target["values"],
        source_round_id=target.get("source_round_id"),
        approval_recommendation_id=args.recommendation_id,
        applied_by="apply_active_parameter_set.py",
        project_root=project_root,
    )
    upsert_active_registry(
        family=target["family"],
        timeframe=target["timeframe"],
        parameter_set_id=target["parameter_set_id"],
        values=target["values"],
        project_root=project_root,
    )

    print(f"\n[OK] 已写入: {path}")
    print(f"[OK] 已更新 active_parameter_registry.json")

    # DB 双写
    db_ok = _try_db_write_active(
        family=target["family"], timeframe=target["timeframe"],
        parameter_set_id=target["parameter_set_id"],
        values=target["values"],
        source_round_id=target.get("source_round_id"),
        recommendation_id=args.recommendation_id,
        applied_by="apply_active_parameter_set.py (apply)",
    )
    if db_ok:
        print("[OK] 已写入数据库 governance.active_parameter_sets")

    # 记录应用日志
    _write_application_log(
        project_root,
        action="apply",
        combo_key=combo_key,
        parameter_set_id=target["parameter_set_id"],
        recommendation_id=args.recommendation_id,
    )

    return 0


def action_apply_frozen(project_root: Path, args: argparse.Namespace) -> int:
    """从所有 frozen 参数自动生成 active sets."""
    from aats.bootstrap.active_parameters import upsert_active_registry, write_active_parameter_set
    from aats.data_platform.governance.parameter_registry import (
        find_parameter_sets,
        load_registry,
    )

    reg_path = project_root / "artifacts/governance/current_parameter_registry.json"
    registry = load_registry(reg_path)
    frozen_sets = find_parameter_sets(registry, status="frozen")

    if not frozen_sets:
        print("[INFO] 没有 frozen 状态的参数可应用")
        return 2

    print(f"找到 {len(frozen_sets)} 个 frozen parameter sets:")
    for ps in frozen_sets:
        combo = f"{ps['family']}_{ps['timeframe'].lower()}"
        print(f"  {combo}: {ps['parameter_set_id']}")

    if args.dry_run:
        print("\n[DRY RUN] 未实际写入")
        return 0

    applied = 0
    for ps in frozen_sets:
        path = write_active_parameter_set(
            family=ps["family"],
            timeframe=ps["timeframe"],
            parameter_set_id=ps["parameter_set_id"],
            values=ps["values"],
            source_round_id=ps.get("source_round_id"),
            applied_by="apply_active_parameter_set.py (apply-frozen)",
            project_root=project_root,
        )
        upsert_active_registry(
            family=ps["family"],
            timeframe=ps["timeframe"],
            parameter_set_id=ps["parameter_set_id"],
            values=ps["values"],
            project_root=project_root,
        )
        # DB 双写
        db_ok = _try_db_write_active(
            family=ps["family"], timeframe=ps["timeframe"],
            parameter_set_id=ps["parameter_set_id"],
            values=ps["values"],
            source_round_id=ps.get("source_round_id"),
            applied_by="apply_active_parameter_set.py (apply-frozen)",
        )
        db_label = " +DB" if db_ok else ""
        print(f"  [OK] {path}{db_label}")
        applied += 1

        _write_application_log(
            project_root,
            action="apply-frozen",
            combo_key=f"{ps['family']}_{ps['timeframe'].lower()}",
            parameter_set_id=ps["parameter_set_id"],
        )

    print(f"\n共应用 {applied} 个 active parameter sets")
    return 0


def action_clear(project_root: Path, args: argparse.Namespace) -> int:
    """清除指定 combo 的 active parameter set."""
    if not args.combo:
        print("[ERROR] --combo 必须指定")
        return 1

    active_dir = project_root / "configs" / "active_parameter_sets"
    file_path = active_dir / f"{args.combo}.json"

    if not file_path.exists():
        print(f"[INFO] {args.combo} 没有 active parameter set")
        return 0

    if args.dry_run:
        print(f"[DRY RUN] 将删除: {file_path}")
        return 0

    file_path.unlink()
    print(f"[OK] 已清除: {file_path}")

    # DB 清除
    engine, ok = _get_db_engine()
    if ok:
        try:
            parts = args.combo.rsplit("_", 1)
            if len(parts) == 2:
                family, timeframe = parts[0], parts[1]
                from sqlalchemy.orm import Session

                from aats.data_platform.governance.active_params_db import db_clear_active_set

                with Session(engine) as session, session.begin():
                    db_clear_active_set(session, family, timeframe)
                print("[OK] 已从数据库清除")
        except Exception as exc:
            print(f"[WARN] DB 清除失败: {exc}")
        finally:
            if engine is not None:
                engine.dispose()

    _write_application_log(
        project_root,
        action="clear",
        combo_key=args.combo,
    )
    return 0


def _write_application_log(
    project_root: Path,
    *,
    action: str,
    combo_key: str,
    parameter_set_id: str | None = None,
    recommendation_id: str | None = None,
) -> None:
    """记录参数应用操作日志."""
    log_dir = project_root / "artifacts" / "governance" / "application_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "parameter_application_history.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "combo_key": combo_key,
        "parameter_set_id": parameter_set_id,
        "recommendation_id": recommendation_id,
    }

    with log_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def action_seed_db(project_root: Path, args: argparse.Namespace) -> int:
    """将所有治理层 JSON 注册表一次性种子到数据库.

    种子 4 张表:
      1. governance.parameter_sets       <- current_parameter_registry.json
      2. governance.recommendations      <- recommendation_registry.json
      3. governance.active_decisions     <- active_decision_registry.json
      4. governance.active_parameter_sets <- active_parameter_registry.json
    """
    db_url = os.environ.get("AATS_ACTIVE_PARAMETER_DB_URL")
    if not db_url:
        print("[ERROR] 环境变量 AATS_ACTIVE_PARAMETER_DB_URL 未设置")
        return 1

    from sqlalchemy import create_engine, text as sa_text
    from sqlalchemy.orm import Session

    # 确保表存在
    engine = create_engine(db_url, pool_pre_ping=True)
    try:
        from aats.data_platform.rdp_models import create_rdp_schema
        create_rdp_schema(engine)
        print("[OK] governance schema + 表已就绪")
    except Exception as exc:
        print(f"[WARN] create_rdp_schema 失败: {exc}（表可能已存在，继续）")

    stats = {"parameter_sets": 0, "recommendations": 0, "active_decisions": 0, "active_parameter_sets": 0}

    with Session(engine) as session, session.begin():
        # ── 1. parameter_registry.json → governance.parameter_sets ──
        from aats.data_platform.governance.parameter_registry import load_registry
        from aats.data_platform.governance.parameter_sets_db import db_upsert_parameter_set

        reg_path = project_root / "artifacts/governance/current_parameter_registry.json"
        if reg_path.exists():
            registry = load_registry(reg_path, skip_db=True)
            for ps in registry.get("parameter_sets", []):
                db_upsert_parameter_set(
                    session,
                    parameter_set_id=ps["parameter_set_id"],
                    family=ps["family"],
                    timeframe=ps["timeframe"],
                    values=ps.get("values", {}),
                    status=ps.get("status", "draft"),
                    symbol=ps.get("symbol", "BTC-USDT-SWAP"),
                    source_round_id=ps.get("source_round_id"),
                    source_phase=ps.get("source_phase"),
                    dataset_version=ps.get("dataset_version", "v1.0"),
                    confidence=ps.get("confidence"),
                    created_at=ps.get("created_at"),
                    frozen_at=ps.get("frozen_at"),
                    deprecated_at=ps.get("deprecated_at"),
                    notes=ps.get("notes"),
                )
                stats["parameter_sets"] += 1
            print(f"  [OK] parameter_sets: {stats['parameter_sets']} 条")
        else:
            print(f"  [SKIP] {reg_path} 不存在")

        # ── 2. recommendation_registry.json → governance.recommendations ──
        from aats.data_platform.decision_system.recommendation_registry import (
            load_recommendation_registry,
        )
        from aats.data_platform.governance.recommendations_db import (
            db_upsert_active_decision,
            db_upsert_recommendation,
        )

        rec_path = project_root / "artifacts/decision_system/recommendation_registry.json"
        if rec_path.exists():
            rec_reg = load_recommendation_registry(rec_path, skip_db=True)
            for rec in rec_reg.get("recommendations", []):
                db_upsert_recommendation(
                    session,
                    recommendation_id=rec["recommendation_id"],
                    family=rec["family"],
                    timeframe=rec["timeframe"],
                    recommendation_type=rec.get("recommendation_type", "require_review"),
                    confidence=rec.get("confidence", "low"),
                    reason=rec.get("reason", ""),
                    symbol=rec.get("symbol", "BTC-USDT-SWAP"),
                    target_parameter_set_id=rec.get("target_parameter_set_id"),
                    evidence_bundle_ref=rec.get("evidence_bundle_ref"),
                    status=rec.get("status", "draft"),
                    approved_by=rec.get("approved_by"),
                    approved_at=rec.get("approved_at"),
                    approval_notes=rec.get("approval_notes"),
                    rejected_by=rec.get("rejected_by"),
                    rejected_at=rec.get("rejected_at"),
                    superseded_at=rec.get("superseded_at"),
                    superseded_by_recommendation_id=rec.get("superseded_by_recommendation_id"),
                    created_at=rec.get("created_at"),
                )
                stats["recommendations"] += 1
            print(f"  [OK] recommendations: {stats['recommendations']} 条")
        else:
            print(f"  [SKIP] {rec_path} 不存在")

        # ── 3. active_decision_registry.json → governance.active_decisions ──
        from aats.data_platform.decision_system.recommendation_registry import (
            load_active_decision_registry,
        )

        dec_path = project_root / "artifacts/decision_system/active_decision_registry.json"
        if dec_path.exists():
            dec_reg = load_active_decision_registry(dec_path, skip_db=True)
            for d in dec_reg.get("decisions", []):
                db_upsert_active_decision(
                    session,
                    family=d["family"],
                    timeframe=d["timeframe"],
                    current_status=d.get("current_status", "require_review"),
                    symbol=d.get("symbol", "BTC-USDT-SWAP"),
                    active_parameter_set_id=d.get("active_parameter_set_id"),
                    last_recommendation_id=d.get("last_recommendation_id"),
                    notes=d.get("notes"),
                )
                stats["active_decisions"] += 1
            print(f"  [OK] active_decisions: {stats['active_decisions']} 条")
        else:
            print(f"  [SKIP] {dec_path} 不存在")

        # ── 4. active_parameter_registry.json → governance.active_parameter_sets ──
        from aats.bootstrap.active_parameters import load_active_parameter_registry
        from aats.data_platform.governance.active_params_db import (
            db_append_history,
            db_upsert_active_set,
        )

        active_reg = load_active_parameter_registry(project_root=project_root, skip_db=True)
        for combo_key, entry in active_reg.get("active_sets", {}).items():
            parts = combo_key.rsplit("_", 1)
            family = parts[0] if len(parts) == 2 else combo_key
            timeframe = parts[1] if len(parts) == 2 else "unknown"

            db_upsert_active_set(
                session,
                family=family, timeframe=timeframe,
                parameter_set_id=entry["parameter_set_id"],
                values=entry.get("values", {}),
                source_round_id=entry.get("source_round_id"),
                approval_recommendation_id=entry.get("approval_recommendation_id"),
                applied_by=entry.get("applied_by", "seed-db"),
            )

            op_id = f"op_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
            db_append_history(
                session,
                operation_id=op_id, operation_type="seed",
                family=family, timeframe=timeframe,
                to_parameter_set_id=entry["parameter_set_id"],
                actor="seed-db",
                notes="初始种子：从文件 registry 导入",
            )
            stats["active_parameter_sets"] += 1
        print(f"  [OK] active_parameter_sets: {stats['active_parameter_sets']} 条")

    engine.dispose()

    print(f"\n=== seed-db 完成 ===")
    for table, count in stats.items():
        print(f"  governance.{table}: {count} 条")
    print("全部使用 ON CONFLICT DO UPDATE，幂等操作。")
    return 0


def main() -> int:
    args = parse_args()
    project_root = ROOT

    if args.action == "show":
        return action_show(project_root)
    elif args.action == "show-active":
        return action_show_active(project_root)
    elif args.action == "apply":
        return action_apply(project_root, args)
    elif args.action == "apply-frozen":
        return action_apply_frozen(project_root, args)
    elif args.action == "clear":
        return action_clear(project_root, args)
    elif args.action == "seed-db":
        return action_seed_db(project_root, args)
    else:
        print(f"[ERROR] 未知操作: {args.action}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
