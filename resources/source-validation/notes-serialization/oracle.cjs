"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "common/datasource/src/main/ets/NoteService.ets";
const TARGET_MARKER = "DFX006_TARGET_CIRCULAR_JSON";
const SortEnum = { CREATE_TIME: "createTime", UPDATE_TIME: "updateTime" };
function method(source, anchor) {
  const start = source.indexOf(anchor), end = source.indexOf("\n  }", start);
  if (start < 0 || end <= start || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedNoteMethodBoundary: " + anchor);
  return source.slice(start, end + 4);
}
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const methods = ["  public async addNote(", "  public updateNote(", "  public getNoteList(", "  public search(", "  public getRecentNotesForCard("];
  const helper = source.indexOf("function putNoteInKvStore(");
  if (helper < 0) throw new Error("MissingPersistenceHelper");
  const fragment = "class Service {\n" + methods.map(anchor => method(source, anchor)).join("\n") + "\n}\n" + source.slice(helper);
  const pending = [];
  const SerializeUtil = { serialize() { return { then(callback) { pending.push(() => callback("encoded-rich-text")); } }; } };
  const Service = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn Service;})()", { filename }).runInNewContext({ SortEnum, SerializeUtil,
    hilog: { info() {}, debug() {} } }, { timeout: 1000 });
  return { Service, flush() { while (pending.length) pending.shift()(); },
    sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function setup(loaded) {
  const service = new loaded.Service(), saved = [], prefs = [];
  const categories = [{ id: "-1", totalCount: 0 }, { id: "work", totalCount: 0 }, { id: "life", totalCount: 0 }];
  Object.assign(service, { notes: [], categoryService: { getCategoryList: () => categories },
    saveNotesToPreferences(context) { prefs.push(context); } });
  return { service, saved, prefs, categories, kv: { put(key, value) { saved.push({ key, value }); } },
    ui: { getHostContext() { return "reviewed-host-context"; } } };
}
function note(id, categoryId = "work", title = "Alpha") {
  return { id, categoryId, title, description: "Body " + id, createTime: "10", updateTime: "20", styledString: {} };
}
function ids(list) { return Array.from(list, item => item.id); }
async function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual add/update/list/search/recent methods and persistence helper; Node type erasure; plain class wrapper",
    mocked_interfaces: ["plain note/category state", "hilog sink", "deferred rich-text serialization and KV recorder", "preferences call recorder and UI host-context sentinel"],
    scope: "Cycle-free list and persistence serialization, note identity/order/filter/search/counts/update; no styled-string codec, actual KV/preferences/card UI" }));
  if (stage === "build") { console.log(stage + " passed"); return; }
  const state = setup(loaded), { service, kv, ui, categories, saved, prefs } = state;
  if (stage === "target") {
    try {
      await service.addNote(note("n1"), kv, ui);
      assert.deepEqual(ids(service.getNoteList()), ["n1"]);
      loaded.flush();
      assert.equal(JSON.parse(saved[0].value).id, "n1");
    } catch (error) { if (error.name === "TypeError" && /circular|cyclic/i.test(error.message)) console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    const first = note("n1"), second = { ...note("n2", "life", "Beta"), createTime: "30", updateTime: "15" };
    await service.addNote(first, kv, ui); await service.addNote(second, kv, ui); loaded.flush();
    assert.deepEqual(ids(service.getNoteList("-1", SortEnum.CREATE_TIME)), ["n2", "n1"]);
    assert.deepEqual(ids(service.getNoteList("work", SortEnum.UPDATE_TIME, "ALPHA")), ["n1"]);
    assert.deepEqual(categories.map(item => item.totalCount), [2, 1, 1]);
    assert.equal(saved.length, 2); assert.equal(saved[0].key, "EffectiveNote_n1");
    assert.equal(JSON.parse(saved[0].value).serializedStyledString, "encoded-rich-text");
    const updated = { ...note("n1"), title: "Updated", updateTime: "50" };
    service.updateNote(updated, kv, ui); loaded.flush();
    assert.deepEqual(ids(service.getNoteList(undefined, SortEnum.UPDATE_TIME)), ["n1", "n2"]);
    assert.equal(service.getNoteList("work")[0].title, "Updated");
    assert.equal(saved.length, 3); assert.equal(JSON.parse(saved[2].value).title, "Updated");
    assert.deepEqual(categories.map(item => item.totalCount), [2, 1, 1]);
    assert.ok(prefs.includes("reviewed-host-context"));
  } else {
    await service.addNote(note("n1"), kv, ui, true); loaded.flush(); assert.equal(saved.length, 0);
    await service.addNote({ ...note("n1"), title: "Replacement" }, kv, ui); loaded.flush();
    assert.equal(service.notes.length, 1); assert.equal(saved.length, 1); assert.equal(service.notes[0].title, "Replacement");
    assert.deepEqual(categories.map(item => item.totalCount), [1, 1, 0]);
    await service.addNote(note("n2", "life", "SearchMe"));
    assert.deepEqual(ids(service.getNoteList(undefined, undefined, "searchme")), ["n2"]);
    assert.deepEqual(ids(service.getNoteList("missing")), []);
    assert.equal(JSON.parse(JSON.stringify(service.getNoteList())).length, 2);
    assert.equal(service.getRecentNotesForCard().length, 2);
  }
  console.log(stage + " passed");
}
if (require.main === module) run(process.argv[2]).catch(error => { console.error(error.stack); process.exitCode = 9; });
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
