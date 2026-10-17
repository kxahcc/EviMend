Evaluator-only local source protocol for EnterpriseRecruitment2 / TC_JSError_009.

Executes the original or frozen candidate PropUtil.js module's own hasKey function.
Only the named export is rebound for loading in a local Node VM. No replacement
algorithm, historical-log replay, platform SDK or device is used.

Inputs follow PropUtil.d.ts (object | undefined) and the caller's Array.find result.
The target reproduces the in-operator TypeError with undefined. Behavior/regression
vectors check existing, absent, undefined-valued, null-valued, inherited and callable
object properties, dictionaries without a prototype, null/missing targets, and lack
of input mutation. A patch that only returns false cannot pass these checks.

Scope: the hasKey runtime contract. ArkUI click routing, subsequent UI cleanup,
resume persistence and end-to-end application behavior are not executed here.
Oracle bytes and checked original/candidate source must be bound by the existing
source-execution configuration and receipts before claiming this protocol passed.
