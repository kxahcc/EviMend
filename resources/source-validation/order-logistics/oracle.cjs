"use strict";
const assert = require("node:assert/strict"), crypto = require("node:crypto"), fs = require("node:fs"), path = require("node:path"), vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "features/order/src/main/ets/views/OrderInfoPage.ets";
const TARGET_MARKER = "DFX_MALL_TARGET_LOGISTICS_UNDEFINED";
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename), source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const start = source.indexOf("  buildAddressCard() {"), end = source.indexOf("\n  @Builder\n  buildPickupBlock(", start);
  if (start < 0 || end <= start) throw new Error("UnsupportedAddressCardBoundary");
  const body = source.slice(start, end).replace("ContainerColumn() {", "ContainerColumn(); {")
    .replaceAll("Row({ space: 8 }) {", "Row({ space: 8 }); {").replace(/}\s*\n(\s*)\./g, "}\n$1__ui.");
  const fragment = "class Page {\n" + body + "\n}", images = [], texts = [], clicks = [], addresses = [];
  const ui = new Proxy({}, { get(target, name) { return (...args) => { if (name === "onClick") clicks.push(args[0]); return ui; }; } });
  const Page = new vm.Script("(() => {" + strip(fragment, { mode: "strip", sourceUrl: filename }) + "\nreturn Page;})()", { filename }).runInNewContext({ __ui: ui,
    ContainerColumn: () => ui, Row: () => ui, Image(value) { images.push(value); return ui; }, Text(value) { texts.push(value); return ui; }, Divider: () => ui,
    FontWeight: { Medium: 500 }, TextOverflow: { Ellipsis: "ellipsis" }, $r: value => value,
    OrderStatusMap: { REFUND_IN_PROGRESS: "refund" }, OrderActionMap: { UPDATE_ADDRESS: "update" }, OrderUtil: { getLogisticStatusLabel: status => "status:" + status },
    buildAddressInfo(address, enabled, callback) { addresses.push({ address, enabled, callback }); } }, { timeout: 1000 });
  return { Page, images, texts, clicks, addresses, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"), fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function check(loaded, mode, details = [{ desc: "arrived" }]) {
  console.log(JSON.stringify({ order_input: { mode, details } })); for (const array of [loaded.images, loaded.texts, loaded.clicks, loaded.addresses]) array.length = 0;
  const page = new loaded.Page(), events = [], pickup = [], address = { city: "city", name: "Alice" }, logistics = { icon: "parcel", status: "delivered", details };
  page.vm = { orderItem: { orderInfo: { status: mode === "refund" ? "refund" : "normal", addressInfo: address,
    ...(mode === "pickup" || mode === "refund" ? { pickupCode: "123", pickupStoreInfo: { id: "store" } } : {}) }, logisticInfo: logistics },
    logisticInfo: mode === "missing" ? undefined : logistics, showPickupSignedRow: false, enableExpressPage: true, enableEditAddress: true,
    jumpToExpressPage() { events.push("jump"); }, handleOrderItem(action) { events.push(action); } };
  page.buildPickupBlock = (code, store) => pickup.push({ code, store });
  const before = JSON.stringify(page.vm.orderItem); page.buildAddressCard(); assert.equal(JSON.stringify(page.vm.orderItem), before);
  if (mode === "pickup" || mode === "refund") { assert.equal(pickup.length, 1); assert.equal(pickup[0].code, mode === "refund" ? "" : "123"); assert.equal(pickup[0].store.id, "store"); assert.equal(loaded.addresses.length, 0); return; }
  assert.equal(loaded.addresses.length, 1); assert.equal(loaded.addresses[0].address, address); assert.equal(loaded.addresses[0].enabled, true);
  loaded.addresses[0].callback(); assert.deepEqual(events, ["update"]);
  if (mode === "normal") {
    assert.ok(loaded.images.includes("parcel")); assert.ok(loaded.texts.includes("status:delivered")); assert.ok(loaded.texts.includes(details[0]?.desc ?? ""));
    assert.equal(loaded.clicks.length, 1); loaded.clicks[0](); assert.deepEqual(events, ["update", "jump"]);
  }
}
function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage)); const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual buildAddressCard; bounded ContainerColumn/Row block lowering and inert style-chain receiver; Node type erasure",
    mocked_interfaces: ["UI primitive/style/event recorders", "order-label helper", "pickup/address delegation and plain view-model state"],
    scope: "Logistics data access, address/edit/express delegation and pickup/refund branches; no actual ArkUI hierarchy/rendering, async view-model refresh or network" }));
  if (stage === "target") { try { check(loaded, "missing"); } catch (error) { if (error.name === "TypeError" && /icon/.test(error.message)) console.error(TARGET_MARKER); throw error; } }
  else if (stage === "behavior") { check(loaded, "normal"); check(loaded, "pickup"); check(loaded, "refund"); }
  else if (stage === "regression") { check(loaded, "normal", []); check(loaded, "missing", []); check(loaded, "normal"); }
  console.log(stage + " passed");
}
if (require.main === module) { try { run(process.argv[2]); } catch (error) { console.error(error.stack); process.exitCode = 9; } }
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
