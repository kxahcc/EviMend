"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/audio_common/src/main/ets/viewModel/EditViewModel.ets";
const TARGET_MARKER = "DFX021_TARGET_PCM_VIEW_BOUNDS";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  if (!source.includes("export class EditViewModel {") || !source.includes("parsePcmFile() {")) throw new Error("UnsupportedPCMModule");
  const fragment = source.replace(/^import \{ fileIo \} from '@kit.CoreFileKit'\s*;?\n/m, "")
    .replace(/^export /gm, "").replace(/@ObservedV2\s*/g, "").replace(/@Trace\s+/g, "");
  let content = Buffer.alloc(0);
  const calls = [];
  const fileIo = { statSync(name) { calls.push(["stat", name]); return { size: content.length }; },
    openSync(name) { calls.push(["open", name]); return { fd: 17 }; },
    readSync(fd, buffer) { assert.equal(fd, 17); new Uint8Array(buffer).set(content); calls.push(["read", buffer.byteLength]); return content.length; },
    close(fd) { assert.equal(fd, 17); calls.push(["close", fd]); } };
  const Model = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn EditViewModel;})()", { filename }).runInNewContext({ fileIo }, { timeout: 1000 });
  return { Model, calls, setContent(value) { content = value; calls.length = 0; },
    sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, samples, channels, rate, expected, tail = 0) {
  const data = Buffer.alloc(samples.length * 4 + tail);
  samples.forEach((sample, index) => data.writeFloatLE(sample, index * 4));
  loaded.setContent(data);
  const model = new loaded.Model();
  model.initProps("reviewed.pcm", channels, rate, 2000);
  console.log(JSON.stringify({ pcm_input: { f32le_samples: samples, channels, sample_rate: rate, trailing_bytes: tail } }));
  const actual = Array.from(model.parsePcmFile());
  assert.equal(actual.length, expected.length, "Only complete sampled frames may produce amplitudes");
  actual.forEach((value, index) => { assert.ok(Number.isFinite(value)); assert.ok(Math.abs(value - expected[index]) < 1e-6); });
  assert.equal(model.size, data.length);
  assert.deepEqual(loaded.calls.map(call => call[0]), ["stat", "open", "read", "close"]);
  assert.equal(model.channelLayout, channels); assert.equal(model.sampleRate, rate); assert.equal(model.duration, 2000);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual audio_common module; remove reviewed fileIo import/observation metadata and erase types",
    mocked_interfaces: ["fileIo stat/open/read/close over supplied f32le PCM bytes"],
    scope: "Complete sampled mono/stereo frames, partial trailing bytes, empty input and resource-close delegation; no real file SDK, audio output, worker, canvas or UI" }));
  if (stage === "target") {
    try { check(loaded, [0.5, -0.25, 1, -0.75], 1, 2, [0.5, 1]); }
    catch (error) { if (error.name === "RangeError" && /bounds|offset|view/i.test(error.message)) console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    check(loaded, [0.5, -0.25, 0.75, -1], 2, 1, [0.375, 0.875]);
    check(loaded, [-0.25, 0.5, -0.75], 1, 1, [0.25, 0.5, 0.75]);
    check(loaded, [0, 0, 0, 0], 2, 1, [0, 0]);
  } else if (stage === "regression") {
    check(loaded, [], 1, 1, []);
    check(loaded, [0.5], 2, 1, []);
    check(loaded, [0.5, -0.25, 0.75, -1], 2, 1, [0.375, 0.875], 3);
    check(loaded, [0.5, -0.25], 1, 8, [0.5]);
  }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
