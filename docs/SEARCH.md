# Search service (Task 8.1)

Status (2026-10-14): implemented and tested locally on `feature/task-8-1-search` (from
`origin/main` 5472481). Not merged, not deployed, no migration. Nothing calls it yet: the
`search_properties` tool, the result cards and the no-match reply are wired in Task 10.1.

Sources: R17 2.1 (stages 4-8 and the output payload), R17 edge cases 1, 8, 9, 14; Demo Scope
section 5 (scenarios 1, 2, 3b, 3c and the "cheaper" rule); Red-Team R-24 (post-search
assertion), R-25 (rank rules); docs/BENCHMARK.md (gold tolerances); docs/REQUIREMENTS.md
(the 7.3 slot shapes); the D2 handoff for 7.4 / 8.1. Code: `app/search.py`; tests
`tests/test_search.py` (offline), `tests/test_search_db.py` (seeded dataset, RLS).

## API

```python
from app import search
result = search.search_conversation(conn, agency_id, conversation_id, gazetteer)
result.status            # ok / no_match / clarification_needed
result.payload(gazetteer)  # what the model / renderer sees (R17 2.1 output)
# after the result cards were actually sent (Task 10.1):
search.record_shown(conn, agency_id, conversation_id, result.search_run_id,
                    [hit.row.id for hit in result.hits[:search.TOP]], card_message_ids)
```

`search.run(conn, gazetteer, conversation_id=, requirement_version=, slots=,
pending_open=, city_code=, nearby=)` is the same inside the caller's tenant transaction.
`nearby=True` (the no-match "Nearby area" tap, Task 10.1) enables the neighbour step from the
start, next to the requested place and with every other requirement kept; with no curated
neighbour it searches nothing and sets `Result.no_neighbours`. A relative "cheaper" never sets
a maximum above the stated one (owner decision 2026-10-15).

## Rules

| Stage | Rule |
|---|---|
| Gate | purpose (an inferred purpose passes and is listed in `echo`); a RESOLVED location; one of type / category, budget, size; no open `pending_clarification`. Otherwise `clarification_needed` with `missing[]`, no `search_run` |
| Hard filters (never relaxed) | RLS agency, `v_public_listing` (ACTIVE only), purpose, property types (apartment includes penthouse; a category alone allows its types), the union of the requested subtrees (the agency city for ANYWHERE_IN_CITY), construction / possession / investment tags when stated (D11 enums only) |
| Tolerances | price <= max x 1.05 (above max = `above_budget`), >= min x 0.9; size +/-10 % of the nominal sq yd (house / plot / portion) or +/-15 % of the sq ft (other types), min / range modes honoured; beds / baths >= minimum |
| Ladder (< 3 results, cumulative, stop at >= 3) | budget +10 % (cap = max x 1.10 x 1.05; skipped when `no_budget_relaxation`, the cheaper rule) -> size +/-20 % -> neighbours (`location_neighbor`, both directions, of the requested nodes and their sub-area ancestors) -> beds - 1 |
| Ranking | in-budget > above-budget, then exact > relaxed (owner decision 2026-10-13: the budget rule wins a conflict), then the R17 score, then `last_confirmed_at` (newer first), then listing code. Score: location 30 (exact 1.0 / descendant 0.9 / neighbour 0.6; city-wide 1.0), price 25 (around: closeness to the stated amount; max: closer to the max is better; above max penalised), size 15, soft tags 15 (0: no vocabulary yet), freshness 10 (linear to 0 at 45 days), media 5 (>= 3 photos) |
| Shown | listings of the latest search with shown items (24 h) are demoted below the unshown ones unless they rank top 3; `excluded_property_ids` (cheaper) never return |
| Assertion (R-24) | every result is re-checked: status, purpose, type, inside the agency city, inside the scope of its own step (neighbour subtree only with the neighbour label), not excluded, price <= its step's cap, construction. A violation is dropped and logged as an error (listing id + reason code only) |
| Payload | top 5 with public facts and `above_budget`, `relaxations`, `location_match`; `total_matches`; `relaxations_applied`; `excluded_reasons` counts (already_shown, over_budget, under_budget, size, beds, baths); `unmapped_prefs`; `echo` (unconfirmed slots and their echo codes); on a no-match `lowest_price_pkr` in the requested scope (R17 edge 8) |

## When a listing counts as shown

`run()` writes one `search_run` (customer-visible `filters`, `relaxations`,
`total_matches`, the requirement version). `shown_item` rows are written only by
`record_shown()`, which 10.1 calls after the cards were sent, so a failed send or a
no-match leaves no display history. 7.3's `RequirementService._shown` (cheaper / bigger)
now reads the latest search **with shown items**, so a later no-match or unsent search
never hides what the customer last saw (tested).

## Golden results (tests/test_search_db.py)

| Case | Result |
|---|---|
| Scenario 1 (office, rent, Karachi, around 5 lakh) | {DH6-108, SHF-101, PEC-101}, no relaxation, DH6-108 first |
| Scenario 2 (house, Phase 6, 500 gaz, 8 crore tak) | DH6-102, DH6-101 exact + DH7-101 labelled `neighbour`; purpose inferred -> echo |
| 3b ("kuch sasta" after scenario 2 was shown) | no match; budget step skipped; 3 already shown excluded; lowest price 9.5 cr |
| 3c ("Phase 8 bhi") | DH8-102 (in budget) then DH8-101 (`above_budget`); DH7-101 stays excluded |
| Bench gold (all 48 `results` / `no_match` items, text + voice) | ids shown, nothing outside ids + allowed; no-match items empty |
| Hidden | DH6-105 SOLD, DH6-106 RESERVED, CLF-104 RENTED never returned |

Order inside scenario 2 follows R17 ("under budget and close to max is best": 8.0 cr before
7.8 cr); Demo Scope judges set membership, not order.

## Decisions and limits

- Echo flags do not block the search: an unconfirmed slot (inferred purpose, voice,
  implausible budget) is listed in `echo` for the wrapper; the voice-critical echo before a
  search is Task 10.2 (docs/VOICE_TURN.md: `CustomerBinding.search_properties` asks first). 7.3 keeps even a restated implausible budget unconfirmed, so a
  blocking gate would never open.
- After injected invalid rows the ladder may have stopped early; the assertion only removes
  rows (tested: 2 valid results remain). It is the last line of defence, not a filter.
- Candidate rows are the hard-filtered scope (requested + neighbour subtrees); tolerances
  run in code. Fine for the demo inventory; a price / size bound in SQL is a later
  optimisation.
- Soft-preference tags score 0 until a vocabulary exists (docs/REQUIREMENTS.md).
- Duplicate groups (R17 edge 14) are not deduplicated: no `duplicate_group_id` exists.
- Clifton neighbours are not seeded (docs/DEMO_DATASET_GAPS.md).
