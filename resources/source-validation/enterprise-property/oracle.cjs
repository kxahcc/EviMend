"use strict";

// Evaluator-only protocol for the actual exported hasKey implementation.
// The public .d.ts admits object | undefined; callers use Array.find results.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE_PATH = "commons/component/src/main/ets/utils/PropUtil.js";
const TARGET_MARKER = "DFX009_TARGET_IN_NON_OBJECT";

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "");
  if (/\bimport\s*(?:[({*]|[A-Za-z_$])/.test(source) ||
      (source.match(/\bexport\s+function\s+hasKey\b/g) || []).length !== 1) {
    throw new Error("UnsupportedSourceModule: expected the reviewed standalone hasKey export");
  }
  const localSource = source.replace(/\bexport\s+function\s+hasKey\b/, "function hasKey");
  const api = new vm.Script("(() => {\n" + localSource + "\n; return hasKey; })()", { filename })
    .runInNewContext({}, { timeout: 1000 });
  assert.equal(typeof api, "function");
  return { api, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex") };
}

function assertPresence(api, object, key, expected) {
  const before = Reflect.ownKeys(object).map(key => Object.getOwnPropertyDescriptor(object, key));
  assert.equal(api(object, key), expected, "property-presence behavior changed for " + key);
  assert.deepEqual(Reflect.ownKeys(object).map(key => Object.getOwnPropertyDescriptor(object, key)), before,
    "property lookup must not mutate its input");
}

function run(stage) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) {
    throw new Error("UnknownCheckStage");
  }
  const { api, sourceSha256 } = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: sourceSha256,
    transformation: "Standalone JavaScript module; only the named export binding is made local",
    scope: "Actual hasKey object/undefined contract; no ArkUI events, resume persistence or device execution" }));
  if (stage === "target") {
    try {
      assert.equal(api(undefined, "jobId"), false, "missing Array.find result must not be treated as a record");
    } catch (error) {
      if (error && error.name === "TypeError" && /in.*undefined|undefined.*in/.test(error.message)
          && /hasKey/.test(error.stack || "")) {
        console.error(TARGET_MARKER);
      }
      throw error;
    }
  } else if (stage === "behavior") {
    assertPresence(api, Object.freeze({ jobId: 7 }), "jobId", true);
    assertPresence(api, Object.freeze({}), "jobId", false);
    assertPresence(api, Object.freeze({ jobId: undefined }), "jobId", true);
    assertPresence(api, Object.freeze({ jobId: null }), "jobId", true);
    assert.equal(api(undefined, "missing"), false);
    assert.equal(api(null, "jobId"), false, "null must not reach the in operator");
  } else if (stage === "regression") {
    const inherited = Object.freeze(Object.create(Object.freeze({ jobId: 3 })));
    assertPresence(api, inherited, "jobId", true);
    const callable = function sample() {};
    callable.jobId = 3;
    Object.freeze(callable);
    assertPresence(api, callable, "jobId", true);
    const dictionary = Object.freeze(Object.assign(Object.create(null), { jobId: 0, empty: "" }));
    assertPresence(api, dictionary, "jobId", true);
    assertPresence(api, dictionary, "empty", true);
    assertPresence(api, dictionary, "missing", false);
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  try {
    run(process.argv[2]);
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exitCode = 9;
  }
}

module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
