# 当前状态

状态：当前进度。最后维护：2026-10-18。
职责：唯一维护已完成工作、当前任务、阻塞及下一步；项目蓝图与目录见 [开发导览](../DEVELOPMENT.md)，阶段定义见 [实施计划](project-work-plan.md)。

## 当前阶段

当前产品是Windows x64本机的独立dfx CLI，复用Deep Agents/LangGraph和官方模型SDK。输入审核源码与日志，自动定位、修改、调用已接纳检查并按反馈修订，输出补丁、冻结源码、根因和用量记录；不要求HarmonyOS SDK、签名、设备或模拟器。云端deepseek-flash保持thinking enabled与完整推理历史。

**第一部分26个有效案例均已有最终源码验证通过的结果。** 每例实际复现原版指定故障，冻结候选通过build、target、behavior和regression；完整应用验证仍为0。B1/B2/B3分别10/6/10例，缺材料和异常输入不混入分母。本机结果入口为`runs/exports/cases/README.md`，统一按每例最后一次成功结果展示，不按验证时点拆分成绩。原报告、候选和检查原件保留，缺标签的根因准确率保持未知；这不是官方评分或全应用稳定性结论。

当前选定26次运行共381次模型调用，缓存命中输入7222016、未命中输入486469、输出182444 Token，估算合计2.72137064元；平均Agent耗时120503.422ms。统计只含最终选定运行，不能解释为全部开发实扣；安全预算仍保留所有实际调用与未知占用。费用、缓存和阶段时间可查主入口的index.json与case-results.csv，逐例RESULT.md关联补丁、源码、根因和验证范围。

本轮新增8例真实闭环：B2-005、B3-014/015/019/020/021/024和B2-003。新闻B2-002的live1017-retry18早已通过四项检查，但旧停止分类误报未完成；该软件问题已独立修正。本轮恢复其冻结导出，重新执行同一原版和候选的完整源码协议通过，没有新增模型调用，也没有改写旧报告。主视图采用此次绑定检查支持的最终源码结论，旧终态事实仍可查。该补丁的作者缺失分支、正常作者、载荷和加载状态在声明范围内通过，不认证真实ArkUI/SpeechKit或网络。

此前25份导出已逐一核验清单，并在独立原版副本上由Git实际应用补丁，结果与冻结候选字节完全一致，25项通过（81.14秒）。最后新闻补丁的应用与清单核验1项通过（7.37秒），结果见本批session。补充源码复验1项通过（54.25秒）；这些交付检查不新增修复成绩。

live1017-retry20在第二次模型请求发生传输失败，1次已知、1次未知，没有候选，未重发。其原账本和终态完整保存；请求级保守占用重核为0.2932604元，用户已明确接纳该run按已知费用加原请求预留继续后续测试。其他25例没有因此重复求解；最后新闻案例通过复用已知成功候选完成交付。既有三项未知run例外不自动扩展；金额边界唯一见评测口径。

当前进入第二部分。解析矩阵23种输入、超大日志拒绝及耗时导出共25项通过（0.10秒），支持UTF-8/BOM、UTF-16、GB18030，覆盖空/噪声/乱码/伪BOM、native/框架/域外栈及头部异常；修正HybridStack的数字#N at前缀，保留真实源码锚点。它是解析软件证据，不是25例模型OOD成功；本机产物为`runs/exports/robustness/parser-matrix.json`。缺材料盘点仍单列。

模型OOD和两例三组的有限消融已在付费前冻结输入、配置和运行源码哈希，接纳/组间配置一致性1项通过（6.77秒）；具体组别、预算、顺序和预期唯一见评测口径。原26例实际观察到4例Skill读取、8次规则查阅，未观察到保存义务或决策、未启用working_context；不能把这些未消费机制写成已测收益。正式提交模板、精简session和提交版Git仍等待全部测试及用户具体要求。

逐例源码协议的实现与反例记录见既有sessions：001/009、025/026、日期/资产、账单/阅读/快递/笔记、七例接纳及最后六例接纳。它们复用LocalApplicationExecutor和CheckLedger，披露类型剥离、有限UI降级和平台mock；不是Agent自主生成陌生工程验证器，也不认证声明范围外行为。用户无需为每例手写配置，现有prepare/configure生成已接纳参数。

本轮复用项目Python、锁定依赖与已有普通Node，未安装依赖、SDK、模拟器或权重，未改全局环境或放宽本机资源限制。失败/调试导出归档，原work与安全费用记录保留，当前结果不依赖archive。

## 已完成与证据

2026-10-15 无硬件主线修正：默认 configure 不再要求 application，新增互斥的 --validation；analysis 无执行配置仍能经完整 SDK 工具循环形成诊断和未验证补丁。source 模式复用原执行器，实际执行原版/候选的固定四阶段检查，scope/mocks 进入启动请求、冻结配置、反馈、上下文和 schema 4 报告，应用状态保持 not_run。九组163项通过、1项符号链接权限跳过；包含普通 Node 实际加载 JS 字节并拒绝仅取固定索引的错误修复，以及三条默认 frontier 上下文整链的交付/零新增调用恢复；其中12次模拟调用覆盖实际业务失败反馈进入下次请求、保留原失败、第二候选通过后清除失败。旧执行器首次59通过/1失败/1跳过是临时回执替换 WinError 5，未改代码/权限/限额的原样复验60通过/1跳过，精确原因未知。新增反馈测试两次因旧测试辅助函数读取引用组时索取内嵌 messages 而进入模型结果未知；改为检查实际发送的完整工具消息后，三项整链通过，未改生产上下文或接受标准。Ruff首次5项导入格式错误已修正并通过，Pyright零错误/警告，文档14份零错误。完整回执见 [本批记录](../sessions/2026-10-15-light-source-execution.json)。本轮未安装依赖、改全局环境/限额/实验开关、运行真实模型或 data_B 修复；普通 JS/Python 软件夹具不证明 ArkTS 语义或方法效果。

2026-10-15 实测前系统复核：沿配置/启动、任务输入、官方SDK与工具、上下文/Skill、诊断/候选、检查、报告及恢复检查实际接线，修复五类缺陷。九组319项通过、1项Windows符号链接权限用例跳过；完整新SDK合成路径同时覆盖15次调用的两候选修订，以及14次调用后预算耗尽但检查完成、恢复零新增调用。应用通过的终态判断使用主机回执fixture，实际子进程均为synthetic，不是HarmonyOS效果。CLI首次47通过/1失败是新断言遗漏模型默认max_bytes字段，按规范化对象修正后48通过；没有放宽接受条件。Ruff/Pyright通过。范围、逐组回执、复核前17项基线及能力边界见 [本轮记录](../sessions/2026-10-15-pretest-system-audit.json)。未安装依赖、改全局环境/资源限额/实验开关、调用真实模型或修改原始数据、提交模板。

2026-10-14 安全网络重试交付：私有文件与模板加入 network 的重试数、请求总超时及连接超时，正式 CLI 生成的模型身份与参数实际冻结该策略；原 Key/预算和实验开关未改。SDK 负责退避及 Retry-After，asyncio 截止取消等待和读取；超长等待不提前重试，结果未知不重发，成功消息/账本保留尝试统计，失败沿现有侧录记录计数。六组171项软件测试通过、1项符号链接权限用例跳过：43项网络反例、16项私有解析、48项CLI、52项旧云端回归、11项侧录及完整新SDK合成修复/交付/恢复。Ruff/Pyright通过；具体回执和首次错误分类、导入/异常边界及决策排序断言失败见 [本批记录](../sessions/2026-10-14-cloud-retry-closure.json)。整链测试改为核最终候选实际绑定的失败修订决策，不把按哈希排序的记录当时间顺序；没有改变业务接受门。真实提供方效果、计费和HarmonyOS运行仍未验证，未安装依赖或扩建调度/会话系统。

2026-10-14 私有模型配置交付：默认 configs/model.local.json 统一提供方/base URL/model/Key/模式和六项单任务额度；configs/*.local.json 被 Git 忽略，本机已通过正式 CLI 创建无 Key 的待填写文件，doctor 明确报告模型与 Key 未填。configure 从标准示例初始化、默认按私有文件生成不含 Key 的运行配置；环境模式继续兼容。文件 Key 不注入环境、错误不回显，地址变化拒绝发送，恢复保留原模型/预算但允许同服务 Key 轮换。四组132项软件测试通过，覆盖私有字段、全部预算落入实际配置、官方 SDK 文件凭据、两候选修订、交付包不含 Key 和完成恢复零新增调用；Ruff/Pyright通过。使用与提交复现步骤已落在原使用说明，具体回执和首次失败见 [本批记录](../sessions/2026-10-14-private-model-config.json)。没有新增依赖、改变全局环境/资源或实验开关，真实模型与应用运行仍为0；单任务预算不冒充全数据集或金额上限。

2026-10-14 标准 Skill 库交付：根目录 skill/ 是唯一作者及竞赛提交入口，四份 SKILL.md 保留原254项指导内容；旧 JSON 移至忽略的本地归档，不再被运行入口或当前文档依赖。新增标准包动态发现，正文/资料按需读取，metadata.dfx-load=always 明确注入完整正文；默认四份仍按需。资源、脚本读取及强加载与完整正文工具使用分开统计，保留预算和未知结果边界。七组软件测试173项通过，包含新包、四类原文、SDK/Mock实际输入、竞赛两候选反馈修订、旧报告聚合和导出；Ruff/Pyright与14份主文档检查通过。具体回执、首次失败、格式修正和真实能力边界见 [本批记录](../sessions/2026-10-14-standard-skill-layout.json)。未安装依赖、改全局环境/限额、启动真实模型或应用，也未改提交模板；本文保留原历史日期和事实。

2026-10-13 首例检查与生产 Skills 接纳软件验收：接收端 --case 精确选择案例全部向量，默认联合协议不变；四份生产 Skill 各自经现有批准与原生 SDK 完整进入后续模型请求，其余未读资源不计使用，完成恢复零新增调用。三组65项软件测试通过，含完整 competition 两候选反馈修订；Ruff/Pyright通过。没有新增依赖、复制 oracle 或安装第三方 Skill 包，应用和 Skill 效果仍未知。[本批记录](../sessions/2026-10-13-pretest-case-skills.json) 保留具体证据及首次导入格式失败。

2026-10-13 实测前日志解码收口：Agent 读取、新日志引用登记、独立上下文和冻结材料共用解码规则；兼容 UTF-16/BOM、UTF-8/BOM、GB18030，损坏替换明确返回告警。原字节/哈希和行段限制保持，旧保存陈述按原解码精确只读回核，不回填新文本。五组183项软件测试通过，含编码日志经实际 SDK 的登记、完成回读和恢复零新增调用；Ruff/Pyright通过。原 data_B、资源限制、环境依赖与提交模板未改。[本批记录](../sessions/2026-10-13-log-decoding-closure.json) 记录具体回执；没有真实模型或 HarmonyOS 运行。

2026-10-13 核心 harness 复核与有限修补：跨诊断移出探索可按原版/冻结候选导航真实回读；必要工具正文不再额外复制到面板，完整组和旧 inline 计划保持；当前决策驱动新面板与有界待查对象，旧决策不覆盖新诊断/候选及实际失败。竞赛演示、云端模板和 configure 已选择新布局。六组软件反例209项通过，覆盖当前/旧布局的实际 SDK/wire、可选摘要不误授结果可见性、两候选反馈修订及完成恢复；Ruff、Pyright 和文档检查通过。首次计数断言和 Ruff 导入分组失败保留并修正，不放宽业务门。[本批记录](../sessions/2026-10-13-harness-core-review.json) 保存复核范围、实际回执及未启用能力。不运行真实模型/应用，不修改提交模板或资源限额。

2026-10-12 完成官方模型 SDK/项目准备与独立 CLI 两批交付。九组最近验收358项通过、2项权限跳过；实际 native1012 演示经官方 SDK 模拟传输产生两个候选，首版 behavior 拒绝后修订，第二版四项合成检查通过；15次模拟调用、225合成 Token，仍为 synthetic_checked/accepted=false/application_verification=not_run。完成恢复的报告和模型账本字节不变；native-stop1012 经正式 stop 确认 canceled/stop_requested、无活动标记。系统目录 PATH 下离线安装和演示/恢复成功；普通外部 Python 的 env/all 清理在合成部署树验收。没有清理旧失败 tmp、改 ACL/注册表/全局环境或实验限额。[交付记录](../sessions/2026-10-12-native-cli-delivery.json) 保留首次启动、测试拦截、长路径与临时树回收失败，不只保留成功结果。此前19组534/3的 [历史验收](../sessions/2026-10-11-product-core-acceptance.json) 是 SDK/后端变更前的证据，不作为当前重复全仓通过。

2026-10-11 受管安装及工具/上下文验收完成，[本批记录](../sessions/2026-10-11-tool-context-acceptance.json) 保存实际回执、首次失败及修复范围。此前 [工具源码准备](../sessions/2026-10-11-repair-tool-closure.json) 与 [接线源码准备](../sessions/2026-10-11-core-wire-closure.json) 保留当时未运行的事实；本轮新增通过不追改历史。

整链接线收口同步云端准备模板与演示的 frontier/decision_layers，明确模板的非思考模式与提供方核对义务，修正整链旧断言和使用说明的报告代际。共享任务策略说明候选回读与上下文/过程使用；实际 SDK/Mock 请求用例新增三态指导在分层布局下的完整交接、冻结候选读取、原业务失败与完整 Skill 留存、终态建议边界及完成恢复零新增请求断言。实际 SDK 决策布局/冻结候选读取、完整 competition 及三态提供方接线用例已运行通过；人工复核区分了完整旧经验可读与基线 review 不能改称新候选 review。[本批记录](../sessions/2026-10-11-core-wire-closure.json) 保留源码准备事实，当前运行证据见本批验收记录。

此前产品前端验收包括直接任务、configure/doctor、status/logs/stop/report/env 的42项用户入口用例；本轮私有配置的最新验收见上方记录。Python 管理器和 Windows Job 的27项测试涵盖挂起启动、子孙停止、日志限额、未知活动/归属/计划漂移、普通外部 Python 清理和深层路径证据保护；安装器6项通过。受管 pytest 使用独立子进程；管理器通过 Windows 扩展路径读取/盘点/清理，规范 owner 和指纹不变。生产无需 PowerShell；历史研究/Export PS 保留兼容，不作为使用 Agent 的前置。独立 wheel/跨平台、无缓存冷安装和真实应用接纳未在本轮认证。

| 内容 | 实际状态 | 依据 |
|---|---|---|
| 软件包 B：代表案例检查准备 | 021/022 共7组固定接口输入与独立预期，观察接收端要求全部且恰一次、完整字段与有限容差；反例拒绝空波形、漏末帧、错声道、常量/截断校验、放过篡改及成功却没有订单。31项软件测试通过；只认证有限比较，accepted=false、application_verification=not_run。采样/UTF-16契约、有效预约、真实候选执行来源与完整业务仍待接纳 | [本批记录](../sessions/2026-10-11-representative-check-preparation.json)、[工具说明](../scripts/preparation/README.md)、resources/preparation/case-oracles.json |
| 软件包 A：决策上下文与探索/失败记忆 | repair_context_layout 从已有任务语义记录构建阶段问题、分槽记录、原关系和回读入口，替换平铺面板；完整字段回组须精确一致，不把建议或历史反例升级为事实/权限。repair_frontier 保留必要日志、有界去重探索及跨诊断最近失败，原登记/协议/级别保持；兼容实际 pass 才清除。旧策略及布局保持原形状。已写阶段/未知/草稿、内容丢失拒绝、探索/失败反例及实际 SDK/wire/完成恢复 fixture；交叉审查修正投影预算用例及早期日志页遗漏。布局18项、筛选61项、SDK交接15项及完整 demo/wire 受管通过。该范围有实现与运行证据，不推定效果贡献成立 | [本批实现记录](../sessions/2026-10-11-context-frontier.json)、repair_context_layout.py、working_context_plan.py、tests/test_repair_context_layout.py、tests/test_working_context_handoff.py |
| 软件包 B：候选提供方协议准备 | 已写 thinking_mode/effort 参数、合法 nullable/aborted 与截断处理；多轮 reasoning、工具结果、无工具回答后下一轮、已保存响应回放及裁剪/摘要先拒绝用例已补。实际思考历史不允许主动裁剪，协议拒绝不隐式切模式；旧 profile/历史消息保留原义。Mock提供方127项、云端适配40项、调用账本21项、SDK多轮17项、三态接线10项通过；CLI真实入口和代表案例接收软件验收完成。真实提供方仍须小额接入核验 | [协议准备记录](../sessions/2026-10-11-provider-mode-preparation.json)、model_provider.py、models.py、model_calls.py |
| 软件包 A：有限工作集与最终根因 | 新 repair_relevance 主动选择已有引用关联的历史，保留业务义务、相关实际失败、未冻结编辑、完整知识读取及近期工具组；默认 legacy 序列化/重建保持原义。competition schema 3 将所选/备选分开，最终定位绑定最新候选，不用备选或未关联诊断补足。新增反例及 demo 接线已写，交叉审查修正无可选反馈的失败遗漏和序列化 exclude 缺陷；上下文筛选、最终根因缺项反例及完整 competition 的20项测试通过；实际产品演示和完成恢复也通过，A/B软件门槛已闭合 | [本批代码与阻塞记录](../sessions/2026-10-10-finite-context-localization.json)、working_context_plan.py、competition.py |
| 上下文核心设计与提交路线 | 规划时核对现有选择器及锁定 SDK 原生摘要，确定沿既有 ID 派生关系视图、必要状态/反例保留、可选证据排序和原文回读。按用户时间目标限定两个软件包、真实接入/强基线/有界筛选/冻结交付；修正旧探针比较及首轮大矩阵。规划记录本身没有新增程序或真实成绩，后续源码状态见本表软件包条目 | [上下文与路线记录](../sessions/2026-10-10-context-submission-plan.json)、设计第 7.1 节、实施计划、评测口径 |
| 实现思路与竞赛范围复核 | 对 5682677 的业务节点、SDK 实际复用、来源依赖和延期扩展只读复核；确认自研范围偏大，冻结扩展而不重写主链。当时发现最终定位完整性缺口，后续源码修正见软件包 A；决策为建议，无诊断运行探针；oracle 身份不认证业务充分性。该复核没有新增软件通过或效果证据 | [范围复核记录](../sessions/2026-10-10-competition-scope-review.json)、本页当前阶段及下一步 |
| 审查收口：受管启动与进程保护 | 受管进程套件 1 项通过，包含完整凭据发布延迟、发布失败、恢复边界异常、恢复前 Stop 及原有 CPU/内存/日志/超时/子树停止/管理器退出回归。凭据原子发布后才 Resume，失败关闭 Job/线程句柄和本次标记；原 Start API 保持兼容。复用前批 Ruff/Pyright 的相同源码检查，最终文档另行核验；本机限额未放宽 | [启动收口](../sessions/2026-10-08-review-closure-startup.json)、scripts/local.ps1、scripts/local_job.cs、tests/test_local_process.py |
| 审查收口：完成恢复与实际报告 | 恢复 15 项、竞赛 14 项、本机执行 38 项通过/1 权限跳过；覆盖调用/Token 满额度完成恢复、未完成预算/截止时间不重置、应用事实/接受分离、源码后验失败阶段保留、旧 Stop 与旧计时未知、相同终态计时不变和新进展刷新。独立只读复核未发现这三项剩余阻断；首次已有候选目录重命名失败保留，原样复验通过，原因未知 | [恢复与报告收口](../sessions/2026-10-08-review-closure-recovery-report.json) |
| 整体代码与能力声明审查 | 对 ca172f5 的竞赛主链、机制接入和两批证据只读审查，当时发现 1 项 P1、3 项 P2；后续修复依据见本页收口记录。所核既有测试去重为 89 项通过、1 项权限跳过，并非全仓测试。上下文实际发送及单份静态 Skill 消费有演示证据；普通检索、三态迁移和效益不能由该演示推出 | [整体审查记录](../sessions/2026-10-08-overall-system-review.json)、本页整体审查与就绪判断 |
| 竞赛第一批：模型与本机检查通道 | 云端 36 项、本机 16 项通过/1 个符号链接权限跳过，SDK 原闭环 12 项及因果诊断 21 项回归通过；正常、格式错误、预算先拒绝、未知不重发、工程映射、阶段失败、超时及进程树停止已覆盖。Ruff 通过，Pyright 0 错误/0 警告，文档检查通过。初次 Job 身份、长路径、换行 fixture 和质量检查失败均保留；未放宽限额，未调用真实模型/应用 | [第一批记录](../sessions/2026-10-08-competition-channels.json) |
| 竞赛第二批：统一产品与完整软件验收 | 4 项 SDK 整链测试通过：首修消除故障但业务失败后修订、完成恢复零新增调用、中途已保存响应回放续跑、预算不足发送前拒绝且首修指标未知。四份正式 Skill 走原生 middleware/read_skill，工作上下文、义务与失败决策接到同一循环；实际 CLI 演示及恢复通过，15 次脚本模型调用、2 个候选、14 次工具动作，原版复现与候选四阶段回执保存。delivery.zip 字节核验通过，Ruff 通过、Pyright 0 错误/0 警告，14 份主文档检查 0 错误；无应用接受或性能结论 | [第二批记录](../sessions/2026-10-08-competition-product.json) |
| 数据与材料核查 | 2026-09-15 完成全量只读盘点；2026-09-28 复核 021/022 两个待接入候选的原日志字节、源码位置与场景脚本。完整运行版本、原故障复现和独立业务 oracle 仍未知；未生成正式任务清单 | [数据报告](preparation/data-b-assessment.md)、resources/preparation/data-b-inventory.json |
| 项目蓝图 | 已说明产品、任务路径、核心机制、技术栈和使用入口；软件边界可逐项审查，真实语义和效果待验证 | DEVELOPMENT.md、开发设计第 1.1 节 |
| 技术选型 | Deep Agents SDK（底层 LangGraph）/Pydantic/SQLite 已接入固定响应路径；HTTPX/FastAPI/Uvicorn 为已完成的模拟协议原型，不再是本机应用执行的必需服务。云端请求和完整工程执行代码已接入统一竞赛入口，提供方与本机设备兼容未实测，应用修复未验证 | 开发设计第 1.1、6 节；uv.lock；W03/W05/W06 自检 |
| 方法设计 | 修复义务贯穿定位、补丁与验证的主线、同知识对照及近邻边界已明确。工作上下文、普通检索和三态交接已接实际 SDK 输入；完整指导与原件保存，引用依据按实际已见材料核验。静态会话与受管配置已有入口；开发反馈固定完整来源并接纳人工待审核经验。不可改写版本、事前验证和完整合成回执已有接口；独立演进/沙盒发布已有合成验收，独立模拟提炼草案已接入。批准版本消费入口已接程序 API，模型来源的四角色验证与独立资格判定通过；模型激活仍有累计读取容量失败，部署/恢复/导出未验收，真实晋升门槛待定。合成核验不证明语义适用、迁移收益或论文创新 | [设计第 7、8 节](development/system-design-draft.md#7-skills-的具体位置)、[模块规格第 3.1 节](design-contracts.md#31-工作上下文经验与-skill-的实施契约) |
| 本机资源与交接设计 | 已定义集中目录、资源限制、停止/清理协议及 W01–W10 实现包；Job 限制对合成 Run、Init、pytest、质量检查、离线 Agent、competition、聚合及 Export 生效，真实模型/应用实验仍禁用 | 开发设计第 9 节、模块规格第 5 节、实施计划 |
| W01 本机管理 | Prepare/Status/Clean 已实现；清理默认预览并校验 owner、目标指纹、活动标记和链接；原批 37 条隔离合成断言通过，后续证据清理保护验收见 W10 | scripts/local.ps1、scripts/test_local_runtime.ps1、sessions/2026-09-23-W01-local-cleanup.json |
| W02 进程保护 | Run/Stop 已实现固定合成场景；Job 的 CPU/提交内存/进程数设置后读回，子进程挂起加入后运行；输出管道按字节上限收集，监测时间、内存预留和磁盘。25 条隔离断言通过 | scripts/local_job.cs、scripts/local_synthetic.ps1、scripts/test_local_process.ps1、sessions/2026-09-23-W02-local-processes.json |
| W03 受管依赖与 SDK 切片 | 项目专属 uv 0.12.9、CPython 3.13.5、uv.lock 已就位；Deep Agents 0.7.18 固定响应脚本验证编辑、候选登记、失败后修订、120 Token 记录、禁用委派与 SQLite 恢复。受管 Init 重复执行 10 项断言通过，SDK 合成测试 1 项通过；不代表真实模型或应用修复成功 | configs/bootstrap.json、uv.lock、tests/test_sdk_compat.py、scripts/test_local_bootstrap.ps1、sessions/2026-09-23-W03-managed-sdk.json |
| W04 首批领域对象与候选工作区 | 严格 Pydantic 对象及证据来源/行号/版本/引用检查；从已校验分析快照独立复制草稿，失败编辑原子回退，登记完整 diff、候选源码与哈希，检查前复核。合成测试 9 通过、1 项符号链接测试因当前账户权限跳过；硬链接隔离测试通过。尚非可构建运行工程 | src/dfx_agent/contracts.py、workspace.py；tests/test_contracts.py、test_workspace.py；sessions/2026-09-23-W04-contracts-workspace.json |
| W05 固定响应离线闭环 | Deep Agents 哈希校验日志读取、通用源码浏览/编辑工具、未确认诊断、候选登记门、合成检查反馈、调用/已知 Token/时间/候选预算与停止报告已接入；固定响应模型调用也通过独立 SQLite 账本，Graph 检查点前完成可回放，已发出未保存时保留未知且不重发。当前 12 项受管合成测试通过；受管 CLI repair/resume 冒烟为 9 月 29 日记录。仅合成文本判据，无应用成功 | src/dfx_agent/agent.py、models.py、model_calls.py、verification.py、cli.py；tests/test_agent_loop.py；.dfx-local/artifacts 中相应记录 |
| W05 任务策略与跨文件检索 | 显式 system_prompt 和主机任务上下文已接入 SDK；基础策略覆盖上游原始行定位、保行为补丁、检查和有依据的反馈修订，增强只按开关追加。search_source 可在核验原版/冻结候选上做分页字面检索，带版本/坐标/编码缺项，共用授权和总预算。57 项检索、19 项策略、9 项 SDK 交接测试通过，检索符号链接用例 1 项权限跳过；脚本跨文件定位→失败→再修订已打通。该批 session 8 冻结项目提示、初始上下文、工具定义和检索限额；尚无真实模型自主修复证据 | src/dfx_agent/task_strategy.py、source_search.py、agent.py；tests/test_task_strategy.py、test_source_search.py、test_task_handoff.py；sessions/2026-10-01-task-strategy-search.json |
| W05 模拟模型与结果交接 | 可配置 MockTransport 接入真实 SDK；唯一规范化发送体绑定消息/工具/参数与 profile，完整响应/原始 usage 保存，未知结果不重发，混合错误工具调用全批停止。该批主机 report 5 核验编辑前初次定位、最新候选/diff/check、决策/条件及调用/动作账本，标明不可回读诊断范围；该批 session 8 冻结 model_profile。受管 PowerShell repair/resume 模拟冒烟通过，正常结束后恢复新增请求为 0；无真实云端兼容或应用效果证据 | model_provider.py、models.py、reporting.py、offline_script.py、cli.py；新增测试；sessions/2026-10-02-model-boundary-report.json |
| W05 有限上下文与主机压缩 | 关闭 SDK 隐式摘要和消息/结果卸载；默认有限上下文，可选按完整工具组压缩模型发送视图。来源、摘要和实际 solver 请求独立冻结，所有用途共用预算；solver 序号与总量分开，恢复核对 Graph/账本。摘要先记费后验格式，unknown 不重发，已完成可在 Graph 前崩溃后回放；摘要不能授权工具、改写检查或写原版。54 项模块、13 项真实 SDK 合成交接及原上下文 24 项回归通过，包含末额度、无收益、产物篡改与 reserve 后链接失败；该批 session 8、report 5/completion 2。首版输入有限，摘要忠实度/真实效果未知 | context_compaction.py、models.py、agent.py、reporting.py、offline_script.py；tests/test_context_compaction.py、test_compaction_loop.py、test_context_boundary.py；sessions/2026-10-02-context-compaction.json |
| W05/W09 执行来源冻结 | 新 session 9/report 6/completion 3 保存受限源码原字节、项目/锁/资源配置、实际 Python/distribution 元数据及 Git 可用/未知观察；兼容摘要不含 Git，续跑保留初始记录。源码/锁/环境、旧会话或破损准入事实在可写账本/调用前拒绝；CLI 初始化拒绝也保留旧终态。旧 8/5/2 只读保持原摘要，新旧混合保留来源未知覆盖，已知兼容漂移拒绝。实际新运行→零新调用续跑→两格聚合→新包 Verify 通过，旧包摘要不变；Git 采集不可用，诚实保留 null。来源哈希/版本不是完整主机代码备份或二进制认证 | provenance.py、agent.py、reporting.py、metrics.py、cli.py、scripts/dfx.py、local_agent.ps1；四个 test_provenance 文件；[本批记录](../sessions/2026-10-03-execution-provenance.json) |
| W09 只读离线聚合 | 显式 task/variant/repeat 完整矩阵、配置/提示/工具重建核验、只读 SQLite 和源树前后检查已接入受管 aggregate；保存初始/最新定位、全部候选、首次/最终合成检查和完整成本。未运行/缺报告保留分母及未知费用，成对 repeat 按任务/声明谱系列出，不宣称独立性；应用指标与缺失历史版本保持 null。原批聚合、只读账本与入口合成验收及实际受管两格冒烟通过；后续导出见 W10 | metrics.py、evidence_io.py、local_metrics.ps1；tests/test_metrics.py、test_readonly_evidence.py、test_metrics_cli.py；sessions/2026-10-02-W09-offline-aggregation.json |
| W10 本机私有证据导出 | 受管 Export 的预览、冻结计划、复核复制、新目录发布及 Verify 已接入；包镜像原 task/work 路径，保留全部失败与未知项，从包内重聚合全文一致。Windows 长路径和 SQLite URI、实际工作进程祖先及新实例回读旧包已做合成验证；Graph 只做字节及数据库完整性检查，不执行。写入前复核清单/汇总大小与哈希，输入增长拒绝；源输入保留，导出与环境共用磁盘预算，终态 work、任务快照和未发布暂存仍需明确丢弃才清理。导出模块 64、入口 40、清理保护 1 项测试通过，实际受管预览/Apply/Verify 冒烟通过 | exporting.py、local_export.ps1、local.ps1；tests/test_exporting.py、test_export_cli.py、test_cleanup.py；[本批记录](../sessions/2026-10-02-W10-private-export.json) |
| W06 HTTP 模拟作业协议 | 版本/协议绑定请求、HTTPX 客户端意图、SQLite 唯一幂等键、查询、取消待确认、重启不确定状态及双端产物哈希校验已实现。FastAPI 进程内模拟服务 7 项合成测试通过，无后台进程或设备；所有结果为 synthetic/not_run。该原型可复用状态语义，不是本机真实执行适配器 | src/dfx_agent/backends.py、remote_service.py；tests/test_remote_contract.py；uv.lock；.dfx-local/artifacts 中对应记录 |
| W07 领域策略合成分支 | 四类规则卡、可关闭的规则/义务/查证开关、修法等价直接路径、区分力/能力/预算判断、候选义务与合成失败反例引用已接入固定响应 Agent 和受管固定脚本；基础与增强通用工具一致。10 项策略合成测试通过；模型推断关联不作硬拒绝，未运行实际诊断探针或应用 | src/dfx_agent/policies.py、agent.py、cli.py、workspace.py；tests/test_policies.py；.dfx-local/artifacts 中本批记录 |
| W07 独立工作上下文包 | 显式焦点取得诊断/义务/候选/反例必要闭包，核验来源、全局 ID、对象及原字节；推断和语义相关性保持未知。独立 packet 的同源重建不认证最新焦点，相关检查合法进展会使旧包重建拒绝；SDK 可见性及历史恢复由下行发送计划承担 | working_context.py；tests/test_working_context.py；[独立包记录](../sessions/2026-10-03-W07-working-context.json) |
| W07 工作上下文发送计划 | 可见授权回执决定焦点/水位，必要包和未表达领域组附入启动消息，近期及完整组按共同预算选择，再组合可选压缩。每个位点冻结原字节与实际发送，历史回读不从最新检查重建；fixed/mock 请求、账本、报告和导出共同核验。评测清单 2 显式比较开关，其余参数不放宽；历史 inline 保持原形状只读，当前材料与投影见下行 | working_context_plan.py、models.py、reporting.py、metrics.py、exporting.py；[发送交接记录](../sessions/2026-10-03-W07-context-send.json) |
| W07 大工程材料与求解投影 | plan 2 引用同 run 原字节 blob，保留完整审计 packet；求解投影仅替换 sources 回执清单，覆盖依据实际全文，其他领域内容原样保留。6.4 MB 源输入可进入实际 SDK 请求，超过原单 blob 上限仍在 reserve 前拒绝。1,201 源文件及候选在独立声明的 300 秒压力预算下完成合成修复/检查；审计 packet 超 131,072 字节、每投影低于 20,000 字节，重复引用按摘要复用。默认 60 秒初测 wall_time 失败保留，容量通过不代表效率达标。崩溃残留/unknown/后续检查/通用压缩、三代旧包只读核验通过 | working_context_materials.py、working_context_projection.py、working_context_plan.py；[本批记录](../sessions/2026-10-03-W07-context-materials.json) |
| W07 规模核验耗时 | 单操作去重入库、已核验任务内部延续、材料直接子文件闭合批量读、整库审计与该次回执共享原字节；读取请求按观察大小加增长哨兵，原限额不变。同一显式求解任务的单次 profiler on 从 165.59 降至 67.90 秒；off 从 23.84 至 21.43 秒。最终无插桩重复/不同内容的 1,201 文件任务均在冻结 max_seconds=60 策略下完成五次固定调用及合成检查，总调用 51.34/65.67 秒；初始化与报告核验不全在该预算计时内，不能说全调用都低于 60 秒。分析器只覆盖当前线程、read_bytes 是部分 I/O 代理；测试准备、顺序和缓存影响未独立控制，单次结果不证明稳定速度或实际修复收益。中间超时、Windows 临时目录重命名拒绝及测试准备换行错误原样保留，未提高预算或改变成功断言 | tests/test_working_context_scale.py；[本批记录](../sessions/2026-10-03-W07-verification-latency.json) |
| W07 经验条目与知识快照 | 已实现独立主机批准库：完整指导含前提/未知问题/步骤/观察分支/保留行为/反例/退出条件；Draft 仅引用批准材料，入库与快照核对全部来源权限交集及已声明历史谱系。原字节、条目、投影和检索声明物化冻结，旧 registry/快照不随新材料变更。构造/回读零创建，闭合回读核验祖先、FD 及恢复 mtime 的同长度改写；单记录、来源总量、引用集合和选择数量有门限。原批多 registry 累计容量缺口已修复，契约 124、存储 85 项合成测试通过；现由检索及 run 复制/SDK 入口消费。历史仅为主机 fixture 声明，应用资格无、声明成本非真实账本；不含学习或 Skill 发布，哈希不认证自然语言或真实报告 | knowledge_contracts.py、knowledge.py；tests/test_knowledge_contracts.py、test_knowledge_store.py；[基础记录](../sessions/2026-10-04-W07-knowledge-snapshot.json) |
| W07 冻结经验检索 | 每快照显式建立 SQLite 派生索引；查询只用获准指导实际值，规范标签/去重词计分，零分/未知标签保留，稳定排序分页并返回完整投影。query 纯读，record 显式保存，read_record 从原件重算全文；只允许回读实际命中页。scope 在索引前核对，该检索接口自身不认证调用者当前任务；原闭包、索引全部行、结构/页/附属文件和精确容量均核验，累计读与每连接 SQL 有门。独立审查的 64 位评分与 sidecar 路径先验缺口已修，新增契约 97、检索 97 项原批合成测试通过；普通检索现由 SDK 入口消费，不含自动学习。排序不证明适用性、创新或提速 | retrieval_contracts.py、knowledge_retrieval.py；tests/test_retrieval_contracts.py、test_knowledge_retrieval.py；[检索记录](../sessions/2026-10-04-W07-knowledge-retrieval.json) |
| W07 当前任务绑定与三态经验交接 | 独立 ApplicabilityStore 绑定外部主机批准的 task/run、源码/日志、知识快照和 scope；完整命中页与逐前提引用核验，矛盾优先、未知保留原问题、支持只建议尝试。诊断/假设/当前候选及递归执行依据实查版本，旧候选失败不能升级新候选事实；上下文/审查从冻结原字节回读，原诊断重验引用准入。发布前核定捕获关闭与后续审计预算，原件/CAS 重复路径亦计费。契约 160、接口 72 项新增合成测试通过；语义仍为推断、独立接口焦点仍为显式主机输入；本接口不调度 SDK/Skill/学习或应用，SDK 外层见下行 | applicability_contracts.py、applicability.py；tests/test_applicability_contracts.py、test_applicability.py；[本批记录](../sessions/2026-10-04-W07-applicability.json) |
| W07 普通经验检索 SDK 与运行证据 | 外部批准的快照/索引/完整来源闭包复制到 run，模型只提交搜索提示或实际返回页引用；两项工具保留完整前提、未知问题、过程及反例，统一授权、调用与工具预算。恢复先只读核知识及账本，完成结果回放；原库不可用仍能恢复。11/8/5 报告重算原指导并关联实际 solver/动作/回执，清单 3 冻结同知识配置，迁移私有包可独立核验。发布前合算 owner/知识与关闭读取预算，并预检双份 ASCII 报告片段；大型中文 12 查询批只发布可容页面，拒绝其余且完整报告仍可回读聚合。新增复制 28、SDK 21、报告/聚合/导出 57 项合成测试通过；原批未含静态 Skill 或 CLI 配置；新增接入见下行，自动学习与真实效果仍未完成 | knowledge_run.py、knowledge_handoff.py、agent.py、reporting.py、metrics.py、exporting.py；[本批记录](../sessions/2026-10-04-W07-knowledge-sdk.json) |
| W07 三态经验 SDK 与实际输入认证 | 每次求解在发送前冻结请求、消息、工具及参数，从完整实际已见结果核验焦点与证据；三项工具提供完整指导、提交 schema、逐前提三态建议和原未知问题。摘要 ID、遗漏材料及同轮兄弟后来结果不能升格；缺焦点返回普通指导。启停两组共用 19 项工具、完整知识和原预算，使用 12/9/6；清单 4 固定该比较因素。恢复先只读审计、完成回执回放，报告及私有导出重建真实调用关联；缺账本只核原件，孤立产物不计实际使用。求解输入与新增报告片段有发布容量门，全领域/矩阵容量待收口；静态 Skill 与受管接入的新增证据见下行；自动学习与真实效果仍未完成 | solver_visibility.py、applicability_handoff.py、agent.py、actions.py、models.py、reporting.py、metrics.py、exporting.py；[本批记录](../sessions/2026-10-05-W07-applicability-sdk.json) |
| W07 静态过程资源与 SDK middleware | 从批准人工条目生成完整过程和冻结清单，保留前提/未知问题、过程分支、保留行为、反例、退出条件及完整投影；原生 SDK 加载/注入前核原件和缓存。启停正文、元数据及完整回读相同，关闭只取消目录注入；没有支持脚本、目录扩展或新执行权限。58 项受管合成自检通过，含重写摘要的伪造正文、共享文件、加载缓存、来源/资源容量和发布中断。此资源层证据不证明实际使用或收益；会话交接及其新增证据见下行，受管 CLI 的新增验收见下行 | static_skills.py、tests/test_static_skills.py；[本批记录](../sessions/2026-10-05-W07-static-skills-core.json) |
| W07 静态 Skill 实际会话交接 | 固定/MockTransport 程序入口将相同 read_skill 接入共同预算；启停只差目录注入，完整读取结果和普通检索均保留。session/report/completion 13/10/7 与清单 5 冻结资源，报告从原件及实际请求/父响应/动作/回执区分读取与后续可见性；未知不重派，缺报告/账本保持未知。恢复先只读核原批准、资源、配置、缓存及未合并 journal，静态 Graph 使用严格 serializer；私有迁移包含完整正文和实际输入。新增读取闭包及双份报告片段门，公共矩阵逐项按 UTF-8 输出容量拒绝；不声明全域 I/O/内存闭合。静态交接 31、固定/模拟 SDK 10、恢复/派发 10、报告/矩阵/迁移 33 项通过；原批未含受管脚本/CLI 配置，新增接入见下行；新增独立演进见下行，版本消费程序入口已实现，完整部署验收待完成；不证明修复或学习收益 | agent.py、static_skill_handoff.py、solver_visibility.py、model_provider.py、reporting.py、metrics.py、exporting.py；[本批记录](../sessions/2026-10-05-W07-static-skills-handoff.json) |
| W07 受管知识与 Skill 配置 | 固定/模拟脚本 5/6 必填主机批准与明确库定位符，按选择生成原会话 11/12/13；启动只读核完整原件/索引，恢复不依赖外部库。三条真实 Windows Job repair（固定目录启停、Mock）均为 3 调用/2 工具、45 合成已知 Token、20 项工具；原库移走后 resume 无新增调用，策略漂移和坏数据库拒绝保留原终态字节。两格固定及一格模拟矩阵受管聚合，私有 Export 在原任务/run 不可用时独立 Verify 通过。4 个相关测试文件 235 通过、1 符号链接权限跳过；Ruff/Pyright 通过，旧脚本含义保留。库制作仍是主机 API；反馈及人工候选的新增证据见下行，自动学习尚未接入；没有真实应用资格 | offline_script.py、cli.py、local_agent.ps1、agent.py；test_offline_knowledge_config.py、test_managed_knowledge_workflow.py；[本批记录](../sessions/2026-10-05-W07-managed-knowledge-cli.json) |
| W07 开发反馈与经验候选 | FeedbackStore 经两份外部主机批准保存单开发运行的完整选择、原件、知识祖先及待审核过程，原库/任务/run 移走后仍独立重聚合。固定和 Mock SDK 各保留首次失败、最新通过、9 调用、105 已知合成 Token 下界、2 次缺 usage，总量仍 null、应用接受 false；普通 session 11 的缺报告合法反馈保持未知，旧代/缺来源/非开发知识拒绝回流。候选仅人工 host_curated、validation_scopes 空、pending_host_review，学习调用 not_run、成本 null；不入旧库或启用 Skill。标准仓库布局、完整批准与原快照认证、各读取阶段累计预算已修复审查缺口；基础 48、集成 33 项通过，相关回归见软件自检。复制/关闭读取有限，不声明聚合内部全 I/O 或自然语言防泄漏 | feedback_contracts.py、feedback.py；test_feedback.py、test_feedback_integration.py；[本批记录](../sessions/2026-10-05-W07-feedback-candidates.json) |
| W07 Skill 版本与事前合成验证 | 完整人工种子/原候选形成内容版本，父链与反馈来源复制且递归核权限/学习谱系；全部角色 work 不存在时冻结代表/边界计划，固定相同知识/资源、模型、工具和预算。实际完整目标读取与后续 solver 可见性从账本重算；普通经验回读串臂也不合使用条件。4 个固定 SDK 角色各保留首次 fail、最新 pass、9 调用、105 已知合成 Token 下界、2 次缺 usage，总量 null；原验证源移走仍独立回读。发布前预留全部递归回读预算的阈值反例通过。基础 37、集成 10 项通过。followed 只认证读取/可见性；同池定向验证不证明不同知识快照收益，学习 not_run/null、应用 none；新增演进判定见下行 | skill_evolution_contracts.py、skill_validation.py；test_skill_validation.py、test_skill_validation_integration.py；[本批记录](../sessions/2026-10-06-W07-skill-validation.json) |
| W07 独立合成演进与沙盒发布 | 冻結事前策略后从原件重算全部尝试的收益、损害、完整矩阵和成本；未定阈值/缺项/未读/未知继续 pending，同计划补齐不重计，跨计划不重置父/策略/配置或重复案例。沙盒新激活须引用候选当前判定与当前别名前驱，后来失败/未知不能拿旧合格记录绕过；祖先回退与旧冻结选择/幂等回读保留原批准。单 API 内复用已核验计划，成功/发布前完整关闭父链/反馈来源，每份回执仍独立重聚合。四组 31 项通过，合成基例 495 已知 Token/33 调用；应用 none、学习 not_run/null，版本消费程序入口已实现但整链部署验收未收口，不证明真实修复或迁移收益 | skill_transition_contracts.py、skill_evolution.py；测试入口见 DEVELOPMENT；[本批记录](../sessions/2026-10-06-W07-skill-evolution.json) |
| P0 动作与共同接受契约 | 模型动作提案与主机引用/能力/预算授权、候选无关的四层共同协议、故障签名、源码/运行版本绑定和保守接受门已形成 Pydantic 契约；13 项合成测试通过。动作授权已接入固定响应工具边界，接受门仍未接入真实打包器、业务 oracle 或设备；模拟或缺项不能成功 | src/dfx_agent/contracts.py、acceptance.py；tests/test_action_acceptance.py；模块规格第 1.2–1.3 节 |
| P0 逐工具授权与结果恢复 | SDK 无知识保持 14 个项目工具，明确批准知识时增加两项普通经验工具，显式三态策略启停两组再增加相同三项工具；执行前校验参数、动作/能力、任务/版本、实际引用、候选与预算，绑定已记账的主机模型调用身份。SQLite 保存授权与完整工具结果，完成回放不重复动作，回执未存保持未知；知识工具复用既有能力和总工具预算。原授权批 15 项反例通过，新增交接见知识 SDK 批。固定响应、模拟请求和 competition 云端适配代码路径共用授权；真实云端仍未实测 | src/dfx_agent/actions.py、agent.py、models.py；tests/test_tool_authorization.py；sessions/2026-10-01-condition-handoff.json |
| P0 因果诊断与修复义务交接 | 已补齐必要条件、值/状态绑定、因果步骤、支持/反驳引用、区分问题和双向义务关联；结构化提交由主机读取源码/日志、核对实际失败检查的候选版本，再原子保存诊断包。schema/既有记录可按需回读；候选和合成反馈共享义务 ID。21 项新增合成测试通过，引用契约 5 项、闭环 12 项、授权 15 项和策略 10 项回归通过。模型状态保持 unknown，此接纳入口仍不自动计算条件，因果真实性及效果未知 | src/dfx_agent/contracts.py、diagnostics.py、agent.py；tests/test_causal_diagnosis.py；sessions/2026-09-30-causal-diagnosis.json |
| P0 已登记诊断到修复决策 | DecisionRequest 绑定同任务/运行/源码的诊断、假设、证据、义务与区分问题；失败后仅使用已保存且匹配原候选的合成检查反馈，有限条件另声明完整期望范围并关联相关假设方向。保存建议与上下文，可回读并由候选成对引用决策/方向；原有直接/补查/未知/失败重审 21 项合成测试回归通过。session schema 8 拒绝旧会话续跑；仍不执行探针、不确认因果或硬拒绝推断义务 | src/dfx_agent/decisions.py、agent.py、actions.py；tests/test_decisions.py；sessions/2026-10-01-condition-handoff.json |
| P0 首批有限条件检查 | 索引范围、DataView 访问、TypedArray 容量/对齐/省略长度及使用点就绪状态已有程序接口，SDK 交接见下行。主机固定合成输入，核对任务/版本/诊断/绑定/对象/路径/采样点与单位；缺值或前提不足保留未知，不求值表达式、不改写假设或应用门。51 项计算合成测试通过；源码值提取和真实运行采样仍未实现 | src/dfx_agent/conditions.py；tests/test_conditions.py；模块规格第 3.0 节；sessions/2026-10-01-condition-handoff.json |
| P0 有限条件结果交接 | 主机输入及内容寻址结果不可改写保存；回读重核诊断、源码、候选、范围和完整计算。SDK 可按输入 ID 计算、按结果 ID 回读并提交同范围决策；violated 仅建议重审相关方向，satisfied/unknown 不确认修复，关闭关联保留未知。结果保存 23 项、交接/授权/恢复 30 项新增合成测试通过，原条件 51 项及授权 15 项、决策 21 项回归通过；报告 schema 5、会话 schema 8 冻结输入指纹。CLI 默认空 registry，程序测试可注入主机 fixture；可信采值及适用性仍未实现 | src/dfx_agent/conditions.py、agent.py、actions.py、decisions.py；tests/test_condition_store.py、test_condition_handoff.py；sessions/2026-10-01-condition-handoff.json |
| P0 可信采值与算子绑定设计 | 已在现有主文档定义操作/参数锚点、主机角色映射、来源分层、采值凭据、对象代次/操作事件、观察扰动、预算/恢复及 schema 迁移；逐项推演同单位角色置换、候选移位、使用前后状态、目标未到达和平台未知等反例，并做跨文档人工审查。独立合成入口见下行，真实采值与平台适用性仍待实现 | [设计第 3.2.1 节](development/system-design-draft.md#321-怎样取得可信的条件输入)、[规格第 3.0.1 节](design-contracts.md#301-可信采值与算子绑定)、实施计划；sessions/2026-10-01-value-source-design.json |
| P0 主机操作绑定与采值凭据合成契约 | 独立 SourceAnchor/BindingProposal/主机合成映射、CaptureIntent/Receipt 和不可改写登记已实现；实查 UTF-8 原字节、父操作、诊断、原版/候选和冻结 manifest。模拟事件核对操作、对象实例/代次/时序，回读不登记；追加 fixture 不改变旧凭据，来源始终 synthetic。66 项新增测试通过；工作区 4 通过/1 权限跳过、因果诊断 21、有限条件 51、条件保存 23、固定响应闭环 12 项回归通过。未接 SDK、新来源计算、解析器或设备 | src/dfx_agent/value_sources.py；tests/test_value_sources.py；sessions/2026-10-01-value-source-contracts.json |
| P0 案例选择契约 | 显式 schema 2 目录校验任务、案例键、谱系、变体、日志成员及可选场景脚本哈希，拒绝重复身份/同字节日志、不安全路径和会复制进分析快照的评测脚本；任务快照仅留下选择摘要。7 项合成测试通过。目录仍不能证明日志/源码同版或应用可运行 | src/dfx_agent/task.py、cli.py；tests/test_case_identity.py；数据报告候选审查 |
| P0 模型调用与恢复契约 | SQLite 请求账本在调用前登记并占用次数，发出后同 ID 不盲重试；完成响应可跨重启回放，格式错误和已知/缺失 usage 分开记录。records 按用途和归属只读列出完整状态，原 schema 1 不扩学习用途。新 schema 2 为独立学习 owner，提炼/格式修复共用预算、未发 preflight 失败保持 reserved，已完成父链/完整响应重核，禁止换来源或混代。新学习账本 51 项、原账本 21 项本轮通过；完整反馈输入和模型草案的验收见 W07 独立模型提炼行。真实提供方、底层重试计费和实际 Token 硬限额仍未接入 | src/dfx_agent/model_calls.py、models.py；tests/test_model_calls.py、test_learning_model_calls.py；模块规格第 1.2.2、3.1 节 |
| 离线证据入口 | prepare/analyze 提供快照、日志解析、栈位置映射和证据报告，结果不自动确认根因或有效修复 | cli.py、logs.py、sources.py；修复循环见 W05 |
| W07 独立模型提炼与待审核草案 | 外部批准绑定原开发 capsule、模型/预算和物理工作根；完整原日志、全部登记候选 diff、聚合及权限进入实际无工具 Mock 请求。提炼/显式格式修复共用独立账本，preflight 失败尚未发、发送未知不重试、完成恢复无新发送；合法输出只成为 model_inference 待审核草案，全部调用、原响应、权限/知识祖先声明谱系及成本前缀冻结。换根、原件/批准/请求漂移、缺库/删行及容量不足拒绝；执行恢复仍要求原根及 capsule，副本回核见来源包行；初始化跨文件失配协调、实际费用与完整版本消费/部署验收仍待完成。基础 5、来源/账本/容量审计 24、根绑定 9 项合成测试通过。首次审计 23 通过/1 失败保留：容量门之前目录身份观察不一致，具体漂移字段未采集；改为明确物理身份并独立拒绝危险属性后，原 headroom/无写入/无发送断言重跑通过。没有提炼质量或学习收益结论 | learning_contracts.py、learning.py、model_provider.py；测试入口见 DEVELOPMENT；[工作流记录](../sessions/2026-10-06-W07-learning-workflow.json) |
| W07 可移植学习来源 | LearningSourceStore 保存完整原操作与开发 capsule、原批准、owner、请求/响应、全部账本及草案成本前缀/全操作成本；副本仅可只读回核，原库移走仍不新发调用。历史重复 PreparedRequest、缺原件、批准/输入/提供方漂移、完整重哈希及容量不足均拒绝。10 项合成测试通过；首次 10 项失败是 fixture 复制采用普通 Windows 长路径，改为既有扩展路径入口后通过，未缩短路径或放宽限额。格式修复失败及费用未知保留；复制包不能独立证明原响应未被整套合法替换。模型版本独立接口、SDK 候选消费和独立资格判定已验收，完整部署整链仍未通过；最新激活容量失败见最终关闭记录 | learning_source.py、learning_contracts.py、learning.py；tests/test_learning_source.py；[来源包记录](../sessions/2026-10-06-W07-learning-source.json)、[最终关闭记录](../sessions/2026-10-07-W07-final-receipt-close.json) |
| W07 审核模型来源版本 | schema 2 的精确主机审核保存父批准、草案/来源/投影身份及完整权限交集，保留 model_inference、原草案前缀/全操作/祖先学习成本。只读版本及文件/空目录联合导出在原学习库移走后可回核；改审核/父/来源、错代、缺原件及容量不足拒绝。独立组 10 项合成测试通过（222.02 秒）；不可改写版本仍为 unvalidated/none。最新整链前两项通过，独立 v2 transition 达到合成资格：验证 33 调用/495 合成已知 Token、学习 2 调用/30 合成已知 Token 分别保留。模型激活在最终回执元数据关闭时超原读取额度；开发部署、回退/旧任务恢复及导出未验收，应用资格 none，不据此宣称自主学习收益 | skill_evolution_contracts.py、skill_validation.py；tests/test_model_skill_versions.py、[版本消费记录](../sessions/2026-10-06-W07-model-version-consumption.json)、[最终关闭记录](../sessions/2026-10-07-W07-final-receipt-close.json) |
| W07 ResearchProfile 与来源历史成本 | 既有逐行 ResearchProfile 保持原 schema/解释，纯契约 5 项通过；普通聚合仍未传完整验证/部署成本。新增显式 VersionSkillService.research_cost_inventory 从已认证的 run 副本核完整学习调用与验证原行，保存来源调用/事前角色完整成员并跨清单去重，保留失败、格式修复、未知和全部模型/退回历史。56 项合成测试通过：含真实人工种子冻结服务的只读、原库移走及副本漂移拒绝；模型成本提取仍为纯序列化 fixture，不称实际模型服务验收。Ruff/Pyright 通过。来源总成本、部署/退回实际成本保持 unknown；未接普通报告、共享预算/截止时间或学习因素比较，完整 ResearchProfile 未完成 | research_profile.py、research_accounting.py、version_skills.py；[来源成本记录](../sessions/2026-10-07-W07-source-cost-inventory.json)、[既有契约修复](../sessions/2026-10-07-research-profile-quality-repair.json) |
| 软件自检 | SDK 候选四项已有通过记录，覆盖暖服务任务来源自洽漂移。最新完整部署重跑 2 通过、2 项 setup 错误（1562.19 秒），在原 1800 秒任务时限内结束；验证和独立资格判定通过，激活最终元数据关闭仍超原 128 MiB 读取额度。未取得部署/恢复/导出通过证据。回执最终关闭及头漂移反例 18 项通过（3.89 秒），旧人工发表/激活/回退 7 项通过（722.16 秒），来源清单回归 56 项通过（5.07 秒）；两次含共享 helper 已导入的 pytest warning。Ruff 通过、Pyright 0 错误/0 警告，未运行全仓测试。历史失败保留，限额未放宽 | [最终关闭记录](../sessions/2026-10-07-W07-final-receipt-close.json)、[此前回执修复](../sessions/2026-10-07-W07-receipt-closure.json)、[此前审查](../sessions/2026-10-06-pretest-readiness-audit.json) |
| 仓库管理 | 本地 Git 已建立，模板与开发材料分开；失效规划已归档，当前文档设有归属及检查规则 | AGENTS.md、DEVELOPMENT.md、Git 历史 |
| 文档自检 | 登记仍只有 DEVELOPMENT 一份、14 份主文档；开发设计第 8.2.1 节新增从任务路径到模块/测试/能力边界的逐项审查入口，既有原型/规格同步操作内回执关闭语义。三个提交模板未改，自动检查实际输出见对应 session；该检查不能代替语义或效果验证 | scripts/check_docs.py；[回执关闭记录](../sessions/2026-10-07-W07-receipt-closure.json) |

## 整体审查与就绪判断

2026-10-15的软件审查覆盖CLI/配置与安装、任务/源码/日志、模型预算和安全重试、SDK工具/上下文/Skill、诊断到候选、固定检查、报告与恢复；当时识别的五类缺口均已修复并有定向反例。这些软件证据没有覆盖逐例真实验证适配。本轮发现其余任务只配analysis而未执行候选，补齐工作和实际覆盖见本页当前阶段；不能延用软件就绪判断声称全量实验已闭环，冻结研究扩展也不是竞赛前置。

| 本轮问题 | 当前行为 | 2026-10-15 软件证据 |
|---|---|---|
| 跨轮合法工具 ID 被上下文/提供方误拒绝 | ID 在同一批内唯一；完整闭组后可复用，动作仍绑定模型父调用；孤儿、重复结果与未闭组继续拒绝 | 实际 AsyncOpenAI/Mock 与默认/frontier 两种上下文均续调；协议128项、分组55项回归通过 |
| 候选漏关联诊断后无法补挂 | 竞赛冻结前要求已登记所选根因及原版行，缺项不冻结、不清草稿；可补完整诊断再提交同稿 | SDK保留草稿后修正、缺所选/行、恢复语义不可更改等5项通过 |
| 最后检查通过却因收尾额度不足丢失接受 | 纯 model_budget/action_budget/wall_time 保留已验证最新候选；最新失败、缺定位或未知调用继续阻断 | 终态8项通过，含14次实际模拟调用后的双候选修订和完成恢复；应用接受只用回执fixture |
| 独立判据只在配置时校验 | 固定路径/字节进入执行身份，准备及每阶段前后核验；旧应用只读降为不确定，新应用缺绑定拒绝 | 本机执行60项通过/1权限跳过、profile12项与CLI48项通过，包含启动前/阶段间/阶段中漂移 |
| 初始化中断缺主回执导致回读失败 | 完全空目录只读呈现不确定，不补写/重启；非空却缺主回执继续拒绝 | 上述执行组覆盖两种模式账本回读及三类残留反例 |

2026-10-08 对 ca172f5 的审查及后续局部修复是下表的历史证据，不冒充本轮重新运行。实际应用环境下的触发频率及效果未知。

| 原级别与问题 | 修复行为 | 当前证据 |
|---|---|---|
| P1：完成恢复在预算边界降级 | 无待执行工作的完成恢复不要求剩余额度，原完成结果不降级；待执行恢复仍受原账本和截止时间控制 | 三组调用/Token/同时耗尽的完成恢复通过；pending 调用、Token、时间限制回归通过，竞赛完整 15 次调用后恢复无新增调用/检查 |
| P2：应用执行事实与最终接受混用 | execution/competition schema 2 单列层级、实际执行结果与最终接受；已执行阶段先保存再核源码，源漂移保留 inconclusive 原件。旧 Stop 缺明确未启动事实时不误写 not_run | 报告状态组合及旧记录反例通过；短时合成程序覆盖原版 build/target 与候选 build/target/behavior 源漂移、回读不重启及原件漂移拒绝。application 状态只用回执 fixture 验证，没有真实应用运行 |
| P2：受管启动与活动标记发布竞争 | 创建并加入 Job 后先原子发布完整 active.json，再检查启动 Stop、Resume；异常终止 Job 并清理本次句柄/标记 | 延迟发布下 worker 立即读取完整身份/限制通过；发布/恢复边界失败和恢复前 Stop 均未运行 worker，Job/标记回收及既有进程保护回归通过 |
| P2：完成恢复膨胀任务耗时 | 相同报告事实复用首次终态观察计时，新增真实进展刷新；旧报告没有可信结束时间且无进展保持 null。计时范围是会话开始至报告观察，不等于定位/修复阶段耗时 | 完整 SDK 恢复计时/报告摘要不变；两天后相同事实不变、新进展刷新、旧假预算停止纠正仍时间未知、计时漂移只读拒绝通过 |

核心机制有实现载体，但必须按实际路径解释：修复义务和失败决策在演示中参与了两候选修订；它们是可选指导/关联，不强制关联全部义务和决策，也不证明业务语义；竞赛候选的完整诊断关联则在冻结前要求。四份静态 Skill 已接原生 SDK；该演示只读其中一份，普通经验查询/读取与三态动作均为空，因此不能声称演示验证了全类别迁移。普通检索/三态的独立 SDK 合成测试与这次演示是不同证据。

工作上下文在审查所用 review01 演示的 15 个实际模型发送位点生效；末位点原消息视图 46147 字节、增强发送视图 66276 字节，增加约 43.6%，omitted_ranges 均为空。该数值是当时的视图字节而非 Token，不通过本次文字/代码修订刷新证据；它证明组织与补充已执行，不能证明节省上下文或费用。需要同模型、同知识、同工具和同预算的真实对照判断额外输入是否换来修复收益。当前决策能力收敛为源码/日志读取且运行探针预算为 0；没有主动运行采值或默认自动跨任务学习，不把延期扩展计为竞赛核心已实现贡献。

当前可以逐项审阅核心方法载体、实际 SDK/工具/反馈路径与软件验证边界，也可以使用独立 CLI 运行演示。用户要求的核心 harness 复核已落到上下文回读、原文去重和当前决策消费，不继续扩建通用架构、供应商框架或高级演进。语义摘要、单行超大工具结果的渐进卸载和可信运行探针未接成竞赛能力；不以它们作为已完成贡献。下一步先完成剩余真实源码修复，再用同预算比较当前实际启用的方法与强基础流程，按真实失败决定是否需要上述能力。方法效益、论文贡献强度和 CCF-B 发表可能性仍须实测，不由代码量或工程验收证明。

## 尚未完成

竞赛交付当前进入异常/OOD与有限分层比较；全部26例已有最终源码验证通过结果。具体覆盖唯一见本页当前阶段。analysis候选只是中间产物，每例须给原版故障复现、冻结候选、正常/边界及回归结果和覆盖说明。已有CLI和代表源码链证据不替代其余案例的检查。方法相对收益、无缓存冷安装、独立wheel、其他系统和完整HarmonyOS应用验证没有新增验收。
派生版本的完整验证/发表到 Agent 消费、部署、恢复/导出整链仍未通过验收，作为扩展未完成项保留。最新重跑的四角色 SDK 验证与独立 v2 资格判定通过，模型版本激活在最终回执元数据关闭时仍超累计读取额度；部署、回退/旧任务恢复及导出未验收。两遍回执关闭与最终链头核对已有反例及旧人工发表回归，不能据此绕过激活容量失败。后续若恢复该扩展，须分解并减少同操作重复来源核验，在原限额内重新验收。首次没有具体字段的祖先漂移原因仍未知，已观察到的目录高位仅作窄范围兼容。

来源历史清单已有完整学习/验证及退回历史核算与合并；其实际模型版本服务入口仍待验收。全生命周期 ResearchProfile 还缺任务/研究报告、solver/部署原件、跨操作共享预算/截止时间及比较身份，不能只补测试；它不替代竞赛任务内已有预算，也不作为本轮交付门槛。模型草案批准与来源版本已通过独立接口验收，学习来源可移植只读回核，但执行恢复仍绑定原物理根。已有独立演进判定/沙盒发布和版本程序入口，只比较同池目标过程的实际读取/后续可见性，尚非不同知识快照的效果研究；完整学习/部署收益未知。开发 capsule 已保存合成原件闭包，人工候选不自动入库；真实应用反馈、模型来源进入旧 KnowledgeStore 的兼容桥接、完整学习/部署成本及代表规模经验库性能/检索质量仍待完成。

SDK 已认证三态焦点的实际发送来源，不认证经验前提的语义真值；主机完整审阅投影和如实声明来源仍是基础。知识、三态及静态报告门覆盖各自新增片段，公共矩阵输出已有逐项及最终完整表示门；全领域报告、输入树快照、全流程 I/O/内存及单项构造峰值仍未闭合。普通路径发布曾失败：原路径长度推算 260 字符、缩短 run 标识后 257 并通过，未保全原目录复现原因；任意 Windows 长路径能力仍须验收，短路径 fixture 不作证明。材料及两类代表千文件任务已有合成验收，全部 data_B 工程的真实耗时和运行能力仍未知；盘点和合成压力不等于真实 Agent/应用实验。静态 RuleCard、任务内检查点和通用压缩继续可用，真实晋升策略数值待开发条件确定。

方法贡献仍须在同模型、同知识、同预算下验证修复义务组织和有条件经验迁移的效果，当前不报告学习收益、根因准确率或完整应用修复率；局部源码验证结果见当前阶段。

效果评测已有代表局部协议、冻结输入清单和本页当前阶段所列真实模型产物；全部源码协议已有声明范围的业务/回归判据，全量最终源码结果与聚合已取得；独立版本对齐、数据划分、同预算比较及可公开脱敏材料仍待后续整理。完整应用工具链/设备验收仅在另选application扩展时需要。摘要忠实度、真实提供方Token窗口/实际成本与Token硬限额、超摘要输入后的滚动历史能力均未实测。有限源码literal提取、可信运行值采集、实际语法/类型下的算子/角色/候选坐标与目标平台适用性核验、新来源接入计算/SDK、其余规则的程序检查仍属未完成扩展，不由合成条件检查推定成立。

真实模型运行与局部源码成功数量见当前阶段；完整应用构建和设备回放仍为0。全数据根因准确率、完整应用修复率、覆盖外稳定性和账户实际扣费尚未知。

## 环境与未决条件

- 本次轻量路径软件验收后受管盘点为 owner_valid=true、resource_accounting=complete、inaccessible_paths=[]、active_state=none、cleanup_available=true，受管目录与已有导出合计约518.2 MiB，不包含 data_B 等非接管资源。复用项目 Python 和 PATH 中既有普通 Node；未安装 npm 包、HarmonyOS SDK、模拟器或权重，未改全局环境或操作旧 tmp 权限。测试临时树由既有入口回收，旧文件替换失败原件/回执保留；盘点时点与字节见本批 session。

- 2026-10-11 恢复验收前的受管 Status 为 owner_valid=true、resource_accounting=complete、inaccessible_paths=[]、active_state=none、cleanup_available=true。本轮没有操作旧 tmp 权限；Init -OfflineSync 因缓存缺 hatchling 失败，随后受管在线 Init 成功，只安装项目 editable 包及所需构建依赖，dfx help/version 通过，锁与环境身份由入口同步。Python 3.13.5、uv 0.12.9、Deep Agents 0.7.18 复用；不改全局 pip/PATH/注册表，不安装 HarmonyOS 或模型权重，不改资源/实验开关。产品验收和最终文档检查后的本机盘点仍为 complete、无活动任务、可清理，受管目录与已有导出合计约435.8 MiB；本轮没有安装 HarmonyOS 或权重、改限额或清理旧目录。最后沙箱内启动器/盘点探查失败，本机只读 CLI version/status 及相同受管 docs 入口通过，精确沙箱原因未单独查明；未排除不可读路径。具体回执和失败见两批验收 session，历史 incomplete 观察不代表当前本机状态。

- 2026-10-10 范围复核的只读 Status 为 owner_valid=true、active_state=none、resource_accounting=incomplete、cleanup_available=false，两处不可读 pytest 临时目录与 10 月 8 日相同。同日两次受管 docs 均在 worker 创建前拒绝，receipt 分别为 53897f25-0c4f-4244-8f34-122d61f38371 与 96ccd373-06ba-4c19-88e0-1fba70528196，child_pid 均为 0，因此 check_docs.py 未执行。用户报告已处理权限后，沙箱内外 Status 仍列同两处不可读目录，精确目录的沙箱外 ACL/枚举仍被 Windows 拒绝；受管 working_context_plan 测试在 worker 前拒绝，receipt 991a2c80-0ab5-4bd0-bce2-034d3d8d240b、child_pid=0。用户随后明确先完成代码、测试稍后继续。该次源码准备未修权限、删除/迁移目录、放宽限额或安装，当时新增用例未运行；10月11日的新增证据不追改这一历史事实。

- 2026-10-08 产品 CLI 本轮受管 Init -OfflineSync 在 worker 创建前失败，receipt d7103fee-423d-426e-be9f-1f6ee677536b。随后沙箱内外两次只读 Status 均为 owner_valid=true、active_state=none、resource_accounting=incomplete、cleanup_available=false；不可读目标为 .dfx-local/tmp/pytest-031b01d69cd34079887b400cb17f1b2c 和 pytest-dc6796292b2a4fe1ae52c628444c4cc4。直接只读目录枚举/ACL 也被拒绝，原因仍未知，不能仅归因于沙箱。没有执行 lock/sync、下载、测试、ACL 修复或清理。此前 complete 的观察仍是历史事实，不代表本轮可启动。

- 用户已决定在普通本机进行后续源码/日志修复与回放评测，不提供硬件环境。HarmonyOS 工具链/设备不作为默认接纳项；特定平台行为无法宿主回放时明确为未覆盖，不能据此阻塞全部诊断和补丁生成。
- 当前受管环境使用项目专属 Python 3.13.5 与锁定依赖。9 月 16 日只读检查未在 PATH 找到 hdc/ohpm/hvigorw，未接通模型提供方；这些是观察记录，不是对所有安装位置和账号的完整证明。
- 2026-10-05 受管知识 CLI 与反馈候选两批均未安装或下载依赖，复用项目专属 Python 和锁定 SDK；反馈验收后观察 managed 与 exports 合计约 309.4 MiB、活动任务为 none。新增合成 fixture、三条离线记录和一个私有包均位于受管范围；反馈软件测试临时树已回收，失败与拒绝记录保留。未更改资源限额、全局环境或提交模板。此前并行中断草稿只在忽略的 archive 保留，当前代码及证据不依赖归档；实际记录见两批 session。
- 2026-10-06 学习及版本消费继续复用已有项目 Python 和锁定 SDK，没有安装依赖、模型权重或 HarmonyOS 工具链。此前两个 pytest 临时目录不可读，管理入口报告盘点 incomplete、关闭清理并拒绝新 Run；历史阻断及访问恢复尝试见版本消费 session。当日 23:47 本机复核显示 resource_accounting=complete、inaccessible_paths=[]、owner_valid=true、active_state=none、cleanup_available=true；此后四项快速检查已实际进入受管子进程。该观察不说明原部署测试完成，也不保证大规模重跑不会复发。该批只读盘点和检查未修 ACL、删除证据、广泛清理或改限额/全局环境/模板。
- 2026-10-08 两批复用既有受管 Python/锁定依赖，未安装、改全局环境、修 ACL、删除证据或放宽限额。测试和质量检查结束后、最终文档检查前的盘点为 complete、inaccessible_paths=[]、owner_valid=true、active_state=none、cleanup_available=true，受管目录与已有导出共 429475489 字节（约 409.6 MiB）；不包含原始 data_B 或其他环境。真实模型和 HarmonyOS 运行仍为 0，完整观察见第二批 session。
- 2026-10-08 审查修复两批同样复用原环境；首次已有候选目录重命名 WinError 5 的原因未知，未改权限/路径/限额，原样复验通过，失败保留。源码测试完成后、最终文档检查前盘点 complete、inaccessible_paths=[]、owner_valid=true、active_state=none、cleanup_available=true，受管目录与已有导出合计 429483346 字节（约 409.6 MiB）。只回收本轮测试自建的临时树，没有广泛清理或接管旧记录；详见两份收口 session。
- 用户已授权本项目模型接入和测试预算，模型参数以本机私有文件为准，费用边界见评测主文档；实际模型启动开关状态见本页当前阶段，不由授权本身推定开启。application、平台SDK安装和权重仍关闭，第三部分先评估条件。实际费用、源码回放覆盖及组委会最终评分认可须保留独立证据。

## 下一步及完成条件

1. 完成异常输入的软件矩阵，单列编码、HybridStack、空/噪声/伪BOM与缺材料的预期和结果，不用解析通过代替模型OOD成功。
2. live1017-retry20已获明确请求级费用接纳；原未知请求不重发。通过原金额门后，先做少量模型证据不足场景，再做冻结代表子集的同模型/知识/工具/协议/预算比较，失败和无收益如实保留。
3. thinking enabled与完整历史保持一致，当前未启用的前沿剪枝、通用语义摘要或自动学习不计入实测方法；只对已落实的领域指导、Skill及修复闭环提出可检验结论。
4. 第二部分完成后只读评估Windows工具链/模拟器是否可用；没有平台不阻塞源码交付，不自动安装或把源码检查填成application成绩。
5. 所需测试及结果核验结束、收到用户具体提交要求后，再填写竞赛模板、精简session和整理提交版仓库。当前不改模板、重写Git历史或推送。

自动提炼/晋升、完整跨版本消费闭包、全生命周期科研账本和自动运行采值仍冻结。内部完成目标、资源边界和比较规则分别引用实施计划、开发设计和评测口径。
