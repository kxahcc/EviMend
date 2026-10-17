"use strict";

// Evaluator-only local method protocol, outside the solver source/project.
// Fixed expectations preserve the app's existing phone prefix/length rule,
// independently visible in commons/common/src/main/ets/utils/CommonUtil.ets.
// That normal namesake is NOT the module_base file identified by the crash.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const BUILTIN_STRIP_TYPES = require("node:module").stripTypeScriptTypes;

const SOURCE_PATH = "components/module_base/src/main/ets/util/CommonUtil.ets";
const TARGET_MARKER = "DFX016_TARGET_REGEX_SYNTAX_ERROR";
const phoneVectors = [
  ["13800138000", true], ["14800138000", true], ["15800138000", true],
  ["17800138000", true], ["18800138000", true], ["19800138000", true],
  ["12800138000", false], ["16800138000", false], ["", false],
  ["1380013800", false], ["138001380000", false], ["1380013800x", false],
  [" 13800138000", false], ["13800138000 ", false], ["+8613800138000", false],
  ["１３８００１３８０００", false],
];

function compileCandidate(bytes, filename, stripTypes = BUILTIN_STRIP_TYPES) {
  if (typeof stripTypes !== "function") {
    throw new Error("UnsupportedTypeScriptRuntime: Node node:module.stripTypeScriptTypes is required");
  }
  const text = bytes.toString("utf8").replace(/^\uFEFF/, "");
  if (/\bimport\s*(?:[({*]|[A-Za-z_$])/.test(text)) {
    throw new Error("UnsupportedSourceImport: this CommonUtil protocol admits no runtime imports");
  }
  const stripped = stripTypes(text, { mode: "strip", sourceUrl: filename });
  if (!/\bexport\s+class\s+CommonUtil\b/.test(stripped)) {
    throw new Error("UnsupportedSourceExport: the CommonUtil named class export must remain");
  }
  const localClass = stripped.replace(/\bexport\s+class\s+CommonUtil\b/, "class CommonUtil");
  return new vm.Script("(() => {\n" + localClass + "\n; return CommonUtil; })()", { filename });
}

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const api = compileCandidate(bytes, filename).runInNewContext({}, { timeout: 1000 });
  for (const method of ["verifyPhoneNo", "padWithZeros", "verifyCardNo"]) {
    assert.equal(typeof api[method], "function", method + " interface missing");
  }
  return { api, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex") };
}

function run(stage, source = SOURCE_PATH) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) {
    throw new Error("UnknownCheckStage");
  }
  // A different, already-correct CommonUtil exists in this application. Never
  // substitute it for the actual module_base source named by the crash.
  if (source !== SOURCE_PATH || path.isAbsolute(source)) {
    throw new Error("SourcePathMustMatchRegisteredModule");
  }
  const { api, sourceSha256 } = loadCandidate(path.resolve(source));
  console.log(JSON.stringify({ stage, source_sha256: sourceSha256,
    transformation: "Node stripTypeScriptTypes + local named export binding",
    scope: "Actual module_base CommonUtil methods; registered app validation rule, no UI or submission coverage" }));
  if (stage === "target") {
    try {
      assert.equal(api.verifyPhoneNo("13800138000"), true, "valid phone replay rejected");
    } catch (error) {
      // Mark only the synchronous regex-construction SyntaxError from the
      // actual method. A timeout or an unrelated load error is not reproduction.
      if (error && error.name === "SyntaxError" && /regular expression|Invalid group/i.test(error.message)
          && /verifyPhoneNo/.test(error.stack || "")) {
        console.error(TARGET_MARKER);
      }
      throw error;
    }
  } else if (stage === "behavior") {
    for (const [phone, expected] of phoneVectors) {
      assert.equal(api.verifyPhoneNo(phone), expected, "phone validation rule changed for " + JSON.stringify(phone));
    }
  } else if (stage === "regression") {
    for (const [number, expected] of [[0, "00"], [1, "01"], [9, "09"], [10, "10"], [100, "100"]]) {
      assert.equal(api.padWithZeros(number), expected, "padWithZeros regression");
    }
    for (const [card, expected] of [
      ["110101200001010001", true], ["11010120000101000X", true], ["11010120000101000x", true],
      ["110101200013010001", false], ["110101200001320001", false], ["", false], ["not-a-card", false],
    ]) {
      assert.equal(api.verifyCardNo(card), expected, "existing card format rule changed");
    }
    for (const phone of ["13800138000", "19800138000", "13800138000"]) {
      assert.equal(api.verifyPhoneNo(phone), true, "repeated phone validation changed");
    }
    assert.equal(api.verifyPhoneNo("1380013800"), false, "short phone boundary changed");
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  try {
    run(process.argv[2], process.argv[3]);
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exitCode = error && /UnsupportedTypeScriptRuntime|UnsupportedSourceImport|UnsupportedSourceExport|UnknownCheckStage|SourcePathMustMatchRegisteredModule/.test(error.message) ? 2 : 9;
  }
}

module.exports = { compileCandidate, loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
