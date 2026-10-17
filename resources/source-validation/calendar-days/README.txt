Evaluator-only source protocol for Calendar1 / TC_JSError_007.

Extracts the entire calculateDaysBetweenDates function at unique exported-function
and next-function anchors. Newlines are normalized and Node erases type syntax;
Date, BigInt, subtraction, Math.abs and rounding execute the original/candidate
body. No lunar library, replacement algorithm, platform mock or device is used.

The log identifies a BigInt-to-number TypeError but does not record date inputs.
A representative two-day interval reproduces that operation and exception in the
original. Candidate expectations follow an absolute whole-day Number interval.
Checks cover equal/reversed dates, leap day, year crossing, fractional days,
23:59:59/24/25-hour boundaries, long ranges and repeated calls, with UTC timestamps
to avoid host timezone ambiguity. Always-constant, signed and unrepaired patches
must fail. Date-picker events, lunar conversion and whole-app behavior are outside
this protocol; invalid date-string behavior is not claimed.
