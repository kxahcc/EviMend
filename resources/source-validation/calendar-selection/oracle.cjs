"use strict";
const assert = require("node:assert/strict"), crypto = require("node:crypto"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "features/statistics/src/main/ets/components/CalendarSummary.ets";
const TARGET_MARKER = "DFX_B2_005_TARGET_EMPTY_DATE";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename), source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "handleDateClick:"; const start = source.indexOf(anchor), end = source.indexOf("\n        },", start);
  if (start < 0 || end <= start || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedDateSelectionCallback");
  const fragment = source.slice(start + anchor.length, end).trim() + "\n}";
  const calls = [];
  const transformed = strip("function makeCallback() { return " + fragment + "; }\nmakeCallback", { mode: "strip", sourceUrl: filename });
  const factory = new vm.Script(transformed, { filename }).runInNewContext({ dayjs(value) { return { format(pattern) { assert.equal(pattern, "YYYY-MM-DD"); calls.push(["format", value]); return value; } }; } }, { timeout: 1000 });
  return { factory, calls, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, list, date) {
  console.log(JSON.stringify({ calendar_input: { list, date } })); loaded.calls.length = 0;
  const selected = [], state = { vm: { dataProcessing: { list }, selectedDateData: "previous", getSelectedDateData(value) { selected.push(value); } } };
  const before = JSON.stringify(list); loaded.factory.call(state)(date);
  assert.deepEqual(selected, [date], "Selecting an empty date must refresh/clear the previous date too");
  assert.equal(JSON.stringify(list), before);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage)); const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual handleDateClick callback with original lexical this and type erasure", mocked_interfaces: ["ISO-date dayjs formatting delegation", "plain bill list and getSelectedDateData recorder"],
    scope: "Populated/empty/missing date selection and refresh delegation with valid ISO picker dates; no dayjs algorithm, DB, calendar UI or card rendering" }));
  if (stage === "target") { try { check(loaded, [], "2026-10-18"); } catch (error) { if (error.name === "TypeError" && /dateStr/.test(error.message)) console.error(TARGET_MARKER); throw error; } }
  else if (stage === "behavior") { check(loaded, [{ dateStr: "2026-10-18", amount: 3 }], "2026-10-18"); check(loaded, [{ dateStr: "2026-10-17", amount: 3 }], "2026-10-18"); }
  else if (stage === "regression") { check(loaded, undefined, "2026-10-18"); check(loaded, [], "2024-02-29"); check(loaded, [{ dateStr: "2024-02-29", amount: 0 }], "2024-02-29"); }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
