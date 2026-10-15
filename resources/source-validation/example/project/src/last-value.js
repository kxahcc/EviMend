"use strict";

function lastValue(values) {
  if (!Array.isArray(values)) {
    throw new TypeError("ArrayRequired");
  }
  if (values.length === 0) {
    throw new RangeError("EmptyValues");
  }

  const index = values.length;
  if (index < 0 || index >= values.length) {
    throw new RangeError("LastValueIndexFault");
  }
  return values[index];
}

module.exports = { lastValue };
