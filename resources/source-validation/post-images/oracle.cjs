"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/travel_discovery/src/main/ets/model/Index.ets";
const TARGET_MARKER = "DFX012_TARGET_IMAGE_JSON";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const fragment = source.replace(/^import \{ StatusFLAG \} from '[^']+';?\n/m, "")
    .replace(/^import \{ IComment, ITravelogue \} from '[^']+';?\n/m, "")
    .replace(/@ObservedV2\s*/g, "").replace(/@Trace\s+/g, "").replace(/^export /gm, "");
  const Model = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn PostCardModel;})()", { filename }).runInNewContext({ StatusFLAG: { NO: "N", YES: "Y" } }, { timeout: 1000 });
  return { Model, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, images, expected) {
  console.log(JSON.stringify({ post_input: { images } }));
  const input = { id: 9, images, title: "Trip", content: "Body", publisher: "Author", publisherAvatar: "avatar",
    publishTime: "2026-10-18", likeNum: 2, collectNum: 3, commentNum: 4, currentUserLikeStatus: "Y", currentUserCollectStatus: "N" };
  const before = JSON.stringify(input);
  const model = new loaded.Model(input);
  assert.ok(Array.isArray(model.images), "ContentCard/PageDetail consume an image array");
  assert.ok(Array.from(model.images).every(item => typeof item === "string"));
  if (expected !== null) assert.deepEqual(Array.from(model.images), expected);
  for (const key of Object.keys(input).filter(key => key !== "images")) assert.equal(model[key], input[key]);
  assert.equal(JSON.stringify(input), before);
  return model;
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual model module, reviewed type/enum import removal, observation metadata removal and Node type erasure",
    mocked_interfaces: ["StatusFLAG NO=N/YES=Y constants"],
    scope: "PostCardModel image normalization and unrelated model fields; no image decoding, network, data-source listeners or UI" }));
  if (stage === "target") {
    try { check(loaded, "mockImage://example_post_image_1", ["mockImage://example_post_image_1"]); }
    catch (error) { if (error.name === "SyntaxError") console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    check(loaded, '["https://img/a","https://img/b"]', ["https://img/a", "https://img/b"]);
    check(loaded, ["https://img/a", "https://img/b"], ["https://img/a", "https://img/b"]);
    check(loaded, "https://img/a", ["https://img/a"]);
    check(loaded, '"https://img/a"', ["https://img/a"]);
  } else if (stage === "regression") {
    for (const input of [undefined, null, "", "[]", []]) check(loaded, input, []);
    for (const input of ["[broken", "null", "{}", "42"]) check(loaded, input, null);
    const empty = new loaded.Model(); assert.deepEqual(Array.from(empty.images), []); assert.equal(empty.id, 1);
  }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
