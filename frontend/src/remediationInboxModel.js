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
  if (optionalInspectionOf(f) || recordedReviewDecision(f, decisions)) return true
  if (RESOLVED_STATUSES.has(String(f?.status || '').toLowerCase())) return true
  const d = decisions[f?.id] ?? decisions[f?.file]
  return !!(d && (d.state === 'accepted' || d.state === 'approved' || d.state === 'rejected' || d.state === 'not_applicable'))
}

/**
 * Whether an approval is ALREADY RECORDED for this row — on the row itself (hitl_queue.status,
 * which outlives the browser session) or in this session's decisions.
 *
 * An approval that is recorded is an approval that will never be asked for again: the exact same
 * proposal cannot be re-approved, and a screen that asks for one is asking for something the
 * backend would refuse. A rejection, a defer and an assignment are decisions, not approvals, and
 * are deliberately excluded — that work really is a person's.
 */
export function approvalRecordedOn(f, decisions = {}) {
  const d = decisions[f?.id] ?? decisions[f?.file]
  if (f?.rejectedFix || ['assigned', 'deferred', 'rejected', 'not_applicable'].includes(d?.state)) return false
  const st = String(f?.status || '').toLowerCase()
  if (['approved', 'applied', 'accepted'].includes(st)) return true
  return ['approved', 'accepted'].includes(d?.state)
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
  if (!approvalRecordedOn(f, decisions)) return false
  if (f?.validated || f?.verified === true) return false
  if (f?.automaticQueued === true) return false
  if (f?.applied === true || f?.autoApplied === true) return false
  return workflowStatusOf(f, decisions) === 'blocked'
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
    return {
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
