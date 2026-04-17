"""RDP governance schema migrations.

Batch A (detailed design: docs/task/rdp_hardening_batch_a_detailed_design.md):
  - batch_a_01_orphan_report.sql        (stage 4.4.1 — read-only orphan/distribution scan)
  - batch_a_02_add_fks.sql              (stage 4.4.2 — add 7 FOREIGN KEY constraints)
  - batch_a_03_add_uqs.sql              (stage 4.4.3 — add source_round_id + partial unique)
  - batch_a_04_add_checks.sql           (stage 4.4.4 — add 9 CHECK constraints)
  - batch_a_99_rollback.sql             (disaster rollback: drops all batch-A constraints)
"""
