"use strict";
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const strip = require("node:module").stripTypeScriptTypes;
const SOURCE_PATH = "features/business_home/src/main/ets/viewModels/ExpressPageVM.ets";
const TARGET_MARKER = "DFX010_TARGET_ARRAYLIST_INDEX";
const ShippingMethod = { SELF_DELIVERY: 1, CONTACT_COURIER: 2, DAILY_PICKUP: 3 };
const ShippingType = { PICKUP_SERVICE: 1, SERVICE_POINT: 2 };
const PAY_METHOD_LIST = [{ text: "寄付现结", value: 1 }, { text: "到付", value: 2 }];
class ArrayList {
  constructor() {
    this.items = [];
    return new Proxy(this, { get(target, key, receiver) {
      if (typeof key === "string" && /^\d+$/.test(key)) {
        const index = Number(key);
        if (index >= target.items.length) {
          const error = new Error("ArrayList index out of bounds"); error.name = "BusinessError"; throw error;
        }
        return target.items[index];
      }
      return Reflect.get(target, key, receiver);
    } });
  }
  add(value) { this.items.push(value); }
  get length() { return this.items.length; }
}
function loadCandidate(filename) {
  const bytes = fs.readFileSync(filename);
  const source = bytes.toString("utf8").replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");
  const anchor = "  changeTemplate(template: Template) {";
  const start = source.indexOf(anchor), end = source.indexOf("\n  // 校验地址是否填写", start);
  if (start < 0 || end <= start || source.indexOf(anchor, start + 1) !== -1) throw new Error("UnsupportedTemplateBoundary");
  const fragment = "class Model {\n" + source.slice(start, end) + "\n}";
  let hour = 10;
  class Clock extends Date { getHours() { return hour; } }
  const Model = new vm.Script("(() => {\n" + strip(fragment, { mode: "strip", sourceUrl: filename }) +
    "\nreturn Model;})()", { filename }).runInNewContext({ ArrayList, ShippingMethod, ShippingType,
    PAY_METHOD_LIST, Date: Clock }, { timeout: 1000 });
  return { Model, setHour(value) { hour = value; }, sourceSha256: crypto.createHash("sha256").update(bytes).digest("hex"),
    fragmentSha256: crypto.createHash("sha256").update(fragment).digest("hex") };
}
function template(overrides = {}) {
  return { shippingMethod: 3, pickupTime: "", paymentMethod: 1, senderAddressId: "s", senderName: "Alice",
    senderPhone: "1", senderFullAddress: "sender", senderProvince: "p", senderCity: "c", receiverAddressId: "r",
    receiverName: "Bob", receiverPhone: "2", receiverFullAddress: "receiver", receiverProvince: "p2", receiverCity: "c2", ...overrides };
}
async function exercise(loaded, input, hour, day, time) {
  loaded.setHour(hour);
  const model = new loaded.Model();
  const requests = [], prices = [];
  Object.assign(model, { senderInfo: {}, receiverInfo: {}, nearestServicePoint: { id: "point" }, selectedGrid: "g",
    selectedCompany: "previous", selectedDayText: "", selectedTime: "", selectedDayIndex: 7,
    handlerCoord(address) { requests.push(address); return Promise.resolve({ latitude: address === "sender" ? 1 : 3, longitude: 2 }); },
    getEstimatedPrice() { prices.push("called"); } });
  const before = JSON.stringify(input);
  model.changeTemplate(input);
  await Promise.resolve(); await Promise.resolve();
  assert.equal(JSON.stringify(input), before, "template must not be mutated");
  assert.equal(model.selectedTemplate, input);
  assert.equal(model.senderInfo.addressId, "s"); assert.equal(model.receiverInfo.name, "Bob");
  assert.equal(model.senderInfo.phone, "1"); assert.equal(model.receiverInfo.city, "c2");
  assert.equal(prices.length, 1);
  assert.deepEqual(requests, ["sender", "receiver"]);
  assert.equal(model.senderLatitude, 1); assert.equal(model.receiverLatitude, 3);
  assert.equal(model.selectedPayMethod.value, input.paymentMethod === 2 ? 2 : 1);
  if (input.shippingMethod === 1) {
    assert.equal(model.selectedShippingMethod, 2); assert.equal(model.selectedServicePoint, model.nearestServicePoint);
    assert.equal(model.selectedGrid, "");
  } else {
    assert.equal(model.selectedShippingMethod, 1); assert.equal(model.selectedCompany, "");
    assert.equal(model.selectedDayIndex, 0); assert.equal(model.selectedDayText, day); assert.equal(model.selectedTime, time);
  }
  if (input.goodsId) {
    assert.equal(model.selectedGoods.id, input.goodsId); assert.equal(model.selectedGoods.count, 1);
    assert.equal(model.selectedGoods.volume, input.goodsVolume); assert.equal(model.goodsName, "box 2kg");
  } else { assert.equal(model.selectedGoods, undefined); assert.equal(model.goodsName, ""); }
}
async function run(stage) {
  assert.ok(["build", "target", "behavior", "regression"].includes(stage));
  const loaded = loadCandidate(path.resolve(SOURCE_PATH));
  console.log(JSON.stringify({ stage, source_sha256: loaded.sourceSha256, fragment_sha256: loaded.fragmentSha256,
    transformation: "Complete actual changeTemplate method; class wrapper and Node type erasure",
    mocked_interfaces: ["ArrayList checked index/length/add subset", "fixed clock hour", "shipping/payment constants", "geocoding Promise and price-call recorders"],
    scope: "Template address/goods/payment/time branches and geocoding delegation; no map, price algorithm, network or UI" }));
  if (stage === "target") {
    try { await exercise(loaded, template(), 10, "", ""); }
    catch (error) { if (error.name === "BusinessError" && /ArrayList index/.test(error.message)) console.error(TARGET_MARKER); throw error; }
  } else if (stage === "behavior") {
    await exercise(loaded, template({ pickupTime: "09:00 - 18:00", goodsId: "g1", goodsName: "box", weight: 2, goodsVolume: 5, paymentMethod: 2 }), 10, "今天", "09:00 - 18:00");
    await exercise(loaded, template({ pickupTime: "09:00 - 18:00" }), 18, "今天", "一小时内");
    await exercise(loaded, template({ pickupTime: "09:00 - 18:00" }), 20, "明天", "09:00 - 18:00");
    await exercise(loaded, template({ shippingMethod: 1, paymentMethod: 99 }), 10);
  } else if (stage === "regression") {
    for (const pickupTime of [undefined, "", "09:00"]) await exercise(loaded, template({ pickupTime }), 10, "", "");
    await exercise(loaded, template({ shippingMethod: 2, pickupTime: "" }), 10, "", "");
    await exercise(loaded, template({ shippingMethod: 2, pickupTime: "09:00 - 18:00" }), 19, "今天", "一小时内");
  }
  console.log(stage + " passed");
}
if (require.main === module) run(process.argv[2]).catch(error => { console.error(error.stack); process.exitCode = 9; });
module.exports = { loadCandidate, run, SOURCE_PATH, TARGET_MARKER };
