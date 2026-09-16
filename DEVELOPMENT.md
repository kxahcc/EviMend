# 开发导览

更新：2026-09-16。这是我们内部设计与开发的阅读入口，独立于竞赛提交用的 [README.md](README.md)。

## 现在做到哪里了

目前处于方案设计与离线基础原型阶段。已经核查数据、整理相关方法、制定实施计划，并写出日志与源码证据整理工具。完整修复 Agent 尚未实现，没有真实模型修复、应用构建或设备回放成绩。

当前路线是：以完整 Agent 修复闭环为底座，增加运行约束引导的根因分析和验证反馈决策；Skills 承载可复用规则，微调后置。HarmonyOS 远程环境后续准备，目前优先完善设计，不开展应用或模型修复实验。

建议按以下顺序阅读：

1. 本文件：理解目录和已有代码。
2. [当前状态](docs/STATUS.md)：已完成、未完成和下一步。
3. [开发设计草案](docs/development/system-design-draft.md)：系统怎么工作，哪些机制还只是设计。
4. [模块契约与规则规格](docs/design-contracts.md)：对象、失败分支、四类规则及静态案例推演。
5. [完整实施计划](docs/project-work-plan.md)：后续开发、实验和交付的阶段安排。

## 提交材料和开发材料如何区分

| 文件 | 角色 | 维护方式 |
|---|---|---|
| `README.md` | 竞赛提交模板 | 保留现有模板，提交准备阶段按规范填写 |
| `docs/technical-design.md` | 正式技术设计提交模板 | 不用日常设计草稿覆盖 |
| `docs/test-report.md` | 正式测试报告模板 | 有实际结果后按规范整理 |
| `DEVELOPMENT.md` | 日常开发导览 | 目录、功能或工作阶段变化时更新 |
| `docs/development/system-design-draft.md` | 当前方案草案 | 可持续修改，设计不等于已实现 |
| `docs/STATUS.md` | 当前工作进度 | 每个有实质变化的工作批次更新 |
| `AGENTS.md` | 编码助手的仓库工作约定 | 固定上述边界与日常维护要求 |

原 `docs/technical-design-bak.md` 已改名并移动为 `docs/development/system-design-draft.md`。历史 session 中的旧路径保留为当时的记录，当前阅读链接已更新。

## 仓库结构

```text
.
├── README.md                         竞赛提交入口模板
├── DEVELOPMENT.md                    当前开发导览
├── AGENTS.md                         仓库工作约定
├── .gitignore                        Git 排除规则
├── pyproject.toml                    Python 项目元数据与 pytest 配置
├── docs/
│   ├── technical-design.md           正式技术设计模板
│   ├── test-report.md                正式测试报告模板
│   ├── STATUS.md                     当前进度
│   ├── project-work-plan.md          完整实施计划
│   ├── design-contracts.md           模块与规则规格
│   ├── development/
│   │   └── system-design-draft.md     开发设计草案
│   └── preparation/                  前期研究、数据核查、指标与环境分析
├── src/dfx_agent/                     离线证据整理原型
├── scripts/                          命令入口和准备工具
├── tests/                            合成输入的软件自检
├── skill/                            模板目录；尚无实现的项目 Skill
├── resources/preparation/            数据清单、研究来源、历史预算等 JSON
├── sessions/                         按工作批次记录的开发摘要
├── data_B/                           本地竞赛数据，不纳入 Git
└── runs/                             临时自检与运行产物，不纳入 Git
```

## src 下到底实现了什么

现在是普通 Python 工具代码，运行部分使用标准库，没有接入大模型、Agent 框架或 HarmonyOS SDK。流程是：

```text
人工核验的任务清单 + 选定源码/日志
    → prepare：生成分析快照和哈希清单
    → analyze：检查输入是否变化 → 解析日志 → 匹配源码 → 汇总证据
    → JSON 报告：栈位置、源码片段、歧义与未运行状态
```

| 文件 | 已实现的职责 | 当前边界 |
|---|---|---|
| [task.py](src/dfx_agent/task.py) | 读取指定日志或 ZIP 成员，筛选分析文件，生成并校验快照及哈希 | 分析快照不是完整可构建工程；不证明日志与源码运行版本一致 |
| [logs.py](src/dfx_agent/logs.py) | 处理常见日志编码，提取错误头、栈帧、源码坐标和解析告警 | 不解释业务因果关系 |
| [sources.py](src/dfx_agent/sources.py) | 建立文件索引，按路径等规则定位源码并提供邻近片段 | 文件名冲突保留歧义；没有 ArkTS 数据流分析 |
| [analysis.py](src/dfx_agent/analysis.py) | 串联校验、解析和映射，输出去重后的栈证据及耗时 | `root_cause`、`patch` 均为空；各层验证是 `not_run` |
| [runtime.py](src/dfx_agent/runtime.py) | 查询 PATH、Python 模块和相关环境变量是否存在 | 名字虽为 runtime，目前只是环境观察；不构建、安装或启动设备 |
| [cli.py](src/dfx_agent/cli.py) | 提供 `doctor`、`prepare`、`analyze` 命令及参数检查 | 没有 `repair` 命令；`prepare` 必须显式提供任务清单 |
| [__init__.py](src/dfx_agent/__init__.py) | Python 包入口文件 | 不启动后台任务 |
| [README.md](src/dfx_agent/README.md) | 原型使用说明及任务清单字段示例 | 示例路径不是已配置的真实任务 |

这部分代码的价值是为后续诊断提供固定、可追溯的输入，并显式保留未知情况。栈位置映射成功不能算根因定位成功。当前还没有真实案例任务清单，也没有运行真实应用求解。

## 其余文件分别做什么

### 工具、测试与占位文件

| 文件 | 用途 |
|---|---|
| [scripts/dfx.py](scripts/dfx.py) | 从项目根目录启动离线工具，加载 `src` 中的包 |
| [inspect_dfx_data.py](scripts/preparation/inspect_dfx_data.py) | 只读盘点 data_B 的应用、日志、归档和路径对应关系；输出数据清单 |
| [estimate_budget.py](scripts/preparation/estimate_budget.py) | 按给定假设估算实验成本；默认配置属于数据到位前的历史方案 |
| [scripts/preparation/README.md](scripts/preparation/README.md) | 两个准备工具的使用说明及历史配置边界 |
| [test_offline_evidence.py](tests/test_offline_evidence.py) | 用合成输入检查编码、路径歧义、文件过滤、输入变化和不误报成功；此前 10 项通过 |
| `pyproject.toml` | 声明 Python ≥ 3.11，并配置 pytest 的测试路径和源码路径；当前不是完整部署清单 |
| `src/.gitkeep`、`tests/README.txt`、`skill/README.txt`、`resources/README.txt` | 从模板保留的占位或目录说明文件 |

### 前期研究与数据记录

| 文件（位于 docs/preparation） | 用途 |
|---|---|
| [README.md](docs/preparation/README.md) | 前期路线总览；顶部标明当前入口与历史内容 |
| [prior-art.md](docs/preparation/prior-art.md) | 相关方法、可借鉴部分与创新边界 |
| [data-b-assessment-2026-09-15.md](docs/preparation/data-b-assessment-2026-09-15.md) | 实际竞赛数据、日志/源码/测试材料问题及路线调整 |
| [metrics-and-gates.md](docs/preparation/metrics-and-gates.md) | 指标定义、比较条件与阶段进入/退出要求 |
| [environment-readiness.md](docs/preparation/environment-readiness.md) | 环境准备核查记录；不是当前设备已可用的证明 |
| [pilot-plan.md](docs/preparation/pilot-plan.md) | 数据未知阶段的试点设想，不能直接作为当前实验执行单 |

| 文件（位于 resources/preparation） | 用途 |
|---|---|
| `data-b-inventory.json` | 数据只读盘点结果；不是正确补丁或根因标签 |
| `sources.json` | 前期研究的来源索引 |
| `public-case-candidates.json` | 公开案例候选元数据，尚非完成的运行基准 |
| `experiment-plan.json` | 数据到位前的实验配置记录 |
| `budget-estimate.json` | 基于历史配置和假设生成的成本估算，不是实际费用 |

### 开发记录

`sessions/template-session.json` 是竞赛提供的摘要格式参考。其他日期命名的 JSON 保存工作批次摘要：9 月 12 日前期研究、9 月 15 日数据核查和整体计划、9 月 16 日设计细化与仓库整理。它们是进度与决策记录，不是模型修复评测轨迹。

## 如何查看与自检

在项目根目录运行：

```powershell
python scripts/dfx.py --help
python scripts/dfx.py prepare --help
python scripts/dfx.py analyze --help
```

已安装 pytest 的开发环境可执行 `python -m pytest tests/test_offline_evidence.py -q`。该测试只构造临时合成数据，不调用模型、SDK 或设备。当前没有用于展示完整自动修复效果的一键命令。

## Git 与日常维护

本项目采用本地 Git 维护，当前整理批次建立初始基线。尚未配置远程或推送。首次提交只能记录整理后的现状，无法恢复此前未纳入 Git 的历史版本。

- 纳入源码、模板、开发文档、测试、准备工具、研究索引及开发摘要。
- `.gitignore` 排除 `data_B/`、`runs/`、Python 缓存、虚拟环境和 `.env` 文件。本地数据继续保留。
- 今后按完整的小改动提交，并同步更新当前状态；新增目录或改变文件职责时更新本导览。
- 修改方案写到开发材料；正式提交阶段再按模板汇总已经实现和验证的内容。
- 历史研究/实验设想保留日期和适用范围，避免把旧计划误当当前结论。

可用 `git status --short` 查看未提交变化，用 `git log --oneline -5` 查看最近提交。

## 下一步

继续审查诊断对象、控制器和四类规则的设计，再逐项实现并做合成自检。模型接入、真实故障复现、自动补丁、远程运行与实际成绩均是后续工作。以 [STATUS](docs/STATUS.md) 为最新进度入口。
