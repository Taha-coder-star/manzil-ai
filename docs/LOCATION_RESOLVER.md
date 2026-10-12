# Location resolver (Task 7.1, component C7)

Status (2026-10-12): **deterministic stage** implemented and tested on
`feature/task-7-1-location-resolver` (from `origin/main` 018c328, after PR #30), with
the owner decisions of 2026-10-12 (docs/DECISIONS.md, "Task 7.1"). Pure code in
`app/location.py`: no database, no LLM, no logging, no migration, no new dependency.
One seed change: Bahria Precinct 6 (demo/; production needs an owner-run seed).
Nothing calls the resolver yet; 7.3 (requirement state) is the first caller.

Fuzzy matching (R17 3.3 step 4, global and scoped) and the read-only database
snapshot + process cache (`app/gazetteer.py`) are implemented and tested locally.
Not wired into the worker yet (7.3).

Sources: R17 section 3 (3.1 output, 3.2 gazetteer additions, 3.3 steps, 3.4 auto vs
must-ask table), Data Model (R11-R13) sections 2-3 and 7, Red-Team R-22 / R-23, the
6.2/6.3 handoff section 5, Execution Plan 7.1, the Task 7.1 discovery report and the
owner decisions of 2026-10-12.

## API

```python
from app import demo_dataset, location
gazetteer = location.Gazetteer.from_dataset(demo_dataset.load())   # tests; IDs = dataset keys
result = location.resolve("DHA chay", active_area_id=None, gazetteer=gazetteer)
result.to_dict()
# {'nodes': ['dha-ph6'], 'mode': 'EXACT', 'city': 'KHI', 'status': 'RESOLVED',
#  'raw': 'DHA chay', 'method': 'exact', 'key': 'dha 6'}
```

`resolve(raw_span, active_area_id, gazetteer, *, city_code="KHI") -> LocationResult`

- `raw_span`: the customer's words as the LLM passed them. Returned unchanged in `raw`.
- `active_area_id`: any node of the current requirement location, or `None`. The
  AREA above it is the context (7.3 can pass `gli-b13`; "block 6" then means Gulshan
  Block 6). An ID the gazetteer does not know raises `ValueError`.
- `city_code`: the agency's `city_default` (agency, migration 007).
- `Gazetteer(places, aliases)` is an immutable snapshot of `location` +
  `location_alias`; it validates the tree (levels, parents, no cycles) and aliases
  (known location, context is an area, usable key) and raises `GazetteerError`.
  IDs are opaque (UUIDs from the database, dataset keys in tests).

## Result contract (R17 3.1)

`LocationResult` (frozen) validates itself on construction (`ValueError`, or
`TypeError` for wrong types):

| status | nodes | mode | candidates | extras |
|---|---|---|---|---|
| RESOLVED | >= 1 ID | EXACT / SUBTREE / ANYWHERE_IN_CITY | none | |
| AMBIGUOUS | none | None | >= 1, unique | |
| UNKNOWN | none | None | none | optional `area_hint` (retained area context) |
| OUT_OF_COVERAGE | none | None | none | `other_city` (required) |

- `mode`: city node -> ANYWHERE_IN_CITY, area -> SUBTREE (R17 3.3 step 5), sub-area
  and zone -> EXACT. Search still filters on the subtree of every node (R17 3.5,
  Data Model 2); EXACT only says the user named that node.
- `candidates`: `Candidate(location_id, score)`. Exact / structural candidates carry
  `score = 1.0` and sort by display name, then ID; fuzzy candidates carry their
  trigram score and keep their ranked order. A single candidate is allowed (R17
  "top 3").
- Extensions beyond R17 3.1 (documented here, not silent): `method`, `key`,
  `area_hint`, `other_city`. `method` values: exact, context_alias,
  context_required, area_in_span, two_areas, level_in_area, not_in_active_area,
  level_number, fuzzy, other_city, unsupported_script, empty, none. What 7.3 persists from
  them is open (see "Open output-schema questions").

## Cleaning (Data Model 3.2; R17 3.3 step 1)

The same `clean()` builds alias keys and span keys, so both sides meet in one form
(the database `alias_norm` is only lower-case + space collapse, so it is not used).

1. NFKC; Arabic yeh / alef maksura / kaf folded to Urdu yeh / keheh; casefold.
2. Tokens = runs of letters or of digits (punctuation and `-` separate; "ph6" ->
   "ph 6", "6th" -> "6", "Shahrah-e-Faisal" -> "shahrah e faisal").
3. Urdu-script terms -> Roman, logical Unicode order (limited coverage, below).
4. Data Model token canon: ph / fase / phaze -> phase; extension / extn -> ext;
   blk -> block; prec -> precinct; defence / defense / dfa -> dha, **except before
   "city"** ("Defence City" stays as written). "p" is kept (not -> precinct): R17
   3.2 also uses p6 for DHA Phase 6, so "p" means phase or precinct by area.
5. Phrases: gulshan / gulshan iqbal -> gulshan e iqbal, but "gulshan e <other>" is
   left alone (never Gulshan-e-Iqbal, R-23); nazimabad north / n nazimabad / n.n ->
   north nazimabad.
6. Numbers through `app.normalise.find_numbers`: digit tokens in any script
   everywhere; number words (Roman Urdu, English, Urdu; up to 3 words) only after a
   level or area word (phase, block, precinct, sector, p, dha, clifton, iqbal,
   bahria, town, city, pechs, nazimabad) or before "ext" ("do extension").
   Integers 1-999 only.
7. Roman numerals (i / v / x forms, 1-39) only after phase, precinct, sector or dha.
   Not after "block": North Nazimabad blocks are letters (A-T).
8. Adjacent repeats collapse ("DHA Defence" -> "dha dha" -> "dha"), which gives the
   DHA area the key "dha" from its existing official alias. "DHA City" is never
   affected: only identical neighbouring tokens collapse, and "dha city" is matched
   whole (exact alias, or the longest area name in the span) before "dha".

## Resolution order

1. Nothing left after cleaning -> UNKNOWN (`empty`).
2. **Active-area scope** (owner decision): a span that is only a level + identifier
   ("phase 8", "block 5", "block h", "precinct 6"), "p" + number ("P6") or a bare
   identifier ("6", "2 ext") is read only inside the active area (its children and
   the aliases located within it). Found -> RESOLVED (`level_in_area`); absent ->
   UNKNOWN with `area_hint` = the active area (`not_in_active_area`), also when the
   area has no such level at all. This runs before R17 step 2 so the seed's
   mechanically derived global aliases ("block 5" -> Clifton) cannot move the
   customer to another area. Identifiers are numbers or single letters, plus
   an optional "ext", compared exactly.
3. **Exact alias on the whole span** (R17 3.3 step 2): context-free aliases plus
   context aliases of the active area; one location -> RESOLVED, several ->
   AMBIGUOUS. A key that exists only as context aliases, none for this area ->
   AMBIGUOUS (`context_required`): "Block 6" -> {Clifton, Gulshan, PECHS Block 6};
   "P6" -> {DHA Phase 6, Bahria Precinct 6} (R17 3.4). A whole-span local alias is
   authoritative even if it contains a city word.
4. **Another city** as a whole token -> OUT_OF_COVERAGE (Lahore, Islamabad), before
   any partial local match.
5. Agency city tokens are dropped ("Clifton Karachi" -> clifton) unless nothing else
   is left ("Karachi" already matched the city node, ANYWHERE_IN_CITY); steps 2-3 run
   again on what remains.
6. **Area named in the span** (longest area name at the start, then the end): "DHA
   phase 8", "Gulshan block chhe", "Bahria precinct sattaees", "DHA City sector 13".
   The rest must resolve inside that area (context alias, context-free alias in its
   subtree, or level / identifier); the span's own area overrides the active area.
   A second area -> AMBIGUOUS with both areas (`two_areas`). Nothing inside ->
   UNKNOWN with `area_hint` ("DHA sector 13" is never DHA City's; "DHA phase 3" is
   never DHA itself).
7. **Bare level + identifier, no active area** (R17 3.3 step 3): valid in one area
   -> RESOLVED; more -> AMBIGUOUS.
8. Otherwise UNKNOWN: `unsupported_script` if non-Latin text is left, else `none`.

## Decisions (owner, 2026-10-12; rationale in docs/DECISIONS.md)

| Item | Behaviour | Examples (seeded data) |
|---|---|---|
| Active-area scope | Level / identifier spans stay in the active area; absent -> UNKNOWN + area_hint | "block 5" @ Gulshan -> UNKNOWN(gli); "phase 8" @ Clifton -> UNKNOWN(clf); "2 ext" @ Clifton -> UNKNOWN(clf); "P6" @ Clifton -> UNKNOWN(clf); "DHA phase 8" @ Clifton -> DHA Phase 8 |
| Bahria Precinct 6 | Seeded (btk-p6 + "precinct 6" + context p6) | "P6" @ Bahria -> Bahria Precinct 6; "P6" @ DHA -> DHA Phase 6; "P6" no area -> AMBIGUOUS {DHA Phase 6, Bahria Precinct 6} |
| Two areas | AMBIGUOUS with both areas; departs from Data Model 3.4.4's combined search | "Clifton DHA" -> AMBIGUOUS {Clifton, DHA} |
| Urdu script | Four Data Model terms only | ڈی ایچ اے -> dha, فیز -> phase, کلفٹن -> clifton, بحریہ -> bahria; "ملیر", Devanagari -> UNKNOWN(unsupported_script) |
| Other cities | Lahore, Islamabad; whole tokens; a whole-span local alias outranks them | "Bahria Lahore" -> OUT_OF_COVERAGE; "Lahori Gate" -> UNKNOWN |
| Defence City | Not DHA City by the generic rule; unresolved | "Defence City" -> UNKNOWN; "DHA City", "DCK" -> DHA City |
| Identifiers | Exact (deterministic now; fuzzy guard next) | "Bahria precinct 12" -> UNKNOWN(btk), never Precinct 1 / 10; "DHA phase 2 ext" -> Phase 2 Ext, never Phase 2 |

Unchanged from the first stage: area known, child absent -> UNKNOWN + area_hint;
bare number with no active area -> UNKNOWN; "DHA 6th phase" (unsupported word order)
-> UNKNOWN. Clifton neighbours are an 8.1 (ladder) question, not used here.

**Urdu coverage is limited.** The four terms are the Data Model's own examples,
verified by decoding the PDF's visual-order presentation forms (reversed + NFKC);
three also equal the owner-authored bench gold (CT-20, CT-21). Code points:
dha U+0688 U+06CC / U+0627 U+06CC U+0686 / U+0627 U+06D2; phase U+0641 U+06CC U+0632;
clifton U+06A9 U+0644 U+0641 U+0679 U+0646; bahria U+0628 U+062D U+0631 U+06CC U+06C1.
They look reversed in some terminals; the stored order is logical. This is not
complete Urdu or voice-script support.

## Boundary with 7.3 (raw-span validation)

The resolver trusts only the gazetteer, never the caller's text:

- **7.3 must check, before calling `resolve`**, that `raw_span` is a substring of
  the turn's customer text or transcript (Red-Team R-22; the 6.1 tool layer already
  does this for update_requirements). The resolver never sees the message.
- The resolver never returns an ID that is not in the snapshot; the LLM never
  supplies one. A clarification answer (R17 3.4 rule) must be checked by 7.3 against
  the stored `pending_clarification.candidates`, not passed to `resolve`.
- 7.3 owns: op semantics (set / add / "bhi"), deriving `active_area_id` from state,
  storing AMBIGUOUS candidates in `pending_clarification`, the 2-turn expiry
  fallback (R17 edge 11), voice echo for weak STT or "6 and 8" (R17 3.4, R16),
  using `area_hint` to offer the area in its reply, and logging `method` / `key`
  for alias review (R-23 c). 8.1 owns subtree search and neighbours; 9.1 the
  canonical-name echo (R-23 a).

## Tests

- `tests/test_location.py`: every seeded alias (87; done condition "exact aliases
  100 %"), context and level rules, R17 3.4 P6 rows on the seeded data, active-area
  scope (UNKNOWN + area_hint) and named areas resolving normally, number variants,
  identifier protection in both directions, cleaning, area-only SUBTREE, city mode,
  DHA City separation and "Defence City", other cities (also with an active area),
  Urdu terms, UNKNOWN / unsupported script, two areas, argument errors, the R17
  `to_dict` shape, result-contract rejections and gazetteer validation. One
  **synthetic** fixture (not demo data) adds a local alias containing "lahore" to
  test that a whole-span local alias outranks an other-city word.
- Fuzzy (in `tests/test_location.py`): positives, auto-resolution at 0.85, the
  floor, the 4-letter and identifier guards, never-guess negatives, dedupe before
  the margin, stable tie order, the length rule and context aliases (the last four
  on **synthetic** one-alias fixtures, not demo data).
- `tests/test_location_db.py` (database): Python score = pg_trgm `similarity()`.
- `tests/test_gazetteer.py`: snapshot validation and cache lifecycle (offline, fake
  loader); runtime-role read-only load, UUID results and DB vs CSV agreement
  (database).
- Scoped fuzzy (in `tests/test_location.py`): positives stay in the named area;
  negatives never broaden, never drop an identifier, never return the area itself.
- `tests/test_location_bench.py`: every location gold slot in bench/ (51: 49
  RESOLVED, 2 AMBIGUOUS); `KNOWN_DIFFERENCES` is empty; gold unchanged.
- `tests/test_seed.py`: `test_precinct_6_upgrade_inserts_only_the_new_gazetteer_rows`
  seeds the previous dataset, reruns with the current one (exactly 1 location + 2
  aliases, no tenant rows) and reruns again (nothing). Count assertions updated to
  53 / 87 (`tests/test_demo_dataset.py`, `tests/test_seed.py`).

## Fuzzy matching (R17 3.3 step 4)

Runs only when no deterministic step matched, in one of two scopes:

- **Global** (no area recognised in the span): candidates are alias keys
  (context-free, or of the active area) and each location's canonical display name
  ("North Nazimabad Block H"). Display names join parent and child names for fuzzy
  matching only; they are never exact aliases and add nothing to the data.
- **Scoped** (owner approval 2026-10-13; the span named an area but the rest did not
  match): the rest is compared only with that area's own descendants (never the
  area itself, never another area), the area's name removed from their keys:
  "DHA bukhari comercial" compares "bukhari comercial" with "bukhari commercial".
  Context aliases count only if they belong to that area. No match -> UNKNOWN with
  `area_hint`, as before.

Rules (owner decisions 2026-10-12 / 13; DECISIONS.md):

1. **Guards before ranking.** Latin text only; >= 4 letters of place name (level
   words, "p" and identifiers not counted) in the span **and** in the candidate
   key; a span with "defence / defense / dfa city" is never fuzzy-matched.
2. **Shape is exact, only names are scored.** Identifiers (numbers, a letter after
   a level word, "ext") and level words must be equal; the pg_trgm `similarity()`
   score (an exact fraction) compares the place-name text only. A shared
   "block 1" therefore never makes Gulshan look like Clifton, an absent child never
   becomes another child, and an explicit child never becomes its parent. The
   scoring function equals PostgreSQL's (`tests/test_location_db.py`).
3. **Dedupe**: each location keeps its best key. **Margin** = best - best of the
   next different location (0 if none). Ties: display name, then ID.
4. **RESOLVED** (`method = fuzzy`) only with score >= 0.85, margin >= 0.15 and the
   winning name within the length rule (|characters without spaces| difference /
   the longer <= 30 %, Red-Team R-23 b). Otherwise **AMBIGUOUS** with the top 3 at
   or above the **0.30** floor (provisional project choice), each with its score,
   in ranked order. Below the floor -> **UNKNOWN** (`none`). Score and margin of a
   fuzzy RESOLVED result stay internal (the public R17 shape is unchanged).

| Input | Result |
|---|---|
| bukhari comercial, ittehad comercial | RESOLVED (0.85) |
| DHA bukhari comercial (scoped) | RESOLVED Bukhari Commercial (0.85 on the name) |
| Bahria sport city / Bahria heigts / PECHS tariq rd (scoped) | AMBIGUOUS, that area's child only (0.77 / 0.50 / 0.54) |
| nort nazimabad block h (display name) | AMBIGUOUS {North Nazimabad Block H} 0.81; "block k" -> UNKNOWN |
| bahriya precinct 10 | AMBIGUOUS {Bahria Precinct 10} 0.50; "bahriya precinct 12" -> UNKNOWN |
| zamzama comercial | AMBIGUOUS: Zamzama 0.84, Badar 0.36, Sehar 0.36 |
| commercial bukhari commercial | AMBIGUOUS: score 1.0 but 37 % longer (length rule) |
| Bahria malir, Clifton bukhari comercial, Bahria precinct 12 | UNKNOWN + area_hint (scoped: no broadening) |
| malir cantt, hyderabad, korangi, saddar, btq, gulshn block 1 / 13 | UNKNOWN |
| Gulshan-e-Maymar, north karachi | AMBIGUOUS: Gulshan-e-Iqbal 0.43 / North Nazimabad 0.40 (open, below) |

### Known unsupported Karachi places (decision needed)

"north karachi" and "Gulshan-e-Maymar" get misleading clarification offers. No
source in the repo or the reference set lists either as a covered or a known
unsupported place: R17 3.4 names Gulshan-e-Maymar only as "if added", and Red-Team
R-23 as a place "which isn't in the gazetteer"; North Karachi appears nowhere. The
only documented treatment is R17 3.4's "Unknown name -> say not covered ... never
guess" (UNKNOWN). `location.in_coverage = FALSE` is documented (INVENTORY_SCHEMA)
for OUT_OF_COVERAGE, i.e. another city's area, so it must not be used for Karachi
neighbourhoods. Nothing was added. Owner decision needed: should known-but-uncovered
Karachi places be listed (the source of the list, and where: a new data file, or a
column, which needs migration 015) so that they return UNKNOWN before fuzzy
matching? The 0.30 floor was not tuned around these two examples.

## Database snapshot and cache (`app/gazetteer.py`)

- `load_snapshot(conn)`: on an idle runtime (`manzil_app`) connection, one
  `REPEATABLE READ, READ ONLY` transaction with a 5 s statement timeout reads
  `location` and `location_alias` (ordered by id), then validates before returning:
  the Gazetteer's own checks (levels, parents, no cycles, alias targets, contexts,
  usable keys) plus not empty, the agency's city node present, at least one area.
  IDs are the database UUIDs. Neighbours are not loaded (8.1 needs them, not the
  resolver).
- `Snapshot`: the Gazetteer, a sha256 version of the rows, row counts.
- `GazetteerCache`: `load()` once at process start (single-flight; later calls
  reuse it); `get()` returns the published Gazetteer or raises
  `GazetteerUnavailable` and never loads; `refresh()` reloads explicitly and
  publishes only a complete, validated, changed snapshot; on failure the previous
  snapshot stays and the error propagates. No empty or partial gazetteer is ever
  published. Picking up a seed change = `refresh()` or a process restart.
- Not wired into the worker yet: 7.3 adds `load()` to worker start beside
  `app.worker.check_schema`, a failure being a configuration error (exit 78).

## Acceptance evidence (local, 2026-10-13)

Disposable PostgreSQL 18.6 (throwaway container), `manzil-ai:test` (Python 3.12.15),
REQUIRE_DATABASE_TESTS=1:

- The runtime role loads the seeded gazetteer (53 locations, 87 aliases) and has no
  INSERT / UPDATE / DELETE / TRUNCATE on the gazetteer tables (an INSERT fails);
  results carry UUIDs (`tests/test_gazetteer.py`).
- The DB snapshot resolves exactly like the seeded-data (CSV) snapshot for 251
  distinct (span, active area) cases: every alias with and without its context, all
  51 gold slots, and the resolver tables (context, scope, numbers, areas, other
  cities, unknowns, fuzzy, scoped fuzzy, P6, Urdu, Devanagari).
- Cache: load reuse, single-flight, an unchanged refresh keeps the snapshot, an
  owner alias insert is invisible until `refresh()`, a failed first load or refresh
  never publishes, an empty database is rejected.
- Bahria Precinct 6 seed upgrade (`tests/test_seed.py`): from the previous dataset,
  a rerun inserts exactly 1 location (Precinct 6 under Bahria Town Karachi,
  sub_area, precinct, no code) and 2 aliases (official "precinct 6", context "p6"
  under Bahria), no tenant rows; the next rerun plans nothing.
- Python trigram score = PostgreSQL `similarity()` (`tests/test_location_db.py`).

**Local completion only.** Nothing is committed, deployed or run against
production. Production still needs: the owner-run seed for Precinct 6 (dry run,
then `--apply`; expected +1 location, +2 aliases), a release containing these files,
and the 7.3 wiring that calls the resolver.

### Output schema

- Owner decision 2026-10-13: the public R17 shape stays unchanged. A fuzzy RESOLVED
  result carries `method = fuzzy` and no score; score / margin stay internal. 7.3
  derives slot confidence (R17 2.1) from `method`; how exactly is a 7.3 decision.
- `Candidate.score` is 1.0 for structural candidates and a trigram score for fuzzy
  ones; one list never mixes both today, but the meaning differs by `method`.
- Which of `method` / `key` / `area_hint` / `other_city` 7.3 stores in
  `requirement_state` (006 checks slot names, not slot keys) is a 7.3 decision.
