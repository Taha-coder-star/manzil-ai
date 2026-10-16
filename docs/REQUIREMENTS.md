# Requirement-state manager (Task 7.3, component C8)

Status (2026-10-13): implemented and tested locally on `feature/task-7-3-state-location`
(from `origin/main` f0cb59b, PRs #31-#33 merged). **Not merged, not deployed, no live call
made.** Code: `app/requirements.py`; wiring in `app/ai_tools.py`, `app/turn_engine.py`,
`app/handlers.py`, `app/worker.py`. No migration. Task 7.2's live benchmark and control
gate are still pending and are not touched.

Sources: R17 sections 2.1, 3, 4, 5 and edge cases 3, 9, 11; Data Model sections 3-4;
Red-Team R-02, R-03, R-22, R-23; docs/LOCATION_RESOLVER.md "Boundary with 7.3";
docs/NORMALISATION.md; docs/AI_TURN_ENGINE.md; docs/SPEECH.md; docs/STATE_SCHEMA.md.

## Flow

`update_requirements` (customer route, `AI_PROVIDER=gemini`) no longer a stub. The engine
gives the tool a `TurnBinding` (connection, turn, the clipped message texts the model saw).
Each call is one patch:

1. **Span check (R-22), message by message.** A span must be a whitespace- and
   case-insensitive substring of one message of this turn: typed text or the accepted
   transcript (`message.text`; a rejected transcript never enters the turn). It cannot
   straddle two messages, history never counts, no Unicode normalisation widens a match.
   Typed text wins over a transcript that has the same words. Failures are dropped
   (`span_not_in_turn`). Only budget, size and purpose are re-extracted by the normalisers
   from the message (R-22 mitigation): exactly one explicit amount / sized unit / purpose
   word; two candidates or none, nothing is stored. A place is never re-extracted. The
   dropped span stays in the log (`dropped_span`).
2. **Merge** (below) inside one tenant transaction holding the `requirement_state` row
   lock: one new version, one `requirement_patch_log` row with every op and its outcome
   (also rejections), pending-clarification changes. Rejections do change the version
   (an audit entry); replays do not.
3. The model gets only op outcomes (`applied` / `rejected` / `unknown` / `clarify` / `ask` /
   `out_of_coverage`) and codes. No ids, no customer words.
4. Customer-visible text: approved Roman Urdu templates (`turn_engine.TEMPLATES`, wording
   pending owner approval), parameterised only with canonical gazetteer names
   (`ai_tools.Notice`). After the tools, `finish_turn` handles an unanswered question.

## Location contract

- `active_area_id` is derived from the **stored** location slot: the one area above its
  nodes. No location, a city-only location, nodes in several areas, or nodes the current
  gazetteer no longer knows give no context. `area_hint` is wording only: it never sets the
  context, a slot or a search.
- Resolution uses the pre-patch state for every op of the call. Several places in one patch
  are a union (a later `set` extends the first).
- **Several places in one span** ("Clifton aur DHA", "Clifton, DHA", "Clifton ya / and / & / /
  DHA"): when the whole span does not resolve to one place, it is split at the separator
  and every part is resolved on its own as a separate grounded span (each part is a
  substring of the verified span; the log keeps `split_from`). Resolved parts are a union,
  an ambiguous part opens the question, an unknown / foreign one is reported; one place is
  never picked arbitrarily. While a question is open, a multi-place reply is no answer and
  the question is asked again. Without a separator ("Clifton DHA") the resolver's decision
  stands: ambiguous between the two areas, ask.
- Only RESOLVED changes the slot: `{nodes, mode, city, status, raw, terms[]}` plus entry
  fields `method`, `key` (alias-review data, R-23 c, kept in the DB; logs carry only the
  status and method), `confirmed`, `confidence`, `echo[]`. UNKNOWN (reply: covered areas, or
  the recognised area by name) and OUT_OF_COVERAGE (reply names the agency city) change
  nothing. "Karachi aur Lahore": Karachi is stored, Lahore declined in the same reply.
- AMBIGUOUS (also one fuzzy candidate; `app.benchmark`'s >= 2 rule is gold-only) opens the
  single `pending_clarification` (location). Each candidate stores `{location_id, score,
  name, origin{op, raw, source, turn, call}}`. The slot is untouched; other slots keep
  accumulating. A new ambiguity replaces the open question; a second one in the same patch
  waits.
- **Answer check.** The answer is resolved on its own and counts only if exactly one
  stored candidate is the resolved node or lies within it ("DHA", "Bahria", "Phase 6"
  answer {DHA Phase 6, Bahria Precinct 6}). The question's own op (set / add) is then
  applied (`method = clarified`). A node that still holds several candidates ("Karachi",
  "DHA" vs Phase 6 / 7) leaves the question open and asks again. A resolved place the
  question never offered supersedes it (`cancelled`). "haan", "DHA wala" and other
  non-names are not answers.
- **Expiry** (R17 edge 11): derived from completed customer turns after the question
  (`status = 'replied'`), so a retried turn counts once. First unanswered turn: keep, process
  the other slots, re-ask; second: expire. 24 h also expires it (checked before the next
  patch). Fallback: if all candidates share one area, the location becomes that area
  (SUBTREE, `source inferred`, unconfirmed, `echo fallback`); across areas (P6, Block 6)
  nothing is invented, the previous location is preserved (owner decision) and the customer
  is asked for an area. The cap is 24 h (`CLARIFICATION_TTL`, a documented stale cap: the
  service window / position validity of R17 5) and two completed customer turns
  (`EXPIRY_TURNS`, R17 edge 11); both are tested at their boundaries.
- **A plain "haan"** confirms the sole pending candidate only when all hold: exactly one
  candidate and it was a fuzzy resolution (the question named it: "Kya aap X ke baare mein
  keh rahe hain?"), the question was sent (the turn that opened it is `replied`), it has not
  expired, and the turn is exactly one message that is a plain yes (`AFFIRMATIONS`; typed,
  or heard without weak STT signals), so no other place can hide in it. The question's own
  op is then applied (`method clarified`, patch detail `affirmed`), whether the model called
  the tool or not (`finish_turn` handles the no-tool case). Not confirmed: several
  candidates, an expired question, "haan Clifton" or a second message, a weak voice
  "haan", a non-fuzzy or purpose question. A different place in the reply follows that
  place (it supersedes the question).
- 90 idle days (no earlier inbound message): `clear` ops for every slot, open question
  cancelled, before the patch (R17 5). Stale state never supplies context.

## Other slots (app/normalise.py does every number)

| Slot | Behaviour |
|---|---|
| purpose | explicit wording only (`parse_purpose`); a different value moves the budget to `previous_budget` (R17 4.4) and asks (rent: monthly) unless the patch gives an amount; no amount-less guesses |
| budget | `parse_budget` with the effective purpose (ops processed purpose -> type -> rest, not in model order); unclear / two amounts: ask, store nothing; period follows purpose when unset |
| purpose inference | only from a budget when no purpose exists: a monthly marker is explicit rent, otherwise `infer_purpose`, always `source inferred`, unconfirmed, `echo inferred`; between thresholds opens a `purpose` question |
| relative budget | by the parse, not the label: cheaper = cheapest shown x 0.97 (+ `excluded_property_ids` += shown, `no_budget_relaxation`), higher = max x 1.10, premium = min highest shown x 1.03; shown = latest `search_run` within 24 h through `v_public_listing`; none: ask, nothing invented (edge 9). An amount with `relative` is just the amount |
| size | `parse_area` with the first stored type and the agency marla basis; unit unclear: ask; "bara" with no number = next nominal bucket above the largest shown |
| property_types / category | type words (plurals, flat / ghar / villa / bungalow); add = union, set = replace, remove; category derived when all types share one |
| beds_min / baths_min | one plain integer 0-20 |
| construction, possession, investment | D11 enums (migration 007 CHECKs): construction `ready` / `under_construction` / `grey_structure` / `off_plan`; possession `possession` / `on_ground` / `file` / `balloted` / `allotted`; investment `{intent, tags}` with tags `rental_income` / `capital_gain` / `pre_launch` / `installments`. set = replace, add = union, remove, clear; no match: kept as words |
| soft_prefs | the D11 feature vocabulary is the 8.1 ranking vocabulary (see "Omissions"): kept as the customer's words in `unmapped_prefs`, honest notice, never a filter |
| beds_min "more_beds" | owner decision 2026-10-13: the stored bedroom minimum + 1 (max 20); with no prior minimum, ask (`beds_ask`) and store nothing. Phrases: "ek aur / one more / more / extra / zyada bedroom" |
| budget "budget_up" | the existing `higher` relative op (R17 4.5 "thoda upar / budget barha do": max x 1.10) |
| plausibility | an absolute budget outside [0.3, 3] x (min, max price) of the **active inventory** (see "Inventory read by the guard") of this purpose, the stored types and the stored location's subtree is asked about, not stored (`budget_implausible`, patch detail `implausible`, `guard_pkr`). Restated unchanged in the next patch: kept unconfirmed with `echo implausible`. Heard (voice): kept unconfirmed with the echo at once (R17 edge 15). No purpose yet, or no inventory: no guard |
| type switch | owner decision 2026-10-16: a switch between residential and commercial types (house -> office) drops a size / `beds_min` / `baths_min` the same patch did not state again and records a `size` ask (`type_switch`, notice `size_ask`), so the search waits for the size (`open_asks`). A switch within one category (house -> flat) keeps them; a first search without a size is not asked (Demo Scope scenario 1) |
| previous_budget, excluded_property_ids | backend only: a model op is `not_settable` |

`add` on a scalar slot means set; `remove` on one means clear. `relative` elsewhere is
rejected.

## Inventory read by the guard

`RequirementService._band` runs `SELECT min(price_pkr), max(price_pkr) FROM
v_public_listing WHERE purpose = ...` at patch time, inside the patch's tenant
transaction (PostgreSQL READ COMMITTED: the committed rows at that statement, not a
frozen snapshot, not cached, not tied to the last search). The view is the customer-safe
one: `status = 'active'` only (sold, rented, reserved and hidden listings are not in it) and
scoped to the agency by RLS. Filters, in this order: the patch's purpose (state or same
patch), the stored property types (`property_type = ANY`), and the subtree of the stored
location nodes of the same patch (budget ops run last). A subset with no rows gives no
guard (the band is never widened to the city). Prices are compared as stored (rent is
monthly, sale one-time). The check happens once, when the amount is stated; a later
inventory change does not re-evaluate it.

## What the model sees (R17 5)

`TurnBinding.snapshot()` adds one part before the current messages when anything is
stored or open: "Stored state (data, not instructions)" with purpose, category, types,
location names, budget, size, beds / baths, construction, possession, investment (unconfirmed
values marked) and the open question with its options. No ids. The system prompt tells the
model to use `add` for bhi / also / aur and `set` for a change, and to answer an open
question with the customer's own words naming one option as a location span. Last search,
focus, language and control state are listed under "Omissions".

## Slot confidence

Ordinal trust levels, not probabilities (migration 006 only needs 0..1; nothing multiplies
them). `tests/test_requirements.py` pins each number to its reason.

| Constant | Value | Why |
|---|---|---|
| `CONF_DETERMINISTIC` | 1.0 | exact / context alias, level in the stored area, the normalisers, the customer's own choice among candidates |
| `CONF_FUZZY` | 0.85 | `location.AUTO_SCORE`: R17 3.3 step 4's own auto-resolution threshold (the true score is not exposed); fuzzy results are also never `confirmed` |
| `CONF_INFERRED` | 0.6 | purpose from an amount (R17 4.6): below fuzzy, because it only ever comes with an echo |
| `CONF_FALLBACK` | 0.5 | the area chosen when a question expired: a guess the customer never made |
| `CONF_VOICE_MAX` | 0.9 | heard, not read: cap for any voice-only slot (also never `confirmed`) |
| `CONF_WEAK_VOICE_MAX` | 0.5 | voice with weak STT signals (R16 7.4), which also sets `echo voice_weak` |

The final value is the lowest cap that applies (fuzzy voice: min(0.85, 0.9) = 0.85; weak
voice 0.5).

**How the numbers are used.** Today they are stored and nothing branches on their value:
`confirmed` and `echo` drive behaviour (what must be echoed before a search), and
`method` / `source` say how a slot was obtained. The number is a compact label for review,
audit and for the search / echo logic of 8.1 and 10.2 to sort slots by trust. Only the
**ordering** is a design claim: deterministic > fuzzy > inferred > fallback = weak voice,
with voice capped below text. They are not calibrated probabilities: no data measures how
often a slot of a given number is right, and the benchmark has too few fuzzy or voice-weak
cases to calibrate them. Do not multiply, average or threshold them as if they were.

**Why 0.85 for a fuzzy resolution.** The resolver accepts a fuzzy match automatically only
at trigram similarity >= 0.85 with a margin of 0.15 (R17 3.3 step 4; the exact score is
not exposed in the result, owner decision 2026-10-13). So every fuzzy `RESOLVED` slot has a
score of **at least** 0.85: 0.85 is the floor of the accepted range, a conservative
statement that is true for all of them (an average would be an invention). It is not the
probability that the match is right: similarity measures how alike two strings are, not how
often that is the intended place, and the guards (length rule, name-word guard) exist
because high similarity alone was wrong for places such as Gulshan-e-Maymar. A fuzzy slot is
therefore also never `confirmed`, and a place the customer did not type exactly is echoed
(R-23).

## Voice

Voice-only slots are `confirmed = false`; weak signals (`stt_meta` has `fallback_reason`,
`rejected_transcript` or `truncated`) cap confidence at 0.5 and add `echo voice_weak`; two
distinct numbers in a voice location span that still resolves add `voice_numbers` (the
resolver itself does not guess between "6 and 8"). Task 10.2 adds `llm_uncertain` (the op's optional
`uncertain: true`, heard values only), the budget operator question (`budget_operator_ask`) and
the confirmation question before a search (docs/VOICE_TURN.md; `requirements.uncertain_slots`,
`RequirementService.confirm_echo`). The serialised turn, control gate, interim, failure
notices, send-once keys and the speech notices are unchanged.

## Replay safety (no migration)

The patch row carries `call` (`c0`, `c1`, ... per tool call, `end`) and `turn` and is found
again by `source_message_id` + `call`. A retried turn (provider execution is at least once)
finds its stored call and applies nothing twice (tested with "thoda upar", which
compounds). The customer notices are part of the stored row (`notices` on each entry), so a
replay rebuilds them in their original order.

**Expiry / confirmation crash window (fixed).** The end-of-turn decision (`finish_turn`:
the question expires, or a plain "haan" confirms its candidate) commits in one transaction
that closes the question, writes any location change **and always writes a patch row**
(`call = end`, one entry with `kind expiry` and its notices; status `expired` when the
location is left as it was, `applied` for a fallback or a confirmation). The reply plan is
stored later (`app.cards.fixed_plan`). A crash between the two used to leave the question
closed with no row to find, so the retry saw nothing to expire and the customer never got
the notice. Now the retry's first step is `stored = patch row (end)`: it decides nothing
twice and returns the notice rebuilt from that row (`RequirementService.replay_notices`).
The same holds for an expiry decided at the start of an `update_requirements` call
(entry `kind expiry`, found again by call id). Send-once is unchanged: the plan is still
sent under `turn:{id}:{seq}` keys, a stored plan is resent without a provider call, and the
serialised turn / conversation lock are untouched. Tests
(`test_a_crash_after_the_decision_never_loses_the_notice`: no-change expiry, fallback expiry
and "haan" confirmation each crash after the state write, retry, and send exactly one
notice with exactly one `end` row; a send-failure variant; an in-patch expiry variant)
fail if the replay branch is removed. Remaining limit: a plan that was never stored and a
provider that answers differently on the retry can word the tool part of the reply
differently; the state, and the expiry notice, are the same.

## Worker start

With `AI_PROVIDER=gemini` the worker checks migrations 006 / 007 tables, then loads the
gazetteer (`app.gazetteer.GazetteerCache.load`) as `manzil_app`. Any failure (database,
empty table, invalid alias, missing city node) is `Worker not started: ... valid location
gazetteer`, exit 78, logging only the exception class. `AI_PROVIDER=stub` never loads it.
Nothing refreshes it on a timer: an owner seed run is picked up at the next start
(`GazetteerCache.refresh()` keeps the previous snapshot on any failure; tested).

## Omissions: exact source, classification, outcome

Classes: **R** required in 7.3 (implemented here), **L** assigned to a later task, **D**
deliberate owner decision.

| Item | Exact source requirement | Class | Outcome |
|---|---|---|---|
| Plausibility bands | R17 4 item 6: "Reject impossible combinations (e.g. a 500 sq yd house for 5,000 PKR as SALE) -> ask." and "Plausibility bands per area/type come from the min/max of active inventory x [0.3, 3]. These are guards, not filters."; edge 15: "Plausibility guard trips on an STT number ("80 crore" for 125 gaz in Bahria) -> echo-confirm before search (R16 voice rule)."; docs/NORMALISATION.md: "Plausibility bands ... are Task 7.3" | R | implemented (above) |
| construction / possession / investment | R17 2.1 stage 5 hard filters: "construction_status, possession_status and investment flags, when the user explicitly asked."; R17 5 state: "construction[], possession[], ... investment{intent,tags[]}"; Demo Scope C4: "Requirement extraction: purpose, type, category, location, budget, size, beds/baths, construction/investment" | R | implemented with the D11 enums |
| more_beds | R17 2.1 stage 1: "relative takes values such as cheaper, bigger, more_beds, budget_up." (no formula given) | R + D | implemented as stored minimum + 1, ask when none (owner decision 2026-10-13) |
| budget_up | same sentence; R17 4.5: "thoda upar" / "budget barha do" -> "max = max x 1.10" | R | already the `higher` op; "budget_up" is its R17 label |
| R17 5 snapshot: requirement_state summary, pending_clarification | R17 5: "What the LLM sees per turn (compact, not the full history): the requirement_state summary; the last search_run with its shown_item list ...; focus; pending_clarification; control_state; the last ~10 messages (transcripts included); the language preference." and D10: "The LLM receives a compact state snapshot plus the last ~10 messages, never the full history." | R (these two items) | implemented |
| R17 5 snapshot: last search_run + shown_item, focus | same sentence | L | nothing writes `search_run` / `shown_item` until the search service (execution plan 8.1 "Search service"); `focus` is Task 10.1 (reference resolution) |
| R17 5 snapshot: language preference | same sentence | L | `conversation.preferred_language` has no writer; language policy is D15 / 10.x |
| R17 5 snapshot: control_state | same sentence | L | the turn-level gate already reads it and stops the AI (5.1); showing it to the model has no use until the handoff states of 11.2 |
| soft_prefs mapping | R17 2.2: "The LLM maps phrases to the controlled vocabulary (D11 features + facing + investment_tags). Phrases it can't map go to unmapped_prefs." | L | the feature / facing vocabulary feeds ranking ("Soft-preference tag matches", R17 2.1 stage 8) in the search service; until then the words are kept in `unmapped_prefs` as R17 2.2 says for unmapped phrases |
| Execution-plan 7.3 contract | "patch ops set / add / remove / clear / relative; span verification, per-slot provenance, versions; buy->rent budget rule, the "cheaper" rule (no budget relaxation, exclude shown); pending_clarification" with unit tests "budget change, location set vs add, buy->rent, cheaper, non-substring span rejected" | R | all implemented and tested (`test_a_budget_replaces_the_budget`, `test_set_replaces_and_add_extends_the_union`, `test_buy_to_rent_moves_the_budget...`, `test_cheaper_uses_the_shown_prices...`, `test_a_span_not_in_the_turn_changes_nothing...`) |
| Across-area expiry | R17 edge 11: "Expire after 2 unanswered turns and fall back to SUBTREE / area level." | D | the previous location is preserved when the candidates share no area (owner 2026-10-13) |
| "haan" | R17 3.4 "Fuzzy tie ... Ask" (no rule for the reply) | D | the exact conditions above (owner 2026-10-13) |

## New customer-facing templates

Every new Roman Urdu notice of 7.3 (all in `turn_engine.TEMPLATES`; {...} values come from
the backend: canonical gazetteer names or fixed words, never model text). Wording is
awaiting owner approval.

| Key | Trigger | Exact text |
|---|---|---|
| `requirements_saved` | a patch changed at least one slot | Theek hai, aap ki requirements note kar li hain. |
| `requirements_unclear` | nothing in the patch could be used | Aap ki baat main theek se samajh nahi saka. Please apni requirement dobara likh kar bhejein. |
| `location_ambiguous` | a place has 2+ candidates | Aap ka matlab kaunsa hai: {options}? Please naam likh kar bhejein. |
| `location_confirm` | a place has one fuzzy candidate | Kya aap {options} ke baare mein keh rahe hain? Agar nahi, to sahi jagah ka naam likh kar bhejein. |
| `location_reask` | the first unanswered turn; an answer naming an area that holds several options; a multi-place reply | Aap ka matlab kaunsi jagah hai: {options}? Please ek naam likh kar bhejein. |
| `location_chosen` | an answer or a "haan" picked a candidate | Theek hai, {area}. |
| `location_unknown` | a place not in the gazetteer | Ye jagah mujhe nahi mili. Hum in areas mein property dikhate hain: {areas}. |
| `location_unknown_in_area` | an area was recognised, the rest was not | {area} mein ye jagah mujhe nahi mili. Please sahi naam ya block / phase likh kar bhejein. |
| `location_out_of_coverage` | another city named | Maazrat, abhi hum sirf {city} mein property dikhate hain; {other} abhi cover nahi hota. |
| `location_expired` | a question expired and its candidates share one area | Jagah ka jawab nahi mila, is liye main {area} ke poore area mein dekhunga. |
| `location_expired_none` | a question expired across areas | In jagahon mein se jawab nahi mila. Apna chaha hua area likh kar bhejein. |
| `purpose_ask` | an amount between the rent and sale thresholds; a purpose question re-asked | Aap property khareedna chahte hain ya kiraye par lena chahte hain? |
| `purpose_inferred` | purpose inferred from an amount ({purpose}: khareedna / kiraye par lena) | Raqam se main ne samjha ke aap {purpose} chahte hain. Agar ghalat hai to bata dein. |
| `budget_ask` | a relative budget phrase with no search / budget to start from | Aap ka budget kitna hai? |
| `budget_ask_rent` | buy -> rent with no new amount | Kiraye ka budget kitna hai (mahana)? |
| `budget_ask_sale` | rent -> buy with no new amount | Khareedne ka budget kitna hai? |
| `budget_unclear` | two amounts or an unreadable budget | Budget ki raqam samajh nahi aayi. Please ek raqam likh kar bhejein (jaise 8 crore). |
| `budget_implausible` | a budget far outside the listings of that area / type (asked, or kept unconfirmed) | Ye budget maujooda milti-julti properties se bohat alag lagta hai. Agar yehi sahi hai to raqam dobara likh kar bhejein. |
| `size_unit_ask` | a size without a usable unit | Size kis unit mein hai? (gaz, marla ya sq ft) |
| `size_ask` | "bara" with no search to compare | Kitne size ki property chahiye? |
| `beds_ask` | "ek aur bedroom" with no stored minimum | Kitne bedrooms chahiye? |
| `pref_unmapped` | a preference with no vocabulary | Is pasand ke liye abhi filter available nahi hai. Please apni baat wazeh kar dein. |

## Tests

`tests/test_requirements.py` (offline): spans (case, whitespace, zero-width, full-width,
straddling, voice vs text), stale / hint / unknown / out-of-coverage / DHA vs DHA City /
multi-location, questions and answers, expiry, purpose / budget / relative / size / types /
beds, vocabularies, more_beds, plausibility, place splitting, "haan" positives and
negatives, every confidence number, the snapshot, voice flags, re-extraction, the 006
shape. `tests/test_requirements_db.py` (real
worker, RLS, Gemini fake): the above through the database, replays, row-lock concurrency,
two worker threads on one conversation, 90-day reset, refresh, schema and startup failures,
tenant isolation, log contents.
