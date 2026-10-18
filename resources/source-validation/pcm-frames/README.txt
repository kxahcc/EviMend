Load the complete actual audio_common module, removing its reviewed fileIo import,
observation metadata and types; exercise initProps and parsePcmFile. The audio_crop
class with the same filename is not substituted. Actual DataView/Float32 reads and
averaging execute; fileIo delegates to supplied f32le bytes and records open/read/
close. Test complete mono/stereo frames, decimation, signs, silence, empty input,
incomplete trailing frames/bytes and large stride. Parameters have valid positive
channel count/sample rate; invalid parameter policy is outside this protocol.
Counterexample inputs are printed with each call so a failed Agent check can be
repaired from actual feedback. No actual audio, SDK file IO, worker, canvas, device
or UI claim. Evaluator-only.
