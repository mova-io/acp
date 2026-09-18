import { isPdfStructuralRow, requiresPdfSourceEditing } from './pdfStructuralProposal.js'
// The pure model behind the master/detail Remediation inbox.
//
// Remediation is queue work: select an item, understand it, act, move to the next. This module
// turns a raw finding (from Remediate.buildHumanQueue / dbItemToUi) into the small set of facts a
// queue row and the detail pane need — the remediation LANE, the effort estimate, the resolved
// status, and the "what did ACP do / what must the reviewer do" phrasing — plus the one behaviour
// that makes the queue feel fast: pick the next unresolved item after an action.
//
// Kept pure and free of React so it is unit-testable and the component stays declarative. The lane
// taxonomy is the centre of the design, so it lives here as data, not scattered in JSX.

// ── Remediation lanes ─────────────────────────────────────────────────────────────────────────
// A finding sits in exactly one lane; the lane drives the row's colour rail, the primary action
// label, and the "what ACP did" line. Colours are the six-state remediation vocabulary.
// `attention` marks the lanes a reviewer must actively unblock — a blocked finding or a rejected AI
// fix handed back for a person. Only those keep a saturated coloured rail; the everyday lanes
// (review/apply/manual/recheck) get a neutral rail (railColorOf), so the queue is not a wall of
// amber/orange bars that all read as "urgent". `color` is unchanged — it still tints each lane's pill.
// `label` is the full lane name; `short` is the quiet remediation-state word a scannable queue row
// shows next to the compact WCAG pill (the row leads with the ISSUE, so the lane is demoted to quiet
// text, not a loud coloured pill on every row).
export const LANES = {
  review: {
    key: 'review', rail: 'green', color: '#1f9d6b', attention: false,
    label: 'Review automatic fix', short: 'Automatic fix', action: 'Approve fix',
    didLine: 'ACP fixed it — review the change',
  },
  apply: {
    key: 'apply', rail: 'blue', color: '#2f6fed', attention: false,
    label: 'Apply suggested fix', short: 'AI-drafted fix', action: 'Apply fix',
    didLine: 'ACP drafted a fix — apply or reject',
  },
  manual: {
    key: 'manual', rail: 'amber', color: '#c2871a', attention: false,
    label: 'Manual edit required', short: 'Manual edit', action: 'Open in Word',
    didLine: 'Needs a manual edit — guided steps provided',
  },
  recheck: {
    key: 'recheck', rail: 'gray', color: '#8a8f98', attention: false,
    label: 'Recheck needed', short: 'Recheck', action: 'Recheck',
    didLine: 'Edited — re-scan to confirm it passes',
  },
  blocked: {
    key: 'blocked', rail: 'red', color: '#c0553f', attention: true,
    label: 'Blocked', short: 'Blocked', action: 'Review block',
    didLine: 'Blocked — cannot be remediated as-is',
  },
  // W2 — the destination for a rejected AI fix. Rejecting an AI proposal used to just remove the
  // finding from the queue and bump a counter; now it lands here, a visible follow-up lane where a
  // person picks the work up by hand. Distinct from `manual` (which was manual from the start) so
  // the reviewer can see what they bounced back, and distinct from `blocked` (which is unfixable) —
  // this one is fixable, just not by the AI's rejected attempt.
  handoff: {
    key: 'handoff', rail: 'orange', color: '#b1622b', attention: true,
    label: 'Rejected — needs manual handling', short: 'Manual (rejected fix)', action: 'Mark as assigned',
    didLine: 'AI fix rejected — a person must handle this',
  },
}

export const LANE_ORDER = ['review', 'apply', 'manual', 'handoff', 'recheck', 'blocked']

// A proposal is not automatically an AI proposal. The backend also offers values derived from
// OCR, chart XML, link targets and structural detectors. Prefer the proposal's persisted source
// over the coarse `hasProposal` compatibility flag so the UI never credits a model for a value it
// did not produce. Legacy rows without source metadata retain the old behaviour.
const NON_MODEL_SOURCE = /\b(deterministic|ocr|chart data|link target|floating text|heuristic|speech recognition|no model)\b/i
const MODEL_SOURCE = /\b(ai|model|ollama|claude|anthropic|openai|gemini|bedrock|hugging\s*face|qwen|llama|vision)\b/i
export function isAiAssistedDraft(f) {
  if (isPdfStructuralRow(f)) return false
  if (!f?.hasProposal) return false
  const sources = [f.proposalSource, ...(Array.isArray(f.proposals) ? f.proposals.map((p) => p?.source) : [])]
    .map((source) => String(source || '').trim()).filter(Boolean)
  if (!sources.length) return true
  if (sources.some((source) => MODEL_SOURCE.test(source) && !NON_MODEL_SOURCE.test(source))) return true
  return !sources.every((source) => NON_MODEL_SOURCE.test(source))
}

// The colour of a row's 4px lane rail. Attention lanes (blocked, rejected-handoff) keep their
// saturated colour so they stand out; everything else gets a neutral rail. This is what "reserve
// orange for items that genuinely require attention" comes down to in the queue.
export const NEUTRAL_RAIL = 'var(--rail-neutral, #d8d3dd)'
export function railColorOf(lane) {
  return lane?.attention ? lane.color : NEUTRAL_RAIL
}

export const optionalInspectionOf = f => f?.inspectionOnly === true || f?._raw?.inspection_only === true || (f?.rule_id || f?.ruleId) === 'auto/verify'
export function recordedReviewDecision(f, decisions = {}) {
  const d = decisions[f?.id] ?? decisions[f?.file]
  if (d?.state === 'not_applicable' || f?.resolution === 'out_of_scope' || f?._raw?.resolution === 'out_of_scope') return 'not_applicable'
  if (d?.state === 'deferred' || ['skipped', 'deferred'].includes(String(f?.status || '').toLowerCase())) return 'deferred'
  return null
}

const RESOLVED_STATUSES = new Set(['approved', 'applied', 'accepted', 'rejected', 'resolved', 'verified'])

// ── Target replaced by a verified fix (contract C1/C3) ───────────────────────────────────────────
// The backend marks a row `superseded_reason: 'target_removed_by_verified_fix'` when the object the
// finding described no longer exists in the CURRENT corrected copy because another, verified change
// replaced it (production: the 1.4.5 OCR text replacement removed the only body image, so the pending
// 1.1.1 alt-text proposal for that image has nothing left to describe). This is a terminal RESULT, not
// a stale suggestion to refresh and not a request for a decision: the row's own status is kept for the
// audit trail (pending stays pending), so every classifier below must read this marker first.
export const TARGET_REMOVED_REASON = 'target_removed_by_verified_fix'
export function isTargetReplaced(f) {
  return (f?.superseded_reason ?? f?._raw?.superseded_reason) === TARGET_REMOVED_REASON
}
/** The server's evidence for a target-replaced row, or null. */
export function targetReplacementOf(f) {
  if (!isTargetReplaced(f)) return null
  const evidence = f?.superseded_evidence ?? f?._raw?.superseded_evidence
  return evidence && typeof evidence === 'object' ? evidence : {}
}

// A writer, retry or eligibility job ACP has admitted and is still running. Only the exact-scope
// projection (automaticReviewQueue → `automaticDisposition`) or an admitted queue entry can claim it.
export const ACTIVE_AUTOMATIC_STATES = ['queued', 'processing', 'checking', 'applying', 'verifying']
export function activeAutomaticStateOf(f) {
  const state = f?.automaticDisposition?.state
  if (ACTIVE_AUTOMATIC_STATES.includes(state)) return state
  return f?.automaticQueued === true ? 'queued' : null
}

/** The lane for a finding, from its status first (blocked/recheck win) then its remediation shape. */
export function laneOf(f) {
  const st = String(f?.status || '').toLowerCase()
  if (optionalInspectionOf(f)) return LANES.review
  if (st === 'blocked') return LANES.blocked
  // A rejection recorded on the row itself (hitl_queue.status), read back on a later load. It is
  // the same outcome as the in-session `rejectedFix` handoff below and gets the same lane — the
  // AI's fix was declined and a person owns it now. NOT `blocked`, which claims the finding
  // cannot be remediated at all; the reviewer declined one attempt, they did not condemn the
  // finding, and the row would have read "Blocked — cannot be remediated as-is" on every reload.
  if (st === 'rejected') return LANES.handoff
  if (st === 'recheck' || st === 'rechecking' || st === 'scanning') return LANES.recheck
  // A rejected AI fix routed back for human handling (the handoff lane). Wins over the
  // remediation-shape checks below — the AI's proposal was declined, so it is no longer offered.
  if (f?.rejectedFix) return LANES.handoff
  if (requiresPdfSourceEditing(f) && !f?.autoApplied && !f?.applied) return {...LANES.manual, didLine: 'PDF tagging requires editing the source document or a PDF accessibility editor'}
  // A deterministic fix ACP already wrote: the reviewer confirms it (the green lane).
  if (f?.autoApplied || f?.applied || f?.rec?.action === 'auto') return LANES.review
  // ACP drafted a value for a person to approve (the blue lane).
  if (f?.hasProposal || (f?.after != null && f?.after !== '')) return isPdfStructuralRow(f)
    ? {...LANES.apply, short:'PDF tag change', didLine:'ACP proposed a source-anchored PDF tag change'} : LANES.apply
  // Nothing ACP can safely write: a person re-authors it in the source app (the amber lane).
  return LANES.manual
}

/** Estimated reviewer effort, in seconds, for a finding — driven by its lane. A review of a
 *  deterministic fix is quick; a manual re-author is the slow one. `f.effortSec` overrides. */
export function effortSecOf(f) {
  if (Number.isFinite(f?.effortSec)) return f.effortSec
  switch (laneOf(f).key) {
    case 'review': return 5
    case 'apply': return 15
    case 'recheck': return 10
    case 'blocked': return 0
    case 'handoff': return 120 // rejected AI fix → a person re-authors it, same cost as manual
    default: return 120 // manual
  }
}

/** A short human effort label, e.g. "~5 sec" or "~2 min". */
export function effortLabel(f) {
  const s = effortSecOf(f)
  if (s <= 0) return '—'
  if (s < 90) return `~${s} sec`
  return `~${Math.round(s / 60)} min`
}

/** Has this finding been acted on? Resolved rows lose their unread emphasis and drop out of the
 *  "next unresolved" walk. A finding is resolved by an explicit status or a recorded decision. */
export function isResolved(f, decisions = {}) {
  if (isTargetReplaced(f) || optionalInspectionOf(f) || recordedReviewDecision(f, decisions)) return true
  if (RESOLVED_STATUSES.has(String(f?.status || '').toLowerCase())) return true
  const d = decisions[f?.id] ?? decisions[f?.file]
  return !!(d && (d.state === 'accepted' || d.state === 'approved' || d.state === 'rejected' || d.state === 'not_applicable'))
}

// Only a finding-specific decision can grant approval; changed bindings need recheck.
function ownApprovalDecision(f, decisions = {}) {
  const byId = f?.id != null ? decisions[f.id] : undefined
  if (byId) return byId
  const byFile = f?.file ? decisions[f.file] : undefined
  const named = byFile?.id ?? byFile?.findingId ?? byFile?.finding_id
  return named != null && f?.id != null && String(named) === String(f.id) ? byFile : null
}

function recordedApprovalState(f, decisions = {}) {
  // Refusal reads both maps — see ownApprovalDecision.
  const blocking = decisions[f?.id] ?? decisions[f?.file]
  if (f?.rejectedFix || ['assigned', 'deferred', 'rejected', 'not_applicable'].includes(blocking?.state)) return 'none'
  const own = ownApprovalDecision(f, decisions)
  const st = String(f?.status || '').toLowerCase()
  if (!(['approved', 'applied', 'accepted'].includes(st) || ['approved', 'accepted'].includes(own?.state))) return 'none'
  // The approval stays on record for the audit trail, but the object it approved a change to was
  // removed by a verified fix. It is not stale (nothing to re-decide) and not an unconfirmed write
  // (the writer refuses it) — `superseded` here means "target gone", not "approval out of date".
  if (isTargetReplaced(f)) return 'current'
  if (f?.applied === true || f?.autoApplied === true || f?.validated || f?.verified === true) return 'current'
  const raw = (f && f._raw) || {}
  // The finding itself stopped being the work that was approved (a re-scan replaced the row, or a
  // shadowed/provenance check retired it). batchReviewSelection already calls this "Stale".
  if (raw.approval_recheck_required === true || f?.stale === true || f?.superseded === true || raw.superseded === true) return 'stale'
  const approvedRevision = raw.approved_source_revision ?? null
  const currentRevision = raw.source_revision ?? f?.source_revision ?? null
  if (approvedRevision != null && currentRevision != null
      && String(approvedRevision) !== String(currentRevision)) return 'stale'
  // Aligned, one slot per proposal, and a null slot means "no snapshot captured for this instance"
  // (legacy data) rather than a mismatch — so only a slot that WAS captured can disagree. A change
  // in length is a proposal added or withdrawn since the approval, which is itself a change.
  const approvedSnapshots = raw.approved_proposal_snapshot_ids
  const currentSnapshots = raw.proposal_snapshot_ids ?? f?.proposal_snapshot_ids
  if (Array.isArray(approvedSnapshots) && Array.isArray(currentSnapshots)) {
    if (approvedSnapshots.length !== currentSnapshots.length) return 'stale'
    if (approvedSnapshots.some((id, i) => id != null && String(id) !== String(currentSnapshots[i]))) return 'stale'
  }
  // Optimistic-concurrency version, when the caller recorded which one its decision was taken
  // against. Remediate's in-session approvals send `expectedVersion` to the server but do not keep
  // it on the local decision, so today this fires only for callers that do; the row-binding columns
  // above are what carry the production case.
  const decidedAt = own?.decisionVersion ?? own?.decision_version
  if (decidedAt != null && raw.decision_version != null
      && Number(decidedAt) !== Number(raw.decision_version)) return 'stale'
  return 'current'
}

export function approvalRecordedOn(f, decisions = {}) {
  return recordedApprovalState(f, decisions) === 'current'
}

export function approvalSuperseded(f, decisions = {}) {
  return recordedApprovalState(f, decisions) === 'stale'
}

/**
 * An approved change whose WRITE has not been confirmed — the production state behind scan
 * 6f07d85b39b8: `status='approved'`, `applied=null`, one proposal, and a writer that ran and
 * declined to write the value (it kept the approved value for retry).
 *
 * This is NOT human work. The approval exists, so there is no second approval to request, and the
 * queue must not ask for one. It is also NOT resolved: nothing was written, the corrected-copy
 * assessment still lists the criterion, and `workflowStatusOf` deliberately keeps reporting it as
 * blocked so the Blocked tab, the release blockers and the unresolved-work summary all still carry
 * it. What it needs is a RECOVERY of the write, which is what the detail pane offers.
 *
 * `applied === true` is excluded on purpose: a written-but-unverified fix is a verification
 * question, already handled by the awaiting-validation path.
 */
export function approvedWriteUnconfirmed(f, decisions = {}) {
  // Target removed by a verified fix: there is nothing to write and nothing to recover.
  if (isTargetReplaced(f)) return false
  // A writer or retry job is running: the write is in progress, which is ACP's Processing work,
  // not a recovery the reviewer should start a second time.
  if (activeAutomaticStateOf(f)) return false
  if (!approvalRecordedOn(f, decisions) || f?.validated || f?.verified === true) return false
  if (f?.applied === true || f?._raw?.applied === 1 || f?.autoApplied === true) return false
  if (['decorative', 'essential_exception', 'out_of_scope'].includes(f?.resolution)) return false
  return Boolean(f?.hasProposal || f?.proposals?.length || f?._raw?.proposals?.length)
}

/**
 * The automatic-approval marker's reason as it applies to a change that is already saved or approved:
 * a pre-approval "a person must judge this" reason (responsibility 'human') is stale by then and is
 * dropped; an ACP-side status reason (e.g. no active verification job) is kept. Null when none.
 */
export function postApprovalMarkerReason(f) {
  const marker = f?.automaticDisposition
  return marker?.reason && marker.responsibility !== 'human' ? marker.reason : null
}

/** The plain-language issue — the dominant text in a row. Strips the "DOCX · " format prefix that
 *  buildHumanQueue puts on the title, and falls back to the criterion name. */
export function issueLabel(f) {
  if (f?.plainIssue) return f.plainIssue
  const t = String(f?.title || '')
  const dot = t.indexOf(' · ')
  const tail = dot >= 0 ? t.slice(dot + 3) : t
  return tail || f?.rule || f?.rule_id || 'Accessibility finding'
}

/** The location line for a row: "Page N" when known, else any provided location, else "". */
export function locationLabel(f) {
  if (f?.page != null && f?.page !== '') return `Page ${f.page}`
  return f?.location || ''
}

/** The compact fact set a queue row renders. Deliberately small — the row communicates only these. */
export function rowModel(f, decisions = {}) {
  const lane = laneOf(f)
  const resolved = isResolved(f, decisions)
  return {
    id: f?.id,
    issue: optionalInspectionOf(f) ? 'Automatic changes recorded' : issueLabel(f),
    file: f?.file || '',
    location: locationLabel(f),
    sc: normSc(f?.rule_id ?? f?.ruleId ?? f?.wcag) || null, // the WCAG SC number, as a compact row pill
    did: isPdfStructuralRow(f) ? 'ACP proposed a source-anchored PDF tag change' : optionalInspectionOf(f) ? 'Saved automatic change' : lane.didLine,
    action: optionalInspectionOf(f) ? 'Browse saved changes' : lane.action,
    laneShort: isPdfStructuralRow(f) ? 'PDF tag change' : optionalInspectionOf(f) ? 'Inspection optional' : lane.short,   // the quiet remediation-state word (demoted from a loud coloured pill)
    severity: f?.severity || null,
    confidence: f?.confidence ?? null,
    effort: effortLabel(f),
    lane,
    resolved,
    unread: !resolved, // unread-style emphasis for not-yet-reviewed findings
  }
}

// ── Inbox top-bar tabs ──────────────────────────────────────────────────────────────────────────
// Status buckets the toolbar offers. A finding belongs to exactly one, derived from its lane +
// resolved state, so the tab counts always sum to the queue length.
export const TABS = ['all', 'auto-fixed', 'needs-attention', 'manual', 'blocked', 'resolved']

export function tabOf(f, decisions = {}) {
  if (isResolved(f, decisions)) return 'resolved'
  const k = laneOf(f).key
  if (k === 'blocked') return 'blocked'
  if (k === 'handoff') return 'needs-attention' // W2 — rejected AI fixes awaiting a person
  if (k === 'manual') return 'manual'
  return 'auto-fixed' // review + apply + recheck are all "ACP did something" work
}

export function matchesTab(f, tab, decisions = {}) {
  if (tab === 'all') return true
  return tabOf(f, decisions) === tab
}

export function tabCounts(list, decisions = {}) {
  const counts = { all: list.length, 'auto-fixed': 0, 'needs-attention': 0, manual: 0, blocked: 0, resolved: 0 }
  for (const f of list) counts[tabOf(f, decisions)] += 1
  return counts
}

// ── Workflow-status tabs (the top bar) ───────────────────────────────────────────────────────────
// The top tabs partition the queue by what the reviewer must DO next, into five stages that each
// carry a precise operational meaning (no vague "in progress" that could mean anything):
//
//   Needs review        — awaiting a human decision (an AI draft to approve, an auto-fix to confirm)
//   Manual fixes         — needs hand-editing in the source app (manual-from-start, a rejected AI
//                          fix handed back, or one a reviewer deferred/assigned to do by hand)
//   Awaiting validation  — a fix is IN but the confirming re-scan has not certified it yet
//   Blocked              — cannot be remediated as-is
//   Completed            — certified by the re-scan, rejected outright, or judged not-applicable
//
// The critical distinction (ADR 0016 / the verify-after-save UX): "Awaiting validation" is NOT
// "Completed" — the UI must never claim a fix is done before the re-scan earns it. And "resolved"
// for the progress line means REVIEWED (a decision is recorded), never conflated with a tab count,
// so a finding is never shown as both resolved AND awaiting validation.
//
// HONESTY (ADR 0016): every stage is derived from REAL state — the finding's status, its lane, and
// the recorded decision. "Manual fixes" absorbs the old "in progress" (assigned/deferred), which is
// still inferred only from a genuine decision, never an invented marker.
export const WORKFLOW_TABS = ['needs-review', 'manual', 'awaiting-validation', 'blocked', 'completed']
export const WORKFLOW_LABELS = {
  'needs-review': 'Approve AI suggestions', manual: 'Fix manually',
  'awaiting-validation': 'Awaiting verification', blocked: 'Blocked', completed: 'Results',
}

/** The pipeline stage a finding sits in, for the workflow top tabs. */
export function workflowStatusOf(f, decisions = {}) {
  // Target removed by a verified fix: a terminal result whatever the row's audit status says.
  if (isTargetReplaced(f)) return 'completed'
  if (optionalInspectionOf(f) || recordedReviewDecision(f, decisions)) return 'completed'
  const st = String(f?.status || '').toLowerCase()
  const d = decisions[f?.id] ?? decisions[f?.file]
  const lane = laneOf(f)
  if (['failed', 'apply_failed', 'verification_failed'].includes(st)) return 'blocked'
  if (st === 'verified' || st === 'resolved_verified' || f?.verified === true || (f?.validated && (f?.autoApplied || f?.applied || st === 'resolved'))) return 'completed'
  // Only automaticReviewQueue's exact-scope projection can supply this state.
  // A jobless approval is a recovery blocker, not ongoing processing.
  if (f?.automaticDisposition?.state === 'blocked') return 'blocked'
  if ((f?.autoApplied || f?.applied) && !['blocked', 'rejected', 'skipped'].includes(st)
      && !f?.rejectedFix && !['assigned', 'deferred', 'rejected', 'not_applicable'].includes(d?.state)) return 'awaiting-validation'

  // ── The decision recorded ON THE ROW (hitl_queue.status), which outlives this browser session.
  // `decisions` only holds what THIS session did, so without these branches a row the reviewer
  // approved yesterday came back from the server classified as outstanding work — or, once the
  // page stopped asking only for `pending` rows, did not come back at all. A decided row is
  // placed by its own durable state first; the session's `decisions` below still speak for the
  // rows this session acted on before the server has been re-read.
  if (st === 'approved' || st === 'applied' || st === 'accepted') {
    // Approval is not completion: the fix is written and the confirming re-scan has not
    // certified it yet (ADR 0016). `validated` is that re-scan's verdict.
    return f?.validated ? 'completed' : 'awaiting-validation'
  }
  // A recorded rejection ends this finding's AI work — the decision is the outcome, and there is
  // no re-scan to await. The in-session handoff row (rejectedFix, no durable status) keeps its
  // place in Manual fixes, which is where the person who bounced it back picks it up.
  if (st === 'rejected') return 'completed'
  if (lane.key === 'blocked') return 'blocked'
  // Completed: fully re-validated, a rejection that ended the work, or an out-of-scope (not
  // applicable) judgement — the last two are settled with no re-scan to await. not_applicable also
  // LEAVES the coverage denominator (accessibility_status.py) — do not re-count it elsewhere.
  if (st === 'verified') return 'completed'
  if (d && d.state === 'rejected') return 'completed'
  if (d && d.state === 'not_applicable') return 'completed'
  // A fix is IN and awaiting the re-scan that certifies it: an approved/accepted decision not yet
  // verified, or the recheck lane (edited, re-scanning). Kept distinct from Completed so the UI
  // never claims done before the re-scan confirms it.
  if (d && (d.state === 'accepted' || d.state === 'approved')) return 'awaiting-validation'
  if (lane.key === 'recheck') return 'awaiting-validation'
  // Needs hand-editing: a reviewer who deferred/assigned it, a manual-from-start finding, or a
  // rejected AI fix handed back for a person. An UNACKNOWLEDGED auto-applied fix is deliberately NOT
  // here and NOT in Awaiting validation — it still needs the reviewer to confirm it, so it falls
  // through to Needs review below. (This is the fix for the auto-fix double-count: an auto-fix used
  // to count as both awaiting-validation AND resolved.)
  if (d && (d.state === 'assigned' || d.state === 'deferred')) return 'manual'
  if (lane.key === 'manual' || lane.key === 'handoff') return 'manual'
  if (f?.automaticQueued === true) return 'awaiting-validation'
  // Awaiting a human decision: an AI draft to approve/reject, or an auto-fix to confirm.
  return 'needs-review'
}

export function matchesWorkflow(f, tab, decisions = {}) {
  if (tab === 'review') return ['needs-review', 'manual', 'blocked'].includes(workflowStatusOf(f, decisions))
  if (tab === 'active') return workflowStatusOf(f, decisions) !== 'completed'
  if (tab === 'all') return true
  return workflowStatusOf(f, decisions) === tab
}

export function workflowCounts(list, decisions = {}) {
  const counts = { all: list.length, 'needs-review': 0, manual: 0, 'awaiting-validation': 0, blocked: 0, completed: 0 }
  for (const f of list) counts[workflowStatusOf(f, decisions)] += 1
  return counts
}

// Which of the sticky footer's three loop steps — Show the problem (0) → Review the proposed change
// (1) → Verify the result (2) — a finding is currently ON, so the footer can light the live step and
// check off the ones behind it. Returns 3 when the finding is fully done (all three complete, none
// active). Derived from the same real state as workflowStatusOf, never an invented step marker.
export function workflowStepIndex(f, decisions = {}) {
  const status = workflowStatusOf(f, decisions)
  if (status === 'completed') return 3
  if (status === 'awaiting-validation') return 2           // a fix is in — verify it via the re-scan
  const lane = laneOf(f)
  if (lane.key === 'apply' || lane.key === 'review') return 1  // an AI proposal is waiting for review
  return 0                                                   // manual / fresh / blocked — show the problem
}

// ── Sorting ─────────────────────────────────────────────────────────────────────────────────────
const SEV_RANK = { CRITICAL: 0, SERIOUS: 1, MODERATE: 2, MINOR: 3 }

export const SORTS = ['priority', 'document', 'newest', 'fastest']

export function sortQueue(list, sort) {
  const a = [...list]
  switch (sort) {
    case 'document':
      return a.sort((x, y) => String(x.file).localeCompare(String(y.file)) || (x.id - y.id))
    case 'newest':
      return a.sort((x, y) => (y.discoveredAt || y.id || 0) - (x.discoveredAt || x.id || 0))
    case 'fastest':
      return a.sort((x, y) => effortSecOf(x) - effortSecOf(y) || (x.id - y.id))
    case 'priority':
    default:
      return a.sort((x, y) =>
        (SEV_RANK[x.severity] ?? 4) - (SEV_RANK[y.severity] ?? 4) ||
        LANE_ORDER.indexOf(laneOf(x).key) - LANE_ORDER.indexOf(laneOf(y).key) ||
        (x.id - y.id))
  }
}

/** Group findings under their document, preserving the incoming (already-sorted) order of first
 *  appearance. Returns [{ file, items }]. Group-by-document is the default view. */
export function groupByDocument(list) {
  const order = []
  const map = new Map()
  for (const f of list) {
    const key = f.file || '—'
    if (!map.has(key)) { map.set(key, []); order.push(key) }
    map.get(key).push(f)
  }
  return order.map((file) => ({ file, items: map.get(file) }))
}

// ── The behaviour that makes it feel fast: auto-advance ──────────────────────────────────────────
/** The next unresolved finding to select after acting on `currentId`. Walks the given (display)
 *  order from just after the current item, wraps once to the start, and returns the first
 *  unresolved id — or null when nothing is left, which is the "inbox zero" signal. */
export function nextUnresolvedId(orderedList, currentId, decisions = {}) {
  const ids = orderedList.map((f) => f.id)
  const start = ids.indexOf(currentId)
  const n = ids.length
  for (let step = 1; step <= n; step++) {
    const f = orderedList[(start + step) % n] // wraps; when start<0, begins at index 0
    if (f && f.id !== currentId && !isResolved(f, decisions)) return f.id
  }
  return null
}

/** Overall progress for the "0 of 6 resolved" header. */
export function progress(list, decisions = {}) {
  const resolved = list.filter((f) => isResolved(f, decisions)).length
  return { resolved, total: list.length }
}

// ── Terminal results and the one review-progress denominator (contract C3) ────────────────────────
const VERIFIED_STATUSES = new Set(['verified', 'resolved_verified'])

/**
 * What kind of recorded RESULT a row is, or null while it is still work.
 *   'target-replaced'  — the object was removed by another verified fix (server evidence)
 *   'inspection'       — saved automatic changes, optional inspection
 *   'decision'         — deferred / not applicable / otherwise closed by a recorded decision
 *   'rejected'         — the suggestion was rejected
 *   'verified'         — written and confirmed by a fresh assessment
 *   'saved-unverified' — sits in Results but has NO verification evidence. Listed as a result, never
 *                        counted as finished (reviewProgressOf puts it in awaitingOutcome).
 * Non-null exactly when workflowStatusOf answers 'completed'.
 */
export function resultKindOf(f, decisions = {}) {
  if (!f) return null
  if (isTargetReplaced(f)) return 'target-replaced'
  if (optionalInspectionOf(f)) return 'inspection'
  if (workflowStatusOf(f, decisions) !== 'completed') return null
  if (recordedReviewDecision(f, decisions)) return 'decision'
  const st = String(f?.status || '').toLowerCase()
  const d = decisions[f?.id] ?? decisions[f?.file]
  if (st === 'rejected' || d?.state === 'rejected') return 'rejected'
  if (VERIFIED_STATUSES.has(st) || f?.verified === true || f?.validated) return 'verified'
  if (f?.applied === true || f?.autoApplied === true || f?._raw?.applied === 1) return 'saved-unverified'
  return 'decision'
}

const FINISHED_KINDS = new Set(['verified', 'rejected', 'decision', 'inspection', 'target-replaced'])

const isAutoFixRow = (f) => typeof f?.id === 'string' && f.id.startsWith('af:')
const critOf = (f) => normSc(f?.rule_id ?? f?.ruleId ?? f?.wcag)
const strs = (...values) => values.flat().filter((v) => v != null && v !== '').map(String)

// The identities a HITL row is PROVEN to stand for: its own id, the finding ids it names, and the
// target locators its proposals / finding instances / supersession evidence point at.
function hitlIdentity(f) {
  const raw = f?._raw || {}
  const proposals = [...(Array.isArray(f?.proposals) ? f.proposals : []), ...(Array.isArray(raw.proposals) ? raw.proposals : [])]
  const evidence = f?.superseded_evidence ?? raw.superseded_evidence
  return {
    ids: new Set(strs(f?.id, raw.id)),
    findings: new Set(strs(f?.findingId, f?.finding_id, raw.finding_id, Array.isArray(raw.finding_ids) ? raw.finding_ids : [],
      Array.isArray(evidence?.finding_ids) ? evidence.finding_ids : [])),
    locators: new Set(strs(f?.locator, raw.locator, raw.instance_key, Array.isArray(raw.instance_keys) ? raw.instance_keys : [],
      proposals.map((p) => p?.locator), Array.isArray(evidence?.targets) ? evidence.targets : [])),
  }
}

/**
 * The review TASKS in a queue population: each object is counted once.
 *
 * An `af:` auto-fix row (applied-change evidence) is dropped only when it is a PROVEN duplicate of a
 * HITL row for the same file and criterion — the same stable item id, finding id or target locator.
 * Two af rows are collapsed only when they name the same file + criterion + target (locator, item or
 * finding). A distinct saved change is never dropped just because another row shares its file and
 * criterion, a HITL row is never dropped for an af row, and a repeated id keeps its first copy.
 */
export function dedupeReviewTasks(rows = []) {
  const list = Array.isArray(rows) ? rows : []
  const hitlByKey = new Map()
  for (const f of list) {
    if (!f || isAutoFixRow(f)) continue
    const key = `${f.file || ''}|${critOf(f)}`
    if (!hitlByKey.has(key)) hitlByKey.set(key, [])
    hitlByKey.get(key).push(hitlIdentity(f))
  }
  const seenIds = new Set()
  const seenAutoTargets = new Set()
  return list.filter((f) => {
    if (!f) return false
    if (f.id != null) {
      const id = String(f.id)
      if (seenIds.has(id)) return false
      seenIds.add(id)
    }
    if (!isAutoFixRow(f)) return true
    const key = `${f.file || ''}|${critOf(f)}`
    const item = strs(f.sourceItemId), finding = strs(f.findingId), locator = strs(f.targetLocator)
    if ((hitlByKey.get(key) || []).some((h) => item.some((v) => h.ids.has(v))
        || finding.some((v) => h.findings.has(v)) || locator.some((v) => h.locators.has(v)))) return false
    const targets = [...item.map((v) => `item:${v}`), ...finding.map((v) => `finding:${v}`), ...locator.map((v) => `locator:${v}`)]
      .map((t) => `${key}|${t}`)
    if (targets.some((t) => seenAutoTargets.has(t))) return false
    targets.forEach((t) => seenAutoTargets.add(t))
    return true
  })
}

/**
 * Review progress over ONE denominator. total = deduped tasks; decided = a decision is recorded or
 * the row is a recorded result; finished = a verified/closed result (a saved-but-unverified change is
 * NOT finished); awaitingOutcome = decided but not finished; open = total − decided.
 */
export function reviewProgressOf(rows = [], decisions = {}) {
  const tasks = dedupeReviewTasks(rows)
  let decided = 0, finished = 0
  for (const f of tasks) {
    const kind = resultKindOf(f, decisions)
    if (kind != null || isResolved(f, decisions)) decided += 1
    if (FINISHED_KINDS.has(kind)) finished += 1
  }
  return { total: tasks.length, decided, finished, awaitingOutcome: decided - finished, open: tasks.length - decided }
}

/** A "About N min remaining" label from a summed effort in seconds. Empty when nothing remains. */
export function remainingLabel(sec) {
  if (!(sec > 0)) return ''
  return sec < 60 ? `About ${sec} sec remaining` : `About ${Math.round(sec / 60)} min remaining`
}

/** Progress through ONE document's findings, for the persistent workspace bar. `file` is the
 *  document to report on (pass the selected finding's file). Returns resolved/total, a 0–100 percent,
 *  the remaining reviewer effort in seconds (unresolved findings only), and a human ETA label. When
 *  `file` is null/absent it reports the whole queue, so the bar has a sensible run-level fallback
 *  before anything is selected. */
export function docProgress(queue = [], file = null, decisions = {}) {
  const items = file == null ? queue : queue.filter((f) => (f?.file || '') === file)
  const total = items.length
  const resolved = items.filter((f) => isResolved(f, decisions)).length
  const remainingSec = items.reduce((s, f) => s + (isResolved(f, decisions) ? 0 : effortSecOf(f)), 0)
  return {
    file, resolved, total,
    pct: total ? Math.round((resolved / total) * 100) : 0,
    remainingSec,
    remainingLabel: remainingLabel(remainingSec),
    done: total > 0 && resolved >= total,
  }
}

// ── The auto-applied fixes, as green review-lane rows ────────────────────────────────────────────
/** Normalize a WCAG id ("SC_1_1_1", "WCAG_1.1.1", "1.1.1") to a bare "1.1.1". */
export function normSc(v) {
  return String(v || '').replace(/^(WCAG_?|SC_)/i, '').replace(/_/g, '.')
}

/** Turn ACP's auto-applied fixes (applied_fixes / remediation diffs) into green REVIEW-lane inbox
 *  rows, so review-of-auto-fixes shares the master/detail flow instead of living in a separate
 *  section. Each row is `autoApplied`, so laneOf() puts it in the green lane ("ACP fixed it — review
 *  the change" · Approve fix · ~5 sec). before/after come from the diff, falling back to the applied
 *  value. `nameOf(sc)` supplies the plain criterion name — injected so this file stays dependency-free
 *  (Remediate passes its ITEM_NAME lookup; a test passes a stub). Ids are namespaced `af:…` so they
 *  never collide with the human-queue's numeric/db ids. */
export function autoFixRows(fixes = [], nameOf = (sc) => sc, { aiApplicationRecords = false } = {}) {
  return fixes.map((a, i) => {
    const sc = normSc(a.sc ?? a.rule_id ?? a.wcag)
    const fmt = (String(a.file || '').split('.').pop() || 'DOC').toUpperCase()
    // Target identity, when the evidence names one — what dedupeReviewTasks proves a duplicate by.
    // A reviewer-approved write records "approved by a reviewer · <locator>" in its note.
    // The locator is everything after that exact prefix — it may contain spaces ("image 1", a Word
    // docPr name) — trimmed only at the ends; an empty suffix names no target.
    const noted = /^approved by a reviewer · ([\s\S]*)$/.exec(String(a.note || ''))?.[1]?.trim() || null
    const targetLocator = a.locator ?? a.target ?? a.instance_key ?? noted ?? null
    const sourceItemId = a.item_id ?? a.review_item_id ?? a.hitl_id ?? null
    const findingId = a.finding_id ?? null
    return {
      ...(targetLocator != null ? { targetLocator: String(targetLocator) } : {}),
      ...(sourceItemId != null ? { sourceItemId: String(sourceItemId) } : {}),
      ...(findingId != null ? { findingId: String(findingId) } : {}),
      id: `af:${a.file || ''}:${sc}:${i}`,
      file: a.file || '',
      page: a.page ?? null,
      ruleId: sc,
      rule_id: sc,
      plainIssue: nameOf(sc),
      title: `${fmt} · ${nameOf(sc)}`,
      before: a.before ?? null,
      after: a.after ?? a.value ?? a.approved_value ?? null,
      autoApplied: true,
      aiApplicationRecord: aiApplicationRecords,
      validated: a.verified === true || a.validated === true || a.status === 'resolved_verified' || a.disposition === 'resolved_verified',
      severity: null,
      effortSec: 5,
    }
  })
}
