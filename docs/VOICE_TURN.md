# Voice end to end (Task 10.2)

Execution plan 10.2: "voice -> in-turn STT -> the same pipeline; voice-critical echo (price / size /
location / purpose) when uncertain". Done: Demo Scope scenario 2 passes from customer B's phone.
Gate M3: transcript critical slots correct, results exactly {DH6-101, DH6-102, DH7-101}, a Roman
Urdu reply, <= 10 s from voice-note receipt (Demo Scope scenario 2; R16 E "inbound -> first reply").

Sources: R16 section 7 (items 3-5), Demo Scope C4 and scenario 2, R17 4.3 / edge 15, Red-Team
R-02 / R-21 / R-22, docs/SPEECH.md, docs/REQUIREMENTS.md, docs/CUSTOMER_TURN.md. Branch
`feature/task-10-2-voice` from `origin/main` 7f5cd3d. No migration, no provider or setting change.
**Every policy below is a project choice awaiting owner approval** (none has been approved yet).

## What already existed (unchanged)

- 6.3: the note is downloaded and transcribed inside the serialised turn, before taps and the
  engine; Scribe -> Gemini fallback; too short / silent / long / failed templates; `stt_meta`
  reuse on a resumed turn; one usage row per attempt.
- The transcript is the message's `text`: extraction, the R-22 span check, the normalisers, the
  resolver, 8.1 search, the 9.1 renderer, grounding and `customer_reply.send` are the text path.
- 7.3: heard slots are `source voice`, never `confirmed`; echo reasons `voice_weak`,
  `voice_numbers`, `fuzzy`, `implausible`, `inferred`.

## Clarification vs confirmation

- **Clarification** (7.3 / 9.1, unchanged): a value is missing or ambiguous (two places, unclear
  purpose, unreadable amount). Nothing is stored for it; the customer is asked; search refuses
  results while a `pending_clarification` is open (`search.gate`).
- **Confirmation** (10.2, `render.CONFIRMATION`): values are stored but heard with an uncertainty
  signal. The customer is shown what was understood and confirms or changes it; nothing is
  searched until then. A clarification always goes first: the confirmation is only asked when no
  clarification is open, and the search asks it once the clarification is answered.

## When a heard value is confirmed first (R16 7.4)

A slot is uncertain when it is voice-critical (`purpose`, `location`, `size`, `budget`),
`source = voice`, not `confirmed`, and its `echo` holds one of the existing reasons
`voice_weak` (STT fallback answered, primary rejected, or cut at 120 s), `voice_numbers`,
`fuzzy`, `implausible`, or the new `llm_uncertain` (the model set `uncertain: true` on the op;
R16 7.4 b). No numeric confidence is invented; these are the provenance flags 7.3 already stores.

- `uncertain` can only add a reason: `false` or a missing field changes nothing, a typed op
  ignores it (it is kept in the patch log), and it never bypasses a clarification (tested).
- **Script is not uncertainty.** R16 7.3 accepts Devanagari output ("the LLM reads it"); a
  transcript in another script is not by itself a transcription error. What goes wrong with such
  output is specific values the normalisers cannot read; those are asked (next section).
- **Inferred purpose.** Demo Scope C4 asks for an echo of an inferred rent / sale and of
  voice-critical slots "when uncertain". A purpose inferred from a heard amount always gets the 7.3
  `purpose_inferred` note in that turn ("Raqam se main ne samjha ke aap khareedna chahte hain.
  Agar ghalat hai to bata dein."), before the cards (10.1 D5). When the budget it came from is
  uncertain, the confirmation question lists the purpose ("khareedne ke liye") and Haan confirms
  it with the rest, so the note is not sent twice. An inferred purpose from a clean heard amount
  is not *blocking* (scenario 2 expects the cards in the first reply); it is never silent.

## Budget operators the normalisers cannot read

| What was heard / written | Before 10.2 | Now |
|---|---|---|
| `8 crore tak` | max 8 cr | max 8 cr |
| `8 crore تک`, `8 کروڑ تک`, `آٹھ کروڑ تک` (Urdu script) | max 8 cr | max 8 cr (unchanged) |
| `8 crore` (bare, nothing after it) | around 7.2-8.8 cr (owner decision 2026-10-12) | unchanged |
| span `8 crore`, message `8 crore tak` / `se upar` / `se 10 crore` / `تک` | **around** (silent) | max / min / range / max, read from the same message; raw `8 crore tak` |
| `8 crore तक` (Devanagari operator, in or after the span) | **around** (silent) | asked: `budget_operator_ask` |
| `8 करोड़ तक`, `आठ करोड़ तक` (Devanagari amount) | **dropped** (`unparsed`, no notice) | asked: `budget_unclear` |

Rules (`normalise.parse_budget(span, after=...)`; `after` is the text right after the verified span
in the same message, R-22: current-turn evidence only):
- the span holds no around word and ends with the amount, and the message continues with an
  operator the normaliser reads (`tak`, `takk`, `se kam`, `se upar`, `se oopar`, `max`, `min`,
  `تک`, `سے کم`) or with a range's second amount (`se 10 crore`): the budget is read with it, with
  the existing max / min / range meaning;
- a non-Latin word in the operator position (inside the span or right after it) that is not one of
  those: the operator is unknown, nothing is stored, the customer is asked with the amount;
- a non-Latin budget span the normaliser cannot read: asked with the existing `budget_unclear`;
- the span occurs more than once in its message and the occurrences read differently ("pehle
  8 crore socha tha, ab 8 crore tak"): which one the model meant cannot be told, so it is asked
  (`budget_unclear`, detail `repeated_amount`); identical readings are used as one.

## Unanswered type / size / budget questions hold the search

7.3 records every question about a value as an `ask` entry (with its notice) in
`requirement_patch_log`. `RequirementService.open_asks` reads them: for `property_types`, `size`
and `budget`, the newest asked-or-applied entry, however old; when it is an ask, that question
is open. There is no expiry: an old unanswered question is asked again before any search, so
neither a missing value nor an earlier one is used (a 90-day reset clears the slots, which
answers it). While one is open (and no location / purpose clarification is, which
goes first, as in the search gate), `search_properties` does not search: it asks the open question
again (rebuilt from the stored entry) and answers the model `answer_first`. So a turn never searches
with an earlier budget or size while asking for its replacement, nor without a stated value it
could not read. The question is answered by an applied (or cleared) value for that slot; a change
to another slot is no answer. No migration: `pending_clarification.slot` is CHECK-limited to
location / purpose / target, and the patch log already holds the question.

**One question per turn.** When a turn's notes hold several questions, only the first in the
existing priority is sent: an open location / purpose question (`location_ambiguous`,
`location_confirm`, `location_reask`, `purpose_ask`), then 7.3's slot order: `type_unclear`,
`size_unit_ask` / `size_ask`, the budget questions. The others stay open in the log and are asked
once the first is answered (tested: type, then the operator). A clarification with buttons
replaces every text question.

This also applies to text: a typed "budget 5 crore ya 6 crore" in a conversation that already has
a budget no longer searches with the old one while asking. The 7.3 buy <-> rent budget question
(`budget_ask_rent` / `budget_ask_sale`, from the purpose switch) is a note, not an ask entry, and is
left as it was (the persisted-purpose follow-up is separate).

## Property type in other scripts

Trace (Scribe output "... 500 gaz का घर ..."): the model copies `घर` as the `property_types`
span (R-22 verifies it), `requirements.parse_types` finds no type word, and 7.3 used to reject it
as `unparsed` with no notice; the search then ran over every type. Now:
- Urdu-script type words are read through the existing alias table: `گھر`, `مکان` -> house,
  `فلیٹ` -> apartment, `پلاٹ` -> plot, `دکان` -> shop, `دفتر` -> office (R16 7.5: the normalisers
  cover both scripts). No transliteration step.
- A type span in words the normaliser cannot read (Devanagari) is asked: `type_unclear`.
- `type_unclear` and `budget_operator_ask` are 7.3 asks, so they hold the search until answered
  (next section): no result is built over every type or on a guessed budget.

Limit: if the model never emits a `property_types` op for an unread word, nothing can detect it
(the backend does not read Devanagari); the search then has no type, as for a customer who named
none. The 7.2 decision on the STT setting (Roman Urdu output) addresses this at the source.

## Scenario 2 by script (tested)

| Transcript | Location | Type | Size | Budget | Purpose | Outcome |
|---|---|---|---|---|---|---|
| Roman Urdu | DHA Phase 6 | house | 500 sq yd | max 8 cr | sale (inferred, noted) | the 3 listings (DB test) |
| Urdu script | DHA Phase 6 | house | 500 sq yd | max 8 cr (`آٹھ کروڑ تک`) | sale (inferred, noted) | the 3 listings (DB test) |
| Scribe mixed Devanagari (`घर`, `8 crore तक`) | DHA Phase 6 | asked first | 500 sq yd | asked next (operator) | not inferred | one question per turn, no search until both answered (DB test) |
| Full Devanagari | not resolved (`location_unknown`) | asked | read as gaz | asked (`budget_unclear`) | not read | questions, no slot guessed |

## Haan / Badlein

The question's [Haan] id is `echo:yes:{requirement_state.version}`; [Badlein] is `echo:edit`.

| Tap | Effect |
|---|---|
| Haan, current | `RequirementService.confirm_echo` on **the tap's conversation**: the tap must quote (WhatsApp `context.id`) a question message this conversation was sent with this very button, less than 24 h ago (`CLARIFICATION_TTL`, measured from that message's `sent_at`); under the `requirement_state` row lock, the version must still be the one the question was rendered from; then every unconfirmed voice-critical slot becomes `confirmed = true`, `confirmed_by = "echo"`; one `requirement_patch_log` row (call `echo`); then one search. |
| Haan on an older question (values changed since), a repeated tap, a question message sent over 24 h ago, or a tap that quotes no question of ours | `echo_stale`; nothing confirmed, nothing searched. The age is the tapped message's own send time: a newer question with the same version, or a refreshed state timestamp, never revives an old message. A question shown now on values stored days ago is answerable. |
| Haan, a retry of the same tap message (at-least-once execution) | the stored row is found ("replayed"); nothing written twice; the turn's stored plan prevents a second send. |
| Badlein | `echo_edit`; nothing changes, and **the search stays blocked**: the block is derived from the slots, not from the question, so every later search asks the same confirmation again (tested) until Haan, or a correction that leaves no uncertain heard slot (a typed value is confirmed; a cleanly heard one carries no reason). |

No migration: `pending_clarification.slot` is CHECK-limited, so the state of the question is the
version on its button (the 9.2 staff-button pattern).

## New customer wording (awaits owner approval; printed from the code)

| Key | Roman Urdu | English |
|---|---|---|
| `render.TEXT voice_echo` | Aap ne kaha: {facts} -- sahi hai? | You said: {facts} -- is that right? |
| button `echo_yes` | Haan | Yes |
| button `echo_edit` | Badlein | Change |
| `to_sale` / `to_rent` (in {facts}) | khareedne ke liye / kiraye par | to buy / to rent |
| `TEMPLATES echo_edit` | Theek hai, sahi detail likh kar ya bol kar bhej dein. | Okay, please type or say the correct details. |
| `TEMPLATES echo_stale` | Ye sawal ab purana ho gaya hai. Kuch badalna ho to likh kar ya bol kar bata dein. | That question is out of date. If you want to change something, please type or say it. |
| `TEMPLATES budget_operator_ask` | Budget {amount} tak hai ya {amount} ke aas paas? Please likh kar bata dein (jaise "{amount} tak"). | Is your budget up to {amount}, or around {amount}? Please type it (for example "up to {amount}"). |
| `TEMPLATES type_unclear` | Property ki qisam samajh nahi aayi: ghar, flat, plot ya office? Please likh kar bata dein. | I couldn't understand the property type: house, flat, plot or office? Please type it. |

{facts} = the stored values as the wrapper writes them, e.g. "DHA Phase 6, 500 gaz, 8 crore tak,
khareedne ke liye" / "DHA Phase 6, 500 sq yd, up to 8 crore, to buy". {amount} = e.g. "8 crore".
Reused (not new): `budget_unclear` "Budget ki raqam samajh nahi aayi. Please ek raqam likh kar
bhejein (jaise 8 crore)."

## Tests

`tests/test_voice_flow_db.py` (21; real worker, real `SpeechService` on fake Scribe / Gemini, the
Gemini fake transport for the model, seeded demo agency, RLS): scenario 2 by voice in Roman Urdu
and in Urdu script (slots, exact ids, the Roman Urdu wrapper, purpose note once, no
confirmation); Scribe's mixed Devanagari asks type and operator and searches nothing, then a
typed answer searches; Devanagari amount asked; weak transcript confirmed first, Badlein (and the
search still blocked after it), Haan, repeated Haan; Haan on an older question; Haan on a question sent over 24 h ago; a new question on day-old
values is answerable; an old button stays stale after a new question and a timestamp refresh;
a Haan quoting no question; model `uncertain`; `uncertain: false`
on weak slots; an open location question before the confirmation; implausible heard budget;
voice + typed correction in one turn and across turns; silent note; both providers failing; a
retried voice turn pays once and sends once; a retried Haan writes once; logs carry no
transcript or phone. `tests/test_search_hold_db.py` (6): a fresh conversation waits for the
budget answer and asks again; an existing budget is not searched while its replacement is asked,
and a correction of another slot is no answer; a size question holds too; an old unanswered question is asked again (with and without an
earlier budget, which stays unused);
a retried held turn asks once and searches nothing. Pure: `tests/test_requirements.py`
(repeated amounts for text and voice, operators in three scripts, an operator
right after the span for text and voice, Urdu type words, unread type / budget asked, scenario 2
in Roman / Urdu / mixed Devanagari / Devanagari, script is not uncertainty, `uncertain` flag,
every uncertainty reason), `tests/test_render.py`,
`tests/test_customer_tools.py` (one question per turn, in priority).

## Live acceptance: customer B, scenario 2 (owner runs it; team phones only)

Prerequisites: owner approval of the policy and wording above; this branch reviewed and merged;
a release image built on the current production base (`manzil-ai:10.1-7f5cd3d`) per the deploy
rules; no migration; settings unchanged (`AI_PROVIDER=gemini`, `STT_PROVIDER=elevenlabs`,
`STT_FALLBACK` as today); the free Scribe minutes left for at least a few notes.

1. **Clean start.** Customer B must have no stored requirements and no open question (a reused
   rental conversation keeps `purpose = rent`: 10.1 handoff). Read-only (tenant session):
   ```sql
   SELECT c.id, rs.version, rs.slots ? 'purpose' AS has_purpose,
          (SELECT count(*) FROM pending_clarification pc
            WHERE pc.conversation_id = c.id AND pc.status = 'open') AS open_questions
   FROM conversation c LEFT JOIN requirement_state rs ON rs.conversation_id = c.id
   WHERE c.customer_wa_id LIKE '%<last 4 digits of customer B>';
   ```
   If anything is stored, use a phone with no conversation, or clear it by the owner's own
   procedure (not part of 10.2). Confirm DH6-101, DH6-102, DH7-101 are in `v_public_listing` and
   DH6-105 / DH6-106 are not.
2. **Send** from customer B one ~8 s voice note, quiet room: "Assalam o alaikum, mujhe DHA Phase 6
   mein 500 gaz ka ghar chahiye, budget aath crore tak hai."
3. **Expected:** the purpose note, then "Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options
   hain:" and the cards DH6-101, DH6-102, DH7-101 (DH7-101 labelled "Phase 7 -- qareeb"). Other
   outcomes to record, not to work around: the confirmation question (record why: `stt_meta`,
   slot `echo`), or the operator question (Scribe wrote Devanagari: an STT-setting finding for
   7.2).
4. **Read-only checks** (no full phone numbers in notes):
   ```sql
   SELECT m.id, m.created_at, m.stt_meta->>'status', m.stt_meta->>'provider',
          m.stt_meta->>'fallback_reason', m.transcript
   FROM message m WHERE m.message_type = 'audio' AND m.conversation_id = '<id>'
   ORDER BY m.created_at DESC LIMIT 1;
   SELECT version, slots FROM requirement_state WHERE conversation_id = '<id>';
   SELECT p.listing_code, s.position FROM shown_item s JOIN property p ON p.id = s.property_id
   WHERE s.search_run_id = (SELECT id FROM search_run WHERE conversation_id = '<id>'
                            ORDER BY created_at DESC LIMIT 1) ORDER BY s.position;
   ```
   Pass: location DHA Phase 6, 500 sq yd, budget max 8 crore, purpose sale (inferred); exactly the
   three codes; the reply in Roman Urdu.
5. **Latency** (receipt -> first reply, <= 10 s; one run is a sample, not the 13.3 p90):
   ```sql
   SELECT min(o.sent_at) - e.received_at AS first_reply_after_receipt,
          max(o.sent_at) - e.received_at AS last_payload_after_receipt
   FROM message m
   JOIN inbound_event e ON e.id = m.inbound_event_id
   JOIN outbound_message o ON o.dedupe_key LIKE 'turn:' || m.turn_id || ':%'
   WHERE m.id = '<audio message id>' AND o.sent_at IS NOT NULL
   GROUP BY e.received_at;
   ```
   `received_at` is the webhook receipt, `sent_at` when Meta accepted the send. On a confirmation
   path report the time to the question and, separately, the time to the cards after Haan.

## STT benchmark (Task 7.2) status

Not run and not passed. The harness is merged (`app/stt_bench.py`, docs/STT_BENCHMARK.md), but no
benchmark recording set, `bench/voice_manifest.csv`, verified gold (`bench/voice_gold_verified.json`),
live provider run, report or control-gate result exists in the repository. Live voice notes that
were recorded and stored are acceptance / smoke checks, not benchmark data: the gate M1(c) note
(2026-10-11, 6.48 s, docs/MEDIA.md), the 6.3 live checks L1-L3 (2026-10-12, docs/SPEECH.md), and
the owner's 10 s Scribe access test (docs/TASK_LOG.md 2026-10-13). Any other pilot audio objects
recorded elsewhere are not described in the repository docs and are not reviewed here.

## Limitations (unsupported scripts and other known gaps)

1. Devanagari is not read by the normalisers or the resolver (no transliteration; 7.1 / 7.2
   decisions): a Devanagari place is `location_unknown`, a Devanagari type, amount or operator is
   asked. A Devanagari transcript therefore cannot pass scenario 2; that is decided at the source
   by the STT setting (7.2), not here.
2. A value the model never extracts cannot be detected (the backend does not read Devanagari):
   with no type op, the search has no type, as for a customer who named none.
3. Devanagari size words are read only through the house default unit ("500 गज" = 500 sq yd
   because a house defaults to gaz); "गज" itself is not read.
4. The 7.3 plausibility guard needs a purpose before the budget, so a heard implausible budget with
   only an inferred purpose is not flagged (pre-existing).
5. An unanswered type / size / budget question holds the search until it is answered, however
   long (by design: no expiry silently lifts it).
6. Live scenario 2, its latency and the owner's approval of the policy, the Urdu type words and the
   wording are pending.

## Not done here

The 7.2 benchmark (above), the STT setting for Roman Urdu output, always-on voice echo (13.3 fallback), Devanagari support in the
normalisers, TTS, Task 10.3 and the 13.3 latency p90. The persisted-rent / implicit-purchase
improvement from the 10.1 handoff is a separate follow-up and is not touched.
