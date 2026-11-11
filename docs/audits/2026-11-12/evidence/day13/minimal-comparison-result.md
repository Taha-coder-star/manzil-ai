# Minimal-effort comparison complete

Same image, models and providers; only thinking effort changed. Two planned text
cases were observed per the separate minimal anchor. The owner's accidental prior
input remains in raw evidence and is explicitly excluded as a protocol deviation.

| Case | Low: first substantive Meta acceptance | Minimal: first substantive Meta acceptance |
|---|---:|---:|
| T01 office | 11.929 s | 11.534 s |
| T02 house | Failed: HTTP 503 after search | 7.162 s |

T01 remained slow; these observations do not establish general improvement or
prove that effort caused the different T02 outcome. Both minimal turns used three
primary LLM calls. No minimal provider error occurred in these six calls.

T02's 6.686 s text was a purchase-assumption notice, excluded from substantive
latency. Its carousel included the summary and expected listings: accepted at
7.162 s, delivered at 11.060 s. The #3 coverless card was accepted later at 7.734 s
but delivered at 10.060 s. This matches the screenshot's order: a delivery-order
inversion, with correct application acceptance order. No later fallback rows were
found. Do not file this as a confirmed application ordering bug from this evidence.

For the fixed ten-text plan and nearest-rank p90, two observed values above 7 s
mean even eight instantaneous remaining responses give p90 >= 7.162 s. This is a
lower bound, not a completed percentile. Eight text and six voice cases are unrun,
retained as unrun. Further same-cohort samples cannot rescue that target.

Experiment ended: owner returned DAY13-LOW-EFFORT-RESTORED at
2026-11-11T19:53:16.277923+00:00. Low is restored on the same verified image,
with all trial evidence preserved. No application patch, migration or billing change was made by this review.
No cache-hit, thinking-zero, dollar-cost, voice or full-Run2 pass is established.

Sources: minimal-send-review-owner.json, minimal-diagnostic-owner.json,
t02-diagnostic-owner.json, owner-visible screenshots and accidental-input correction.
