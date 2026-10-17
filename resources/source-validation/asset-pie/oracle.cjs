"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const stripTypes = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "features/assets/src/main/ets/viewmodels/AssetAnalysisVM.ets";
const TARGET_MARKER = "DFX005_TARGET_OBJECT_KEYS_UNDEFINED";
const types = Object.freeze({ NET_ASSET: "netAsset", LIABILITY: "liability" });
const plain = value => JSON.parse(JSON.stringify(value));

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "  public get pieChartData():";
  const start = source.indexOf(anchor);
  const end = source.indexOf("\n  @Computed", start);
  if (start < 0 || source.indexOf(anchor, start + anchor.length) !== -1 || end <= start) {
    throw new Error("UnsupportedGetterBoundary: expected complete reviewed pieChartData getter");
  }
  const fragment = source.slice(start, end);
  const transformed = stripTypes("class SourceGetter {\n" + fragment + "\n}", { mode: "strip", sourceUrl: filename });
  const Type = new vm.Script("(() => {\n" + transformed + "\n;return SourceGetter;})()", { filename })
    .runInNewContext({ AssetChangeType: types }, { timeout: 1000 });
  assert.equal(typeof Object.getOwnPropertyDescriptor(Type.prototype, "pieChartData").get, "function");
  return { Type, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}

function instance(Type, kind, fund = [], credit = []) {
  const api = new Type();
  api.curType = kind;
  api.assetProcessing = { assetGroup: { fund: plain(fund), credit: plain(credit) } };
  return api;
}

function check(api, expected) {
  const before = JSON.stringify(api.assetProcessing);
  const result = api.pieChartData;
  assert.deepEqual(plain(result), expected, "pie data projection changed labels, sign, resources or order");
  assert.equal(JSON.stringify(api.assetProcessing), before, "getter changed source assets");
  if (result.length) result[0].label = "changed output";
  assert.deepEqual(plain(api.pieChartData), expected, "result leaked into source state or reused mutable output");
}

function run(stage) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) throw new Error("UnknownCheckStage");
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete pieChartData getter extracted by unique getter/next-decorator anchors; newline normalization and Node type erasure",
    mocked_interfaces: ["AssetChangeType string constants; plain fund/credit groups instead of observed DB-backed state"],
    scope: "Actual empty-state handling and fund/credit pie projection; no DB loading, observer scheduling, chart UI or device" }));
  if (stage === "target") {
    try { check(instance(loaded.Type, types.NET_ASSET), []); }
    catch (error) {
      if (error && error.name === "TypeError" && /Object.keys/.test(error.stack || "")) console.error(TARGET_MARKER);
      throw error;
    }
  } else if (stage === "behavior") {
    const fund = [{ name: "cash", amount: 120.5, subType: 1 }, { name: "offset", amount: -10, subType: 2 }];
    const credit = [{ name: "loan", amount: -80, subType: 3 }, { name: "refund", amount: 15, subType: 4 }];
    check(instance(loaded.Type, types.NET_ASSET, fund, credit), [
      { label: "cash", value: 120.5, resource: 1 }, { label: "offset", value: -10, resource: 2 }]);
    check(instance(loaded.Type, types.LIABILITY, fund, credit), [
      { label: "loan", value: 80, resource: 3 }, { label: "refund", value: -15, resource: 4 }]);
  } else if (stage === "regression") {
    for (const kind of Object.values(types)) check(instance(loaded.Type, kind), []);
    check(instance(loaded.Type, types.NET_ASSET, [], [{ name: "other", amount: -9, subType: 3 }]), []);
    check(instance(loaded.Type, types.LIABILITY, [{ name: "other", amount: 9, subType: 2 }], []), []);
    const api = instance(loaded.Type, types.NET_ASSET, [{ name: "zero", amount: 0, subType: 1 }], []);
    check(api, [{ label: "zero", value: 0, resource: 1 }]);
    api.curType = types.LIABILITY;
    check(api, []);
    api.curType = types.NET_ASSET;
    check(api, [{ label: "zero", value: 0, resource: 1 }]);
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  try { run(process.argv[2]); }
  catch (error) { console.error(error && error.stack ? error.stack : String(error)); process.exitCode = 9; }
}
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
