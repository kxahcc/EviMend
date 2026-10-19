# 数据准备工具

状态：当前工具说明。最后维护：2026-10-19。

`office_application_demo.py`是已退出本次提交范围的B3-017可选平台实验脚本。用户已取消完整应用验证并自行删除SDK环境和application-demo，当前不再使用或重建这些资源，不把SDK或手动IDE入口作为Agent运行前置。代码及历史测试在开发仓库保留原义；提交版按实际import/测试依赖筛选，不能照搬已失效的环境或说明。正式软件/证据组织见[实施计划](../../docs/project-work-plan.md#交付与修改范围)。

`report_evaluation.py` 用标准库读取 `runs/exports/evaluation-runs.json` 和各个有效终态导出的 `evidence.zip`，分别生成robustness与ablations的README、统计JSON/CSV及逐次RESULT.md，总统计为evaluation-summary.json。它只整理报告，不启动模型、检查或恢复任务。冻结SQLite从ZIP精确读取到项目临时目录并以只读immutable方式打开，不读取活动数据库；思考历史和Key不输出。统计分开保留缓存命中/未命中输入、输出、费用、源码验证和实际机制消费，核对比较的模型、知识、工具、输入及其他限额；Token上限有差异时明确保留，不能称为严格同额度通过率比较。命令：`python scripts/preparation/report_evaluation.py`，需已有本机有效导出；不依赖旧work或archive。

`inspect_dfx_data.py` 只读检查本地 data_B 的应用、日志、ZIP 内容和源码映射，输出 resources/preparation/data-b-inventory.json；不联网，不执行数据中的脚本，不修改应用源码，结果不是根因标签或修复成绩。现有全量清单采集于 2026-09-15；重新盘点前须先为该脚本接入受管 `Run` 任务，目前不使用直接 Python 命令绕过本机资源入口。

重新生成清单时同步核对 [数据报告](../../docs/preparation/data-b-assessment.md) 的统计和证据日期。当前运行准备与阶段条件见 [实施计划](../../docs/project-work-plan.md)。

`check_case_observations.py` 是未来单元/设备适配器的有限断言接收端，仅用 Python 标准库读取完整观察 JSON，与 [预登记预期](../../resources/preparation/case-oracles.json) 比较。资源在 2026-10-11 根据实际接口准备，仍为 `pending_host_and_environment_review`，不是新应用采集证据。运行和软件验收状态见 STATUS。

021 的4组输入固定小型交错 float32 小端 PCM 原字节、正采样率和声道数。独立预期按每秒首帧的各声道绝对值均值给出，覆盖整秒后不存在的帧、真实末帧、双声道及正负样本；用于拒绝空/全零波形、遗漏末帧、只读一声道和有符号平均等错误。2 Hz 是接口测试参数，不能代替真实音频转换或播放验证；空/残缺帧、非法参数和非有限样本尚无完整业务契约，不纳入通过范围。主机仍须审查这一采样规则是否符合产品意图。

022 的3组请求包含 ASCII、中文及补充字符，固定完整请求字段、属性顺序和 `JSON.stringify` 的紧凑原文。预期校验值按 UTF-16 code units 的 uint32 滚动规则独立固定，不能只比较候选 `create` 与 `verify` 的相互一致。观察还须包括原校验通过、错误校验值拒绝、实际付款字段改动后旧校验拒绝，以及 PRE_ORDER 的 HTTP 状态和订单新增数量；拒绝全局关闭验证、返回成功却没有订单等错误。路由状态在每次请求前重置，订单新增数量须从真实状态读取。这些是接口输入，商品/服务/时间未按真实预约接纳，不能直接代替场景里的有效订单；预约页面、支付及完整订单详情仍需另外接纳业务/回归判据。

观察文件的顶层字段必须恰为 `schema_version: 1`、`scope: "case_observations_only"`、与资源一致的 `oracle_id` 和 `observations`。每行恰含 `case_id`、`vector_id`、`input`、`observed`：input 是全部预登记输入的回执，observed 是实际适配器取得的完整输出，字段形状与对应 expected 一致。所有7组都要出现且恰一次；缺组、重复、未知组、输入不符、输出缺项或错误均失败。音频数值仅接受资源声明的绝对容差，订单布尔/状态/计数和校验字符串严格比较。结果只给组 ID 与失败类型，不打印预期答案或输入。测试里的手工观察只是接收端合成 fixture。

未来受管适配器可用绝对项目 Python 可执行文件接收实际工具输出，参数形状为：`<managed-python.exe> <absolute-check_case_observations.py> --observations <actual-observations.json> --oracles <evaluator-case-oracles.json>`。这不是当前已开放的独立 Run 命令；测试须经现有 `Run -Task test -TestPath tests/test_case_observations.py` 入口执行，实际结果见 STATUS，不直接绕过管理器。现有 local_execution 不直接启动 `.cmd`、`.bat` 或 `.ps1`，未来 build/target/behavior/regression 可由受管 Python 绝对入口包装真实工具执行并消费观察；具体构建、设备、进程/设备停止和结果来源绑定仍待接纳，本工具没有伪造 xdevice 成功包装器。

退出码0只表示完整观察符合有限预期，退出码1表示不符或输入无效。结果始终 `scope=case_observations_only`、`accepted=false`、`application_verification=not_run`；不认证观察来源、候选版本、设备或实际输入是否送达。未来适配器必须执行真实候选接口并绑定工程/候选/工具链和原始输出，不能解析或翻译 ArkTS、从输入计算 observed 或把手工 JSON 当应用证据。仅拥有 oracle_sha256 或通过此接收端不能接纳完整应用修复。

这些资源、预期与测试仅供 evaluator，保持在求解源码快照和模型上下文之外；不要把完整资源当错误反馈回送 Agent，也不修改原始 data_B。实际源码/日志可以按既有任务准入进入求解器，预登记答案、拒绝示例和构造报告不进入。
