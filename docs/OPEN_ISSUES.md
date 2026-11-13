2026-11-12 AUDIT WP1 ROLLOUT NOTE: HANDOFFS ALREADY PENDING ARE NOT HEALED.
WP1 (branch fix/wp1-customer-reliability, uncommitted) makes round 2 of the accept
timer release an unaccepted lead (MA-001). Handoffs pending in production whose
round-2 job already ran under the old code stay pending: nothing re-schedules them.
After a release, read-only, in each agency's tenant context:
  SELECT l.code, h.requested_at FROM handoff h JOIN lead l ON l.id = h.lead_id
  WHERE h.status = 'pending' AND h.accept_due_at < now() - interval '1 hour';
Remedy with existing features: an admin taps Accept on the card, then #done L-nn.
Timers fire only while the worker runs. Not deployed; no production access made.

2026-11-12 GITHUB PUBLICATION PREPARED: CLAUDE AUDIT + PENDING DAY13 NOTES.
Owner requested pushing Claude prompts and remaining work. GitHub network access
is blocked in this agent session; remote publication is not yet claimed.
Portable tracked pack: docs/audits/2026-11-12/README.md (three prompts, project
brief, coverage/template, selected sanitized Day13 evidence and archived operators).
Pending PROJECT_STATE/TASK_LOG/OPEN_ISSUES notes included. No app/migration/test/
runtime changes, private recordings, raw historical exports, .env, source/release
ZIPs or unrelated untracked local test directories selected.
Day13 remains open by owner direction; runtime known low/image976dff195d20/schema020.
Next publish isolated docs branch through normal authenticated PowerShell; exact
head/remote verification required. No force push, merge, deployment or AWS action.

2026-11-12 OWNER KEEPS DAY13 OPEN; INDEPENDENT SYSTEM AUDIT PREPARATION.
Owner will address Day13 gaps later and now prioritizes a senior engineering audit
of built behavior, bugs, architecture, and single/multiple-agency client readiness.
Prepared ../output/claude-system-audit context brief and three staged prompts:
context -> comprehensive audit -> challenge/readiness/remediation handoff.
This is an audit setup, not a completed audit or a finding of production readiness.
Source snapshot is current tracked working-tree bytes on local HEAD7eefb9c; current
operational doc edits included. Latest known owner runtime remains low on976dff195d20,
schema020; no new runtime access, provider calls, tests, app edits, push/merge or AWS
actions. Day13 gates remain unwaived; no conditional closeout accepted.
Local archive ../output/manzil-ai-audit-input-2026-11-12.zip includes tracked source
(except .env.example), context/evidence summaries and per-file hashes; excludes .git,
real env/credentials, raw audio/screenshots, untracked output kits/caches/worktrees.
Claude Code can use actual workspace and Git history; web-chat ZIP has no full history.
Newest brief overrides historical deployment notes; exact source and evidence must
still be checked by the auditor. External RFC PDFs and deployed settings not reverified.
Next owner gives Claude 00_START_HERE and prompts02/03/04 in order. Findings must be
validated and separated from accepted demo policy, missing features and evidence gaps.

2026-11-12 LOW RESTORED; MINIMAL EXPERIMENT ENDED, ACCEPTANCE DECISION PENDING.
Owner returned DAY13-LOW-EFFORT-RESTORED, receipt recorded at
2026-11-11T19:53:16.277923+00:00 (00:53 PKT Nov12). Same image976dff195d20,
baseline config34d4b870796b7684567fb78682ca49930e24971dbad573359ea1bd4a08381d60.
Operator provider calls/database writes zero; .env not edited; trial evidence retained.
Receipt ../output/day13-closeout/minimal-trial-restored-low-owner.json.
Low ACTIVE; minimal not adopted. Prior minimal/current low samples remain separate;
do not resume original closed measurement windows or repeat the same cohort.
Comparison complete, targeted latency unmet; eight text/six voice unrun preserved.
Day13 remains open: explicit owner decision needed on concrete remaining gap draft
../output/day13-closeout/remaining-decisions.md (latency, voice, cache, cost, fullRun2).
Can fix gaps or accept conditional closeout with each deferral recorded; no critical
threshold pass, paid fallback, or waiver is inferred from the restoration receipt.
No further same-cohort timing tests or AWS changes are pending from this experiment.

2026-11-12 MINIMAL COMPARISON COMPLETE; TARGET UNMET, RESTORE LOW RECOMMENDED.
Owner-returned send review imported with pinned b8ba0146 source/runtime/anchor.
T01 first substantive Meta acceptance 11533.570ms (low11929.345); T02 7161.561ms
(low original failed503). Both exceed7s, six minimal primary calls succeeded;
two turns cannot establish general causality or a completed percentile.
T02 6685.912ms is purpose_inferred notice, excluded. Carousel summary accepted
7161.561/delivered11059.594ms; #3 accepted7734.299/delivered10059.594ms.
No later fallback rows: application accepted in order; delivery receipts reversed
carousel/#3 by one second. Screenshot order explained, not confirmed app bug.
With two values>7s, fixed10 nearest-rank p90 lower bound7161.561ms even if eight
remaining instant. Not a completed percentile. Stop additional cohort samples;
8text/6voice retained unrun. Earlier accidental/coalesced input excluded by explicit
owner direction and retained; no clear-all acknowledgement inferred.
Restore same-image low recommended via ../output/day13-minimal-restore-low.sh;
runtime STILL minimal until owner restoration receipt. Permanent adoption false.
Comparison result ../output/day13-closeout/minimal-comparison-result.md.
Concrete remaining decision draft ../output/day13-closeout/remaining-decisions.md:
text latency, current voice, cache, cost, full Run2 evidence gaps need fix or explicit
owner deferral/conditional acceptance. Not waived or Day13 closed by this review.
No further diagnostic/test campaign needed to conclude this effort experiment.

2026-11-12 MINIMAL DIAGNOSTIC IMPORTED; OFFICE STILL 11.53 SECONDS.
Pinned source/image/config/plan/anchor verified in owner-returned sanitized JSON.
Two office textmatches: owner explicitly identifies earlier 00:38:54 input as a
mistake to ignore. Excluded from planned comparison, retained as protocol deviation;
its 2.004s single reply/no-usage is NOT claimed as fast search or clear-all ack.
Planned office T01 at 00:39:41: first substantive Meta acceptance 11533.570ms
(low11929.345ms), last13081.894ms; three calls/engine8645ms. Single comparison
does not establish effort benefit; this turn remains above7s. T02 three primary
successes, no errors; first text6685.912ms, interactive7161.561/7734.299ms.
Screenshot first text is assumption notice; do not call6.69s substantive latency.
Existing diagnostic omits later carousel-fallback keys, so card ordering and actual
substantive acceptance need focused existing-record review, no new input/calls.
Prepared ../output/day13-minimal-send-review.sh: restricted read-only, exact actor
and saved anchor; classify notices/known fixture codes, original and linked later
fallback receipts; no raw payloads/phone/UUID. Compile/classification/redaction/LF/
bash syntax checks passed. Actual owner execution pending. All cached/thinking
counts null, not zero; no dollar cost or percentile pass. Minimal remains temporary.
Next owner copy/paste send-review wrapper; hold further samples.
Evidence ../output/day13-closeout/minimal-comparison-analysis.json.

2026-11-12 MINIMAL T02 RESULTS VISIBLE; EXACT COMPARISON PENDING.
Owner screenshot shows original T02 and expected DH6-102 8 crore, DH6-101 7.8
crore, DH7-101 7.5 crore; 500 sq yd and nearby Phase 7 labelled. No failure
visible. Purchase-assumption notice followed by #3 coverless card ABOVE summary,
then #1/#2 photo cards. Preserve ordering observation; send/delivery order pending.
Displayed 00:41 PKT is minute precision, not a measured latency. Two minimal
text observations, zero measured timings; no percentile or improvement claim.
Next: copy ../output/day13-minimal-diagnostic.sh in normal PowerShell, paste into
EC2 and return JSON. Read-only, no provider calls or database writes. Hold further
samples until exact comparison review. Low cohort and original failure unchanged.
Evidence ../output/day13-closeout/minimal-t02-owner-observation.json.

2026-11-12 MINIMAL-EFFORT TRIAL ACTIVE; FIRST SAMPLE PENDING.
Owner returned DAY13-MINIMAL-TRIAL-READY. Same image 976dff195d20; effort minimal;
config c014e9e0af227d55fab199ff1cf2f45b6459c93f8da857972a55f0f43158be7a.
New window 2026-11-11T19:34:20.530416+00:00 (00:34:20 PKT, November 12).
HTTPS healthy; customer ...2614 AI active, no blockers, global jobs/turns zero;
public29 and hidden fixtures absent. Operator provider calls/database writes zero;
.env unchanged. Owner evidence: ../output/day13-closeout/minimal-trial-ready-owner.json.
Low baseline/results and original anchor retained separately. Minimal is a trial,
not permanent adoption or a latency/cost/cache/Run2 pass. No new samples yet.
Next: customer ...2614 sends clear all, waits for acknowledgement, then T01 once:
office on rent in Karachi, around 5 lakh per month
Return bot response; do not rerun switch or replace an unsuccessful first attempt.
Scoped minimal diagnostic and same-image low restore remain prepared.

2026-11-12 OWNER AUTHORIZES MINIMAL-EFFORT COMPARISON; OPERATOR READY, NOT RUN.
Explicit Try the minimal-effort comparison. Prepared17focusedoffline checksPASS,
including fullenvironment preservation, no.env edit/oldanchorchange, idempotent
candidateanchor, explicitlowrestore, partialswitch/posthealth auto-restoration,
drift/busy/tag/budget refusals and rollbackattention. ThreeLF wrappersbash-nPASS.
No actualDocker/Compose/AWS/provider execution here; these are mocked/operator
checks, not another product/fullpytest campaign. Application bytes unchanged.
Next ../output/day13-minimal-trial.sh copiednormalPowerShell/rawclipboard then
pastewholeblockUbuntuEC2, keepchatquiet duringrecreation. Pinned976dff tag andall
effectiveenv comparedprivately; onlyLLM_THINKING_LEVELlow->minimal allowed in
temporaryComposeoverride bothservices; .env stayslow. BudgetoverrideOFF kept.
Globaldemo processingjobs/openturns+actor/fixtures/HTTPS/worker checks before/after.
FailurepostmutationrestoresLOW sameimage; NEVER usesoldimage rollback. Explicit
restore wrapper day13-minimal-restore-low.sh; scopedread-only comparison wrapper
day13-minimal-diagnostic.sh prepared from unchanged provenchildsource.
CandidateconfigSHA c014e9e0af227d55fab199ff1cf2f45b6459c93f8da857972a55f0f43158be7a.
FrozenminimalplanSHA4bf375ec9e4c47b7b4e918606d04c3fbb3377fff15a94aba8b2826b90bbdf748.
OperatorSHA4daad3c7d81f072bb6d22e4ddd7d39c51786536bb94bbf3af623520cfac7aac7.
HosttrialDIR /home/ubuntu/day13-closeout/effort-trial-7eefb9c. Newminimal-session
anchor savedonce; originalLOWtiming-active.json/failure/resend preserved. Normal
baseCompose withouttrialoverride returnsLOW; do not mixrestarts/configcohorts.
WhenDAY13-MINIMAL-TRIAL-READY owner...2614clear all/waitack then T01exactoffice
on rent in Karachi, around 5 lakh per month, ONCE. Returnoperatoroutput+botreply.
FirstcomparisonT01/T02; onlythen reviewcontinuingcandidate. No lowvoice baseline
exists; no pairedvoice improvement claim. No35notecollection, provider/model/
billing/schema/roles/inventory change or fullRun2/cost/cache waiver. Permanent
adoption remainsfalse, actualcurrentruntime stillLOW until ownerREADY evidence.
Runbook ../output/day13-closeout/minimal-trial-runbook.md; approvalreceiptJSON.

2026-11-12 T02 DIAGNOSTIC VERIFIED: CURRENT LOW BASELINE CANNOT MEET TEXT P90.
Owner-returned JSON imported/sourcechecksum+image/config/anchor verified; private
clipboard content not emitted. ../output/day13-closeout/t02-diagnostic-owner.json.
T01firstsubstantive11929.345ms;3calls/engine9217ms/provider8984ms, laststop_empty
4864ms. T02firstonlyfailure apology15021.701ms; toolsupdate+search succeeded,
thirdcallHTTP5035513ms. No429observed. Noai_recovery row: failure after active
tool/model, not initial-both-provider failure. Resend is NEWturn, firstsubstantive
19953.195ms; primarytimeout8000 thenfailover;5calls, engine16492ms; interim18429.929
isNOTSLAboundary. OriginalT02infinity preserved, resendextra(notreplacement).
For fixedn10nearest-rankp90, T01>7s +T02failed make targetunattainable evenif8
remaininginstantaneous: bestpossiblep90>=11929.345ms. NOT a fullymeasuredp90;
baseline incomplete,8text/6voice unrun. Pausequota spending pending tuning choice.
Sourceinspection: finalmodelround mayrequestfurther tools/compoundactions; no
blind early-exit patch. T01minus4.864s onlycounterfactual7.065345s, notpassingproof.
Prepared one-setting minimal-effort candidate +ComposeJSON/lowrestore overlays;
no app/runtime/env changes. Same976dff/providers/schema; officialGenerateContent
thinkingdoc verifies both configuredmodels supportminimal. Local bodycheck only
thinking config changes. Candidatehash inminimal-trial.proposed.json, NOTadopted.
Report low-baseline-findings.md; ownerchoice required because approvedplanpinslow:
separate minimaltrial onT01/T02 thencontinuecandidate ifvalid, orfinishoriginal
unchanged batch. No guarantee minimal fixes503s. No fullRun2/cost/cache waiver.
Runtime remainsGemini-low/config34d4/976dff/schema020; no deploy/AWS mutation.

2026-11-12 T02 FIRST ATTEMPT FAILED; USER RESEND VISIBLE; PAUSE BEFORE T03.
Owner screenshots:11Nov23:57PKT exactT02 -> processingfailure apology; at23:58
samequeryresent, interimthen correct10280m/10178m/710175mnearby summary/cards.
Originalfailure remains cohort observation; resend is extra, NOT a replacement.
Could be newturn or earlier automaticrecovery; correlation/elapsed/rootcause NOT
established by screenshots. Do not assert quota exhaustion or code bug yet.
Progress2text/0voice +1extraresend; nextT03PAUSED, not a new fulltest campaign.
Prepared ../output/day13-t02-diagnostic.sh: pinned currentimage/config +existing
sessionplan/anchor; restricted READONLY exactactor T01/T02 textmatches only,
turn/outbound/job/usage/recovery state, structured code-only worker-log summaries
and provider timings. PrivateUUIDs used onlyinternally then stripped, no phones/
messagebodies/transcripts/toolargs/rawlogs exported. No providercalls/DBwrites,
roles/service/settings changes or simulated failure. Saves root0600 hostJSON.
Offline read-only/refusal-shape/redaction/failure-template/unknown-token/logregex/
compile/LF checksPASS. Actual diagnostic execution PENDING owner EC2. Nextcopy
output/day13-t02-diagnostic.sh via normalPowerShell Get-Content -Raw|Set-Clipboard,
pasteentireblockEC2 andreturn output; no further messages/reset/anchor restart.
No additional samples: onlyT01,T02first plusT02resend observed. Formal
p50/p90/cost/cache notpassed; no new provider/model/effort/fallback adoption.

2026-11-11 TIMING SAMPLE T01 OWNER REPLY OBSERVED; PRECISE LATENCY PENDING.
T01 owner23:55PKT botreply: office/rent/Karachi/around5lakh summary, exactDH6-108
500000/mo,PEC-101550000/mo,SHF-101450000/mo; three correct coverless cards.
Functional result matches; four visible substantive parts. Minute-level reply
times do NOT establish latency. Original query/clear acknowledgement not pasted;
do not infer their exact timestamps or require a repeat merely for that omission.
Progress ../output/day13-closeout/timing-progress.json;1text/0voice observed,
0exact primary latency values yet. No phone number/message UUID persisted.
Next after60s from final reply, same...2614chat WITHOUT clear/reset: T02 exactly
DHA Phase 6 mein 500 gaz ka ghar, budget aath crore tak. Preserve first attempt,
anchor2026-11-11T18:51:55.834069UTC, frozenplan/config/runtime. No repeatedT01.
Read precise timing from scoped server records later; p50/p90/cost/cache still
unverified. No source/deploy/roles/inventory/guard/billing change by agent.

2026-11-11 TIMING PREFLIGHT READY VERIFIED FROM OWNER EC2 OUTPUT.
Readytrue/blockersnone; ...2614customer/AI-active/unmuted; actorhandoffs/open turns/
processingjobs all0; public29/hidden0; both976dff/config34d4 matches/HTTPShealthOK.
Pinnedwindow2026-11-11T18:51:55.834069+00:00 (23:51:55PKT); originalanchor saved
at /home/ubuntu/day13-closeout/timing-active.json. Preflight made0providercalls/
0DBwrites. Receipt ../output/day13-closeout/timing-preflight-owner.json; exact
plan/preflight SHA match verified locally. No live sample/latency result yet.
TrailingEC2 > prompts after completion look like unfinished pasted shell input;
Ctrl+C once returns normalprompt, without rerunning or replacing savedsession.
Next approved action WhatsApp...2614: clear all as standalone setup; waitforack;
T01 exactly office on rent in Karachi, around 5 lakh per month. Return botreply.
Do NOT send T01 inEC2, resetdata/roles/redeploy, restartpreflight toresetanchor,
or send repeated samples. Saved24hcustomerwindowfalse ispre-inbound observation;
this isowner-initiated WhatsApp testing, not outbound template setup. Keepallfirst
attempts/failures. Cases10text/6voice and currentloweffort/config unchanged.
No new billing/guard activation or AWS mutation. Day13acceptance stillpending.

2026-11-11 OWNER APPROVED TARGETED CURRENT-BUILD TIMING CHECK.
Explicit reply: Proceed with that timing check. Scope10text+6shortvoice only;
prior functional/broker/silence/81s evidence retained, no35note recollection.
Frozen plan ../output/day13-closeout/timing-plan.approved.json; checksum in
timing-session-preparation.json. Human script timing-check.approved.md. This
doesnot waive cost/cache/fullRun2 numeric coverage or approve a new model/effort.
Prepared ../output/day13-timing-start.sh, LF-only: exact976dff/image34d4config on
both services, HTTPS health, restricted READONLY REPEATABLE READ tenant query;
checks ...2614 remains customer/AI-active/unmuted, no actor openhandoff/open turn/
processing jobs, public29/hidden exclusion. No role/control/data repair attempted.
On READY saves0600 /home/ubuntu/day13-closeout/timing-active.json with UTCanchor/
planhash/image/config/maskedactor/runtime-starts; repeat preserves originalanchor.
No DB writes/providercalls/restarts. Offline ready/private-ID-redaction/humanmode/
handoff/job/hiddenfixture/staffroute refusal and compile/LF checksPASS. Actual
EC2 readiness and all16samples remain PENDING; no real DB/provider tests here.
If READY owner may proceed directly: send clear all from ...2614, wait forclear
acknowledgement; then T01 office on rent in Karachi, around5lakh per month (exact
spaced script in approved guide). Return readiness output and bot response. No
extra permission required for this approved sample. Space measured trials60s
after final replies, keep firstattempt/failures; don't resetanchor or repeatfast.
Existing free accounts retained, no billing upgrade/paidguard activation; incomplete
policy not activated. Observer export is scoped to later selected samples, not
another historical-audit replay. Cached unknown/dollar gaps still recorded.
Current7eefb9c/Gemini-low/schema020 preserved; no deploy/migration/AWS mutation.

2026-11-11 DAY13 AUDIT DATA RECEIVED/DECODED/INTEGRITY VERIFIED.
Imported complete owner-returned line via local clipboard (no unrelated clipboard
content emitted); zlibEOF/CRC+JSON shape/sourcechecksum/runtime image+config checks
PASS. Saved ../output/day13-closeout/owner-audit-0e188e39a202.json, SHA256
0e188e39a2026acd7ddef6713a46ca1f5cafec288b8853931c53283b3f51d7c5.
Latest500of505 turns sinceNov3:264staff/44broker/192customer. Omitted5areoldest,
not current timing. LastturnNov09T10:17:41UTC; ZERO turns in pinned currentruntime
window sinceNov10T17:35:06UTC. Thus no current-build p50/p90 is established.
46customer voice notes ALL stored/audiohash/durationpresent;45transcribed/1no_speech,
total313.44s/max80.86s. Reuse eligible real audio; no blanket35note rerecording.
Speaker/device/noise/case assignment and human gold coverage NOT established.
483LLM usage rows:424inputcounts/345outputcounts/70thinkingcounts/0cachedcounts;
allstoredcostNULL. All4tokenfields nevercomplete. Cacheunknown is NOT0%hits;
full-dollarcost unknown, free tier doesnot turn missing metering into zero cost.
Summary ../output/day13-closeout/recovered-evidence-report.md +analysisJSON;
sanitized timing and audio inventories CSV. Fiveknown cases matched, allhistoric;
do not pool mixedimages/controls/failures as formal SLA. Prior functions retained.
Async owner scope question PENDING: recommend ONLY10text+6shortvoice fresh timing
observations oncurrentbuild, or explicitly defer performance/cache/fullcost as
open exceptions (no false thresholdpasses). Concrete proposed timing plan under
day13-closeout/targeted-timing-plan.proposed.md; not executed/adopted yet. Small
sample doesnot replace fullformal30/30/3repeat denominators automatically.
All38formal gates stillunwaived; Day13notclosed. No newcalls/tests/deployment/AWS
mutation/priceupgrade/guardactivation. RetainGemini-low976dff/schema020.
Next incorporate owner choice; do not rerun audit or request more account screens.

2026-11-11 OWNER REPORTS CLOSEOUT AUDIT COMPLETE: provider_calls0/database_writes0.
Only completion marker returned; summary and DAY13_AUDIT_DATA_BASE64 not supplied.
Execution receipt ../output/day13-closeout/audit-execution-owner-receipt.json.
Do NOT rerun the audit/tests. Read the newest existing-evidence-*.json already
saved under /home/ubuntu/day13-closeout and return its compressed evidence line.
No metrics evaluated or Day13 closure inferred from marker. Other state unchanged.

2026-11-11 DAY13 CLOSEOUT STARTED: EXISTING EVIDENCE RECONCILED, LIVE AUDIT READY.
Current status ../output/day13-closeout/status.json +README.md. All38original
thresholds mapped to evidence families; no automatic passes/waivers/denominators.
Retain earlier accepted customer/photos/safety/voice/broker behavior. Later living
tracker PROVES S04/S05/S06 livePASS on1ef4080 (missingfields/Edit/staleCancel/new
Cancel/no write), originaltext/sourceowner_direct/3photo draft+Cancel pass; exact
late-webhook race only DB-covered. S18 oldpriceUndo-after-photo refusal PASSED the
version guard; undo-across-later-write deferred, not required as new repair.
Historical timings preserved separately: cheaper voice4.646513s, English6.574384s,
Day12 textburst11.474417s and voicecorrection7.338028s. NO pooled/current p90 claim.
Current7eefb9c CIbothSUCCESS/deployed976dff/Gemini-low/schema020 unchanged.
Prepared ../output/day13-closeout-audit.sh: checksum-pinned exact-runtime gate,
restricted manzil_app READONLY REPEATABLE READ demo-tenant queries; newest500
turn inventory with explicit truncation, timestamps, usage and audio provenance
presence only. No phones, message bodies, transcripts, rawUUIDs, keys, DSNs or
storage keys exported. Candidate classifications are provisional, controls/failures
retained; runtime-start window association not per-turn image attestation.
OfflinechecksPASS: compile/notice+echo/unknownbilling/cachemissing/failureinfinity/
read-only-mock endtoend/redaction/audio/compressionLF. No real DB tests or provider
calls. Async owner onepaste EC2 execution requested; actual execution PENDING.
Returns summary+DAY13_AUDIT_DATA_BASE64 and saves private hostJSON. Next import,
classify/reconcile, identify ONLY remaining measurements/changed paths, then final
acceptance/gap decisions. D2-01 deliveryorder and newarea staffNote/source finding
still need reconciliation; do not silently close. No new35-recording/manual/broker
replay by default. Budget guardOFF, policy incomplete; no runtime/AWS mutation.

2026-11-11 OWNER STEERING: REUSE STABILIZATION TESTS; AVOID REDUNDANT TESTING.
This supersedes the default next-step request for a new35-note corpus / full
manual replay. Reviewed extracted stabilization plan, living tracker, accepted
Day2 checks and cohorts, Day12 final handoff/traces, current CI/deploy receipts.
Substantial functional search/photos/follow-up/safety/voice and owner-accepted
broker evidence exists. Reuse it, including81s duration exception; no new broker
setup, data reset, duplicate full pytest/CI/deployment or blanket manual rerun.
Plan ../output/day13-evidence-reuse-review.md and source-hash inventory JSON.
Next first reconcile stale checklist rows against later acceptance and inspect
existing timing/metering/audio metadata READ-ONLY. Historical functions remain
accepted; older timings stay separate by source/config, not pooled as current
p50/p90. Day12 handoff explicitly leaves latency/cost open (text11.474s and
voice7.338s individual samples do not establish p90). Need only demonstrably
missing13.3 performance/cost/cache evidence and affected changed-path checks,
then13.4 consolidated evidence/gap report. Low effort stays until tuning needed.
Current38-gate full3repeat/corpus denominators are not proven; user direction to
avoid duplicates is NOT a numerical threshold waiver, safety cut or formal Run2
completion. Any intentionally deferred formal coverage needs a recorded scope
decision. Do not fabricate passes, denoms or eligible corpus from old screenshots.
Account screenshots resolved; policy incomplete/guardOFF unchanged. Existing
free976dff/Gemini-low/schema020 runtime retained. No calls/tests/AWS mutations.

2026-11-11 ELEVENLABS USAGE SCREENSHOT MATCHES VERIFIED FREE ACCOUNT.
397/10000 used;9603 remaining credits. No separate Scribe minute allowance shown.
Account screenshot request RESOLVED; do not repeatedly request the same dashboard.
Official generic pricingFAQ https://elevenlabs.io/pricing says APPROX330credits/min
STT; balance suggests29.10min BEFORE keyterms/minimums/other products. This is NOT
verified account allowance or an enforceable billing rate. API pricing and credit
pricing pages differ; retain unknowns instead of silently applying a conversion.
Receipt ../output/day13-elevenlabs-capacity.json; screenshot privately in output.
Assignment targets: speech193s +controls164s; collection plus3backend
repeats target1428s, plus3deployed speech repeats target
2007s BEFORE tuning/retries/rehearsals. Targets are
not actual durations or billable totals (control rejection/truncation matters).
No assertion of sufficient/insufficient remaining credit from this estimate.
Existing prices-draft stays INCOMPLETE/account_verified=false. Besides Scribe
rounding/one-credit floor mapping, Gemini generateContent exact-model combined
thinking bound/default single candidate still need proof; current docs redirect
to Interactions API, which must not be silently treated as exact endpoint proof.
Guard remainsOFF; no generations/transcriptions/upgrade/runtime/AWS mutation.
Keep deployed7eefb9c/976dff/Gemini-low/schema020. No redeploy or repeated account
probe needed. Next independent work is recording/device assignment preparation;
activation/formal dispatch waits for complete bounds/policy, not more screenshots.
Day13.3/13.4 measured acceptance remains open; paid upgrade not requested/adopted.

2026-11-11 GEMINI PROJECT LIMITS RECORDED from owner AI Studio screenshot.
Primary gemini-3.5-flash-lite15RPM/250000inputTPM/500RPD; failover gemini-3.6-flash
5RPM/250000inputTPM/20RPD. Screenshot is PEAK usage over7days;137/500 and16/20
are historical peaks, NOT remaining daily allowance. Current headroom unknown.
Receipt ../output/day13-gemini-quota-evidence.json, screenshot in recordings/.
Formal5set3repeat matrix342scenario turns before setup/tuning/collection/deployed
cohort; provider call count can be higher. Free-tier run may span quota days;
pace BETWEEN measured trials, preserve in-turn retry/429 latency/failures, never
silently omit failed cases or pool changed configurations. Project-level limits
apply across keys; no key/project workaround or paid upgrade performed. No actual
quota enforcement added here; this is planning/evidence only. Provider docs reset
RPD at midnight Pacific. Gemini limit question resolved; no repeat request needed.
Pending ElevenLabs Scribe/API allowance plus billing floor/rounding/credit mapping,
verified reference-cost policy, shared guard activation, real corpus/human freeze,
tuning and full measured13.3/13.4. Budget guard remainsOFF; no benchmark/provider
calls/AWS or deployment changes. Day13 remains open; current976dff/schema020 kept.

2026-11-11 DAY13 READ-ONLY PROVIDER CHECK PASSED (owner EC2 evidence).
Completion marker observed; noisy terminal paste echo did not prevent execution.
Both configured Gemini models metadata HTTP200, generateContent listed, verified
input1048576/output65536 tokens. Exact deployed976dff/config34d4 gates passed in
wrapper. This is metadata access, NOT a generation/quota/cache/latency pass.
ElevenLabs subscription HTTP200 confirms tier/status free;397/10000 credits used,
9603 reported credits remaining, overage extension0 and both extension flagsfalse.
Do not convert these credits to Scribe seconds without verified account mapping.
Evidence ../output/day13-provider-account-evidence.json; no keys/raw account data.
No generation/transcription, mutation, billing upgrade or guard activation.
Next owner to provide current API-project primary/failover RPM/TPM/RPD from AI
Studio and remaining Scribe/API allowance from ElevenLabs Usage. Async question
pending; unavailable values must remain unknown. No repeat of completed check.
New official billing research: ElevenLabs2026-10-26 changelog says TTS/STT requests
below one credit are charged one credit. Account credit conversion and a policy
minimum/rounding that safely covers this floor must be verified before activation;
do not silently assume per-second pricing alone covers very short controls.
Source https://elevenlabs.io/docs/changelog/2026/10/26 . Policy remains incomplete;
free-credit cost and verified standard-rate usage estimates stay separate. Runtime
7eefb9c/976dff/Gemini-low/schema020 retained; guardOFF. No redeploy/migration or AWS
action needed for this read-only step. Formal13.3/13.4 acceptance still pending.

2026-11-11 DAY13 PROVIDER TIERS CLARIFIED: owner reports BOTH Gemini and ElevenLabs
are FREE API accounts, no paid upgrade. This supersedes the unknown-tier question;
API account/quota verification remains pending. Keep current deployed7eefb9c /
976dff195d20/Gemini-low/schema020; guard OFF, no service/AWS/billing change here.
Do not set reference prices to zero or claim cost/SLA/cache acceptance from free
credits. Record actual charges separately from standard-rate metered estimates,
retain quota failures/retries, and label the tested tier. Google's current pricing
table lists Flash-Lite free-tier caching unavailable; actual eligibility/metadata
needs evidence and any remaining gap a recorded decision, not an assumed pass.
Prepared ../output/day13-recordings/provider-account-check.sh: LF-only, exact
deployed image/config/running gates, Docker-exec GET model metadata and ElevenLabs
subscription allowlist only. No generation/transcription/countTokens, raw env,
account response, invoices or credentials output; no mutations. Offline GET-only,
redaction and denial/malformed/timeout/missing-key checks PASS. Owner must run it
on EC2 and return sanitized output. Read-only API permission failure is not proof
that generation fails; do not broaden permissions automatically. Also read the
current project's Gemini model RPM/TPM/RPD in AI Studio (not exposed by modelGET).
Policy remains incomplete/account_verified=false; $10(2/7/1) reference cap retained.
No benchmark/recordings/guard activation performed;13.3/13.4 acceptance and Day13
closure remain pending. No rebuild/redeploy/migration/bootstrap repeat needed.
Receipt ../output/day13-provider-tier-status.json; keep published kit unchanged.

2026-11-10 Day13 tooling published(PR66)/CI-tested/deployed at7eefb9c. Owner EC2
verified both services976dff195d20/Gemini/low/schema020, fixture/health/worker/tree
gatesPASS; no migration. Budget guard remainsOFF. Current open work is measured
13.3/13.4 acceptance: verified account/billing/cache policy, shared ledger activation,
real35-note corpus and human freeze, bounded effort comparison, full3repeatRun2,
actualWhatsApp p50/p90/cost/cache and Android/Web/manual evidence. iPhone exception
approved; no echo/Sonnet fallback adopted. AWS stays running; no automatic shutdown.
Prior c42 image/recovery artifacts retained; deployment is complete, do not repeat
bootstrap/migrations. No paid benchmark run yet; Day13 is not closed.

2026-11-10 Day13.3/13.4 tooling implementation underway; acceptance NOT COMPLETE.
Missing real35-note private corpus + human audio-bound gold, verified account
prices/Scribe rounding/Flash-Lite cache eligibility, bounded exact-image deployed
cohort/shared mount, three formal repeats and Android/Web rehearsal evidence.
No paid calls/new deploy/AWS action. iPhone exception approved; keep untested.
Owner resolved SC-18 current graph/no_match and ST-01 RFC duplicates DH6-101/106
before paid runs; those two historical gold questions are now closed for this
cohort. No neighbour/data changes or weakened duplicate warning. Current runtime
remains c42e91b03f6e/Gemini/schema020, without new budget controls. No measured
latency/cost/cache/fallback adoption claim. Guide docs/DAY13_BENCHMARK.md.

2026-11-10 Day13.1/13.2 IMPLEMENTED/TESTED/DEPLOYED with Gemini. Operator fixture
relative-path deployment finding resolved; resume/finalverification successful.
Bothservicesc42e91b03f6e/Gemini/schema020; fixtures/healthPASS. PR65/CIbothSUCCESS;
no merge. Prior rollback entry below is historical. Receipts output/day13-deployment-
verified.json/status.json. Live new-feature WhatsApp smoke remains unperformed;
13.3/13.4 remain outside scope. AWSrunning by ownerauthorization; no shutdown.

2026-11-10 Day13 deployment partial:020applied/immutable, newimagec42built and
stubchecked, Gemini verification failed on operator fixture relativepath aftercd.
Safe rollback to prior877/providerstub verified;020retained. Resume helperfix ready
and18offline repair/resume checksPASS. Deployment NOT complete until Gemini/final
marker verified. See PROJECT_STATE current entry and output/day13-deployment-status.json.

2026-11-10 Day13.1/13.2 locally implemented/validated:3982passed/42platformskips,
4024unique cases, no remaining failures/errors; output/day13-validation.json.
New migration020 is NOT production-applied. AWS remains stopped. New exact-head
Linux CI/gitleaks and live WhatsApp acceptance pending before release. Compatibility
limits (legacy stored detail payloads/history) and manual #reconfirm flow are in
DAY13_SECURITY.md. No13.3/13.4 work, merge, deployment or production reset.

# Open issues

2026-11-09 owner verified36989b5 build/switch: web/worker both sha256:521a0033b221d7db37ab7b5fde10aaa884fe7b79e369e2a7d87cc6ce53eb5cff, SWITCH-VERIFIED-STUB-36989b5; Gemini enabledhealthDB/bucketOK; seed30/public29/GLI103-108/L27/roles/preparedmediagatesPASS. Originalstrictvoice5s01:33-01:34PKT screenshot exactly2Phase8options DH8-10174m/DH8-10269m,noDH7/neighbour. D2-07visiblefailingcasePASS; persistedflag/search/timing and budget-followup acceptance pending. Evidence output/day2-36989b5-strict-visible-evidence.json. Next samechat typed Budget8.5croretakkardo withoutreset/area mention; expectonlysamePhase8codes,noPhase7, then collectbothturntraces together. PR62reviewmergepending; formalDay2incomplete/no mixedimagep90. Preserve29public/L27/L28history/roles/photos/81sV06exception.

2026-11-09 D2-07 strictarea fix COMMITTED36989b5ef044843c20b31b575d79d3249278461d onfix/day2-strict-location, parentb51146c. Ownerexplicitlyrequires sirfPhase8 exclude all nearbyphases. Source-derivedlocation.no_nearby persistsbudgetfollowup/clarification; strictreplacementoverridesmodeladd, searchneverautomaticallyfetches/relaxesneighbours. ExplicitNearbyactioncanbroadenwithauditnearby_requested. Broad419search/requirements/voice/customerflowPASS6292.94s (elapsed includes userpause/systemtime); final237followupPASS18.03s, includingclarification andnomatchboundary; suitesoverlap, no additivesum. Initialboundarytest assumed7cr couldnotfitPhase6, but existing1.05*1.10toleranceallowed78m; fixturecorrectedto6.6cr, no search-policychange. Ruff/stagedwhitespacePASS. No testsrunning. Ownerpush/draftstackedPR/CIcommands output/day2-strict-location-next.txt; body output/day2-strict-location-pr.md. No push/newCI/deployclaimed; liveb51146c/image146c43d4/Gemini/29public/L27/L28history/roles/photosunchanged. V04nowFAILperownerrestriction untilacceptedrelease/originalvoicererun. Owneraccepted81sV06exception retained. RemainingDay2timing/testdoc/findingclosure notpassed.

2026-11-09 continuing D2-07 strictarea fix after ownerexplicitpolicy: sirfPhase8 MUSTexcludePhase7 evenlabellednearby. Branchfix/day2-strict-location fromb51146c: deterministic verified-spanexclusivecontext -> location.no_nearby; persistbudgetfollowups/clarification; strictreplacementoverridesadditiveoperation; searchomitsneighboursbeforecandidatefetch/ladder, explicitNearbyactioncanbroadenwithnearby_requested auditflag. Focused223PASS9.45s/RuffPASS. Broadsearch/requirements/voice/customerflow running session17253, nearcompletion; no finalpassclaimyet. No newdeployment; runtimeb51146c/image146c43d4/Gemini unchanged. Owner81slongnoteexception remainsaccepted; no re-recordsolelyduration. Preserve29public/L27/L28history/single2614/photos/roles.

2026-11-08 V04placename5svoice screenshot23:05PKT onb51146c: DHA8correctsummary500gazsale/max8cr, #1DH8-10174m/#2DH8-10269m correct; #3DH7-10175m explicitlynearby. PlacerecognitionvisiblePASS; scriptedrequestSirfDefencePhase8 suggestsstrictarea intent, so fullV04acceptance pending transcript/filter/policytriage, do not claimonly2matchpass or unsupportedroot. Timingpending. CancontinueV05silence5-10s withoutreset, then batchscopedtraceV04/V05. No newsource/deploychange; preservechat/29public/L27/L28history/roles/photos. Formalp90notpassed.

2026-11-08 D2-06 FUNCTIONALLY RESOLVED onverifiedb51146c/image146c43d4: ownertraceEnglishturne46ff34f-d708-4d7d-abb8-de80cbe48adb patch45budgetAROUND72-88m->MAX80m/minNone. Transcriptup_to present; search2f1a958b exactDHA6/house/sale/80m/no lowerbound/shown10280m,10178m,710175mnearby. Receipt18:00:13.725573-lastsent20.299957=6574.384ms; Scribe806ms/7740msaudiountruncated. Evidence output/day2-b51146c-english-acceptance-evidence.json. PR61reviewmergepending/D2-01deliveryorderopen. Created finalcandidatevoicecohort output/day2-b51146c-final-voice-cohort.json withV03functionalPASS6.574384s; older2ee/0ecohortsretainedseparate, no mixedimagep90. NextV04placenamevoice SirfDefencePhase8meinghardikhao,budget8croretak; expectDHA8/house/sale/max80m/500gazretained, DH8-10269m and10174m, no wrongparent/city. FormalV01/V02rerunonfinalcandidate, silence,2min,timing/text/findingclosurestillpending. Preserve29public/L27/L28history/roles/photos.

2026-11-08 ownerverified b51146c build/switch: bothweb/worker sha256:146c43d4b029058a3e130200f9dce4cbc051493b61e5e3bab79bf748b0cf494a, SWITCH-VERIFIED-STUB-b51146c; GeminienabledhealthDB/bucketOK; seed30/public29/GLI103-108/L27/roles/preparedmediagatesPASS. English8svoice23:00PKT screenshot nowup-to8crore summary; DH6-10280m/10178m andDH7-10175mnearbycorrect. VisibleD2-06failingcaseretestPASS, storedmode/searchtrace/timingpending. D2-01coverless3beforesummarycarousel stillreproduced. Preparedcompiledread-only output/day2-b51146c-english-acceptance-diagnostic.txt exactlyone2614audio17:59-18:02UTC; expectmode max/max80m/minNone. No newsourcechange; PR61reviewmergepending, formalvoicefinalcandidatecohortnotcomplete. Preservechat/29public/L27/L28history/roles/photos; noreset.

2026-11-08 D2-06 localfix COMMITTEDb51146ce1af783cf6c642582fb2b82b3caf9251e onfix/day2-budget-prefix-operator parent2ee415b. Pairedsame-sourcebefore/afterspancontext, knownadjacentprefixoperatorrecovery, repeatedreadingask/crossmessageisolationpreserved. Final290normalise/requirementsDB/voice/searchholdPASS122.83s/artifact output/day2-location-fix-20261108T171149524534Z; additional276requirements/AItoolpurePASS0.92s; Ruff/stagedwhitespacePASS. Initialfocus186+1languageassertfailure is superseded, notcountedadditively. No testsrunning. Ownerpush/draftstackedPR/CIcommands output/day2-budget-prefix-next.txt, PRbody output/day2-budget-prefix-pr.md. No remotepush/newCI/deployclaim. Live2ee415b/image8966e9de/Gemini; failedEnglishcontext44around72-88m retained for exactrerunafteracceptedrelease, noreset. D2-06OPEN/V03failed; D2-05functionalresolved; 29public/L27/L28history/roles/photospreserved. Formalvoicebatch/timingcompletionstillpending; no mixed-imagep90.

2026-11-08 D2-06High confirmedV03operatorpreservation: transcriptup_to signal, patch44bare8croresignals savedAROUND72-88m, searchDH6-10395mabovebudget. Receipt-lastsend5340.554ms, notfunctionalpass. Branchfix/day2-budget-prefix-operator from2ee415b implements pairedsame-sourcebefore/afterEvidence; normalisereadsanchoredknownprefixoperators when spanhasnone; preserves repeatedreadingclarification andcrossmessageisolation. Initialfocus186passed/1testlanguageassertfailed (expectedRomanUrdu vsactualEnglish); assertioncorrected, nofunctionaltestbypass. Finalnormalise/requirementsDB/voice/searchhold RUNNINGsession30314, artifactlatestoutput/day2-location-fix. Additional276requirements/AItoolpurePASS0.92s,fullRuffPASS. No newdeploy/sourcecommit yet. Evidence output/day2-english-budget-operator-evidence.json. D2-05functionalresolved; V03failed/cohortpaused. Preservecurrent2eeimage8966/Gemini/29public/L27/L28history/roles/photos.

2026-11-08 D2-05 FUNCTIONALLY RESOLVED onverified2ee415b/image8966e9de. Owneracceptancetrace turn33e852a2-8888-4d69-b42b-7e799e71022a: Scribe2200msaudiotranscribed456ms; onebudgetrelativeAPPLIEDpatch43 80m->72.75m; searchf4f64e60 version43DHA6/house/sale cap72.75m/excluded3/zero matches/shown. Correctsoriginalvoicefailure withoutreset. Receipt17:05:13.314261 tosent17.960774=4646.513ms. Evidence output/day2-2ee415b-voice-acceptance-evidence.json. PR60peerreview/mergepending, notformalclosure. New final-imagevoicecohort output/day2-2ee415b-final-voice-cohort.json V02PASS; prior0ecohortretainedseparate, V01rerunneededonfinalcandidate beforefinalp90. NextV03 English voice fullDHA6/500sqyd/buy/budget8cr explicitrestore; thenplace/silence/2min. Formalp90notyetpassed. Preserve29public/L27/L28history/roles/photos.

2026-11-08 owner verified2ee415b deployment: bothweb/worker sha256:8966e9de8c41fff4395788c2f62d66cde19c40040e231ac207166021222b6f44, SWITCH-VERIFIED-STUB-2ee415b; Gemini enabled healthDB/bucketOK; seed30/public29/GLI103-108/L27/roles/preparedmedia gatesPASS.22:05PKT2svoicecheaper screenshot shows correctPhase6/500gaz/sale no-match/floor7.8cr/alternatives,no repeatedcards. VisiblefailingcaseacceptancePASS; need single scopedpatch/search/STT/timingtrace beforeD2-05functionalclosure. Prepared compiledread-only output/day2-2ee415b-voice-acceptance-diagnostic.txt, exactlyone2614audio17:04-17:07UTC. Preservecurrentchat/noreset; PR60reviewmergepending; old0evoicecohortcannotmixforfinalp90. FormalEnglish/place/silence/2min and texttimings/findingclosure stillpending.

2026-11-08 D2-05 localcandidate COMMITTED2ee415bd79965530c8ae1287f119782f4941c921 onfix/day2-voice-cheaper-script parent0e0ad24. Final213customerintent/customerflow/voiceDBPASS116.76s, additional242normalise/AItoolpurePASS0.52s, fullRuff/stagedwhitespacePASS; initial129overlaps, notadditive. JUnit artifact output/day2-location-fix-20261108T164433672868Z. No testsrunning. New4filecommit only commandrecognition+tests+doc; dirtytrackingstate/openissues preservedunstaged. Ownerpush/draftstackedPR/CIcommands output/day2-voice-cheaper-next.txt, PRbody output/day2-voice-cheaper-pr.md. No remotepush/CI/newdeployclaimed. Live0e0ad24image286c39ad/Gemini, failedV02budget80m/contextpreserved for rerunafteracceptedrelease. D2-05OPENuntilreview/deploy/live; formalvoicebatchpaused. Preserve29public/L27/L28history/roles/photos; no reset/brokerretet.

2026-11-08 D2-05 voicecheaperHigh confirmed class: exactfailedmessage flags raw/romanisedcommandFalse, Devanagaricheaper,7unsupportedtokens,noquestion,15chars. Existing guard onlyLatinallowlist; normalise.romanise translatescheaper butleavescommandfillers. Branch fix/day2-voice-cheaper-script from0e0ad24 adds existingnormalise+boundedcommandlocalDevanagarifillermap, returnoriginalspan; unknown/negated/conditional/mixedrequests stillrejected. SyntheticDevanagaricheaper fixture fails deployedparent recognizer/passescandidate. Initial129focusPASS; finalcustomerintent/customerflow/voiceDBsuite RUNNINGsession90171. Additional242normalise/AItoolpurePASS0.52s,fullRuffPASS. No newproductionchange/STTsettingchange. Evidence output/day2-voice-cheaper-script-evidence.json; live0e0ad24/V02failed, pausebeforevoicebatchcontinuation. Preserve29public/L27/L28history/2614context/roles/photos.

2026-11-08 V02trace confirmed: turn56184598-4b00-4a6d-b0e1-023473030774/Scribe2300msaudiotranscribed530ms hascheapervocab signal, no budgetpatch, search844fb61d version42 kept80m andsame3cards; setupturn8e927055 patch42 restored80m from72.75m. Not STTprovideroutage; exacttranscriptcommandform notexported/rootnotfullyproven. Code inspection cheaper_command onlyLatinallowlist, while CustomerBinding.text includesrawtranscripts; Urdu/Devanagari gap plausible. Prepared output/day2-voice-cheaper-script-check.txt read-only exactmessagebooleans/scriptsignals/unsupportedtokencount, no transcript/tokentext. Next owner runbefore targetednormalisationfix. V02FAIL/open, live0e0ad24unchanged.

2026-11-08 owner21:38PKT twoaudio screenshots: first3s budget-restorationnote returns80m/DH6-10280m,10178m,710175mnearby; second2s expectedcheaperV02 repeats80m/samecards. V02 visibleFAIL, notpass; causeunproven until STT/patch/search trace. No candidatecodefix yet. Prepared syntaxchecked read-only output/day2-voice-cheaper-retest-diagnostic.txt for exactlytwo2614audios16:37-16:40UTC; fixedcheaper/show/budget/eightcrore signals pluspatchbudgetvalues/searchfilters/times only, no transcript/contactexport. Pausefurthermessages/resets until trace; formalvoicecohort annotated. Prior21:37typedcheaperPASS remains separate. Preserveallcontext/L27/roles/photos.

2026-11-08 owner21:20PKT documents safety: DH6-103 ke documents verified hain? -> documents/legal confirmation delegated to agent with Agentsebaat button, no invented verification claim. Safety-content PASS. Extra preceding "In jagahon mein se jawab nahi mila. Apna chaha hua area likh kar bhejein." is unrelated to listing-document question; response-quality issue recorded, cause unproven (priorp6clarification remained unanswered). Do not mark overall response clean or claim new root/fix. Next samechat finalprice safety DH6-103 ki final price kya hai? Expect no fabricated negotiated/finalprice, distinguish advertisedprice ifgiven, agentconfirmationoffer. Collect scoped trace during triage for unrelatedlocationnotice; preserveconversation. Formalvoice/texttimings/reviewfindingclosure still pending.

2026-11-08 D2-04 FUNCTIONALLY RESOLVED on verified0e0ad24/image286c39ad. Owner read-only two-voice trace: first turn379c7609 versions31/32 fullBahria+Phase8 UNKNOWN, no search/outboundclarification; second cd8ce046 version33DHA8APPLIED,house/2bedminimum/3bath, budget8.1-9.9m,no matches/shown. Screenshots confirm no staleDHA acknowledgement on unresolved input and correct correction reply. Receipt-to-outbound-sent7100.765ms/5457.662ms (not formalvoicecohort/p90); STT524/921ms,6380/7240msaudio,no truncation. Evidence output/day2-0e0ad24-voice-retest-evidence.json. PR58/59 peerreview/merge pending; no generalSTTaccuracy/DD-orientationfilter claim. Next resume unresolved formalDay2 safety/voice/text timing/customer-staff checks on same single2614customer; clearack then ambiguousp6 no-area test to ensure clarification rather than inheritedarea guess. Existingf17partialtextcohort retained asbaseline, finalnewimagecohort needed. Preserve29public/L27/roles/photos; no broker retest/reset.

2026-11-08 owner21:12PKT correction voice screenshot and confirmed gold: Defence Phase8 mein ghar dekho,2bedDD,3bathroom,parkfacing,westopen. Reply DHA8/retained90lakh no-match/floor6.9crore with nearby/budget/agent buttons, no staleBahria. Visible correction/recovery PASS on verified0e0ad24. Prepared syntax-checked read-only output/day2-0e0ad24-voice-retest-diagnostic.txt for exactly two2614audio notes16:09-16:14UTC, signals/patches/search/timing only. Need voice1fullqualifiedUNKNOWN/no search and voice2DHA8APPLIED/beds2/baths3/no-match audit before closing D2-04; no screenshot latency/p90 or unsupported-preference validation claim. FormalDay2 cohort/safety/customer-staff remainder still pending, broker excluded perowner. Preserve data/context/L27; no new source or deployment change.

2026-11-08 owner verified0e0ad24 build/switch: web and worker both sha256:286c39ad6cb2db56ff7633e7a96271a78e3edb833aea66322154f30c0162f7bf, SWITCH-VERIFIED-STUB-0e0ad24, Gemini enabled and health database/bucketOK. Seed30/public29/GLI103-108/L27/roles/preparedmedia guards PASS.21:08PKT typed and21:10-11PKT six-second voice screenshots show unknown-qualified BahriaPhase8 clarification only, no staleDHA acknowledgement/no listings. Visible response acceptance PASS for both; no scoped voice transcript/search audit yet, no timing claim from screenshot. Next samechat2614 complete correction voice: Defence Phase8 mein ghar dekho,2bedDD,3bathroom,parkfacing,westopen. Expect correctDHA8 and minbeds2/baths3; inherited90lakh may correctly no-match/floor6.9cr. Then scoped trace/voice acceptance and remaining formalDay2 tests; no reset or broker retest. D2-04 functional closure awaits recovery/trace; reviews/merges remain pending.

2026-11-08 owner21:08PKT screenshot: typed "Bahria Town Phase 8 mein 2 bed DD 3 bathroom park facing" receives only unknown-place clarification under Bahria Town Karachi, no stale DHA8 acknowledgement and no listing shown. Visible typed acceptance PASS. Owner has not yet supplied new0e0ad24 build/switch/image/provider/health output; do not infer verified deployment from screenshot. Last terminal-verified runtime22d7015/dc9. Next request SWITCH-VERIFIED-STUB-0e0ad24 section with image/health, then same full qualified unsupported-place voice and complete DefencePhase8 correction. Voice/STT trace and final Day2 checks remain pending. Preserve2614context/29public/L27/photos/roles; no reset or broker retest.

2026-11-08 residual D2-04 acknowledgement fix committed 0e0ad246687cf67c0daaad7796201dc8642d9c89 on fix/day2-unresolved-location-summary, parent22d7015. filters_text excludes unresolved location from this turn summary; stored state retained. 152 customer/rendering/location/search-hold/requirements tests PASS96.73s (no failures/skips), full Ruff and staged whitespace PASS. Artifact output/day2-location-fix-20261108T154505573385Z. Owner push/draft stacked PR/CI commands output/day2-location-summary-next.txt; body output/day2-location-summary-pr.md. No new push/CI/deploy claimed; runtime22d7015/dc9b8f86 Gemini healthy. Typed guard sample passed (full location UNKNOWN, no search) but acknowledgement defect requires this follow-up and live acceptance; voice batch still pending. Preserve29public/L27/2614context/photos; no broker retest/reset.

2026-11-08 typed22d7015 trace confirmed: turn2c268db9-3600-4fe3-8fc3-0136af2d0b4e/version27 keeps full Bahria+Phase8 UNKNOWN, beds2/baths3 applied, zero search_run rows, one sent outbound. Guard passes this typed sample; voice acceptance still pending. Residual acknowledgement repeats stored DHA8 because CustomerBinding.filters_text includes old location despite place_unresolved. New branch fix/day2-unresolved-location-summary from22d7015 excludes only location from summary during this hold, keeps stored history/search behavior unchanged. End-to-end disposable DB regression PASS including subsequent complete correction; broader customer/requirements validation running, Ruff PASS. No new deployment; live remains22d7015/dc9b8f86/Gemini,29public/L27/roles preserved. Evidence output/day2-22d7015-typed-retest-evidence.json. Do not close D2-04 or claim improved STT/preference filters.

- D2-04 local candidate committed 22d701558629524103027b127d081208abf8c51f: qualifiedplace repair guard/promptguidance, originalf17failure reproduced,595pure +214finalcustomer/voice/state/searchPASS/0fails-errors-skips, RuffPASS. Push/fullCI/review/acceptedrelease/livequalification recheck pending. This fixes silent scope broadening, not transcriptionaccuracy/newDD-orientation filters; remainsOPEN. Runtimef17unchanged.

- D2-04 confirmedHigh: Scribe1 hasBahria+Phase8; two UNKNOWNqualified updates were followed by APPLIEDparentBahriaTown, then broadenedsearch/BTK101. Beds2/baths3 correctlystored; voice2DefencePhase8 correct/no-matchconsistent90lakh, park/westunmapped. Localguard/promptcandidate prevents dropping qualifier withinrepair; baselinefailure reproduced,595pure+4initialDBPASS, finalwidevalidationRUNNING. No STTaccuracy fix or live deployment claimed. Evidence output/day2-location-scope-evidence.json; full transcripts not exported.

- **D2-04 provisionalHigh8Nov19:27PKT:** first Phase8/2bedDD/3bath/park-facing voice reports unknown underBahriaTown then showsBTK-101. Second explicitDefencePhase8/2bedDD/3bath/park-facing/west-open usesDHA8 but gives unsupportedpreference notice + no-match at retained90lakh; latter price no-match itself consistent. Need scopedSTTsignals/patches/searchfilters to separate recognition, extraction and location-gate problems. Readonly diagnostic prepared; no cause/fix claim. Preserve state; pauseT07/voicebatch, requeststub.

- **D2-01 diagnostic update8Nov:** plan/send order proven correct forT02b2134af2: carousel accepted14:13:52.313UTC then coverless3at52.830; delivered3at54 beforecarousel55, matching screenshot. Observed delivery-order reversal occurs aftersend; exact downstream mechanism unknown. Keep presentationrisk open for triage, no blind send-loop reorder. Evidence output/day2-t02-ordering-evidence.json includes separate firstnotice/result/fullpayload times.

- D2-01 reproduced8Nov19:13PKT onf17 duringT02: coverless3DH7-101 precedes summary/covered102+101carousel; allfacts/filtermembership correct. Read-only first-T02 storedplan/send/delivery/fallback trace prepared in output/day2-t02-ordering-paste.txt; actual trace pending. Source loopsplaninorder; cause still unproven, do not assume Meta or reorder code yet.

- D2-03 live acceptance complete on f17dc3f: C05no-match/floor/alternatives PASS03:05PKT; C06addPhase8/retainPhase6/correct6.9cr+7.4crabove-budgetlabel PASS03:08PKT. Functionally resolved; PR57/56 review/merge still unreported. Precise latency and remaining Day2 not passed. Earlier pending notes below are history.

- D2-03 latest (7 Nov03:05PKT): **live C05 retest PASS on verified deployed f17dc3f/imagebc7a7c4575e4**. Same failed conversation/noreset now gives honest cheaper no-match/7.8cr floor/3alternatives/no repeatedcards. Functionally resolved; C06follow-up and PR57/56 peer review/merge pending. Earlier failure remains historical. D2-01 stillopen.

- D2-03 latest: f17dc3f full CI PR42299469694 and push42299311710 both SUCCESS, all steps green. PR57 ready/Taha review next; current runtime still5d, no f17 deployment or live C05-C06 acceptance. Finding remains OPEN.

- D2-03 update: zero persisted C05 patches/version11/80m/noexclusions confirmed by live diagnostic. Local fix committed f17dc3ffe3692668373cdeaf1be322bdba834f0d on fix/day2-cheaper-followup: apply missing standalone cheaper update before search with existing normaliser, once per turn; hold on failure. Regression reproduced before fix; Ruff/411targeted/407compatPASS (0errors/failures/skips). Push/fullCI/review/deploy/liveC05-C06 pending; still OPEN. Keep conversation intact, C06paused.

- **D2-03 High (7 Nov02:29PKT, deployed5d2f682): C05 cheaper ignored.** After C04, `Kuch sasta dikhao` repeats original8crore summary and all three house cards. Expected honest cheaper no-match/7.8crore floor and alternatives, no repeats. Rootcause unproven; read-only patch/budget/exclusions/search/prior-shown diagnostic prepared in output/day2-c05-diagnostic-paste.txt. Preserve state, pause C06, request stub. No fix or closure claimed.

- D2-01 reproduced on deployed 5d2f682 during C-02 voice (7 Nov 02:23 PKT): coverless #3 DH7-101 precedes summary and covered #1/#2 carousel. Membership/prices/nearby label correct; C02 functional pass only, exact timing unverified. Cause still needs plan/API-send/delivery metadata.

- D2-02 update (7 Nov, 02:20 PKT): **functional retest PASS on deployed 5d2f682**. Owner terminal verifies both roles on image a139f24988d2, health/fixtures intact; screenshots after clear acknowledgement show exact C-01 returning DH6-108 5, PEC-101 5.5, SHF-101 4.5 lakh/month with correct cards. Fresh C-01 now PASS; resume C-02. PR56 review/merge pending; deployment was owner-authorized demo exception. Exact latency and remaining regression pending. Earlier notes below are historical.

- D2-02 confirmed cause/update: live53/87catalogresolvesKarachi;modelmisclassifiedwholeofficeintentandbudgetaslocations. Localcandidate4e7536f/fix-day2-location-spansrejectsmalformedupdatesbeforewrites,holdssearchuntilcorrection,andprovidesboundedrepairguidancewithoutbroadeningresolver. Ruff+579targeted+218compatibilityPASS(0errors/fails/skips). Pushnetworkblocked/fullCI/Tahareview/deploy/livererunpending;remainsOPEN. No freshC01PASSorclosedfindingclaim.

- D2-02 (7Nov2026, Blocker): freshC01afterexplicitclearackrejectsKarachiunknownandreturnsnoofficecards(exactqueryoffice on rent in Karachi, around 5 lakh per month). Screenshotacksoffice/rent/budget,thenservedareaslist. Local1efdatasetresolveracceptsKarachi/inKarachi/Karachimein;notyetrootcaused. Preservefailedconversation,collectlivecatalogresolution+scriptedlocationpatchspan/status beforefix. Possibleextraction/catalog/runtimepathsremainhypotheses. EarlierScenario1PASSisnotfreshregressionpass. No code/catalog/gold changes.

- D2-01 (6 Nov 2026, provisional High, client-visible): owner screenshots after Scenario1 -> typed DHA6/500gaz/8croretak show coverless DH7-101 card labelled #3 before the text summary and covered #1 DH6-102/#2 DH6-101 carousel. Membership, prices and nearby label are correct. Visual ordering is confirmed; whether the cause is server send order or WhatsApp/media delivery is unverified. Read-only source inspection: cards.render constructs carousel/summary first, coverless cards afterwards; customer_reply.send iterates the stored plan in order. Inspect sanitized stored plan/outbox/send/delivery timestamps before attributing or fixing the issue. No fix or latency pass claimed. Functional3a/b/callpassed. Keep actual card-position mapping for3a; do not reorder expected facts to match old CT-27 hardcoded code.

- Webhook routing uses the demo env map (Task 2.4). A DB/platform resolver is needed before onboarding several agencies; replace EnvSenderResolver only.
- Worker tenant discovery uses the env sender map (Task 3.1); it moves with the webhook resolver when a DB/platform resolver replaces EnvSenderResolver.
- Migrations 001-010 are applied on RDS (008 / 009 / 010 by the owner on 2026-10-09 / 10, each after a named snapshot). Earlier history: 001-006 per Taha's handoff (release manzil-ai:2c447d3, snapshot manzil-pre-task34-2026-10-08 before 006); 007 applied by the owner on 2026-10-08 (`1 applied, 6 skipped`, repeat `0 applied, 7 skipped`, /health 200, echo received). 001/002 keep their CRLF checksums.
- Worker has no lease heartbeat: a job running longer than 300 s with no conversation key could be re-run by the reaper. Fine for the demo (status jobs are fast and idempotent).
- GEMINI_API_KEY and ELEVENLABS_API_KEY were missing on the server in the last reported check; partner delivery is pending. Do not claim installation or retesting.
- Meta system-user token was regenerated on 2026-10-08 (after the exposure) with no expiry. A non-expiring token must be rotated immediately if ever exposed (revoke all, then generate).
- The Meta app webhook subscribes 10 fields; only messages is handled (others are skipped with 200). Untick the rest to reduce noise.
- Remaining team-phone hello_world receipts are unconfirmed. new_lead_alert is Active (approved, English `en`, owner 2026-10-16); its variables are assumed numbered.
- Voice silence (Task 6.3, docs/SPEECH.md): no audio-level silence check (packet sizes cannot show silence on WhatsApp notes; no decoder). Silent notes cost ~5 s of Scribe minutes each and are caught by Scribe no_speech; a silent note on which Scribe errors reaches Gemini, guarded only by speech_present. Scribe output for Roman Urdu speech came back in Devanagari + Latin (live 2026-10-12), which the 6.2 normalisers do not read: decide language_code / transliteration / primary in the Day 8 benchmark. ElevenLabs free-tier data terms (training / retention; zero retention is enterprise-only) are unverified: team / volunteer phones only. The free tier (~12 min / month) is mostly used by the 35 benchmark notes. Demo data remains fictional and team-only.
- Local pre-commit secret hook deferred; CI scans full history with Gitleaks.
- Server deploy source: ~/manzil-ai on EC2 is a **stale** checkout (its migrations folder has only 001/002; app code predates 3.1) and must not be used to build releases. The live release is the image manzil-ai:2c447d3 (web / worker); the 007 migration ran from manzil-ai:2c447d3-007, built `FROM manzil-ai:2c447d3` plus the single 007 file (build dir ~/task33-007). Until the server source is rebuilt from main, ship migrations by layering them on the running release image so 001-006 stay byte-identical to what RDS recorded. The server has no .git; transfers use an approved paste archive through the EC2 browser terminal.
- Control-state integration is partial: Task 5.1's turn-level gate stub reads control_state and mode (AI only when ai_active and mode 'ai'; otherwise the turn is 'gated', nothing is forwarded yet). The send-time gate still reads conversation.mode (owner decision for Task 3.4); it moves with the handoff tasks (11.2 control states). control_state exists since 006 and is kept consistent by CHECK (control_state = 'ai_active' OR mode <> 'ai'). When the gate moves (handoff work, 11.2), cancel AI messages if any of mode <> 'ai', control_state <> 'ai_active' or ai_muted_until > now() holds (handoff_pending behaves like human_owned), update tests/test_gateway.py, and decide whether mode is then retired. Until then, code that sets control_state to a silenced value must also set mode. A control change committed during the HTTP call itself is not seen (window bounded by one call).
- WhatsApp token is the env META_ACCESS_TOKEN for the demo; per-tenant encrypted tokens (C15, tenant_secret) are deferred.
- Outside the 24 h window session sends are recorded failed/outside_window; the broker alert falls back to new_lead_alert (Task 11.1, docs/HANDOFF.md); customer messages are never templated.
- '#demo image' needs a fictional, DEMO-watermarked demo/echo-test.jpg in the bucket (upload needs AWS approval).
- Release archives and migration checksums: RDS recorded CRLF checksums for 001 and 002 (owner, verified on RDS and EC2), while Git/CI carry LF bytes. Never rewrite migration history or edit 001-005. Before any deployment, compare the sha256 of every migrations/*.sql in the image against RDS schema_migration (not only against Git); keep the server's 001/002 bytes and ship only new migrations. The local main checkout E:\manzilai has CRLF working copies of 001-004 (pre-.gitattributes checkout): do not build releases from it. Plain `git archive` applies autocrlf conversion to files without an eol attribute. A packaging check that tolerates the documented 001/002 production bytes (without weakening the runner) is still undecided.
- is_demo is not database-enforced per agency: production agencies must hold no is_demo rows (needs an agency-level demo flag in a later migration).
- v_public_listing is not a hard privilege boundary while manzil_app can read property; a customer-path role would be needed for that.
- Tenant seed data under FORCE RLS (Task 4.3) must be written as manzil_app inside tenant_transaction (the owner has no policy); the gazetteer is loaded by the migration/admin role.
- property_event rows are kept indefinitely; retention is undecided.
- Privacy deletion: transcripts, stt_meta, requirement slots / patch history, leads and handoffs (006) are customer data that the runtime cannot delete (no DELETE grant, append-only histories), and FORCE RLS also blocks the owner. The privacy page promises deletion on request, so a reviewed admin deletion workflow (dependency order: handoff, lead, media_sent, shown_item, search_run, patch log, state, focus, clarification, messages, conversation) and a retention period are needed before real customers.
- Handoff / lead items outside 006: reassignment history for #pass / #assign (R-32, production). (staff.areas_covered and agency city_default / marla_sqft now come from 007.)
- Task 3.4 contracts not enforced by the database (docs/STATE_SCHEMA.md): control_state and the open handoff stay in step; an expired clarification is closed before a new one opens; requirement_state.version matches the latest patch-log version; assigned staff are brokers/admins; JSONB ids (excluded_property_ids, candidates) are not referential. Implementers of 8.1 / 10.1 / 11.x must honour them; lead numbers must come from next_lead_number() in the inserting transaction, never inside an upsert VALUES list.
- Resolved by migration 007 (applied on RDS 2026-10-08): 005 (applied) diverged from the D11 / R17 specs: docs/SPEC_ALIGNMENT.md section 2. G1-G9 block the Task 4.3 seed or demo scenarios (no ZONE level for Bukhari / Badar Commercial, no location code for {AREA_CODE}-{seq}, no off_plan / grey_structure, no possession / installment / investment fields, no nominal sq-yd bucket, missing D11 property types, no on_hold / archived, no last_confirmed_at, no reconfirmed event). Owner decision: a separate follow-up task and PR (new forward migration; 005 untouched) before the 4.3 seed; not part of the Task 3.4 branch.
- service_window_expires_at is deliberately not stored (owner decision; departs from R17 5 / RFC 8): the window is derived from last_customer_message_at. Any later task that needs a stored value must add reliable update logic and tests with it.
- message.transcript, stt_meta and conversation.last_modality are written by Task 6.3 speech (customer voice notes, when STT_PROVIDER=elevenlabs); conversation.preferred_language is not written yet (10.x). last_modality is set to 'voice' only (a text turn does not reset it yet). message.context_wamid is written from Task 5.2 (inbound only; earlier rows stay NULL).
- Marla basis (007): area_sqft converts marla / kanal with property.area_marla_sqft, which must equal the agency's marla_sqft (composite FK). Open: the 6.2 normaliser must read agency.marla_sqft (never hardcode 225); changing an agency's marla_sqft after it has marla listings is blocked by the FK and needs a reviewed re-normalisation procedure.
- Migration 007 was applied on RDS on 2026-10-08 with this runbook (kept for later migrations). Lock scope (all held until the single migration-run transaction commits, app/migrate.py): ACCESS EXCLUSIVE on 8 tables (location, location_alias, location_neighbor, project, property, property_event, agency, staff) from ALTER TABLE ADD COLUMN / ADD / DROP CONSTRAINT and ALTER COLUMN SET DEFAULT / SET EXPRESSION; SHARE ROW EXCLUSIVE (writes blocked, reads allowed) on message, staff and agency as targets of the new FKs; ACCESS EXCLUSIVE on v_public_listing (CREATE OR REPLACE VIEW). Only property is rewritten (SET EXPRESSION); other steps are metadata or validation scans. Tables are empty or tiny, so the hold is expected to be well under a second, but webhook ingest (agency, conversation, message), the worker (agency join, message) and outbound (message) block meanwhile (their statement_timeout is 5 s; Meta retries webhooks; jobs retry). Real risk: queueing behind a long transaction. Runbook: (1) quiet window, no demo traffic; (2) RDS snapshot; (3) compare migration checksums in the image against RDS schema_migration (001/002 CRLF bytes kept); (4) run the migrator with the admin migrate.env and `PGOPTIONS='-c lock_timeout=3s'` so it fails fast instead of stalling; (5) expect 1 applied, 7 skipped; check /health and the echo bot; (6) a lock timeout or a failed CHECK rolls everything back, so investigate and retry. Every reader of last_confirmed_at must treat NULL as never confirmed.
- Task 4.1 is live (2026-10-09): migrations 001-008 on RDS; release manzil-ai:4.1-40e9913 = manzil-ai:latest (rollback manzil-ai:pre-4.1); the bucket's card/* is public-read by bucket policy (nothing else is public). The next release must be built FROM manzil-ai:4.1-40e9913 (or from a fresh server source that keeps the CRLF 001/002 bytes), never from the stale ~/manzil-ai.
- Staff photos are only fetched for senders in the staff table (admin / agent, active). Only the owner's phone is a staff row so far (agent, demo agency); the rest of the 1.1 role map is open. Once role routing exists that phone is staff, so it should not double as a scenario customer.
- inbound_media rows that end failed / rejected are not yet reported to staff; the staff flow (7.x / 9.x) owns "ask to resend" (D14).
- A transient failure exhausting all 3 attempts marks the photo failed; with the 5 s / 10 s backoff that window is short. Fine for the demo (the media ID stays valid for days, so a later manual replay can recover it).
- Task 4.2: Meta refuses an invalid media ID synchronously (verified 2026-10-09: http_400_meta_100), which falls back to the link at send time. An expired (> 30 days) ID is not verified; the failed-status path (media_link_fallback) stays as the backstop. Any failed status of an image sent by ID triggers one link resend, even when the cause is not the media (e.g. an undeliverable recipient): at most one extra send.
- Task 4.2 is live (2026-10-09): RDS 001-009; release manzil-ai:4.2-f65094d = manzil-ai:latest (rollback manzil-ai:pre-4.2). The next release builds FROM manzil-ai:4.2-f65094d.
- Task 5.1 turns: a job whose turn is still 'open' after its last attempt (dead-lettered by a non-send error) leaves the turn open; the next process_message job of that conversation resumes and answers it first. Gated turns are not forwarded to the broker until Task 11.x. Migration 010 locks message (ALTER TABLE ADD COLUMN + FK) and conversation / job / staff as FK targets for the migration transaction: apply in a quiet window with lock_timeout, after a snapshot, like 007-009.
- Task 5.2 is in production (2026-10-10; docs/CAROUSEL_RUNBOOK.md section 1): migration 011 applied on RDS, release `manzil-ai:5.2-ffeb311` live, pilot cover rows and card URLs verified, the carousel renders on the first device tested. Open: the button-tap fix deploy, the other two clients (gate M1(e)), the Task 4.4 private-copy check and staging clean-up.
- Task 5.2 carousel, to confirm on the device test: (1) card `"type": "cta_url"` with quick-reply buttons (app.whatsapp.CAROUSEL_CARD_TYPE): accepted by Meta and rendered on the first device (2026-10-10); confirm on the other clients. (2) Resolved 2026-10-10: a carousel quick-reply tap arrives as `type: button` with `button.payload` = the id (not in Meta's carousel docs); the turn now reads it (follow-up to PR #22). (3) The 1.91:1 card crop (docs/DECISIONS.md, MEDIA.md). (4) Meta states no maximum for quick replies per card; the builder allows 2.
- Task 5.3 inbound audio: no retention purge job yet (owner-run delete after the benchmark; C14 later); Meta's `voice` flag, codec and size on real Android / iPhone notes still to confirm (voice addendum A1-A7) when the benchmark notes come in. Durations are read only from Ogg; other formats leave duration_ms NULL.
- Task 5.2 known limits: an 'unknown' send stops the turn without resending, so later messages of that plan (a coverless card, the overflow list) are not sent; the carousel_fallback job reuses the carousel's image links, so a carousel that failed because its image could not be fetched will likely fail again as cards (not retried further); the overflow list is not reachable from the demo commands. The tap replies are stubs until Tasks 10.x / 11.x, and no shown_item / media_sent rows are written yet (search_run does not exist before Task 7.x).
- Task 6.1 (local, not deployed; migrations 013 and 014 not applied on RDS): live acceptance pending (docs/AI_TURN_ENGINE.md section 8): a real stub-tool turn and usage rows, a live failover, the request shape on the account's models, the phone check, and the cache check. Implicit caching needs >= 4,096 prompt tokens on Gemini 3.6 / 3.8 Flash; the 6.1 prompt + tools are far smaller, so cachedContentTokenCount > 0 is not expected: record the live result as a finding, never as a pass. Model availability / free-tier limits for the chosen IDs are only visible in AI Studio. Template wording awaits owner approval (including the interim "Ek minute, check kar raha hoon." added in the 6.1 follow-up). DNS resolution and the TCP connect are outside the call deadline, and database statements are bounded only by statement_timeout (docs/AI_TURN_ENGINE.md section 3). The worker's exit-78 restart behaviour is checked on local Docker only, not on EC2. Migration 014 is needed by this code with any AI_PROVIDER (apply 013 + 014 before deploying). A process_message job that dies before its message is in a turn (e.g. a database outage over all attempts) still leaves that message unanswered (existing dead-job behaviour); Thinking level is sent as low (LLM_THINKING_LEVEL; acceptance on generateContent is part of L4, latency unmeasured). A failed turn now gets one failure notice (6.1 follow-up); a crash between the close and the notice loses it.
- docs/PROJECT_STATE.md and docs/TASK_LOG.md on main start with a pasted chat transcript and lack Tasks 5.2-5.4 (5.3/5.4 handoff section 8): a separate docs-repair task; Task 6.1 deliberately does not touch them.
- Task 6.2 normalisers (docs/NORMALISATION.md): "1,800 ka plot" resolves to 1,800 sq yd by the <= 2,000 missing-unit rule although Data Model 4.1 cites it as a case to ask; revisit with 7.3's plausibility bands. With a 272.25 marla basis, "10 marla" falls in the 300 sq-yd bucket, not Bahria's market 250. Display formatting ("7.8 crore", "85 lakh") is left to the 9.1 renderer.
- Task 7.1 location resolver (docs/LOCATION_RESOLVER.md; owner decisions in DECISIONS.md): merged into main (PR #31, a42085f); review fixes in the corrective PR from fix/task-7-1-review-findings; not wired (7.3 loads the cache at worker start), not deployed. Closed by the review fixes: misleading fuzzy offers for unknown places sharing a word ("north karachi", Gulshan-e-Maymar / Jauhar / Hadeed, Nazimabad: name-word guard) and bare "Gulshan <other>" becoming Gulshan-e-Iqbal. Open: (1) 7.3 must treat area_hint as diagnostic only and read candidates with a default (docs/LOCATION_RESOLVER.md "Serialised form"); the benchmark gold validator's ">= 2 candidates" rule does not apply to resolver output; (2) Bahria Precinct 6 reaches production only through the owner-run seed (dry run, then --apply: +1 location, +2 aliases); (3) Clifton neighbours (8.1); (4) "Defence City" unresolved; (5) Urdu limited to four terms, Devanagari waits for the Day-8 STT decision; (6) the 0.30 floor and the four-letter guard are provisional project choices.
- Task 7.3 requirement state (docs/REQUIREMENTS.md): local branch, not merged or deployed. Open: (1) the confidence numbers are ordinal labels, not calibrated (docs/REQUIREMENTS.md "Slot confidence"); (2) R17 5 snapshot parts that wait for later tasks: last search_run + shown_item and focus (search service, 10.1), language (D15 / 10.x), control_state (11.2); (3) soft_prefs stay unmapped until the ranking vocabulary exists; (4) the gazetteer is refreshed only by a restart; (5) all new Roman Urdu templates await owner approval (table in docs/REQUIREMENTS.md).
- Task 7.2 STT benchmark (docs/STT_BENCHMARK.md): harness ready for review, acceptance pending. Open: (1) the 30 + 5 recordings, bench/voice_manifest.csv and human-verified gold (bench/voice_gold_verified.json) do not exist; (2) the control gate (zero requirements) cannot be run until the requirement-extraction path can take a transcript (7.3 / 8.1); (3) R16 does not say how items with no critical slot (CV-27, CV-29, CV-30) count or how a free transcript is mapped to slots: harness rules (docs/STT_BENCHMARK.md): they stay in the 30 and pass only on clean accepted speech with no invented requirement, with a separate 27-item diagnostic; (4) keyterms are rebuilt from demo/ (collation and Precinct 6 may differ from production; supply --keyterms-file); (5) no verified unit price for Scribe or Gemini audio, and Scribe's free allowance (~12 min / month) is about half used by one full run; (6) Devanagari output from Scribe on Roman Urdu speech (6.3 finding) is scored as it is: variants (language_code=ur, transliteration, Gemini primary) need owner approval and would use separate cache keys.
- Task 7.4 staff buffer (docs/STAFF_BUFFER.md): merged (PR #35); deployed before review by the owner's recorded decision. Migration 015 is on RDS. Open: (1) staff_photo_only / staff_media_missing wording awaits owner approval; (2) the stub segment reply stays until 8.2 draft extraction, the photo-only attach list is 9.2; (3) a description without a type word or amount ("ghar DHA 6 mein") is treated as a note; (4) no retention for segment rows yet.
- Task 8.1 search service (docs/SEARCH.md): merged (PR #36); 10.1 still needs to wire search_properties, cards, the no-match reply and record_shown. Open: (1) soft-preference tags score 0 until a vocabulary exists; (2) no duplicate_group_id dedupe (R17 edge 14); (3) Clifton neighbours not seeded; (4) tolerances run in code over the hard-filtered scope (fine for demo inventory); (5) echo-before-search for voice-critical slots: built in 10.2 (docs/VOICE_TURN.md).
- Task 9.1 renderer + grounding validator (docs/RENDERER.md): merged (PR #38); nothing calls it until 10.1. Open: (1) customer wording awaits owner approval (RENDERER.md section 2); (2) 10.1 wires render_search into search_properties, sends the plan, calls record_shown after the send, handles the nomatch:* / clarify:* / handoff:request taps and registers a detail tool around app/details.py; (3) switch turn_engine.ground_reply to grounding.check_payloads (it reads only the interactive body today, not card bodies or button titles); (4) 7.3's purpose_inferred notice and the renderer's echo line can both fire in one turn: 10.1 keeps one; (5) the validator does not parse dates or percentages other than 100 %, so a model wrapper stays off.
- Task 9.2 staff commit (docs/STAFF_COMMIT.md section 6): live as manzil-ai:9.2b-64f0fd0 with migrations 016 + 017 on RDS (per the 10.1 handoff); PR #39 landed on the 8.2 branch, PR #40 brings it into main. Open: (1) all new staff wording awaits owner approval; (2) device check whether every photo of a reply album carries context.id; (3) live scenario 4a-4c on the staff phone not yet run; (4) customer search of the new / updated listing depends on 10.1 (in progress); (5) media_added is not undoable; (6) R-13 bulk-status rate limit is production-only and not built.
- Task 10.1 customer turn wiring (docs/CUSTOMER_TURN.md): PR open for review, based on main with PR #40 (9.2) inside until #40 merges; not deployed, no live WhatsApp check yet. Open: (1) all new customer wording (6 Roman Urdu notices, the reference question, English versions of 29 customer notices) awaits owner approval; (2) scenario 3a live needs DH6-102's photos (4.4) and a carousel needs 2 covers; (3) a quoted carousel resolves only with a position on it; (4) PHOTOS_PER_ASK = 5 and the shown-cover rule are project choices; (5) the deploy layers on manzil-ai:9.2b-64f0fd0 and needs the owner's notice first (deploy rule 2026-10-14).
- Task 10.2 voice end to end (docs/VOICE_TURN.md): local, uncommitted, not deployed, no live voice check. Open: (1) live scenario 2 from customer B and its latency (receipt -> first reply <= 10 s); (2) owner approval of the uncertainty proposal, the search hold, the Urdu type words and all new wording; (3) Devanagari (unsupported script): places are not resolved, types / amounts / operators / units in the fixed `normalise.romanise` table are read since 2026-10-17 (others are asked), a value the model never extracts cannot be detected; the Scribe mixed transcript (Latin place, Devanagari "घर" / "तक") passes scenario 2, a fully Devanagari one cannot (place); the STT setting (Scribe language_code, or Gemini as primary) stays with the 7.2 benchmark; (4) 7.2 not run: no benchmark recordings, manifest, verified gold, live run or control gate (the stored live notes are acceptance / smoke checks); (5) the 7.3 plausibility guard needs a purpose before the budget (pre-existing); (6) the buy <-> rent budget question (a 7.3 note, not an ask) does not hold the search (persisted-purpose follow-up, separate).
- Tasks 11.1 + 11.2 handoff (docs/HANDOFF.md): branch feature/task-11-1-handoff, not deployed, no live check. Open: (1) live scenario 5 on the team phones and the closed-window template path (gate M5); (2) owner approval of the customer and staff wording; (3) a forward to an agent whose window is closed is recorded, not delivered (no template for forwards; the broker writes first on demo day); (4) low-confidence voice intent confirmation, outcome buttons and R-32 reassignment history are not built; (5) template variables assumed numbered; (6) rolling back to a pre-11.1 image leaves open handoffs gated: #done them first.

2026-11-03 Day 1 preparation (Codex, fix/day1-stabilisation on 26ca5cc): reset refuses non-demo property/project inventory before changes; inactive staff conversations now reset as customers. Owner approved ACTIVE-only description targeting and migration-free reset report exception. Added regressions, current-baseline app-only kit builder and Day 1 operator checklist; existing historical kit unchanged. Incomplete-draft card test passes; worker/live S-04 pending. 134 targeted tests +4 packaging tests passed, 57 DB tests skipped; lint/shell syntax/diff checks passed. Docker is running per owner, but sandbox named-pipe access denied; owner-running DB check requested. No CI/review/merge/deploy/live results. Live baseline and generated concrete release paste still pending; use docs/DAY1_STABILISATION.md. No Day 2 work.

2026-11-03 verification update (owner-reported): disposable postgres:18 container created on 127.0.0.1:55432, then REQUIRE_DATABASE_TESTS=1 run of tests/test_demo_reset.py, tests/test_staff_commit_db.py, tests/test_staff_turn_db.py, tests/test_staff_update_db.py and tests/test_staff_find.py passed: 119 passed in 116.64s, no skips/failures. Covers reset guards, inactive-staff reset, staff commit/Undo/Edit/confirmation and incomplete-draft worker behavior. The earlier 2-pass attempt was interrupted before the DB container existed; it is not a failed application test or a completed DB run. Live S-04 remains pending. CI, Taha review, deployment and Day1 live gates remain unverified; GitHub network access still blocked from this session. Next: owner pushes branch for draft PR/CI and review, then supplies fresh production baseline.

2026-11-03 Day1 live findings, branch fix/day1-live-findings from origin/main bbd106d: owner completed core staff create/update/Undo/photo/SOLD-search checks. W06High repeated Note and W08High already-shown-then-SOLD bare-code lookup asking which property reproduced live. W08 cause: reference history resolves old shown item, details_card public refetch returns empty but mapped to listing_unknown. Reuse existing listing_unavailable template, no facts/status leaked. W06: drop only whole description clauses accounted for by parsed verified facts; exact aliases only for location, conservative preservation of unknown/negated/different facts and extra detail; recheck corrections and Save of old drafts. No migration or wording additions. Added pure/worker regressions. DB tests requested from owner; live remains bbd106d. Release must layer on current009f2dbb image after new PR review/merge/CI; retain SOLD109 for retest. Other open Day1 checks: incompleteDraft/Edit/Cancel, late-photo finding, photo-blocked Undo finding, final secondreset/role restoration/shutdown. iPhone and independent multiple-phone evidence absent; Android/Web confirmed by owner. W07provider failure recovered on controlled4c retry; keep as demo risk, no persistent code fix claimed.

2026-11-04 update: PR54/a05b477 deployed as image2e98679882875373113f978eb878f4b2f89c98a9ae3bab9a6d48048f0c295111, W08 live unavailable regression closed. W06 original repeatedNote symptom absent with exact source; extra-detail live test pending. New W09High: explicit owner direct omitted from draft despite source confirmed; W10Blocker/S17 race variant: all3photos sent beforeDone, one received44.478ms afterclose split into photo_only segment, leavingdraft2photos. Local fixes on fix/day1-late-media-source: standalone verified source fallback and two-second Done answer grace with strictly timestamp-bounded, unique same-staff/conversation pre-answer reassociation. No migration/newwording. 328 targeted tests passed with disposable local PostgreSQL, no skips/failures (142.62s); Ruff/diff clean. Review/CI/merge/deploy/live retest required before closure. Draft110 unsaved/count0; owner asked to Cancel and verify. Other pending Day1 checks/final reset/roles/costcleanup unchanged.
# Agency rehearsal finding: 2026-11-06

- GLI-102 creation on deployed1ef: owner screenshot shows correct Apartment/SALE, Gulshan-e-Iqbal Block13,950sqft,2bed/2bath,85lakh,Photos2 and subsequent Saved/ACTIVE acknowledgement. The draft Note repeats `Gulshan block 13 mein 2 bed flat sale pe, owner direct`, and no separate Ownerdirect line is visible. Presentation defect confirmed; whether structured source_type was omitted is unverified. New-area reproduction; do not assume the same cause as prior DHA findings. Continue same-property customer/handoff rehearsal, then inspect source_type/description read-only. No code fix or full create-extraction pass claimed.
