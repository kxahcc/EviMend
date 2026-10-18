"use strict";
const assert = require("node:assert/strict"), crypto = require("node:crypto"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "commons/utils/src/main/ets/player/VideoPlayPage.ets";
const TARGET_MARKER = "DFX011_TARGET_SHARE_CONSTRUCTION";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename), source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const start = source.indexOf("  shareEvent = () => {"), end = source.indexOf("\n  aboutToAppear()", start);
  if (start < 0 || end <= start) throw new Error("UnsupportedVideoShareBoundary");
  const calls = [];
  class SharePicker { constructor(options) { calls.push({ kind: "construct", options }); } show() { calls.push({ kind: "show" }); } }
  const fragment = "class Page {\n" + source.slice(start, end) + "\n}";
  const Page = new vm.Script("(() => {" + strip(fragment, { mode: "strip", sourceUrl: filename }) + "\nreturn Page;})()", { filename }).runInNewContext({ SharePicker }, { timeout: 1000 });
  return { Page, calls, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, title, imageUrl) {
  console.log(JSON.stringify({ video_input: { title, imageUrl } })); loaded.calls.length = 0;
  const page = new loaded.Page(), info = { title, imageUrl, videoUrl: "video", id: "id" }; page.videoInfo = info;
  const before = JSON.stringify(info); page.shareEvent();
  assert.equal(loaded.calls.length, 2); assert.equal(loaded.calls[0].kind, "construct"); assert.equal(loaded.calls[1].kind, "show");
  assert.equal(loaded.calls[0].options.title, title); assert.equal(loaded.calls[0].options.coverUrl, imageUrl);
  assert.equal(JSON.stringify(info), before);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Actual complete shareEvent field, class wrapper and type erasure", mocked_interfaces: ["Native class-shaped SharePicker constructor/show recorder; plain video state"],
    scope: "Class construction, share payload, show delegation and unchanged video state; no actual sharing SDK, UI or transport" }));
  if (stage === "target") { try { check(loaded, "Video", "https://cover/a"); } catch (error) { if (error.name === "TypeError" && /class constructor.*without 'new'/i.test(error.message)) console.error(TARGET_MARKER); throw error; } }
  else if (stage === "behavior") { check(loaded, "中文视频", "https://cover/b"); check(loaded, "Changed", "https://cover/c"); }
  else if (stage === "regression") { check(loaded, "", ""); check(loaded, "😀", "mockImage://cover"); }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
