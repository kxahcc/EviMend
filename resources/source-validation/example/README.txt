无设备的源码回放示例

本目录展示如何让已有源码验证执行器运行候选的真实 JavaScript 字节。
它是公开的独立软件示例，不来自 data_B，不包含竞赛答案，也不代表
ArkTS/.ets 编译、HarmonyOS 平台接口仿真或任何真实应用覆盖结果。

文件职责：
  project/src/last-value.js  有意保留末元素索引越界的普通 CommonJS 程序。
  oracle.cjs                主机固定的独立检查，必须保持在 project 外。

仅需已有的普通 Node.js（支持 node: 标准库入口），无 npm 包、SDK、
设备、签名或 Codex 运行时依赖。系统仍通过现有受管执行器启动 Node，
复制 project 为原版/候选副本，冻结 oracle 的字节与命令，不在原件上修复。

四阶段使用同一个 oracle.cjs 和显式阶段参数：
  build       通过 vm.Script 检查实际源码语法。
  target      在实际导出的函数上重放单元素故障输入。
  behavior    检查多元素数字和字符串数组的最后元素，拒绝取巧的固定索引 0。
  regression  检查空输入/错误类型的既有异常、冻结数组及合法 undefined 元素。

原版的 index = values.length 会在 target 抛出 LastValueIndexFault，退出 7。
修复为 index = values.length - 1 后，四阶段都应通过。
仅改成 index = 0 虽能通过 target，却应在 behavior 被拒绝。
每阶段会输出被读取源码的 SHA256，用来核对执行的是当前原版或候选字节；
实际判断来自执行结果和断言，不依靠查找补丁字符串或硬编码“正确修改”。

配置应使用 LocalCheckConfig 的 source 模式，声明 validation_scope 为
纯 JavaScript 数组末元素逻辑检查，mocked_interfaces 显式写 []。
commands 的 argv 使用普通 Node 可执行文件绝对路径、独立 oracle.cjs
绝对路径及阶段参数；project_root 指向 project。四阶段需冻结同一 oracle
的 independent_oracles 和 oracle_sha256，并以退出 7 与上述故障标记
核验原版复现。测试 tests/test_source_replay_example.py 演示完整接线。

此示例不安装依赖、不执行任意仓库指令，也不是跨语言或平台自动转换器。
对真实 .ets 案例仍需提供对应的可执行局部逻辑/明确 mock 和独立业务判据；
缺少这种适配时只能交付分析/补丁，不能报告源码回放已经通过。
