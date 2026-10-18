"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/select_category/src/main/ets/components/MainPage.ets";
const MODEL_PATH = "components/select_category/src/main/ets/model/SelectCateModel.ets";
const TARGET_MARKER = "DFX019_TARGET_LEARNING_CYCLE";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename), modelBytes = fs.readFileSync(path.resolve(MODEL_PATH));
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const models = modelBytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n")
    .replace(/@ObservedV2\s*/g, "").replace(/@Trace\s+/g, "").replace(/^export \{[^}]*\}\s*;?/gm, "");
  const anchor = "export struct SelectCategory {";
  const start = source.indexOf(anchor), end = source.indexOf("\n  build() {", start);
  if (start < 0 || end <= start || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedLearningBoundary");
  const body = source.slice(start + anchor.length, end).replace(/@(Param|Require|Local|Event)\s*/g, "");
  const fragment = models + "\nclass Page {\n" + body + "\n}";
  const Page = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn Page;})()", { filename }).runInNewContext({ $r: name => name }, { timeout: 1000 });
  return { Page, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    dependencySha256: crypto.createHash("sha256").update(modelBytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function item(id, nextId = "") { return { id, title: "title-" + id, nextId }; }
function check(loaded, source, expected) {
  const before = JSON.stringify(source), page = new loaded.Page();
  page.secondParamObj = { list: ["category"], secondItem: { list: [{ id: "chapter", title: "Chapter", list: source }] } };
  console.log(JSON.stringify({ learning_input: source }));
  page.initDataSource();
  assert.deepEqual(Array.from(page.contentData[0].list, node => node.id), expected);
  for (const node of page.contentData[0].list) {
    const original = source.find(value => value.id === node.id);
    assert.equal(node.title, original.title); assert.equal(node.nextId, original.nextId);
    assert.notEqual(node, original, "output remains a distinct display object");
  }
  assert.equal(page.contentData[0].id, "chapter"); assert.equal(page.contentData[0].title, "Chapter");
  assert.equal(page.secondDataList, page.secondParamObj.list);
  assert.equal(JSON.stringify(source), before);
  page.initDataSource();
  assert.deepEqual(Array.from(page.contentData[0].list, node => node.id), expected);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, dependency_sha256: loaded.dependencySha256,
    fragment_sha256: loaded.fragmentSha256, transformation: "All actual page fields/lifecycle/helpers before UI build plus model classes; strip observation/parameter metadata and types",
    mocked_interfaces: ["plain secondParamObj instead of ArkUI Param delivery; resource-name sentinel"],
    scope: "Ordered learning links/fallback, cycles/missing targets, model copies, repeated initialization and input immutability; no UI/navigation" }));
  if (stage === "target") {
    try { check(loaded, [item("a", "b"), item("b", "a")], ["a", "b"]); }
    catch (error) { if (error.name === "RangeError" && /stack|recursion/i.test(error.message)) console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    check(loaded, [item("a"), item("b"), item("c")], ["a", "b", "c"]);
    check(loaded, [item("a", "c"), item("b"), item("c")], ["a", "c"]);
    check(loaded, [item("a", "b"), item("b", "c"), item("c")], ["a", "b", "c"]);
  } else if (stage === "regression") {
    check(loaded, [], []); check(loaded, [item("a", "a")], ["a"]);
    check(loaded, [item("a", "missing"), item("b")], ["a"]);
    check(loaded, [item("a"), item("b", "c"), item("c", "b")], ["a", "b", "c"]);
  }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
