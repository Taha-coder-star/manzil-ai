# AI turn engine (Task 6.1, component C6)

Status (2026-10-12): implemented and tested locally on `feature/task-6-1-turn-engine`
(from `origin/main` 9368002), with the review fixes of section 11. **Not complete**: the
live acceptance checks in section 8 are pending, no model ID has been verified for this
account, and nothing is deployed. Migrations 013 and 014 are not applied anywhere except
disposable test databases.

Update (2026-10-12): merged (PR #27) and deployed in stub mode as `manzil-ai:6.1-f76e30e`
with 013 and 014 applied (Taha's handoff). Follow-up on `fix/task-6-1-followups`: the
interim message + continuation (section 3), failure notices for failed turns (section
4), the thinking level and the owner's model IDs (section 2). Live acceptance (L1-L5)
is still pending.

Sources: Execution Plan Day 6 (6.1), RFC sections 3, 5 and 9 (L1-L4), Red-Team R-17,
R-21, R-22, R-36, R-37, R16 sections 3 and 8, the 5.3 / 5.4 handoff (section 6), and the
owner's decisions of 2026-10-12 (section 9). Gemini facts: ai.google.dev pages read
2026-10-12 (models, caching, generateContent reference, thinking; each "last updated
2026-10-09").

## 1. Where it runs

`app.orchestrator._answer` keeps its order: control-state gate (turn-level) ->
mark-read -> staff `#demo …` command -> listing-card tap stub -> **reply**. The reply comes
from `app.turn_engine.TurnEngine.respond` when `AI_PROVIDER=gemini`, otherwise from the
Task 5.1 stub responders (default). Payloads still go out through
`app.outbound.send_message` under `turn:{turn_id}:{seq}`: the send-time gate (R-28,
`conversation.mode`), the 24 h window and at-most-once sending are unchanged.

| File | Role |
|---|---|
| `app/llm.py` | Gemini `generateContent` adapter over httpx (stateless), response classification, usage parsing, wall-clock deadline |
| `app/ai_tools.py` | Tool registry by route, argument validation, stub tool bodies |
| `app/turn_engine.py` | Turn loop, budget, failover, metering, context, templates, grounding seam |
| `app/config.py` | `AI_PROVIDER`, `GEMINI_API_KEY`, `LLM_PRIMARY_MODEL`, `LLM_FAILOVER_MODEL`; `check_ai_settings` |
| `app/handlers.py`, `app/orchestrator.py`, `app/worker.py` | Wiring; failed turns closed as `failed`; the worker exits (code 78) on unusable AI settings or schema |
| `migrations/013_usage_event_llm_detail.sql` | `usage_event.cached_input_tokens`, `thinking_tokens`, `outcome_detail` |
| `migrations/014_conversation_turn_failed.sql` | `conversation_turn.attempts` and the `failed` status |

## 2. Configuration

- `AI_PROVIDER`: `stub` (default) or `gemini`. Anything else is a configuration error.
- With `gemini`, all of `GEMINI_API_KEY`, `LLM_PRIMARY_MODEL` and `LLM_FAILOVER_MODEL`
  are required, and before claiming any job the worker checks, as the runtime login inside
  a tenant transaction, that migration 013's `usage_event` columns exist
  (`turn_engine.check_schema`). With **any** `AI_PROVIDER`, including `stub`, it also
  checks migration 014 (`conversation_turn.attempts` and the `failed` status;
  `orchestrator.check_schema`), because turn assembly uses it; a database that cannot be
  reached for this check also stops the start. Any failure logs `Worker not started: …`
  (setting or migration names only, never values or server text) and the process exits
  with code 78 (EX_CONFIG). It never falls back to the stubs silently. The web process
  does not read these settings. Deployment order: apply 013 and 014, then start this
  code.
- What the exit does in the demo deployment: `deploy/compose.yml` gives the worker
  `restart: unless-stopped`, so Docker restarts it after each exit with its increasing
  restart delay, and `docker ps` shows `Restarting (78)` instead of `Up`. Checked locally
  on Docker 29.4.1 (a container exiting 78 at once: 8 restarts in 20 s, status
  `restarting`); the EC2 Docker version has not been checked. No job type runs while the
  worker is down (messages, media, audio, status reconciliation, carousel fallback, lease
  reaping); the web process keeps storing webhooks, so jobs wait as `pending` and run once
  the settings are fixed. The routing / `DATABASE_URL` checks keep their older idle
  behaviour (not changed here).
- Model IDs have no defaults and must match `[a-z0-9][a-z0-9.-]{1,63}` (they become part
  of the request path).
- Model IDs (owner decision 2026-10-11, CLAUDE.md section 1; pinned, no aliases):
  primary `gemini-3.5-flash-lite`, failover `gemini-3.6-flash`. Benchmark run #1 decides
  whether Flash-Lite meets R16; if not, Flash becomes primary (config only). Free-tier
  availability and limits are shown only in AI Studio: check both IDs there before
  activation.
- `LLM_THINKING_LEVEL` (follow-up 2026-10-12): every call sends
  `generationConfig.thinkingConfig.thinkingLevel`, default `low` (Gemini 3 defaults to
  `high`; both chosen models accept minimal / low / medium / high per the thinking guide
  read 2026-10-12). `off` leaves the field out, a kill switch if the live check (L4)
  finds it rejected; any other value is a configuration error. It sits after
  `contents`, outside the stable prefix.
- The key goes only in the `x-goog-api-key` header; `Settings` hides it from `repr`.

## 3. Budget and deadline

Constants in `app/turn_engine.py`: `CALL_TIMEOUT_SECONDS = 8`, `TURN_BUDGET_SECONDS = 15`,
`MIN_ATTEMPT_SECONDS = 1`, `MAX_ITERATIONS = 3`.

These are scheduling limits on provider attempts, **not** a hard 8 s per request or 15 s
end-to-end guarantee.

- The turn clock starts when the engine starts building a plan (after turn assembly,
  the gate and mark-read). Its time counts the history query, every provider attempt
  (primary and failover), tool execution and the per-attempt usage inserts.
- Each attempt gets `min(8 s, 15 s - elapsed)`; none starts with less than 1 s left.
  Example: the primary times out at 8 s -> the failover gets 7 s; iteration 1 takes 3 s,
  iteration 2 times out at 8 s -> no failover (see section 4), but the continuation below.
- **Interim + continuation** (Red-Team R-17, follow-up 2026-10-12): when the turn budget
  runs out before an answer (an attempt cannot start with 1 s left, or an iteration ends
  in `timeout`), the engine sends `TEMPLATES["interim"]` ("Ek minute, check kar raha
  hoon.") once, through the orchestrator under `turn:{id}:interim` (send-time gate, 24 h
  window, at most once: a retried job never repeats it), then gets
  `CONTINUATION_BUDGET_SECONDS` (15 s) more and retries that iteration: the same model if
  one has answered (no cross-model replay), else primary -> failover. Attempts after the
  interim are metered under keys ending in `:c`. A second exhaustion ends the turn with
  the failure template. Fast provider errors (503 on both models), refusals, malformed
  answers and 4xx get no interim. Worst case before the reply: about 30 s plus the
  overruns below.
- What bounds an attempt (`app.llm`): once the TCP connection is up, a watchdog ends the
  call at its deadline. httpx's own timeouts bound single socket operations, not the
  request, so the request runs in the calling worker thread while a `threading.Timer`
  waits; at `connect_tcp.complete` (httpx's documented `trace` request extension) the
  watchdog takes its **own `dup()`** of the TCP socket, and at the deadline calls
  `shutdown(SHUT_RDWR)` on that dup. `shutdown` acts on the connection, so the TLS
  handshake, the request upload or a response read fails at once in the calling thread.
  The dups are closed only after the timer is cancelled and joined, so the timer can only
  touch descriptors it owns, never a descriptor number reused by another connection.
  Keep-alive is off: each call has its own connection and nothing stays open or pooled
  afterwards. A body that completes exactly as the deadline fires is discarded.
- **Outside the watchdog** (remaining limitation): DNS resolution (blocking
  `getaddrinfo`, unbounded by this code) and the TCP connect itself, bounded only by the
  connect timeout `min(3 s, deadline)` *per resolved address*. When DNS plus those
  connect attempts take longer than the deadline, the call ends that much later: for
  example a 2 s deadline with two addresses can end after about 4 s plus DNS; an 8 s
  deadline overruns only with three or more slow addresses or a slow resolver.
- **Not bounded by the budget**: the database work. The history read, each usage
  insert and the reply-plan read / write (`cards.fixed_plan`) are limited only by the
  5 s `statement_timeout` per statement (which does not cover a stalled network to the
  database). The last usage insert and the plan write happen after the final budget
  check. A plan can therefore take longer than 15 s: roughly 15 s plus one usage insert
  plus the plan write, plus any connect overrun.
- Outside the budget: sending. The orchestrator logs outbound time separately
  (`Turn … : N message(s) through outbound in … ms`); each send is bounded by the
  WhatsApp client's own timeouts.
- The httpx client is built once per `GeminiClient`, outside any deadline.
- Tested against loopback servers: one that never answers (cut at 0.5 s; the server sees
  the close), one that trickles the body a byte every 0.2 s (cut at 1 s), and one that
  trickles the TLS handshake with socket timeouts set to 30 s (cut at 1 s by the watchdog
  alone; before the fix this ran until the socket timeout). Also tested: the watchdog's
  shutdown leaves another connection that took over the request's descriptor number
  untouched, and a cut call leaves no descriptor behind.

## 4. Failover, refusals and failures

- Failover-able errors: timeout, transport errors, HTTP 404 / 408 / 429 / 5xx. Not:
  400 / 401 / 403, invalid JSON (including absurd nesting, `RecursionError`), an
  undecodable body (`request:decodingerror`) or another httpx request error
  (`request:…`), oversized body (1 MB). Any of these after the watchdog fired is a
  `timeout`. Programming errors (`TypeError`, `httpx.StreamError`, `InvalidURL`) are not
  caught and fail the job.
- Failover happens only for the **first call of a turn**: the failover model gets the
  byte-identical request in the remaining budget. Once a model has answered, the turn
  stays on it (a turn that failed over continues on the failover model). A later
  failure ends the turn with the failure template: Gemini 3 response parts carry
  thought signatures and their acceptance by another model is not documented or
  verified, and executed tool actions are never replayed. No signatures are invented or
  stripped.
- Retries are bounded: at most one primary and one failover attempt per iteration,
  three iterations, the turn budget.
- Classification (`app.llm.parse_response`): `promptFeedback.blockReason` or finish
  reason SAFETY / PROHIBITED_CONTENT / BLOCKLIST / SPII / RECITATION / IMAGE_* ->
  **refused** (refusal template, no failover); MALFORMED_FUNCTION_CALL /
  UNEXPECTED_TOOL_CALL / TOO_MANY_TOOL_CALLS / MISSING_THOUGHT_SIGNATURE /
  MALFORMED_RESPONSE, more than 4 calls, a malformed `functionCall` -> **malformed**;
  MAX_TOKENS -> **truncated**; any other non-STOP reason -> **other**. Only STOP
  responses carry tool calls; the others run nothing and end with the failure template.
- No automatic retry job (the RFC's "retry job in 30 s" is plan Task 13.1). The failure
  template is truthful because processing has ended: "Maazrat, abhi aap ki request
  process nahi ho saki. Thori dair baad dobara bhejein."
- **Failed turns tell the customer** (RFC 13 poison jobs, follow-up 2026-10-12): every
  turn closed as `failed` gets the failure template once
  (`orchestrator.send_failure_notice`, key `turn:{id}:failed`), through the send-time gate
  (nothing after a takeover) and the 24 h window, and only if its status really is
  `failed` and no part of its reply plan went out. One notice per job, sent before the
  job answers newer messages. Paths: a job that will not retry (`fail_turn`); an open
  turn given up at the attempt cap (`assemble_turn`); and a process_message job that
  dies of a crash on its last attempt: `queue.reap_expired` then queues a
  `turn_failed_notice` job (same conversation key) whose handler
  (`orchestrator.notify_failed_job`) closes the turn as `failed` unless another job of
  the conversation will still resume it, and sends the notice. Send or database errors
  in the notice are logged, never raised.

## 5. Tools and roles

- Route from the allowlist only (Task 5.1): customer -> L1 tools
  (`update_requirements`, `search_properties`, `send_property_photos`,
  `request_handoff`); admin / agent -> staff L2 tools (`draft_listing`, `find_listings`,
  `propose_update`, `list_my_listings`); broker -> no tools and **no provider call** (a
  fixed help reply).
- `commit_change` is never declared and is refused if a model names it (button-triggered,
  Task 9.2).
- `app.ai_tools.execute` re-checks at execution time: unknown or non-string names ->
  `unknown_tool`; a tool outside the route -> `tool_not_allowed`; arguments failing the
  local schema (types, required, no extra keys, enums, lengths, item counts; booleans are
  not integers) -> `invalid_arguments`. The model receives the error as the
  `functionResponse`; nothing runs.
- Tool bodies are 6.1 stubs: they search, save, send and forward nothing, tell the model
  so, and set a notice that tells the user so. `update_requirements` also reports how many
  raw spans are not substrings of the current turn's text (R-22, whitespace- and
  case-insensitive); nothing is stored. Real bodies: 7.x (state / patches), 8.1 (search),
  8.2 / 9.2 (staff), 10.x / 11.x (customer wiring, handoff).
- The model's parts are sent back unchanged in the next iteration (function-call `id`s,
  thought signatures); each `functionResponse` repeats the call's `name` and `id`.

## 6. Replies and grounding (R-21)

- Customer-visible text is only approved templates (`turn_engine.TEMPLATES`, Roman
  Urdu) plus notices from trusted tool results. Model prose is never sent; the adapter
  does not even return it. With several tool notices, one message joins them in order.
- No text in the turn (a photo without caption, voice with STT_PROVIDER=off or on a staff
  turn): a template, no provider call (voice / staff photo / generic). Task 6.3: customer
  voice notes are transcribed before the engine runs and arrive as text (docs/SPEECH.md).
- `turn_engine.ground_reply(payloads, trusted_codes)` is the seam for the **Task 9.1**
  renderer + grounding validator (Execution Plan Day 10; the 5.3/5.4 handoff's "11.x" is
  wrong). In 6.1 it is defence in depth only: a listing-code shaped token not supplied by
  a trusted tool payload replaces the reply with the failure template. The guarantee in
  6.1 is that replies are templates.
- The template wording is a proposal for owner approval.

## 7. Context, replay and metering

- Context (`load_history`, `current_messages`): one `user` content with an optional
  history part and the current-turn part. History = up to 10 earlier messages of the same
  agency and conversation, created before the turn was assembled and not in it (later
  messages and this turn's own earlier replies are excluded), newest kept within 3,000
  characters, 500 per message. The current turn lists each of its messages once, in the
  turn's order (WhatsApp timestamp, receive time, WAMID), 1,000 characters each, 4,000 in
  all; media without text appear as placeholders. Both are labelled as data in the
  system prompt.
- Stable prefix: per route, `systemInstruction` + `tools` + `toolConfig` are module
  constants serialised in a fixed order ahead of `contents`; tested identical across
  turns. No padding and no explicit `cachedContents` (owner decision).
- Plan persistence: `respond` wraps the build in `app.cards.fixed_plan`. fixed_plan runs
  one short read transaction, then the build with **no transaction or lock held**
  (provider calls happen here), then one short `UPDATE … SET reply_plan =
  COALESCE(reply_plan, …) RETURNING`. Guarantees:
  - plan stored, then a crash or send retry: the stored plan is resent under the same
    `turn:{id}:{seq}` keys, **no provider call**;
  - crash before the plan is stored: the retry calls the provider again (new usage keys);
  - two builds of one turn (normally prevented by the conversation lock): the first
    stored plan wins and is what both return;
  - provider execution is at least once per turn attempt, **not exactly once**; a crash
    between a provider response and its usage insert leaves that call unmetered.
- Metering: one `usage_event` row per provider attempt (primary and failover), in its
  own short tenant transaction after the call; `provider` = `gemini`, `model` = the
  reported `modelVersion` or the configured ID, `operation` = `L1` / `L2`, `job_id` = the
  running job, `request_key` = `turn:{turn}:job:{job}:{job attempt}:{iteration}:{primary|failover}`.
  `job.attempts` is incremented and committed at every claim (`queue.claim_job`), so a
  retry or a process restart never reuses a key; a different job resuming the turn has a
  different job id; a job attempt cannot run twice. A collision would still be a plain
  `INSERT` error (no `ON CONFLICT`). `outcome` = the provider call
  (`succeeded` = a usable HTTP 200 body), not task success; the reason is
  `outcome_detail` (`stop`, `tool_calls`, `refused:safety`, `blocked:safety`,
  `malformed:…`, `truncated:max_tokens`, `timeout`, `http_429`, `transport:…`).
  `input_tokens` = `promptTokenCount` (includes cached tokens), `output_tokens` =
  `candidatesTokenCount`, `cached_input_tokens` = `cachedContentTokenCount`,
  `thinking_tokens` = `thoughtsTokenCount`; NULL when not reported (always on
  failures). A reported count that is not a non-negative integer within BIGINT range
  (booleans, floats, negatives, >= 2^63), or a cache count above the input count, is
  stored as NULL, never clamped, and the field name is logged (`unusable usage counts
  stored as NULL: …`). `estimated_cost` / `currency` stay NULL until a free-tier policy
  for this key is verified (DECISIONS: no invented zero costs).
- Metering failures: a usage `INSERT` error of SQLSTATE class 22 (data), 23 (integrity,
  including a repeated key) or 42 (schema / privilege) cannot be fixed by a retry, so it
  raises `PermanentJobError("usage_event_<sqlstate>")`: no failover call, no retry, the
  job is dead and the turn is closed as `failed`. Any other database error (connection,
  statement timeout, ...) uses the job's bounded retries (`max_attempts`, default 3); each
  retry repeats the provider call because no plan was stored. Metering is never skipped
  and the error is never swallowed.
- Failed turns (migration 014, `app.orchestrator`). `failed` is terminal (every status
  change is `WHERE status = 'open'`) and never a reply; its messages keep their `turn_id`,
  so they are never assembled again. A turn becomes `failed` in two ways:
  - **Exception path** (`fail_turn`): the turn's job will not retry (a `PermanentJobError`
    or the failure of its last attempt). The error still propagates, so the job records it.
  - **Crash path** (`assemble_turn`): a crash runs no cleanup, so every attempt is counted
    first: `conversation_turn.attempts` is 1 at assembly and incremented, in the committed
    assembly transaction, each time any job resumes the turn. An open turn found with
    `MAX_TURN_ATTEMPTS` (3) attempts is closed as `failed` instead of resumed, and the job
    goes on to the waiting messages. So an abandoned turn gets at most 3 attempts in all,
    whichever jobs make them, and a newer job spends at most 2 of its own attempts on it.
  - Either way the turn's trigger job, if it is still `pending` (waiting to retry), is
    dead-lettered with `turn_failed`: its message is in the failed turn, so a retry would
    only report success. Jobs are only changed under the conversation lock, as for the
    coalesced siblings.
  - When a resumed (older) turn fails on this job's last attempt, the job still assembles
    and answers its own messages, then raises the failure (job `dead`, error code kept),
    so a newer message is not stranded behind an older turn. If the failure was a usage
    row that cannot be written (`turn_engine.UsageWriteError`), the newer messages are
    recorded as a `failed` turn instead, with no provider call.
  - If closing the turn fails (the database still down), it stays open, and the attempt
    cap still bounds later resumes.
- Logs: turn / iteration / model / outcome / detail / latency / token counts and
  registry tool names only. Never prompts, message text, tool arguments, model output,
  phone numbers or the key (tested).

## 8. Acceptance

Offline (CI, fake transport, no key, no network): all pass locally, see the test files
`tests/test_llm_gemini.py`, `tests/test_ai_tools.py`, `tests/test_turn_engine.py`.

| Plan wording | Gemini translation | Offline | Live |
|---|---|---|---|
| Test turn calls a stub tool | A turn's first call returns a `functionCall`; the stub runs; the second call answers | pass | pending (L1) |
| Forced timeout triggers the failover model | Primary timeout -> configured failover model with the identical request | pass (fake transport + real-socket deadline) | pending (L3) |
| 2nd call shows cached input tokens > 0 | `cachedContentTokenCount` > 0 on a repeated prefix, stored in `cached_input_tokens` | parsing and storage pass | **pending; likely not met** (L2) |
| A usage row is written | One `usage_event` per attempt | pass | pending (L1) |

Live checks (owner-run, with the key, after an approved deploy; none done yet):

- L1: one real turn on the primary reaches a stub tool; `usage_event` rows appear
  (read-only query, no text).
- L2: two calls with the same route prefix; record the second call's
  `cachedContentTokenCount` as reported. Implicit caching needs at least 4,096 prompt
  tokens on Gemini 3.6 / 3.8 Flash (2,048 on 2.5 Flash), and the system prompt plus
  tools here are well below that, so a 0 / absent count is expected: record it as an
  open finding, never as a pass.
- L3: forced failover live (for example a nonexistent primary ID -> 404, or a short
  timeout in a check-only setting); two rows, the failover answers.
- L4: whether the account's models accept the request shape (`functionDeclarations`
  with uppercase types, `toolConfig` AUTO, `functionResponse` with `id`, role `user`).
- L5: phones: customer -> template reply via the engine; broker -> help reply; staff ->
  a stub tool notice; card taps and `#demo` unchanged.

Any operator script for these checks calls Gemini and writes usage rows: it is a live
smoke test, not a read-only tool.

## 9. Decisions (owner, 2026-10-12)

- httpx + stateless `generateContent`; `AI_PROVIDER=stub` default; explicit `gemini`
  with missing settings fails clearly; configurable models, pinned proposals unverified.
- Migration 013 for `cached_input_tokens` / `thinking_tokens` / `outcome_detail`; no
  prompt padding, no explicit caching; missing cache evidence stays an open finding.
- `estimated_cost` NULL until billing is verified; `outcome` means provider-call success.
- No 30 s retry job in 6.1; truthful Roman Urdu failure wording; brokers make no provider
  call; non-text turns get templates.
- Real cancellable deadline (after the TCP connect; section 3); template-only replies; no
  mid-turn cross-model replay.

## 10. Open items

- Live checks L1-L5; model availability and free-tier limits for the chosen IDs.
- Implicit cache minimum vs prompt size (likely no cache hits); revisit with the real
  7.x / 8.x prompts and tool payloads.
- Template wording approval (Roman Urdu; English variants not written).
- Thinking level: `low` is sent as `generationConfig.thinkingConfig.thinkingLevel`. The
  guide read on 2026-10-12 shows the values and model support but its REST example is
  the Interactions API (snake_case); acceptance on `generateContent` is part of L4
  (`LLM_THINKING_LEVEL=off` if rejected). Latency at `low` is unmeasured.
- Interim / failure-notice wording awaits owner approval with the other templates.
- DNS resolution and the TCP connect are outside the watchdog (section 3); database
  statements are bounded only by `statement_timeout`.
- Failed-turn limits: a crash between closing a turn as `failed` and sending its notice
  loses that notice (the close is committed first). A process_message job that dies
  **before** its message is in any turn (for example
  three attempts that fail before turn assembly, such as a database outage) still leaves
  that message unanswered: assembly only takes messages of the current or `pending`
  jobs. This is the queue's existing dead-job behaviour, not specific to failed turns;
  coalescing dead jobs' messages later would need an age / 24 h bound and is not done
  here. A runtime schema regression after startup costs one provider call per new turn
  before the permanent error stops it.
- The worker's exit / restart behaviour is checked locally only (Docker 29.4.1), not on
  EC2.
- `docs/PROJECT_STATE.md` / `docs/TASK_LOG.md` on main still start with a pasted chat
  transcript (handoff section 8): a separate docs-repair task, not part of this branch.

## 11. Review fixes (2026-10-12)

Resolved (offline tests in `tests/test_llm_gemini.py` and `tests/test_turn_engine.py`):

| Finding | Resolution |
|---|---|
| The watchdog could not cut the TLS handshake (it held the raw socket, which TLS detaches; reproduced: a 2 s deadline ran to a 10 s socket timeout) | Owned `dup()` of the TCP socket from `connect_tcp.complete`, closed after the timer is joined; handshake-stall test |
| The timer's shutdown could race a socket close and hit a reused descriptor | Same fix: the timer only touches its own open dups; isolation and no-leak tests |
| A repeated metering / schema error after a provider call left the turn open for ever; every later message resumed it, called the provider again and failed | Persistent SQLSTATE classes raise `PermanentJobError` (no retry, no further call); a turn whose job will not retry is closed as `failed` (migration 014); startup schema check; tests: transient errors exhaust 3 attempts (3 provider calls) then a later message is answered once, persistent error stops after 1 call |
| Token counts beyond BIGINT failed the insert | Counts outside 0..2^63-1 (and non-integers) stored as NULL and logged by field name |
| Unusable AI settings left the worker idle but `Up`, with every job type stopped | Exit code 78 with a sanitised log; Compose `restart: unless-stopped` restarts it visibly (section 2) |
| `httpx.DecodingError` / other request errors and JSON `RecursionError` escaped the adapter unmetered | Mapped to `request:…` / `invalid_json` provider errors (metered, failure template); `timeout` if the watchdog fired |
| The docs claimed a wall-clock request / turn guarantee | Section 3 states what is and is not bounded |

Remaining limitations: section 3 (DNS and TCP connect, database statements) and
section 10. Pending live acceptance: section 8 (L1-L5), unchanged.

Follow-up (failed-turn lifecycle, 2026-10-12):

| Finding | Resolution |
|---|---|
| A turn whose attempts all crashed (the reaper dead-letters the job; no exception handler runs) stayed open, and every later message resumed it; with a deterministic crash each later message's job died too and its message was stranded | `conversation_turn.attempts` (migration 014), counted before work at each resume; an open turn with 3 attempts is closed as `failed` at assembly and the waiting messages get their own turn. Regression tests through crash, `reap_expired` and resume; with the cap disabled the same tests fail (the later message resumes the abandoned turn) |
| A newer message's job that failed its last attempt while resuming an older turn died with its own message unassembled (stranded) | The job answers its own messages after the older turn is failed, then raises the failure; a `UsageWriteError` records them as a failed turn without a provider call. Regression tests for both |
| A pending trigger job of a failed turn would later report success | Dead-lettered with `turn_failed` when its turn is failed |

## 12. Customer test fixes (2026-10-18)

- A customer turn whose every tool call was `update_requirements` and whose notices are only
  acknowledgements, with no open question, gets the normal search in code at a plain STOP
  (`detail` `stop_search`), as it already did at the iteration limit (`iteration_limit_search`).
  See docs/REQUIREMENTS.md "Customer test fixes".
- `render_customer` sends an identical customer-visible payload once, and counts photos as a
  delivered reply (`customer_reply.delivered`), so "Shukriya! ..." no longer precedes photos.
- A message that is only a listing code is answered before the model (`_customer_shortcut`).
