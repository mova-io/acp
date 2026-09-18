import { useId, useState } from 'react'
import { authEpoch } from './apiIdentity.js'
import useRemediationAttemptStory from './useRemediationAttemptStory.js'
import RemediationCategoryPill from './RemediationCategoryPill.jsx'
import DocumentWideAiOutcomes from './DocumentWideAiOutcomes.jsx'
import './waterfall-drawer-changes.css'

const rows = value => Array.isArray(value) ? value : []
const saved = value => value == null ? 'Not retained' : typeof value === 'string' ? value || 'Empty recorded value' : JSON.stringify(value, null, 2)
const bounded = value => value.length > 1800 ? `${value.slice(0, 1800)}…` : value
function Comparison({ proposal, verified }) {
  const before = proposal?.before
  const after = proposal?.proposed_value
  let start = 0
  let end = 0
  const comparable = typeof before === 'string' && typeof after === 'string' && before.length + after.length <= 24000
  if (comparable) {
    while (start < Math.min(before.length, after.length) && before[start] === after[start]) start++
    while (end < Math.min(before.length, after.length) - start && before[before.length - end - 1] === after[after.length - end - 1]) end++
  }
  const preview = (value, removed) => {
    if (!comparable) return bounded(saved(value))
    if (before === after) return bounded(saved(value))
    const middle = value.slice(start, value.length - end)
    const commonStart = value.slice(Math.max(0, start - 180), start)
    const commonEnd = end ? value.slice(value.length - end, value.length - end + 180) : ''
    const Tag = removed ? 'del' : 'ins'
    return <>{start > 180 && '…'}{commonStart}{middle ? <Tag aria-label={removed ? 'Removed text' : 'Added text'}>{bounded(middle)}</Tag> : !value && 'Empty recorded value'}{commonEnd}{end > 180 && '…'}</>
  }
  return <>
    <p className="waterfall-change-diff-key"><del>Removed</del> / <ins>Added</ins>{!comparable ? ' · Preview only; comparison unavailable for missing, structured, or large values.' : before === after ? ' · Saved values are identical.' : ' · Highlight shows the changed span.'}</p>
    {/* A verified change reads Original / Corrected: "Proposed" would describe a saved value as a
        suggestion, and "Before" invites reading the old text as what the document holds now. */}
    <div className="waterfall-change-comparison"><section><h5>{verified ? 'Original · saved excerpt' : 'Before · saved excerpt'}</h5><pre>{preview(before, true)}</pre></section><section><h5>{verified ? 'Corrected · verified value' : 'Proposed value'}</h5><pre>{preview(after, false)}</pre></section></div>
    <details className="waterfall-change-full-values"><summary>Read full saved values</summary><h5>{verified ? 'Original' : 'Before'}</h5><pre>{saved(before)}</pre><h5>{verified ? 'Corrected' : 'Proposed'}</h5><pre>{saved(after)}</pre></details>
  </>
}

const approvalChecks = [
  ['selected_criteria', 'Within your selected scope'],
  ['complete_proposed_values', 'Complete suggestion'],
  ['supported_writer', 'Supported correction'],
  ['current_source', 'Source is current'],
  ['exact_run_provenance', 'Exact model output recorded'],
  ['ai_review', 'AI review'],
  ['application', 'Application'],
  ['post_change_verification', 'Post-change checks'],
]
function ApprovalEvidence({ evidence }) {
  const results = { passed: 'Passed', accepted: 'Accepted', not_required: 'Not required', pending: 'Pending' }
  return <section className="waterfall-approval-checks" aria-label="Automatic approval checks">
    <p>Checks recorded at approval time:</p>
    <dl>{approvalChecks.map(([key, label]) => {
      const value = evidence[key]
      const satisfied = value === 'passed' || value === 'accepted'
      return <div key={key}><dt>{label}</dt><dd data-result={satisfied ? 'passed' : value === 'pending' ? 'pending' : 'neutral'}><span aria-hidden="true">{satisfied ? '✓' : '○'}</span> {results[value] || 'Not recorded'}</dd></div>
    })}</dl>
    <p>Application and verification can finish later. These checks describe the recorded approval decision.</p>
  </section>
}

export default function WaterfallDrawerChanges({ scanId, batchId, live = false, paused = false, modelFilter, documentWide }) {
  const id = useId()
  const [selection, setSelection] = useState(null)
  const identity = JSON.stringify([authEpoch(), scanId, batchId, modelFilter])
  const current = selection?.identity === identity ? selection : { offset: 0 }
  const offset = current.offset || 0
  const { data, loading, error, refresh } = useRemediationAttemptStory({ scanId, batchId, live, paused, open: true, offset })
  const exact = Array.isArray(modelFilter?.attemptIds) ? modelFilter.attemptIds : null
  const filtered = exact !== null || !!(modelFilter?.provider && modelFilter?.model)
  const matchingIds = new Set(rows(data?.attempts).filter(attempt => exact ? exact.includes(attempt.attempt_id) : attempt.provider === modelFilter?.provider && attempt.model === modelFilter?.model).map(attempt => attempt.attempt_id))
  const proposals = rows(data?.proposals).filter(proposal => !filtered || (exact ? exact.includes(proposal.attempt_id) : matchingIds.has(proposal.attempt_id)))
  const files = [...new Set(proposals.map(proposal => proposal.file).filter(Boolean))].sort()
  const file = files.includes(current.file) ? current.file : files[0] || ''
  const changes = proposals.filter(proposal => proposal.file === file)
  const selected = changes.find(proposal => proposal.snapshot_id === current.snapshot) || changes[0]
  const selectedAttempt = rows(data?.attempts).find(attempt => attempt.attempt_id === selected?.attempt_id)
  const callCharge = Number.isSafeInteger(selectedAttempt?.actual_cost_units) && selectedAttempt.actual_cost_units >= 0
    ? `$${(selectedAttempt.actual_cost_units / 1000000).toFixed(6)} USD` : 'Not recorded in this page'
  const choose = update => setSelection({ ...current, identity, ...update })
  const limit = Number.isSafeInteger(data?.pagination?.limit) && data.pagination.limit > 0 ? data.pagination.limit : 100
  return <section className="waterfall-changes" aria-label="Saved AI changes">
    <div className="waterfall-changes-heading"><div><h4>Explore AI changes</h4><p>Compare saved content and inspect the evidence behind each change.</p></div><button type="button" disabled={loading || !scanId || !batchId} onClick={refresh}>Refresh changes</button></div>
    {error && <p role="status">Changes could not be refreshed.{data ? ' Showing the last saved record page.' : ''}</p>}
    {!scanId || !batchId ? <p>Select a remediation run.</p> : !data ? <p role="status">{loading ? 'Loading saved changes…' : 'Saved changes are unavailable.'}</p> : <>
      <p className="waterfall-changes-scope">{filtered ? 'Changes explicitly linked to the selected model attempts.' : 'All saved proposals in this record page.'} Records are paginated; this is not a document-wide total.{data.coverage !== 'complete' ? ' Some historical records are missing.' : ''}</p>
      <label htmlFor={`${id}-document`}>Document<select id={`${id}-document`} value={file} disabled={!files.length} onChange={event => choose({ file: event.target.value, snapshot: null })}>{!files.length && <option value="">No matching saved changes</option>}{files.map(name => <option key={name} value={name}>{name}</option>)}</select></label>
      {changes.length ? <>
        <nav className="waterfall-change-map" aria-label="Recorded change locations">{changes.map((proposal, index) => <button type="button" key={proposal.snapshot_id || index} aria-pressed={proposal === selected} onClick={() => choose({ snapshot: proposal.snapshot_id })}><span className={proposal.version_verified === true ? 'waterfall-change-marker verified' : 'waterfall-change-marker'} aria-hidden="true" /><span>Change {index + 1}</span><small>{typeof proposal.proposal?.locator === 'string' && proposal.proposal.locator ? proposal.proposal.locator : 'Document location not recorded'}</small></button>)}</nav>
        <article className="waterfall-change-detail" aria-label="Selected change comparison">
          <div className="waterfall-change-detail-heading"><h5>{selected.rule_id ? `Rule ${selected.rule_id}` : 'Rule not recorded'}</h5>{selected.version_verified === true ? <RemediationCategoryPill category="verified" fullLabel /> : <span className="remediation-category-pill waterfall-change-status">{rows(selected.system_approvals).length ? 'Auto-approved · verification unconfirmed' : 'Saved proposal · verification not recorded'}</span>}</div>
          <p className="waterfall-change-verification">{selected.verification_reason || 'Exact-version application and verification are not recorded.'}</p>
          <Comparison proposal={selected.proposal} verified={selected.version_verified === true} />
          {selected.proposal?.rationale && <p><strong>Recorded rationale:</strong> {saved(selected.proposal.rationale)}</p>}
          <details className="waterfall-change-model"><summary>Model and recorded cost</summary>
          <p><strong>Model:</strong> {selected.proposal?.model || selectedAttempt?.model || 'Not recorded'}{selectedAttempt?.provider ? ` · ${selectedAttempt.provider}` : ''}</p>
          <p><strong>Recorded call charge:</strong> {callCharge}. One call may cover several findings.</p>
          <p>Model confidence is an estimate, not proof that a repair worked. This record does not retain a calibrated confidence score. Exact-version verification is shown above.</p></details>
          {rows(selected.system_approvals).length > 0 && <p className="waterfall-change-approval">Automatically approved · {rows(selected.system_approvals).length} saved decision(s). Approval and post-change verification are separate steps.</p>}
          <details><summary>Related evidence</summary><p>Related records alone do not establish that this exact value was written.</p>{['validation_events', 'human_reviews', 'system_approvals'].map(key => <section key={key}><h5>{({ validation_events: 'Validation', human_reviews: 'Human decisions', system_approvals: 'Automatic decisions' })[key]}</h5>{rows(selected[key]).length ? <ul>{rows(selected[key]).map((event, index) => <li key={event.id || index}>{saved(event.outcome || event.action)}{event.detail && <p>{saved(event.detail)}</p>}{event.approval_evidence && <ApprovalEvidence evidence={event.approval_evidence} />}</li>)}</ul> : <p>Not recorded</p>}</section>)}</details>
        </article>
      </> : <p>No matching saved changes on this page. Other record pages may contain changes.</p>}
      <nav className="waterfall-changes-pages" aria-label="Saved change record pages"><button type="button" disabled={!offset || loading} onClick={() => choose({ offset: Math.max(0, offset - limit), snapshot: null })}>Previous records</button><span>Record page {Math.floor(offset / limit) + 1}</span><button type="button" disabled={!data.pagination?.has_more || loading} onClick={() => choose({ offset: offset + limit, snapshot: null })}>Next records</button></nav>
    </>}
    {!filtered && (data?.document_wide || documentWide) && <DocumentWideAiOutcomes snapshot={data?.document_wide || documentWide} />}
    <p className="waterfall-changes-footnote">Page images are unavailable in these saved records. Structural and content changes are shown as recorded values; no visual document comparison is inferred.</p>
  </section>
}
