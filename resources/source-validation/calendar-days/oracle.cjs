"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const stripTypes = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/date_calculation/src/main/ets/utils/Utils.ets";
const TARGET_MARKER = "DFX007_TARGET_BIGINT_MATH_FLOOR";

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "export function calculateDaysBetweenDates(";
  const start = source.indexOf(anchor);
  const end = source.indexOf("\nexport function getLunarDay(", start);
  if (start < 0 || source.indexOf(anchor, start + anchor.length) !== -1 || end <= start) {
    throw new Error("UnsupportedFunctionBoundary: expected complete reviewed date-difference function");
  }
  const fragment = source.slice(start, end).replace(/^export /, "");
  const transformed = stripTypes(fragment, { mode: "strip", sourceUrl: filename });
  const calculate = new vm.Script("(() => {\n" + transformed + "\n;return calculateDaysBetweenDates;})()",
    { filename }).runInNewContext({}, { timeout: 1000 });
  assert.equal(typeof calculate, "function");
  return { calculate, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}

function check(calculate, first, second, expected) {
  const result = calculate(first, second);
  assert.equal(typeof result, "number", "date interval must remain a Number");
  assert.equal(result, expected, "date interval must be absolute and rounded down to whole days");
}

function run(stage) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) throw new Error("UnknownCheckStage");
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete exported date-difference function extracted by function/next-function anchors; newline normalization and Node type erasure",
    mocked_interfaces: [], scope: "Actual Date, BigInt and Math operations; representative repro input and finite UTC date cases; no lunar/date-picker/UI coverage" }));
  if (stage === "target") {
    try { check(loaded.calculate, "2026-10-10", "2026-10-12", 2); }
    catch (error) {
      if (error && error.name === "TypeError" && /BigInt/.test(error.message)) console.error(TARGET_MARKER);
      throw error;
    }
  } else if (stage === "behavior") {
    for (const args of [
      ["2026-10-10", "2026-10-10", 0], ["2026-10-13", "2026-10-10", 3],
      ["2024-02-28", "2024-03-01", 2], ["2023-12-31", "2024-01-02", 2],
      ["2026-10-10T00:00:00Z", "2026-10-10T12:00:00Z", 0],
      ["2026-10-10T00:00:00Z", "2026-10-11T12:00:00Z", 1]
    ]) check(loaded.calculate, ...args);
  } else if (stage === "regression") {
    for (const [first, second, expected] of [
      ["2026-10-10T00:00:00Z", "2026-10-10T23:59:59Z", 0],
      ["2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z", 1],
      ["2026-10-10T00:00:00Z", "2026-10-11T01:00:00Z", 1],
      ["2000-01-01", "2020-01-01", 7305]
    ]) {
      check(loaded.calculate, first, second, expected);
      check(loaded.calculate, second, first, expected);
      check(loaded.calculate, first, second, expected);
    }
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  try { run(process.argv[2]); }
  catch (error) { console.error(error && error.stack ? error.stack : String(error)); process.exitCode = 9; }
}
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
