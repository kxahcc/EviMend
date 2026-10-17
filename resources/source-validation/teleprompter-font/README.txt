Evaluator-only source protocol for Teleprompter1 / TC_JSError_026.

Loads the complete font-size changeValue callback from the checked ConfigSheet.ets
by unique sliderValue and builder-end anchors, then erases TypeScript syntax. The
default [10,100] interval is read from the checked PrompterConfigModel.ets, not
inferred from the candidate patch. hilog.error and ArkUI observed state are replaced
with a log sink and ordinary config/configList objects. The callback body executes
unchanged; the font-size update is not reimplemented.

The maximum valid slider value 100 reproduces the original toFixed RangeError.
The candidate must assign each normal/minimum/maximum value, preserve unrelated
color/speed/options and survive repeated endpoint changes. No-op and incorrect
clamping patches must fail. Log formatting is not asserted as a business feature.

Scope excludes actual slider event delivery, reactive rendering and devices.
Receipts must bind the original, frozen candidate and oracle before scoring.
