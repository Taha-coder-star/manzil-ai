# Low-effort baseline findings and next tuning choice

| Attempt | First substantive Meta acceptance | Outcome |
|---|---:|---|
| T01 original |11.929345s|Correct offices and prices|
| T02 original |None; failure apology accepted at15.021701s|Gemini503 after successful requirement update and search|
| T02 manual resend |19.953195s|Correct houses after primary timeout, failover and continuation|

No429quota error was recorded. There was no automatic recovery row; the success
belongs to a separate manually resent turn. Engine/job `replied`/`succeeded`
does not turn a failure-template answer into a successful substantive response.

The frozen10text cohort cannot meet7s p90: T01 exceeds7s and first T02 is a failure
(infinity). Even eight hypothetically instantaneous remaining observations leave
the ninth-ranked value at least11.929345s. This is a mathematical lower bound,
**not a fully measured10sample p90**. Eight text/six voice samples remain unrun;
the incomplete baseline and resend remain visible rather than being discarded.

## Source inspection

T01 used3provider calls; provider durations totaled8.984s. The final stop_empty
call alone took4.864s. T02 first used3calls and failed on the last one, after
search already succeeded. The resend used5calls after timeout/failover; the
interim notice is not the SLA endpoint. The optional continuation after an
active model is separate from durable recovery after initial provider failure.

Simply removing the final model round can suppress a requested subsequent tool
action or compound request. No such optimization has been applied. Even the
arithmetic T01-minus-final-call estimate is7.065345s, not proof of a passing fix.

## Concrete configuration candidate

```diff
- LLM_THINKING_LEVEL=low
+ LLM_THINKING_LEVEL=minimal
```

Same976dff image, same Gemini primary/failover, same STT/providers, same safety,
tools, grounding, roles, data and schema. Both configured models support minimal
in the official GenerateContent thinking guide. Minimal is an effort level,
not a guarantee of no thinking or a cure for service503s.

This would recreate web/worker with a temporary Compose override and begin a
**separate** cohort. Candidate override and image-only low restore override are
prepared in this directory; no `.env` or runtime settings have been changed.
The local request-construction check confirms only thinking configuration differs.

Compare the same T01/T02 cases first. If quality/latency improve, continue the
remaining small timing sample on that fixed candidate. Failed low results stay
historical baseline, never silently relabelled. No full Run2/cost/cache waiver.

The currently approved timing plan pins low effort. Owner scope choice is needed
before switching to this candidate rather than finishing that original batch.
No provider requests, app-code changes, deployments or AWS mutations were made.
