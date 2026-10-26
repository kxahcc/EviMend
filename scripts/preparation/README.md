# 数据与执行适配辅助工具

状态：既有适配工具说明。最后维护：2026-10-26。

本目录保留已有数据解释、观察检查、结果统计和平台试验辅助脚本。它们不是通用仓库修复 API，不会因 EviMend 更名自动支持新的数据域。

| 工具 | 作用及范围 |
|---|---|
| inspect_dfx_data.py | 只读盘点已有数据布局，调用者应为新盘点指定本机输出路径 |
| check_case_observations.py | 根据仍被测试使用的 case-oracles.json 核对观察输入 |
| report_evaluation.py | 读取已有结果结构，生成对应统计；新研究指标须另行接纳 |
| report_material_gaps.py | 检查既有数据适配中的缺失输入，不将缺材料当代码不可修复 |
| office_application_demo.py | 可选平台实验的兼容辅助工具；不作为默认部署依赖，也不授予安装或设备操作权限 |

旧数据完整盘点与阶段材料由 dfx-agent-v1.0 保存。当前运行不依赖本机 archive，也不读取 answer_B 作为代码来源。仍被源码、质量检查或回归测试引用的工具保留，避免仅为清理名称而破坏接口。

新的任务适配先明确源码版本、触发输入、独立判据、环境和正常行为。研究路线见[实施计划](../../docs/project-work-plan.md)，使用方式见[Agent 说明](../../src/dfx_agent/README.md)。
