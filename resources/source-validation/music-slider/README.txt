Evaluator-only source protocol for Music1 / TC_JSError_025.

Loads the complete progress-slider onChange callback from the checked .ets bytes
using the unique callback and next height-call anchors, then erases type syntax.
Actual repeat/log10 calculations, branches and seek calls run unchanged. The
musicModel is supplied as plain state; Logger and MediaService record calls.
SliderChangeMode uses distinct equality sentinels. Platform enum numeric values,
UI dispatch, actual audio, asynchronous playback timing and devices are not tested.

Original target: playing, progressMax=1000, End event value=1000; String.repeat
throws RangeError for an infinite count. A candidate must retain the seek. Normal
checks require the original finite markers at 0/500/900/999 and the correct seek
for both Begin and End. Regression checks zero/one/normal duration, paused state,
and no seek or marker on Moving/Click. Unrelated model state must remain unchanged.
Removing the callback, seek or marker behavior must not pass.
