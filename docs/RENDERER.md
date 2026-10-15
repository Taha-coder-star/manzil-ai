# Renderer and grounding validator (Task 9.1, C11)

Sources: Red-Team R-21 (template facts), R-05 (unknown path, feature vocabulary, legal always
deferred), R-06 (prohibited promises), R-26 (answer-time re-fetch), R-36; RFC C11; D9, D15; Demo
Scope scenario texts (p5-7); CLAUDE.md rule 1. Taken over from Taha on 2026-10-14
(`Manzil_AI_D2_Handoff_Task_9_1.pdf`).

## Decisions (owner, 2026-10-14)

- **The wrapper line is a template.** No model prose reaches the customer. The validator and the
  regenerate-once helper are built and tested with injected candidate text (section 4). They are
  the seam a future model-written wrapper would call. Nothing calls them live.
- **Words follow the customer, facts do not.** English or Roman Urdu labels and buttons. Codes,
  `PKR` amounts, units and place names are identical in both (D15).
- **The unknown-field answerer is pure, in 9.1.** 10.1 registers the customer tool that calls it.
  `app/ai_tools.py` and `app/turn_engine.py` are unchanged, so there is no conflict with PR #37.

## 1. Contract for 10.1 (`app/render.py`)

```python
plan = render.render_search(
    result.payload(gazetteer),                       # 8.1 search.Result.payload
    cards.load_listings(conn, agency_id, codes),     # the answer-time re-fetch (R-26)
    render.detect_language(last_text, preferred),    # "english" | "roman_urdu"
    request=search.Request.from_slots(slots),        # wrapper, echo, no-match wording
    gazetteer=gazetteer, pending=pending,            # pending: the open clarification
    fallback=carousel_fallback_flag, echo=True)
```

`RenderPlan` fields:

| Field | Meaning |
|---|---|
| `payloads` | Ordered WhatsApp payloads for `cards.fixed_plan` / `cards.send_plan`. |
| `kind` | `results`, `no_match` or `clarification`. |
| `shown` | `[(position, property_id, code)]` as rendered. 10.1 passes the ids to `search.record_shown` after a successful send. |
| `trusted_codes` | The codes `turn_engine.ground_reply` may let through. |
| `facts` | The `grounding.Facts` of what was rendered (for any later wrapper). |
| `dropped` | Payload codes that had no public row at re-fetch. |

- **Re-fetch boundary.** Card facts come from `cards.load_listings` (v_public_listing, tenant
  transaction, ACTIVE only), never from the payload.
  - A result whose listing is no longer public is dropped and the positions close up, so "#n" is
    what was sent.
  - If every result is gone, the reply is a no-match.
  - Version or price-change notices ("Update: price ab ...") are 10.1's.
- **Pure.** The renderer has no DB, model or clock. Every plan goes through
  `grounding.check_payloads` over all of its texts, including card bodies, headers and button
  titles. A template that fails raises `GroundingError` instead of being sent.
- **Echo.** Unconfirmed slots in `payload.echo` get one line each. Since 7.3 already sends
  `purpose_inferred` in the turn it infers the purpose, 10.1 should pass `echo=False` there or drop
  the 7.3 notice.

### Formats

| Search status | Reply |
|---|---|
| `ok` | The wrapper and echo lines open the first message. 1 result: image (or text-header) card with 3 buttons. 2-5: a carousel with the numbered summary as its body. Coverless listings and the per-contact fallback are as in Task 5.2. |
| `no_match` | One reply-buttons message: the no-match line, plus "price starts from" when `lowest_price_pkr` is set, and [Nearby area] [Budget barhayein] [Agent se baat]. |
| `clarification_needed` | One question, never listings. A location pending with 1 candidate gets [Haan] [Nahi, aur jagah]; 2-3 short names get buttons; otherwise a list. Purpose gets [Khareedna] [Kiraye par]. A missing location or type / budget gets a text question. |

- **Card labels.** Relaxations are never silent:
  - `location_match = neighbour` → "Phase 7 -- qareeb" / "Phase 7 -- nearby" (the place's short
    name);
  - `above_budget` or a budget relaxation → "budget se thoda upar" / "slightly above budget";
  - size → "size thoda alag" / "size differs a little";
  - beds → "bedroom kam" / "fewer bedrooms".
- **Button ids** (tap handling is 10.1): `nomatch:nearby`, `nomatch:budget`, `handoff:request`,
  `clarify:location:{index into the stored candidates}`, `clarify:location:none`,
  `clarify:purpose:sale|rent`. Card buttons are unchanged from 5.2: `photos:` / `visit:` /
  `similar:{code}`.
- **Language.** `detect_language(text, preferred)`:
  - a stored preference wins;
  - otherwise English only when the text has at least 2 English words and more English than
    Roman Urdu words;
  - mixed, unclear or Urdu-script text gets Roman Urdu. Urdu-script replies are on the cut list.

## 2. Exact wording (for owner approval)

Rendered from the tests' demo rows (`app/render.py` `TEXT`, `app/cards.py` `WORDS`).

```
S1 EN  Here are offices for rent in Karachi, around 5 lakh per month:
       3 matching listings:
       #1 DH6-108 · Office for rent · Bukhari Commercial · PKR 5 lakh / month
       ...
       Reply with a number, or tap a card.
       card: #1 DH6-108 · Office for rent / Bukhari Commercial · 1200 sq ft /
             PKR 5 lakh / month · Ready      [More photos] [Visit / Details]

S2 RU  Ji, DHA Phase 6 mein 500 gaz, 8 crore tak -- ye options hain:
       Ye khareedne ke options hain -- agar kiraye par chahiye to bata dein.
       3 options:
       #1 DH6-102 · Ghar · sale · DHA Phase 6 · PKR 8 crore
       #2 DH6-101 · Ghar · sale · DHA Phase 6 · PKR 7.8 crore
       #3 DH7-101 · Ghar · sale · DHA Phase 7 · PKR 7.5 crore · Phase 7 -- qareeb
       Number likh kar jawab dein, ya card par tap karein.
       card: #3 DH7-101 · Ghar · sale / DHA Phase 7 · 500 sq yd · 4 bed / 5 bath /
             PKR 7.5 crore · Tayyar · Phase 7 -- qareeb      [Aur photos] [Visit / Detail]

S3b RU DHA Phase 6 mein is se kam price ka 500 gaz ghar abhi available nahi. Yahan price
       9.5 crore se shuru hoti hai.   [Nearby area] [Budget barhayein] [Agent se baat]
S3b EN No cheaper 500 sq yd houses for sale in DHA Phase 6 are available right now. Prices
       there start from 9.5 crore.    [Nearby area] [Raise budget] [Talk to agent]

S3c RU Ji, DHA Phase 6 / DHA Phase 8 mein 500 gaz, pehle se kam price par -- ye options hain:
       #2 DH8-101 · Ghar · sale · DHA Phase 8 · PKR 7.4 crore · budget se thoda upar

Unknown (R-05)  Ye maloomat mere paas nahi -- agent confirm kar denge.   [Agent se baat]
                I don't have that information -- the agent will confirm it. [Talk to agent]
Legal           Documents aur legal baatein hamesha agent hi confirm karte hain -- main aap ko
                agent se mila sakta hoon.                                  [Agent se baat]
Negotiation     Price ki baat agent hi karte hain -- main aap ko agent se mila sakta hoon.
Clarification   Aap ka matlab kaunsi jagah hai? / Kya aap {place} keh rahe hain? [Haan]
                [Nahi, aur jagah] / Aap property khareedna chahte hain ya kiraye par lena chahte
                hain? [Khareedna] [Kiraye par] / Aap kis area mein property dekh rahe hain? /
                Kis qisam ki property chahiye (ghar, flat, plot, office) aur budget kitna hai?
```

The other strings are the echo lines (budget / location / size "... samjha hai -- ghalat ho to
bata dein"), "Ghar / Flat / Dukaan / Godown", "Tayyar / Zer-e-tameer", "photos jald",
"Milti-julti", "Aur options", and "{n} aur options. Detail ke liye ek chunein.".

## 3. Grounding validator (`app/grounding.py`)

`validate(text, facts, gazetteer)` returns a list of `Violation(kind, text, detail)`.
`facts_for(listings, positions=, request=, gazetteer=, extra_money=)` builds the facts from
rendered `cards.Listing` rows and the customer's request only. Model text is never a source.

| Kind | Rule |
|---|---|
| `money` | `normalise.find_numbers` in digits, number words and Urdu script ("saade saat crore" = 7.5 cr). Each amount must be a rendered price (or its rounded display), a stated budget, or the no-match lowest price. A bare number of 1000 or more is checked too. |
| `area` / `unit` | (value, unit) must be rendered. "500 gaz" is fine for 500 sq yd; "500 sq ft" is a `unit` violation. |
| `count` / `position` | "n bed" / "n bath" must match. "#n" must be a rendered position, and "#n CODE" must pair them as rendered (the scenario 3a ID echo). |
| `code` | Any listing code not rendered. |
| `place` | Gazetteer aliases, names and display names. A mentioned place must be rendered or an ancestor of one ("DHA", "Karachi"). |
| `feature` | The 007 vocabulary (features, facing, corner, furnishing, possession, construction). A claim must be true in the row. Absent means unknown, so "gas nahi" is a violation too; only an explicit false (`is_corner = false`) may be stated negatively. |
| `promise` | guarantee, final price, mil jayega, zaroor mil, pakka, arrange ho jayega, 100%, discount, "price kam ho jayegi", plus Urdu-script forms (R-06). |

- **Per-listing attribution.** A value after a listing's code or "#n" (until the next reference) is
  checked against that listing's own row. "DH6-101 saade saat crore" is rejected even though 7.5 cr
  is DH7-101's true price. Text before any reference is checked against all rendered facts.
- **Not checked.** Bare small numbers that are not money, sizes, positions or room counts
  ("Phase 7", "3 options"). They cannot state a price, size or count alone.

**Regenerate once (R-21).** `ground_wrapper(generate, facts, fallback, gazetteer)` calls
`generate(None)` and validates the result. On a violation it calls `generate(violations)` once.
If that also fails, or the generator raises, it returns the template with outcome
`template_only`. It is exercised only with fake generators (tests).

`check_payloads(payloads, facts, gazetteer)` validates every customer-visible text of built
payloads. `turn_engine.ground_reply` reads only interactive bodies (`whatsapp.summary`), so 10.1
should switch it to `check_payloads` once #37 (which changes its call) has merged.

## 4. Unknown fields, legal, negotiation (`app/details.py`)

- `load_known(conn, agency_id, code)` re-fetches one listing's public facts from
  `v_public_listing`: the 007 columns plus the allowed `public_features` keys. It returns None for
  a hidden or other-agency listing.
- `field_for(text)` maps question text to fields deterministically. Legal and negotiation always
  come first.
- `answer_detail(known, fields, language, position=None)` gives:
  - **known fields** → "#2 DH6-102: corner hai.";
  - **unknown fields, or a listing that is gone** → the agent-confirm template with
    [Agent se baat] (`handoff:request`);
  - **mixed** → the known lines, then the agent-confirm template;
  - **legal or documents** (documents, kagzat, NOC, registry, lease, legal, transfer, mutation,
    ownership, file clear, approval) → always the agent, even when a related field exists;
  - **negotiation** (price kam, negotiable, discount, last / final price) → always the agent
    (CLAUDE.md section 10). `price_negotiable` is never told to the customer.

## 5. Changes to existing code

`app/cards.py` gains these optional parameters, with defaults that keep the 5.2 output byte-for-byte
(`tests/test_cards.py` unchanged, all passing): `language`, `labels` (`{code: (label, ...)}`) and
`note` (a line opening the first message). `Listing` gains the public feature columns `features`,
`facing`, `is_corner`, `furnishing` and `possession_status` (defaults; `load_listings` selects
them).

## 6. Limits and open items

- Wired by Task 10.1 (docs/CUSTOMER_TURN.md): `search_properties` renders and sends the plan,
  then calls `record_shown`; the `nomatch:*` / `clarify:*` / `handoff:request` taps are handled
  in code; `get_property` wraps `details`. Detail answers echo "#2 · DH6-102".
- Wording awaits owner approval. The "Nearby area" button stays English in Roman Urdu, as in Demo
  Scope 3b.
- The Roman Urdu kind word is "Ghar · sale". Urdu-script replies are not rendered (cut list).
- The feature vocabulary covers the 007 enums only. Project terms (payment plan, approvals) go to
  the agent through the legal / unknown path.
- The validator does not parse dates or percentages other than "100%". A date in a wrapper would
  pass unchecked, so the wrapper stays templated.
