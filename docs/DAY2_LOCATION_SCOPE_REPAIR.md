# D2-04: keep the qualified place during extraction repair

Live voice turn `45249901-5fa3-4d7d-8cda-928f3429d2ef` on f17dc3f contained
Bahria and Phase 8 in the accepted Scribe transcription, although the owner said
only Phase 8. The location update was UNKNOWN twice. A third model update supplied
only Bahria Town, which resolved, replaced Gulshan Block 13, cleared the turn's
search hold, and returned BTK-101. The resolver correctly rejected the qualified
place; the repair silently discarded its qualifier.

Both voice notes stored beds_min=2 and baths_min=3. The second explicit Defence
Phase 8 resolved correctly. Park-facing and west-open were stored as unmapped
preferences, not filters. The retained 90-lakh budget explains that turn's no-match.
This patch does not claim to improve transcription accuracy, add DD or orientation
filters, or change minimum bedroom semantics.

CustomerBinding now rejects a location span that omits an adjacent stated locality
qualifier. It also remembers grounded qualified location attempts from this turn
and rejects a shorter parent or unscoped child used as a retry. A retry that resumes
the turn can reload those attempts from its existing patch audit rows. Canonical
tokens and identifiers come from the existing location normaliser; no place or
identifier is guessed.

An incomplete span is rejected before the patch writes anything and holds search.
The tool returns an instruction to keep the complete place or ask for clarification.
A complete grounded alternative remains allowed. A later customer message can
explicitly choose the broader area; previous turns do not restrict that new request.
Normal resolver ambiguity, coverage, voice echo and search gates remain in place.

The guard applies at the customer tool boundary, where model repair happens. It
does not add aliases, silently map Bahria Phase 8 to DHA, change prices, widen
search, or change speech providers. No migration, seed, deployment or dependency
changes are required. The existing provider iteration and time limits are retained.

Regression checks reproduce the deployed class's broadening, then verify rejection
of parent-only extraction, complete alternatives, next-turn broadening, audit-based
retry recovery and the real voice-service path with fake Scribe. Live acceptance
must follow CI/review and an accepted release: an unsupported qualified place asks
for clarification without showing its broader area's listings; an explicit complete
correction uses its actual location. Current production remains f17 until deployment.
