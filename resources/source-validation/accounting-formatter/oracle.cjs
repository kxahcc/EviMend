"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/bill_chart/src/main/ets/components/BillBarChart.ets";
const TARGET_MARKER = "DFX002_TARGET_CONSTRUCTOR_WITHOUT_NEW";

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const start = source.indexOf("private _formatterBuilder:");
  const end = source.indexOf(";", start);
  const cls = source.indexOf("class MyAxisValueFormatter implements IAxisValueFormatter {");
  if (start < 0 || end <= start || cls <= end || source.indexOf("private _formatterBuilder:", start + 1) !== -1) {
    throw new Error("UnsupportedFormatterBoundary");
  }
  const fragment = source.slice(cls) + "\nclass Chart {\n" + source.slice(start, end + 1) + "\n}";
  const calls = [];
  const dayjs = reference => ({ add(offset, unit) {
    return { format(pattern) { calls.push({ reference, offset, unit, pattern }); return `${reference}/${offset}/${unit}/${pattern}`; } };
  } });
  const loaded = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn { Chart, Formatter: MyAxisValueFormatter }; })()", { filename }).runInNewContext({ dayjs }, { timeout: 1000 });
  return { ...loaded, calls, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function exercise(loaded, reference, value) {
  const formatter = new loaded.Chart()._formatterBuilder(reference);
  assert.ok(formatter instanceof loaded.Formatter, "factory must construct the actual formatter class");
  assert.equal(formatter.referenceDate, reference);
  assert.equal(formatter.getFormattedValue(value), `${reference}/${value - 1}/day/M.D`);
  assert.deepEqual(loaded.calls.at(-1), { reference, offset: value - 1, unit: "day", pattern: "M.D" });
  return formatter;
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Actual factory field and complete trailing formatter class; Node type erasure; class wrapper",
    mocked_interfaces: ["dayjs add/format delegation recorder"], scope: "Factory construction and date-formatter delegation; no dayjs calendar arithmetic, chart, UI or persistence" }));
  if (stage === "target") {
    try { exercise(loaded, "2026-10-01", 1); }
    catch (error) {
      if (error.name === "TypeError" && /class constructor.*without 'new'/i.test(error.message)) console.error(TARGET_MARKER);
      throw error;
    }
  } else if (stage === "behavior") {
    exercise(loaded, "2026-10-01", 3);
    exercise(loaded, "2026-09", 1);
    const first = exercise(loaded, "2024-02-28", 2);
    const second = exercise(loaded, "2026-12-31", 1);
    assert.notEqual(first, second);
    assert.equal(first.referenceDate, "2024-02-28");
  } else if (stage === "regression") {
    for (const reference of ["", "2024-02", "2026-01-01"]) {
      for (const value of [0, 1, 31, 1.5]) exercise(loaded, reference, value);
    }
  }
  console.log(stage + " passed");
}
if (require.main === module) {
  try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; }
}
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
