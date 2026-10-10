# Benchmark set (Task 5.3)

Source: R16 section 4 (80 items + 5 controls), R17 (requirement slots, tools, tolerances, ladder),
Demo Scope section 4-5 (scenarios), CLAUDE.md section 1 (DH8-101 / DH8-102 overrides, "cheaper" rule).
Validator: `python -m app.benchmark` (also run by `tests/test_benchmark.py` in CI).

## Files (`bench/`)

| File | Items | R16 make-up (checked) |
|---|---|---|
| `customer_text.json` | CT-01..30 | English 5, Roman Urdu 7, mixed 7, Urdu script 2, incomplete / ambiguous 5, follow-ups + handoff 4 |
| `customer_voice.json` | CV-01..30 | English 5, Roman Urdu 10, mixed 10, ambiguous 3, handoff 2; 12 spoken versions of text items (`paired_with`) |
| `voice_controls.json` | VC-01..05 | 1 silent, 1 near-silent, 1 music, 1 Punjabi, 1 over 120 s (not scored) |
| `staff.json` | ST-01..20 | new full 6, new missing fields 3, update by ID 4, by description single 2, multiple 3, Bahria nominal / marla 2 |
| `voice_manifest.csv` | after recording | one row per recorded note (below) |

Text coverage (checked): >= 12 prices (lakh, crore, sawa, saade, dedh, dhai, "80 lac", "7.8cr"),
>= 10 sizes in all four units (sq ft, sq yd / gaz / yard, marla, kanal; one without a unit),
>= 15 locations incl. aliases, >= 8 explicit and >= 4 inferred buy / rent.

The five demo scenarios are in the set: CT-01 (1), CT-13 / CV-06 (2), CT-27..29 (3a-3c),
ST-01 / ST-10 / ST-16 (4a-4c), CT-30 / CV-29 (5).

## Item format

- `id`, `set`, `category`, `language` (`en`, `roman_urdu`, `mixed`, `urdu_script`).
- `input` (text and staff) or `script` + `gold_transcript {roman, native}` + `recording` (voice).
- `context {after, shown}`: follow-ups run after the named item, with those listings shown (positions 1..n).
- `gold_extraction`: R17 slots (migration 006 names). Each has `raw` (a substring of the input or
  roman transcript, R-22) and normalised values: `purpose {value, source}` (`inferred` when there is no
  span), `property_types [..]`, `location {value: [keys], status, candidates}`, `budget {min, max, mode,
  period}`, `size {value, unit, sqft}`, `beds_min`, `construction`, `investment`.
- `expected_tools`: customer tools `update_requirements` (patch of `{slot, op, raw_span}`),
  `search_properties`, `send_property_photos`, `request_handoff`; staff tools `draft_listing`,
  `find_listings`, `propose_update`. Never `commit_change` (a button, not the LLM).
- `expected_outcome`:
  - `results`: `ids` must be shown; `allowed` may also be shown (ladder steps: budget +10 %, size
    +/-20 %, neighbours, beds -1); anything else is an off-criteria result. `search` is the effective
    request (for follow-ups, the merged state).
  - `no_match` (honest "abhi match nahi" + options), `clarify {slots, candidates}`, `photos {listing}`,
    `handoff {ask_property}`; controls `fallback` / `truncated`.
  - staff `draft {fields, missing, save_allowed, duplicate_of, expected_code}`, `update {listing,
    changes}`, `disambiguate {candidates, allowed}`; every write `confirm_required`.

Rules used for the gold values: `tak` / `under` / `max` / `up to` = max; `around` and a bare amount =
+/-10 %; rent budgets are monthly; no unit = sq yd for house / plot / portion <= 2,000; 1 marla =
225 sq ft, 1 kanal = 500 sq yd; price <= max x 1.05, >= min x 0.9; size +/-10 % (sq yd types) or
+/-15 % (sq ft types). The validator re-checks every expected ID against `demo/` with these filters and
fails when a clear match is missing or a hidden (sold / reserved / rented) listing is expected. Gold
values are hand-derived; a disagreement with the normalisers (Task 7.x) is a finding for review, not
an automatic fix of the gold.

## Recording protocol (voice + controls)

Owner-run, with volunteers. Nothing here goes to Git except `voice_manifest.csv`.

1. **Consent first.** Each volunteer agrees in writing (a message is enough) to: recording a short
   fictional property request, storage in our private bucket until the benchmark closes, use only for
   this evaluation, and deletion afterwards. Scripts are fictional; nobody says a name, number or
   address of their own.
2. **Phones.** Add each volunteer number to the Meta test-number recipient list (owner only). Keep the
   number -> speaker id (S1..S6) and phone class (`android_low`, `android_mid`, `iphone`) mapping in a
   private sheet, never in Git or chat. Before Task 6.1 puts Gemini's free tier on the path, only team
   and consenting volunteer phones may message the test number.
3. **Record inside WhatsApp** (hold-to-record voice note, not a file), sent to the test number, in
   item order per speaker as `recording` says (speaker, phone, noise: 15 quiet / 10 moderate / 5 car or
   street; `target_seconds` 3-45; controls as described in `voice_controls.json`). Note each sent
   item id and time in the private sheet. A retake replaces the item: note which send counts.
4. **Pulled through the real path.** Each note goes webhook -> `inbound_audio` -> `fetch_audio` ->
   private `tenant/<agency>/audio/` object (Task 5.3 audio storage, migration 012).
5. **Manifest.** In the running worker container:
   `python -m app.bench_manifest --agency-id <uuid> --since <start, with offset>` prints CSV
   (seq, conversation prefix, received_at UTC, status, is_voice, mime_type, duration_ms, byte_size,
   sha256, storage_key). Add `item_id`, `speaker`, `phone` and `noise` from the sheet, drop rows that
   are not benchmark notes, and commit it as `bench/voice_manifest.csv`. Every stored row must be
   `stored`; a `rejected` / `failed` note is re-recorded.
6. **Retention.** The audio stays private in the bucket until benchmark run #2 (Task 10.3 / 14.x),
   then the owner deletes the `tenant/<agency>/audio/` objects of the benchmark notes (manifest keys).

Gold transcripts are the scripts. If a speaker deviates, correct `gold_transcript` (and spans) to what
was actually said before scoring.
