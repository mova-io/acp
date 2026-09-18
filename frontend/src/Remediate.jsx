import RemediationAutomationLayout from './RemediationAutomationLayout.jsx'
import ReportModeMenu from './ReportModeMenu.jsx'
import { verifySavedRemediation } from './verifySavedRemediation.js'
import { checkSelfRemediation } from './checkSelfRemediation.js'
import useAutomaticReleaseStatus from './useAutomaticReleaseStatus.js'
import AutomaticReleasePackage from './AutomaticReleasePackage.jsx'
import { remediationWorkRunning } from './remediationWorkRunning.js'
import useAcceptedRemediationIdentity from './useAcceptedRemediationIdentity.js'
import AcceptedRemediationPlanSummary from './AcceptedRemediationPlanSummary.jsx'
import { getAcceptedRemediationPlan } from './api.js'
import { automaticReviewQueue } from './automaticReviewQueue.js'
import useRunAiApproval from './useRunAiApproval.js'
import useReviewQueueRefresh from './useReviewQueueRefresh.js'
import ReviewRefreshNotice from './ReviewRefreshNotice.jsx'
import { authEpoch } from './apiIdentity.js'
import { assessMetrics } from './assessMetrics.js'
import { reviewableRemediationItems } from './remediationReviewAvailability.js'
import RemediationLiveDocuments from './RemediationLiveDocuments.jsx'
import RemediationReleasePlan from './RemediationReleasePlan.jsx'
import { authorizeAcceptedRelease } from './releasePlanIntent.js'
import { remediationReviewCounts, remediationDiffPage } from './remediationCountSummary.js'
import { selectionFingerprint } from './batchReviewSelection.js'
import AssessSummary from './AssessSummary.jsx'
import { useState, useEffect, useMemo, useRef } from 'react'
import AssessmentScopeCard from './AssessmentScopeCard.jsx'
import { Bars } from './charts.jsx'
import ReviewDrawer from './ReviewDrawer.jsx'
import RemediationInbox from './RemediationInbox.jsx'
import RemediationRunHeader from './RemediationRunHeader.jsx'
// The approved-board core (R2/R3, R5, R6, R9, R11, R12). Every one of these shipped to main
// unmounted; this is the pass that puts them on the screen they were written for.
import RemediationWork from './RemediationWork.jsx'
import ManualWork from './ManualWork.jsx'
import RemediationVerify from './RemediationVerify.jsx'
import DeliveryPanel from './DeliveryPanel.jsx'
import CloseoutPanel from './CloseoutPanel.jsx'
// The per-ITEM three, injected into the inbox's detail pane rather than mounted on the page:
// each answers a question about ONE finding or ONE document, and a page-level copy would have no
// subject. R8 is page-level because a failure list is a property of the run.
import DocumentAudit from './DocumentAudit.jsx'
import FindingComments from './FindingComments.jsx'
import ReviewDetails from './ReviewDetails.jsx'
import DueDate from './DueDate.jsx'
import UndoFix from './UndoFix.jsx'
import FixOutcomes from './FixOutcomes.jsx'
import { autoFixRows, matchesWorkflow, isAiAssistedDraft, dedupeReviewTasks } from './remediationInboxModel.js'
import { explainReviewPopulation, findingInputsFrom, QUEUE_TAB_KEY } from './reviewPopulationExplanation.js'
import RemainingFindingsExplainer from './RemainingFindingsExplainer.jsx'
import { OPEN_REVIEW_ITEM_EVENT, requestOpenReviewItem, takePendingReviewItem } from './openReviewItem.js'
import ReconcileReviewTargets from './ReconcileReviewTargets.jsx'
import { STAGE_LINEAGE_REFRESH_EVENT } from './useCanonicalStageLineage.js'
import FileDrawer, { SOURCE_URL } from './FileDrawer.jsx'
import SegmentDrawer from './SegmentDrawer.jsx'
import { SENIORITY_ORDER, REMEDIATION_ACTIONS } from './sim.js'
import { PRI_RANK } from './ontology.js'
import { remediateScan, getRemediationStatus, getRemediationExceptions, downloadRemediated, listAllHitl, updateHitlItem, assignHitlItem, suggestFix, getAppliedFixes, getScanRemediationDiffs, getHitlAnalytics, getScanAiCalls, openTraceUrl, getQueueEstimate, getReleaseStatus, getScan, verifySavedCopy, retryApprovedWrite } from './api.js'
import { stageExecutionNotice } from './stageExecutionNotice.js'
import { SIM, simProposalsFor } from './sim.js'
import { TraceChip } from './Transparency.jsx'
import QueuePanel from './QueuePanel.jsx'
import ProcessingStatusPanel from './ProcessingStatusPanel.jsx'
import RemediationOpsPanel, { RemediationActivityPanel } from './RemediationOpsPanel.jsx'
import RemediationWorkspaceTabs from './RemediationWorkspaceTabs.jsx'
import RemediationImpactCard from './RemediationImpactCard.jsx'
import { remediationImpactScope } from './remediationImpactScope.js'
import { createReviewEvidenceCache } from './reviewEvidenceCache.js'
import './remediation-prior-results.css'
import { deriveRemediateProcessingState } from './remediateProcessingState.js'
import { groupFixesByRule, summarizeImpact, totalFixes, scOf } from './fixSummary.js'
import { remediationWork, batchScope } from './remediationWork.js'
import { remediationResume } from './resumeInFlight.js'
import { firstProposed, firstBefore, firstThumb, firstKind, firstRationale, firstSource, pageOf,
         appliedFixAlt } from './reviewCard.js'
import ProposalThumb from './ProposalThumb.jsx'
import { remediableFiles, emptyScopeReason, scopeSummary, ineligibleReason,
         hasDocumentSelection, documentSelection, documentScopeSentence } from './remediableScope.js'
import { measuredReviewTime, REVIEW_TIME_BASIS } from './reviewerTime.js'
import { unreadableCaveat, reviewLeadLine } from './reviewQueueCopy.js'
import { canClaimLowRisk, unassessedRiskText } from './riskOverUnassessed.js'

// Steps 6-8: Automated Remediation + HITL + Re-validate. Owns the remediation plan
// (what to fix, prioritized, accept/reject/modify), the HITL queue, and self-remediation.
//
// The view is framed around the four decisions a compliance officer actually makes —
// Review → Approve → Verify → Publish — with the engine (worker queue, plan decisions,
// live metrics, business-risk graphs) folded behind an "Advanced" disclosure. Every count,
// confidence and risk statement traces to real pipeline data (applied-fix evidence, the
// live HITL queue, confidence.js, the recommendation model); nothing is fabricated.
const REM_ACTIONS = REMEDIATION_ACTIONS
const SR_COLOR = { Executive: 'var(--info-fg)', Director: '#D85A30', Manager: '#BF8C00', Staff: '#9a948f' }
const exposureOf = (f) => (f.tags || []).includes('public-facing') ? 'public-facing' : (f.tags || []).includes('high-traffic') ? 'high-traffic' : 'internal'
const EXP_COLOR = { 'public-facing': 'var(--info-fg)', 'high-traffic': '#D85A30', internal: '#9a948f' }
const SR_W = { Executive: 3, Director: 2, Manager: 1, Staff: 0 }
const priority = (f) => (f.tags || []).filter((t) => t === 'public-facing' || t === 'high-traffic').length * 2 + (SR_W[f.seniority] || 0) + (f.issues || []).filter((i) => i.severity === 'CRITICAL').length * 2

const FIX_WCAG_LABELS = {
  SC_1_1_1: { label: 'alt-text generated', color: '#639922' },
  SC_1_3_2: { label: 'reading order fixed', color: '#157A56' },
  SC_2_4_2: { label: 'headings / titles tagged', color: '#378ADD' },
  SC_3_1_1: { label: 'language set', color: '#726BC6' },
  SC_1_3_1: { label: 'table headers', color: '#A56814' },
}
const ITEM_ICON = { '1.1.1': '▦', '1.2.1': '🎧', '1.2.2': '🎬', '1.2.5': '🎬', '1.3.1': '⊞', '1.3.2': '¶', '1.4.3': '◑', '2.4.2': '¶', '2.4.4': '↗', '3.1.1': '✦' }
const ITEM_NAME = { '1.1.1': 'non-text content', '1.2.1': 'audio-only & video-only', '1.2.2': 'captions', '1.2.5': 'audio description', '1.3.1': 'info & relationships', '1.3.2': 'meaningful sequence', '1.4.3': 'contrast minimum', '2.4.2': 'page titled', '2.4.4': 'link purpose', '3.1.1': 'language of page' }
const ITEM_BA = {
  '1.1.1': { meta: 'AI alt text — review accuracy', before: (d) => d || 'image / chart — no alt text' },
  '1.2.1': { meta: 'transcript draft — verify accuracy', before: () => 'audio — no transcript' },
  '1.2.2': { meta: 'ASR captions — review timing & accuracy', before: () => 'video — no caption track' },
  '1.2.5': { meta: 'audio description script — needs review', before: () => 'video — no audio description' },
  '1.3.1': { meta: 'table structure — human judgement needed', before: (d) => d || 'table without header row' },
  '1.3.2': { meta: 'two plausible reading orders', before: () => 'multi-column layout — reading order ambiguous' },
  '1.4.3': { meta: 'contrast fix needs design sign-off', before: (d) => d || 'text below 4.5:1 contrast ratio' },
  '2.4.4': { meta: 'link text — needs human rewrite', before: (d) => d || 'non-descriptive link text ("click here")' },
}
const SEV_RANK = { CRITICAL: 0, SERIOUS: 1, MODERATE: 2, MINOR: 3 }
// SCs the local Ollama model can draft a concrete replacement value for (api/ai.py
// _SUGGEST_KIND). The drawer shows these as an editable AI draft instead of a static
// canned template, and persists whatever the reviewer approves (approved_value).
const AI_DRAFTABLE_SCS = new Set(['1.1.1', '2.4.4', '2.4.9'])

// A pool-capacity response can arrive after the decision's transaction began. When the server
// explicitly says `changes: unknown`, neither success nor failure is safe to infer from the PUT.
// Re-read the durable queue (all statuses, not only pending) and settle from the row itself.
export async function reconcileHitlPutFailure(itemId, wanted, err, readQueue = listAllHitl) {
  if (err?.code !== 'DB_CAPACITY_BUSY' || err?.changes !== 'unknown') {
    return { outcome: 'not_saved', error: err }
  }
  try {
    const rows = await readQueue()
    const row = (rows || []).find((candidate) => String(candidate.id) === String(itemId))
    const expected = typeof wanted === 'string' ? { status: wanted } : wanted
    const sameValues = expected.approvedValues == null || expected.approvedValues.every((value, i) => {
      const actual = (row?.proposals || row?.evidence || [])[i]?.approved_value
      return String(actual || '').trim() === String(value || '').trim()
    })
    const matches = (!expected.requestId || row?.last_decision_request_id === expected.requestId)
      && row?.status === expected.status
      && String(row?.reviewer_note || '') === String(expected.reviewerNote || '')
      && String(row?.approved_value || '') === String(expected.approvedValue || '')
      && String(row?.resolution || '') === String(expected.resolution || '')
      && sameValues
    if (matches) return { outcome: 'saved', row }
    if (expected.requestId && row?.status === expected.status) return { outcome: 'unknown', error: err }
    if (row) return { outcome: 'not_saved', row, error: err }
  } catch { /* the reconciliation read failed too; preserve uncertainty below */ }
  return { outcome: 'unknown', error: err }
}

export function hitlFailureCopy(item, kind, err, outcome = 'not_saved') {
  const action = kind === 'deferred' ? 'skip' : kind
  const file = item?.file || 'this finding'
  if (outcome === 'unknown') {
    return `ACP could not confirm whether your ${action} of “${file}” was saved because database capacity was exhausted. `
      + 'The card is showing its last known state — refresh the queue before trying again.'
  }
  return `Your ${action} of “${file}” was NOT saved: ${err?.message || err}. It is back in the queue — try again.`
}

// The remediation endpoint deduplicates an identical effective file set against its sealed
// assessment input and decision digest. That makes retrying the SAME selection safe, but a 503
// whose `changes` are unknown still cannot be described as "not enqueued": the response may have
// been lost after the durable batch was created. Keep that distinction visible to the operator.
export function remediationSubmissionFailureCopy(err) {
  if (err?.code === 'DB_CAPACITY_BUSY' && err?.changes === 'unknown') {
    return 'ACP could not confirm whether this remediation request was accepted because database capacity was exhausted. '
      + 'Wait for capacity to recover, then retry the same selection; ACP will reuse any matching work already queued.'
  }
  if (err?.code === 'DB_CAPACITY_BUSY' && err?.changes === 'none') {
    return 'Remediation was not submitted because database capacity is currently exhausted. Wait for capacity to recover, then try again.'
  }
  return `Could not enqueue: ${err?.message || err}`
}

// The decision recorded on a hitl_queue row, in the inbox model's vocabulary. 'pending' becomes
// undefined so a queued row is still classified by its remediation shape, exactly as before;
// every other status is a durable decision the inbox must place (remediationInboxModel's
// workflowStatusOf), not a row it may quietly drop.
export function uiStatusOf(row) {
  const status = String(row?.status || '').toLowerCase()
  return !status || status === 'pending' ? undefined : status
}

// Rows keyed by id, first occurrence winning. The inbox is assembled from four sources that can
// legitimately hold the same finding at once — the pending queue, this session's rejected
// handoffs, the durable decided rows, and the applied-fix evidence — and a finding counted twice
// is the same class of defect as one counted not at all.
export function dedupeById(rows) {
  const seen = new Set()
  return rows.filter((row) => {
    const key = row?.id
    if (key == null) return true
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

export function dbItemToUi(it, files) {
  const sc = (it.rule_id || '').replace(/^(WCAG_?|SC_)/, '').replace(/_/g, '.')
  const ba = ITEM_BA[sc] || { meta: 'review AI proposal', before: (d) => d || 'issue found' }
  const fileRec = (files || []).find((f) => f.file === it.file) || {}
  const issue = ((fileRec.issues || []).find((i) => (i.wcag || '').replace(/^SC_/, '').replace(/_/g, '.') === sc)) || {}
  return {
    id: it.id,
    _raw: it, // Preserve server proposal lineage on every load, including completion refresh.
    icon: ITEM_ICON[sc] || '◈',
    title: `${((it.file || '').split('.').pop() || 'DOC').toUpperCase()} · ${it.rule_name || ITEM_NAME[sc] || sc}`,
    meta: ba.meta,
    file: it.file,
    scanId: it.scan_id,
    ruleId: it.rule_id,
    rule_id: it.rule_id,
    validated: !!it.validated,
    applied: it.applied === 1 || it.applied === true,
    // The reviewer's recorded decision. Without it a row that was approved, rejected or skipped
    // came back from the server indistinguishable from untouched work.
    status: uiStatusOf(it),
    resolution: it.resolution || null,
    inspectionOnly: it.inspection_only === true || it.rule_id === 'auto/verify',
    autoApplied: it.rule_id === 'auto/verify',
    aiDraftable: AI_DRAFTABLE_SCS.has(sc),
    source: fileRec.sourceName,
    rule: `WCAG ${sc}${it.rule_name ? ' — ' + it.rule_name : ITEM_NAME[sc] ? ' — ' + ITEM_NAME[sc] : ''}`,
    // The passage the fix would replace. The proposal's own `before` is the literal offending
    // text the analyser matched (`propose_language_parts` carries the actual sentence); fall
    // back to the rule's description of the defect when no proposal names one.
    before: firstBefore(it) || ba.before(issue.detail, fileRec),
    beforeLiteral: !!firstBefore(it),
    // The page the finding sits on, per the analysers (hitl_queue.page). Null when they could
    // not attribute one — the card must then not claim a page.
    page: pageOf(it),
    // The AI's actual draft, from hitl_queue.proposals — what the vision model wrote for THIS
    // image. `approved_value` only ever holds what a REVIEWER typed back (nothing writes it
    // server-side), so the old `it.approved_value || template` fell through to the template on
    // every item: "AI-generated alt text added — confirm or reword" rendered identically for
    // every image in every document, whether or not a model had said anything. There is no
    // template fallback now: null means nothing was drafted, and the card says exactly that.
    after: firstProposed(it) || it.approved_value || null,
    // Carried through so the card can build its current→remediated comparison at RENDER time.
    // The remediation_diff rows load in parallel with this queue, so a comparison baked in
    // here would be empty on first paint and never refresh.
    proposals: it.proposals,
    // The offending image itself + why the model said what it said. Null when no proposal.
    thumb: firstThumb(it),
    thumbKind: firstKind(it),
    rationale: firstRationale(it),
    proposalSource: firstSource(it),
    // Distinguishes a real model draft from the canned fallback, so the UI never labels a
    // template as a suggestion the AI made.
    hasProposal: !!firstProposed(it),
    // The finding's severity (from the matched issue), carried so the collapsed inbox row can show
    // it and search can match on it. Null when the file record has no matching issue.
    severity: issue.severity || null,
  }
}

function buildHumanQueue(files, triage = {}) {
  const hasInscope = hasDocumentSelection(triage)
  const active = files.filter((f) => !(f.remediated_at || f.drive_write_url))  // exclude already-fixed
  const candidates = hasInscope ? active.filter((f) => triage[f.file] === 'inscope') : active
  const assisted = candidates.filter((f) => (f.rec?.action === 'assisted' || f.rec?.action === 'review') && (f.issues || []).length > 0)
  // fallback: any inscope/active file with issues that isn't fully auto-fixable
  const fallback = candidates.filter((f) => (f.issues || []).length > 0 && f.rec?.action !== 'auto' && !assisted.find((a) => a.file === f.file))
  const pool = assisted.length >= 3 ? assisted : [...assisted, ...fallback].slice(0, 8)
  return pool.slice(0, 8).map((f, idx) => {
    const issue = [...(f.issues || [])].sort((a, b) => (SEV_RANK[a.severity] || 3) - (SEV_RANK[b.severity] || 3))[0]
    if (!issue) return null
    const sc = (issue.wcag || '').replace(/^SC_/, '').replace(/_/g, '.')
    const ba = ITEM_BA[sc] || { meta: 'review AI proposal', before: (d) => d || 'issue found' }
    // The demo's stand-in for hitl_queue.proposals. Present only for the criteria the model
    // drafts a value for; the rest either carry an applied remediation_diff (simRemediationDiffs,
    // once the demo has run remediation) or are a judgement call with nothing to show. No card
    // gets a canned "AI fix applied" string — EvidenceCard shows a value only when one exists.
    const proposals = simProposalsFor(sc, f.file)
    const p = proposals?.[0]
    return {
      id: idx + 1,
      icon: ITEM_ICON[sc] || '◈',
      title: `${(f.file.split('.').pop() || 'DOC').toUpperCase()} · ${issue.detail || ITEM_NAME[sc] || sc}`,
      meta: ba.meta,
      file: f.file,
      ruleId: sc,
      // buildEvidenceCard reads the hitl_queue column name, `rule_id`. The SIM item carried only
      // the camelCase `ruleId`, so every demo card fell back to card.wcag = '—' and showed no
      // criterion at all — the one number a reviewer needs to know what they are being asked.
      rule_id: sc,
      aiDraftable: AI_DRAFTABLE_SCS.has(sc),
      source: f.sourceName,
      rule: `WCAG ${sc}${ITEM_NAME[sc] ? ' — ' + ITEM_NAME[sc] : ''}`,
      before: ba.before(issue.detail, f),
      after: p?.proposed_value ?? null,
      proposals,
      thumb: p?.thumb ?? null,
      rationale: p?.rationale ?? null,
      proposalSource: p?.source ?? null,
      hasProposal: !!p,
    }
  }).filter(Boolean)
}

// REMOVED 2026-08-19 — the ProgressRail (redesign spec R4 item 1: "drop the ProgressRail as a
// persistent element").
//
// It rendered Scan › Assess › Remediate › Review queue › Verify › Publish across the top of the
// page. The spec's complaint was four competing navigation systems; this was one of them, and by
// now it was the LAST redundant one — every state it showed is said better, and closer to the
// work, somewhere else on the page:
//
//   Scan, Assess      hard-coded 'done'. Constants, not state — decorative by construction.
//   Remediate         the hero line ("N documents processed · N issues fixed automatically").
//   Review queue      the section's own sentence and progress bar. #272/#273 deduplicated the
//                     repeated `N` badges down to one dominant statement; the rail was a fourth.
//   Verify            the Verification RemSection, whose <VerifyState> carries state, percentage,
//                     remaining and ready — strictly more than 'done' | 'active' | 'pending'.
//   Publish           the hero's primary CTA becomes "Publish Certified Copy →", and Publish is
//                     a top-level tab of its own.
//
// What made this safe to delete NOW rather than when the spec was written is that the contextual
// status the spec asked for in its place has since been built: #366's workflow tablist inside
// RemediationInbox, and #370's footer lighting each finding's live step. Deleting the rail before
// those existed would have removed a wayfinder and put nothing there.
//
// Guarded by remediateNavigation.test.jsx, which asserts the rail is gone AND that each of those
// replacements is still on the page — deleting a duplicate is only correct while the original
// survives.

// Recent AI fixes — a GROUPED summary (§6), not a repetitive per-row list. One chip per
// rule ("Added 14 image descriptions"), an "Accessibility improvements" impact row (§7),
// and "View details" expands to the real applied values + thumbnails (applied_fixes).
// Everything is a straight count of what was written.
function GroupedFixes({ fixGroups, appliedFixes = [], impact }) {
  const [showDetail, setShowDetail] = useState(false)
  if (!fixGroups.length) return null
  return (
    <details className="panel rem-sec">
      <summary className="rem-sec-sum">
        <h2 className="rem-sec-title">Recent AI fixes <span className="muted" style={{ fontSize: 13, fontWeight: 400 }}>· what was corrected automatically</span></h2>
        <span className="reviewpill">{fixGroups.length}</span>
      </summary>
      <div className="rem-sec-body">
        <div className="fixgroups">
          {fixGroups.map((g) => <span className="fixgroup" key={g.sc}><span aria-hidden="true">✓</span> {g.phrase}</span>)}
          {appliedFixes.length > 0 && (
            <button className="linkbtn" onClick={() => setShowDetail((v) => !v)}>{showDetail ? 'Hide details' : 'View details →'}</button>
          )}
        </div>
        {impact && impact.length > 0 && (
          <div className="impactrow" aria-label="Accessibility improvements by category">
            <span className="impacthd muted">Accessibility improvements</span>
            {impact.map((c) => (
              <span className="impacttile" key={c.category}><b>{c.count}</b> <span className="muted">{c.category}</span></span>
            ))}
          </div>
        )}
        {showDetail && appliedFixes.length > 0 && (
          <div className="recentfixes" style={{ marginTop: 12 }}>
            {appliedFixes.slice(0, 12).map((a, i) => {
              const sc = scOf(a.rule_id)
              return (
                <details className="recentfix" key={i}>
                  <summary>
                    {/* Through ProposalThumb, not a raw <img>: it is the one place that checks a
                        thumb is really a base64 image data-URL before it reaches an `src`, and it
                        letterboxes rather than cover-cropping — a PDF's thumb is a whole rendered
                        page, which a cover-crop would reduce to a slice of margin. */}
                    <ProposalThumb thumb={a.thumb} size={36} alt={appliedFixAlt(a.file)}
                                   className="recentfix-thumb" />
                    <span className="fmtchip">{((a.file || '').split('.').pop() || 'DOC').toUpperCase()}</span>
                    <span className="muted" style={{ fontSize: 12 }}>WCAG {sc} · {ITEM_NAME[sc] || 'non-text content'} · {a.file}</span>
                    <span className="fixauto" style={{ marginLeft: 'auto', fontSize: 12 }}>⚡ auto-applied</span>
                  </summary>
                  <div className="diffbox after"><span className="difftag">applied</span>{a.value}{a.source ? <span className="muted" style={{ fontSize: 11, marginLeft: 6 }}>· {a.source}</span> : null}</div>
                </details>
              )
            })}
          </div>
        )}
      </div>
    </details>
  )
}

// Verification state (§8) — real, tied to the re-scan/job state; never "0 → 0".
// A remediation section that collapses. Remediate stacks eight panels, and an operator working
// the queue reads one of them — the rest are reference they scroll past every time.
//
// TWO RULES, both learned from the panels this replaces:
//
//   * The SUMMARY carries the number. A collapsed section that says only "Deferred" hides the one
//     fact you need to decide whether to open it; the count has to survive the collapse or the
//     control just costs a click.
//   * `defaultOpen` is DERIVED from content, never a constant. A section with nothing in it opens
//     to disappointment, and a section with work in it should not need discovering. Callers pass
//     the same expression that decides whether to render at all.
//
// <details> rather than a button + state: it is natively keyboard-operable and announces its own
// expanded state, so this adds no focus handling and no aria-expanded to keep in sync — which is
// the kind of thing that rots silently on a product that certifies accessibility.
function RemSection({ id, title, count, hint, defaultOpen = false, children }) {
  return (
    <details className="panel rem-sec" id={id} open={defaultOpen}>
      <summary className="rem-sec-sum">
        <h2 className="rem-sec-title">{title}</h2>
        {count != null && <span className="reviewpill">{count}</span>}
        {hint && <span className="muted rem-sec-hint">{hint}</span>}
      </summary>
      <div className="rem-sec-body">{children}</div>
    </details>
  )
}


function VerifyState({ state, pct, remaining, ready, latest }) {
  if (state === 'running') return (
    <div className="verify-run" role="status" aria-live="polite">
      <div className="verify-track"><i style={{ width: `${pct}%` }} /></div>
      <span>Running… <b>{pct}%</b>{latest ? <> · last verified <span className="fname">{latest}</span></> : null}</span>
    </div>
  )
  if (state === 'waiting') return (
    <div className="verify-wait" role="status">⏳ Waiting on approval · <b>{remaining}</b> item{remaining === 1 ? '' : 's'} remaining. Verification begins automatically once you approve.</div>
  )
  if (state === 'complete') return (
    <div className="okline verify-done">✓ Verification complete · <b>{ready}</b> document{ready === 1 ? '' : 's'} re-validated and ready to publish.</div>
  )
  return <div className="muted">Not started — approve the review items or run remediation and verification begins automatically.</div>
}

// readOnly: time-travel replay — historical scans are for looking, not enqueuing
// real remediation jobs against (decisions stay editable: per-scan decision saves
// are the time-travel feature itself).
const reviewEvidence = createReviewEvidenceCache(getHitlAnalytics)

export default function Remediate({ run, files = [], decisions = {}, setDecisions, triage = {}, setTriage, assignees = {}, setAssignees, myEmail = null, aiEnabled = true, readOnly = false, resultsOnly = false, onRefresh, onHitlCount, onNavigate, cap = null, assessment = null, assessedAt = null,
                                   // The run's live state and its ONE stream, owned by
                                   // useRemediationRun at App level so both survive this
                                   // component being unmounted on every tab change.
                                   runStream = null, delivery = null, progressHostId = null,
                                   // The canonical remediate STAGE snapshot (App's lineage), whose
                                   // domain_reconciliation names the server's unresolved findings.
                                   // Optional: without it the explanation says findings are unknown
                                   // rather than implying there are none.
                                   remediationStage = null }) {
  const [queue, setQueue] = useState([])
  // The master/detail RemediationInbox owns its own view state (search, tabs, sort, selection),
  // so the old accordion/prefs plumbing (single-open openId, the search/severity/criterion/group
  // filters, and their sessionStorage rehydration) is gone with it.
  const [acted, setActed] = useState({ approved: 0, rejected: 0, deferred: 0 })
  const [deferredItems, setDeferredItems] = useState([])
  // The run's rows that already carry a decision (approved / rejected / skipped). Held beside the
  // pending queue rather than in it, so every count that means "still to do" (`queue.length`,
  // "N remaining", the nav badge) keeps its meaning, while the inbox and the run total can
  // account for the whole queue the server holds.
  const [decidedItems, setDecidedItems] = useState([])
  // W2 — a rejected AI fix is not a dead end. It becomes a manual-handling item that stays visible
  // in the inbox's "Needs manual handling" lane until a person picks it up, rather than vanishing
  // from the queue the moment it is rejected. Kept as its own state (like deferredItems) so it
  // survives the pending-queue reload below — the server drops it from `pending`, but the reviewer
  // still needs to see the work it handed back.
  const [rejectedItems, setRejectedItems] = useState([])
  // Real applied-fix evidence: scan-wide before→after (all fix types, verified-cleared) +
  // the concrete AI-written values/thumbnails. Every hero/impact/recent-fix count is a
  // straight count of these rows — never a fabricated number (see fixSummary.js).
  const [scanDiffs, setScanDiffs] = useState([])
  const [diffTotals, setDiffTotals] = useState(null)
  const [appliedFixes, setAppliedFixes] = useState([])
  const [reviewExceptions, setReviewExceptions] = useState(null)
  // Reviewer acknowledgements of the auto-applied (green) fixes shown in the inbox, keyed by their
  // `af:…` id. Local: an auto fix is already applied and re-scanned, so "Approve" is a confidence
  // check, not a re-application — it just marks the row resolved and advances to the next.
  const [ackd, setAckd] = useState({})
  // W6 — where each file's AI actually RAN, from the real per-call ledger (one fetch for the whole
  // scan, not one per card). Maps file → 'local' | 'cloud'. This is the ACTUAL zone the bytes were
  // processed in, not the configured provider — so a GPU→CPU fallback shows the truth on the card
  // instead of the config's intent. Any cloud call for a file wins (privacy-conservative); a file
  // with no AI call at all stays absent (deterministic fix — no badge, nothing to claim).
  const [aiZoneByFile, setAiZoneByFile] = useState({})
  // Earlier stages cannot restart work; current-run review decisions remain available.
  const reviewReadOnly = readOnly
  const reviewReadOnlyRef = useRef(reviewReadOnly)
  reviewReadOnlyRef.current = reviewReadOnly
  readOnly = readOnly || resultsOnly
  const resultsOnlyRef = useRef(resultsOnly)
  resultsOnlyRef.current = resultsOnly
  const runId = run?.id
  const verificationRunRef = useRef(runId)
  verificationRunRef.current = runId
  const [releasePlanIntent, setReleasePlanIntent] = useState(null)
  const [releasePlanNotice, setReleasePlanNotice] = useState('')
  const [automaticReleaseState, setAutomaticReleaseState] = useState(null)
  const releasePlanScan = useRef(runId)
  releasePlanScan.current = runId
  useEffect(() => { setReleasePlanIntent(null); setReleasePlanNotice('') }, [runId])
  useEffect(() => { setReviewExceptions(null) }, [runId])
  const fixRequest = useRef(0)
  const fetchFixes = () => {
    const request = ++fixRequest.current
    const epoch = authEpoch()
    if (!runId) { setScanDiffs([]); setDiffTotals(null); setAppliedFixes([]); setAiZoneByFile({}); return }
    Promise.all([getScanRemediationDiffs(runId, true), getAppliedFixes(runId), getScanAiCalls(runId),
      getRemediationExceptions(runId).then(exceptions => {
        if (request === fixRequest.current && authEpoch() === epoch) setReviewExceptions(exceptions)
      }).catch(() => {
        if (request === fixRequest.current && authEpoch() === epoch) setActError('Review exceptions could not be loaded. Some review items may be unavailable.')
      })])
      .then(([d, a, calls]) => {
        if (request !== fixRequest.current || authEpoch() !== epoch) return
        const page = remediationDiffPage(d)
        setScanDiffs(page.items); setDiffTotals(page); setAppliedFixes(Array.isArray(a) ? a : [])
        const byFile = {}
        ;(Array.isArray(calls) ? calls : []).forEach((c) => {
          if (!c || !c.file) return
          if (byFile[c.file] === 'cloud') return
          byFile[c.file] = c.zone === 'local' ? (byFile[c.file] || 'local') : 'cloud'
        })
        setAiZoneByFile(byFile)
      })
      .catch(() => {})
  }
  // ONE read of the run's durable review queue, split into the work still outstanding and the
  // decisions already recorded. Both halves are kept.
  //
  // This used to ask for `status=pending` only, so a row the reviewer had approved, rejected or
  // skipped never reached the page: the run total shrank by one on every decision (a run whose
  // logs named 13 review items showed 12), and the Completed tab — where an approved AI
  // suggestion is meant to be tracked — could only ever show what THIS browser session had
  // decided, so it was empty after a reload. Superseded rows are still dropped by the server,
  // deliberately: a finding re-verified as passing has stopped being work.
  const applyHitlRows = (items) => {
    const rows = (items || []).map((it) => ({ ...dbItemToUi(it, files), _raw: it }))
    const served = new Set(rows.map((row) => row.id))
    setQueue(rows.filter((row) => !row.status))
    // A decision taken in this browser is kept until the server's own row for it arrives. The
    // read is triggered by the same event that announces the decision and can therefore outrun
    // its write — api.js deliberately suppresses a row whose PUT is still in flight — so
    // dropping it here would take the item out of both lists and dip the run total by one.
    setDecidedItems((previous) => [...rows.filter((row) => row.status),
                                   ...previous.filter((row) => !served.has(row.id))])
    return items || []
  }
  const recordDecided = (item, status, resolution = null) => {
    if (!item?.id) return
    setDecidedItems((d) => (d.some((x) => x.id === item.id)
      ? d : [...d, { ...item, status, resolution, validated: false }]))
  }
  useEffect(() => {
    setActed({ approved: 0, rejected: 0, deferred: 0 }); setDeferredItems([]); setRejectedItems([]); setAckd({}); setDecidedItems([])
    clearInterval(pollRef.current); setRemProg(null); setRemBusy(false); setServerFixed(0); setRemMsg(''); setDiffTotals(null); setScanDiffs([]); setAppliedFixes([])
    fetchFixes()
    if (!runId) { setQueue(SIM ? buildHumanQueue(files, {}) : []); return }
    if (SIM) { setQueue(buildHumanQueue(files, {})); return }
    // The scoped queue reader below loads recorded rows; opening Plan never approves work.
    setQueue([])
  }, [runId]) // eslint-disable-line react-hooks/exhaustive-deps

  // Derive fix-type breakdown from auto-action files in the corpus
  const fixTypesDisplay = useMemo(() => {
    const counts = {}
    files.filter((f) => f.rec?.action === 'auto').forEach((f) => (f.issues || []).forEach((i) => {
      const m = FIX_WCAG_LABELS[i.wcag]; if (m) counts[m.label] = (counts[m.label] || { value: 0, color: m.color, order: Object.keys(FIX_WCAG_LABELS).indexOf(i.wcag) })
      if (m) counts[m.label].value++
    }))
    // No fabricated fallback: an estate with zero auto-fixable files shows an
    // honest 0 and hides the breakdown, never invented counts.
    return Object.entries(counts).map(([label, { value, color }]) => ({ label, value, color })).sort((a, b) => b.value - a.value).slice(0, 5)
  }, [files])
  const autoFixed = fixTypesDisplay.reduce((a, f) => a + f.value, 0)
  const [selItem, setSelItem] = useState(null)
  const [self, setSelf] = useState([])
  const [sel, setSel] = useState(null)
  const [seg, setSeg] = useState(null)
  const [remBusy, setRemBusy] = useState(false)
  const [remMsg, setRemMsg] = useState('')
  const [workspaceRequest, setWorkspaceRequest] = useState(null)
  const [releaseAnswered, setReleaseAnswered] = useState(false)
  const [planRevision, setPlanRevision] = useState(0)
  const [remProg, setRemProg] = useState(null)   // authoritative SSE status + client-known batch total
  const [remUpdates, setRemUpdates] = useState('idle') // live | polling | idle
  // The server-owned run snapshot (api/remediation_run.py). Held separately from `remProg`
  // because the two answer different questions and must not be merged into one client-side
  // object: remProg is the batch progress bar's denominator and its latest filename; this is the
  // authoritative state, the reconciled partition, and the server's own integrity verdict.
  // NOTHING here is derived — see RemediationOpsPanel for why the derivation moved to the server.
  // The resume cursor: the last scan_events.seq this browser actually rendered. Null means "no
  // cursor" — a first connection, which the server answers with live frames and no backfill.
  const [serverFixed, setServerFixed] = useState(0)  // files fixed server-side this scan (persists after each batch)
  const [acceptedLaunch, setAcceptedLaunch] = useState(null)
  const [acceptedPlan, setAcceptedPlan] = useState(null)
  const { batchId: acceptedBatchId, authorization: acceptedAuthorization, scopedSnapshot } = useAcceptedRemediationIdentity({
    scanId: runId, snapshot: runStream?.snapshot, launch: acceptedLaunch, clearLaunch: setAcceptedLaunch, releaseState: automaticReleaseState,
  })
  const runAiApproval = useRunAiApproval(runId, acceptedBatchId)
  const [reviewRefreshError, setReviewRefreshError] = useState(null)
  useEffect(() => { setReviewRefreshError(null) }, [runId, acceptedBatchId])
  useEffect(() => {
    let active = true
    setAcceptedPlan(null)
    if (runId && acceptedBatchId) {
      setAcceptedPlan({ scanId: runId, batchId: acceptedBatchId, loading: true })
      getAcceptedRemediationPlan(runId, acceptedBatchId).then(result => {
        if (active) setAcceptedPlan({ scanId: runId, batchId: acceptedBatchId, policy: result.policy, loading: false })
      }).catch(() => { if (active) setAcceptedPlan({ scanId: runId, batchId: acceptedBatchId, loading: false }) })
    }
    return () => { active = false }
  }, [runId, acceptedBatchId])

  const pollRef = useRef(null)
  const remStartRef = useRef(false)   // synchronous guard — remBusy is state, two clicks in one frame both read false
  useEffect(() => () => clearInterval(pollRef.current), [])
  // The "Estimated pickup" range (GET /scans/{id}/queue-estimate?kind=remediate) for the window
  // between clicking Remediate and the first document actually finishing — same 10s cadence as
  // Discover's and Assess's own pickup polls. Stops the instant a document completes (remProg.done
  // > 0): once files are moving, Remediate's own live progress bar already answers "is this
  // working", and the estimate has nothing left to add. Omitted (stays null) until the backend has
  // enough recent-completion history for an honest range.
  const [pickupEstimate, setPickupEstimate] = useState(null)
  useEffect(() => {
    setPickupEstimate(null)
    const waiting = remBusy && (!remProg || remProg.done === 0)
    if (!waiting || !runId) return undefined
    let on = true
    const load = () => getQueueEstimate(runId, 'remediate').then((d) => { if (on) setPickupEstimate(d) }).catch(() => {})
    load()
    const id = setInterval(load, 10000)
    return () => { on = false; clearInterval(id) }
  }, [remBusy, remProg?.done, runId])
  const REMKEY = (id) => `acp-remed-${id || 'none'}`

  // A batch of N documents cannot report more than N failures. The server scopes its own count
  // to the latest batch now, so this is the last line of defence rather than the fix: on
  // 2026-09-04 an unscoped `failed` of 294 against a 147-document batch reached this component
  // and was rendered as "-147 documents remediated". Clamping here means no future counting bug
  // upstream can produce an impossible number on screen.
  const clampFailed = (total, failed) =>
    Math.min(Math.max(0, Number(total || 0)), Math.max(0, Number(failed || 0)))

  const finishRemediation = (total, status = {}) => {
    clearInterval(pollRef.current)
    watchTotalRef.current = null   // stop reacting to frames for a batch that is finished
    const failed = clampFailed(total, status.failed)
    setRemProg((previous) => ({ ...previous, total, done: total,
                 latest: status.latest_file || previous?.latest || null,
                 failed, queued: 0, running: 0, activity: null, history: previous?.history || [] }))
    setRemBusy(false); setRemUpdates('idle')
    const ok = Math.max(0, total - failed)
    setServerFixed((n) => n + ok)
    setRemMsg(`✓ Remediation complete — ${ok} document${ok === 1 ? '' : 's'} fixed${failed ? `, ${failed} failed` : ''}.`)
    try { sessionStorage.removeItem(REMKEY(runId)) } catch { /* ignore */ }
    onRefresh?.(); fetchFixes()
    if (!SIM) refreshReviewQueue()
  }

  // THE SNAPSHOT AND THE STREAM ARE NO LONGER OWNED HERE. `useRemediationRun` holds both, at App
  // level, and passes them down as `runStream` — because this component is unmounted on every tab
  // change, and a connection that dies with it cannot keep a run watched, cannot keep ADR 0051's
  // resume cursor, and cannot honestly let the persistent card say "Live". What used to be an
  // `ops` state, an `acceptSnapshot`, a 5s snapshot poll and an `openRemediationStream` call is
  // now four reads of one prop.
  //
  // The batch DENOMINATOR still belongs here: it comes from the enqueue response or
  // sessionStorage, and the hook has no idea how many documents this run submitted. So the hook
  // owns the transport and this owns the arithmetic.
  const watchTotalRef = useRef(null)
  const lastStatusRef = useRef(null)
  const endedSeenRef = useRef(0)

  const applyRemediationStatus = (total, s) => {
    // The snapshot riding this frame is accepted by the hook, not here — one acceptor, one
    // revision guard, one place a superseded read can be dropped.
    const done = Math.max(0, total - (s.in_flight || 0))
    setRemProg((previous) => {
      const activity = s.activity || null
      const history = [...(previous?.history || [])]
      if (activity?.text && history[0]?.text !== activity.text) {
        history.unshift(activity)
        history.splice(6)
      }
      const failed = clampFailed(total, s.failed)
      const metrics = {
        fixes: Number(s.fixes_applied || 0), verified: Number(s.verified_documents || 0),
        stored: Number(s.stored_documents || 0), failed,
      }
      const before = previous?.metrics || {}
      const deltas = previous?.metrics
        ? Object.fromEntries(Object.entries(metrics).map(([key, value]) =>
            [key, Math.max(0, value - Number(before[key] || 0))]))
        : {}
      return { total, done, latest: s.latest_file, failed, activity, history,
               metrics, deltas, queued: s.queued, running: s.running, workers: s.workers,
               byRule: s.by_rule || [], recentFiles: s.recent_files || [] }
    })
  }

  // Polling remains the fallback for a proxy/browser that cannot keep the authenticated SSE
  // response open. It reads the exact same server-side snapshot as the stream.
  const startPoll = (total) => {
    clearInterval(pollRef.current)
    setRemUpdates('polling')
    let pollFails = 0
    pollRef.current = setInterval(async () => {
      try {
        const s = await getRemediationStatus(runId)
        pollFails = 0
        applyRemediationStatus(total, s)
        if (!s.in_flight) finishRemediation(total, s)
      } catch {
        // Transient errors retry, but not forever: ~30s of consecutive misses means
        // the API is unreachable — stop and say so instead of spinning silently.
        // The sessionStorage denominator survives, so a reload resumes watching.
        if (++pollFails >= 20) {
          clearInterval(pollRef.current)
          setRemBusy(false); setRemProg(null)
          setRemMsg('Lost contact with the job queue — the batch may still be running server-side. Reload to resume watching.')
        }
      }
    }, 1500)
  }

  // Start watching a batch of `total` documents. It no longer opens anything: the stream is
  // already running in `useRemediationRun`, whether or not this tab is showing. All this records
  // is the DENOMINATOR, which the hook cannot know — it is the count this component just enqueued
  // or restored from sessionStorage, not something the server's snapshot reports.
  const startWatching = (total) => {
    watchTotalRef.current = total
    endedSeenRef.current = runStream?.endedAt || 0   // don't finalize on a PREVIOUS run's close
    setRemUpdates(runStream?.connected ? 'live' : 'polling')
  }

  // Every stream frame the hook receives, turned into this component's progress arithmetic.
  // Effect rather than callback because the frames arrive as a prop now.
  useEffect(() => {
    const total = watchTotalRef.current
    if (!total || !runStream?.status) return
    lastStatusRef.current = runStream.status
    applyRemediationStatus(total, runStream.status)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runStream?.status])

  // The server closed the stream: the batch drained. `endedAt` is a timestamp rather than a flag
  // precisely so a second run's close is distinguishable from the first's still being set —
  // comparing against what we last SAW is what stops a stale close finalizing a fresh batch.
  useEffect(() => {
    const ended = runStream?.endedAt || 0
    const total = watchTotalRef.current
    if (!ended || ended === endedSeenRef.current || !total) return
    endedSeenRef.current = ended
    finishRemediation(total, lastStatusRef.current || { failed: 0 })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runStream?.endedAt])

  // The legacy progress bar needs the legacy status shape, which the hook's own fallback does not
  // fetch (it polls the reconciled snapshot). So when the stream is down while we are watching a
  // batch, this polls that shape — HTTP, not a second stream.
  useEffect(() => {
    if (!watchTotalRef.current) return undefined
    if (runStream?.connected) { clearInterval(pollRef.current); setRemUpdates('live'); return undefined }
    startPoll(watchTotalRef.current)
    return () => clearInterval(pollRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runStream?.connected])

  // Resume the live view across tab switches / reloads: the poll lives in this component, so
  // without this, navigating away mid-run and back would freeze the cards. The denominator is
  // restored from sessionStorage (written when the run starts).
  useEffect(() => {
    if (!runId) return
    let saved = null
    try { saved = JSON.parse(sessionStorage.getItem(REMKEY(runId)) || 'null') } catch { /* ignore */ }
    if (saved?.total) { setRemBusy(true); setRemProg({ total: saved.total, done: 0, latest: null, failed: 0, history: [] }); startWatching(saved.total) }
    // NO LOCAL MEMORY IS NOT NO RUN. Sign out wipes every `acp-` key and reloads (App.jsx), so a
    // batch still running server-side comes back to a browser that has never heard of it — and
    // before this, to no card at all. Ask the server instead of assuming: it is the same snapshot
    // the live view already consumes, and it knows the batch and its size.
    if (!saved?.total) {
      let cancelled = false
      getRemediationStatus(runId).then((s) => {
        if (cancelled) return
        const resume = remediationResume(s)
        if (!resume) return
        // Re-seed the denominator so a later remount in this tab costs nothing, exactly as
        // starting a run does.
        try { sessionStorage.setItem(REMKEY(runId), JSON.stringify({ total: resume.total })) } catch { /* ignore */ }
        setRemBusy(true)
        setRemProg({ ...resume, activity: null, history: [] })
        startWatching(resume.total)
      }).catch(() => { /* no reconnect available — the card stays idle, as before */ })
      return () => { cancelled = true }
    }
    return undefined
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])
  // SIM: rebuild queue when triage changes (real mode: queue is DB-driven, unaffected by triage).
  useEffect(() => {
    if (SIM) setQueue(buildHumanQueue(files, triage))
  }, [triage]) // eslint-disable-line react-hooks/exhaustive-deps

  const runServerRemediation = async (scopeFiles, remediationPolicy, releaseIntent = null) => {
    if (!runId || readOnly || resultsOnlyRef.current || remBusy || remStartRef.current) return
    // The page-level controls pass file records; RemediationWork's deterministic batch passes
    // filenames because it partitions findings rather than owning the scan records. Normalize
    // both entry points here so they reach the same durable Remediate queue and progress watcher.
    const scope = (scopeFiles || [])
      .map((f) => typeof f === 'string' ? f : f?.file)
      .filter(Boolean)
    // Say why nothing will happen, BEFORE the round trip. The old path posted an empty scope,
    // the server answered enqueued:0, and the UI said "no eligible files with issues" — true,
    // useless, and indistinguishable from a button that did nothing at all.
    if (scope.length === 0) {
      setRemMsg(emptyScopeReason(files, scopeOpts))
      return
    }
    remStartRef.current = true
    setRemBusy(true); setRemMsg(''); setRemProg(null); setReleasePlanNotice('')
    try {
      const r = await remediateScan(runId, scope, remediationPolicy)
      if (!r.enqueued) {
        // We sent a non-empty scope and the server enqueued nothing: the client's view of
        // eligibility is stale (another session remediated them, or the scan moved on). Say
        // that, rather than repeating the empty-scope line and implying the user chose wrong.
        setRemMsg(`Nothing to remediate — the server found no eligible work in the ${scope.length} `
                  + `document${scope.length === 1 ? '' : 's'} sent. They may already have been `
                  + `remediated elsewhere; re-scan to refresh.`)
        setRemBusy(false); return
      }
      setAcceptedLaunch({ scanId: runId, batchId: r.batch_id || r.execution_id || r.stage_execution_id })
      setPlanRevision(value => value + 1)
      setWorkspaceRequest({ mode: 'live' })
      if (releaseIntent) {
        const notice = await authorizeAcceptedRelease(runId, scope, r, releaseIntent)
        if (releasePlanScan.current === runId) setReleasePlanNotice(notice)
      }
      // In-process pool OR the standalone worker container's heartbeat (#113) counts as manned.
      if (!r.workers && !r.worker_tier_alive) { setRemMsg(`Enqueued ${r.enqueued}, but no workers are available — the worker service looks down; check Monitor.`); setRemBusy(false); return }
      setRemMsg(stageExecutionNotice('Remediation', r))
      const total = r.enqueued
      setRemProg({ total, done: 0, latest: null, failed: 0, history: [] })
      try { sessionStorage.setItem(REMKEY(runId), JSON.stringify({ total })) } catch { /* ignore */ }
      startWatching(total)
    } catch (e) {
      setRemMsg(remediationSubmissionFailureCopy(e)); setRemBusy(false)
    } finally {
      remStartRef.current = false
    }
  }
  const triageFile = (file, st) => { if (resultsOnlyRef.current || readOnly) return; setTriage((t) => { const n = { ...t }; if (st == null) delete n[file]; else n[file] = st; return n }) }
  const revalidated = files.filter((f) => f.compliant)

  // A review decision that fails to reach the server must NOT look like one that succeeded.
  // The optimistic update pulls the card out of the queue and bumps the counter before the
  // write; every write used to end in `.catch(() => {})`, so a 401 or a 500 left the reviewer
  // believing they had signed something off that the server never recorded. In a compliance
  // product an unrecorded approval is worse than a visible failure. Put the card back, undo
  // the count, and say so.
  const undoAct = (item, kind, err, outcome = 'not_saved') => {
    if (item) {
      setQueue((q) => (q.some((x) => x.id === item.id) ? q : [item, ...q]))
      if (kind === 'deferred') setDeferredItems((d) => d.filter((x) => x.id !== item.id))
      // W2 — the reject also created a handoff row; a refused write must pull that back too.
      if (kind === 'rejected') setRejectedItems((r) => r.filter((x) => x.id !== item.id))
      // …as must the optimistic decided row: an undone decision is not a recorded one.
      setDecidedItems((d) => d.filter((x) => x.id !== item.id))
    }
    setActed((a) => ({ ...a, [kind]: Math.max(0, (a[kind] || 0) - 1) }))
    window.dispatchEvent(new Event('acp:hitl-changed'))
    setActError(hitlFailureCopy(item, kind, err, outcome))
  }

  const settleActFailure = async (item, kind, wanted, err) => {
    const settled = await reconcileHitlPutFailure(item?.id, wanted, err)
    if (settled.outcome === 'saved') {
      // The PUT response was lost to capacity pressure, but the durable row proves the decision
      // landed. Keep the optimistic UI and refresh derived compliance state just as on a normal
      // approval response. Never ask the reviewer to repeat an already-recorded decision.
      window.dispatchEvent(new Event('acp:hitl-changed'))
      try { const r = onRefresh?.(); if (r && typeof r.catch === 'function') r.catch(() => {}) }
      catch { /* reconciliation already proved the decision; refresh remains cosmetic */ }
      return
    }
    undoAct(item, kind, err, settled.outcome)
    throw err
  }

  // Returns a promise that RESOLVES when the decision is durably recorded and REJECTS when the
  // server refused it. That return value is the contract the review pane's auto-advance is built on:
  // it awaits this, and a rejection keeps the reviewer on the finding with the error stated inline
  // instead of advancing them past it behind a banner they have already scrolled away from.
  // `undoAct` still performs the local rollback; the re-throw is what makes the failure visible.
  const act = (id, kind, editedValue, approvedValues, resolution = null, frozen = null) => {
    if (reviewReadOnlyRef.current) return Promise.reject(new Error('Historical scans are available for results browsing only.'))
    const current = queue.find((x) => x.id === id)
    if (frozen && (!current || selectionFingerprint(current) !== frozen.decision.selectionFingerprint)) {
      return Promise.reject(Object.assign(new Error('Proposal or source changed — review and select again.'), { status: 409 }))
    }
    const item = frozen?.finding || current
    setActError(null)
    setQueue((q) => q.filter((x) => x.id !== id))
    setSelItem(null)
    if (kind === 'self') { if (item) setSelf((s) => [{ ...item, status: 'awaiting' }, ...s]); return Promise.resolve() }
    if (kind === 'deferred') {
      if (item) setDeferredItems((d) => [...d, item])
      // A skip is recorded on the row as `skipped`; keep it in the inbox as manual work owed,
      // not as an item that quietly left the run.
      recordDecided(item, 'skipped')
      setActed((a) => ({ ...a, deferred: a.deferred + 1 }))
      if (!SIM && item?.id) {
        return updateHitlItem(item.id, 'skipped', null, null, {
          expectedVersion: item._raw?.decision_version ?? 0,
        }).catch(
          (e) => settleActFailure(item, 'deferred', { status: 'skipped' }, e))
      }
      return Promise.resolve()
    }
    setActed((a) => ({ ...a, [kind]: a[kind] + 1 }))
    // W2 — rejecting an AI fix routes it back to the inbox as a manual-handling item instead of
    // dropping it. Clear the AI proposal (after/proposals/hasProposal/aiDraftable) and any status so
    // laneOf() lands it in the amber handoff lane ("Needs manual handling"), not the green/blue
    // approve lanes. The `rejected` audit write below still fires — this only adds the destination.
    if (kind === 'rejected' && item) {
      const handoff = { ...item, status: undefined, after: null, proposals: null,
                        hasProposal: false, autoApplied: false, aiDraftable: false, rejectedFix: true }
      setRejectedItems((r) => (r.some((x) => x.id === handoff.id) ? r : [...r, handoff]))
    }
    // An approval is a recorded decision awaiting the confirming re-scan — it belongs in the
    // inbox's Awaiting-verification stage, and in the run total, from the moment it is taken.
    // (A rejection is already represented by its handoff row above, which the inbox prefers.)
    if (kind === 'approved') recordDecided(item, 'approved', resolution)
    window.dispatchEvent(new Event('acp:hitl-changed'))
    const apiStatus = kind === 'approved' ? 'approved' : kind === 'rejected' ? 'rejected' : null
    // approved_value is the headline text (audit log, telemetry); approvedValues carries one
    // final text per image, keyed by position to the row's proposals. The server writes those
    // into the document at each proposal's locator, re-scans the result, and only then lets
    // the file certify — so what is approved here is what the document ends up saying.
    if (!SIM && item?.id && apiStatus) {
      const p = updateHitlItem(item.id, apiStatus, null,
                               apiStatus === 'approved' ? (editedValue || null) : null,
                               { approvedValues: apiStatus === 'approved' ? (approvedValues || null) : null,
                                 expectedVersion: frozen?.decision.expectedVersion ?? item._raw?.decision_version ?? 0,
                                 requestId: frozen?.decision.requestId,
                                 expectedProposalSnapshotIds: frozen?.decision.expectedProposalSnapshotIds,
                                 expectedSourceRevision: frozen?.decision.expectedSourceRevision,
                                 // A WCAG-exception / out-of-scope resolution: status stays 'approved'
                                 // but it writes NO value — the reason is persisted on the row.
                                 resolution: apiStatus === 'approved' ? (resolution || null) : null })
      // On approval the server re-validates the file: once its every review item is
      // approved it flips to compliant and enters the publish queue. Refresh the scan so
      // "Re-validated & ready to publish" (and the Publish tab) pick that up immediately.
      // Two-arg then, NOT .then().catch(): a chained catch would also see a rejection from
      // onRefresh() and roll back a decision the server had already accepted. The refresh is
      // cosmetic; only the write's own failure may undo the decision.
      return p.then(
        () => {
          if (apiStatus !== 'approved') return
          try { const r = onRefresh?.(); if (r && typeof r.catch === 'function') r.catch(() => {}) }
          catch { /* the refresh is cosmetic — never let it disturb a saved decision */ }
        },
        (e) => settleActFailure(item, kind, {
          status: apiStatus, requestId: frozen?.decision.requestId,
          approvedValue: apiStatus === 'approved' ? (editedValue || null) : null,
          approvedValues: apiStatus === 'approved' ? (approvedValues || null) : null,
          resolution: apiStatus === 'approved' ? (resolution || null) : null,
        }, e),
      )
    }
    return Promise.resolve()
  }
  const draftAi = (item) => { if (reviewReadOnlyRef.current) return Promise.reject(new Error('Historical scans are available for results browsing only.')); return suggestFix(item.scanId || runId, item.file, item.ruleId).then((r) => r?.suggestion) }
  const verifySaved = async (item) => {
    const result = await verifySavedRemediation({ runId, item, canAct: () => !reviewReadOnlyRef.current && verificationRunRef.current === runId, getReleaseStatus, getScan, verifySavedCopy })
    onRefresh?.()
    return result
  }
  // Retry saving a recorded approval; do not create a second review decision.
  const retryApprovedFix = async (item) => {
    if (reviewReadOnlyRef.current) throw new Error('Historical scans are available for results browsing only.')
    const itemId = item?._raw?.id ?? item?.id
    if (!itemId) throw new Error('This review item has no server record to retry.')
    const result = await retryApprovedWrite(itemId)
    if (result?.accepted && result?.in_flight) {
      window.dispatchEvent(new Event('acp:hitl-changed'))
      try { const r = onRefresh?.(); if (r && typeof r.catch === 'function') r.catch(() => {}) }
      catch { /* the refresh is cosmetic — the queued job is the outcome, and it is already queued */ }
    }
    return result
  }
  const rescan = async (id) => {
    if (reviewReadOnlyRef.current) return
    const item = self.find((x) => x.id === id)
    if (!item) return
    setSelf((s) => s.map((x) => x.id === id ? { ...x, status: 'scanning' } : x))
    const outcome = await checkSelfRemediation(item, verifySaved)
    if (!reviewReadOnlyRef.current && verificationRunRef.current === runId) setSelf((s) => s.map((x) => x.id === id ? { ...x, ...outcome } : x))
  }
  const verified = self.filter((x) => x.status === 'verified').length
  // Live re-verified KPI = manual self-fixes + banked server fixes + the in-flight batch's
  // completions (ticks every poll), so the card moves in real time with the worker queue.
  const liveFixed = remProg ? Math.max(0, remProg.done - (remProg.failed || 0)) : 0
  const reVerified = verified + serverFixed + liveFixed
  const remLive = remediationWorkRunning(runStream?.snapshot, acceptedBatchId, remBusy, remProg)
  useEffect(() => {
    if (remBusy && !remLive && acceptedBatchId && runStream?.snapshot?.batch_id === acceptedBatchId) setRemBusy(false)
  }, [remBusy, remLive, acceptedBatchId, runStream?.snapshot?.batch_id])
  const pendingHitlFiles = new Set(queue.map((q) => q.file))
  // The run's review total, from the queue the SERVER holds: outstanding rows plus rows that
  // already carry a decision. It used to be the pending count plus this session's own tally, so
  // it reset to "0 of 12 resolved" on every reload of a run whose queue held 13 items and one
  // decision. `self` items (fixed by hand here, no server decision) are counted only while the
  // server has not returned them.
  const decidedIds = new Set(decidedItems.map((d) => d.id))
  const queuedIds = new Set(queue.map((q) => q.id))
  const selfOnly = self.filter((s) => !decidedIds.has(s.id) && !queuedIds.has(s.id))
  const totalHitl = queue.length + decidedItems.length + selfOnly.length
  const hitlProgress = totalHitl > 0 ? Math.round(((totalHitl - queue.length) / totalHitl) * 100) : 0
  // Redesign R4: the ONE dominant statement — how many findings need review across how many
  // documents. Derived below from the assembled inbox queue (not the raw human queue) so the
  // headline is the SAME count as the "Needs review" tab and the two can never disagree.
  // (The nav badge that mirrors this count is reported below, once reviewCount is derived.)
  // Don't surface remediation numbers until the user has actually started remediating
  // (ran "Remediate all" or acted on a review item) — pre-engagement estimates read as
  // in-progress work and confuse first-time users. Until then the cards show zeros.
  const remStarted = remProg != null || remBusy || serverFixed > 0 || (acted.approved + acted.rejected + acted.deferred + self.length) > 0

  // --- remediable set + decisions (moved from Discover) ---
  // Published business ontology takes precedence in the queue order (Critical → Low),
  // then the AI risk triage breaks ties.
  const ontRank = (f) => f.ont?.priority ? PRI_RANK[f.ont.priority] : 9
  const hasInscopeSelections = hasDocumentSelection(triage)
  // One eligibility test, shared with emptyScopeReason() — so what the button acts on and what
  // it says when it can't act on anything are derived from the same rules, in the same order.
  const scopeOpts = { triage, hasInscopeSelections, remActions: REM_ACTIONS }
  // Same eligibility test as `remediable` below, counted rather than filtered — so what the
  // panel SAYS it will skip and what the button actually skips cannot drift apart.
  const scopeInfo = scopeSummary(files, scopeOpts)
  const remediable = remediableFiles(files, scopeOpts)
  // Planning includes human-only files and files with residual findings after a prior fix.
  // The old automatic-action cohort would hide precisely the work this card explains.
  const impactScope = remediationImpactScope(files, triage)
    .sort((a, b) => (ontRank(a) - ontRank(b)) || (priority(b) - priority(a)))
  useAutomaticReleaseStatus(runId, impactScope, setAutomaticReleaseState)
  const dcount = (st) => remediable.filter((f) => decisions[f.file]?.state === st).length

  // business priority (findings-based)
  const flagged = files.filter((f) => (f.issues || []).length)
  const findingsBy = (keyFn, order) => {
    const m = {}; flagged.forEach((f) => { const k = keyFn(f); if (k != null) m[k] = (m[k] || 0) + f.issues.length })
    return (order ? order.filter((k) => m[k]).map((k) => [k, m[k]]) : Object.entries(m).sort((a, b) => b[1] - a[1]))
  }
  const deptData = findingsBy((f) => f.department).slice(0, 8).map(([label, value]) => ({ label, value, color: 'var(--focus-ring)' }))
  const senData = findingsBy((f) => f.seniority, SENIORITY_ORDER).map(([label, value]) => ({ label, value, color: SR_COLOR[label] }))
  const expData = findingsBy(exposureOf, ['public-facing', 'high-traffic', 'internal']).map(([label, value]) => ({ label, value, color: EXP_COLOR[label] }))
  const pubCrit = flagged.filter((f) => (f.tags || []).includes('public-facing') && (f.issues || []).some((i) => i.severity === 'CRITICAL')).length
  const execFlagged = flagged.filter((f) => f.seniority === 'Executive').length
  const drill = (title, sub, pred) => setSeg({ title, subtitle: sub, files: flagged.filter(pred) })

  const _triageFiles = files.filter((f) => !(f.remediated_at || f.drive_write_url))
  const written = files.filter((f) => f.drive_write_url).length
  // Once remediation has run, an empty HITL queue means every fix went through the
  // automated path — approved/deferred (HITL decision counts) are meaningless there,
  // so swap them for the KPI that actually reflects what happened: writes to Drive.
  const pureAutomated = remStarted && totalHitl === 0

  // ── Reframed view (Review → Approve → Verify → Publish) ──────────────────────────
  // Every count below is a straight tally of real pipeline rows — applied-fix evidence,
  // the live HITL queue, the recommendation estimate — never a fabricated number.
  const fixSource = scanDiffs.length ? scanDiffs : appliedFixes   // diffs cover all fix types; applied_fixes is the fallback for older scans
  // Fold the auto-applied fixes into the inbox as green REVIEW-lane rows, so review-of-auto-fixes
  // shares the master/detail flow. The human review queue (assisted/manual) comes first; the
  // green auto-fixes follow. Ack'd ones resolve in place (RemediationInbox's Resolved tab).
  const autoFixItems = autoFixRows(fixSource, (sc) => ITEM_NAME[sc] || sc, { aiApplicationRecords: !scanDiffs.length })
  // W2 — rejected AI fixes sit between the live human queue and the auto-applied rows, in the amber
  // "Needs manual handling" lane, so a reviewer sees exactly what was bounced back for a person.
  // Decided rows sit between the two: after this session's handoffs (a rejection this reviewer
  // just made is shown as the manual work it created, not as a closed decision) and before the
  // applied-fix evidence. Deduped by id because a finding can legitimately be in two of these
  // sources at once, and counting it twice is the same defect as dropping it.
  const reviewQueue = reviewableRemediationItems(dedupeById([...queue, ...rejectedItems, ...decidedItems, ...autoFixItems]),
    { files, exceptions: reviewExceptions?.run_id === runId ? reviewExceptions : null })
  // Each review TASK once. dedupeById above only collapses repeated ids; an `af:` applied-change row
  // that is a proven second representation of a HITL row (same item, finding or target) is dropped
  // here, so every count and the progress line below read one denominator.
  const reviewTasks = dedupeReviewTasks(reviewQueue)
  const inboxQueue = automaticReviewQueue(reviewTasks, runAiApproval.policy, { ...decisions, ...ackd })
  const refreshReviewQueue = useReviewQueueRefresh({
    scanId: runId, batchId: acceptedBatchId, approval: runAiApproval.policy, disabled: SIM,
    progressKey: `${runStream?.snapshot?.revision ?? ''}:${runStream?.snapshot?.review?.items ?? ''}:${runStream?.snapshot?.fixes?.applied ?? ''}:${runStream?.snapshot?.documents?.completed ?? ''}:${runStream?.status?.queued ?? ''}:${runStream?.status?.running ?? ''}:${runStream?.events?.[0]?.id ?? ''}`,
    active: runAiApproval.enabled === true && inboxQueue.some(row =>
      row.automaticQueued || (isAiAssistedDraft(row) && !row.inspectionOnly
        && (matchesWorkflow(row, 'needs-review', { ...decisions, ...ackd })
          || matchesWorkflow(row, 'awaiting-validation', { ...decisions, ...ackd })))),
    onRows: items => {
      setReviewRefreshError(null)
      const seeded = {}
      applyHitlRows(items).forEach(item => { if (item.assignee) seeded[item.file] = item.assignee })
      if (Object.keys(seeded).length) setAssignees?.(previous => ({ ...seeded, ...previous }))
      fetchFixes()
    },
    onError: error => setReviewRefreshError(error),
  })
  const hasRemediationResults = inboxQueue.length > 0 || files.some(file => file.remediated_at || file.drive_write_url)
    || (runStream?.snapshot?.terminal === true && runStream.snapshot.total_documents > 0)
  const inboxDecisions = { ...decisions, ...ackd }
  // The hero "N need review" IS the Needs-review tab's population (workflowStatusOf over the inbox
  // queue), not the raw human queue — so an unconfirmed auto-fix counted under Needs review shows in
  // both, and the headline can never diverge from the tab (the R4 count-consistency fix). The doc
  // count is the distinct documents among exactly those findings.
  const reviewNeeds = inboxQueue.filter((f) => matchesWorkflow(f, 'needs-review', inboxDecisions))
  const reviewCount = reviewNeeds.length
  const reviewCounts = remediationReviewCounts(inboxQueue, inboxDecisions, {}, runAiApproval.enabled === true)
  // The Review queue's progress, from the SAME (queue, decisions) pair the inbox pane's own
  // "N of M reviewed" counter reads — RemediationInbox calls progress(queue, decisions) on exactly
  // these props. It used to read `totalHitl`/`hitlProgress`, a session tally of the raw human queue
  // plus this session's decisions and self-fixes, so the header said "6 of 15" two lines above a
  // sentence counting 9 of a different 12. Two numbers, two denominators, no way to reconcile them
  // by reading. `totalHitl` still drives the Advanced block's engine-level "HITL queue" metric,
  // which is a different question asked in a different place.
  // ONE explanation of what is left — the lead line, the progress line, the remaining-findings
  // panel and the inbox's own counters all read it, so none can say "All clear" while a status
  // check or a server-reported unresolved finding remains. Findings come from the remediate stage
  // snapshot only when it is for this scan; otherwise they are unknown (null), never zero.
  const stageDomain = remediationStage && (remediationStage.scan_id == null || remediationStage.scan_id === runId)
    ? remediationStage.domain_reconciliation : null
  // The snapshot too, so its own integrity/reconciliation signals can mark the ledger inconsistent.
  const findingInputs = findingInputsFrom(stageDomain, stageDomain ? remediationStage : null)
  const reviewExplanation = explainReviewPopulation({
    rows: inboxQueue, decisions: inboxDecisions, automatic: runAiApproval.enabled === true, files, ...findingInputs,
  })
  const reviewProgress = reviewExplanation.progress
  // The bar fills with FINAL outcomes only: a recorded decision whose change is still awaiting its
  // re-check is not done, so it does not paint the bar green.
  const reviewPct = reviewProgress.total > 0 ? Math.round((reviewProgress.finished / reviewProgress.total) * 100) : 0
  const reviewDocCount = new Set(reviewNeeds.map((f) => f.file).filter(Boolean)).size
  // Navigation counts pending human review items, excluding already-applied inspection rows.
  useEffect(() => { onHitlCount?.(reviewCounts.pendingItems) }, [reviewCounts.pendingItems, onHitlCount])
  // C5 — open one specific review item. Anything may ask (the remaining-findings panel below, the
  // stage card's queue drawer) through the window event; this page is the one listener. It shows
  // the review workspace, and the inbox selects, scrolls to and focuses the row in the tab that
  // holds it. A request made before this page mounted is picked up once, on mount.
  const [reviewItemRequest, setReviewItemRequest] = useState(null)
  useEffect(() => {
    const open = (detail) => {
      if (detail?.itemId == null) return
      if (detail.scanId && runId && detail.scanId !== runId) return
      takePendingReviewItem(runId)
      // focusPanel:false — the inbox moves focus to the requested row; the panel must not take it back.
      setWorkspaceRequest({ mode: 'review', focusPanel: false })
      setReviewItemRequest((previous) => ({ itemId: String(detail.itemId),
        tab: QUEUE_TAB_KEY[detail.tab] || detail.tab || null, nonce: (previous?.nonce || 0) + 1 }))
    }
    const listener = (event) => open(event.detail)
    window.addEventListener(OPEN_REVIEW_ITEM_EVENT, listener)
    const pending = takePendingReviewItem(runId)
    if (pending) open(pending)
    return () => window.removeEventListener(OPEN_REVIEW_ITEM_EVENT, listener)
  }, [runId])
  const openReviewItem = (itemId, tab) => requestOpenReviewItem({ itemId, scanId: runId, tab })
  // The automation-first summary's numbers. Every one counts something the run actually produced —
  // applied-fix evidence, the live HITL queue, the workflow partition — computed from the same
  // sources the panels below use, so the header can never advertise a different total than they do.
  const workflowCount = (k) => inboxQueue.filter((f) => matchesWorkflow(f, k, inboxDecisions)).length
  const manualCount = workflowCount('manual')
  const revalidatingCount = workflowCount('awaiting-validation')
  const blockedCount = workflowCount('blocked')
  // The deterministic batch, taken from the SAME partition RemediationWork's own button uses.
  const workPartition = remediationWork(files, { cap, assessment })
  const autoBatch = batchScope(workPartition)
  // Impact preview reads the full stored assessment on the server. Review cards enrich
  // findings there; they are not added to a partial automatic-only browser population.

  const fixGroups = groupFixesByRule(fixSource)
  const impact = summarizeImpact(fixSource)
  const fixTotal = scanDiffs.length || !appliedFixes.length ? diffTotals?.total : null
  const fixDocumentTotal = fixTotal != null ? diffTotals?.documents : null
  const fixedCount = fixTotal ?? totalFixes(fixSource)
  const fixesByFile = {}; fixSource.forEach((r) => { fixesByFile[r.file] = (fixesByFile[r.file] || 0) + 1 })
  const reviewByFile = {}; queue.forEach((q) => { reviewByFile[q.file] = (reviewByFile[q.file] || 0) + 1 })
  // Reviewer time, MEASURED (hitl_events.review_ms). Replaces "est. savings", which was one
  // invented constant (35 min/finding by hand) minus another (~1 min/finding automated).
  // A saving needs a counterfactual nobody ever timed; an average review time is a fact.
  // The signed record of what was changed. Built from the same three sources the UI shows —
  // the diffs (what), applied_fixes (when), and the live review queue (what is still open) —
  // so the PDF cannot claim anything this page does not.
  const [reportBusy, setReportBusy] = useState(false)
  const [reportErr, setReportErr] = useState(null)
  const APPLIED_FIX_CAP = 200        // the server's LIMIT in list_applied_fixes; disclosed if hit
  const [reportProgress, setReportProgress] = useState(null)
  // The remediation report now has the same three modes as the other two report kinds — Summary,
  // Reviewer packet, Full evidence — instead of one unlabelled "(PDF)" button whose content nobody
  // could choose. ReportModeMenu owns the progress line and surfaces every refusal, including the
  // render route's 409 "regenerate" when the evidence has moved on since the facts were read.
  const runRemediationReport = async (mode) => {
    setReportErr(null)
    const sid = run?.id
    setReportProgress({ loaded: 0, total: null, complete: false })
    try {
      const { gatherRemediationEvidence } = await import('./remediationReportData.js')
      const [evidence, fixes] = await Promise.all([
        gatherRemediationEvidence(sid, { onProgress: setReportProgress }),
        getAppliedFixes(sid).catch(() => []),
      ])
      const { exportRemediationReport } = await import('./pdfReport.js')
      // The renderer's own outcome is RETURNED, not swallowed: the menu is what tells the reader
      // whether a PDF exists, so a report that fell back to HTML must not reach it as a success.
      return await exportRemediationReport({
        ...evidence, mode, files, appliedFixes: fixes || [], reviewByFile,
        scanId: sid, level: run?.target || 'AA', org: run?.org || '',
        generatedAt: new Date().toISOString(), cappedAt: APPLIED_FIX_CAP,
      })
    } finally { setReportProgress(null) }
  }

  // R20 · CSV companion — same data as the PDF (shared buildRemediationModel), machine-readable.
  const downloadRemediationCsvReport = async () => {
    setReportBusy(true); setReportErr(null)
    try {
      const sid = run?.id
      const [diffs, fixes] = await Promise.all([
        getScanRemediationDiffs(sid).catch(() => []),
        getAppliedFixes(sid).catch(() => []),
      ])
      const diffsByFile = {}
      ;(diffs || []).forEach((d) => { (diffsByFile[d.file] = diffsByFile[d.file] || []).push(d) })
      const { downloadRemediationCsv } = await import('./remediationCsv.js')
      downloadRemediationCsv({
        files, diffsByFile, appliedFixes: fixes || [], reviewByFile,
        scanId: sid, level: run?.target || 'AA', org: run?.org || '',
        generatedAt: new Date().toISOString(), cappedAt: APPLIED_FIX_CAP,
      })
    } catch (e) {
      setReportErr(`CSV not generated: ${e?.message || e}`)
    } finally { setReportBusy(false) }
  }

  // A review decision the server refused. Loud, and sticky until the next attempt.
  const [actError, setActError] = useState(null)
  const [reviewStats, setReviewStats] = useState(null)
  const reviewEvidenceKey = reviewEvidence.key(runId, run?.revision)
  useEffect(() => {
    if (!runId || SIM) { setReviewStats(null); return }
    let live = true
    const pull = (refresh = false) => reviewEvidence.load(runId, run?.revision, { refresh })
      .then((result) => { if (live && result.key === reviewEvidenceKey) setReviewStats(result.value) }).catch(() => {})
    pull()
    const refresh = () => pull(true)
    window.addEventListener('acp:hitl-changed', refresh)
    return () => { live = false; window.removeEventListener('acp:hitl-changed', refresh) }
  }, [runId, run?.revision, reviewEvidenceKey])
  const measured = measuredReviewTime(reviewStats)

  // Verification state — tied to the real re-scan/job state (§8), never "0 → 0".
  const verifyPct = remProg ? Math.round((remProg.done / Math.max(1, remProg.total)) * 100) : 0
  const verifyState = remLive ? 'running'
    : queue.length > 0 ? 'waiting'
    : (revalidated.length > 0 || written > 0 || reVerified > 0) ? 'complete'
    : 'idle'

  // Business risk — one recommendation from real signals (§9): public / high-traffic
  // exposure tags × open critical findings. Never invented.
  const publicDocs = files.filter((f) => (f.tags || []).includes('public-facing'))
  const trafficDocs = files.filter((f) => (f.tags || []).includes('high-traffic'))
  const criticalOpen = files.reduce((n, f) => n + (f.issues || []).filter((i) => i.severity === 'CRITICAL').length, 0)
  const pubCritDocs = publicDocs.filter((f) => (f.issues || []).some((i) => i.severity === 'CRITICAL')).length
  const risk = publicDocs.length && pubCritDocs > 0
    ? { level: 'high', text: `Public-facing content · ${pubCritDocs} document${pubCritDocs === 1 ? '' : 's'} with critical findings · HIGH RISK under ADA / EAA` }
    : (publicDocs.length || trafficDocs.length) && criticalOpen > 0
    ? { level: 'med', text: `${publicDocs.length ? 'Public-facing' : 'High-traffic'} content · ${criticalOpen} critical finding${criticalOpen === 1 ? '' : 's'} open · MEDIUM RISK` }
    : criticalOpen > 0
    ? { level: 'med', text: `Internal content · ${criticalOpen} critical finding${criticalOpen === 1 ? '' : 's'} open · MEDIUM RISK` }
    // BOTH low-risk branches are reached by `criticalOpen === 0`, and zero findings is exactly
    // what an estate nobody could analyse produces. On 2026-08-19 that rendered "overall risk LOW"
    // over 22 documents that were never fetched (#481) — absence of evidence as evidence of
    // absence, in the sentence most likely to end up in a status report.
    //
    // Only the REASSURING verdicts are gated. The branches above state findings that were
    // genuinely found; those stay true with unassessed documents present, and suppressing them
    // would hide a real problem to avoid an imaginary one.
    : !canClaimLowRisk(files)
    ? { level: 'med', text: unassessedRiskText(files) }
    : publicDocs.length
    ? { level: 'low', text: 'Public-facing content · no critical findings · overall risk LOW' }
    : { level: 'low', text: 'Internal content only · overall risk LOW' }

  // One primary action at a time (§11): review → run → verify → publish.
  // Remediation is offered whenever a document is ELIGIBLE for it — `remediable` already
  // excludes anything with a fixed copy. It used to be gated on `!remStarted`, and remStarted
  // is true as soon as `acted.approved + acted.rejected + acted.deferred > 0`. So clearing the
  // review queue — the step that unblocks remediation — was exactly what removed the button
  // that runs it. The two branches were mutually exclusive and nobody could reach the second.
  const remRunning = remLive
  // ONE action, and which one it is follows the state of the run (PRD §5.2): apply what ACP can do
  // unattended, then work the exceptions, then publish. It never approves an AI draft — the automatic
  // branch is scoped to `autoBatch`, the deterministic partition, and drafts are not in it.
  //
  // There is deliberately NO "Revalidate approved work" button. Revalidation is something the server
  // does when an approval is written; the only re-scan this frontend can trigger re-reads the
  // ORIGINAL document (see RemediationVerify's own footnote), which is not a re-run over the
  // corrected copy. A button claiming otherwise would claim an action ACP cannot perform, so the
  // awaiting-revalidation count is reported as state in the summary line instead.
  const planAccepted = !!acceptedBatchId || !!(acceptedLaunch && acceptedLaunch.scanId === runId)
  const remediationHasStarted = planAccepted || remStarted || hasRemediationResults || !!scopedSnapshot?.batch_id
  const openRemediationPlan = () => { if (!readOnly && !resultsOnlyRef.current) setWorkspaceRequest({ mode: planAccepted ? 'live' : 'plan' }) }
  const primary = readOnly ? null
    : remRunning ? { label: 'Applying fixes…', disabled: true }
    : !planAccepted && autoBatch && autoBatch.count > 0
      ? { label: 'Start remediation', onClick: openRemediationPlan, disabled: !runId }
    : reviewCount > 0
      ? { label: 'Review next finding',
          onClick: () => setWorkspaceRequest({ mode: 'review' }) }
    : verifyState === 'running' ? { label: 'Revalidating…', disabled: true }
    : (verifyState === 'complete' || revalidated.length > 0)
      ? { label: 'Open Release', onClick: () => onNavigate?.('publish') }
    : null

  // Documents list (§5): triage + plan merged — one row per doc. Not-yet-fixed first, then
  // business priority. Triage counts drive the summary chips.
  const inscopeCount = _triageFiles.filter((f) => triage[f.file] === 'inscope').length
  const naCount = _triageFiles.filter((f) => triage[f.file] === 'na').length
  const deferCount = _triageFiles.filter((f) => triage[f.file] === 'defer').length
  const docList = [...files].sort((a, b) => {
    const aR = !!(a.remediated_at || a.drive_write_url), bR = !!(b.remediated_at || b.drive_write_url)
    if (aR !== bR) return aR ? 1 : -1
    return (ontRank(a) - ontRank(b)) || (priority(b) - priority(a))
  })
  const written2 = files.filter((f) => f.drive_write_url)
  const downloadOnly = files.filter((f) => f.remediated_at && !f.drive_write_url)

  const _DONE_STATES = new Set(['done', 'complete', 'completed', 'finalized', 'cancelled', 'interrupted', 'superseded'])
  const assessRunning = run?.status && !_DONE_STATES.has(run.status)
  const showPriorResultsNotice = assessRunning && files.length > 0

  // ── The page, composed in the order a reviewer needs it ──────────────────────────────────────
  // The compact run header states what ACP already did. The review workspace is the next thing on
  // the page, because it is the only part that needs a person. Everything else — the lane
  // partition, delivery, verification detail, the documents table, the engine internals — is real
  // and still reachable, one click away under Run details, rather than eleven panels the reviewer
  // scrolls past before reaching the work. Naming the blocks here keeps that ORDER readable in one
  // screen instead of spread across five hundred lines of JSX.

  const runSummaryDetail = (
    <div className="rem-hero-main">
          <div className="rem-hero-line">
            <b>{files.length}</b> document{files.length === 1 ? '' : 's'} processed
            {fixedCount > 0 && <> · <b className="rh-fixed">{fixedCount}</b> {fixTotal == null ? 'applied-change records loaded (total unavailable)' : `issue${fixedCount === 1 ? '' : 's'} fixed automatically`}</>}
            {/* Redesign R4: "N need your review" removed here — the Review queue section below is the
                single dominant place that count lives, so the hero no longer repeats it. */}
            {measured && (
              <span title={REVIEW_TIME_BASIS}> · avg review <b className="rh-review">{measured.avg}</b>
                <span className="muted"> over {measured.reviewed} decision{measured.reviewed === 1 ? '' : 's'}</span>
              </span>
            )}
          </div>
          {files.length > 0 && (
            <div className={`rem-risk risk-${risk.level}`}><b>Business risk:</b> {risk.text}</div>
          )}
    </div>
  )

  const scopeRecord = (
    <>
      {/* Compact scope record — what was in scope for this assessment, always visible.
          R4: replaced the old ScopeBanner <details> with a compact card that shows the three
          axes at a glance (criteria, formats, source/doc count). Navigates to Assess tab for
          rescoping, because scope is an Assess decision, not a Remediate one. */}
      <AssessmentScopeCard
        run={run}
        fileCount={files.length}
        state="done"
        onReassess={readOnly ? undefined : () => onNavigate?.('assess')}
        docScope={documentScopeSentence(documentSelection(files, triage))}
      />
    </>
  )

  const workLanes = (
    <>
      {/* ══ THE APPROVED BOARD CORE ══════════════════════════════════════════════════════════
          R2/R3, R5, R6, R9, R11, R12, in the board's order. Each one self-guards: given nothing
          measurable it renders nothing rather than a frame of zeros, so a run that has not reached
          a stage simply has no panel for it.

          They sit ABOVE the Review queue deliberately. The board's argument is that a reader must
          see how the work DIVIDES — what ACP fixes without asking, what needs a decision, what only
          a person can do — before being handed a queue of it. A queue first invites the reader to
          treat every item as the same kind of work, which is the thing the lanes exist to deny.

          The pre-existing hero, Review queue and charts below are untouched. Mounting ten
          components and deleting the screen they replace are two changes; doing both at once makes
          a regression impossible to attribute. The removal is its own commit. */}

      {/* R2 · the work, partitioned once — and R3, the deterministic batch inside it. */}
      <RemediationWork files={files} cap={cap} assessment={assessment}
                       onOpenPlan={readOnly ? undefined : openRemediationPlan}
                       applying={remBusy} />


      {/* R6 · the manual lane — work ACP cannot do at all, derived from the capability lanes
          rather than a hardcoded list. */}
      <ManualWork files={files} cap={cap} assessment={assessment} />

      {/* R9 · verification. Did the fixes actually hold? A remediation nobody re-checked is a
          claim, not a result. */}
      <RemediationVerify files={files} cap={cap} assessment={assessment} />

      {/* R11 · delivery — where the fixed files go. It reads the Drive-mirror setting itself and
          states the destination as a function of that setting, because under ADR 0010 the mirror
          defaults ON and a copy IS written into the customer's own drive. */}
      <DeliveryPanel files={files} />



      {/* R12 · close the loop. */}
      <CloseoutPanel docs={files}
                     onReverify={() => onRefresh && onRefresh()}
                     onReview={() => { const el = document.getElementById('rem-review'); if (el) el.scrollIntoView({ behavior: 'smooth' }) }}
                     onPublish={() => onNavigate && onNavigate('publish')} />
    </>
  )

  const fixFailures = (
    <>
      {/* R8 · when a fix fails. Page-level, because "what did not work in this run" is a property
          of the run rather than of whichever finding happens to be selected. It reads its own
          decisions and jobs, so it needs only the scan. */}
      <FixOutcomes scanId={run?.id} files={files} cap={cap} />
    </>
  )

  const referenceSections = (
    <>
      {/* ── VERIFICATION (§8) — real state, tied to the re-scan/job, auto-begins on approval. ── */}
      <RemSection id="rem-verify" title="Verification"
                  count={revalidated.length || null}
                  hint={verifyState === 'idle' ? '· nothing to verify yet' : null}
                  defaultOpen={verifyState === 'running' || revalidated.length > 0}>
        <VerifyState state={verifyState} pct={verifyPct} remaining={queue.length} ready={revalidated.length} latest={remProg?.latest} />
        {revalidated.length > 0 && (
          <div className="publist" style={{ marginTop: 12 }}>
            {revalidated.slice(0, 12).map((f) => (
              <div className="pubrow" key={f.file}>
                <button className="remname" onClick={() => setSel(f)}>{f.file}<span className="muted"> · {f.sourceName}</span></button>
                <span className="okline" style={{ fontSize: 13 }}>✓ verified {f.score} / 100</span>
              </div>
            ))}
            {revalidated.length > 12 && <div className="muted" style={{ fontSize: 12, padding: '6px 2px' }}>+{revalidated.length - 12} more</div>}
          </div>
        )}
      </RemSection>

      {/* ── DOCUMENTS (§5) — file triage + remediation plan merged into ONE list: per-doc
          progress · fixes · items needing you · scope · Open. Everything about a doc here. ── */}
      <RemSection id="rem-docs" title="Documents" count={docList.length}
                  defaultOpen={false}>
        <div className="rem-sec-hd">
          {/* The report exports used to live here. This section is inside `runDetailSections`,
              which is BUILT AND NEVER RENDERED — so the remediation report had no route into the
              UI at all, in any state of the run. They now sit in the Review queue header, which is
              mounted, and which is where a reviewer is when they want the record. */}
          <div className="triagesum">
            <span className="trstatchip inscope">{inscopeCount} in scope</span>
            <span className="trstatchip na">{naCount} N/A</span>
            <span className="trstatchip defer">{deferCount} deferred</span>
            {/* The chips above count only EXPLICIT decisions. When an in-scope selection exists it
                also excludes every unmarked document, so without this chip the panel reported
                "2 in scope" beside 258 listed rows and said nothing about the 256 being dropped. */}
            {scopeInfo.restrictedBySelection && (
              <span className="trstatchip out" title="Marking any document ✓ restricts the run to marked documents only. These are not marked, so Remediate all will skip them.">
                {scopeInfo.excluded.outOfScope.toLocaleString()} excluded — not marked ✓
              </span>
            )}
          </div>
        </div>
        <p className="muted" style={{ fontSize: 12, margin: '0 0 10px' }}>
          Everything about each document in one place — progress, fixes applied, items needing you, and whether it’s in scope.
          Set scope per row: <b style={{ color: 'var(--success-fg)' }}>✓</b> in scope · <b>N/A</b> skip · <b style={{ color: 'var(--info-fg)' }}>⏸</b> defer.
          {scopeInfo.restrictedBySelection
            ? <> <b style={{ color: '#8A2A20' }}>Because you marked {inscopeCount.toLocaleString()} document{inscopeCount === 1 ? '' : 's'} ✓, remediation runs on those alone</b> — the other {scopeInfo.excluded.outOfScope.toLocaleString()} are excluded until you mark them or clear the selection.</>
            : <> No document is marked ✓, so remediation runs on <b>all eligible documents</b>. Marking even one ✓ restricts the run to marked documents only.</>}
        </p>
        <div className="doclist">
          <div className="docrow dochead">
            <span>Document</span><span>Progress</span><span style={{ textAlign: 'center' }}>Loaded fixes</span><span style={{ textAlign: 'center' }}>Review</span><span>Scope</span><span />
          </div>
          {docList.map((f) => {
            const done = !!(f.remediated_at || f.drive_write_url)
            const pct = done ? 100 : (f.score != null ? f.score : 0)
            const dec = triage[f.file]
            const nFix = fixesByFile[f.file] || 0
            const nRev = reviewByFile[f.file] || 0
            // Excluded BY THE SCOPE SELECTION specifically — not merely "unmarked". A document
            // with no automatic fix is already ineligible for its own reason and marking it ✓
            // would not change that, so calling it "excluded by your selection" would be the
            // same false precision this panel is being fixed for.
            const outOfScope = ineligibleReason(f, scopeOpts) === 'outOfScope'
            return (
              <div className={outOfScope ? 'docrow outofscope' : 'docrow'} key={f.file}>
                <div className="doccell-name">
                  <button className="remname" onClick={() => setSel(f)}>{f.file}</button>
                  <div className="docsub muted">
                    {f.sourceName}{f.department ? ` · ${f.department}` : ''}
                    {/* Said on the row itself, because three untouched buttons in the Scope cell
                        read as "not decided yet" — which is how 256 dropped documents looked
                        identical to 256 pending ones. */}
                    {outOfScope && <span className="docexcl"> · excluded — not marked ✓</span>}
                  </div>
                </div>
                <div className="docprog">
                  <div className="docbar"><i style={{ width: `${pct}%`, background: pct >= 90 ? 'var(--success-fg)' : pct >= 60 ? '#BF8C00' : 'var(--error-fg)' }} /></div>
                  <span className="docpct">{done ? '✓ certified' : `${pct}%`}</span>
                </div>
                <span className="doccount">{nFix > 0 ? <b style={{ color: 'var(--success-fg)' }}>{nFix}</b> : <span className="muted">—</span>}</span>
                <span className="doccount">{nRev > 0 ? <b style={{ color: 'var(--warn-fg)' }}>{nRev}</b> : <span className="muted">—</span>}</span>
                <span className="docscope">
                  {done ? <span className="trstatchip inscope">done</span>
                  : dec ? <><span className={`trstatchip ${dec}`}>{dec === 'inscope' ? '✓ in scope' : dec === 'na' ? 'N/A' : '⏸ deferred'}</span><button className="ghost small" onClick={() => triageFile(f.file, null)} title="Undo">↺</button></>
                  : <span className="scopebtns">
                      <button className="trbtn inscope" onClick={() => triageFile(f.file, 'inscope')} title={outOfScope ? 'Excluded — mark ✓ to include this document in remediation' : 'In scope — include in remediation'}>✓</button>
                      <button className="trbtn na" onClick={() => triageFile(f.file, 'na')} title="Not applicable — skip">N/A</button>
                      <button className="trbtn defer" onClick={() => triageFile(f.file, 'defer')} title="Defer — decide later">⏸</button>
                    </span>}
                </span>
                <button className="ghost small docopen" onClick={() => setSel(f)}>Open →</button>
              </div>
            )
          })}
        </div>
      </RemSection>

      {/* ── Recent AI fixes, grouped (§6) + Accessibility improvements impact (§7) ── */}
      <p className="muted">{fixSource.length} applied-change records loaded{fixTotal != null ? ` of ${fixTotal} total` : ' · total unavailable'}. Detailed groups and document counts below describe these loaded records.</p>
      <GroupedFixes fixGroups={fixGroups} appliedFixes={appliedFixes} impact={impact} />

      {/* Self-remediation — you're fixing these yourself; visible whenever active. */}
      {self.length > 0 && (
        <RemSection id="rem-self" title="Self-remediation" count={self.length}
                    hint="· you’re fixing these — check the saved copy" defaultOpen>
          <div className="queue">
            {self.map((it) => (
              <div className={`qrow${it.status === 'verified' ? ' qdone' : ''}`} key={it.id}>
                <span className="qico" aria-hidden="true">{it.icon}</span>
                <div className="qmain">
                  <div className="qtitle">{it.title} <span className="muted" style={{ fontSize: 12 }}>· {it.file}</span></div>
                  <div className="qmeta">{it.rule}</div>
                  <div className="selfstatus">
                    {it.status === 'awaiting' && <>
                      <span className="muted">awaiting your fix — edit the source, assess and save the updated correction, then check its evidence</span>
                      {SOURCE_URL[it.source] && <a className="ghost small" style={{ marginLeft: 8 }} href={SOURCE_URL[it.source]} target="_blank" rel="noopener noreferrer">↗ Open the file</a>}
                    </>}
                    {it.status === 'scanning' && <span className="muted"><span className="spinner" /> checking the exact saved copy…</span>}
                    {it.verificationMessage && <span className="muted" role={it.status === 'error' ? 'alert' : 'status'}>{it.verificationMessage}</span>}
                  </div>
                </div>
                {it.status === 'verified'
                  ? <span className="qbtn verified">✓ confirmed</span>
                  : <button className="qbtn rescan" disabled={reviewReadOnly || it.status === 'scanning'} onClick={() => rescan(it.id)}>↻ Check saved copy</button>}
              </div>
            ))}
          </div>
          <p className="muted" style={{ marginTop: 12 }}>The check assesses the exact saved correction. It does not approve a manual change or certify meaning automatically. Source edits must first be assessed and saved as an updated correction.</p>
        </RemSection>
      )}

      {deferredItems.length > 0 && (
        <RemSection id="rem-deferred" title="Deferred" count={deferredItems.length}
                    hint="— resurface on next scan">
          <div className="queue">
            {deferredItems.map((it) => (
              <div className="qrow" key={it.id} style={{ opacity: 0.7 }}>
                <span className="qico" aria-hidden="true">{it.icon}</span>
                <div className="qmain">
                  <div className="qtitle">{it.title} <span className="muted" style={{ fontSize: 12 }}>· {it.file}</span></div>
                  <div className="qmeta">{it.rule}</div>
                </div>
                <span className="trstatchip defer" style={{ fontSize: 12, padding: "3px 10px" }}>⏸ deferred</span>
                <button className="ghost small" onClick={() => { setDeferredItems((d) => d.filter((x) => x.id !== it.id)); setQueue((q) => [...q, it]); setActed((a) => ({ ...a, deferred: a.deferred - 1 })) }}>↺ restore</button>
              </div>
            ))}
          </div>
          <p className="muted" style={{ marginTop: 10 }}>Deferred findings are tracked in the compliance record and flagged automatically when the next scheduled scan runs.</p>
        </RemSection>
      )}

      {/* ── ADVANCED (§10) — the engine, hidden by default: live metrics, worker queue,
          the remediation plan + accept/reject decisions, business-risk graphs, the
          server-side remediation runner, and the fixed-copy write-back. For architects /
          support; the default view above is the four-decision flow. ── */}
      <details className="rem-advanced rem-adv-block" id="rem-advanced">
        <summary className="rem-adv-summary"><b>Advanced</b> · engine internals — remediation plan, worker queue, live metrics &amp; business-risk graphs</summary>
        <div className="rem-adv-body">

          <div className="metrics">
            <div className={`metric${remLive ? ' livecard' : ''}`} title="Estimated number of issues that can be fixed automatically — populates once you run remediation"><span>auto-fixable (est.)</span><b style={{ color: remStarted ? 'var(--success-fg)' : '#9AA1B4' }}>{remStarted ? autoFixed : 0}</b></div>
            <div className={`metric${remLive && queue.length > 0 ? ' livecard' : ''}`}>
              <span>HITL queue{remLive && queue.length > 0 && <span className="activedot" aria-hidden="true" style={{ marginLeft: 5 }} />}</span>
              {remStarted
              ? <><b key={queue.length} className={remStarted ? 'tick' : undefined} style={{ color: queue.length ? 'var(--warn-fg)' : 'var(--success-fg)' }}>{totalHitl === 0 ? 'no items' : `${queue.length} remaining`}</b>{totalHitl > 0 && <span className="muted" style={{ fontSize: 11 }}> · {hitlProgress}% done</span>}</>
              : <b style={{ color: '#9AA1B4' }}>—</b>}</div>
            {pureAutomated ? (
              <div className="metric" title="Fixed copies written back to the source Drive folder"><span>written to Drive</span><b key={written} className={written ? 'tick' : undefined} style={{ color: 'var(--success-fg)' }}>{written}</b></div>
            ) : (
              <>
                <div className="metric"><span>approved</span><b key={acted.approved} className={acted.approved ? 'tick' : undefined}>{acted.approved}</b></div>
                <div className="metric"><span>deferred</span><b key={acted.deferred} className={acted.deferred ? 'tick' : undefined} style={{ color: 'var(--info-fg)' }}>{acted.deferred}</b></div>
              </>
            )}
            <div className={`metric${remLive ? ' livecard' : ''}`} title="Documents fixed and re-validated against all engines — ticks in real time as the worker queue completes each file">
              <span>re-verified{remLive && <span className="livedot">live</span>}</span>
              <b key={reVerified} className={reVerified ? 'tick' : undefined} style={{ color: 'var(--success-fg)' }}>{reVerified.toLocaleString()}</b>
            </div>
          </div>

          <QueuePanel />

          {/* Write-back results — proof the fixed copies landed in Drive / Blob. */}
          {(written2.length > 0 || downloadOnly.length > 0) && (
            <div style={{ margin: '12px 0 14px', padding: '10px 14px', borderRadius: 9,
                          background: 'var(--success-bg)', border: '1px solid #C5DBA8',
                          display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--success-fg-strong)' }}>
                ✓ {written2.length} fixed document{written2.length !== 1 ? 's' : ''} written back to Drive
                {downloadOnly.length > 0 && ` · ${downloadOnly.length} remediated (no Drive write)`}
              </span>
              <span style={{ display: 'flex', gap: 10, flexWrap: 'wrap', alignItems: 'center' }}>
                {written2.slice(0, 6).map((f) => (
                  <a key={f.file} href={f.drive_write_url} target="_blank" rel="noreferrer"
                     style={{ fontSize: 12, color: '#185FA5' }} title={`Open ${f.file} in the Remediated/ folder`}>
                    {f.file} ↗
                  </a>
                ))}
                {written2.length > 6 && <span className="muted" style={{ fontSize: 12 }}>+{written2.length - 6} more</span>}
                {downloadOnly.slice(0, 6).map((f) => (
                  <button key={f.file} className="ghost small" style={{ fontSize: 12 }}
                          title={`Download the fixed copy of ${f.file} (stored in Blob)`}
                          onClick={() => downloadRemediated(runId, f.file)}>
                    ⤓ {f.file}
                  </button>
                ))}
                {downloadOnly.length > 6 && <span className="muted" style={{ fontSize: 12 }}>+{downloadOnly.length - 6} more</span>}
              </span>
            </div>
          )}

          {/* The "Remediation plan" band (bulk "Auto-fix N" / "Accept full plan" buttons + the plan-card
              grid) was retired here — it was the second bulk decision surface. Accepting or auto-fixing
              work now happens per finding in the RemediationInbox above, and server-side remediation runs
              the whole remediable set from the runner below; neither depended on this band. */}

          {flagged.length > 0 && (
            <div className="prioritypanel">
              <div className="priorityhd"><b>Business priority</b> <span className="muted">· what to fix first — weighted by exposure, severity &amp; ownership</span></div>
              <div className="prioritynote">⚑ {pubCrit} public-facing document{pubCrit === 1 ? '' : 's'} ha{pubCrit === 1 ? 's' : 've'} critical findings and {execFlagged} are executive-owned — the highest business risk under ADA / EAA. Start here.</div>
              <div className="prioritygrid">
                <section className="ppanel"><h3>Open findings by department</h3><Bars items={deptData} cols="118px 1fr 28px" onPick={(it) => drill(`${it.label} · open findings`, `${it.value} findings`, (f) => f.department === it.label)} /></section>
                <section className="ppanel"><h3>By owner seniority</h3><Bars items={senData} cols="92px 1fr 28px" onPick={(it) => drill(`${it.label}-owned · open findings`, `${it.value} findings`, (f) => f.seniority === it.label)} /><div className="muted ppfoot">executive / director-owned content carries more reputational weight</div></section>
                <section className="ppanel"><h3>By exposure</h3><Bars items={expData} cols="98px 1fr 28px" onPick={(it) => drill(`${it.label} · open findings`, `${it.value} findings`, (f) => exposureOf(f) === it.label)} /><div className="muted ppfoot">public-facing pages are the top legal-exposure set</div></section>
              </div>
            </div>
          )}

          {/* The legacy file-level "Documents to remediate" accept/reject/modify table was retired here:
              per-finding accept / reject / assign decisions now live solely in the RemediationInbox above
              (the single decision surface). Server-side remediation still runs the whole remediable set from
              the runner below and the hero CTA; it never consumed the file-level decisions. */}

          {/* Server-side remediation runner. */}
          <div className="remcta">
            <div className="remcta-label">
              {dcount('accepted') + dcount('override') > 0
                ? <span>✓ <b>{dcount('accepted') + dcount('override')}</b> file{(dcount('accepted') + dcount('override')) !== 1 ? 's' : ''} accepted — ready to remediate</span>
                : <span className="muted">Accept files above, then run remediation</span>}
              {/* A refusal is not chatter. "Nothing to remediate — of 3 documents: 3 already
                  remediated." answers the question the click asked, and must read like an
                  answer; only success stays green, only progress stays muted. */}
              {remMsg && (
                <span role="status" aria-live="polite"
                      style={{ marginLeft: 12,
                               color: remMsg.startsWith('✓') ? 'var(--success-fg)'
                                    : remMsg.startsWith('Nothing to remediate') ? '#8A4B00'
                                    : 'var(--muted)',
                               fontWeight: remMsg.startsWith('Nothing to remediate') ? 600 : undefined }}>
                  {remMsg}
                </span>
              )}
            </div>
            {!resultsOnly && <button disabled={remBusy || !runId || readOnly} onClick={openRemediationPlan}
                    title="Review permissions and impact before starting remediation."
                    style={{ flexShrink: 0 }}>
              {remBusy ? '⏳ Enqueueing…' : planAccepted ? 'Show progress' : 'Start remediation'}
            </button>}
            {(serverFixed > 0 || remProg) && <TraceChip scanId={runId} kind="session" label="View scan traces" />}
          </div>

          <ProcessingStatusPanel derived={deriveRemediateProcessingState({ remBusy, remProg, pickupEstimate, updateMode: remUpdates })} />

          {remProg && (
            <div style={{ margin: '4px 0 14px', maxWidth: 560 }} role="status" aria-live="polite">
              <div style={{ height: 9, borderRadius: 6, background: 'var(--line)', overflow: 'hidden' }}>
                <i style={{ display: 'block', height: '100%',
                            width: `${Math.round((remProg.done / Math.max(1, remProg.total)) * 100)}%`,
                            background: '#BF8C00', transition: 'width .35s' }} />
              </div>
              <div className="muted" style={{ fontSize: 12.5, marginTop: 6 }}>
                ⚡ <b>Live</b> · remediating {remProg.done.toLocaleString()} of {remProg.total.toLocaleString()} files in real time
                {remProg.latest ? <> · last fixed <span className="fname">{remProg.latest}</span></> : '…'}
                {remProg.failed ? ` · ${remProg.failed} failed` : ''}
              </div>
              {remProg.activity?.text && (
                <div style={{ fontSize: 12.5, marginTop: 5 }}>
                  <span className="pulsedot" aria-hidden="true" /> {remProg.activity.text}
                </div>
              )}
            </div>
          )}

          {fixTypesDisplay.length > 0 && (
            <div style={{ marginTop: 14 }}>
              <div className="advh3">Automated fixes by type</div>
              <Bars items={fixTypesDisplay} cols="140px 1fr 30px" />
            </div>
          )}

          <p className="muted" style={{ fontSize: 12, marginTop: 14 }}>
            Remediated files are stamped <b>_a11y-certified-{new Date().toISOString().split('T')[0]}</b> · originals kept for the audit trail · always written to <b>Azure Blob</b> (download from each file’s drawer), with an optional Drive mirror (Settings → Remediated storage).
          </p>
        </div>
      </details>
    </>
  )

  const reviewWorkspace = (
    <>
      {/* ── HUMAN REVIEW (§3) — the only section that needs interaction, so it dominates,
          directly under the hero. Each card carries its own badge (§4) and a
          "Why am I reviewing this?" panel (real confidence + reason + suggested value). ── */}
      <section className="panel rem-review-panel" id="rem-review">
        <p className="muted rem-review-scope">{documentScopeSentence(documentSelection(files, triage))}</p>
        <div className="rem-sec-hd">
          {/* Redesign R4: one dominant statement (findings × documents) replaces the repeated `N`
              badges. The numeric pill is gone — the count lives in the sentence, said once. */}
          <div>
            <h2 style={{ margin: 0 }}>Review queue</h2>
            {reviewCounts.pendingItems > 0
              ? <p className="rem-review-lead" style={{ margin: '2px 0 0', fontSize: 13 }}>
                  <b>{reviewCounts.pendingItems}</b> review item{reviewCounts.pendingItems === 1 ? '' : 's'} require attention across{' '}
                  <b>{reviewCounts.documents}</b> document{reviewCounts.documents === 1 ? '' : 's'}
                  {/* One review item is one (document, criterion) pair and can cover several
                      findings, so the item count and the run's finding count are different
                      numbers. Said here when they differ, because the alternative is a reader
                      comparing 12 cards against 13 findings in a log and concluding one was
                      lost. */}
                  {reviewCounts.findings > reviewCounts.pendingItems && <>
                    {' · covering '}<b>{reviewCounts.findings}</b> finding{reviewCounts.findings === 1 ? '' : 's'}
                  </>}
                  {/* Applied fixes awaiting the reviewer's confirmation are deliberately OUT of this
                      count and out of the nav badge (#1888 — a run with 2000 of them would drown the
                      346 items that need a decision). They are in the Needs-review LIST, though, so
                      leaving them unsaid is what made the headline and the tab disagree. Said here,
                      quietly, rather than folded into a number that means something else. */}
                  {reviewCounts.inspection > 0 && <span className="muted">
                    {' · '}{reviewCounts.inspection} applied change{reviewCounts.inspection === 1 ? '' : 's'} to confirm
                  </span>}
                </p>
              // NOT unconditionally "All clear": an unreadable document is not a clear one, and
              // the reader who sees "All clear" stops reading (reviewQueueCopy.js).
              // And not on the human count alone either: status checks, processing, saved changes
              // awaiting their outcome and server-reported unresolved findings all withhold it. The
              // explanation's headline is the only sentence allowed to say it (reviewPopulationExplanation.js).
              : <p className="muted" style={{ margin: '2px 0 0', fontSize: 13 }}>{hasRemediationResults ? reviewLeadLine(files, reviewCounts.pendingItems, reviewExplanation) : 'Run the plan to generate fixes. Review items appear when there is a proposal or an exception to handle.'}</p>}
          </div>
          {reviewProgress.total > 0 && (
            <div className="rem-sec-prog">
              <div className="conftrack" style={{ width: 120 }}><i style={{ width: `${reviewPct}%`, background: reviewPct === 100 ? 'var(--success-fg)' : 'var(--info-fg)' }} /></div>
              {/* One denominator, two named measures: a recorded decision is not a final outcome,
                  and a saved change awaiting its re-check is counted as awaiting, never as done.
                  The inbox prints the same two labels from the same explanation. */}
              <span className="muted rem-review-progress" title={reviewProgress.definition}>{reviewProgress.decidedLabel} · {reviewProgress.finishedLabel}</span>
            </div>
          )}
          {/* The remediation report, in the same three modes as the document and scan reports
              (Summary / Reviewer packet / Full evidence). It is mounted HERE, in the Review queue
              header, because this is the section a reviewer is looking at when they want the
              record — and because the section that used to hold it is never rendered. */}
          <div className="rem-report-exports" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <ReportModeMenu label="Remediation report" disabled={!run?.id || reportBusy}
                            disabledReason={run?.id ? null : 'Select a remediation run first.'}
                            progress={reportProgress && `Reading report evidence… ${reportProgress.loaded}${reportProgress.total != null ? ` of ${reportProgress.total}` : ''} document(s)`}
                            formats={[{ key: 'pdf', label: 'PDF', run: runRemediationReport }]} />
            <button className="exportbtn" onClick={downloadRemediationCsvReport} disabled={reportBusy}
                    title="The same changes as a machine-readable CSV — one row per document × criterion, for pulling into your own tracker">
              {reportBusy ? 'Generating…' : '⤓ Changes (CSV)'}
            </button>
            {reportErr && <span style={{ fontSize: 12, color: 'var(--error-fg-strong)' }} role="alert">⚠ {reportErr}</span>}
          </div>
          {/* Reviewer analytics (vision #39) — real counts from hitl_events, not a fabricated score:
              approval rate, how often the reviewer edited the AI draft (the calibration signal), and
              the average review time already computed by measuredReviewTime. */}
          {reviewStats && reviewStats.reviewed > 0 && (
            <div className="rev-analytics" style={{ display: 'flex', gap: 14, flexWrap: 'wrap', fontSize: 12, marginTop: 6, color: 'var(--muted)' }}>
              {reviewStats.approval_rate != null && (
                <span>Approval rate <b style={{ color: 'var(--ink)' }}>{Math.round(reviewStats.approval_rate * 100)}%</b></span>
              )}
              {reviewStats.edit_rate != null && (
                <span title="how often a reviewer edited the AI draft before approving — a calibration signal, not a confidence score">
                  AI draft edited <b style={{ color: 'var(--ink)' }}>{Math.round(reviewStats.edit_rate * 100)}%</b>
                </span>
              )}
              {measured && <span title={measured.basis}>Median review <b style={{ color: 'var(--ink)' }}>{measured.median}</b></span>}
            </div>
          )}
          {/* AI Quality (feedback intelligence): which rules are weakest + WHY rejections happen —
              real reviewer-decision counts, weakest-first, never a fabricated score. Renders only
              once there is signal (a rejection, or 3+ reviewed on some rule). */}
          {reviewStats && ((reviewStats.by_rule || []).some((r) => r.rejected > 0 || r.reviewed >= 3)) && (
            <div className="rev-quality" style={{ marginTop: 8, fontSize: 12, color: 'var(--muted)' }}>
              <span style={{ fontWeight: 700, color: 'var(--ink)' }}>AI quality · weakest rules first:</span>{' '}
              {(reviewStats.by_rule || []).filter((r) => r.reviewed > 0).slice(0, 4).map((r, i) => (
                <span key={r.key} title={Object.entries(r.reject_reasons || {}).map(([k, n]) => `${k.replace(/_/g, ' ')}: ${n}`).join(' · ') || 'no rejections'}>
                  {i > 0 && ' · '}
                  <b style={{ color: r.rejected > 0 ? 'var(--error-fg-strong)' : 'var(--ink)' }}>{r.key}</b>
                  {' '}{r.approved}✓{r.rejected > 0 && <span style={{ color: 'var(--error-fg-strong)' }}> {r.rejected}✕</span>}
                </span>
              ))}
              {Object.keys(reviewStats.reject_reasons || {}).length > 0 && (
                <span> — top reject reasons: {Object.entries(reviewStats.reject_reasons)
                  .sort((a, b) => b[1] - a[1]).slice(0, 3)
                  .map(([k, n]) => `${k.replace(/_/g, ' ')} (${n})`).join(', ')}</span>
              )}
            </div>
          )}
          {/* Automation maturity signal (ADR 0019 §8.5): rules whose reviewer edit-rate and
              approval-rate pass the three-threshold gate are surfaced as promotion candidates.
              This is evidence from real reviewer decisions, never a fabricated confidence score. */}
          {reviewStats && (reviewStats.promotable_rules || []).length > 0 && (
            <div className="rev-maturity" style={{ marginTop: 8, fontSize: 12 }}>
              {(reviewStats.promotable_rules).map((ruleKey) => (
                <span
                  key={ruleKey}
                  title={`Rule ${ruleKey} has ≥10 approvals, ≤20% edit rate, and ≥90% approval rate — consistently low reviewer intervention. Consider migrating this criterion to AI-Assisted mode in Settings → Automation.`}
                  style={{
                    display: 'inline-flex', alignItems: 'center', gap: 4,
                    background: 'var(--success-bg, #EDF7EE)', color: 'var(--success-fg, #1A6B2A)',
                    border: '1px solid var(--success-border, #A3D9A8)',
                    borderRadius: 12, padding: '2px 8px', marginRight: 6, cursor: 'default',
                  }}
                >
                  ↑ {ruleKey} ready for AI-Assisted
                </span>
              ))}
            </div>
          )}
        </div>
        {/* A decision the server refused. It rolled back, so the card is in the queue again —
            say so loudly, because a reviewer who thinks they signed something off and did not
            is the worst outcome this screen can produce. */}
        {/* What is still open, by name: each remaining task and each server-reported unresolved
            finding, with the tab that holds it and a button that opens it (C5). Renders nothing
            when the explanation is all clear. */}
        {hasRemediationResults && <RemainingFindingsExplainer explanation={reviewExplanation} onOpenItem={openReviewItem} showHeadline={false} />}
        {/* C8 — settle an already-completed run on request: re-check the open items against the
            recorded check of the saved copy. Offered only while something is still open, and not
            on a historical (time-travel) scan. On success the queue and the stage snapshot are both
            re-read, so the panel above and the tiles reflect the server's new ledger. */}
        {hasRemediationResults && runId && !reviewReadOnly && <ReconcileReviewTargets scanId={runId}
          available={!reviewExplanation.allClear && (reviewExplanation.remaining.length > 0
            || reviewExplanation.unmatchedFindings.length > 0 || !!reviewExplanation.unlistedFindings)}
          onReconciled={() => {
            window.dispatchEvent(new CustomEvent('acp:hitl-changed', { detail: { scanId: runId } }))
            window.dispatchEvent(new CustomEvent(STAGE_LINEAGE_REFRESH_EVENT, { detail: { scanId: runId } }))
          }} />}
        <ReviewRefreshNotice error={reviewRefreshError} onRetry={refreshReviewQueue}/>
        {actError && (
          <p role="alert" className="rem-act-error"
             style={{ margin: '0 0 12px', padding: '10px 12px', borderRadius: 8, fontSize: 13,
                      background: '#FDECEC', color: '#8A1F1F', border: '1px solid #E9A8A8' }}>
            {actError}
          </p>
        )}
        {/* Both branches of reviewEmptyLine are true about the QUEUE and incomplete about the
            ESTATE, so the "N could not be analysed" caveat is appended to whichever one renders
            rather than living inside one of them — which is how the original defect happened. */}
        {inboxQueue.length === 0 ? (
          <div className="remediation-complete" role="status">
            <h3>{hasRemediationResults ? 'No items awaiting review.' : 'No fixes to review yet.'}</h3>
            <p className="muted">{hasRemediationResults ? `No generated proposals or remediation exceptions are currently queued. ${unreadableCaveat(files)}`
              : 'Run the plan first. Eligible generated fixes proceed automatically unless you select Review before applying.'}</p>
            <div className="remediation-complete-counts" aria-label="Remediation completion summary">
              <span><b>{acted.approved || 0}</b> approved</span>
              <span><b>{revalidated.length}</b> verified</span>
              <span><b>{acted.deferred || 0}</b> manual or deferred</span>
              <span><b>{blockedCount || 0}</b> blocked</span>
            </div>
            {!files.some(file => file.compliant && file.remediated_at && file.corrected_sha256) && <p className="muted remediation-release-blocked">No verified corrected copy is available for Release yet.</p>}
          </div>
        ) : (
          // R4, R7 and R10 ride in the detail pane, beside the finding they describe. `sel` is
          // null when nothing is selected and each component self-guards on that, so an empty
          // selection renders an empty pane rather than three frames of nothing.
          //
          // A LINE comment, not {/* */}: this is an expression position, not a children position,
          // and a JSX comment here is a parse error. Second time tonight.
          <RemediationInbox
            readOnly={reviewReadOnly}
            autoApprove={runAiApproval.enabled}
            automaticApprovalPolicy={runAiApproval.policy}
            onAutoApproveChange={reviewReadOnly ? undefined : (...args) => { if (!reviewReadOnlyRef.current) return runAiApproval.change(...args) }}
            autoApproveSaving={runAiApproval.saving}
            autoApproveError={runAiApproval.error}
            approvalExplanation={runAiApproval.explanation}
            afterRelease={resultsOnly && !reviewReadOnly}
            onAutoApproveRetry={runAiApproval.retry}
            autoApproveNotice={runAiApproval.notice} onDismissAutoApproveNotice={runAiApproval.dismissNotice}
            onPublish={reviewReadOnly ? undefined : () => onNavigate?.('publish')}
            onOpenPlan={readOnly ? undefined : openRemediationPlan}
            preparingProposals={!runStream?.snapshot?.terminal && ((runStream?.status?.running ?? remProg?.running ?? 0) > 0 || (runStream?.status?.queued ?? remProg?.queued ?? 0) > 0)}
            onVerifySaved={reviewReadOnly ? undefined : verifySaved}
            onRetryApproved={reviewReadOnly ? undefined : retryApprovedFix}
            renderDetailExtra={(sel) => (sel ? (
              <>
                {/* R15 · only for a row ACP applied itself — a drafted-AI or manually-authored
                    finding was never something ACP claimed to fix on its own, so there is
                    nothing here to un-claim for those rows. */}
                {!reviewReadOnly && sel.autoApplied && !sel.inspectionOnly && (
                  <UndoFix scanId={sel.scanId || run?.id} file={sel.file} ruleId={sel.ruleId}
                           onUndone={onRefresh} />
                )}
                {sel.inspectionOnly && <DocumentAudit scanId={sel.scanId || run?.id} file={sel.file} />}
                <ReviewDetails key={sel.id}>
                {!sel.inspectionOnly && <DocumentAudit scanId={sel.scanId || run?.id} file={sel.file} />}
                <DueDate scanId={sel.scanId || run?.id} file={sel.file}
                         value={decisions[sel.file]?.due_date || ''}
                         assignee={decisions[sel.file]?.assignee || ''} />
                <FindingComments scanId={sel.scanId || run?.id} finding={sel} />
                </ReviewDetails>
              </>
            ) : null)}
            queue={inboxQueue}
            decisions={inboxDecisions}
            explanation={reviewExplanation}
            requestedSelection={reviewItemRequest}
            scanId={run?.id}
            onDecide={(f, d) => {
              if (reviewReadOnlyRef.current) return Promise.reject(new Error('Historical scans are available for results browsing only.'))
              // W2 — a handoff row (a rejected AI fix) is already out of the hitl queue; acting on it
              // here ("Mark as assigned") just clears it from the needs-manual-handling lane. It is
              // owned by a person now — this is the acknowledgement that they have it.
              if (f.rejectedFix) { setRejectedItems((r) => r.filter((x) => x.id !== f.id)); return Promise.resolve() }
              // Auto-applied (green) rows are already applied + re-scanned — "Approve" acknowledges
              // them locally (resolve + advance); the human review lanes route to the hitl flow.
              if (f.autoApplied) { setAckd((a) => ({ ...a, [f.id]: d })); return Promise.resolve() }
              // d.value carries a reviewer-EDITED proposed value (the "Save edited fix" flow); fall
              // back to the AI's proposal when they didn't touch it. act() writes it to the document.
              // Every branch RETURNS act()'s promise. The review pane awaits it and only advances to
              // the next finding once the write has actually landed — see act() above.
              if (d.state === 'accepted') return act(f.id, 'approved', d.value ?? f.after ?? null, d.approvedValues, null, d.selectionFingerprint ? { finding: f, decision: d } : null)
              if (d.state === 'rejected') return act(f.id, 'rejected')
              if (d.state === 'assigned') return act(f.id, 'deferred')
              // Not applicable / out of scope: resolved as approved-with-no-value + an out_of_scope
              // resolution, so it never blocks certification and leaves the coverage denominator.
              if (d.state === 'not_applicable') return act(f.id, 'approved', null, undefined, 'out_of_scope')
              return Promise.resolve()
            }}
            assignees={assignees}
            myEmail={myEmail}
            onAssign={(file, email) => {
              if (readOnly || resultsOnlyRef.current) return
              setAssignees?.((a) => {
                const next = { ...a }
                if (email) next[file] = email; else delete next[file]
                return next
              })
              // Persist to DB for every pending item that belongs to this file
              queue.filter((it) => it._raw?.file === file).forEach((it) => {
                assignHitlItem(it._raw.id, email || null).catch(() => {})
              })
            }}
          />
        )}
      </section>
    </>
  )

  // Run details (PRD §11). `alert` hoists a section OUT of the disclosure so it stays on screen
  // while collapsed — used only for failures the reviewer must not have to go looking for.
  const runDetailSections = [
    { id: 'rd-outcomes', title: 'Documents with no corrected copy',
      hint: 'Failed, unreadable, unverified or refused — with the reason recorded for each.',
      alert: (remProg?.failed || 0) > 0, children: fixFailures },
    { id: 'rd-work', title: 'How the work divides',
      hint: 'What ACP applies deterministically, what it drafts for you, and what only a person can author.',
      defaultOpen: true, children: workLanes },
    { id: 'rd-summary', title: 'Run summary', children: runSummaryDetail },
    { id: 'rd-scope', title: 'Assessment scope', children: scopeRecord },
    { id: 'rd-reference', title: 'Verification, documents and engine detail', children: referenceSections },
  ]

  return (
    <>
      {planAccepted && <>
        <RemediationAutomationLayout progressHostId={progressHostId} scanId={runId} batchId={scopedSnapshot?.batch_id}
          policy={runAiApproval.policy} error={runAiApproval.error} saving={runAiApproval.saving}
          reviewCount={reviewCounts.pendingItems} statusCheckCount={reviewExplanation.statusCheckCount} onOpenReview={() => setWorkspaceRequest({ mode: 'review' })} onRetry={runAiApproval.retry}
          authorization={acceptedAuthorization} publicationPending={acceptedAuthorization === undefined && !automaticReleaseState?.error}
          publicationError={automaticReleaseState?.error} destinationLabel={acceptedAuthorization?.destination?.provider} />
      </>}
      <RemediationWorkspaceTabs
        assessmentReady={!readOnly && !assessRunning && files.length > 0 && !!assessedAt}
        assessmentIdentity={runId && assessedAt ? `${runId}:${assessedAt}` : runId}
        planAccepted={planAccepted}
        reviewOptional={acceptedAuthorization?.allow_remaining_issues === true && ['active', 'waiting', 'publishing', 'blocked', 'completed'].includes(acceptedAuthorization?.status)}
        runId={runId}
        workspaceRequest={workspaceRequest}
        plan={<>
          <RemediationImpactCard requireAnswers automaticRelease={!!releasePlanIntent} releaseAnswered={releaseAnswered} key={`${runId || 'current'}:${planRevision}`} runId={runId}
            runBusy={remBusy} readOnly={readOnly} myEmail={myEmail}
            assessmentTotal={assessMetrics(files, { cap, assessment }).totalFindings}
            scopeFiles={impactScope.map(file => file.file)}
            refreshKey={`${fixedCount}:${reviewCount}:${remBusy}`}
            renderAssessment={forecast => <AssessSummary files={files} cap={cap} assessment={assessment}
              assessedAt={assessedAt} run={run} notStarted={run?.not_assessed?.count}
              remediationForecast={forecast} reviewSummary={reviewCounts}
              onOpenReview={() => setWorkspaceRequest({ mode: 'review' })} />}
            releaseOption={<RemediationReleasePlan compact requireChoice onAnswered={setReleaseAnswered} scanId={runId} files={impactScope.map(file => file.file)}
              intent={releasePlanIntent} onChange={setReleasePlanIntent} disabled={readOnly || remBusy} />}
            onRun={readOnly ? undefined : (policy) => {
              const intent = releasePlanIntent
              return runServerRemediation(impactScope, policy, intent)
            }} />
          {remMsg && <div role="status">{remMsg}</div>}
          {releasePlanNotice && <div role="status">{releasePlanNotice}</div>}
        </>}
        reviewCount={reviewCounts.pendingItems}
        snapshot={runStream?.snapshot || null}
        review={reviewWorkspace}
        live={<>
          {runStream?.snapshot?.scan_id === runId && <RemediationActivityPanel
            snapshot={runStream.snapshot} activity={runStream.status?.activity || null} rows={inboxQueue} decisions={inboxDecisions} automatic={runAiApproval.enabled === true} events={runStream.events || []} connected={!!runStream.connected}
            receivedAt={runStream.receivedAt || null} activityStatus={runStream.activityStatus || 'loading'}
            updateMode={runStream.connected ? 'live' : 'polling'} />}
          {/* App owns the live Assessment card above the workflow tabs. This compact label only
              qualifies the older Remediation snapshot below; it does not compete with that card. */}
          {showPriorResultsNotice && (
            <div className="rem-prior-results" role="status">
              <strong>Previous remediation results · read only</strong>
              <span>The results below are from{assessedAt ? ` ${assessedAt}` : ' the previous assessment'} and will refresh after the active assessment completes.</span>
            </div>
          )}

          {/* The automation-first run header (PRD §5.1/§5.2): what ACP already did, what is left for a
              person, and the ONE action this state of the run calls for. Counts come from the same
              derivations the panels under Run details use, and a lane with no data passes nothing rather
              than a zero, so "none" and "not known" never read the same. */}
          {/* The summary card is retired after launch; Live and document status own progress. */}
          {remediationHasStarted && <p className="muted rem-live-scope">{documentScopeSentence(documentSelection(files, triage))}</p>}
          {!remediationHasStarted && <RemediationRunHeader
            assessedAt={assessedAt}
            docScope={documentScopeSentence(documentSelection(files, triage))}
            counts={{ automaticOnly: false, autoFixed: fixTotal ?? undefined, autoFixedLoaded: fixSource.length, documents: fixDocumentTotal ?? undefined,
              needsApproval: reviewCounts.ready, individualReview: reviewCounts.individual, inspection: reviewCounts.inspection,
                      manual: reviewCounts.manual, revalidating: revalidatingCount, blocked: blockedCount }}
            primary={primary}
            readOnly={readOnly}
            />}
          {planAccepted && <details className="panel" aria-label="Saved automation settings">
            <summary>Saved automation plan · repairs and verification continue automatically</summary>
            <AcceptedRemediationPlanSummary
              policy={acceptedPlan?.scanId === runId && acceptedPlan?.batchId === acceptedBatchId ? acceptedPlan.policy : null}
              loading={acceptedPlan?.loading === true}
              authorization={acceptedAuthorization} />
          </details>}
          <AutomaticReleasePackage scanId={runId} authorization={acceptedAuthorization} />
          {/* Embedded publication and run-detail panels deliberately retired; Release owns delivery. */}
          {releasePlanNotice && <div role="status">{releasePlanNotice}</div>}
          {remMsg && <div role="status">{remMsg}</div>}
          <RemediationLiveDocuments progressHostId={progressHostId} onShowDocuments={() => setWorkspaceRequest({ mode: 'live' })} snapshot={scopedSnapshot} events={runStream?.events || []} connected={!!runStream?.connected} key={runId} scanId={runId} files={impactScope} cap={cap} assessment={assessment}
            fixes={fixSource} fixTotal={fixTotal} refreshKey={`${fixedCount}:${reviewCount}:${remBusy}`} />
        </>}
        waterfall={<>
          {/* The large panel consumes the App-owned controller. Mounting this view opens no
              stream of its own, so the compact card, global card and panel stay on one cursor. */}
          <RemediationOpsPanel streamlined hideActivity snapshot={runStream?.snapshot || null}
                               assessmentContext={{ files, cap, assessment, scanId: runId, runStatus: run?.status }}
                               connected={!!runStream?.connected}
                               receivedAt={runStream?.receivedAt || null}
                               events={runStream?.events || []}
                               activityStatus={runStream?.activityStatus || 'loading'}
                               updateMode={remUpdates} />

        </>} />
      {seg && <SegmentDrawer title={seg.title} subtitle={seg.subtitle} files={seg.files} onClose={() => setSeg(null)} onPickFile={(f) => { setSeg(null); setSel(f) }} />}
      {sel && <FileDrawer file={sel} context="remediate" aiEnabled={aiEnabled} scanId={run?.id} readOnly={readOnly} onClose={() => setSel(null)} />}
      {selItem && <ReviewDrawer item={selItem} onClose={() => setSelItem(null)} onAct={act} onDraft={!reviewReadOnly && selItem.aiDraftable ? draftAi : null} />}
    </>
  )
}
