# FS-004 Research Factory 候选选择与封存测试集整改证据

> 文档状态：Phase 3V train/valid 选择协议与 test 内容封存已实施；最终 OOS、历史产物审计、walk-forward 与独立复核开放  
> 最后核对：2026-08-25  
> 起始基线：`00b6df0f8a8d2665d6cae3e88996843767cd1f56`  
> 工作区：`codex/fs-002-kill-switch-p0` 上未提交 Phase 3A–3V 叠加变更  
> 运行时边界：未读取 `.env.*`，未连接研究/交易数据库、Redis、NATS、交易所或账户，未运行历史实验、发布参数、部署或下单  
> 生产决定：**REAL-MONEY PRODUCTION: NO-GO**

## 1. 整改前事实与 Phase 2 裁定

原 `research_factory_real_data_runner_v1` 虽构造 train/valid/test 三段，却只从 test 段计算
factor、future-return label、baseline metrics 和 deterministic gate，再据此写 candidate 与
recommendation。train/valid 仅参与数据准备和行数证据，没有承担开发稳定性或候选选择职责。

代码中未发现自动超参搜索、候选排名、自动参数发布或自动 live promotion，因此 Phase 2
把该问题从 P1 降为 P2；该降级只修正直接资金路径的严重度推断，不否定 test 被当作候选
评价集的事实。重复人工观察同一 test 后修改表达式或阈值，仍可能污染名义 OOS。

## 2. Phase 3V 当前实现

### 2.1 显式选择协议

real-data runner 升级为 `research_factory_real_data_runner_v2`，固定协议
`train_valid_selection_test_holdout_v2`：

1. train 独立计算 factor、segment-local label、metrics 和 stability gate；
2. valid 独立计算同一套证据，作为 candidate benchmark；
3. train 与 valid 必须同时 PASS，任一失败均不生成 candidate；
4. `metrics_snapshot.json` 与 candidate metrics 只代表 valid development evidence；
5. test 只参与 dataset input integrity/quality 与内容封存，不进入 factor evaluator、label、
   baseline/execution performance metrics 或 selection gate。

执行现实性也属于选择证据：Phase 3V 进一步要求 summary 声明
`benchmark_segment=valid`，且 `window_start/window_end` 精确匹配 valid segment；覆盖完整
实验窗口或标记为 test 都会失败关闭。execution metrics 只合并进 valid，不再进入 train
stability gate，从而阻断 test 经外部全窗口 summary 间接影响 candidate 的路径。

segment-local label 不跨 segment 借用未来价格。该约束阻止 train 尾部 label 读取 valid，
也阻止 valid 尾部 label 读取 test，但它不等价于 purged/embargoed walk-forward。

### 2.2 test 内容封存

`segment_content_fingerprint()` 对 prepared test rows、dataset fingerprint 和 segment name
执行规范化 SHA-256，生成 `rfseg_` 前缀的内容 seal。`development_evidence.json` 记录 test
row count、seal、`sealed_not_evaluated` 和 `metrics_exposed=false`，不保存 test 绩效。
独立的 dataset quality/source integrity gate 仍会检查包括 test 在内的行数、gap、funding
缺失和来源一致性；这是输入可用性验证，不是策略评价，但异常会阻止生成 candidate。

seal 证明本次 artifact 所引用的 test 内容身份；它不证明 test 从未被人查看、历史运行未
使用该数据、数据质量正确，或后续评估只执行一次。

### 2.3 artifact lineage 与失败关闭

manifest、candidate、recommendation 和 research-memory record 均引用
`development_evidence.json`。携带新 holdout 字段的 candidate 在 recommendation 构建时
必须同时满足：

- protocol、development segments、benchmark segment、holdout segment/status 精确匹配；
- test seal 具有合法 `rfseg_` SHA-256 形式；
- candidate 的 development evidence ref 与 recommendation evidence refs 完全一致。

任一字段不满足即拒绝构建 recommendation。新 recommendation 还固定披露：sealed test
尚未评估，当前 metrics 只是 development evidence。旧 candidate 不被原地伪装成 v2。

## 3. 代码证据

| 位置 | 当前事实 |
|---|---|
| `datasets/gold_bars.py:21-23,211-238` | test/任意 segment 精确内容 seal schema 与实现 |
| `real_data.py:70-76` | runner v2、选择协议、development evidence 与 holdout 常量 |
| `real_data.py:795-904` | train/valid 分段评估、segment 查找、双门合并和 development evidence |
| `real_data.py:906-960` | candidate/recommendation lineage 与 holdout 元数据 |
| `evidence.py:173-218,398-489` | execution report 模型、valid segment 与精确 benchmark window 契约 |
| `recommendations.py:226-258` | v2 sealed candidate 完整性失败关闭与限制披露 |
| `scripts/rdp_run_execution_realism.py` | Phase 4 summary contract/source/window/segment/dataset identity 写入 |
| `test_real_data_runner.py:244-430` | artifact 闭环、test 未评估、双门和 seal 对抗测试 |
| `test_recommendations.py:175-220` | development ref 失败关闭与限制披露 |
| `test_fs004_research_selection_holdout.py` | 协议常量与双门契约 |

行号只对应本次未提交工作区；后续改动应以符号名和测试为准。

## 4. 当前验证结果

| 检查 | 结果 | 可信边界 |
|---|---|---|
| FS-004 + real-data/recommendation/gold focused | `57 passed` | 含全窗口/test execution summary 失败关闭；无数据库、历史 artifact 或外部服务 |
| recommendation 加固相关回归 | `35 passed` | 新旧 candidate 版本边界与 v2 失败关闭 |
| Research Factory + Phase 4 contract 扩大回归 | `327 passed` | 包含 workflow/preapply/integrity 与 summary identity helper |
| focused Ruff | `All checks passed!` | 只证明本次研究模块和测试的静态质量 |
| unit strict-marker collection | `4439 tests collected` | 当前 Windows/Python 3.14 环境；无 unknown unit marker |
| 标准完整 unit（仓库内唯一 basetemp） | `4409 passed, 30 skipped, 1660 warnings, 85 subtests passed in 115.95s` | 完整 `tests/unit/`；无外部服务成功声明 |
| 仓库要求的原样完整 unit | `87 passed` 后系统 Temp ACL `PermissionError` | fixture setup 失败；无业务断言失败，随后以仓库内 basetemp 完整复跑 |
| 应用 Ruff `--fix` / 全仓 Ruff | `All checks passed!` / `All checks passed!` | 当前工作区静态检查 |
| dependency/connection verifier + pip check | `runtime=46 ci=33 images=9`; `ceiling=150 reserve=47 components=14 engine_calls=13`; `No broken requirements found` | 纯本地静态/开发环境一致性 |
| YAML / Markdown / diff | `16 YAML`; `789 files / 1049` 个普通相对本地目标，扩展复核 `1260` 个本地目标（含 Windows 编辑器绝对路径和 `:line` 引用）；`git diff --check` 通过 | 不验证外部 URL、anchor、Compose runtime 或 live 状态 |
| 历史 artifact/registry 审计 | **未执行** | 需要只读清单、lineage 和人工查看记录 |
| 最终 OOS / walk-forward | **未实现、未执行** | 当前 test 只有输入质量/内容 seal，没有绩效证据 |

完整 unit 先按仓库原样命令执行；`tmp_path` 在
`C:\Users\汤显彬\AppData\Local\Temp\pytest-of-汤显彬` 因既有 ACL 于第 88 项 setup 失败。
随后确认仓库内 Phase 3V basetemp 原先不存在，并用它重跑同一完整范围得到上表 4409/30
结果。没有删除或绕过系统目录权限，也没有把 fixture 环境错误写成业务测试通过。

## 5. 威胁模型与失败姿态

整改前，operator 可以把名为 test 的评价结果当作候选质量依据，再根据结果反复调整研究
输入。Phase 3V 消除了当前 real-data v2 runner 内 test 直接参与 candidate selection 的路径；
train/valid 任一不通过时也不能用另一段结果生成 candidate。

仍无法由仓库代码排除：

- 研究者或仓库外工具直接读取 test 数据；
- v1 artifact 已经反复使用 test，或人工根据其结果调参；
- 同一研究假设跨 dataset/window 重复尝试造成 multiple-testing bias；
- execution summary 的 metadata 与底层数据不一致；当前契约验证身份声明，不重新计算外部 artifact；
- recommendation 下游忽视“holdout 未评估”限制；
- 最终 test 解封被重复执行、选择性披露或在错误 dataset seal 上运行。

因此该实现是选择路径 containment，不是统计有效性、最终 OOS 或资本授权证明。

## 6. 未关闭项与关闭标准

当前裁定：
`PARTIALLY REMEDIATED / TEST SEALED FROM CANDIDATE SELECTION / FINAL OOS & HISTORY AUDIT OPEN`。

关闭 `FS-004` 至少需要：

1. 只读盘点所有 v1/v2 candidate、recommendation、registry 与相关人工决策 lineage；
2. 标记曾以 test 作为 benchmark、缺少完整 lineage 或疑似重复适配的历史证据为不可放行；
3. 建立独立的一次性 holdout evaluator、actor/reason/access ledger 和 seal 精确匹配；
4. 在 holdout 前完成 purged/embargoed walk-forward 或等价多窗口稳定性证据；
5. 记录 proposal family、尝试次数和 multiple-testing correction；
6. 证明 production gate 会拒绝只有 development evidence、没有最终 OOS 的 candidate；
7. 由未参与研究选择的 reviewer 复核 protocol、artifact、统计方法与最终裁定。

在这些证据完成前，v2 recommendation 只能进入研究审查/观察，不能解释为 test PASS、
最终 OOS PASS、参数可应用或真实资金上线许可。完整设计与验收边界见
[`../../docs/task/fs_004_research_selection_holdout_sow_2026_08_25.md`](../../docs/task/fs_004_research_selection_holdout_sow_2026_08_25.md)。
