# Day13 remaining closeout decisions — draft, not accepted

Tasks 13.1/13.2 are implemented and deployed. Task 13.3/13.4 tooling is deployed;
the following acceptance gaps remain. Existing stabilization evidence is reused.
No new full manual campaign is proposed by default.

| Gap | Evidence / practical limit | Decision still required |
|---|---|---|
| Text latency | Low baseline and minimal experiment cannot meet the planned 7 s p90; minimal T01 11.534 s, T02 7.162 s to substantive Meta acceptance | Fix latency or explicitly accept a conditional closeout with this critical gap deferred; no SLA pass |
| Voice timing | Historical notes recovered; six approved current-cohort voice cases unrun | Explicit deferral or bounded current-runtime measurement; no voice SLA pass |
| Prompt cache | Cached-input counts all null for the comparison | Defer verification or fix observability/eligibility; null is not zero hit rate |
| Per-turn cost | Incomplete usage/dollar pricing evidence on free accounts | Explicit deferral or complete metering/pricing evidence; free access does not prove production unit economics |
| Full benchmark #2 | Historical functional evidence retained; formal full Run2 not established | Accept a documented evidence-reuse/exception closeout or execute missing required coverage; no full benchmark pass |

No Sonnet-primary or new always-on voice-echo fallback is adopted by this trial.
No paid account/provider change is authorized or performed. Remaining scope gates
in status.json keep their existing evidence and unwaived state. Owner acceptance
of a conditional closeout must be recorded explicitly; this draft is not approval.

Cleanup complete: owner returned DAY13-LOW-EFFORT-RESTORED at
2026-11-11T19:53:16.277923+00:00. Low is active on the same verified image;
.env and all trial evidence are preserved. No additional provider calls or
database writes were made by the restoration operator. Decision above remains pending.
