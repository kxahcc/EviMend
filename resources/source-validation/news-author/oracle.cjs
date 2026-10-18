"use strict";
const assert = require("node:assert/strict"), crypto = require("node:crypto"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "commons/lib_news_feed_details/src/main/ets/views/ArticleFeedDetails.ets";
const TARGET_MARKER = "DFX_NEWS_TARGET_MISSING_AUTHOR";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename), source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const start = source.indexOf("  readNewsBuilder() {"), end = source.indexOf("\n  @Builder\n  shareBuilder()", start);
  if (start < 0 || end <= start) throw new Error("UnsupportedNewsBuilderBoundary");
  const body = source.slice(start, end).replace("Row({ space: 10 }) {", "Row({ space: 10 }); {");
  const fragment = "class Page {\n" + body + "\n}", calls = [];
  const Page = new vm.Script("(() => {" + strip(fragment, { mode: "strip", sourceUrl: filename }) + "\nreturn Page;})()", { filename }).runInNewContext({ Row() {}, TextRead(value) { calls.push(value); }, handlerCoverImage(data) { return data.cover; } }, { timeout: 1000 });
  return { Page, calls, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, author, loading = false, appName = "News") {
  console.log(JSON.stringify({ news_input: { author, loading, appName } })); loaded.calls.length = 0;
  const page = new loaded.Page(), details = { id: "story", title: "Title", author, cover: "cover", content: "body" };
  Object.assign(page, { newsFeedVM: { loading, articleDetails: details }, bundleInfo: appName === undefined ? undefined : { appName }, pointSplit() {}, readBody() { return "reviewed-body"; } });
  const before = JSON.stringify(details); page.readNewsBuilder(); assert.equal(JSON.stringify(details), before);
  if (loading) { assert.equal(loaded.calls.length, 0); return; }
  assert.equal(loaded.calls.length, 1); const call = loaded.calls[0], info = call.readInfoList[0];
  assert.equal(call.currentId, "story"); assert.equal(info.id, "story"); assert.equal(info.title.text, "Title");
  assert.equal(info.bodyInfo, "reviewed-body"); assert.equal(info.imgSrc, "cover");
  assert.equal(typeof info.author.text, "string", "Missing author must still produce valid reader text");
  if (typeof author?.authorNickName === "string") assert.equal(info.author.text, author.authorNickName);
  assert.equal(call.readerParam.businessBrandInfo.panelName, appName ?? "综合新闻模板");
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage)); const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual readNewsBuilder; inert Row call/block lowering and Node type erasure", mocked_interfaces: ["Row/TextRead recorders, split/body/cover helper results and plain article state"],
    scope: "Loading guard and real reader payload construction with optional author; no actual ArkUI, reader playback, image decode or split layout" }));
  if (stage === "target") { try { check(loaded, undefined); } catch (error) { if (error.name === "TypeError" && /authorNickName/.test(error.message)) console.error(TARGET_MARKER); throw error; } }
  else if (stage === "behavior") { check(loaded, { authorNickName: "Alice" }); check(loaded, { authorNickName: "中文作者" }); check(loaded, undefined, true); }
  else if (stage === "regression") { check(loaded, null); check(loaded, {}); check(loaded, { authorNickName: "" }); }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
