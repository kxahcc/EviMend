"use strict";

// Evaluator-only protocol for the actual DataManager class. Expectations come
// from queryTasks/loadTodosByState, RequestProxy.queryTasks and AgencyTaskView,
// not the benchmark construction instructions or a reimplemented query engine.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const BUILTIN_STRIP_TYPES = require("node:module").stripTypeScriptTypes;

const SOURCE_PATH = "scenes/agency/src/main/ets/mock/DataManager.ets";
const TARGET_MARKER = "DFX017_TARGET_REGEX_SYNTAX_ERROR";
const tasks = [
  { taskID: "task-a", parentID: "root", title: "Alpha [x]", isFinished: false },
  { taskID: "task-b", parentID: "root", title: "alpha archived", isFinished: true },
  { taskID: "task-c", parentID: "root", title: "Beta (test)", isFinished: false },
  { taskID: "task-d", parentID: "root", title: "请联系客户", isFinished: false },
  { taskID: "task-e", parentID: "root", title: "", isFinished: false },
  { taskID: "task-f", parentID: "root", isFinished: false },
  { taskID: "task-g", parentID: "other", title: "Alpha no state" },
];

function compileCandidate(bytes, filename, stripTypes = BUILTIN_STRIP_TYPES) {
  if (typeof stripTypes !== "function") {
    throw new Error("UnsupportedTypeScriptRuntime: Node node:module.stripTypeScriptTypes is required");
  }
  let text = bytes.toString("utf8").replace(/^\uFEFF/, "");
  const unusedPreferences = /^[ \t]*import\s+preferences\s+from\s*(['"])@ohos\.data\.preferences\1\s*;?[ \t]*(?:\r?\n|$)/m;
  const taskType = /^[ \t]*import\s+(?:type\s+)?\{\s*TaskItem\s*\}\s*from\s*(['"])\.\.\/types\/Types\1\s*;?[ \t]*(?:\r?\n|$)/m;
  text = text.replace(unusedPreferences, "").replace(taskType, "");
  // The reviewed original imports preferences but never uses its binding.
  // Removing an import is NOT a mock for a newly used platform operation.
  if (/\bpreferences\b/.test(text)) {
    throw new Error("UnsupportedPlatformDependency: preferences must remain unused in this local protocol");
  }
  if (/\bimport\s*(?:[({*]|[A-Za-z_$])/.test(text)) {
    throw new Error("UnsupportedSourceImport: only the reviewed unused preferences and TaskItem imports are admitted");
  }
  const stripped = stripTypes(text, { mode: "strip", sourceUrl: filename });
  if (/\bTaskItem\b/.test(stripped)) {
    throw new Error("UnsupportedTaskItemRuntimeUse: TaskItem is admitted only as an erased interface type");
  }
  if (!/\bexport\s+class\s+DataManager\b/.test(stripped)) {
    throw new Error("UnsupportedSourceExport: the DataManager named class export must remain");
  }
  const localClass = stripped.replace(/\bexport\s+class\s+DataManager\b/, "class DataManager");
  return new vm.Script("(() => {\n" + localClass + "\n; return DataManager; })()", { filename });
}

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const api = compileCandidate(bytes, filename).runInNewContext({}, { timeout: 1000 });
  for (const method of ["saveTodos", "loadTodos", "loadTodosByState", "queryTasks", "queryById", "querySubTasks"]) {
    assert.equal(typeof api[method], "function", method + " interface missing");
  }
  return { api, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex") };
}

function checkQuery(api, keyword, finished, expectedIds, inventory) {
  const found = api.queryTasks(keyword, finished);
  assert.equal(Array.isArray(found), true, "queryTasks must retain its synchronous array interface");
  assert.deepEqual(Array.from(found, (item) => item.taskID), expectedIds,
    "queryTasks returned wrong IDs for " + JSON.stringify({ keyword, finished }));
  assert.equal(JSON.stringify(api.loadTodos()), inventory, "queryTasks changed stored task inventory");
}

async function run(stage, source = SOURCE_PATH) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) {
    throw new Error("UnknownCheckStage");
  }
  if (source !== SOURCE_PATH || path.isAbsolute(source)) {
    throw new Error("SourcePathMustMatchRegisteredModule");
  }
  const { api, sourceSha256 } = loadCandidate(path.resolve(source));
  console.log(JSON.stringify({ stage, source_sha256: sourceSha256,
    transformation: "Node stripTypeScriptTypes + erased TaskItem interface/unused preferences import + local export binding",
    scope: "Actual DataManager Map/query methods; fixed task inputs, no preferences persistence or UI coverage" }));
  if (stage === "build") {
    console.log(stage + " passed");
    return;
  }
  // Actual save/load methods initialize the actual class's own Map. A JSON
  // clone only supplies fixed inputs; it does not implement filtering/storage.
  await api.saveTodos(JSON.parse(JSON.stringify(tasks)));
  const inventory = JSON.stringify(api.loadTodos());
  assert.equal(inventory, JSON.stringify(tasks), "saveTodos/loadTodos changed registered inputs");
  if (stage === "target") {
    try {
      checkQuery(api, "Alpha", false, ["task-a"], inventory);
    } catch (error) {
      if (error && error.name === "SyntaxError" && /regular expression|unterminated group|capture/i.test(error.message)
          && /queryTasks/.test(error.stack || "")) {
        console.error(TARGET_MARKER);
      }
      throw error;
    }
  } else if (stage === "behavior") {
    for (const [keyword, finished, expected] of [
      ["alpha", false, ["task-a"]], ["ALPHA", true, ["task-b"]],
      ["[x]", false, ["task-a"]], ["(", false, ["task-c"]], ["请", false, ["task-d"]],
      ["$", false, []], ["", false, ["task-a", "task-c", "task-d"]],
      ["", true, ["task-b"]], ["missing", false, []],
    ]) {
      checkQuery(api, keyword, finished, expected, inventory);
    }
  } else {
    for (const [keyword, expected] of [
      [".", []], ["[", ["task-a"]], [")", ["task-c"]], ["  ", []], ["ALPHA", ["task-a"]],
    ]) {
      checkQuery(api, keyword, false, expected, inventory);
    }
    assert.equal(api.queryById("task-c").taskID, "task-c", "existing queryById changed");
    assert.equal(api.queryById("not-present"), undefined, "missing ID no longer returns undefined");
    assert.deepEqual(Array.from(api.querySubTasks("root"), (item) => item.taskID),
      ["task-a", "task-b", "task-c", "task-d", "task-e", "task-f"], "existing subtask lookup changed");
    assert.equal(JSON.stringify(api.loadTodos()), inventory, "query methods changed stored task inventory");
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  // A pending Promise alone does not keep Node alive. Keep a bounded timer so
  // an uncompleted saveTodos cannot exit 0 before any assertions are reached.
  // This is a refusal deadline, never evidence for the target SyntaxError.
  const completion = setTimeout(() => {
    console.error("OracleAsyncCompletionTimeout: local Map initialization did not complete");
    process.exitCode = 2;
  }, 5000);
  run(process.argv[2], process.argv[3]).then(() => clearTimeout(completion), (error) => {
    clearTimeout(completion);
    console.error(error && error.stack ? error.stack : String(error));
    process.exitCode = error && /UnsupportedTypeScriptRuntime|UnsupportedPlatformDependency|UnsupportedTaskItemRuntimeUse|UnsupportedSourceImport|UnsupportedSourceExport|UnknownCheckStage|SourcePathMustMatchRegisteredModule/.test(error.message) ? 2 : 9;
  });
}

module.exports = { compileCandidate, loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
