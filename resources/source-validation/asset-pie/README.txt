Evaluator-only source protocol for MoneyTrack1_2 / TC_JSError_005 (B1).

Extracts the complete pieChartData getter and erases type syntax. The computed
decorator, DB loading and observed-state scheduling are outside this bounded
protocol. AssetChangeType uses the original string values; fund/credit groups
are supplied as ordinary state. Object.keys, all fallbacks and the getter's own
loop execute unchanged. No pie projection is substituted for candidate code.

Both empty groups reproduce the Object.keys(undefined) TypeError in the original.
Candidate checks require empty output on empty selected groups, correct fund and
credit signs, labels, resources and item order; zero balances remain present.
Fallback to a nonempty other category must not silently import its entries.
Repeated reads and type switching preserve source state and independent output.
Always-empty and wrong-sign repairs must fail. This case is distinct from B2
MoneyTrack1 with a different fault despite the shared numeric case ID.
