import { useEffect, useRef, useState } from 'react'
import { TARGET_REPLACED_EXCLUSION, batchDecision, exclusionReason, proposalValues, selectionProblem, snapshotFinding } from './batchReviewSelection.js'
import LiveCounter from './LiveCounter.jsx'
import './batch-review-selection.css'

const EXCLUSION_HELP = {
  'Missing proposal': 'no drafted value yet, or fewer drafts than findings',
  'Version unavailable — review individually': 'no recorded proposal version to approve against',
  'Manual work': 'needs an edit you make in the source document',
  'Already reviewed': 'already decided; no further approval needed',
  'Already applied — review individually': 'already written; check its verification instead',
  'Stale — refresh and review': 'the source changed after the draft was made',
  'Unsaved edit — review individually': 'you edited this value; save or discard it first',
  'Blocked or unavailable': 'no supported automatic route for this issue',
  [TARGET_REPLACED_EXCLUSION]: 'another verified fix removed what this item described; it is a recorded result',
}
// Finished work, not work this approval skips: decided, or replaced by a verified change.
const SETTLED_REASONS = new Set(['Already reviewed', 'Approval recorded', TARGET_REPLACED_EXCLUSION])

const countFindings = f => Number.isSafeInteger(f._raw?.finding_count) && f._raw.finding_count >= 0 ? f._raw.finding_count : 1
const PAGE_SIZE = 10
export default function BatchReviewSelection({ visible = [], decisions = {}, drafts = {}, scopeKey, scopeLabel = 'Current approval scope', onDecide, onResult, onBusy, onReviewExcluded, onShowAllReady, readyOutsideScope = 0, confirmRequest = 0, onConfirmRequestHandled, applyRequest = 0, onApplyRequestHandled, preparingProposals = false, onOpenPlan, disabled = false, open = true }) {
  const handledApply = useRef(0)
  const [entries, setEntries] = useState([])
  const [page, setPage] = useState(0)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [results, setResults] = useState([])
  const [attempt, setAttempt] = useState(null)
  const [attemptResults, setAttemptResults] = useState([])
  const [announcement, setAnnouncement] = useState('')
  const attemptNumber = useRef(0)
  const lock = useRef(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const [groupBy, setGroupBy] = useState('file')
  const heading = useRef(null)
  const latest = useRef({ visible, decisions, drafts, scopeKey })
  latest.current = { visible, decisions, drafts, scopeKey }
  // A real scope change invalidates the selection — different findings, different proposals.
  useEffect(() => { setEntries([]); setConfirming(false); setPage(0); setAnnouncement('') }, [scopeKey])
  // Closing the panel does not. It disarms the CONFIRMATION — reopening must never land on a
  // confirm step nobody asked for this time — and leaves the chosen proposals alone, so coming back
  // finds the work where it was left. Each entry is still revalidated at approve time
  // (selectionProblem), so a selection that sat through a change is refused, not silently applied.
  useEffect(() => { if (!open) { setConfirming(false); setPage(0) } }, [open])
  useEffect(() => { if (confirming) heading.current?.focus() }, [confirming])
  const findingCount = entries.reduce((n, e) => n + countFindings(e.finding), 0)
  const problems = entries.map(e => selectionProblem(e, visible, decisions, drafts))
  const successfulIds = new Set(results.filter(r => r.state === 'recorded').map(r => r.id))
  const uncertainIds = new Set(results.filter(r => r.state === 'uncertain').map(r => r.id))
  const selectable = f => !exclusionReason(f, decisions, drafts) && !successfulIds.has(f.id) && !uncertainIds.has(f.id)
  const eligible = visible.filter(selectable)
  const readyCount = eligible.reduce((n, f) => n + countFindings(f), 0)
  const readyProposals = eligible.reduce((n, f) => n + proposalValues(f).length, 0)
  const shown = [...(confirming ? entries.map(e => e.finding) : eligible)].sort((a, b) =>
    String(groupBy === 'file' ? a.file : a.ruleId || a.rule_id || a.title).localeCompare(String(groupBy === 'file' ? b.file : b.ruleId || b.rule_id || b.title)))
  const pages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE))
  const currentPage = Math.min(page, pages - 1)
  const pageItems = shown.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE)
  // Plain-language "so what do I do about it" for each reason exclusionReason can return.
  // Keep in sync with batchReviewSelection.js::exclusionReason -- a reason with no entry
  // still renders, it just carries no advice.
  const exclusions = visible.reduce((out, f) => {
    const reason = exclusionReason(f, decisions, drafts)
      || (uncertainIds.has(f.id) ? 'Decision uncertain — refresh before retrying' : successfulIds.has(f.id) ? 'Approval recorded' : null)
    if (reason) out[reason] = (out[reason] || 0) + 1
    return out
  }, {})
  // Work that is FINISHED is not work this approval is skipping. Rolling both into one headline made
  // it read as a warning — "17 review items outside this approval" on a screen whose own header
  // counted 9 — when most of that 17 was already-decided work that was never a candidate. Split, so
  // the headline number is the one a reviewer might actually need to go and do something about.
  const settledCount = Object.entries(exclusions)
    .filter(([reason]) => SETTLED_REASONS.has(reason))
    .reduce((n, [, count]) => n + count, 0)
  const notIncludedCount = Object.values(exclusions).reduce((a, b) => a + b, 0) - settledCount
  // Derived from that split rather than recomputed, so the empty-state breakdown below and the
  // collapsed summary can never disagree about how many items are in scope.
  const excludedTotal = settledCount + notIncludedCount
  const confirmAllReady = () => {
    // This explicit action freezes the complete eligible scope, independent of inspection pages.
    // Reuse retained request identities on retries; never substitute a refreshed proposal.
    setEntries(eligible.map(f => entries.find(e => e.finding.id === f.id) || snapshotFinding(f)))
    setConfirming(true); setPage(0)
  }
  // A whole-run request freezes the eligible set immediately; later live updates never expand it.
  useEffect(() => { if (confirmRequest) { confirmAllReady(); onConfirmRequestHandled?.() } }, [confirmRequest])
  const toggle = f => {
    setConfirming(false)
    setEntries(old => old.some(e => e.finding.id === f.id) ? old.filter(e => e.finding.id !== f.id) : [...old, snapshotFinding(f)])
  }
  useEffect(() => {
    if (!applyRequest) { handledApply.current = 0; return }
    if (handledApply.current === applyRequest) return
    handledApply.current = applyRequest
    onApplyRequestHandled?.()
    if (!open || disabled) return
    const batch = eligible.map(f => entries.find(e => e.finding.id === f.id) || snapshotFinding(f))
    setEntries(batch)
    void approve(batch)
  }, [applyRequest])
  async function approve(requestedEntries) {
    const batch = Array.isArray(requestedEntries) ? requestedEntries : entries
    if (lock.current || disabled || !batch.length || batch.some(e => selectionProblem(e, visible, decisions, drafts)) || !onDecide) return
    lock.current = true; setBusy(true); onBusy?.(true)
    const initialScope = scopeKey, outcomes = []
    setAttempt({ scopeKey, number: ++attemptNumber.current, items: batch.length, findings: batch.reduce((n, e) => n + countFindings(e.finding), 0),
      proposals: batch.reduce((n, e) => n + proposalValues(e.finding).length, 0),
      files: new Set(batch.map(e => e.finding.file)).size })
    setAttemptResults([])
    setAnnouncement(`Recording approval for ${batch.length} review items.`)
    // Sequential writes bound pressure and permit a changed scope/source to stop unsent work.
    for (const entry of batch) {
      const current = latest.current
      const problem = !mounted.current || current.scopeKey !== initialScope ? 'Review scope changed' : selectionProblem(entry, current.visible, current.decisions, current.drafts)
      if (problem) { outcomes.push({ id: entry.finding.id, state: 'failed', message: problem }); setAttemptResults([...outcomes]); continue }
      try {
        await onDecide(entry.finding, batchDecision(entry))
        outcomes.push({ id: entry.finding.id, state: 'recorded' })
      } catch (error) {
        // Transport ambiguity must never turn into a fresh write with another request id.
        const uncertain = error?.changes === 'unknown' || error instanceof TypeError || !error?.status
          || (error.status >= 500 && error.changes !== 'none')
        outcomes.push({ id: entry.finding.id, state: uncertain ? 'uncertain' : 'failed', message: error?.message || 'Decision not confirmed' })
      }
      setAttemptResults([...outcomes])
      setResults(old => [...old.filter(r => !outcomes.some(o => o.id === r.id)), ...outcomes])
    }
    setResults(old => [...old.filter(r => !outcomes.some(o => o.id === r.id)), ...outcomes])
    setEntries(old => old.filter(e => !outcomes.some(r => r.id === e.finding.id && r.state !== 'failed')))
    setConfirming(false); setBusy(false); lock.current = false; onBusy?.(false)
    setAnnouncement(latest.current.scopeKey === initialScope ? `Approval finished: ${outcomes.filter(r => r.state === 'recorded').length} approved, ${outcomes.filter(r => r.state === 'failed').length} failed, ${outcomes.filter(r => r.state === 'uncertain').length} uncertain. Writing and verification remain separate.` : '')
    onResult?.(outcomes)
  }
  const activeAttempt = attempt?.scopeKey === scopeKey ? attempt : null
  const summary = confirming && !busy ? { items: entries.length, findings: findingCount, proposals: entries.reduce((n, e) => n + proposalValues(e.finding).length, 0), files: new Set(entries.map(e => e.finding.file)).size } : activeAttempt
  const approved = attemptResults.filter(r => r.state === 'recorded').length
  const failed = attemptResults.filter(r => r.state === 'failed').length
  const uncertain = attemptResults.filter(r => r.state === 'uncertain').length
  return <section className="batch-review" aria-label="Select findings for approval">
    <h3 ref={heading} tabIndex={-1}>{busy ? 'Approving proposals' : confirming ? 'Confirm approval' : activeAttempt ? 'Approval results' : eligible.length ? 'Ready to approve' : preparingProposals ? 'Preparing proposals' : 'No proposals ready'}</h3>
    {(eligible.length > 0 || entries.length > 0 || activeAttempt) && <>
      <p><b>Scope: {scopeLabel}</b></p>
      <p>Approve the suggested fixes. ACP then saves the changes and checks the result.</p>
    </>}
    <p className="batch-sr-only" role="status" aria-live="polite" aria-atomic="true">{announcement}</p>
    {summary && <div className="batch-approval-summary" aria-label="Approval summary">
      <p><b>{summary.findings} findings · {summary.items} review item{summary.items === 1 ? '' : 's'} · {summary.proposals} proposal{summary.proposals === 1 ? '' : 's'} · {summary.files} file{summary.files === 1 ? '' : 's'}</b></p>
      {activeAttempt && (!confirming || busy) && <>
        <dl className="batch-approval-counts" aria-live="off">
          <div className="batch-approved"><dt>Approved</dt><dd><span aria-hidden="true"><LiveCounter key={attempt.number} value={approved} /></span><span className="batch-sr-only">{approved}</span></dd></div>
          <div><dt>Pending</dt><dd>{Math.max(0, attempt.items - attemptResults.length)}</dd></div>
          <div><dt>Failed</dt><dd>{failed}</dd></div>
          <div><dt>Uncertain</dt><dd>{uncertain}</dd></div>
        </dl>
        <p>Approved counts server-confirmed decisions. Writing and verification remain separate.</p>
      </>}
    </div>}
    {(eligible.length > 0 || entries.length > 0) && <div className="batch-review-sticky">
      <span><b>{entries.length > 0 ? `${findingCount} findings selected` : `${readyCount} findings covered by ${readyProposals} ready proposals`}</b> · {new Set((entries.length > 0 ? entries.map(e => e.finding) : eligible).map(f => f.file)).size} files</span>
      {confirming ? <><button type="button" disabled={busy} onClick={() => setConfirming(false)}>Back</button>
        <button type="button" className="primary" disabled={busy || disabled || problems.some(Boolean) || !entries.length || !onDecide} onClick={approve}>{busy ? 'Recording decisions…' : `Confirm approval of ${findingCount} findings`}</button></>
        : <>
          <button type="button" className="primary" disabled={busy || disabled || !eligible.length || !onDecide || problems.some(Boolean)} onClick={confirmAllReady}>Approve all ready ({readyProposals} proposals)</button>
          {entries.length > 0 && <button type="button" disabled={busy || disabled || problems.some(Boolean) || !onDecide} onClick={() => { setConfirming(true); setPage(0) }}>Approve selected</button>}
        </>}
    </div>}
    {confirming && <p>Only these selected proposals will be approved. New proposals and excluded work are not included. Inspection is optional.</p>}
    {!eligible.length && !entries.length && !activeAttempt && <div role="status" className="batch-review-empty">
      <b>{preparingProposals ? 'Remediation is still processing this run.' : 'No proposals are ready for approval in this scope.'}</b>
      <p>{preparingProposals ? 'Readiness will update as processing finishes. You can approve ready proposals together without inspecting each item.' : visible.length
        ? Object.keys(exclusions).every(reason => reason.startsWith('Already') || reason === 'Approval recorded')
          ? 'These changes are already applied or approved. View their results and verification status; another proposal approval is not needed.'
          : Object.keys(exclusions).every(reason => reason === 'Manual work')
            ? 'These issues need your input in the source document. Open individual review for the required edits and instructions.'
            : 'ACP does not have approval-ready fixes for these items. Open the remediation plan to generate new suggestions, or review the document instructions yourself.'
        : 'There are no pending proposals in this scope. Choose another category to see completed changes, verification, or manual work.'}</p>
      {/* Every reason, always, and summing to the scope. This used to name two of the eight
          reasons in prose and hide the rest in a collapsed disclosure, so a screen reading
          "0 ready" explained a fraction of the items and the visible numbers did not
          reconcile: the count above this panel is the whole RUN, these are the current
          SCOPE, and nothing said so. */}
      {!preparingProposals && excludedTotal > 0 && <details className="batch-review-why">
        <summary>Why these items are not ready ({excludedTotal})</summary>
        <p><b>Why nothing can be approved here</b> — all {excludedTotal} review {excludedTotal === 1 ? 'item' : 'items'} in {scopeLabel.toLowerCase()}:</p>
        <ul>{Object.entries(exclusions).sort((a, b) => b[1] - a[1]).map(([reason, count]) =>
          <li key={reason}><b>{count}</b> {reason.toLowerCase()}{EXCLUSION_HELP[reason] ? ` — ${EXCLUSION_HELP[reason]}` : ''}</li>)}</ul>
        {(exclusions['Missing proposal'] || exclusions['Version unavailable — review individually']) > 0
          && <p>Bulk approval requires valid proposals with recorded versions. Generating fresh proposals requires a separately approved run.</p>}
        <p>Counts above this panel cover the whole run, so they will be larger than this scope.</p>
      </details>}
      {readyOutsideScope > 0 && onShowAllReady && <button type="button" className="primary" onClick={onShowAllReady}>Show all ready in this scan ({readyOutsideScope})</button>}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        {!preparingProposals && (exclusions['Version unavailable — review individually'] || exclusions['Missing proposal']) && onOpenPlan && <button type="button" className="primary" onClick={onOpenPlan}>Open remediation plan</button>}
        {!preparingProposals && onReviewExcluded && <button type="button" className="ghost" onClick={onReviewExcluded}>Open individual review</button>}
      </div>
    </div>}
    {(eligible.length > 0 || entries.length > 0) && <details className="batch-review-accounting">
      <summary>Approval details</summary>
      <p>{entries.length > 0
        ? <><b>{findingCount} findings selected</b> ({entries.length} review items) · {entries.reduce((n, e) => n + proposalValues(e.finding).length, 0)} proposals · {new Set(entries.map(e => e.finding.file)).size} files</>
        : <><b>{readyCount} findings ready</b> · {eligible.length} review items · {eligible.reduce((n, f) => n + proposalValues(f).length, 0)} proposals · {new Set(eligible.map(f => f.file)).size} files</>}</p>
    </details>}
    {Object.keys(exclusions).length > 0 && <details className="batch-review-exclusions">
      <summary>{notIncludedCount} pending review item{notIncludedCount === 1 ? '' : 's'} not included{settledCount > 0 ? ` · ${settledCount} already resolved` : ''}</summary>
      <ul>{Object.entries(exclusions).map(([reason, count]) => <li key={reason}>{count} {reason.toLowerCase()}</li>)}</ul>
      <p>These items will not be approved by this action.</p>
    </details>}
    {problems.some(Boolean) && <p role="alert">Selection needs review: {problems.filter(Boolean).join(' · ')}. Clear affected selections and select the current proposals.</p>}
    {entries.length > 0 && <button type="button" disabled={busy} onClick={() => { setEntries([]); setConfirming(false) }}>Clear selection</button>}
    {shown.length > 0 && <details className="batch-review-inspection" key={confirming ? 'confirmation' : 'selection'}>
      <summary>{confirming ? 'Inspect selected proposals (optional)' : 'Inspect proposals or choose a subset (optional)'}</summary>
      <div className="batch-review-controls">
        <label>Group batch by <select value={groupBy} onChange={e => { setGroupBy(e.target.value); setPage(0) }}><option value="file">File</option><option value="change">Change type</option></select></label>
        {!confirming && <button type="button" disabled={busy || disabled} onClick={() => setEntries(eligible.map(f => entries.find(e => e.finding.id === f.id) || snapshotFinding(f)))}>Select all ready</button>}
      </div>
      <ol start={currentPage * PAGE_SIZE + 1}>
        {pageItems.map(f => {
          const entry = entries.find(e => e.finding.id === f.id)
          return <li key={f.id}>
            <label><input type="checkbox" checked={!!entry} disabled={busy || disabled || (!entry && !selectable(f))}
              onChange={() => toggle(f)} /> <strong>{f.file}</strong> · {f.ruleId || f.rule_id || f.title}</label>
            {proposalValues(f).map((value, i) => <details key={i}>
              <summary>Proposal {i + 1}: {String(value || 'Missing proposal').slice(0, 100)}{String(value || '').length > 100 ? '…' : ''}</summary>
              <dl><dt>Current value</dt><dd>{(f.proposals || f._raw?.proposals)?.[i]?.before || f.before || 'Not recorded'}</dd>
                <dt>Proposed value</dt><dd>{value || 'Missing proposal'}</dd></dl>
            </details>)}
          </li>
        })}
      </ol>
      {pages > 1 && <nav aria-label="Batch selection pages"><button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous batch page</button>
        <span>Page {currentPage + 1} of {pages} · {shown.length} review items</span>
        <button type="button" disabled={currentPage + 1 === pages} onClick={() => setPage(currentPage + 1)}>Next batch page</button></nav>}
    </details>}
    {results.some(r => r.state !== 'recorded') && <details className="batch-approval-errors">
      <summary>{results.filter(r => r.state !== 'recorded').length} review items need attention</summary>
      {results.filter(r => r.state !== 'recorded').map(r => <p key={r.id}>Finding {r.id}: {r.message}{r.state === 'failed' && r.message?.includes('stale source revision') ? ' — refresh this page, then select the current proposals and confirm again.' : ''}{r.state === 'uncertain' ? ' — refresh and check the recorded decision before retrying.' : ''}</p>)}
    </details>}
  </section>
}
