# Customer turn wiring (Task 10.1)

Wires the customer route to 7.3 requirements, 8.1 search, the 9.1 renderer / validator /
detail answers and the 4.2 media send. Handoff: "Document from taha.pdf" (2026-10-14).
No migration: `shown_item`, `focus`, `media_sent` and `search_run` exist since 006.

## Decisions (owner, 2026-10-15: the handoff defaults D1-D7, with clarifications)

| # | Decision | Built |
|---|---|---|
| D1 | Tools | `get_property` (details around `load_known`), `send_property_photos`; `send_location` deferred (not registered); `request_handoff` stays a stub that says no agent was told |
| D2 | When search runs | the model calls `search_properties` after `update_requirements`; the 8.1 gate decides |
| D3 | References | `app/references.py`: quote, code, position, description, focus; ambiguous -> one list question; every hit echoes `#n · CODE` |
| D4 | Grounding | every customer payload (cards, buttons, list rows, photo captions, notes) through `grounding.check_payloads` |
| D5 | Echo | `echo=False` when 7.3 sent `purpose_inferred` in the same turn |
| D6 | Language | `render.detect_language` per turn; a tap uses the customer's latest typed message; `preferred_language` stays unwritten |
| D7 | Card ids | the internal `message.id` of each sent card (looked up by dedupe key) goes to `shown_item.card_message_id` |

Owner clarifications (2026-10-15):

1. **One question per turn.** When a clarification is open, `search_properties` renders the 9.1
   question with answer buttons and the 7.3 text question of the same turn is dropped
   (`customer_tools.QUESTIONS`). No listing cards are sent while a question is open.
2. **Notes before cards**, in the customer's language (`turn_engine.TEMPLATES_EN`); what the
   results line already says (`requirements_saved`, `location_chosen`) is not repeated.
3. **"Kuch sasta" / "Budget barhayein" are separate.** Cheaper keeps the 7.3 rule (max =
   cheapest shown x 0.97, shown listings excluded, no budget step) and now never goes above the
   stated maximum. The `nomatch:budget` button asks for a new maximum (`budget_ask_sale` /
   `_rent`) and changes nothing. A typed "budget barha do" still uses the 7.3 rule (max x
   1.10, owner decision 2026-10-13).
4. **"Nearby area"** (`nomatch:nearby`): `search_conversation(..., nearby=True)` searches the
   requested place plus its curated `location_neighbor` places from the start, with every other
   requirement kept; neighbour cards carry the "-- qareeb" label; the stored location does not
   change. With no neighbour configured the customer is asked which areas they would consider
   (`nearby_ask`); nothing widens to the area or the city.
5. **D4 wording.** Card, detail and photo facts are rendered from rows re-fetched at answer time,
   so the verified database value is already what is sent. A payload that still fails the check
   (a template bug) is replaced by the agent-confirm reply, and its SHOWN entry is dropped so
   nothing counts as shown. A failing note becomes the failure template.
6. **D1 truthfulness.** `handoff:request` and `request_handoff` answer `handoff_unavailable`
   ("abhi kisi agent ko itlaa nahi di gayi").

## Flow

- Engine (`turn_engine.TurnEngine.plan`): a `customer_tools.CustomerBinding` wraps the 7.3
  binding. Tool payloads, notices and the internal plan entries go into the stored reply plan
  (`cards.fixed_plan`), so a retry sends the same plan. `render_customer` orders, localises and
  grounds the reply.
- Taps (`TurnEngine.tap`, called by the orchestrator before the 5.2 stub): handled in code,
  never sent to the model, under the same fixed plan.

| Tap | Answer |
|---|---|
| `photos:CODE` | the listing's unsent photos (focus via `button`) |
| `listing:CODE` | the listing's card from its public row |
| `visit:CODE`, `similar:CODE` | truthful stub notices (11.x) |
| `clarify:location:{n}` / `none`, `clarify:purpose:sale` / `rent` | `RequirementService.choose` (patch row, call `tap`), then a search; a stale tap changes nothing |
| `nomatch:nearby` / `nomatch:budget` | clarifications 4 / 3 above |
| `handoff:request` | `handoff_unavailable` |

- Send (`customer_reply.send`): payloads in order under `turn:{id}:{seq}`; a refused carousel
  falls back to cards; SHOWN is recorded only after every earlier payload was sent; an IMAGE
  entry goes through `media.send_image` and `media_sent`.
- Photos: `PHOTOS_PER_ASK = 5` unsent photos per ask, in `sort_order`, public listings only;
  the cover counts as sent once the listing was shown on a card.

## New customer wording (awaits owner approval)

Roman Urdu (`TEMPLATES`): `nearby_ask`, `listing_unknown`, `no_more_photos`,
`visit_unavailable`, `similar_unavailable`, `details_unavailable`; the reference question
"Aap kaunsi property keh rahe hain?" [Chunein]. English versions of all customer notices
(`TEMPLATES_EN`) and "Which property do you mean?" [Choose]. The detail answers now echo
"#2 · DH6-102" (was "#2 DH6-102").

## Tests

`tests/test_customer_search_db.py` (T1-T2), `tests/test_customer_flow_db.py` (references,
photos 3a, details, taps, 3b / 3c, nearby, one question per turn, grounding) and
`tests/test_customer_tools.py` (pure). Live WhatsApp checks wait for the owner's review.

## Limits

- 3a live needs DH6-102's photos (4.4); a carousel needs at least 2 covers.
- A quoted carousel resolves only with a position on it (one WAMID for all cards).
- `PHOTOS_PER_ASK` and the cover rule are project choices.
- Urdu-script replies, voice end to end (10.2), the real handoff (11.1) and `send_location`
  are out of scope.
