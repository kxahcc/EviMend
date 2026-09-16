# 离线预算规划

从仓库根目录运行：

    python scripts/preparation/estimate_budget.py

默认只估算首轮 E1：24 案例 × 3 配置，加最多 12 次瓶颈诊断参照，共 84 次完整模型求解。工具本身没有模型、网络或容器调用。

保存估算：

    python scripts/preparation/estimate_budget.py --output resources/preparation/budget-estimate.json

估算所有条件阶段：

    python scripts/preparation/estimate_budget.py --all

取得实测单次开销后替换假设：

    python scripts/preparation/estimate_budget.py --phase E1 --unit-cost 0.5 --minutes-per-run 15 --workers 2

上述 0.5 美元和 15 分钟是演示输入，不是实测值或当前模型报价。单次平均成本应包括诊断、探针、失败重试与回退。

程序同时给出成对二元结果的粗略样本量估算。它不处理项目聚类、重复种子或非劣性检验，不能直接当成正式论文功效分析。

完整方案见 [准备文档](../../docs/preparation/README.md)。
# 2026-09-15 数据盘点

从工作区根目录运行 `python scripts/preparation/inspect_dfx_data.py`，生成 `resources/preparation/data-b-inventory.json`。脚本只读取本地文件和 ZIP 内容，不解压、不执行数据中的代码、不联网，不修改原始 data_B。清单包含候选案例、日志头/栈位置、路径检查、编码和归档问题，不是修复评测或根因标签。

下面的预算工具对应数据未知时的历史配置；实际数据到位后的实验规模见 `docs/preparation/data-b-assessment-2026-09-15.md`。
