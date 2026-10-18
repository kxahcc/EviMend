"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Encoder = require("node:util").TextEncoder;
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/aggregated_share/src/main/ets/views/ShareView.ets";
const TARGET_MARKER = "DFX_B2_024_TARGET_SHARE_ALIGNMENT";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "  aboutToAppear(): void {";
  const start = source.indexOf(anchor), end = source.indexOf("\n  aboutToDisappear()", start);
  if (start < 0 || end <= start || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedShareLifecycle");
  const fragment = "class Page {\n" + source.slice(start, end) + "\n}";
  const calls = [];
  class TextEncoder { encodeInto(value) { return new Encoder().encode(value); } }
  const Page = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn Page;})()", { filename }).runInNewContext({ util: { TextEncoder },
    AppStorage: { setOrCreate(key, value) { calls.push(["cache", key, Array.from(value)]); } } }, { timeout: 1000 });
  return { Page, calls, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, title, subTitle) {
  console.log(JSON.stringify({ share_input: { title, subTitle } }));
  loaded.calls.length = 0;
  const page = new loaded.Page(), data = { title, subTitle, contentHash: 999, sheetTitle: "Share", other: "preserved" };
  Object.assign(page, { data, systemShare: { init(input) { assert.equal(input, data); loaded.calls.push(["init"]); },
    onKnockShare() { loaded.calls.push(["knock"]); } },
    wxShare: { registerOnWXRespCallback(input) { assert.equal(input, data); loaded.calls.push(["wx"]); } },
    configModel: { registerWindowBottomPadding() { loaded.calls.push(["padding"]); } } });
  page.aboutToAppear();
  assert.equal(data.title, title); assert.equal(data.subTitle, subTitle); assert.equal(data.other, "preserved");
  assert.ok(Number.isInteger(data.contentHash));
  assert.deepEqual(loaded.calls, [["cache", "cacheFiles", []], ["init"], ["knock"], ["wx"], ["padding"]]);
  if (title === "" && subTitle === "") assert.equal(data.contentHash, 0);
  else assert.notEqual(data.contentHash, 0, "ordinary nonempty content must retain its distinguished share title");
  return data.contentHash;
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual aboutToAppear with local class wrapper and Node type erasure",
    mocked_interfaces: ["util.TextEncoder encodeInto backed by native UTF-8 encoder", "AppStorage/share/WX/window-padding recorders"],
    scope: "Arbitrary byte-length content fingerprint properties and complete setup delegation; no exact hash algorithm requirement, share transport or ArkUI" }));
  if (stage === "target") {
    try { check(loaded, "share", ""); }
    catch (error) { if (error.name === "RangeError" && /multiple of 4|length/i.test(error.message)) console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    const first = check(loaded, "ABCD", "");
    assert.equal(check(loaded, "ABCD", ""), first, "fingerprint must be deterministic");
    assert.notEqual(check(loaded, "ABCD", "EFGH"), first, "fingerprint must depend on the subtitle too");
    assert.notEqual(check(loaded, "WXYZ", ""), first, "fingerprint must depend on content");
  } else if (stage === "regression") {
    check(loaded, "", "");
    for (const title of ["a", "ab", "abc", "中文", "中", "😀", "x😀y"]) {
      const hash = check(loaded, title, ""); assert.equal(check(loaded, title, ""), hash);
    }
  }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
