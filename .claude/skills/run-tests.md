---
name: run-tests
description: 运行 AATS 单元测试或集成测试
---

# /run-tests — AATS 测试执行

## Windows 单元测试（默认）

```bash
# 全量单元测试
.venv\Scripts\python.exe -m pytest tests/unit/ -x -q

# 指定模块
.venv\Scripts\python.exe -m pytest tests/unit/ -k "关键词" -x -q

# 指定文件
.venv\Scripts\python.exe -m pytest tests/unit/test_xxx.py -x -q
```

## WSL2 集成测试

集成测试使用 testcontainers，必须在 WSL2 中运行：

```bash
wsl -d Ubuntu bash -c "cd ~/aats && source ~/aats-venv/bin/activate && pytest tests/integration/ -x -q"
```

## Lint 检查

```bash
.venv\Scripts\python.exe -m ruff check aats/ --fix
.venv\Scripts\python.exe -m ruff format aats/
```

## 注意事项

- Windows Python 路径: `.venv\Scripts\python.exe`
- WSL2 venv: `~/aats-venv`
- 不要声称测试通过但没实际运行
- 测试失败时解释清楚原因，不要隐瞒
