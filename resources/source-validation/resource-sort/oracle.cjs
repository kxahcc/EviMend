"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const stripTypes = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/money_track/src/main/ets/viewmodels/ResourceManageVM.ets";
const TARGET_MARKER = "DFX001_TARGET_SORTNUM_UNDEFINED";
// Actual Enums.ets string values. Only selection of the two arrays is mocked;
// all bounds checks, swaps and sortNum updates execute the source method.
const BalanceChangeType = Object.freeze({ INCOME: "income", EXPENSE: "expense" });

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "");
  const anchor = "  public customSortResource(";
  const start = source.indexOf(anchor);
  const end = source.lastIndexOf("\n}");
  if (start < 0 || source.indexOf(anchor, start + anchor.length) !== -1 || end <= start ||
      !/^\n}\s*$/.test(source.slice(end))) {
    throw new Error("UnsupportedMethodBoundary: expected the reviewed final customSortResource method");
  }
  const fragment = source.slice(start, end);
  const transformed = stripTypes("class SourceMethod {\n" + fragment + "\n}", { mode: "strip", sourceUrl: filename });
  const Type = new vm.Script("(() => {\n" + transformed + "\n; return SourceMethod; })()", { filename })
    .runInNewContext({ BalanceChangeType }, { timeout: 1000 });
  assert.equal(typeof Type.prototype.customSortResource, "function");
  return { Type, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}

function instance(Type, kind, count) {
  const api = new Type();
  api.balanceType = kind;
  api.resource = { income: [], expense: [] };
  for (const category of ["income", "expense"]) {
    api.resource[category] = Array.from({ length: count }, (_, i) =>
      ({ key: category + "-" + i, sortNum: 10 + 10 * i, name: "resource " + i }));
  }
  return api;
}

function run(stage) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) throw new Error("UnknownCheckStage");
  const { Type, sourceSha256, fragmentSha256 } = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: sourceSha256, fragment_sha256: fragmentSha256,
    transformation: "Entire final customSortResource method extracted by unique header/class-end anchors, Node type erasure",
    mocked_interfaces: ["BalanceChangeType string constants; supplied income/expense resource arrays"],
    scope: "Actual integer-index guards, swap and sortNum preservation; no Grid event routing, persistence or resource lifecycle" }));
  if (stage === "target") {
    const api = instance(Type, "expense", 19);
    const before = JSON.stringify(api.resource);
    try {
      api.customSortResource(0, 19);
      assert.equal(JSON.stringify(api.resource), before, "drop on the Add slot must leave real items unchanged");
    } catch (error) {
      if (error && error.name === "TypeError" && /sortNum/.test(error.message) && /customSortResource/.test(error.stack || "")) {
        console.error(TARGET_MARKER);
      }
      throw error;
    }
  } else if (stage === "behavior") {
    for (const kind of ["income", "expense"]) {
      const api = instance(Type, kind, 3);
      const other = kind === "income" ? "expense" : "income";
      const beforeOther = JSON.stringify(api.resource[other]);
      api.customSortResource(0, 2);
      assert.deepEqual(Array.from(api.resource[kind], item => item.key), [kind + "-2", kind + "-1", kind + "-0"]);
      assert.deepEqual(Array.from(api.resource[kind], item => item.sortNum), [10, 20, 30], "sortNum must remain attached to slots");
      assert.equal(JSON.stringify(api.resource[other]), beforeOther, "unselected resource category changed");
      const swapped = JSON.stringify(api.resource);
      api.customSortResource(1, 1);
      assert.equal(JSON.stringify(api.resource), swapped, "same-index drop must preserve content");
    }
  } else if (stage === "regression") {
    for (const kind of ["income", "expense"]) {
      for (const [count, first, second] of [[0, 0, 0], [3, -1, 0], [3, 0, -1], [3, 3, 0], [3, 0, 3], [3, 50, 50]]) {
        const api = instance(Type, kind, count);
        const before = JSON.stringify(api.resource);
        api.customSortResource(first, second);
        assert.equal(JSON.stringify(api.resource), before, "invalid drop changed resource arrays");
      }
    }
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  try { run(process.argv[2]); }
  catch (error) { console.error(error && error.stack ? error.stack : String(error)); process.exitCode = 9; }
}
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
