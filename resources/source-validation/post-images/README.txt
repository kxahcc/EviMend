Load actual complete model module with reviewed imports/observation metadata/types
removed. StatusFLAG values are supplied. Exercise PostCardModel constructor only.
The supplied raw mockImage URI reproduces SyntaxError. Consumers ContentCard and
PageDetail use images as an array; require preservation of ordinary URI/array/JSON
array input, scalar JSON string normalization, empty defaults and unrelated fields.
For malformed/scalar-object JSON require a non-crashing string array, without
inventing a particular invalid-image fallback. Print failing inputs for feedback.
No image rendering/decoding/network/data-source listener or complete app coverage.
Evaluator-only; do not add the expected normalization or repair to solver inputs.
