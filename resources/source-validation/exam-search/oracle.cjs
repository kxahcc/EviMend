"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "commons/datasource/src/main/ets/ExamService.ets";
const TARGET_MARKER = "DFX014_TARGET_SEARCH_REGEX";
const EXAM_MANAGER_TYPE = Object.fromEntries(["sequence", "random", "chapter_name", "easy", "simple", "normal", "mistake", "difficult", "single", "multi", "judging", "error", "collect", "search", "mock_exam"].map((name, index) => [name, index]));
const ClassificationTypeEnum = { EASY: 0, SIMPLE: 1, MIDDLE: 2, HARD: 3, EASY_MISTAKE: 4 };
const QuestionTypeEnum = { RADIO: 0, CHECK_BOX: 1, JUDGE: 2 };
const WRONG_COLLECT = { WRONG: 0, COLLECT: 1 };
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "  getExamQuestionList(type:";
  const start = source.indexOf(anchor), end = source.indexOf("  private generateExamDetail()", start);
  if (start < 0 || end <= start || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedExamSearchBoundary");
  const fragment = "class ExamService {\n" + source.slice(start, end) + "\n}";
  const Service = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn ExamService;})()", { filename }).runInNewContext({ EXAM_MANAGER_TYPE, ClassificationTypeEnum, QuestionTypeEnum, WRONG_COLLECT }, { timeout: 1000 });
  return { Service, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function setup(loaded) {
  const service = new loaded.Service();
  service.examDetails = [
    { id: 1, question: "what * means", chapterName: "A", classificationType: 0, questionType: 0, isCorrect: false, isCollect: true },
    { id: 2, question: "question?", chapterName: "B", classificationType: 1, questionType: 1, isCorrect: true, isCollect: false },
    { id: 3, question: "period.a[0]", chapterName: "A", classificationType: 2, questionType: 2, isCorrect: true, isCollect: true },
    { id: 4, question: "normal 驾驶", chapterName: "B", classificationType: 3, questionType: 0, isCollect: false }
  ];
  return service;
}
function check(service, type, search, expected, chapter = "", wrong) {
  console.log(JSON.stringify({ exam_input: { type, search, chapter, wrong } }));
  const before = JSON.stringify(service.examDetails);
  const output = service.getExamQuestionList(type, chapter, search, wrong);
  assert.deepEqual(Array.from(output, item => item.id), expected);
  assert.equal(JSON.stringify(service.examDetails), before);
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH)), service = setup(loaded);
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual getExamQuestionList and following random helper region, class wrapper and type erasure",
    mocked_interfaces: ["declared numeric enum values; supplied question records"],
    scope: "Literal keyword search, selected sequence/chapter/category/type/error/collection branches and input immutability; no question generation, UI, persistent records or untested branches" }));
  if (stage === "target") {
    try { check(service, EXAM_MANAGER_TYPE.search, "*", [1]); }
    catch (error) { if (error.name === "SyntaxError") console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    for (const [query, expected] of [["normal", [4]], ["question", [2]], ["驾驶", [4]], ["[0]", [3]], [".", [3]]]) check(service, 13, query, expected);
    check(service, 0, "", [1, 2, 3, 4]); check(service, 2, "", [1, 3], "A");
    check(service, 11, "", [1]); check(service, 12, "", [1, 3]);
  } else if (stage === "regression") {
    for (const [query, expected] of [["?", [2]], ["(", []], ["", [1, 2, 3, 4]], ["absent", []]]) check(service, 13, query, expected);
    check(service, 3, "", [1]); check(service, 8, "", [1, 4]);
    check(service, 2, "", [1], "A", WRONG_COLLECT.WRONG);
    check(service, 2, "", [1, 3], "A", WRONG_COLLECT.COLLECT);
  }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
