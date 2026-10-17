"use strict";

// Evaluator-only protocol for the admitted create/verify interface. Keep this
// file outside the solver source/project and freeze its bytes before solving.
// Expectations were copied from resources/preparation/case-oracles.json,
// SHA256 ed48a6f30641f9587dc28de49c3e5377cf08bc24bc1c68727c113a0b1699c95b.
// These are fixed interface expectations, not a second checksum implementation.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const BUILTIN_STRIP_TYPES = require("node:module").stripTypeScriptTypes;

const SOURCE_PATH = "commons/lib_foundation/src/main/ets/http/OrderRequestChecksum.ets";
const TARGET_MARKER = "DFX022_TARGET_RANGE_ERROR";
const vectors = [
  {
    id: "order_ascii",
    payload: '{"goodId":"g1","addressName":"home","addressStr":"room1","contactName":"A","contactPhone":"10000000000","selectServiceId":1,"appointTime":0,"remark":"ready","actualPay":10}',
    checksum: "1e09f387",
  },
  {
    id: "order_cjk",
    payload: '{"goodId":"cleaning","addressName":"家","addressStr":"中路1号","contactName":"李","contactPhone":"13800000000","selectServiceId":1,"appointTime":0,"remark":"请提前联系","actualPay":20}',
    checksum: "2d1dee28",
  },
  {
    id: "order_supplementary_character",
    payload: '{"goodId":"cleaning","addressName":"家","addressStr":"中路1号","contactName":"李","contactPhone":"13800000000","selectServiceId":1,"appointTime":0,"remark":"请联系😀","actualPay":20}',
    checksum: "98d4a482",
  },
];

function compileCandidate(bytes, filename, stripTypes = BUILTIN_STRIP_TYPES) {
  if (typeof stripTypes !== "function") {
    throw new Error("UnsupportedTypeScriptRuntime: Node node:module.stripTypeScriptTypes is required");
  }
  let text = bytes.toString("utf8").replace(/^\uFEFF/, "");
  // This import only supplies the parameter's erased interface. Do not resolve
  // platform/application modules or quietly remove other runtime dependencies.
  const interfaceImport = /^[ \t]*import\s*\{\s*ISubmitOrderRequest\s*\}\s*from\s*(['"])\.\.\/model\/IRequest\1\s*;?[ \t]*(?:\r?\n|$)/m;
  text = text.replace(interfaceImport, "");
  if (/\bimport\s*(?:[({*]|[A-Za-z_$])/.test(text)) {
    throw new Error("UnsupportedSourceImport: only the erased IRequest interface import is admitted");
  }
  const stripped = stripTypes(text, { mode: "strip", sourceUrl: filename });
  const localClass = stripped.replace(/\bexport\s+class\s+OrderRequestChecksum\b/, "class OrderRequestChecksum");
  // vm.Script rejects other exports, decorators, or non-erasable syntax. The
  // class body is the actual candidate, not extracted/reimplemented methods.
  return new vm.Script("(() => {\n" + localClass + "\n; return OrderRequestChecksum; })()", { filename });
}

function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const script = compileCandidate(bytes, filename);
  const api = script.runInNewContext({}, { timeout: 1000 });
  assert.equal(typeof api.create, "function", "create interface missing");
  assert.equal(typeof api.verify, "function", "verify interface missing");
  assert.equal(api.HEADER_NAME, "X-Order-Checksum", "checksum header changed");
  return { api, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex") };
}

function checkVector(api, vector) {
  const request = JSON.parse(vector.payload);
  assert.equal(JSON.stringify(request), vector.payload, "registered JSON order changed");
  assert.equal(api.create(request), vector.checksum, vector.id + ": fixed checksum mismatch");
  assert.equal(api.verify(request, vector.checksum), true, vector.id + ": valid checksum rejected");
  assert.equal(api.verify(request, "0"), false, vector.id + ": wrong checksum accepted");
  const tampered = { ...request, actualPay: request.actualPay + 1 };
  assert.equal(api.verify(tampered, vector.checksum), false, vector.id + ": tampered request accepted");
  assert.equal(JSON.stringify(request), vector.payload, vector.id + ": input mutated");
}

function run(stage, source = SOURCE_PATH) {
  if (!["build", "target", "behavior", "regression"].includes(stage)) {
    throw new Error("UnknownCheckStage");
  }
  if (path.isAbsolute(source) || source.split(/[\\/]/).includes("..")) {
    throw new Error("SourcePathMustRemainInsideProject");
  }
  const filename = path.resolve(source);
  const { api, sourceSha256 } = loadCandidate(filename);
  console.log(JSON.stringify({ stage, source_sha256: sourceSha256,
    transformation: "Node stripTypeScriptTypes + erased IRequest import + local export binding",
    scope: "OrderRequestChecksum.create/verify; no PRE_ORDER, UI, device or server coverage" }));
  if (stage === "target") {
    try {
      assert.equal(api.create(JSON.parse(vectors[0].payload)), vectors[0].checksum, "ASCII replay mismatch");
    } catch (error) {
      if (error && error.name === "RangeError") {
        console.error(TARGET_MARKER);
      }
      throw error;
    }
  } else if (stage === "behavior") {
    vectors.forEach((vector) => checkVector(api, vector));
  } else if (stage === "regression") {
    // Preserve input bytes, deterministic repeated calls and the exact textual
    // checksum format; all inputs remain within the registered request shapes.
    for (const vector of [vectors[0], vectors[2], vectors[1], vectors[0]]) {
      const request = Object.freeze(JSON.parse(vector.payload));
      assert.equal(api.create(request), vector.checksum, vector.id + ": repeated/frozen input mismatch");
      assert.equal(api.verify(request, vector.checksum), true, vector.id + ": valid frozen input rejected");
      for (const invalid of ["", "0" + vector.checksum, vector.checksum.toUpperCase()]) {
        assert.equal(api.verify(request, invalid), false, vector.id + ": noncanonical checksum accepted");
      }
      assert.equal(JSON.stringify(request), vector.payload, vector.id + ": frozen input changed");
    }
  }
  console.log(stage + " passed");
}

if (require.main === module) {
  try {
    run(process.argv[2], process.argv[3]);
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exitCode = error && /UnsupportedTypeScriptRuntime|UnsupportedSourceImport|UnknownCheckStage|SourcePathMustRemainInsideProject/.test(error.message) ? 2 : 9;
  }
}

module.exports = { compileCandidate, loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
