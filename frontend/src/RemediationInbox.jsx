import QualityReviewEvidence from './QualityReviewEvidence.jsx'
import { matchesAutomaticReview, automaticReviewResponsibility } from './automaticReviewResponsibility.js'
import { isPdfStructuralRow, pdfStructuralSummary, proposalsFor, requiresPdfSourceEditing } from './pdfStructuralProposal.js'
import { automaticReviewQueue } from './automaticReviewQueue.js'
import { useMemo, useState, useEffect, useRef } from 'react'
import {
  rowModel, laneOf, sortQueue, groupByDocument, nextUnresolvedId, progress, railColorOf,
  matchesWorkflow, workflowCounts, workflowStatusOf, workflowStepIndex, isResolved, isAiAssistedDraft,
  WORKFLOW_TABS, WORKFLOW_LABELS, SORTS, optionalInspectionOf, recordedReviewDecision,
} from './remediationInboxModel.js'
import { clusterRows, clusterOfFinding, batchTargetsOf } from './remediationClusters.js'
import { fixSteps, appName } from './remediationGuide.js'
import { scOf } from './fixSummary.js'
import { aiAppliedUnverified } from './remediationCategories.js'
import { changeSentence, isContrastFinding } from './remediationEvidence.js'
import WorkspaceProgress from './WorkspaceProgress.jsx'
import WorkspaceFooter from './WorkspaceFooter.jsx'
import './RemediationInbox.css'
import CompletionDrain from './CompletionDrain.jsx'
import ReviewQueueTabs from './ReviewQueueTabs.jsx'
import MatchingReviewPreview from './MatchingReviewPreview.jsx'
import BatchReviewSelection from './BatchReviewSelection.jsx'
import { remediationReviewCounts } from './remediationCountSummary.js'
import { exclusionReason } from './batchReviewSelection.js'
import { reviewQueueAction, reviewQueueActions } from './reviewQueueAction.js'
import { remediationRecoveryGuidance } from './remediationRecoveryGuidance.js'
import RemediationSourceLink from './RemediationSourceLink.jsx'

// Master/detail Remediation inbox. Remediation is queue work — select an item, understand it, act,
// move to the next — so the layout is a TWO-column split: a 35% work queue on the left to find and
// choose the next finding, and a 65% remediation WORKSPACE on the right that stacks, in one scrolling
// column, everything needed to finish it — Problem → Evidence → How to fix → Decision. Full-document
// viewing is an explicit action instead of a persistent third pane. Selecting a row NEVER expands
// it; it populates the workspace. Acting
// on a finding auto-advances to the next unresolved one, which is what makes the whole thing feel
// fast. All derivation lives in remediationInboxModel.js; this file is presentation.

const SORT_LABEL = { priority: 'Priority', document: 'Document', newest: 'Newest', fastest: 'Fastest to resolve' }
const fmtOf = (file) => String(file || '').split('.').pop().toLowerCase()
// The success-criterion key a finding shares with its siblings, used to batch a decision across
// every other queued finding of the same rule (W8). Normalised so 'SC_1_1_1' / 'WCAG 1.1.1' / '1.1.1' all match.
const scKeyOf = (f) => scOf(f?.rule_id || f?.ruleId || f?.wcag)

const WHY_BY_SC = {
  '1.1.1': 'Text alternatives let screen-reader users understand images and other non-text content.',
  '1.3.1': 'Programmatic structure helps assistive technology identify headings, lists, tables, and relationships.',
  '1.3.2': 'A meaningful reading order ensures content makes sense when it is read aloud or navigated without its visual layout.',
  '1.4.3': 'Sufficient contrast makes text easier to read for people with low vision and in difficult viewing conditions.',
  '2.4.2': 'A descriptive document title helps people identify the document and distinguish it from other open content.',
  '2.4.4': 'Descriptive link text helps people understand a link’s destination without relying on surrounding context.',
  '3.1.1': 'The correct document language helps screen readers pronounce and interpret the content accurately.',
  '3.1.2': 'Correct language metadata helps screen readers pronounce passages written in another language.',
  '4.1.2': 'Accessible names and roles let assistive technology identify controls and explain how to use them.',
}

function LaneRail({ lane }) {
  return <span aria-hidden="true" style={{ flex: '0 0 4px', alignSelf: 'stretch', borderRadius: 4, background: railColorOf(lane) }} />
}

function Meta({ row }) {
  // Quiet metadata — WCAG, page, confidence, effort — never competing with the task heading.
  return (
    <div className="muted" style={{ display: 'flex', flexWrap: 'wrap', gap: 12, fontSize: 12, marginTop: 6 }}>
      {row.wcag && <span>WCAG {row.wcag}</span>}
      {row.location && <span>{row.location}</span>}
      {row.confidence != null && <span>Confidence {Math.round(row.confidence * 100)}%</span>}
      {row.effort && row.effort !== '—' && <span>{row.effort}</span>}
    </div>
  )
}

// Detector payloads occasionally contain serialized HTML entities. They are data, not markup, so
// decode the small HTML entity surface we display without using dangerouslySetInnerHTML.
function displayText(value) {
  return String(value ?? '')
    .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n)))
    .replace(/&#x([\da-f]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
    .replace(/&nbsp;/gi, '\u00a0').replace(/&amp;/gi, '&').replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>').replace(/&quot;/gi, '"').replace(/&apos;|&#39;/gi, "'")
}

const EXCERPT_LIMIT = 280
const excerptOf = value => value.length > EXCERPT_LIMIT ? `${value.slice(0, EXCERPT_LIMIT).trimEnd()}…` : value

function problemOf(f, issue) {
  if (isPdfStructuralRow(f)) return 'ACP found an existing PDF tag that can be repaired without replacing document text.'
  if (f.problemStatement) return excerptOf(displayText(f.problemStatement))
  const before = displayText(f.before || f.observed || '')
  const after = displayText(f.after || '')
  if (before && after && before.length + after.length < 180) return `ACP found ${before} where ${after} is recommended.`
  return `ACP found an issue with ${issue.toLowerCase()} in this document.`
}

function whyOf(f) {
  return displayText(f.whyMatters || f.rationale || WHY_BY_SC[scKeyOf(f)]
    || 'Correcting this issue helps people using assistive technology understand and use the document.')
}

// Highlight only the characters that changed. The full value remains in the accessible label, so
// screen readers receive a clean comparison rather than punctuation around separate fragments.
function ChangedValue({ from, to }) {
  const a = displayText(from)
  const b = displayText(to)
  if (!a || !b || a === b) return <>{b || 'Not recorded'}</>
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start += 1
  let end = 0
  while (end < a.length - start && end < b.length - start && a[a.length - 1 - end] === b[b.length - 1 - end]) end += 1
  const prefix = b.slice(0, start)
  const changed = b.slice(start, b.length - end || undefined)
  const suffix = end ? b.slice(-end) : ''
  return <>{prefix}{changed && <mark className="remediation-change">{changed}</mark>}{suffix}</>
}

// `showFile` is false for rows sitting under a document group header (the header already names the
// file) and true for a standalone single-finding row (no header, so the row carries the filename).
// Either way the filename appears exactly once on screen for a given finding.
function QueueRow({ f, decisions, selected, onSelect, showFile = true, automatic = false }) {
  const action = reviewQueueAction(f, decisions, automatic)
  const r = rowModel(f, decisions)
  const railed = railColorOf(r.lane)
  const subline = showFile ? `${r.file}${r.location ? ` · ${r.location}` : ''}` : r.location
  return (
    <button
      type="button"
      id={`rinbox-row-${f.id}`}
      onClick={() => onSelect(f.id)}
      aria-current={selected ? 'true' : undefined}
      // Roving tabindex: only the selected row is a Tab stop; Arrow/j-k keys move between rows (handled
      // on the list container), so a keyboard user reaches the queue in ONE tab and steps through it.
      tabIndex={selected ? 0 : -1}
      // A clean spoken label — the issue, its document, and the remediation state — instead of the
      // raw concatenation of the visible chips.
      aria-label={`${r.issue}, ${r.file}${r.location ? `, ${r.location}` : ''}${r.laneShort ? ` — ${r.laneShort}` : ''}`}
      className="rinbox-row"
      style={{
        display: 'flex', gap: 10, width: '100%', textAlign: 'left', cursor: 'pointer',
        padding: '10px 12px', border: 'none', borderBottom: '1px solid var(--line, #e2dce4)',
        background: selected ? 'var(--sel, #eef3ff)' : 'transparent',
        borderLeft: selected ? `3px solid ${railed}` : '3px solid transparent',
      }}
    >
      <LaneRail lane={r.lane} />
      <span style={{ minWidth: 0, flex: '1 1 auto' }}>
        {/* Dominant text is the ISSUE, not the filename — the issue determines what to do next. */}
        <span style={{ display: 'block', fontWeight: r.unread ? 700 : 500, fontSize: 13.5,
                       whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {r.issue}
        </span>
        {subline && (
          <span className="muted" style={{ display: 'block', fontSize: 12, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {subline}
          </span>
        )}
        {/* Compact chips: the WCAG SC number as the one prominent pill, then the remediation state as
            QUIET text (the lane's colour is already carried by the rail on the left, so the state does
            not need a loud coloured pill on every row). The full "what ACP did" sentence (r.did) is
            stated once in the workspace detail, never repeated per row. */}
        <span style={{ display: 'flex', gap: 8, marginTop: 4, alignItems: 'center', flexWrap: 'wrap' }}>
          {r.sc && (
            <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.02em',
                           background: 'var(--surface-2,#f0eef3)', color: 'var(--ink,#2a2340)',
                           borderRadius: 5, padding: '1px 6px',
                           fontFamily: 'var(--font-mono)' }}>
              {r.sc}
            </span>
          )}
          {r.laneShort && <span className="muted" style={{ fontSize: 11 }}>{r.laneShort}</span>}
          {r.effort !== '—' && <span className="muted" style={{ fontSize: 11 }}>{r.effort}</span>}
          <span className={`rinbox-action-chip rinbox-action-chip--${action.key}`}>{action.label}</span>
          {f?.status === 'in_review' && !r.resolved && (
            <span style={{ fontSize: 10, fontWeight: 600, letterSpacing: '.04em',
                           background: 'var(--accent-subtle,#e8f0fe)', color: 'var(--accent,#3b6fd6)',
                           borderRadius: 4, padding: '1px 5px', marginLeft: 'auto' }}>
              In review
            </span>
          )}
          <span className="muted" style={{ fontSize: 11, marginLeft: 'auto' }}>{f.automaticQueued ? f.automaticQueueLabel || 'Queued for automatic checks' : WORKFLOW_LABELS[workflowStatusOf(f, decisions)]}</span>
        </span>
      </span>
    </button>
  )
}

// ── A CLUSTER row: many findings, one decision ────────────────────────────────────────────────
// The row a reviewer actually works. A production run put 265 findings into this queue, largely for
// one criterion; a queue that long invites rubber-stamping however well each row is laid out. So the
// unit of the queue is the cluster — same criterion, same format, same lane — and the reviewer
// inspects ONE representative and decides once for the group.
//
// Two controls, side by side, because one button cannot legally contain another: the row itself
// selects the cluster's shown finding, and a separate disclosure expands the members so any
// individual one can still be inspected and decided on its own.
// The formats a cluster spans, as reading text. Format is NOT part of the cluster key, so a group
// can cover .docx and .pdf at once; this is what keeps that breadth visible instead of implied.
function formatList(formats) {
  const f = (formats || []).map((x) => String(x).toUpperCase())
  if (f.length === 0) return ''
  if (f.length === 1) return f[0]
  if (f.length === 2) return `${f[0]} and ${f[1]}`
  return `${f.slice(0, -1).join(', ')} and ${f[f.length - 1]}`
}

const SEV_ORDER = ['CRITICAL', 'SERIOUS', 'MODERATE', 'MINOR', 'UNRATED']
function severityLine(severities) {
  const parts = SEV_ORDER.filter((k) => severities?.[k]).map((k) => `${severities[k]} ${k === 'UNRATED' ? 'unrated' : k.toLowerCase()}`)
  return parts.join(' · ')
}

function ClusterRow({ row, shown, decisions, selectedId, onSelect, expanded, onToggle, automatic = false }) {
  const actions = reviewQueueActions(row.items, decisions, automatic)
  const r = rowModel(shown, decisions)
  const railed = railColorOf(row.lane)

  const remaining = row.unresolved.length
  const listId = `rinbox-cluster-${row.key.replace(/[^\w-]/g, '_')}`
  // When collapsed the header IS the selected member's row, so it carries the selection. When
  // expanded the member rows carry it, and the header steps back — exactly one row is current.
  const selected = !expanded && shown.id === selectedId
  // Spoken as one unit: what the group is, how big it is, and how much of it is left — the three
  // facts that decide whether a reviewer opens it. The per-member rows carry their own labels.
  const label = `${r.issue}, ${row.count} findings across ${row.fileCount} document${row.fileCount === 1 ? '' : 's'}`
    + `, ${formatList(row.formats)}${row.lane?.short ? ` — ${row.lane.short}` : ''}`
    + `, ${remaining} awaiting a decision`
  return (
    <div className="rinbox-clusterwrap">
      <div style={{ display: 'flex', alignItems: 'stretch', borderBottom: '1px solid var(--line, #e2dce4)',
                    background: selected ? 'var(--sel, #eef3ff)' : 'transparent',
                    borderLeft: selected ? `3px solid ${railed}` : '3px solid transparent' }}>
        <button
          type="button"
          id={`rinbox-row-${shown.id}`}
          onClick={() => onSelect(shown.id)}
          aria-current={selected ? 'true' : undefined}
          tabIndex={selected ? 0 : -1}
          aria-label={label}
          className="rinbox-row rinbox-cluster-row"
          style={{ display: 'flex', gap: 10, flex: '1 1 auto', minWidth: 0, textAlign: 'left',
                   cursor: 'pointer', padding: '10px 12px', border: 'none', background: 'transparent' }}
        >
          <LaneRail lane={row.lane} />
          <span style={{ minWidth: 0, flex: '1 1 auto' }}>
            <span style={{ display: 'block', fontWeight: remaining > 0 ? 700 : 500, fontSize: 13.5,
                           whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {r.issue}
            </span>
            {/* The scale of the group, which is the reason it is one row instead of many. */}
            <span className="muted" style={{ display: 'block', fontSize: 12 }}>
              {row.count} findings · {row.fileCount} document{row.fileCount === 1 ? '' : 's'}
            </span>
            <span style={{ display: 'flex', gap: 8, marginTop: 4, alignItems: 'center', flexWrap: 'wrap' }}>
              {row.sc && (
                <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: '.02em',
                               background: 'var(--surface-2,#f0eef3)', color: 'var(--ink,#2a2340)',
                               borderRadius: 5, padding: '1px 6px',
                               fontFamily: 'var(--font-mono)' }}>
                  {row.sc}
                </span>
              )}
              <span className="muted" style={{ fontSize: 11 }}>{formatList(row.formats)}</span>
              {row.lane?.short && <span className="muted" style={{ fontSize: 11 }}>{row.lane.short}</span>}
              {/* Show the action mix for this group; severity remains available in priority filters
                  and the selected group detail, independently of who owns the next step. */}
              {actions.map(action => <span key={action.key} className={`rinbox-action-chip rinbox-action-chip--${action.key}`}>{actions.length > 1 ? `${action.count} ` : ''}{action.label}</span>)}
              {row.resolvedCount > 0 && (
                <span className="muted" style={{ fontSize: 11, marginLeft: 'auto' }}>
                  {row.resolvedCount} of {row.count} decided
                </span>
              )}
            </span>
          </span>
        </button>
        <button
          type="button"
          onClick={() => onToggle(row.key)}
          aria-expanded={expanded}
          aria-controls={listId}
          aria-label={`${expanded ? 'Collapse' : 'Expand'} the ${row.count} findings in ${r.issue}`}
          style={{ flex: '0 0 auto', border: 'none', borderLeft: '1px solid var(--line,#e2dce4)',
                   background: 'transparent', cursor: 'pointer', padding: '0 12px', fontSize: 12,
                   color: 'var(--muted,#5b6774)' }}
        >
          <span aria-hidden="true">{expanded ? '\u25be' : '\u25b8'}</span>
        </button>
      </div>
      {expanded && (
        <div id={listId} style={{ background: 'var(--surface-2,#faf9fb)' }}>
          {row.items.map((f) => (
            <QueueRow key={f.id} f={f} decisions={decisions} automatic={automatic} selected={f.id === selectedId}
                      onSelect={onSelect} showFile />
          ))}
        </div>
      )}
    </div>
  )
}
function ManualSteps({ f }) {
  const fmt = fmtOf(f.file)
  const [os, setOs] = useState('win') // 'win' | 'mac'
  const steps = fixSteps(f.rule_id || f.ruleId, fmt)
  const text = steps ? (typeof steps === 'string' ? steps : steps[os] || steps.win || steps.mac) : null
  return (
    <div>
      <h4 style={{ margin: '0 0 6px' }}>Fix this in {appName(fmt)}</h4>
      <div role="tablist" aria-label="Platform" style={{ display: 'inline-flex', border: '1px solid var(--line,#e2dce4)', borderRadius: 8, overflow: 'hidden', marginBottom: 10 }}>
        {[['win', 'Windows'], ['mac', 'Mac']].map(([k, l]) => (
          <button key={k} role="tab" aria-selected={os === k} onClick={() => setOs(k)}
                  style={{ fontSize: 12, fontWeight: 600, padding: '4px 14px', cursor: 'pointer', border: 'none',
                           background: os === k ? 'var(--ink)' : 'transparent', color: os === k ? '#fff' : 'var(--ink)' }}>{l}</button>
        ))}
      </div>
      <p style={{ fontSize: 13.5, lineHeight: 1.5, margin: 0 }}>{text || 'Open the document in its native editor and correct the flagged item, then upload the revised file to recheck.'}</p>
      {/* Present only on the criteria a menu path cannot resolve (1.4.1, 1.4.11, 2.1.2, 2.4.3).
          Knowing where to click does not tell a reviewer when they are DONE, and for these ACP
          cannot check the result at all — saying so is what separates guidance from a false
          sense of completion. Empty for every other criterion, so nothing renders. */}
      {steps?.completion && (
        <p style={{ fontSize: 13.5, lineHeight: 1.5, margin: '10px 0 0' }}>
          <b>Done when:</b> {steps.completion}
        </p>
      )}
      {steps?.limits && (
        <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.5, margin: '8px 0 0' }}>
          <b>ACP cannot verify this:</b> {steps.limits}
        </p>
      )}
    </div>
  )
}

// The plain, imperative "Your task" line — what a normal reviewer is expected to DO, framed as a
// remediation task rather than an engineering evidence record. Criterion- and lane-aware so a contrast
// fix reads like a contrast decision, not a generic "review the change".
function taskLineOf(f, lane, automaticMode = false, decisions = {}) {
  if (f.applied && !f.validated) return 'This change is already applied. Recorded verification is incomplete; another approval is not needed.'
  if (automaticMode) {
    const responsibility = automaticReviewResponsibility(f, decisions)
    if (responsibility === 'acp') return 'ACP is handling the admitted automatic work. No individual approval or human confirmation is needed now.'
    if (responsibility === 'check') return 'Automatic admission or verification is not confirmed yet. Check the recorded status; this is not a request to approve the fix again.'
  }
  if (requiresPdfSourceEditing(f) && !f.applied) return 'This PDF needs tagging in the source document or a PDF accessibility editor. The suggested outline is guidance; approving it does not write a PDF structure tree.'
  if (isPdfStructuralRow(f)) return 'ACP can write this change into the saved PDF after approval. Apply ready fixes together; individual inspection is optional. The corrected copy will be assessed before publication.'
  if (f.autoApplied) return 'This change is already applied. Inspect it if you want, or flag a problem.'
  const contrast = isContrastFinding(f)
  switch (lane.key) {
    case 'review':
      return contrast
        ? 'Review ACP’s contrast fix — confirm the darker text still looks right for this document, then approve it.'
        : 'Review ACP’s fix — confirm it looks right for this document, then approve it.'
    case 'apply':
      return contrast
        ? 'Review ACP’s contrast fix — confirm the darker text reads well, then apply it (or edit it first).'
        : 'Review ACP’s proposed fix — apply it, edit it first, or reject it to a person.'
    case 'handoff':
      return 'ACP’s fix was rejected — pick this one up by hand in the source app using the steps below.'
    case 'recheck':
      return 'This was edited — re-scan to confirm it now passes.'
    case 'blocked':
      return 'This can’t be remediated as-is — review what’s blocking it.'
    default: // manual
      return 'ACP can’t safely change this automatically — fix it by hand in the source app using the steps below.'
  }
}

function DetailPane({ f, decisions, readOnly = false, automaticMode = false, preparingProposals = false, onDecide, onOpenWord, onRecheck, onOpenPlan, onVerifySaved, matchingFindings = [], matchingReadyCount = 0, legacyApprovalControls = false, onApplyToMatching, cluster = null, draft = null, onDraftChange, saving = false, error = null, headingRef = null, detailExtra = null, emptyState = null }) {
  const [matchingPreviewOpen, setMatchingPreviewOpen] = useState(false)
  const [copiedValue, setCopiedValue] = useState('')
  const draftRef = useRef(null)
  const [verification, setVerification] = useState(null)
  const selectedRef = useRef(f?.id)
  selectedRef.current = f?.id
  useEffect(() => { setVerification(null) }, [f?.id])
  useEffect(() => { setMatchingPreviewOpen(false) }, [f?.id])
  useEffect(() => { setCopiedValue('') }, [f?.id])
  if (!f) {
    return (
      <div style={{ display: 'grid', placeItems: 'center', height: '100%', textAlign: 'center', padding: 24 }}>
        <div className="muted">
          <div style={{ fontSize: 34 }} aria-hidden="true">✓</div>
          {emptyState || <p style={{ marginTop: 8 }}>Select a finding from the inbox to review its recommended action.</p>}
        </div>
      </div>
    )
  }
  const r = rowModel(f, decisions)
  const matchingCount = matchingFindings.length
  const lane = laneOf(f)
  // Handoff (a rejected AI fix, W2) is worked by hand like a manual finding — guided steps + the
  // "Mark as assigned" action — so it shares the manual detail treatment.
  const isHandoff = lane.key === 'handoff'
  const responsibility = automaticMode ? automaticReviewResponsibility(f, decisions) : null
  const isManual = requiresPdfSourceEditing(f) || (lane.key === 'manual' || isHandoff) && (!automaticMode || responsibility === 'human')
  // A deterministic fix ACP already applied. Its decision is a plain approve / "this looks wrong",
  // not an edit-and-apply — the change is already written, so we don't offer an editable draft.
  const isAutoFix = lane.key === 'review'
  const resolved = isResolved(f, decisions)
  const inspectionOnly = optionalInspectionOf(f)
  const reviewDecision = recordedReviewDecision(f, decisions)
  const eyebrow = automaticMode && responsibility === 'check' ? (f.applied && !f.validated ? 'Applied · verification incomplete' : 'Status check') : automaticMode && responsibility === 'acp' ? 'ACP processing' : inspectionOnly ? 'Saved changes · optional inspection' : isHandoff ? 'Needs manual handling' : lane.key === 'manual' ? 'Manual remediation' : 'Review'
  // A drafted AI value the reviewer can adjust before applying. `draft` falls back to the finding's
  // proposed value until the reviewer types; `edited` flips the primary action to "Save edited fix".
  const structuralRow = isPdfStructuralRow(f)
  const structuralReady = !structuralRow || proposalsFor(f).every(pdfStructuralSummary)
  const automaticState = f._raw?.automatic_approval?.state || f.automatic_approval?.state
    || f._raw?.auto_approval_status || f.auto_approval_status
  const automaticLabel = f.automaticQueueLabel || ({checking:'Checking…',processing:'Applying…',applying:'Applying…',verifying:'Verifying…'})[automaticState] || 'Queued automatically'
  const canEdit = !structuralRow && !resolved && !isManual && !isAutoFix && f.after != null && f.after !== ''
  const draftValue = draft ?? (f.after ?? '')
  const edited = canEdit && draftValue !== (f.after ?? '')
  // The plain-language "What ACP changed" sentence — real values only (null when nothing to describe).
  const changed = !isManual && !structuralRow ? changeSentence(f) : null
  const hasProposedValue = f.after != null && f.after !== ''
  const currentValue = displayText(f.before || f.observed || 'Not recorded')
  const proposedValue = structuralRow ? proposalsFor(f).map(p => pdfStructuralSummary(p) || 'Structural proposal unavailable — refresh suggestions').join('; ') : displayText(draftValue || f.after || '')
  const copyValue = async (kind, value) => {
    if (!navigator.clipboard?.writeText) return
    await navigator.clipboard.writeText(value)
    setCopiedValue(kind)
  }
  const why = whyOf(f)
  const recovery = !resolved && !preparingProposals && remediationRecoveryGuidance(f, decisions)
  const retryVerification = async () => {
    const id = f.id
    setVerification({ busy: true })
    try {
      const result = await onVerifySaved(f)
      const remaining = Array.isArray(result?.remaining_issues) ? result.remaining_issues.length : null
      if (selectedRef.current === id) setVerification({ message: result?.assessment_ok
        ? remaining == null ? 'Saved copy checked. Remaining issue details are unavailable.' : remaining > 0 ? `Saved copy checked: ${remaining} remaining issues.` : 'Saved copy checked: no remaining issues in the recorded assessment scope.'
        : result?.reason || 'The saved copy could not be assessed. No verification is claimed.' })
    } catch (error) {
      if (selectedRef.current === id) setVerification({ error: error.message || 'Saved-copy verification failed. The recorded result remains unchanged.' })
    }
  }

  return (
    <div className="remediation-detail" style={{ display: 'flex', flexDirection: 'column' }}>
      {/* Decision controls come first so reviewers can act without scrolling. */}
      <div className="remediation-detail-actions" role="group" aria-label={`Decision actions for ${displayText(r.issue)}`}
           style={{ borderTop: '1px solid var(--line,#e2dce4)', background: 'var(--bg, #fff)' }}>
        {recovery && <section aria-label="What this item needs" style={{ padding: '10px 22px', borderBottom: '1px solid var(--line,#e2dce4)', fontSize: 12.5 }}>
          <b>{recovery.title}</b><p>{recovery.reason}</p><p className="muted">{recovery.next}</p>
          {recovery.plan && onOpenPlan && <button type="button" className="linklike" disabled={readOnly || saving} onClick={onOpenPlan}>Open remediation plan</button>}
        </section>}
        {onVerifySaved && f.applied && !f.validated && <div style={{ padding: '10px 22px', fontSize: 12.5 }}>
          <button type="button" className="ghost" disabled={readOnly || saving || verification?.busy} onClick={retryVerification}>{verification?.busy ? 'Checking saved copy…' : 'Retry verification of saved copy'}</button>
          <p className="muted">Checks the current saved bytes. It does not regenerate or reapply fixes.</p>
          {verification?.message && <p role="status">{verification.message}</p>}
          {verification?.error && <p role="alert">{verification.error}</p>}
        </div>}
        {/* W8 — batch a decision across every other queued finding of the same rule/SC. Explicit and
            reversible-feeling: it names the count, and each target routes through the same onDecide
            (so approvals re-validate and rejections hand off) as if the reviewer acted on them one by
            one. Offered only for actionable (non-manual, unresolved) findings that actually have
            matches. */}
        {/* Retired matching approval panel retained for restoration; live approval is run-wide. */}
        {legacyApprovalControls && !resolved && !isManual && matchingCount > 0 && (
          <div style={{ display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap',
                        padding: '10px 22px', borderBottom: '1px solid var(--line,#e2dce4)',
                        background: 'var(--surface-2,#f6f5f8)', fontSize: 12.5 }}>
            {/* The scope, stated in full before the decision. A batch is the one control here that
                reaches findings the reviewer has not looked at, so it names the criterion, the format,
                the number of documents, and — because severity is deliberately not part of what
                groups a cluster — the severity mix it spans. */}
            <span className="muted">
              You are looking at one of {matchingCount + 1} findings that share this issue
              {cluster ? <> — {scKeyOf(f) ? `WCAG ${scKeyOf(f)}` : 'the same criterion'} in {formatList(cluster.formats)} files,
                across {cluster.fileCount} document{cluster.fileCount === 1 ? '' : 's'}</> : null}.
              {cluster && cluster.formats.length > 1
                ? <> This decision covers more than one document format.</> : null}
              {cluster && severityLine(cluster.severities)
                ? <> The group spans {severityLine(cluster.severities)}.</> : null}
              {' '}The other {matchingCount} carry the same criterion and an actionable proposal; manual,
              blocked and already-decided findings are excluded.
            </span>
            <div>
              <button type="button" className="linklike" aria-expanded={matchingPreviewOpen}
                      onClick={() => setMatchingPreviewOpen((open) => !open)}>Review matching items</button>
              {matchingPreviewOpen && <MatchingReviewPreview findings={matchingFindings} />}
            </div>
            <div style={{ flexBasis: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
                          padding: '10px 12px', border: '1px solid var(--line,#e2dce4)', borderRadius: 9,
                          background: 'var(--bg,#fff)' }}>
              <span style={{ lineHeight: 1.4 }}>
                <b style={{ display: 'block', color: 'var(--ink)', fontSize: 13 }}>Review a batch of matching proposals</b>
                Select from this item and {matchingCount} similar finding{matchingCount === 1 ? '' : 's'}
                {' '}across {new Set([f.file, ...matchingFindings.map((x) => x.file)]).size} files.
              </span>
              <button type="button" className="primary" disabled={readOnly || saving}
                      onClick={() => onApplyToMatching?.(f)}
                      style={{ flex: '0 0 auto', fontWeight: 750, padding: '9px 14px' }}>
                {`Select matching proposals (${matchingCount + 1})`}
              </button>
            </div>
          </div>
        )}
        {!legacyApprovalControls && !resolved && matchingCount > 0 && <details style={{ padding: '8px 22px' }}><summary>{matchingCount + 1} similar findings · {matchingReadyCount} ready to apply</summary><MatchingReviewPreview findings={matchingFindings} /></details>}
        <div style={{ padding: '12px 22px', display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap' }}>
          {resolved ? (
            // Verification appears only AFTER a fix is saved (spec §10): the decision is recorded and a
            // fresh scan re-validates it before it can be certified — shown here, not before the work.
            // A rejection writes nothing, so it gets its own line: saying "Written → Re-scan →
            // Certified" under a declined fix would describe a change that was never made.
            <span className="muted" style={{ fontSize: 12.5, display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              {inspectionOnly ? <><span>Automatic change recorded.</span><span>Inspection is optional. Verification: {f.validated ? 'passed the recorded checks' : 'not confirmed by this review record'}.</span></> : reviewDecision ? <><span>Decision recorded: {reviewDecision === 'deferred' ? 'Deferred' : 'Not applicable'}.</span><span>{reviewDecision === 'deferred' ? 'Remaining work stays recorded for follow-up.' : 'This criterion was excluded from scope.'} No fix or verification is claimed.</span></> : String(f?.status || '').toLowerCase() === 'rejected' || decisions[f?.id]?.state === 'rejected' ? (
                <>
                  <span style={{ fontSize: 13, color: 'var(--ink)', fontWeight: 600 }}>✓ Decision recorded.</span>
                  <span>You rejected this suggestion — nothing was written to the document.</span>
                </>
              ) : f?.validated ? (
                <>
                  <span style={{ fontSize: 13, color: 'var(--ink)', fontWeight: 600 }}>✓ Verified.</span>
                  <span>Verification: Written → Re-scan → <b>Certified</b> — a fresh scan confirmed this fix.</span>
                </>
              ) : (
                <>
                  <span style={{ fontSize: 13, color: 'var(--ink)', fontWeight: 600 }}>✓ Saved.</span>
                  <span>Verification: <b>Written</b> → Re-scan → Certified — a fresh scan confirms it before it’s certified.</span>
                </>
              )}
            </span>
          ) : isManual ? (
            <>
              {!onOpenWord && <RemediationSourceLink finding={f} />}
              {onOpenWord && <button className="primary" disabled={readOnly || saving} onClick={() => onOpenWord(f)}>Open in Word</button>}
              {onRecheck && <button className="ghost" disabled={readOnly || saving} onClick={() => onRecheck(f)}>Upload &amp; recheck</button>}
              {legacyApprovalControls && <button className="ghost" disabled={readOnly || saving} onClick={() => onDecide?.(f, { state: 'assigned' })}>Defer</button>}
              {/* Out of scope — this criterion doesn't apply to the document. Resolves the finding and
                  takes it out of the coverage denominator (persisted as an out_of_scope resolution). */}
              {legacyApprovalControls && <button className="ghost" disabled={readOnly || saving} onClick={() => onDecide?.(f, { state: 'not_applicable' })}>Not applicable</button>}
            </>
          ) : isAutoFix ? (
            /* An auto-applied fix: the change is already written, so the decision is a clear approve or
               a flag that it looks wrong — not an edit-and-apply. "This looks wrong" hands the finding
               back for a person; it does NOT auto-revert the applied change (no backend undo exists —
               see PR body), so it is labelled as a flag, not a "reject & revert". */
            <>
              <button className="primary" disabled={readOnly || saving || f.automaticQueued} onClick={() => { if (!f.automaticQueued) onDecide?.(f, { state: 'accepted' }) }}>
                {f.automaticQueued ? automaticLabel : saving ? 'Saving…' : f.autoApplied ? 'Mark inspected →' : legacyApprovalControls ? 'Yes, apply fix' : 'Apply this fix'}
              </button>
              <button className="ghost" disabled={readOnly || saving} onClick={() => onDecide?.(f, { state: 'rejected' })}>This looks wrong</button>
              {onOpenWord && <button className="ghost" disabled={readOnly || saving} onClick={() => onOpenWord(f)}>Open source document</button>}
              {legacyApprovalControls && <button className="ghost" disabled={readOnly || saving} onClick={() => onDecide?.(f, { state: 'not_applicable' })}>Not applicable</button>}
            </>
          ) : (
            <>
              <button className="primary" disabled={readOnly || saving || !structuralReady || f.automaticQueued}
                      onClick={() => { if (!f.automaticQueued) onDecide?.(f, { state: 'accepted', value: canEdit ? draftValue : undefined }) }}>
                {f.automaticQueued ? automaticLabel : saving ? 'Saving…' : legacyApprovalControls ? 'Yes, apply fix' : f.automaticReason ? 'Review and apply' : 'Apply this fix'}
              </button>
              {/* A specific action, not a bare "Reject": declining an AI fix hands the finding to a
                  person (the handoff lane), so the label names that outcome rather than leaving the
                  reviewer to guess what "Reject" does. */}
              <button className="ghost" disabled={readOnly || saving} onClick={() => onDecide?.(f, { state: 'rejected' })}>{legacyApprovalControls ? 'No, needs manual work' : 'Needs manual work'}</button>
              <details><summary>More options</summary>
              {canEdit && <button className="ghost" disabled={readOnly || saving} onClick={() => draftRef.current?.focus()}>Edit proposed fix</button>}
              {legacyApprovalControls && <button className="ghost" disabled={readOnly || saving} onClick={() => onDecide?.(f, { state: 'assigned' })}>Defer</button>}
              {legacyApprovalControls && <button className="ghost" disabled={readOnly || saving} onClick={() => onDecide?.(f, { state: 'not_applicable' })}>Not applicable</button>}
              {onOpenWord && <button className="ghost" disabled={readOnly || saving} onClick={() => onOpenWord(f)}>Open source document</button>}
              </details>
            </>
          )}
        </div>
        {/* The decision that did NOT save, stated where the reviewer pressed the button. The finding
            stays selected and unresolved behind this — nothing advanced, and nothing was recorded. */}
        {error && (
          <div role="alert"
               style={{ margin: '0 22px 14px', padding: '10px 12px', borderRadius: 8, fontSize: 12.5,
                        border: '1px solid #C0392B', background: '#FDEDEC', color: '#7B241C' }}>
            <b>Not saved.</b> {error.message} This finding is still waiting for your decision — nothing was recorded and you have not moved on.
          </div>
        )}
      </div>
      {/* Keep this content-sized. The workspace owns scrolling; making this child 100% tall
          pushed the decision bar to the bottom of a tall review canvas and left a large blank
          region between the evidence accordions and their actions. */}
      <div className="remediation-detail-content" style={{ padding: '18px 22px' }}>
        {/* 1 · What is this — and what do I need to DO about it? */}
        <div className="remediation-review-header">
          <p className="muted" style={{ margin: 0, fontSize: 12, fontWeight: 600 }}>{eyebrow}</p>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 16 }}>
            <div style={{ minWidth: 0 }}>
              <h3 ref={headingRef} tabIndex="-1" style={{ margin: '4px 0 4px', fontSize: 20 }}>{displayText(r.issue)}</h3>
              <p className="muted" style={{ margin: 0, fontSize: 13, overflowWrap: 'anywhere' }}>{displayText(r.file)}</p>
            </div>
            {onOpenWord && <button className="ghost" onClick={() => onOpenWord(f)}>View full document</button>}
          </div>
          <Meta row={{ ...r, wcag: inspectionOnly ? '' : (f.rule_id || f.ruleId || '') }} />
          {/* Indigo, not the generic "applied" blue — a distinct color for a distinct claim (this
              specific change has durable, recorded evidence of AI origin), per aiAppliedUnverified's
              own evidence gate above. Still counted in the generic 'applied' bucket everywhere else
              (remediationCategory), so no total changes — this is a rendering-only distinction. */}
          {aiAppliedUnverified(f) && <span className="remediation-category-pill remediation-category-pill--ai_applied" title="AI wrote this change. Verification has not confirmed that the finding is resolved. It remains in pending counts.">AI applied · not verified</span>}
        </div>
        <p className="remediation-review-problem" style={{ fontSize: 15, lineHeight: 1.55, margin: '18px 0 0' }}>{inspectionOnly ? 'Saved automatic changes are available to browse. No review action is needed.' : problemOf(f, r.issue)}</p>
        {displayText(f.problemStatement).length > EXCERPT_LIMIT && <details className="remediation-full-text" key={`problem-${f.id}`}>
          <summary>Show full problem description</summary><p>{displayText(f.problemStatement)}</p>
        </details>}

        {/* Your task — the imperative, so the reviewer is never left guessing what to do here. Hidden
            once the finding is resolved (the verification line below then speaks instead). */}
        {!resolved && (
          <p style={{ fontSize: 13.5, lineHeight: 1.5, margin: '10px 0 0' }}><b>Your task:</b> {taskLineOf(f, lane, automaticMode, decisions)}</p>
        )}

        {inspectionOnly ? <section className="remediation-saved-changes" aria-label="Saved automatic changes"><h3>Saved automatic changes</h3><p>The automatic remediation change has been recorded. Browse the saved change evidence below; no approval or inspection is required.</p><p>Verification: {f.validated ? 'Recorded checks passed.' : 'This inspection record does not confirm verification. See the recorded run results.'}</p><QualityReviewEvidence finding={f} /></section> : isManual ? (
          /* Manual / handoff: there is no applied change to judge — show HOW to make it instead. */
          <div style={{ marginTop: 18 }}>
            <ManualSteps f={f} />
          </div>
        ) : (
          <>
            <QualityReviewEvidence finding={f} editedValue={canEdit ? draftValue : undefined} />
            <div className="remediation-comparison" aria-label="Current and proposed values">
              <div><b>{currentValue.length > EXCERPT_LIMIT ? 'Current excerpt' : 'Current'}</b><span>{excerptOf(currentValue)}</span><button type="button" className="linklike remediation-copy-value" onClick={() => copyValue('current', currentValue)}>{copiedValue === 'current' ? 'Copied' : 'Copy current'}</button></div>
              <div><b>{proposedValue.length > EXCERPT_LIMIT ? 'Proposed excerpt' : 'Proposed'}</b><span>{proposedValue.length > EXCERPT_LIMIT ? excerptOf(proposedValue) : <ChangedValue from={currentValue} to={proposedValue} />}</span><button type="button" className="linklike remediation-copy-value" onClick={() => copyValue('proposed', proposedValue)}>{copiedValue === 'proposed' ? 'Copied' : 'Copy proposed'}</button></div>
            </div>
            {currentValue.length > EXCERPT_LIMIT && <details className="remediation-full-text" key={`source-${f.id}`}>
              <summary>Show full source</summary><p>{currentValue}</p>
            </details>}
            {proposedValue.length > EXCERPT_LIMIT && <details className="remediation-full-text" key={`proposed-${f.id}`}>
              <summary>Show full proposed value</summary><p>{proposedValue}</p>
            </details>}
            {structuralRow && <details className="remediation-full-text"><summary>Technical plan</summary>{proposalsFor(f).map((p, i) => <pre key={i} className="machine-value">{p.proposed_value}</pre>)}</details>}
            {changed && (changed.length > EXCERPT_LIMIT ? <details className="remediation-full-text" key={`change-${f.id}`}>
              <summary>Change description</summary><p>{displayText(changed)}</p>
            </details> : <p style={{ fontSize: 13.5, lineHeight: 1.5, margin: '10px 0 0' }}>{displayText(changed)}</p>)}

            {/* Editable draft (apply lane only) — the reviewer adjusts the exact text ACP will write,
                then applies their version. Empties reset to the AI's proposal, never a blank fix.
                Preserves the #412/#415 "Save edited fix" behaviour. */}
            {canEdit && (
              <div style={{ marginTop: 14 }}>
                <label className="muted" htmlFor="rem-draft" style={{ display: 'block', margin: '0 0 6px', fontSize: 12, fontWeight: 600 }}>Edit proposed value</label>
                <textarea ref={draftRef} id="rem-draft" value={draftValue} onChange={(e) => onDraftChange?.(e.target.value)}
                          aria-label="Edit the proposed fix" rows={2}
                          style={{ width: '100%', fontSize: 13.5, padding: '8px 10px', borderRadius: 8,
                                   border: '1px solid var(--line,#e2dce4)', fontFamily: 'inherit', resize: 'vertical' }} />
                {edited && <p className="muted" style={{ fontSize: 11.5, margin: '4px 0 0' }}>Edited — applying this fix writes your version instead of the AI’s.</p>}
              </div>
            )}
          </>
        )}

        {!isManual && <p className="muted" style={{ fontSize: 13, lineHeight: 1.45, margin: '14px 0 0' }}>
          {f.applied && !f.validated ? 'This change is applied, but verification is incomplete. No additional approval is needed and it is not counted as verified.'
            : automaticMode && responsibility === 'acp' ? 'ACP is handling this admitted work automatically. No human confirmation is required now.'
            : automaticMode && responsibility === 'check' ? 'Automatic admission or verification needs a status check. The absence of a verification action does not imply that human approval is required.'
            : f.autoApplied ? 'Inspecting this applied change does not approve or apply another change.'
            : onRecheck ? 'After approval, ACP will create a corrected copy and verify this criterion again.'
                     : 'Verification is not recorded here. Check the saved result; approval and verification are separate.'}
        </p>}
        <section aria-labelledby="why-this-matters" style={{ marginTop: 18 }}>
          <h4 id="why-this-matters" style={{ margin: '0 0 5px', fontSize: 14 }}>Why this matters</h4>
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.5, margin: 0 }}>{excerptOf(why)}</p>
          {why.length > EXCERPT_LIMIT && <details className="remediation-full-text" key={`reason-${f.id}`}>
            <summary>Show full review reason</summary><p>{why}</p>
          </details>}
        </section>

        {!isManual && hasProposedValue && (
          <details style={{ marginTop: 16 }}>
            <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>Detection and verification details</summary>
            <dl className="remediation-details-list">
              <div><dt>How ACP detected this</dt><dd>{displayText(f.detectionMethod || f.proposalSource || 'Automated document analysis')}</dd></div>
              <div><dt>Observed value</dt><dd>{currentValue}</dd></div>
              <div><dt>Verification</dt><dd>{f.applied && !f.validated ? 'Applied, verification incomplete. Independent verification is not recorded yet.' : automaticMode && responsibility === 'acp' ? 'ACP handles the admitted application and verification work.' : automaticMode && responsibility === 'check' ? 'Automatic admission or verification has not been confirmed.' : onRecheck ? `The corrected copy will be rescanned for WCAG ${scKeyOf(f) || 'compliance'}.` : 'Verification capability is not recorded here; check the saved outcome.'}</dd></div>
              <div><dt>Current verification state</dt><dd>{resolved ? 'Awaiting verification' : 'Awaiting approval'}</dd></div>
            </dl>
          </details>
        )}
        {(f.proposalSource || f.evidence) && (
          <details style={{ marginTop: 8 }}>
            <summary style={{ cursor: 'pointer', fontSize: 13, fontWeight: 600 }}>Technical evidence</summary>
            <p className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
              {f.evidence}{f.proposalSource ? ` · source: ${f.proposalSource}` : ''}
            </p>
          </details>
        )}
        {detailExtra}
      </div>


    </div>
  )
}

// Below this the queue and the review panel each get the full width, one at a time. Chosen so the
// review panel keeps a readable measure at 200% zoom rather than at a device size.
const NARROW_Q = '(max-width: 820px)'

// A workspace preference (the reviewer sets it once), so it lives in localStorage keyed globally —
// unlike search/filter state, which is per-scan sessionStorage. Every access is guarded: storage
// can throw (private mode, disabled) and must never take the inbox down with it.
const LS = 'acp.remediate.'
function readLS(k, dflt) { try { const v = localStorage.getItem(LS + k); return v == null ? dflt : v } catch { return dflt } }
function readNum(k, dflt) { const n = parseFloat(readLS(k, '')); return Number.isFinite(n) ? n : dflt }
function writeLS(k, v) { try { localStorage.setItem(LS + k, String(v)) } catch { /* storage unavailable — keep the in-memory value */ } }
function readSession(k, dflt) { try { return sessionStorage.getItem(k) ?? dflt } catch { return dflt } }
function writeSession(k, v) { try { sessionStorage.setItem(k, String(v)) } catch { /* keep in-memory state */ } }
const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n))

// A keyboard-operable resize handle. Pointer drag resizes in the browser; Arrow keys nudge it,
// which is both an accessibility requirement for role="separator" and what makes the resize
// verifiable in jsdom (which has no layout, so getBoundingClientRect is zero and pointer math
// no-ops). aria-valuenow carries the current split so assistive tech can read the ratio.
function Divider({ orientation, label, value, min, max, onDrag, onNudge }) {
  const dragging = useRef(false)
  const vertical = orientation === 'vertical' // the bar is vertical → it divides left|right
  const down = (e) => { dragging.current = true; try { e.currentTarget.setPointerCapture(e.pointerId) } catch {} e.preventDefault() }
  const move = (e) => { if (dragging.current) onDrag(e.clientX, e.clientY) }
  const up = (e) => { dragging.current = false; try { e.currentTarget.releasePointerCapture(e.pointerId) } catch {} }
  const key = (e) => {
    const dec = vertical ? 'ArrowLeft' : 'ArrowUp'
    const inc = vertical ? 'ArrowRight' : 'ArrowDown'
    if (e.key === dec) { onNudge(-1); e.preventDefault() }
    else if (e.key === inc) { onNudge(1); e.preventDefault() }
  }
  return (
    <div role="separator" tabIndex={0} aria-label={label}
         aria-orientation={vertical ? 'vertical' : 'horizontal'}
         aria-valuenow={Math.round(value)} aria-valuemin={min} aria-valuemax={max}
         onPointerDown={down} onPointerMove={move} onPointerUp={up} onKeyDown={key}
         style={{ flex: '0 0 7px', alignSelf: 'stretch', cursor: vertical ? 'col-resize' : 'row-resize',
                  background: 'var(--line,#e2dce4)', touchAction: 'none',
                  ...(vertical ? {} : { width: '100%' }) }} />
  )
}

export default function RemediationInbox({
  queue: suppliedQueue = [], decisions = {}, onDecide, onOpenWord, onRecheck, onOpenPlan, onVerifySaved, onPublish, preparingProposals = false, readOnly = false, legacyApprovalControls = false, autoApprove = null, automaticApprovalPolicy, onAutoApproveChange, autoApproveSaving = false, autoApproveError = null, approvalExplanation = null, afterRelease = false, onAutoApproveRetry, autoApproveNotice = null, onDismissAutoApproveNotice,
  initialSort = 'priority', initialTab = 'review', initialGroup = 'document', scanId = null,
  assignees = {}, myEmail = null, onAssign,
  // The per-ITEM board components (R4 fix preview, R7 per-document progress, R10 audit trail)
  // belong beside the selected finding, but this component must not import them: it already owns
  // the hardest state on the page, and three more imports would make it the place every future
  // panel lands. A render prop keeps the composition with the parent, which owns the page.
  //
  // Called with the selected finding, or null when nothing is selected — the callee decides what
  // an empty selection means rather than this component guessing on its behalf.
  renderDetailExtra = null,
}) {
  const queue = useMemo(() => automaticReviewQueue(suppliedQueue, automaticApprovalPolicy, decisions), [suppliedQueue, automaticApprovalPolicy, decisions])
  const currentQueueRef = useRef(queue)
  currentQueueRef.current = queue
  const readOnlyRef = useRef(readOnly)
  readOnlyRef.current = readOnly
  const [selectedId, setSelectedId] = useState(null)
  const [cardCollapsed, setCardCollapsed] = useState(false)
  const [tab, setTab] = useState(initialTab)
  const [sort, setSort] = useState(initialSort)
  const [search, setSearch] = useState('')
  const filterKey = `acp.remediate.filters.${scanId || 'current'}`
  const [priorityFilter, setPriorityFilter] = useState(() => readSession(`${filterKey}.priority`, 'all'))
  const [formatFilter, setFormatFilter] = useState(() => readSession(`${filterKey}.format`, 'all'))
  const [sourceFilter, setSourceFilter] = useState(() => readSession(`${filterKey}.source`, 'all'))
  useEffect(() => { writeSession(`${filterKey}.priority`, priorityFilter) }, [filterKey, priorityFilter])
  useEffect(() => { writeSession(`${filterKey}.format`, formatFilter) }, [filterKey, formatFilter])
  useEffect(() => { writeSession(`${filterKey}.source`, sourceFilter) }, [filterKey, sourceFilter])
  const [collapsed, setCollapsed] = useState({}) // file -> true when a document group is collapsed
  const [drafts, setDrafts] = useState({}) // finding id -> reviewer-edited proposed value (null until edited)
  const [assignedOnly, setAssignedOnly] = useState(false) // "Assigned to me" filter — files whose assignee is myEmail
  // A corrected copy is written and verified per document, so document is the predictable default.
  // Reviewers can still switch to the issue lens for safe pattern/batch work; an explicit saved
  // preference wins over the default.
  const [group, setGroup] = useState(() => {
    const fallback = initialGroup === 'issue' ? 'issue' : 'document'
    return readLS('group', fallback) === 'issue' ? 'issue' : 'document'
  })
  useEffect(() => { writeLS('group', group) }, [group])
  const [expandedClusters, setExpandedClusters] = useState({})  // cluster key -> true
  const toggleCluster = (key) => setExpandedClusters((e) => ({ ...e, [key]: !e[key] }))
  const [bulkPreviewOpen, setBulkPreviewOpen] = useState(false)
  const [confirmRunRequest, setConfirmRunRequest] = useState(0)
  const [applyRunRequest, setApplyRunRequest] = useState(0)
  const [batchScopeIds, setBatchScopeIds] = useState(null)
  const batchPanelRef = useRef(null)
  useEffect(() => { if (bulkPreviewOpen) batchPanelRef.current?.focus() }, [bulkPreviewOpen, batchScopeIds])

  const [leftW, setLeftW] = useState(() => clamp(readNum('leftW', 33), 28, 40))
  useEffect(() => { writeLS('leftW', leftW) }, [leftW])

  // ── Narrow viewports: two panels side by side stop being two panels and become two half-panels.
  // Below the breakpoint the workspace shows ONE of them at a time — the queue, or the finding with
  // a way back to the queue (PRD §12). matchMedia is absent in jsdom and in older engines, so the
  // guard is a capability check, not a version check, and its failure mode is the desktop layout.
  const [narrow, setNarrow] = useState(() => {
    try { return !!window.matchMedia?.(NARROW_Q).matches } catch { return false }
  })
  useEffect(() => {
    let mq
    try { mq = window.matchMedia?.(NARROW_Q) } catch { return undefined }
    if (!mq) return undefined
    const on = (e) => setNarrow(e.matches)
    // addListener is the pre-2019 spelling; Safari only grew addEventListener here in 14.
    if (mq.addEventListener) { mq.addEventListener('change', on); return () => mq.removeEventListener('change', on) }
    if (mq.addListener) { mq.addListener(on); return () => mq.removeListener(on) }
    return undefined
  }, [])
  // Which of the two the narrow layout is showing. Selecting a finding moves to it; "Back to queue"
  // returns. Ignored entirely at desktop widths, where both panels are on screen at once.
  const [narrowPane, setNarrowPane] = useState('queue')

  const rowRef = useRef(null)   // the .rinbox flex row — the frame for horizontal (column) resizes
  // Drag: translate a pointer position into a percentage of the relevant frame. Guarded on a real
  // measured size, so jsdom's zero-size rects leave the value untouched (keyboard drives the tests).
  const dragLeft = (x) => { const r = rowRef.current?.getBoundingClientRect(); if (r?.width) setLeftW(clamp(((x - r.left) / r.width) * 100, 28, 40)) }

  const runCounts = remediationReviewCounts(queue, decisions, drafts, autoApprove === true)
  // Bulk approval offers exactly what the Needs-review tab holds. The queue now also carries rows
  // that already carry a decision (approved / rejected / skipped, read back from hitl_queue), and
  // `exclusionReason` alone would let a deferred row with a usable proposal into a run-wide
  // "approve all ready" the reviewer opened from a tab that does not show it.
  const readyAcrossScan = queue.filter(f => matchesWorkflow(f, 'needs-review', decisions)
    && !exclusionReason(f, decisions, drafts))
  const unreadyReasons = queue.filter(f => matchesWorkflow(f, 'needs-review', decisions)).reduce((out, f) => {
    const reason = exclusionReason(f, decisions, drafts)
    if (reason && reason !== 'Manual work') out[reason] = (out[reason] || 0) + 1
    return out
  }, {})
  const counts = useMemo(() => workflowCounts(queue, decisions), [queue, decisions])
  const prog = useMemo(() => progress(queue, decisions), [queue, decisions])

  // Findings whose FILE is assigned to the current reviewer — mirrors the backend's
  // files_assigned_to(decisions, email): an empty/absent email matches nothing (never "everything").
  const assignedToMe = (f) => !!myEmail && assignees[f.file] === myEmail
  const myAssignedCount = useMemo(
    () => (myEmail ? queue.filter(assignedToMe).length : 0),
    [queue, assignees, myEmail]) // eslint-disable-line react-hooks/exhaustive-deps
  const aiDraftCount = useMemo(
    () => queue.filter((f) => (tab === 'all' || matchesAutomaticReview(f, tab, decisions, autoApprove === true)) && isAiAssistedDraft(f)).length,
    [queue, tab, decisions])

  const visible = useMemo(() => {
    const q = search.trim().toLowerCase()
    const filtered = queue.filter((f) => (tab === 'all' || matchesAutomaticReview(f, tab, decisions, autoApprove === true)) &&
      (!assignedOnly || assignedToMe(f)) &&
      (priorityFilter === 'all' || String(f.severity || 'unrated').toLowerCase() === priorityFilter) &&
      (formatFilter === 'all' || fmtOf(f.file) === formatFilter) &&
      (sourceFilter === 'all' || (sourceFilter === 'ai' ? isAiAssistedDraft(f) : !isAiAssistedDraft(f))) &&
      (!q || rowModel(f, decisions).issue.toLowerCase().includes(q) || String(f.file).toLowerCase().includes(q)))
    const sorted = sortQueue(filtered, sort)
    const order = { 'needs-review': 0, manual: 0, blocked: 1, 'awaiting-validation': 2, completed: 3 }
    return tab === 'all' || tab === 'active' ? sorted.sort((a, b) => order[workflowStatusOf(a, decisions)] - order[workflowStatusOf(b, decisions)]) : sorted
  }, [queue, tab, sort, search, decisions, assignedOnly, assignees, myEmail, priorityFilter, formatFilter, sourceFilter, autoApprove]) // eslint-disable-line react-hooks/exhaustive-deps

  // Keep a valid selection: default to the first unresolved visible row.
  useEffect(() => {
    if (selectedId != null && visible.some((f) => f.id === selectedId)) return
    const firstOpen = visible.find((f) => !isResolved(f, decisions)) || visible[0]
    setSelectedId(firstOpen ? firstOpen.id : null)
  }, [visible, selectedId, decisions])

  // A decision being written, and the last one refused. Both are keyed by finding id so the pane can
  // only ever show a busy or failed state against the finding it actually belongs to.
  const [savingId, setSavingId] = useState(null)
  const [saveError, setSaveError] = useState(null)
  const [savedMessage, setSavedMessage] = useState('')
  const savedTimerRef = useRef(null)
  useEffect(() => () => clearTimeout(savedTimerRef.current), [])
  // Findings the parent has optimistically removed from `queue` while their write is in flight. Kept
  // only until the write settles, so the review pane never blanks mid-decision.
  const heldRef = useRef(new Map())

  const selected = queue.find((f) => f.id === selectedId) || heldRef.current.get(selectedId) || null
  const reviewHeadingRef = useRef(null)
  const focusReviewRef = useRef(false)
  // One selection entry point for both layouts: at desktop widths this is just setSelectedId; when
  // only one panel fits, picking a finding is also the navigation TO it.
  const selectRow = (id) => { focusReviewRef.current = true; setSelectedId(id); setNarrowPane('detail') }
  useEffect(() => {
    if (!focusReviewRef.current) return
    focusReviewRef.current = false
    reviewHeadingRef.current?.focus()
  }, [selectedId, narrowPane, bulkPreviewOpen])
  const groups = useMemo(() => groupByDocument(visible), [visible])
  const firstDocumentGroup = groups.find(g => g.items.length > 1)?.file
  const documentCollapsed = file => collapsed[file] ?? (file !== firstDocumentGroup)
  const previousSelection = useRef(selectedId)
  useEffect(() => {
    const previous = previousSelection.current
    previousSelection.current = selectedId
    if (previous && selectedId && previous !== selectedId && selected?.file)
      setCollapsed(c => ({ ...c, [selected.file]: false }))
  }, [selectedId, selected?.file])
  useEffect(() => { setCollapsed({}); previousSelection.current = null }, [scanId])
  const clusters = useMemo(() => clusterRows(visible, decisions), [visible, decisions])

  // The finding a cluster row SHOWS. Normally its representative (the first undecided member), but
  // the selected member when the selection is inside it — so a decision made from inside a cluster,
  // or an auto-advance into one, is always visible without forcing the group open.
  const shownOf = (row) => row.items.find((f) => f.id === selectedId) || row.items.find((f) => f.id === row.representativeId) || row.items[0]

  // Navigation units, in display order: what Up/Down step through and what "N of M" counts. A
  // collapsed cluster is ONE unit (the finding it shows); an expanded one contributes its members.
  // Grouping by document leaves every finding its own unit, exactly as before.
  const navFindings = useMemo(() => {
    if (group !== 'issue') return visible
    const out = []
    for (const row of clusters) {
      if (row.type === 'single') { out.push(row.finding); continue }
      if (expandedClusters[row.key]) out.push(...row.items)
      else out.push(shownOf(row))
    }
    return out
  }, [group, clusters, expandedClusters, visible, selectedId]) // eslint-disable-line react-hooks/exhaustive-deps
  // Moving off a failed finding clears its error — the message belongs to that decision, not the page.
  useEffect(() => { setSaveError((e) => (e && e.id !== selectedId ? null : e)) }, [selectedId])

  // W8 — every OTHER unresolved queued finding that shares this one's rule/SC. Drives the
  // "apply to all matching" count and the batch action. Restricted to the actionable approve/apply
  // lanes: a batch decision must not silently sweep in a finding a person already rejected (handoff),
  // one that needs a manual re-author, or a blocked one.
  // W8 — the batch. Its scope is the SELECTED FINDING'S CLUSTER, not "every finding sharing this
  // criterion": what the reviewer sees grouped in the queue is exactly what one decision reaches,
  // and there is no second, invisible notion of "matching". That is stricter than the rule it
  // replaces, which spanned formats — the same criterion is remediated differently in a .docx and a
  // .pdf and the evidence differs, so the run's own policy (PRD Tier C) requires format to match.
  //
  // batchTargetsOf applies the safety filter: unresolved, and in an actionable lane. A manual,
  // blocked, handed-off or already-decided finding is never swept into a batch.
  const selectedCluster = useMemo(
    () => (selected ? clusterOfFinding(clusters, selected.id) : null), [clusters, selected])
  const matchingOf = (f) => {
    const row = clusterOfFinding(clusters, f?.id)
    return batchTargetsOf(row, decisions).filter((x) => x.id !== f?.id)
  }
  const matchingFindings = selected ? matchingOf(selected) : []
  const queueComplete = queue.length > 0 && queue.every((f) => isResolved(f, decisions))
  const automaticCheckingCount = queue.filter(row => row.automaticQueued).length
  const automaticChecksOnly = automaticCheckingCount > 0 && !(counts['needs-review'] || counts.manual || counts.blocked)
  const reviewCompletion = automaticCheckingCount > 0 ? <><b style={{color:'var(--ink)'}}>Automatic checks are queued.</b><p>{automaticCheckingCount} awaiting automatic checks · {counts.completed || 0} recorded results. No fix is marked verified by this queue state.</p></> : counts['awaiting-validation'] > 0 ? <><b style={{color:'var(--ink)'}}>Review decisions saved. Changes are still processing.</b><p>{counts['awaiting-validation']} applying or awaiting verification · {counts.completed || 0} recorded results</p></> : <><b style={{color:'var(--ink)'}}>All review items have recorded outcomes.</b><p>{prog.resolved} reviewed · {counts.completed || 0} recorded results</p></>
  const emptyReviewState = queue.length === 0 || queueComplete || automaticChecksOnly
    ? <div>{reviewCompletion}</div>
    : <p style={{ marginTop: 8 }}>No items are available in {WORKFLOW_LABELS[tab] || 'Review'}. Choose another status from the inbox.</p>

  // Act on a finding, then auto-advance to the next unresolved one — the behaviour that makes the
  // queue feel like a controlled worklist rather than a scroll through an audit report.
  //
  // ADVANCING IS CONDITIONAL ON THE WRITE SUCCEEDING. This used to call onDecide and move on in the
  // same breath: the decision was fire-and-forget, so a server refusal advanced the reviewer to the
  // next finding anyway and the only trace was a banner rendered OUTSIDE this component, above the
  // whole inbox. The reviewer saw a decision they had made land on a finding that had scrolled past.
  // Now the save is awaited — on failure the item stays selected, the error is stated inline next to
  // the buttons that failed, and nothing advances.
  async function act(f, decision) {
    if (readOnlyRef.current || !f || savingId != null) return
    // Recheck the current server-projected row: an old pane callback must not
    // manually approve a proposal admitted automatically since it rendered.
    if (decision?.state === 'accepted' && (currentQueueRef.current.find(row => row.id === f.id)?.automaticQueued || requiresPdfSourceEditing(f))) return
    // The parent removes the row from `queue` optimistically and puts it back only if the write
    // fails, so hold our own reference to keep the pane rendering THIS finding while it is in flight.
    heldRef.current.set(f.id, f)
    setSavingId(f.id)
    setSaveError(null)
    let ok = true
    try {
      // `onDecide` returns a promise once the parent has a write to report on; older call sites
      // return undefined, which awaits to undefined and keeps the previous advance-always behaviour.
      await onDecide?.(f, decision)
    } catch (e) {
      ok = false
      setSaveError({ id: f.id, message: e?.message || String(e || 'The server did not accept it.') })
    }
    setSavingId(null)
    if (!ok) { setSelectedId(f.id); return }   // stay put — the decision was NOT recorded
    heldRef.current.delete(f.id)
    // `visible` is the list as it was when this handler was created, i.e. BEFORE the parent removed
    // the decided row — which is exactly the ordering the "next" finding should be taken from.
    const nextDecisions = { ...decisions, [f.id]: decision }
    setSavedMessage(`${rowModel(f, decisions).issue} saved. Moving to the next finding.`)
    clearTimeout(savedTimerRef.current)
    savedTimerRef.current = setTimeout(() => setSavedMessage(''), 2400)
    focusReviewRef.current = true
    setSelectedId(nextUnresolvedId(visible, f.id, nextDecisions))
  }

  function applyToMatching(f) {
    if (readOnlyRef.current || !f || savingId != null) return
    setBatchScopeIds([f.id, ...matchingOf(f).map(item => item.id)])
    setBulkPreviewOpen(true)
  }
  function batchResult(results) {
    const failed = results.find(r => r.state !== 'recorded')
    const nextDecisions = { ...decisions }
    results.filter(r => r.state === 'recorded').forEach(r => { nextDecisions[r.id] = { state: 'accepted' } })
    setSavedMessage(`${results.filter(r => r.state === 'recorded').length} approval decisions recorded. Writing and verification remain separate.`)
    focusReviewRef.current = true
    setSelectedId(failed?.id ?? nextUnresolvedId(visible, selectedId, nextDecisions))
  }

  // Explicit linear navigation through the visible queue — Previous / Next step the SELECTION without
  // acting, so a reviewer can look before deciding and always sees their place ("N of M").
  const visIds = navFindings.map((f) => f.id)
  const curIdx = visIds.indexOf(selectedId)
  const position = curIdx >= 0 ? curIdx + 1 : 0
  const goPrev = () => { if (curIdx > 0) setSelectedId(visIds[curIdx - 1]) }
  const goNext = () => { if (curIdx >= 0 && curIdx < visIds.length - 1) setSelectedId(visIds[curIdx + 1]) }

  // ── Keyboard navigation + screen-reader support for the queue ──
  // The queue is operable end to end without a mouse: one Tab lands on the selected row (roving
  // tabindex on QueueRow), then Up/Down (or j/k) step the selection and Home/End jump to the ends. A
  // polite live region announces the moved-to finding and its N-of-M place — and the SAME announcement
  // fires on the auto-advance after a decision, so a screen-reader user always knows where the
  // workspace just went. (An accessibility tool should itself be exemplary here.)
  const listRef = useRef(null)   // the queue's scroll container; catches key events bubbling from the rows
  const kbNavRef = useRef(false) // set when the selection moved by keyboard, so focus follows it
  const onQueueKey = (e) => {
    const k = e.key
    if (k === 'ArrowDown' || k === 'j') { e.preventDefault(); kbNavRef.current = true; goNext() }
    else if (k === 'ArrowUp' || k === 'k') { e.preventDefault(); kbNavRef.current = true; goPrev() }
    else if (k === 'Home') { e.preventDefault(); if (visIds.length) { kbNavRef.current = true; setSelectedId(visIds[0]) } }
    else if (k === 'End') { e.preventDefault(); if (visIds.length) { kbNavRef.current = true; setSelectedId(visIds[visIds.length - 1]) } }
  }
  // Move focus to the newly-selected row ONLY when the change came from the keyboard, so a mouse click
  // (or the auto-advance after a decision) never yanks focus out from under the reviewer.
  useEffect(() => {
    if (!kbNavRef.current) return
    kbNavRef.current = false
    listRef.current?.querySelector('[aria-current="true"]')?.focus()
  }, [selectedId])
  const announce = visIds.length === 0
    ? 'No findings in this view.'
    : (selected ? `Finding ${position} of ${visIds.length}: ${rowModel(selected, decisions).issue}, in ${selected.file}.` : '')

  // The two workspace panes, defined once and placed differently per layout (side by side, stacked,
  // or guided-only). The layout toggle sits on the guided header — the pane that is always shown.
  const guidedHeader = (
    <div style={{ flex: '0 0 auto', padding: '8px 12px', borderBottom: '1px solid var(--line,#e2dce4)',
                  display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
        {narrow && (
          <button type="button" onClick={() => setNarrowPane('queue')}
                  style={{ fontSize: 11.5, fontWeight: 600, padding: '4px 10px', borderRadius: 8, cursor: 'pointer',
                           border: '1px solid var(--line,#e2dce4)', background: 'var(--bg,#fff)' }}>
            &larr; Back to queue
          </button>
        )}
        <span style={{ fontSize: 13, fontWeight: 700 }}>Guided remediation</span>
        {(selected?.automaticReason || selected?.automaticQueued) && <p className="automatic-review-queued" role="status"><b>{autoApprove === true ? (automaticReviewResponsibility(selected, decisions) === 'human' ? 'You' : selected.automaticQueued ? 'ACP' : 'Status check') : selected.automaticQueued ? 'ACP' : selected.automaticDisposition?.owner || 'You'}: </b>{autoApprove === true && workflowStatusOf(selected, decisions) === 'awaiting-validation' && automaticReviewResponsibility(selected, decisions) === 'check' ? 'Approval is already recorded. Verification is still pending; no additional approval is needed.' : selected.automaticReason || 'Automatic eligibility checks are queued.'}{selected.automaticQueued && <> This is not yet an applied or verified fix.</>}</p>}
      </span>
    </div>
  )
  const guidedBody = (
    <>
      <DetailPane f={selected} decisions={decisions} readOnly={readOnly} automaticMode={autoApprove === true} preparingProposals={preparingProposals} onDecide={act} onOpenWord={onOpenWord} onRecheck={onRecheck} onOpenPlan={onOpenPlan} onVerifySaved={onVerifySaved}
                  headingRef={reviewHeadingRef}
                  saving={savingId != null && savingId === selected?.id}
                  error={saveError && selected && saveError.id === selected.id ? saveError : null}
                  matchingFindings={matchingFindings} matchingReadyCount={[selected, ...matchingFindings].filter(f => f && readyAcrossScan.some(ready => ready.id === f.id)).length} legacyApprovalControls={legacyApprovalControls} onApplyToMatching={applyToMatching} cluster={selectedCluster?.type === 'cluster' ? selectedCluster : null}
                  draft={selected ? (drafts[selected.id] ?? null) : null}
                  onDraftChange={(v) => selected && setDrafts((d) => ({ ...d, [selected.id]: v }))}
                  detailExtra={renderDetailExtra ? renderDetailExtra(selected) : null}
                  emptyState={emptyReviewState} />
    </>
  )
  return (
    <div className="rinbox-wrap">
      <button type="button" className="ghost small" aria-expanded={!cardCollapsed} onClick={() => setCardCollapsed(value => !value)} style={{ margin: 12 }}>
        {cardCollapsed ? 'Expand remediation card' : 'Collapse remediation card'}
      </button>
      <div hidden={cardCollapsed}>
      {/* Retired skip-inspection entry is retained only for legacy restoration. Q3 authorizes automatic publishing. */}
      {legacyApprovalControls && !bulkPreviewOpen && !readOnly && onPublish && <div style={{ padding: '14px 22px', borderBottom: '1px solid var(--line)' }}>
        <button type="button" className="primary" disabled={savingId != null} onClick={onPublish}>Skip inspection and publish →</button>
        <p className="muted" style={{ margin: '8px 0 0' }}>Choose verified copies or publish saved copies with remaining issues on the next screen. This does not approve pending suggestions or mark anything inspected.</p>
      </div>}

      {/* Screen-reader announcer: the selected finding and its place in the queue, updated on every
          selection change — manual, keyboard, or the auto-advance after a decision. Visually hidden. */}
      <div aria-live="polite" role="status"
           style={{ position: 'absolute', width: 1, height: 1, padding: 0, margin: -1, overflow: 'hidden', clip: 'rect(0 0 0 0)', whiteSpace: 'nowrap', border: 0 }}>
        {announce}
      </div>
      {savedMessage && <div className="remediation-saved" role="status" aria-live="polite">✓ {savedMessage}</div>}
      {/* One review workspace. Status is an optional filter, not a separate approval step. */}
      <div className="rinbox-topbar" style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap', padding: '9px 16px', background: '#1f2b3a', color: '#fff', borderRadius: '12px 12px 0 0' }}>
        <strong>Review</strong>
        <span style={{ flex: 1, fontSize: 12 }}>{queue.length} items · {counts['awaiting-validation'] || 0} awaiting verification · {counts.completed || 0} recorded results</span>
        <select aria-label="Filter by status" value={tab} disabled={savingId != null}
          onChange={event => { setBulkPreviewOpen(false); setBatchScopeIds(null); setTab(event.target.value) }}>
          <option value="review">{autoApprove === true ? `Needs your input (${queue.filter(row => automaticReviewResponsibility(row, decisions) === 'human').length})` : `Needs review (${(counts['needs-review'] || 0) + (counts.manual || 0) + (counts.blocked || 0)})`}</option>
          <option value="active">Remaining ({queue.length - (counts.completed || 0)})</option>
          {autoApprove === true && <option value="status-check">{autoApprove === true ? `ACP status checks (${queue.filter(row => automaticReviewResponsibility(row, decisions) === 'check').length})` : 'Status checks'}</option>}
          <option value="all">All statuses ({queue.length})</option>
          {WORKFLOW_TABS.map(status => <option key={status} value={status}>{WORKFLOW_LABELS[status]} {counts[status] || 0}</option>)}
        </select>
      </div>
      {(!bulkPreviewOpen || readyAcrossScan.length > 0) && <section className="run-approval-summary" aria-label="Whole-run approval">
        <div><strong>Review and verify changes</strong>
          {approvalExplanation && <p role="status">{approvalExplanation}</p>}
          {afterRelease && <p role="status">Outstanding review decisions remain available after publication. A newly saved change requires a fresh check and a new publication authorization; previously delivered copies remain recorded. {onPublish && <button type="button" className="linklike" onClick={onPublish}>Open Release for updated copies</button>}</p>}
          {/* Keep approval readiness separate from verification and completed counts. */}
          {legacyApprovalControls ? <p>{runCounts.ready} ready review items · {runCounts.individual} need proposal information or individual review · {runCounts.inspection} applied changes available to inspect · {runCounts.manual} manual review items</p> : <p>{runCounts.ready} ready to apply · {runCounts.individual} still need a valid proposal or individual review · {runCounts.manual} need manual work</p>}
          <p>{autoApprove === true ? 'Auto-apply is on. ACP automatically applies eligible AI suggestions. Items needing your judgment remain below.' : runCounts.ready ? 'Approve the ready fixes together. ACP will save the changes and check the results.' : preparingProposals ? 'Please wait for remediation to finish preparing suggestions.' : 'No fixes are ready to apply. Ready AI fixes will apply automatically when auto-apply is on.'}</p>
          {preparingProposals && <p role="status">Preparing proposals — remediation is still processing. Readiness updates as work finishes.</p>}
          {!legacyApprovalControls && Object.keys(unreadyReasons).length > 0 && <details><summary>Why some fixes aren’t ready</summary><ul>{Object.entries(unreadyReasons).map(([reason, count]) => <li key={reason}>{count} · {reason === 'Version unavailable — review individually' ? 'Need fresh proposal versions' : reason === 'Missing proposal' ? 'Need a complete suggestion' : reason}</li>)}</ul>{onOpenPlan && <button type="button" className="linklike" disabled={readOnly} onClick={onOpenPlan}>Refresh suggestions from the remediation plan</button>}</details>}
        </div>
        <div className="run-approval-actions" aria-label="Remediation actions">
          {legacyApprovalControls && <button type="button" className={readyAcrossScan.length ? 'primary' : 'ghost'} disabled={savingId != null || (readyAcrossScan.length > 0 && (readOnly || !onDecide))}
            onClick={() => { setBatchScopeIds(null); setBulkPreviewOpen(true); if (readyAcrossScan.length) { if (legacyApprovalControls) setConfirmRunRequest(n => n + 1); else setApplyRunRequest(n => n + 1) } }}>
            {readyAcrossScan.length ? legacyApprovalControls ? `Approve all ready in this run (${readyAcrossScan.length})` : `Apply ready fixes (${readyAcrossScan.length})` : 'View run readiness'}
          </button>}
          {/* Publication and readiness actions are intentionally absent from Review. */}
          {!legacyApprovalControls && <label className={`run-auto-approve-switch${autoApprove === true ? ' is-on' : ''}`}>
            <input type="checkbox" role="switch" aria-label="Auto-apply AI fixes" checked={autoApprove === true}
              disabled={readOnly || autoApprove === null || autoApproveSaving || !onAutoApproveChange}
              onChange={event => onAutoApproveChange?.(event.target.checked)} />
            <span className="run-auto-approve-switch__track" aria-hidden="true"><span /></span>
            <span>Auto-apply AI fixes <b>{autoApproveSaving ? 'Saving…' : autoApprove === null ? autoApproveError ? 'Unavailable' : 'Checking…' : autoApprove ? 'On' : 'Off'}</b></span>
          </label>}
          {autoApproveError && <div role="alert" className="run-auto-approve-error"><p>{autoApproveError}</p>{onAutoApproveRetry && <button type="button" className="ghost small" disabled={readOnly || autoApproveSaving} onClick={onAutoApproveRetry}>Retry AI approval check</button>}</div>}
          {autoApproveNotice && <div className="run-auto-approve-toast" role="status" aria-live="polite" aria-atomic="true"><button type="button" className="ghost small" aria-label="Dismiss automatic approval notification" onClick={onDismissAutoApproveNotice}>×</button><b>AI reviews will be automatically approved.</b><p>Ready AI fixes will be applied automatically. Items needing manual work stay in the review queue.</p></div>}
        </div>
      </section>}
      {/* Persistent progress bar — the selected document's remediation progress + ETA, above the panes. */}
      {!bulkPreviewOpen && <>
        <p className="remediation-category-help">{{
          'status-check': 'These items have no confirmed automatic admission yet. ACP status or recovery needs checking; they are not verified fixes.',
          review: autoApprove === true ? 'Only changes needing your judgment or manual editing appear here. Eligible automatic work and status checks remain available in their own views.' : 'Approve a suggestion to move it to Processing. Results contain verified fixes and remaining work; approval alone does not verify a fix.',
          active: 'Verified fixes move to Results automatically. Changes awaiting verification remain Pending.',
          all: 'Select an item to approve a proposal, make a manual correction, or check its result. Items awaiting automatic verification do not need another approval.',
          'needs-review': 'AI suggestions have proposed changes you can approve. Already-applied changes are available for individual review.',
          manual: 'These issues need your input. Select an issue to see the required edit and instructions for fixing the source document.',
          'awaiting-validation': 'These changes are awaiting writing or verification. Another approval is not needed here.',
          blocked: 'These issues cannot continue yet. Select an issue to see what needs attention.',
          completed: 'Results include verified fixes and remaining work. Select an item to inspect its recorded outcome.',
        }[tab]}</p>
        <WorkspaceProgress queue={queue} decisions={decisions} selected={selected} />
        <ReviewQueueTabs automatic={autoApprove === true} queue={queue} decisions={decisions} scanId={scanId} value={tab} disabled={savingId != null}
          onChange={value => { setBulkPreviewOpen(false); setBatchScopeIds(null); setTab(value) }} />
      </>}
      <div className="rinbox" data-layout="two-column" data-narrow={narrow ? narrowPane : undefined} ref={rowRef} style={{ display: bulkPreviewOpen ? 'none' : 'flex', gap: 0, border: '1px solid var(--line,#e2dce4)', borderRadius: '0 0 12px 12px', overflow: 'hidden', minHeight: 480 }}>
      {/* ── Left: the work queue — find and select the next finding (resizable) ── */}
      <div className="rinbox-queuepane" hidden={narrow && narrowPane !== 'queue'}
           style={{ ...(narrow ? { flex: '1 1 auto', maxWidth: 'none' } : { flex: `0 0 ${leftW}%`, maxWidth: `${leftW}%` }),
                    display: narrow && narrowPane !== 'queue' ? 'none' : 'flex', flexDirection: 'column', minHeight: 480 }}>
        <div style={{ flex: '0 0 auto', padding: '10px 12px', borderBottom: '1px solid var(--line,#e2dce4)' }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 8 }}>Review queue</div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <input type="search" value={search} onChange={(e) => setSearch(e.target.value)}
                   placeholder="Search documents" aria-label="Search documents"
                   style={{ flex: '1 1 auto', minWidth: 0, fontSize: 13, padding: '6px 10px', borderRadius: 8, border: '1px solid var(--line,#e2dce4)' }} />
            <select value={group} onChange={(e) => setGroup(e.target.value)} aria-label="Group findings" title="Group findings"
                    style={{ flex: '0 0 auto', fontSize: 11.5, padding: '5px 6px', borderRadius: 6, border: '1px solid var(--line,#e2dce4)' }}>
              <option value="issue">By issue</option>
              <option value="document">By document</option>
            </select>
            <select value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort findings" title="Sort findings"
                    style={{ flex: '0 0 auto', fontSize: 11.5, padding: '5px 6px', borderRadius: 6, border: '1px solid var(--line,#e2dce4)' }}>
              {SORTS.map((s) => <option key={s} value={s}>{SORT_LABEL[s]}</option>)}
            </select>
          </div>
          <div className="remediation-filters" aria-label="Filter remediation inbox">
            <select value={priorityFilter} onChange={(e) => setPriorityFilter(e.target.value)} aria-label="Filter by priority">
              <option value="all">All priorities</option>
              <option value="critical">Critical</option><option value="serious">Serious</option>
              <option value="moderate">Moderate</option><option value="minor">Minor</option><option value="unrated">Unrated</option>
            </select>
            <select value={formatFilter} onChange={(e) => setFormatFilter(e.target.value)} aria-label="Filter by file format">
              <option value="all">All formats</option><option value="docx">DOCX</option><option value="pdf">PDF</option>
              <option value="pptx">PPTX</option><option value="xlsx">XLSX</option>
            </select>
            <select value={sourceFilter} onChange={(e) => setSourceFilter(e.target.value)} aria-label="Filter by fix source">
              <option value="all">All fix sources</option>
              <option value="ai">AI-assisted drafts ({aiDraftCount})</option>
              <option value="other">Automatic &amp; manual</option>
            </select>
            {(priorityFilter !== 'all' || formatFilter !== 'all' || sourceFilter !== 'all') && (
              <button className="linklike" onClick={() => { setPriorityFilter('all'); setFormatFilter('all'); setSourceFilter('all') }}>Clear filters</button>
            )}
            <details className="remediation-shortcuts">
              <summary>Keyboard help</summary>
              <span>↑/↓ or J/K: move · Home/End: first/last · Enter: open selected item</span>
            </details>
          </div>
          {/* Retired sidebar approval control; the whole-scan action above is authoritative. */}
          {legacyApprovalControls && (tab === 'review' || tab === 'active' || tab === 'all' || tab === 'needs-review') && <button type="button" className="ghost" aria-expanded={bulkPreviewOpen}
                  disabled={savingId != null}
                  onClick={() => { setBatchScopeIds(null); setBulkPreviewOpen(open => !open) }}
                  style={{ marginTop: 8, fontWeight: 700 }}>
            Bulk approve ready proposals · all documents
          </button>}
          {/* "Assigned to me" filter + a context assign chip for the selected document. Mirrors the
              #417 backend (files_assigned_to); shown only for a signed-in reviewer with an assign
              action, so it is never a dead control. Assigning is per-DOCUMENT (a file's whole set of
              findings), which is how the backend keys the assignee. */}
          {myEmail && onAssign && (
            <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap', marginTop: 8 }}>
              <button type="button" onClick={() => setAssignedOnly((v) => !v)} aria-pressed={assignedOnly}
                      title="Show only findings in documents assigned to you"
                      style={{ fontSize: 11.5, fontWeight: 600, padding: '3px 10px', borderRadius: 20, cursor: 'pointer',
                               border: `1px solid ${assignedOnly ? 'transparent' : 'var(--line,#e2dce4)'}`,
                               background: assignedOnly ? 'var(--accent,#3b6fd6)' : 'var(--bg,#fff)',
                               color: assignedOnly ? '#fff' : 'inherit' }}>
                Assigned to me{myAssignedCount > 0 ? ` (${myAssignedCount})` : ''}
              </button>
              {selected && (assignees[selected.file] === myEmail
                ? <button type="button" onClick={() => onAssign(selected.file, null)}
                          title={`Unassign ${selected.file} from you`}
                          style={{ fontSize: 11.5, padding: '3px 10px', borderRadius: 20, cursor: 'pointer',
                                   border: '1px solid var(--line,#e2dce4)', background: 'var(--bg,#fff)', color: 'inherit' }}>
                    ✓ Assigned to you
                  </button>
                : <button type="button" onClick={() => onAssign(selected.file, myEmail)}
                          title={`Assign ${selected.file} to you`}
                          style={{ fontSize: 11.5, padding: '3px 10px', borderRadius: 20, cursor: 'pointer',
                                   border: '1px solid var(--line,#e2dce4)', background: 'var(--bg,#fff)', color: 'var(--muted,#5b6774)' }}>
                    + Assign to me
                  </button>)}
            </div>
          )}
          <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 8 }}>
            {/* "reviewed" (a decision is recorded), NOT "resolved" — an approved fix awaiting the
                re-scan is reviewed but not yet Completed, so this never contradicts the tab counts. */}
            <span className="muted" style={{ fontSize: 11.5, fontWeight: 600 }}>{prog.resolved} of {prog.total} reviewed · tasks and change inspections</span>
          </div>
        </div>
        <div ref={listRef} onKeyDown={onQueueKey} aria-label="Findings — use Up and Down arrow keys to move between them"
             style={{ flex: '1 1 auto', overflowY: 'auto' }}>
          <CompletionDrain queue={queue} decisions={decisions} scanId={scanId} active={tab !== 'completed' && tab !== 'all'} />
          {visible.length === 0 ? (
            <div className="muted" style={{ padding: 16, fontSize: 13 }}>
              {queue.length === 0 || queueComplete || automaticChecksOnly
                ? <>{reviewCompletion}</>
                : search.trim()
                ? <>No findings match “{displayText(search.trim())}”. <button className="linklike" onClick={() => setSearch('')}>Clear search</button></>
                : priorityFilter !== 'all' || formatFilter !== 'all' || sourceFilter !== 'all'
                ? <>No findings match these filters. <button className="linklike" onClick={() => { setPriorityFilter('all'); setFormatFilter('all'); setSourceFilter('all') }}>Clear filters</button></>
                : assignedOnly
                ? <>Nothing in this view is assigned to you. <button className="linklike" onClick={() => setAssignedOnly(false)}>Show all</button></>
                : <>No items in {WORKFLOW_LABELS[tab] || 'Review'}. {tab !== 'needs-review' && <button className="linklike" onClick={() => setTab('needs-review')}>Review AI suggestions</button>}</>}
            </div>
          ) : group === 'issue' ? clusters.map((row) => (
            row.type === 'single'
              ? <QueueRow key={row.key} f={row.finding} decisions={decisions} automatic={autoApprove === true}
                          selected={row.finding.id === selectedId} onSelect={selectRow} showFile />
              : <ClusterRow key={row.key} row={row} shown={shownOf(row)} decisions={decisions} automatic={autoApprove === true}
                            selectedId={selectedId} onSelect={selectRow}
                            expanded={!!expandedClusters[row.key]} onToggle={toggleCluster} />
          )) : groups.map((g) => (
            // A document with a SINGLE finding needs no expandable group header — the row itself
            // names the file. Only multi-finding documents get the collapsible 📄 header, so the file
            // is stated once either way.
            g.items.length === 1 ? (
              <QueueRow key={g.items[0].id} f={g.items[0]} decisions={decisions} automatic={autoApprove === true}
                        selected={g.items[0].id === selectedId} onSelect={selectRow} showFile />
            ) : (
              <div key={g.file}>
                <button type="button" className="rinbox-document-toggle" aria-expanded={!documentCollapsed(g.file)} onClick={() => setCollapsed((c) => ({ ...c, [g.file]: !documentCollapsed(g.file) }))}
                        style={{ display: 'flex', width: '100%', alignItems: 'center', gap: 8, padding: '6px 12px', cursor: 'pointer',
                                 border: 'none', borderBottom: '1px solid var(--line,#e2dce4)', background: 'var(--surface-2,#f6f5f8)', fontSize: 12, fontWeight: 700 }}>
                  <span aria-hidden="true">{documentCollapsed(g.file) ? '▸' : '▾'}</span>
                  <span style={{ flex: '1 1 auto', minWidth: 0, textAlign: 'left', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>📄 {g.file}</span>
                  <span className="muted" style={{ fontWeight: 400 }}>{g.items.length}</span>
                </button>
                {!documentCollapsed(g.file) && g.items.map((f) => (
                  <QueueRow key={f.id} f={f} decisions={decisions} automatic={autoApprove === true} selected={f.id === selectedId} onSelect={selectRow} showFile={false} />
                ))}
              </div>
            )
          ))}
        </div>
      </div>

      {/* Divider between the inbox and the workspace — present whenever both are on screen. */}
      {!narrow && (
        <Divider orientation="vertical" label="Resize the inbox" value={leftW} min={28} max={40}
                 onDrag={dragLeft} onNudge={(d) => setLeftW((w) => clamp(w + d * 2, 28, 40))} />
      )}

      {/* ── The workspace: the review canvas, plus the document preview when it is open ──
          ONE tree for all three states, rather than a branch per layout. The guided column keeps the
          same position in the element tree whether the preview is closed, beside it, or below it, so
          React reconciles it instead of remounting — which is what lets the reviewer open the full
          preview mid-decision and come back to their scroll position and their unsaved edit. */}
      <div className="rinbox-workspace" hidden={narrow && narrowPane !== 'detail'}
           style={{ flex: '1 1 auto', minWidth: 0, minHeight: 480,
                    display: narrow && narrowPane !== 'detail' ? 'none' : 'flex',
                    flexDirection: 'row' }}>
        <div style={{ flex: '1 1 auto', minWidth: 0, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          {guidedHeader}
          <div className="rinbox-guided-scroll"
               style={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto', overflowX: 'hidden' }}>
            {guidedBody}
          </div>
        </div>

      </div>
      </div>
      <div hidden={!bulkPreviewOpen} ref={batchPanelRef} tabIndex={-1}>
        <button type="button" className="ghost" disabled={savingId != null}
                onClick={() => { setBulkPreviewOpen(false); focusReviewRef.current = true; setNarrowPane('detail') }}>Return to individual review</button>
        <BatchReviewSelection
          visible={(batchScopeIds ? queue.filter(f => batchScopeIds.includes(f.id)) : queue).filter(row => !row.automaticQueued)}
          decisions={decisions} drafts={drafts}
          confirmRequest={confirmRunRequest} onConfirmRequestHandled={() => setConfirmRunRequest(0)} preparingProposals={preparingProposals} onOpenPlan={onOpenPlan}
          applyRequest={applyRunRequest} onApplyRequestHandled={() => setApplyRunRequest(0)}
          // What this key NAMES is the set of findings the batch covers — and that set is the
          // `visible` prop above, which reads scanId and batchScopeIds and does not read `tab` at
          // all. `tab` was in the key anyway, so switching category counted as a scope change and
          // reset the panel: a reviewer who had frozen five proposals and touched a tab lost all
          // five, with no prompt, no undo, and nothing on screen to say it had happened. The stale
          // ARMED CONFIRMATION that reset also cleared is still cleared — by `open` below, which is
          // what actually goes stale when the panel closes.
          scopeKey={JSON.stringify([scanId, batchScopeIds])}
          open={bulkPreviewOpen}
          scopeLabel={batchScopeIds ? 'Selected matching issue in this scan' : 'All documents in this scan'}
          readyOutsideScope={batchScopeIds ? readyAcrossScan.filter(f => !batchScopeIds.includes(f.id)).length : 0}
          onShowAllReady={() => setBatchScopeIds(null)}
          onReviewExcluded={() => { setBulkPreviewOpen(false); setBatchScopeIds(null); focusReviewRef.current = true; setNarrowPane('detail') }}
          disabled={readOnly || (savingId != null && savingId !== 'selected-batch')}
          onBusy={busy => setSavingId(busy ? 'selected-batch' : null)}
          onDecide={onDecide} onResult={batchResult} />
      </div>
      {/* Sticky workflow guide (Show → Review → Verify) + Previous / N of M / Next navigation. */}
      {!bulkPreviewOpen && <WorkspaceFooter position={position} total={visIds.length} onPrev={goPrev} onNext={goNext}
                       activeStep={selected && !optionalInspectionOf(selected) && !recordedReviewDecision(selected, decisions) ? workflowStepIndex(selected, decisions) : null} />}
      </div>
    </div>
  )
}
