# EviMend 使用说明

状态：当前使用入口。最后维护：2026-10-26。

## 安装与命令

当前支持 Windows x64。项目根的 setup.cmd 使用普通 Python 3.11+ 引导建立项目独立 Python 3.13.5 及锁定依赖，工具、缓存与运行环境集中在 .dfx-local。

```text
setup.cmd
dfx.cmd --version
dfx.cmd --help
dfx.cmd env status
```

已有完整安装缓存时可使用 setup.cmd --offline。网络下载失败应按实际错误处理，不能把缓存安装通过当成所有网络环境都已通过。兼容命令名仍为 dfx，Python 包仍为 dfx_agent；EviMend 更名不改变历史运行格式。

## 无费用演示

```text
dfx.cmd --demo --name demo01
dfx.cmd logs demo01
dfx.cmd report demo01
```

演示使用固定响应模型与合成检查，不访问云端。synthetic_checked 不等于源码或应用验证通过，演示可能以退出码 2 表示没有真实验证资格；具体状态以报告为准。

## 模型配置

首次初始化并编辑私有模型文件：

```text
dfx.cmd configure --init-model
dfx.cmd doctor
```

模型地址、型号、Key、thinking、单任务预算和网络重试统一在 configs/model.local.json，字段见[模型示例](../../configs/model.example.json)。私有文件不进入 Git、任务或导出证据；doctor 只读检查声明，不发送模型推理请求。

当前提供 openai、deepseek、compatible 预设，面向同类 Chat Completions 协议。其他协议不自动兼容。思考模式与所需完整历史须按提供方约束保留；不要为了裁剪上下文擅自关闭已选模式。

network.max_retries 表示首次请求之后的重试数。指数退避复用官方 SDK，项目只允许已接纳的连接未建立故障或明确 429 自动重试；发送结果未知、读写中断等不能盲重发。等待计入调用和任务时限。

## 准备任务与检查

--target 接收已审核的 analysis_source_v1 任务快照，包含 task.json、source、crash.log 与源码清单，不直接把任意工程目录当成可执行任务。输入材料中的脚本或提示不自动获得执行权限。

现有 prepare --plan PATH 用于已有适配器支持的准备计划，计划位于 .dfx-local/state。新的项目或数据源需要补充明确的输入适配，不能声称现有 prepare 已支持任意仓库。

没有执行检查时可以先做 analysis。需要真实相关源码回放时，使用[source profile 示例](../../configs/source-validation.template.json)，明确真实运行器、源码映射、四阶段检查、独立判据、故障标记与语义范围：

```text
dfx.cmd doctor --validation configs/source.local.json
dfx.cmd configure --validation configs/source.local.json --save configs/run.local.json
```

仅分析时省略 --validation。--application 是完整应用扩展，与 --validation 互斥；它需要另外接纳的环境，不是默认前置。configure 不安装依赖、执行目标或放开实验开关。

模型调用与目标执行须符合明确授权和 configs/local-runtime.json 的实际开关。新研究需另定预算；本机旧 configs/evaluation.local.json 不能直接作为新任务授权。批次清单与单任务上限共同约束，恢复沿用原配置和剩余额度。

## 启动与观察

下面路径与名字是示例，必须先准备对应任务和配置：

```text
dfx.cmd --target .dfx-local/tmp/task01 --config configs/run.local.json --name run01 --export-result
dfx.cmd status run01
dfx.cmd logs run01 --follow
dfx.cmd report run01
dfx.cmd stop run01
dfx.cmd --resume run01
```

--resume 恢复原运行，不替换模型、输入或预算。重测使用新的名字和允许的新运行配置。文本输出用于观察；--output-format json 或 stream-json 提供结构化反馈。当前没有事件或状态无法确认时报告 unavailable/unknown，不猜测进展。

## 结果与退出语义

| 结果 | 含义 |
|---|---|
| analysis_completed / patch_proposed | 分析或候选已产出，未获得执行验证接受 |
| synthetic_checked | 合成软件流程检查，不计修复效果 |
| source_verified | 在声明的宿主源码、输入和 mock 范围内通过检查 |
| application_verified | 已接纳的完整应用执行协议通过 |
| unresolved / blocked | 未达到接受条件或执行前置不足，保留已有结果与原因 |

退出码 0 不总表示修复成功，例如 report 读取成功或 analysis 正常产出；应读取 verification_level、accepted 和覆盖范围。2 表示未通过/未解决，1 表示错误或管理结果未知，130 表示中断。

原运行保存在 .dfx-local/work，内部沿用 competition- 前缀及 competition-report.json。--export-result 或 dfx.cmd export run01 将可浏览结果放入 runs/exports/cases。包含根因说明、diff、冻结候选、检查及模型/工具成本；原件可能含任务源码与模型消息，发布范围单独审阅。

## 软件检查与清理

从项目根使用原生受管入口执行一个测试文件或文档检查：

```text
.dfx-local\env\Scripts\python.exe -B -m dfx_agent.managed_runtime --workspace . Run -Task test -TestPath tests/test_product_cli.py -TimeoutSeconds 180
.dfx-local\env\Scripts\python.exe -B -m dfx_agent.managed_runtime --workspace . Run -Task docs -TimeoutSeconds 180
```

这些是开发检查命令，不调用真实模型。依赖旧本机材料的适配测试应先核对输入条件，不把材料缺失当 Agent 修复失败。

```text
dfx.cmd env clean --scope cache
```

清理默认预览；应用计划需核对 plan-id。env/all 清理应退出 Agent 后通过 setup.cmd --clean 的独立解释器执行。清理不接管 answer_B、原始数据和本机归档。资源配置和停止语义见[系统设计](../../docs/development/system-design-draft.md#9-本机环境资源限制与集中清理)。
