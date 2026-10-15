"use strict";

// Host-owned check; keep this file outside the writable project and run tree.
// vm executes the actual candidate bytes. It is not a security sandbox.
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const stage = process.argv[2];
if (!["build", "target", "behavior", "regression"].includes(stage)) {
  console.error("Unknown check stage");
  process.exitCode = 2;
} else {
  try {
    const filename = path.resolve("src/last-value.js");
    const bytes = fs.readFileSync(filename);
    const script = new vm.Script(bytes.toString("utf8"), { filename });
    const sourceSha256 = crypto.createHash("sha256").update(bytes).digest("hex");
    console.log(JSON.stringify({ stage, source_sha256: sourceSha256 }));

    if (stage !== "build") {
      const loaded = { exports: {} };
      script.runInNewContext({ module: loaded, exports: loaded.exports }, { timeout: 1000 });
      const lastValue = loaded.exports.lastValue;
      assert.equal(typeof lastValue, "function", "lastValue export missing");

      if (stage === "target") {
        // A constant index 0 can avoid this fault; behavior checks must reject it.
        assert.equal(lastValue([17]), 17, "single-element failure remains");
      } else if (stage === "behavior") {
        assert.equal(lastValue([2, 4, 9]), 9, "last element semantics changed");
        assert.equal(lastValue(["first", "last"]), "last", "string array result changed");
      } else {
        assert.throws(() => lastValue([]), { name: "RangeError", message: "EmptyValues" });
        assert.throws(() => lastValue(null), { name: "TypeError", message: "ArrayRequired" });
        assert.equal(lastValue(Object.freeze([-8, -3, 0])), 0, "boundary or input mutation regression");
        assert.equal(lastValue([null, undefined]), undefined, "undefined is a valid stored last element");
      }
    }
    console.log(stage + " passed");
  } catch (error) {
    console.error(error && error.stack ? error.stack : String(error));
    process.exitCode = 7;
  }
}
