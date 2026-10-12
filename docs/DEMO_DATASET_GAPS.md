# Demo dataset: open decisions and gaps (Task 4.3)

Status 2026-10-09, after the checkpoint 1 corrections. None of these is written into a
numeric or enum field; the affected cells are blank or use a symbolic reference.

## Settled by the owner (2026-10-09)

- **Staff reuse.** The existing production agent (the owner's phone, demo agency) is the
  dataset's agent: matched by (agency, phone) and reused with its name, role and phone
  unchanged. No fourth staff row; existing staff are never renamed. "Admin (DEMO)" and
  "Staff (DEMO)" are acceptable names for rows the loader creates.
- **Freshness.** DH6-104 at 35 days old, set only on first insertion; reruns keep every
  existing `last_confirmed_at`.
- **Reruns.** Insert missing rows only; never update existing rows, so later edits survive.
- **DHA neighbours.** 5-6, 6-7, 7-8 (R17's "DHA 6 <-> 7 <-> 8" read as a chain; a 6-8 pair
  would break Demo Scope scenario 2, and a test guards this).
- **Canonical names.** Each location has an `official` alias for its canonical name
  (mechanically derived, see DEMO_DATASET.md).

## Remaining before a production seed run

Nothing blocks the loader code or its disposable-database tests.

- **Admin phone (production runtime only).** No allowlisted test phone is the admin's
  (Demo Scope p5 lists customer A, customer B / client, staff, broker, spare).
  `env:DEMO_ADMIN_PHONE` must have a value before the loader runs against production.
  Disposable-database tests use synthetic values.
- **Loader:** implemented and tested on a disposable database (checkpoint 2,
  docs/DEMO_SEED.md); not run against production.

## Needed before search work, not for loading

- **Bahria 10 marla.** D11 p14 expects BTK-103 and BTK-104 for "Bahria 10 marla ka ghar",
  but R17's +/- 10% rule on the 250 bucket (225-275) also admits BTK-102 (235 sq yd). The
  listing sizes are unchanged and the validator checks only D11's exact-bucket expectation;
  which rule search applies is open and unverified.
- **Clifton neighbours.** R17 p3 "Clifton Blk 2/4/5 <-> Blk 8/9" could mean every cross pair
  (6 pairs) or something narrower. Not included until decided.
- **Bahria Precinct 6.** Settled 2026-10-12 (owner, Task 7.1): added as `btk-p6` with its
  official alias and R17's context alias `p6` (Bahria); no listing, no neighbour.
  docs/DECISIONS.md.

## Optional gaps (blank on purpose)

- **Urdu-script aliases.** D11 p6 shows Urdu forms for dha, phase, clifton, bahria (as
  transliteration examples), and D11 section 7 asks for Urdu-script and voice-STT aliases per
  area. The extracted PDF text could not be checked against a rendered page, so these are
  omitted until visually verified or supplied by the owner. The validator accepts Urdu text;
  a test loads a valid Urdu alias. Update 2026-10-12 (Task 7.1): the four D11 forms were
  decoded (visual-order presentation forms reversed + NFKC) and are used by the resolver's
  term table in app/location.py, not seeded as `urdu_script` aliases; per-area Urdu and
  voice-STT aliases remain a gap.
- Further aliases for other locations; alias `kind` for the sourced examples (all `common`)
  and weights (all 1, schema default).
- Centroids, developer names, project amenities, approval notes, titles, descriptions,
  address details, owner names / phones, images (Demo Scope p4: 4-6 per listing, watermarked).
- DH8-102 construction status (the override row omits it); DH8-101's original "New build" note
  has no column and was dropped with the override.
- Plot category (BTK-106, DHC-101); Tariq Road `is_commercial_zone` (left false, unstated).
- Investment tags beyond PRJ-103's `rental_income` (CLF-102 rental yield, DHC-101
  instalments, BTK-106 file are not tagged; their typed facts are set).
- CLF-101 "ground floor" (floor numbering convention not stated); PRJ down-payment amounts
  (percentages only); PRJ-P2 completion date.
- DH6-106 "Token received" kept as `internal_notes` (internal only).
