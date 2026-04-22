# Startup Prewarm live profile 修复设计稿

> 项目定位声明：本文件默认服从 AATS 的统一目标：在严格风控、可审计、可恢复、可治理前提下，通过自动化交易追求长期稳定盈利，为 AI 的持续自治与终身发展积累资本。详见 [项目定位声明](../project_positioning.md)。

- 日期：2026-04-21
- 状态：待审批
- 相关脚本：`scripts/prewarm_wsl2_aats.ps1`、`scripts/deploy.sh`、`.codex/skills/wsl2-deploy/scripts/run-deploy.ps1`
- 相关测试：`tests/unit/test_startup_prewarm_scripts.py`

## 1. 背景

Windows 登录后，`AATS-WSL2-Prewarm-derivatives-live` 计划任务执行 `scripts/prewarm_wsl2_aats.ps1`。它负责：唤醒 WSL → 等 Docker ready → 检查容器和 gateway `/healthz` → 若不健康则触发一次 repair deploy。

本次是重启电脑后实际观测到：prewarm 卡在 `deploy.sh` 的交互提示 `继续部署 WSL2 侧现有代码？[y/N]`，永不返回。

## 2. 症状证据

截图记录：

```
[startup-prewarm] AATS stack still not ready after 120s
[startup-prewarm] aats-gateway running healthy
[startup-prewarm] aats-market running healthy
[startup-prewarm] aats-decision running healthy
[startup-prewarm] aats-execution running healthy
[startup-prewarm] aats-rdp-daemon running healthy
[startup-prewarm] triggering repair deploy via standard wrapper
...
[deploy] 以下文件有改动：
 M .claude/settings.local.json
?? docs/design/post_only_maker_exit_mode_2026_04_21.md

继续部署 WSL2 侧现有代码？[y/N]
```

两项关键矛盾：

1. 5 个容器全部 `running healthy`，却被判定 "stack still not ready"。
2. 触发了 repair deploy；repair 用的是 `--skip-sync --skip-commit`，但仍然弹了交互提示，说明 `--skip-sync` 路径本身带 `read`。

## 3. 根因

### 3.1 Bug A — prewarm `Test-GatewayHealth` 协议写死 HTTP

[scripts/prewarm_wsl2_aats.ps1:160-170](../../scripts/prewarm_wsl2_aats.ps1):

```powershell
function Test-GatewayHealth {
    param([int]$Port)
    try {
        $response = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/healthz" -f $Port) ...
```

而 [scripts/deploy.sh:179-209](../../scripts/deploy.sh) 对任何 live profile（`spot-live`/`derivatives-live`/`derivatives-live-monolith`）都会：

```bash
OPERATOR_TLS_ENABLED=true
OPERATOR_HEALTH_SCHEME="https"
```

并生成本地自签证书，让 gateway 监听 HTTPS。

实测：
- `curl http://127.0.0.1:8011/healthz` → `000`（连接失败）
- `curl -k https://127.0.0.1:8011/healthz` → `200`

所以在 `derivatives-live` profile 下，prewarm 的 `Test-GatewayHealth` **永远**返回 false，`Wait-Until` 必然 120s 后放弃，继而总是触发 repair deploy。这是一个每次开机都必然踩的 bug。

### 3.2 Bug B — `deploy.sh` 在非交互环境下 `read` 阻塞

[scripts/deploy.sh:321-337](../../scripts/deploy.sh):

```bash
if repo_has_uncommitted_changes; then
    if [[ "$SKIP_SYNC" == true ]]; then
        log_warn "检测到未提交改动，且 --skip-sync 已开启..."
        git status --short
        echo
        read -r -p "继续部署 WSL2 侧现有代码？[y/N] " confirm
        if [[ "$confirm" != [yY] ]]; then
            log_info "已取消"
            exit 0
        fi
    else
        ...
```

Scheduled Task 以 `-WindowStyle Hidden` 通过 PowerShell 启动，无 TTY。`read` 在无 stdin 时会立即读到 EOF 得到空串，进入 `"" != [yY]` 分支 → `exit 0`。

等等 —— 这里有一个关键细节：用户截图显示脚本**卡住**在提示符，说明确实有 TTY（截图里能看到 Windows Terminal 标题 `C:\Windows\System32\Windo...`）。这表明这次开机 prewarm 不是被计划任务调起的 hidden 进程，而是用户手工在命令行看到或者计划任务意外以交互方式触发。

但无论 TTY 是否存在，**目标架构应当是无人值守**：即便 Scheduled Task 配成 hidden，一旦发生 Bug A 触发 repair，再碰上 Windows 工作区有未提交改动（常态），就会 `exit 0` —— 表面上"成功"返回，实际 repair 未执行，AATS 栈也就没有真正自愈。只是今天碰巧 TTY 存在，我们才看到了这个潜在死锁。

### 3.3 Bug C — 单元测试凝固了 Bug A

[tests/unit/test_startup_prewarm_scripts.py:11](../../tests/unit/test_startup_prewarm_scripts.py):

```python
assert 'http://127.0.0.1:{0}/healthz' in text
```

这条断言要求 prewarm 必须用 HTTP，修复时必须同时更新测试。

## 4. 修复方案

### 4.1 改动 A — prewarm 对 live profile 切换 HTTPS

在 `scripts/prewarm_wsl2_aats.ps1`：

- 新增函数 `Get-HealthScheme -ResolvedProfile` 返回 `'http'` 或 `'https'`，live 三个 profile 返回 `'https'`
- `Test-GatewayHealth` 接收 `[string]$Scheme` 参数，用该 scheme 拼 URL
- 对 `https` 分支跳过证书校验（自签证书）。PowerShell 5.1 兼容写法：通过 `[System.Net.ServicePointManager]::ServerCertificateValidationCallback` 临时放开，或使用 `-SkipCertificateCheck`（PS 6+）。为兼容 Windows 自带 PowerShell 5.1，采用 callback 方案。
- 两处 `Wait-Until` 的 condition 里把 scheme 一并传入

伪代码：

```powershell
function Get-HealthScheme {
    param([string]$ResolvedProfile)
    switch ($ResolvedProfile) {
        'spot-live' { return 'https' }
        'derivatives-live' { return 'https' }
        'derivatives-live-monolith' { return 'https' }
        default { return 'http' }
    }
}

function Test-GatewayHealth {
    param([int]$Port, [string]$Scheme)
    $url = '{0}://127.0.0.1:{1}/healthz' -f $Scheme, $Port
    try {
        if ($Scheme -eq 'https') {
            [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
        }
        $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return ($response.StatusCode -eq 200)
    } catch { return $false }
    finally {
        if ($Scheme -eq 'https') {
            [System.Net.ServicePointManager]::ServerCertificateValidationCallback = $null
        }
    }
}
```

### 4.2 改动 B — `deploy.sh` 支持非交互

策略：**双保险**。

1. 新增 `--yes`/`-y` 参数，开启后跳过 read 直接按 yes 走
2. 若未显式传 `--yes`，在 `read` 前用 `[[ -t 0 ]]` 检测 stdin 是否为 TTY；不是 TTY 时**不默认 yes**而是**明确报错退出非零**，让 prewarm 感知到失败

理由：默认 yes 有"未经授权扩大授权范围"的风险（CLAUDE.md 原则：Authorization stands for the scope specified, not beyond）。宁可让上层显式传 `--yes`。

伪代码（替换 [deploy.sh:321-337](../../scripts/deploy.sh)）：

```bash
# 新增参数解析
--yes|-y) ASSUME_YES=true; shift ;;

# step_commit 中
if repo_has_uncommitted_changes; then
    if [[ "$SKIP_SYNC" == true ]]; then
        log_warn "检测到未提交改动，且 --skip-sync 已开启；本次部署不会同步这些 Windows 改动"
        git status --short
        if [[ "$ASSUME_YES" == true ]]; then
            log_info "--yes 已指定，继续部署 WSL2 侧现有代码"
        elif [[ -t 0 ]]; then
            read -r -p "继续部署 WSL2 侧现有代码？[y/N] " confirm
            if [[ "$confirm" != [yY] ]]; then
                log_info "已取消"
                exit 0
            fi
        else
            log_error "非交互环境检测到未提交改动；请显式传 --yes 或先提交/同步"
            exit 4
        fi
    else
        ...
```

### 4.3 改动 C — `run-deploy.ps1` 透传 `-AssumeYes`，prewarm repair 默认开启

- `run-deploy.ps1` 新增 `[switch]$AssumeYes`；透传为 `--yes`
- `prewarm_wsl2_aats.ps1` 的 `Invoke-RepairDeploy` 内调 `run-deploy.ps1` 时带 `-AssumeYes`

这样语义清晰：
- 人类手工跑 `deploy.sh` 默认仍保留交互确认（保护）
- prewarm 自愈路径因为**明确知道**自己是自动化 + `--skip-sync` 语义受控，显式带 `--yes`

### 4.4 改动 D — 更新测试

`tests/unit/test_startup_prewarm_scripts.py`：

- 第 11 行：改为断言同时存在 `http://` 和 `https://` URL 模板，或断言 `Get-HealthScheme` 函数存在
- 新增：断言 `Invoke-RepairDeploy` 传了 `-AssumeYes`
- 新增一个针对 `deploy.sh` 的 grep-style 测试（用 Python 读取脚本文本）：
  - 断言 `--yes)` / `-y)` 分支存在
  - 断言 `ASSUME_YES` 被引用
  - 断言 `[[ -t 0 ]]` 分支存在

## 5. 影响面

| 影响对象 | 影响 | 风险 |
|---|---|---|
| 登录自动预热 | 不再误触发 repair；即便触发也不卡住 | 低 |
| 手工跑 `deploy.sh`（不带 `--yes`） | 行为不变，仍弹交互 | 零 |
| 手工跑 `deploy.sh --skip-sync` 通过管道/CI | 从"静默 exit 0"变成"exit 4 报错" | **行为变化**，但更安全 |
| `run-deploy.ps1` 既有调用方 | 默认不开 `-AssumeYes`，行为不变 | 零 |
| 单元测试 | 需同步更新，否则 CI 红 | 可控 |

## 6. 不做的事

- 不改 `prewarm_wsl2_aats.ps1` 调用拓扑（仍通过 `run-deploy.ps1` 包装）
- 不改 `deploy.sh` 主流程的 7 步顺序
- 不解决"每次 repair 都会重建全部容器导致订单暂停"这类更大的问题（本稿只管"不卡住"）
- 不把 `--skip-sync` 默认值改掉
- 不动 keepalive 脚本

## 7. 测试计划

### 7.1 单元测试

```bash
.venv\Scripts\python.exe -m pytest tests/unit/test_startup_prewarm_scripts.py -x -q
```

新增断言覆盖改动 A/B/C/D 全部。

### 7.2 脚本静态验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prewarm_wsl2_aats.ps1 -Profile derivatives-live -DryRun
```

dry-run 输出应提到 https scheme。

### 7.3 端到端（手工）

1. 保证 Windows 工作区有未提交改动（当前就有，符合条件）
2. 手工触发：
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\prewarm_wsl2_aats.ps1 -Profile derivatives-live
   ```
3. 预期：由于 HTTPS 生效，health check 首次就通过，输出 `AATS stack already healthy` 并 return；不触发 repair，不卡住。

### 7.4 故障注入（可选）

手工 `wsl -d Ubuntu bash -c 'docker stop aats-gateway'`，再跑 prewarm，验证 repair path 能自动走完不卡 `read`。

## 8. 回滚

改动范围都是单文件文本编辑，回滚即 `git revert` 相应 commit 即可。无数据库、无配置、无镜像变化。

## 9. 验收标准

- [ ] 单元测试 `tests/unit/test_startup_prewarm_scripts.py` 全绿
- [ ] prewarm 在 `derivatives-live` 健康栈上不再触发 repair
- [ ] 若注入故障触发 repair，能自动完成（不卡在 read 提示）
- [ ] 手工运行 `deploy.sh --skip-sync`（不带 `--yes`）在交互终端仍弹 `[y/N]`（保留人工保护）
- [ ] CLAUDE.md、现有运维文档无需改动（行为向后兼容）
