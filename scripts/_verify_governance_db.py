#!/usr/bin/env python3
"""验证 governance DB 表创建和 seed-db."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 从 .env.research 加载环境变量
env_file = ROOT / "deploy/wsl2-dev/.env.research"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()

user = os.environ.get("POSTGRES_USER", "admin")
pw = os.environ.get("POSTGRES_PASSWORD", "123456")
db_url = f"postgresql+psycopg://{user}:{pw}@localhost:5432/aats_research"
os.environ["AATS_ACTIVE_PARAMETER_DB_URL"] = db_url

print(f"DB URL: ...@localhost:5432/aats_research")

from sqlalchemy import create_engine, text
from aats.data_platform.rdp_models import create_rdp_schema

engine = create_engine(db_url)

# Step 1: 建表
print("\n=== Step 1: create_rdp_schema ===")
create_rdp_schema(engine)
print("[OK] create_rdp_schema() completed")

# Step 2: 列出 governance 表
print("\n=== Step 2: governance tables ===")
with engine.connect() as conn:
    rows = conn.execute(text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'governance' ORDER BY table_name"
    ))
    tables = [r[0] for r in rows]
    for t in tables:
        print(f"  governance.{t}")
    print(f"Total: {len(tables)} tables")

# Step 3: 检查新表是否存在
expected_new = {"parameter_sets", "recommendations", "active_decisions"}
existing = set(tables)
missing = expected_new - existing
if missing:
    print(f"\n[FAIL] 缺少表: {missing}")
    engine.dispose()
    sys.exit(1)
else:
    print(f"\n[OK] 3 张新表全部存在")

# Step 4: 检查当前数据量
print("\n=== Step 3: current data ===")
with engine.connect() as conn:
    for t in sorted(tables):
        count = conn.execute(text(f"SELECT count(*) FROM governance.{t}")).scalar()
        print(f"  governance.{t}: {count} rows")

engine.dispose()
print("\n[OK] 验证完成")
