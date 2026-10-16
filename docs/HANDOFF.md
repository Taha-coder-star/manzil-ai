# Human handoff (Tasks 11.1 + 11.2)

Customer -> human broker handoff, the broker's lead card, and the control states that keep the AI
silent while a human owns the conversation. Sources: R6 (D3 / D4), RFC 7 (C13 / C14) and 13,
Red-Team R-15, R-28 - R-31, Demo Scope scenario 5 and C14, Execution Plan 11.1 / 11.2 and gate M5.
No migration: migration 006 has `lead`, `lead_property`, `handoff`, `conversation.control_state`
and `staff.areas_covered`.

Code: `app/handoff.py` (request, routing, forwarding, holding reply), `app/handoff_alert.py`
(lead card, L3 summary, template, accept timer), `app/handoff_actions.py` (buttons, commands, help,
release), `app/outbound.py` (send-time gate), `app/orchestrator.py` (where each runs).

## Owner decisions (2026-10-15 / 16)

| Topic | Decision |
|---|---|
| Full details | Sent automatically to whoever taps Accept (the card keeps 3 buttons: [Accept] [Pass] [Called]) |
| Accept timeout | 15 min, `HANDOFF_ACCEPT_MINUTES` (1-1440) |
| Several shown listings | "Kaunsi property ke liye visit?" lists every shown listing plus "Sab"; the lead links each chosen one |
| Escalation | The timeout **adds** the admins and keeps the broker: while pending, the assigned broker or any active admin may Accept, the first tap wins, later taps are told who has it. Only Pass reassigns to ADMIN |
| Template | The submitted `new_lead_alert` (Active, English `en`): "A customer has requested agent assistance for property {{1}}. Inquiry reference: {{2}}. Please review this customer inquiry." -> `property,lead` |

## States

`conversation.control_state` and the open `handoff.status` change together, in one transaction
(STATE_SCHEMA: application contract). `mode` follows (`conversation_control_state_gate_check`).

| Event | handoff | lead | control_state / mode | Who |
|---|---|---|---|---|
| Handoff request (tool, Visit button, [Agent se baat], list pick) | `pending` (accept due +15 min) | `open` (upsert) | `handoff_pending` / `human` | customer turn |
| Accept (first tap wins) | `accepted`, release due +48 h | | `human_owned` / `human` | assigned broker or any admin |
| Pass | `pending`, assigned -> admin, `escalated_at` | assigned -> admin | unchanged | assigned broker |
| Accept timeout (round 1) | `escalated_at`; admins get the card | | unchanged | job |
| Called | `called_at` (first) | | unchanged | assigned / accepter / admin |
| Customer message while pending / accepted | release due +48 h (accepted) | | unchanged | forwarded |
| `#done L-xx` | `completed` | `closed` | `ai_active` / `ai` | accepter or admin |
| 48 h without a customer message | `released` | `closed` | `ai_active` / `ai` | job |

HANDOFF_PENDING behaves as HUMAN_OWNED (R-28 b): no AI reply, search, pricing or booking; messages
are forwarded. `closed` (R17) is not used.

## Customer side (11.1)

- **Entry**: `request_handoff(reason, description?)` (reason: visit, negotiation, agent, legal,
  frustration); a card's Visit button (`visit:CODE`, the listing is named); [Agent se baat]
  (`handoff:request`); a row of the which-property list (`handoff:{reason}:{CODE|all}`).
- **Property (R-31)**: only the customer's own reference names a listing: a quoted card, a code,
  a position or a description in their words, or a tapped button. Focus does not (the stale-focus
  case of scenario 5 after 3a). Several shown and nothing named -> the list question. One shown ->
  echoed `#n · CODE`. None shown -> a lead with requirements only.
- **Routing**: the listing's `assigned_broker_id`, then a broker whose `areas_covered` contains the
  listing's place (or its area / city), then the active broker with the fewest open handoffs, then
  an active admin. Nobody -> `handoff_no_agent`, nothing written.
- **Reply** (a SYSTEM plan entry, sent with `ai_generated=False` because the conversation is now
  pending): "Main Manzil ka AI assistant hoon -- aapko hamare consultant Ahmed se connect kar raha
  hoon. Woh jald rabta karenge." + `Property: #2 · DH6-102` + `Aapka reference: L-01`, and a CTA
  "Chat with Ahmed" -> `https://wa.me/<broker>?text=Ref%20L-01`. English when the customer writes
  English. Nothing else of that turn is sent.

## Broker side (11.1)

- **Lead card** (`handoff_alert` job, queued with the request): to the agent's **own**
  conversation (their 24 h window and gate apply). R-30 order, English (D15): `New lead L-01
  (visit)`, customer name / number, `Wants:` (the requirement snapshot), `Property:` (re-fetched
  public rows), `Last message:` (the customer's own latest words, not a button title), `Summary:`,
  `Tap Accept within 15 min.`; cover image header when there is one.
- **Completeness**: customer, requirements (or a listing), last message and reason; a missing one
  is logged and shown as "not stated".
- **L3 summary**: one structured call (primary, then failover), `usage_event` operation `L3`, no
  phone number in the prompt, checked with `grounding.check_payloads` against the lead's facts;
  unsupported -> left out; any failure -> the card goes without it (never blocks).
- **Closed window**: the `new_lead_alert` template instead (`LEAD_ALERT_*`). It has no buttons:
  when the broker replies, their window opens and the card follows once (`resend_cards`).
- **Nothing delivered** (card and template refused): the admins get it at once.
- **Buttons** `ho:accept|pass|called:{handoff_id}`: handled in code before the staff buffer and the
  engine. The id is looked up under the sender's tenant (RLS) and the sender's role / assignment
  are re-read under the handoff row lock: forged, stale or other-tenant ids get "That lead was not
  found."; a customer's tap is only forwarded.
- **Accept** replies "You accepted L-01. Chat with the customer: https://wa.me/<customer>" and the
  Full details text (customer, wants, properties, the last 10 customer messages with Karachi
  times, voice as transcript).

## Control and forwarding (11.2)

- **Send-time gate** (`outbound._prepare`, in the send transaction): an AI message goes out only
  while `mode = 'ai'`, `control_state = 'ai_active'` and `ai_muted_until` is not in the future.
  A reply drafted before a handoff or an Accept landed is cancelled (`control_gate`), including
  cards, carousel fallbacks, photos and retries. Exempt (`ai_generated=False`): the handoff
  message, the holding reply, forwards, lead cards, staff replies, templates; app code chooses,
  never model output. A cancelled payload stops the plan and nothing counts as shown. Residual
  window: one HTTP call (a change committed after the check).
- **Forwarding** (gated customer turn with an open handoff): each message one way to the agent
  holding it, `L-01 · +92300…: <text>` (voice as its transcript, `(voice)`; transcribed only when
  there is a handoff), in the agent's conversation (their window applies: a closed window records
  `outside_window` and the holding reply still goes). No model call, no search ("aur dikhao"
  included, R-28 c).
- **Holding reply**: once per handoff ("Ahmed ko aapka message bhej diya hai; woh jald jawab
  denge."), dedupe key `handoff:{id}:holding`, then `holding_reply_sent_at`.
- **Commands** (R-15): `#done L-07` (also `#done L-7`, or bare `#done` with one accepted lead),
  `#pass L-07`. Any other broker message -> the help reply with their open leads; never listing
  extraction, never a model call. Admin texts that are not commands keep the staff flow.

## Timers (C14)

| Job | Dedupe key | When | Does |
|---|---|---|---|
| `handoff_alert` | `handoff-alert:{id}` | at once | summary + card / template |
| `handoff_accept_timeout` | `handoff-timeout:{id}:1`, `:2` | +15 min each | round 1: admins get the card; round 2: log only. No-op unless pending |
| `handoff_release` | `handoff-release:{id}:{due epoch}` | +48 h after Accept / each customer message | released if accepted and `release_due_at <= now()`; else no-op (a newer job exists) |

No callback time is promised to the customer on escalation (R-29's "aaj 6 baje tak" is not
fabricated).

## Settings

`HANDOFF_ACCEPT_MINUTES` (default 15), `LEAD_ALERT_TEMPLATE` (default `new_lead_alert`),
`LEAD_ALERT_LANGUAGE` (default `en`), `LEAD_ALERT_PARAMS` (default `property,lead`; fields: lead,
reason, customer, wants, property, in the template's `{{n}}` order). Nothing else changes.

## Wording awaiting owner approval

Customer: the handoff message and CTA, "Kaunsi property ke liye visit?" / "Sab", `handoff_no_agent`,
the holding reply (Roman Urdu + English). Staff (English): the card, Full details, the button
replies, the commands' replies, the help, the release notice.

## Limits

- A forward to an agent whose window is closed is recorded, not delivered (no template for
  forwards); the demo-day rule stands: the broker writes to the bot first (R-29).
- Low-confidence voice intent confirmation ([Haan] [Nahi] before a voice handoff) is not built;
  a voice handoff still confirms the property.
- Outcome buttons (visit booked / not interested / deal) and reassignment history (R-32) are not
  built (not in 11.1 / 11.2).
- Template variables are assumed numbered (`{{1}}`); named ones need a small change.

## Live acceptance (gate M5; team phones only)

Before: tell Taha (10.3 runs); confirm the broker phone in `staff` (active, role broker); the
broker phone is on the test number's recipient list.

1. Customer B: scenario 2 search (3 cards), then "Mujhe visit karni hai, kal shaam" -> the list
   with every shown listing and "Sab" -> tap one -> the CTA message with `#n · CODE` and `L-xx`.
2. Broker (wrote to the bot first): the card with every field and [Accept] [Pass] [Called].
3. Accept -> "You accepted …" + Full details. Customer: "price kam?" -> forwarded to the broker,
   the customer gets the one holding reply, no AI message.
4. Broker `#done L-xx` -> the customer's next message gets an AI answer.
5. Closed window: a broker whose last message is over 24 h old (or a second broker phone) gets
   `new_lead_alert`; replying brings the card.
6. Optional: `HANDOFF_ACCEPT_MINUTES=2` to show the admin escalation.

Read-only checks: `handoff` / `lead` rows, `outbound_message` statuses (`control_gate` for
anything the AI drafted after Accept), `usage_event` L3 rows.

## Rollout

Code only: the changed `app/` files layered on the live 10.2 image (tag from Taha); no migration;
optional settings above. Rollback = retag the previous image; open handoffs stay in the database
(a rolled-back image gates those conversations silently: close them with `#done` before rolling
back, or reset their `control_state` / `mode`).
