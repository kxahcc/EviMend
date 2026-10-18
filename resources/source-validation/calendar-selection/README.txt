Replay complete actual date-click callback, retaining lexical component this and
erasing types. The dayjs recorder accepts already-ISO picker dates and records
format delegation, without implementing calendar arithmetic. Check populated,
empty/missing lists and unmatched dates still delegate selection refresh, leave
bill records intact and request the chosen date rather than suppressing refresh.
This certifies the actual callback/delegation, not StatisticsVM, DB, display state
updates or rendered calendar/card UI.
Evaluator-only; counterexample inputs are printed.
