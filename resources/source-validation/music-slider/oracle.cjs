"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const stripTypes = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/module_musicplay/src/main/ets/components/PlayerControlArea.ets";
const TARGET_MARKER = "DFX025_TARGET_REPEAT_RANGE";
// Equality-only event kinds: this protocol does not assert platform numeric values.
const modes = Object.freeze({ Begin: Symbol("Begin"), End: Symbol("End"), Moving: Symbol("Moving"), Click: Symbol("Click") });
const sha = bytes => crypto.createHash("sha256").update(bytes).digest("hex");

function loadCandidate(root) {
  const filename = path.resolve(root, SOURCE_PATH);
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "");
  const anchor = ".onChange(";
  const start = source.indexOf(anchor);
  const end = source.indexOf("\n          .height(", start);
  if (start < 0 || source.indexOf(anchor, start + anchor.length) !== -1 || end <= start) {
    throw new Error("UnsupportedCallbackBoundary: expected unique reviewed progress-slider callback");
  }
  const call = source.slice(start + anchor.length, end).trim();
  if (!call.endsWith(")")) throw new Error("UnsupportedCallbackBoundary: missing call end");
  const fragment = call.slice(0, -1).trim();
  const transformed = stripTypes("function makeCallback() { return " + fragment + "; }\nmakeCallback",
    { mode: "strip", sourceUrl: filename });
  return { make(isPlay, progressMax) {
    const logs = [], seeks = [];
    const factory = new vm.Script(transformed, { filename }).runInNewContext({
      SliderChangeMode: modes,
      Logger: { info: (...args) => logs.push(args) },
      MediaService: { getInstance: () => ({ seek: value => seeks.push(value) }) }
    }, { timeout: 1000 });
    const host = { musicModel: { isPlay, progressMax, progress: 8 } };
    return { host, logs, seeks, callback: factory.call(host) };
  }, sourceSha256: sha(bytes), fragmentSha256: sha(fragment) };
}

function invoke(api, value, mode, expectedMarker) {
  const before = JSON.stringify(api.host);
  api.callback(value, mode);
  assert.equal(JSON.stringify(api.host), before, "callback changed unrelated model state");
  const active = mode === modes.Begin || mode === modes.End;
  assert.deepEqual(api.seeks, active ? [value] : [], "seek was lost, duplicated or executed for a moving event");
  if (active && api.host.musicModel.isPlay) {
    assert.equal(api.logs.length, 1, "playing marker was lost or duplicated");
    assert.equal(api.logs[0][0], "PlayerControlArea");
    if (expectedMarker !== undefined) assert.equal(api.logs[0][1], "marker: " + expectedMarker);
    else assert.match(api.logs[0][1], /^marker: #*$/);
  } else assert.equal(api.logs.length, 0, "paused/moving event should not render a playing marker");
}

function run(stage) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) throw new Error("UnknownCheckStage");
  const loaded = loadCandidate(process.cwd());
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete onChange callback extracted by unique call/next-height anchors; Node type erasure",
    mocked_interfaces: ["SliderChangeMode equality sentinels; Logger.info capture; MediaService.seek recorder; plain musicModel"],
    scope: "Actual marker calculation, Begin/End seek and paused/moving dispatch; no audio output, player timing, rendering or device" }));
  if (stage === "target") {
    try { invoke(loaded.make(true, 1000), 1000, modes.End); }
    catch (error) {
      if (error && error.name === "RangeError" && /repeat/.test(error.stack || "")) console.error(TARGET_MARKER);
      throw error;
    }
  } else if (stage === "behavior") {
    for (const mode of [modes.Begin, modes.End]) {
      for (const [value, marker] of [[0, ""], [500, ""], [900, "#"], [999, "###"]]) {
        invoke(loaded.make(true, 1000), value, mode, marker);
      }
    }
  } else if (stage === "regression") {
    for (const isPlay of [true, false]) {
      for (const mode of [modes.Begin, modes.End, modes.Moving, modes.Click]) {
        for (const max of [0, 1, 1000]) invoke(loaded.make(isPlay, max), max, mode);
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
