# D2-02: malformed customer location spans

Fresh C-01 on the verified1ef4080 demo image rejected Karachi despite a valid live
53-location/87-alias catalog. The failed turn's location operations contained
`office on rent in Karachi` and `around 5 lakh per month`. Bare Karachi resolved
correctly to the city subtree; the requirement clauses correctly did not.

The customer tool now rejects an update containing an unresolvable location span
shaped as an English property-intent clause or numeric money clause. It does not
extract a shorter place, relabel money, add aliases, alter model settings, or
change any inventory fact. Existing resolved/ambiguous locations and other-city
coverage checks bypass this repair classifier; ordinary unsupported place names
retain their previous behavior. Only spans grounded in the current customer text
are considered; ordinary substring verification remains authoritative elsewhere.

`invalid_location_span` is an internal function-response error with static
slot-specific guidance. The entire malformed update is rejected before persistence.
Search stays gated for that turn until a later location update is applied. The
existing iteration/time budgets bound repair; no additional provider retry loop is
introduced. The generic clarification fallback is removed by the existing renderer
when successful corrected results are available. No location/requirements values
are added to application logs.

Regression coverage includes the exact two bad clauses followed by a corrected
patch and real-worker office results, no partial writes, suppression of premature
or stale-filter searches, valid catalog aliases containing intent words, ambiguous
places, unsupported names, and other-city coverage. Local results are recorded in
the workspace execution report; full CI and reviewed live acceptance remain gates.

Review against base `1ef408038fb29c075fe26184d6036e4a45b70168`. Do not deploy
or mark D2-02 closed from offline replay alone. After reviewed release, clear the
test requirements and repeat the exact C-01 message before continuing C-02.
