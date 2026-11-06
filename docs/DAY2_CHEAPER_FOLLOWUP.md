# D2-03: search must apply a standalone cheaper request

On 7 November 2026, live C-05 on 5d2f682 repeated the three house cards after
`Kuch sasta dikhao`. Diagnostic turn `92203f30-d351-40d1-962c-0acc6fcd817e`
had no requirement patch rows. Search reused requirement version 11, maximum
80,000,000 and no exclusions. The prior shown search had the same three properties.
This confirms the missing persisted update; provider arguments were not logged.

Before searching, CustomerBinding now checks for a standalone affirmative cheaper
request and supplies its exact span to the existing requirements update when that
turn has not already handled it. The existing R17 4.5 normaliser and merge compute
the cap, exclusions and no-budget-relaxation flag. With the recorded prices,
75,000,000 x 0.97 gives 72,750,000. No new price rule or SQL filter is introduced.

An explicit cheaper update is not repeated; repeated search calls in one turn do
not apply it twice. No previous shown search asks for a budget. Update failure
holds search instead of reusing the older filters. Existing location, clarification,
inherited-state and voice-echo gates remain in force.

The recognizer accepts only a small standalone vocabulary, including the observed
Roman Urdu request and plain English `show me something cheaper`. Mixed requests,
absolute amounts, places, types, negation, conditions and questions still use normal
model extraction. This guard covers the demonstrated stale-search path; it does not
claim to solve arbitrary model omissions or all relative requirement changes.

Regression coverage includes omitted update (reproduced before the fix), explicit
update, repeated search, missing prior results, failed update, negation and Phase 8
after the repaired cheaper request. No migration, seed, dependency or deployment
configuration changes. Live C-05/C-06 acceptance is pending a validated release.
