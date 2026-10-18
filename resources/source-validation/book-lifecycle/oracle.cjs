"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "feature/book_home/src/main/ets/views/BookViewListPage.ets";
const DATA_PATH = "components/swiper_card/src/main/ets/model/LazyDataVM.ets";
const TARGET_MARKER = "DFX004_TARGET_UNINITIALIZED_LOAD";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const dataBytes = fs.readFileSync(path.resolve(DATA_PATH));
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const data = dataBytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "  aboutToAppear(): void {";
  const start = source.indexOf(anchor), end = source.indexOf("\n  build()", start);
  const dataEnd = data.indexOf("\n@ObservedV2");
  if (start < 0 || end <= start || dataEnd < 0 || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedLifecycleBoundary");
  const fragment = data.slice(0, dataEnd).replace(/^export /gm, "") + "\nclass Page {\n" + source.slice(start, end) + "\n}";
  const calls = [];
  let params;
  const TCRouter = { getParams(route) { calls.push(route); return params; } };
  const loaded = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn { Page, LazyDataVM }; })()", { filename }).runInNewContext({ TCRouter,
    Constants: { BOOK_LIST_ROUTE: "reviewed-book-list-route" }, console: { info() {} } }, { timeout: 1000 });
  return { ...loaded, calls, setParams(value) { params = value; },
    sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    dependencySha256: crypto.createHash("sha256").update(dataBytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, page, params) {
  loaded.setParams(params);
  page.aboutToAppear();
  assert.equal(page.category, params?.category);
  assert.ok(page.books instanceof loaded.LazyDataVM);
  const expected = params?.books ?? [];
  assert.equal(page.books.totalCount(), expected.length);
  assert.deepEqual(Array.from(page.books.getAllData()), expected);
  assert.equal(loaded.calls.at(-1), "reviewed-book-list-route");
  for (let i = 0; i < expected.length; i++) assert.equal(page.books.getData(i), expected[i]);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, dependency_sha256: loaded.dependencySha256,
    fragment_sha256: loaded.fragmentSha256, transformation: "Complete actual lifecycle and LazyDataSource/LazyDataVM classes; Node type erasure; omit unrelated decorated ObservedArray",
    mocked_interfaces: ["TCRouter parameter provider and Constants route sentinel; no ArkUI observation"],
    scope: "First/repeated lifecycle, category and actual lazy-data contents; no navigation, listeners delivered by ArkUI, UI or device" }));
  if (stage === "target") {
    try { check(loaded, new loaded.Page(), { category: "novels", books: [{ id: "b1" }] }); }
    catch (error) {
      if (error.name === "TypeError" && /undefined.*load/.test(error.message)) console.error(TARGET_MARKER);
      throw error;
    }
  } else if (stage === "behavior") {
    const page = new loaded.Page();
    check(loaded, page, { category: "history", books: [{ id: "b1" }, { id: "b2" }] });
    check(loaded, page, { category: "science", books: [{ id: "b3" }] });
    check(loaded, page, { category: "empty", books: [] });
  } else if (stage === "regression") {
    for (const params of [undefined, {}, { category: "empty" }, { category: "", books: [] }]) {
      check(loaded, new loaded.Page(), params);
    }
    const page = new loaded.Page();
    check(loaded, page, { category: "first", books: [{ id: "x" }] });
    check(loaded, page, undefined);
  }
  console.log(stage + " passed");
}
if (require.main === module) {
  try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; }
}
module.exports = { loadCandidate, run, SOURCE_PATH, DATA_PATH, TARGET_MARKER };
