# Strict location restrictions

D2-07: the owner confirms that sirf/only Phase8 must exclude nearby phases. The 8 November place-name voice test returned DH8-101 and DH8-102 correctly but also DH7-101. A nearby label does not satisfy that explicit restriction.

Derive a backend no_nearby flag from only/sirf immediately attached to the verified location span in its own source message, including known Urdu/Devanagari forms. Store it with the location and carry it through budget-only follow-ups and location clarification. An exclusive replacement overrides an additive extraction operation, so previous areas cannot survive a sirf replacement. Unknown words or only attached to a budget do not create this flag.

Search reads the persisted restriction and excludes neighbours before fetching candidates and applying the relaxation ladder. It may return fewer than three results. The existing explicit Nearby-area action can authorize a broader search; its search-run filters record nearby_requested. Ordinary searches keep their existing labelled nearby policy. Budget/size/bedroom tolerances and ranking are unchanged.

Tests cover known script variants, source-message isolation, negation and budget-only wording, the real voice chain with an additive tool mistake, budget-follow-up persistence, exact two Phase8 results and explicit nearby authorization. No migration, provider setting, inventory or role changes. Production remains b51146c until CI and a verified release; live strict-area acceptance is required.
