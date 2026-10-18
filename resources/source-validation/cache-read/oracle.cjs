"use strict";
const assert = require("node:assert/strict"), crypto = require("node:crypto"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "components/feedback/src/main/ets/utils/FileUtils.ets";
const TARGET_MARKER = "DFX_B3_024_TARGET_CACHE_ALIGNMENT";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename), source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const start = source.indexOf("  public static verifyCacheFile(");
  if (start < 0 || source.indexOf("  public static verifyCacheFile(", start + 1) !== -1) throw new Error("UnsupportedCacheMethodBoundary");
  const fragment = "class FileUtils {\n" + source.slice(start);
  let content = Buffer.alloc(0), flags = {}, calls = [];
  const mockFs = { AccessModeType: { EXIST: 1, READ: 2 }, OpenMode: { READ_ONLY: 1 },
    accessSync(name, kind) { calls.push("access"); return kind === 1 ? flags.exists !== false : flags.readable !== false; },
    openSync() { calls.push("open"); return { fd: 7 }; }, statSync() { calls.push("stat"); return { size: content.length }; },
    readSync(fd, buffer) { assert.equal(fd, 7); calls.push("read"); if (flags.readError) throw new Error("simulated-read-failure"); new Uint8Array(buffer).set(content); return content.length; },
    closeSync(file) { assert.equal(file.fd, 7); calls.push("close"); } };
  const FileUtils = new vm.Script("(() => {" + strip(fragment, { mode: "strip", sourceUrl: filename }) + "\nreturn FileUtils;})()", { filename }).runInNewContext({ fs: mockFs, contextConstant: { AreaMode: { EL2: 2 } } }, { timeout: 1000 });
  FileUtils.getUIAbilityContext = () => ({ cacheDir: "/reviewed-cache" }); FileUtils.changeAreaMode = () => flags.area !== false;
  return { FileUtils, setInput(value, options) { content = Buffer.from(value); flags = options; calls = []; }, getCalls: () => calls,
    sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, bytes, flags = {}) {
  console.log(JSON.stringify({ cache_input: { bytes, flags } })); loaded.setInput(bytes, flags); let failure;
  try { loaded.FileUtils.verifyCacheFile("file.bin"); } catch (error) { failure = error; }
  if (flags.readError) assert.equal(failure?.message, "simulated-read-failure"); else if (failure) throw failure;
  const expected = flags.area === false ? [] : flags.exists === false ? ["access"] : flags.readable === false ? ["access", "access"] : ["access", "access", "open", "stat", "read", "close"];
  assert.deepEqual(loaded.getCalls(), expected);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage)); const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual verifyCacheFile and following safeClose region, class wrapper and type erasure", mocked_interfaces: ["context/area values and in-memory fs access/open/stat/read/close"],
    scope: "Byte alignment, actual file-read/finally-close delegation, unavailable/denied/read-failure branches; internal discarded checksum correctness is not observable or certified; no real SDK filesystem" }));
  if (stage === "target") { try { check(loaded, [1, 2, 3, 4, 5]); } catch (error) { if (error.name === "RangeError" && /multiple of 4|length/i.test(error.message)) console.error(TARGET_MARKER); throw error; } }
  else if (stage === "behavior") { check(loaded, [1, 2, 3, 4]); check(loaded, [255, 128, 0, 4, 5, 6, 7, 8]); check(loaded, [], { exists: false }); check(loaded, [], { readable: false }); }
  else if (stage === "regression") { for (const bytes of [[], [1], [1, 2], [1, 2, 3], [1, 2, 3, 4, 5, 6, 7]]) check(loaded, bytes); check(loaded, [1], { area: false }); check(loaded, [1], { readError: true }); }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
