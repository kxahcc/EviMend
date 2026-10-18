Execute entire actual addNote/updateNote/getNoteList/search/getRecentNotesForCard
methods and putNoteInKvStore helper after type erasure. Plain note/category state,
logging, host-context/preferences recorders and a deferred rich-text codec stand
in for platform interfaces. The actual JSON serialization still executes in both
list logging and persistence helper; no algorithm is rewritten in another language.
The deferred then callback is explicitly drained before persistence assertions;
this does not certify real codec, Promise timing, KV/preferences or card UI.
Check cycle reproduction, contents/count/order/search/category filters, updates,
replacement by ID, persistence payloads and skipPersist. No claim about old stored
cyclic objects or full application behavior. Evaluator-only resource.
