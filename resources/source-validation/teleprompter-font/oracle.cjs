"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const stripTypes = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/teleprompter/src/main/ets/components/ConfigSheet.ets";
const OPTIONS_PATH = "components/teleprompter/src/main/ets/model/PrompterConfigModel.ets";
const TARGET_MARKER = "DFX026_TARGET_TOFIXED_RANGE";
const sha = bytes => crypto.createHash("sha256").update(bytes).digest("hex");

function loadCandidate(root) {
  const filename = path.resolve(root, SOURCE_PATH);
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "");
  const anchor = "sliderValue: this.config.fontSize,";
  const section = source.indexOf(anchor);
  const start = source.indexOf("changeValue:", section + anchor.length);
  const end = source.indexOf("\n          })", start);
  if (section < 0 || source.indexOf(anchor, section + anchor.length) !== -1 || start < 0 || end <= start) {
    throw new Error("UnsupportedCallbackBoundary: expected reviewed font-size slider property");
  }
  const fragment = source.slice(start + "changeValue:".length, end).trim();
  const transformed = stripTypes("function makeCallback() { return " + fragment + "; }\nmakeCallback",
    { mode: "strip", sourceUrl: filename });
  const options = fs.readFileSync(path.resolve(root, OPTIONS_PATH));
  const ranges = [...options.toString("utf8").matchAll(/fontSizeList\s*:\s*number\[\]\s*=\s*(\[[\d\s,]+\]);/g)];
  if (ranges.length !== 1) throw new Error("UnsupportedOptions: expected numeric default font-size interval");
  const range = JSON.parse(ranges[0][1]);
  assert.deepEqual(range, [10, 100], "default option contract changed; review vectors before scoring");
  return { make(logs) {
    const factory = new vm.Script(transformed, { filename }).runInNewContext({
      hilog: { error: (...values) => logs.push(values) }
    }, { timeout: 1000 });
    const host = { config: { fontSize: 20, fontColor: "#FFFFFF", speechRate: 3 }, configList: { fontSizeList: [...range] } };
    return { host, callback: factory.call(host) };
  }, sourceSha256: sha(bytes), fragmentSha256: sha(fragment), optionsSha256: sha(options) };
}

function check(api, value) {
  api.callback(value);
  assert.equal(api.host.config.fontSize, value, "font-size change was lost or clamped incorrectly");
  assert.equal(api.host.config.fontColor, "#FFFFFF");
  assert.equal(api.host.config.speechRate, 3);
  assert.deepEqual(api.host.configList.fontSizeList, [10, 100], "options mutated");
}

function run(stage) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) throw new Error("UnknownCheckStage");
  const loaded = loadCandidate(process.cwd());
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    options_source_sha256: loaded.optionsSha256,
    transformation: "Complete font-size changeValue callback extracted at unique slider property and builder-end anchors; Node type erasure",
    mocked_interfaces: ["hilog.error capture; plain config/configList state instead of ArkUI observation"],
    scope: "Actual callback, default interval, assigned font size and other state preservation; no rendering, reactive lifecycle or device" }));
  if (stage === "target") {
    try { check(loaded.make([]), 100); }
    catch (error) {
      if (error && error.name === "RangeError" && /digits|fraction|toFixed/.test(error.message)) console.error(TARGET_MARKER);
      throw error;
    }
  } else if (stage === "behavior") {
    for (const value of [10, 20, 50, 90, 98]) check(loaded.make([]), value);
  } else if (stage === "regression") {
    const logs = [];
    const api = loaded.make(logs);
    for (const value of [100, 10, 100, 64, 20]) check(api, value);
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  try { run(process.argv[2]); }
  catch (error) { console.error(error && error.stack ? error.stack : String(error)); process.exitCode = 9; }
}
module.exports = { loadCandidate, run, SOURCE_PATH, OPTIONS_PATH, TARGET_MARKER };
