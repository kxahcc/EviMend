# data_B 数据核查

状态：当前证据汇总。最后维护：2026-09-28。
证据采集：2026-09-15 完成全量只读盘点与代表材料静态审查；2026-09-28 只读复核 021、022 两个候选的原始日志字节、关键源码、工程入口及场景脚本。均未运行应用；9 月 28 日没有重新盘点全量数据。

职责：仅维护数据事实、问题和来源。当前阶段见 [实施计划](../project-work-plan.md)，实现方案见 [设计草案](../development/system-design-draft.md)，环境状态见 [STATUS](../STATUS.md)。数据以 HarmonyOS/ArkTS 注入故障为主，附带 Prompt/AGENTS 文本是数据材料而非本项目工作指令。

## 1. 实际盘点结果

统计由 [只读盘点脚本](../../scripts/preparation/inspect_dfx_data.py) 生成，完整条目见 [本地清单](../../resources/preparation/data-b-inventory.json)。

| 项目 | 实际观察 | 解释 |
|---|---:|---|
| 文件 | 23,225 | 约 834 MB，包含图片、模板、测试及重复日志 |
| `.ets` 文件 | 8,866 | 是文件数量，不是有效业务模块数量 |
| 应用源码目录 | 27 | B1 为 11、B2 为 5、B3 为 11；B1 另有共享测试目录 |
| 不同应用目录名 | 26 | WebShortDrama1 在 B2/B3 都出现；同名不代表同一缺陷 |
| 候选案例变体 | 28 | B1/B2 实际故障目录 16 个，B3 README 列出 12 项 |
| 至少带一份崩溃文件的案例变体 | 26 | 仅表示有文件，尚未确认源码对应、独立性或可复现性 |
| 崩溃文件/ZIP 内日志条目 | 59 | 包括共享测试区、重复运行及历史材料；不能当 59 个独立缺陷 |
| 按原始字节去重的日志 | 57 | 仍包含同一故障多次运行 |
| ZIP | 23 | 11 个故障归档、11 个 baseline 资源包、1 个测试脚本包 |

59 份已解析日志都能提取到候选业务源码位置。其中 57 份能确定单一应用所属；53 份的首个候选业务路径直接存在且行号未越界，另 4 份可通过唯一同名文件找到候选。剩余 2 份共享 MoneyTrack 日志存在版本归属歧义。

这是**日志结构与路径可用性统计**，不是定位准确率，也不证明这些行就是根因。这里的“业务栈”通过排除已知框架路径做启发式识别，后续仍需符号与版本核验。

按日志条目统计，显式 Error name 为 TypeError 32、RangeError 19、SyntaxError 5，另 3 份缺少该字段；这些比例不能当作独立故障分布。解析也需要兼容 Reason、相对路径、设备路径、短文件名和 UTF-16。

## 2. 已证实的数据问题及处理方式

| 问题 | 本地证据 | 对我们的影响 |
|---|---|---|
| 文档数目与条目不一致 | 学生指南写 B3 11 案例，表中和 B3 README 实际均为 12 条；B1/B2 的新闻应用还有 003 故障目录 | 以“分组 + 应用版本 + 用例 + 源码快照 + 日志”建清单，不能只按标题计数 |
| B3 013 材料缺失 | README 列出 Exam1 的 013，但未找到其日志 ZIP 与 Hypium 脚本；同一源码树目前有 019 | 标记待补，不把 013 算作可评测故障 |
| B3 018 日志归档内容不对 | `Logs/故障现场/TC_JSError_018-ImageProcessing1.zip` 仅含 `MainEntry.ets` | 不能从源码伪造崩溃日志，暂不进入“日志输入”评测 |
| 017 编码及目录结构特殊 | ZIP 内直接是 `faultlogger/...`，日志带 UTF-16 BOM | 可在适配层处理；不是“没有日志” |
| 024 局部解码问题 | B3 024 日志不能完整按 UTF-8/GB18030 严格解码，错误头和源码栈可提取 | 原字节保留，解码告警留痕，不静默抹去异常 |
| 案例编号不能唯一识别故障 | 001 对应工具/商城；002 对应记账/新闻；B2/B3 的 024 分别落在分享与反馈缓存 | 不能按编号合并或直接训练查表 |
| 历史故障与新故障混杂 | 新闻 002 的旧材料指向 `ArticleFeedDetails`，共享目录的新材料指向 `HistoryVM`；MoneyTrack 也有多个栈与错误消息 | 逐个确认日志/源码/操作入口；“最新时间”不能单独证明正确归属 |
| 高度相似的应用变体 | MoneyTrack 两版 367 个同路径 `.ets` 文件中，统一换行与行尾空白后 365 个相同 | 两版应放入同一划分组，避免跨训练/验证泄漏 |
| baseline 资源包不等于完整干净工程 | 11 个 baseline ZIP 中 9 个只有用例 JSON；018 是一个源码文件，024 是两个源码文件 | 当前未发现完整干净应用或可直接安装 HAP，不能假定已有完整基线 |
| 构造报告过期 | 报告将 016/018 的应用映射对调；019 仍描述 `locateNext`，当前源码和日志为 `appendLearningOrder` | 报告适合帮助人工标注，不能未经核对就当真值 |
| 运行入口配置不一致 | Hypium `main.py` 指向 018，`user_config.xml` 指向 021，11 份 JSON 同时存在 | 接入时需要显式选择单个任务和工作目录，不直接照 README 跑入口 |

`SchoolLife1` 按指南及目录检查只有源码，没有对应故障输入。保留为待定资源，不计入故障修复分母。

## 3. 从源码看到的困难，比“找报错行”更具体

以下是静态审查支持的诊断示例，不是已验证补丁或模型成绩。

| 案例 | 日志指向与当前源码 | 实际应验证的约束 |
|---|---|---|
| BookRead2 / 004 | `BookViewListPage.ets:21` 调用 `books!.load`，初始化在下一行 | 生命周期中的初始化先于使用，修复后仍能正常显示书目 |
| ComprehensiveTool1 / 001 | `ResourceManageVM.ets:83` 读取 `list[index2].sortNum`，此前只检查部分 `index1` 条件 | 排序两端索引都应有效；不能通过取消排序来消除崩溃 |
| Exam1 / 019 | 栈顶先出现框架对象创建，业务栈反复进入 `appendLearningOrder` | 沿 nextId 的遍历应终止；需要检查循环关系及业务顺序，不能只改对象分配语句 |
| AudioEditing1 / 021 | `getFloat32` 在第 58 行越界；第 52 行循环使用 `<= totalFrames` | 读宽度、偏移、声道数、采样步长和缓冲区长度共同决定合法范围；报告里的 `getInt16` 已非当前代码 |
| HouseholdService1 / 022 | 第 22 行按字符个数分配字节，第 23 行按同样数量创建 Uint16 元素视图 | 字节数与元素数一致，且生成的校验值仍与 verify 兼容 |
| WebShortDrama1 / 024 | B2 对分享文本创建 Uint32Array；B3 对反馈缓存创建 Int32Array | 数据字节长度、视图宽度及尾部数据处理；两个变体不能互相套用文件位置 |

这些是静态观察，尚不能证明某种方法优于强模型直接分析。当前方法选择见开发设计草案。

### 待接入的两项代表应用

021 AudioEditing1 与 022 HouseholdService1 暂作为 P2 首轮接入候选：二者来自不同应用目录，分别涉及音频帧读取边界和订单校验编码；都有单一明确的 B3 ZIP 日志成员、对应的业务栈文件以及 Hypium 操作脚本。**这只是候选筛选，不是正式任务接纳或评测分母**。两项同属 B3 材料，应用谱系与跨组复用关系仍须在划分前核对。

| 候选 | 2026-09-28 静态身份核对 | 可借用的操作与现有断言 | 接入前缺口 |
|---|---|---|---|
| 021 / AudioEditing1 | ZIP 及成员原字节 SHA-256 与旧盘点相符；栈定位 `EditViewModel.ets:58`，源码行存在；应用根有 AppScope、构建配置与包配置 | `HypiumProject/testcases/TC_JSError_021.py` 进入音频调速，选择内置 demo，检查进程、目标 RangeError 与“音速：”页面 | 原版同环境复现、完整运行源码/产物绑定；页面到达不能证明 PCM/波形数值正确，需独立音频行为判据及回归 |
| 022 / HouseholdService1 | ZIP 及成员原字节 SHA-256 与旧盘点相符；栈定位 `OrderRequestChecksum.ets:23`，源码行存在；应用根有 AppScope、构建配置与包配置 | `HypiumProject/testcases/TC_JSError_022.py` 填写预约并检查订单详情页；源码还暴露 `create` 与 `verify` 的校验接口 | 原版同环境复现、完整运行源码/产物绑定；页面到达不能证明请求校验值或订单语义正确，需独立请求/校验判据及回归 |

上述 ZIP 和日志成员的完整路径及日志 SHA-256 保存在[只读盘点](../../resources/preparation/data-b-inventory.json)；9 月 28 日分别重新计算了 ZIP SHA-256（021：`0007108696426d0717ea334cc2807884e241bb209ef363e018d794be69b17f8e`；022：`98bae7821a4daed03cf6a7d5705d0eab0e004d8211b316fe5bd9ca7f16d14ce7`）及成员原字节 SHA-256（021：`e3c5caf418873eeababb0bc900f046db00366fb5e489f63c868cbbc018d86289`；022：`23389c850924ba4607d016b661eb8b38c763b30b60ca755ded5bfd515c12d80d`），均与旧盘点一致。关键源码 SHA-256 分别是 `6658bc6f664ba0f0785c23151189aaf1aebf57c6c87faaea474926dfc96f505d`、`773508dbe84f2ca5ae9e998bc3816970309ca82ad43d4f313e21a0cade76581c`；场景 Python 脚本 SHA-256 分别是 `8e25c35211f65725d5b1a3fd44faf8c418ca5d5af3302ba079991280754030e5`、`ff8fd848dfae2872cf24a0f9bb071aeebec7946513c761928a4fd1a10cbfaa52`。这些是文件身份，不是日志与运行源码属于同一次构建的证明；也没有冻结完整工程快照。

正式准备时须为每个候选显式选择一个日志成员、分析源码版本和完整运行工程，记录应用谱系组与故障变体；先实测原版目标故障和正常行为，再冻结候选无关的业务/回归协议。现有 021 脚本主要是目标回放入口；022 的订单详情断言可作为部分路径观测，但两者都不能直接充当完整业务 oracle。构造测试和含答案报告仅供准备者审查，不输入求解器，也不作为修复后必须通过的检查。

## 4. 现有测试能证明什么

**构造契约测试：** B3 的五个 Python 测试文件主要检查故障注入、脚本规范、文件内容和历史目录状态。022 的测试甚至明确要求出现 `new ArrayBuffer(payload.length)` 这一故障表达式。把它们当修复后必须通过的测试，会奖励保留错误。部分测试还依赖原作者的绝对路径、Excel 和旧基线。

**应用模板测试：** 文件很多，但抽查的 `LocalUnit.test.ets` 是 `'abc'` 包含 `'b'`、值等于自身的模板断言。不能凭测试文件数量声称有充分业务回归覆盖。

**Hypium 场景脚本：** 提供了有价值的操作路线。审查的 021 还验证目标页面到达，并对退出后的新 jscrash 做检查；但到达页面和进程存活仍不足以证明波形内容正确。应保留路径测试，补充与修复影响相关的业务断言。

这些材料不足以直接建立完整修复验收。验证层和接受条件统一见 [评测口径](metrics-and-gates.md) 与 [模块规格](../design-contracts.md)。

## 本地证据入口

- [学生指南](../../data_B/学生指南-如何阅读DFX数据集.md)
- [B3 README](../../data_B/data_B3/benchmarkfordfx-better/README.md)
- [历史注入报告：含答案且部分过期](../../data_B/data_B3/benchmarkfordfx-better/docs/故障注入报告-App与基线对比.md)
- [BookRead2 源码](../../data_B/data_B1/BookRead2/feature/book_home/src/main/ets/views/BookViewListPage.ets)
- [Exam1 递归逻辑](../../data_B/data_B3/benchmarkfordfx-better/App/Exam1/components/select_category/src/main/ets/components/MainPage.ets)
- [AudioEditing1 解析逻辑](../../data_B/data_B3/benchmarkfordfx-better/App/AudioEditing1/components/audio_common/src/main/ets/viewModel/EditViewModel.ets)
- [HouseholdService1 校验逻辑](../../data_B/data_B3/benchmarkfordfx-better/App/HouseholdService1/commons/lib_foundation/src/main/ets/http/OrderRequestChecksum.ets)
- [022 构造契约测试](../../data_B/data_B3/benchmarkfordfx-better/tests/test_tc_jserror_022_contract.py)
- [021 Hypium 场景脚本](../../data_B/data_B3/benchmarkfordfx-better/HypiumProject/testcases/TC_JSError_021.py)
