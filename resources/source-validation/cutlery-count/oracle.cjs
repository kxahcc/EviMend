"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "features/order/src/main/ets/pages/ConfirmOrderPage.ets";
const TARGET_MARKER = "DFX020_TARGET_CUTLERY_ARRAY_LENGTH";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const builder = source.indexOf("  cutlerySheetBuilder() {");
  const next = source.indexOf("  showTimePicker() {", builder);
  const anchor = "      .onClick(() => {";
  const start = source.indexOf(anchor, builder), end = source.indexOf("\n      })", start);
  if (builder < 0 || start < builder || end <= start || next < end) throw new Error("UnsupportedCutleryCallbackBoundary");
  const fragment = (source.slice(start + "      .onClick(".length, end) + "\n}").replace(/^\(\)\s*=>/, "function ()");
  const callback = new vm.Script("(() => { " + strip("const callback = " + fragment + ";", { mode: "strip", sourceUrl: filename }) +
    "\nreturn callback;})()", { filename }).runInNewContext({ $r: value => value }, { timeout: 1000 });
  return { callback, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, index, goods, count, label = "label") {
  console.log(JSON.stringify({ cutlery_input: { index, goods, label } }));
  const state = { cutleryIndex: index, carGoods: goods, cutlery: label, selectArray: ["old"], cutlerySheetShow: true };
  const before = JSON.stringify(goods);
  loaded.callback.call(state);
  assert.equal(state.selectArray.length, count);
  const originalLabel = label || "app.string.cutlery_none";
  assert.ok(Array.from(state.selectArray).every(value => value === originalLabel));
  assert.equal(state.cutlery, count > 0 ? `${count}份` : originalLabel);
  assert.equal(state.cutlerySheetShow, false); assert.equal(JSON.stringify(goods), before);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual cutlery confirmation callback, component-this binding and Node type erasure; surrounding UI omitted",
    mocked_interfaces: ["resource-name sentinel; plain picker/cart state"],
    scope: "None/by-order/fixed picker counts, labels, array reset and sheet close with valid cart quantities; no picker delivery, order network or UI" }));
  if (stage === "target") {
    try { check(loaded, 0, [{ num: 2 }], 0); }
    catch (error) { if (error.name === "RangeError" && /array length/i.test(error.message)) console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    check(loaded, 1, [{ num: 2 }, { num: 1 }], 3); check(loaded, 2, [{ num: 8 }], 1); check(loaded, 4, [], 3);
  } else if (stage === "regression") {
    check(loaded, 1, [], 0); check(loaded, 0, [], 0, ""); check(loaded, 2, [], 1, "");
    check(loaded, 3, [{ num: 1 }], 2); check(loaded, 0, [{ num: 1 }], 0);
  }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
