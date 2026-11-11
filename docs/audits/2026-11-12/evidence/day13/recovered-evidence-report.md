# Day13 recovered evidence — measured gaps identified

**Evidence recovery is complete; Day13 acceptance remains open.**
Owner's read-only audit captured at 2026-11-11T18:26:10.332376+00:00 recovered the latest
500 of 505 demo-agency turns. The omitted
five are the oldest turns, not current-build test results. The export is retained
with SHA256 `0e188e39a2026acd7ddef6713a46ca1f5cafec288b8853931c53283b3f51d7c5`.

| Evidence | Result |
|---|---:|
| Customer turns | 192 |
| Staff turns | 264 |
| Broker turns | 44 |
| Existing customer voice notes, stored/hash/duration present | 46 |
| Longest retained voice note | 80.86s |
| LLM usage rows | 483 |
| LLM rows with cached-input counts | 0 |
| Stored dollar estimates | 0 |
| Turns in current Day13 runtime window | 0 |

The recovered turns end on9November, before the pinned Day13 runtime window
starting10November17:35:06UTC. Historical first-answer candidates and failures are
retained in `recovered-timing-inventory.csv`; they are not pooled into a current
release percentile. Mechanical answer classifications are still unreviewed.
Clears, controls, handoff notices and different builds must not silently become
one SLA cohort. No failed or missing observations were replaced with faster ones.

## What can be reused

Previously accepted functional cases remain accepted with their original dates
and builds. There are46 genuine stored voice notes with hashes/durations; their
private bytes can be reused where needed. No blanket35-note rerecording is needed.
Their presence does not prove30case assignments, six-speaker/three-device/noise
coverage or human-reviewed gold; those are separate evidence requirements.

## What cannot be concluded from this export

- Current-build text/voice p50 or p90: there are no current-build samples.
- Prompt-cache hit rate: all483LLM rows have unknown cached-token counts.
  This is not a measured zero-percent hit rate.
- Complete per-turn dollar cost: all stored dollar estimates are NULL; output
  and thinking metadata are incomplete. None has all four required token fields.
  Free account status is recorded, but no actual zero-charge or production-price
  acceptance is inferred from that status.
- Full Run2 numerical coverage or a passed safety/quality percentage: an audit
  status and stored audio are not substitutes for scored cases and denominators.

## Smallest useful next work

Use only a preselected10text/6shortvoice current-build performance sample if fresh
latency evidence is required. Existing silence/long-note and functional/broker
acceptance need not be repeated merely for that sample. This small sample would
be explicitly qualified and would not silently replace the original full matrix.

Keep Gemini/low while measuring; no new provider, effort, billing, deployment or
paid guard activation is required by this evidence-analysis step. If any formal
coverage is deferred to close the delivery milestone, obtain a concrete recorded
owner decision. Mark unverified metrics as deferred, never as passed. Safety
requirements and actual reported failures remain visible.

No new provider calls, live tests, database writes or service changes were made.
