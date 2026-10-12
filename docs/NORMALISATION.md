# Normalisers (Task 6.2, component C7)

Status (2026-10-12): implemented and tested offline on `feature/task-6-2-normalisers`
(from `origin/main` f76e30e). Pure code in `app/normalise.py`: no database, no LLM, no
new dependency, no migration. Nothing calls it yet; 7.3 (state manager), 8.1 (search),
8.2 (staff extraction) and 9.1 (grounding validator) are the callers.

Sources: Data Model (R11-R13) section 4 (4.1 units, 4.2 money, 4.3 budget, 4.4 purpose),
R17 section 4, CLAUDE.md section 6, docs/BENCHMARK.md (gold rules), Red-Team R-21 / R-22,
and the owner decisions of 2026-10-12 (docs/DECISIONS.md).

## API

Every parse returns a frozen dataclass with `status`: `ok`, `ambiguous` (the caller asks;
`reason` says why) or `none` (nothing found). Nothing is guessed.

| Function | Use |
|---|---|
| `parse_money(span)` | One amount -> integer PKR. Two separate amounts -> ambiguous |
| `find_money(text)`, `find_areas(text)`, `find_numbers(text)` | Every match with `start` / `end` / `value`: for the 9.1 validator (it must parse digits and Urdu number words the same way, R-21) and the R-22 re-extraction path |
| `parse_number(text)` | The first number expression, any kind |
| `parse_area(span, *, property_type, marla_sqft=225)` | Size -> `value`, `unit`, `sqft`, `unit_source` (stated / default), `nominal_sqyd`, `mode` (target / min / range), `min_sqft` / `max_sqft` |
| `nominal_bucket(sqyd)` | Nearest market bucket within +/-10 % (272 -> 250), else None |
| `parse_budget(span, *, purpose)` | Budget -> `mode`, `min`, `max`, `stated_pkr`, `period`; a relative phrase -> `{op: relative, relative}` |
| `parse_purpose(text, *, source)` | Explicit sale / rent wording -> `value`, `raw`, `source`, `confirmed=True` |
| `infer_purpose(pkr, category)` | From the amount alone -> `source='inferred'`, `confirmed=False` (echo before the first search) |
| `category_of(property_type)` | residential / commercial / None |
| `relative_op(span)`, `cheaper_max`, `higher_max`, `premium_min` | R17 4.5 phrases and formulas; the state rules stay in 7.3 |

`to_dict()` output uses the gold keys of docs/BENCHMARK.md (`budget {raw, min, max, mode,
period}`, `size {raw, value, unit, sqft}`, `purpose {value, raw, source}`), plus extras.

## Rules

- **Numbers**:
  - digits with thousands commas ("1,800"), comma decimal ("7,8 cr"), Urdu / Arabic-Indic digits;
  - Roman Urdu 1-99 (ek ... ninyaanve, incl. pachees, pachaas, assi, nabbe), sau / hazaar;
  - English one ... ninety-nine, hundred, thousand, "and a half";
  - a few Urdu-script words;
  - fractions: aadha 0.5, sawa +0.25, saade / saadhe / sarhe +0.5, paune -0.25, dedh 1.5, dhai 2.5 ("sawa crore" = 1.25 crore, "paune do crore" = 1.75 crore);
  - "saat point aath" = 7.8;
  - compounds "1 crore 5 lakh", "ek crore saath lakh", "teen lakh pachaas hazaar".
- **Money**:
  - needs a multiplier (hazar / k / thousand, lakh / lac / L, million / m, crore / cr / karor / karod, arab, Urdu-script forms) or a currency word (Rs / PKR / rupees);
  - k, L and m count only when glued to digits ("50k", "5L", "2m");
  - a number word alone is never money ("do you", "saath" = with);
  - a number after a lakh / crore multiplier starts a new expression ("8 crore 500 gaz"), but one after sau / hazaar does not ("teen sau pachaas" = 350).
- **Area**:
  - 1 sq yd = 9 sq ft; marla = the agency's `marla_sqft` (225 default, 272.25 allowed by migration 007); kanal = 20 marla; acre 43,560 sq ft.
  - No unit: house / plot / portion up to 2,000 -> sq yd; apartment / flat / shop / office / showroom / warehouse / building / floor -> sq ft; above 2,000 for a sq-yd type, or an unknown type -> ambiguous.
  - The bucket is computed for sq-yd types (and for sq-yd / marla / kanal units when the type is unknown).
- **Budget**:
  - `max`: under / below / tak / max / up to / se kam / zyada se zyada / تک.
  - `around` (+/-10 %): around / lagbhag / kareeb / taqreeban / ke aas paas, **and a bare amount or "budget X"**.
  - `min`: kam se kam / at least / se upar.
  - `range`: "X se Y", "X-Y", "between X and Y", with a shared trailing multiplier ("2 se 3 crore").
  - A max word beats "budget" ("budget aath crore tak" = max).
  - Period: a monthly marker (per month / a month / monthly / mahana) -> monthly, else from the purpose (sale -> one_time, rent -> monthly), else None.
  - Search tolerances (max x 1.05 "above budget", min x 0.9) are 8.1's, not stored here.
- **Purpose**:
  - Explicit wording wins, rent before sale ("kiraye pe lena hai" = rent).
  - Rent: on / for rent, rent pe, kiraye pe, kiraya, lease, کرائے پر.
  - Sale: for sale, buy, purchase, khareedna / khareedne / khareedni hai, lena hai, sale, خرید.
  - A monthly amount marker means rent.
  - Inference by amount: residential <= 15 lakh rent, >= 50 lakh sale; commercial <= 25 lakh rent, >= 1 crore sale; in between ambiguous. Unknown category: rent only up to 15 lakh, sale only from 1 crore.

## Deliberately unresolved / not here

- "1,800 ka plot": Data Model 4.1 says the number "contradicts the type, ask", but the <= 2,000 rule (CLAUDE.md, BENCHMARK.md) gives 1,800 sq yd. We follow the rule; recorded in OPEN_ISSUES.
- A 272.25 marla basis puts Bahria "10 marla" in the 300 bucket (2,722.5 sq ft), not 250. That's correct for that basis; the demo agency uses 225.
- Plausibility bands, rejecting impossible combinations, the buy <-> rent `previous_budget` move, the "cheaper" ladder rule (skip the budget step, exclude shown) and merging into `requirement_state` are Task 7.3. Location is 7.1. Search tolerances and ranking are 8.1. Display formatting ("7.8 crore", "85 lakh") is 9.1.
- `parse_number` also reads a lone number word ("do" -> 2). Money and area never do without a multiplier or unit.

## Tests

- `tests/test_normalise.py`: 166 table-driven cases (money, non-money, areas, ambiguity, marla basis, buckets, area modes, budgets, relative ops, purpose explicit / inferred boundaries, categories, consistency with `app.benchmark`'s unit table).
- `tests/test_normalise_bench.py`: every gold budget / size / purpose slot in `bench/` (116 cases). `KNOWN_DIFFERENCES` is empty.
