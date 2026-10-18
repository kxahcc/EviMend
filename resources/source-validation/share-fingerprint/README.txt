Execute the entire actual share lifecycle after class wrapping/type erasure. The
Kit encoder's encodeInto(string) shape returns native UTF-8 bytes; cache/share/WX/
padding calls are recorded. Native typed arrays still execute. Reproduce five-byte
alignment failure, then check deterministic content/subtitle-sensitive integer
fingerprints, empty/nonempty title behavior, Unicode byte lengths and setup order.
The internal fingerprint has no fixed external hash contract: do not force one
particular word/byte algorithm. Reject constant/dropped-subtitle repairs instead.
No share transport, ArkUI rendering or real window/device execution. Evaluator-only.
