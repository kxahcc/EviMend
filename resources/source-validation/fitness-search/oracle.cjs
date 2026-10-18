"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/search/src/main/ets/views/ResultPage.ets";
const TARGET_MARKER = "DFX015_TARGET_GROUP_NAME";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "                submitSearch:";
  const start = source.indexOf(anchor), end = source.indexOf("\n                },", start);
  if (start < 0 || end <= start || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedFitnessCallbackBoundary");
  const fragment = (source.slice(start + anchor.length, end).trim() + "\n}").replace(/^\(([^)]*)\)\s*=>/, "function ($1)");
  let city;
  const stores1 = [{ id: 1, address: "北京健身" }, { id: 2, address: "北京[VIP]" }];
  const stores2 = [{ id: 3, address: "南京健身" }, { id: 4, address: "南京(南)" }];
  const callback = new vm.Script("(() => { " + strip("const callback = " + fragment + ";", { mode: "strip", sourceUrl: filename }) +
    "\nreturn callback;})()", { filename }).runInNewContext({ AppStorage: { get() { return city; } },
    STORE_SEARCH_RESULTS1: stores1, STORE_SEARCH_RESULTS2: stores2, SearchSource: { USER_INPUT: "user-input" } }, { timeout: 1000 });
  return { callback, stores1, stores2, setCity(value) { city = value; },
    sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, city, query, expected, bp = "sm", shallow = false) {
  console.log(JSON.stringify({ fitness_input: { city, query, bp, shallow } }));
  loaded.setCity(city);
  const calls = [], previous = [{ id: 99 }], ui = {};
  const vm = { isShallowWindow: shallow, handleSearch() {}, submitSearch(...args) { calls.push(args); }, closePop() { calls.push("close"); } };
  const state = { vm, uiContext: ui, bp, storeSearchList: previous };
  const before = JSON.stringify([loaded.stores1, loaded.stores2]);
  loaded.callback.call(state, query);
  assert.deepEqual(Array.from(state.storeSearchList, item => item.id), expected);
  assert.equal(calls[0][0], ui); assert.equal(calls[0][1], query); assert.equal(calls[0][2], "user-input"); assert.equal(calls[0][3], vm.handleSearch);
  assert.equal(calls.length, ["lg", "xl"].includes(bp) && shallow ? 2 : 1);
  if (calls.length === 2) assert.equal(calls[1], "close");
  assert.equal(JSON.stringify([loaded.stores1, loaded.stores2]), before);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual submitSearch callback; explicit component-this binding and Node type erasure; surrounding ArkUI omitted",
    mocked_interfaces: ["current-city storage", "representative store arrays", "VM submit/close recorders and SearchSource sentinel"],
    scope: "Literal store address filtering, city aliases, empty/metacharacter queries, delegated search and popup branch; no UI/network" }));
  if (stage === "target") {
    try { check(loaded, "北京", "健身", [1]); }
    catch (error) { if (error.name === "SyntaxError") console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    check(loaded, "北京市", "北京", [1, 2], "lg", true);
    check(loaded, "南京", "健身", [3]); check(loaded, "南京市", "南京", [3, 4], "xl", true);
    check(loaded, "北京", "absent", []);
  } else if (stage === "regression") {
    check(loaded, "北京", "[", [2]); check(loaded, "北京", "[vip]", [2]); check(loaded, "南京", "(", [4]);
    check(loaded, "北京", "", [1, 2]); check(loaded, "other", "健身", [99]);
    check(loaded, "南京", "健身", [3], "lg", false);
  }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
