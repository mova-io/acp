import { useEffect, useRef, useState } from 'react'
import Drawer from './Drawer.jsx'
import Thumbnail from './Thumbnail.jsx'
import AccessibilityStatus from './AccessibilityStatus.jsx'
import EvidenceCard from './EvidenceCard.jsx'
import { getFileRemediationDiffs, downloadRemediated } from './api.js'
import './graph-findings-drawer.css'
import { requiresPdfSourceEditing } from './pdfStructuralProposal.js'
import { remediationRecoveryGuidance } from './remediationRecoveryGuidance.js'
import { viewedBindingKey } from './viewedApprovalBinding.js'

const sc = value => String(value || '').replace(/^(?:SC|WCAG)[_\s]*/i, '').replace(/_/g, '.')
const text = value => value == null ? 'Not recorded' : typeof value === 'string' ? value : JSON.stringify(value)
export default function GraphFindingsDrawer({ file, scanId, onClose, items = [], readOnly = false, onAct, notice = null }) {
  const previewRef = useRef(null)
  const [records, setRecords] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [retry, setRetry] = useState(0)
  const [tab, setTab] = useState(null)
  const [selected, setSelected] = useState(null)
  // The OPEN suggestion is held by id and read from the CURRENT `items` on every render — never a
  // snapshot taken when it was opened — so the card shows the same row version the approval binds to
  // (FileDrawer.drawerAct reads that row by id). The card is keyed by that version, so a poll that
  // brings a new one remounts it with freshly seeded text (viewedApprovalBinding.js).
  const [reviewId, setReviewId] = useState(null)
  const [downloadError, setDownloadError] = useState('')
  useEffect(() => {
    let live = true
    setRecords(null); setTab(null); setSelected(null); setReviewId(null); setDownloadError(''); setLoadError('')
    const load = () => getFileRemediationDiffs(scanId, file.file, { strict: true }).then(rows => { if (live) { setRecords(rows || []); setSelected(null); setLoadError('') } }).catch(() => { if (live) setLoadError('Recorded changes could not be loaded. This does not mean the document has no changes.') })
    load()
    window.addEventListener('acp:file-remediated', load)
    return () => { live = false; window.removeEventListener('acp:file-remediated', load) }
  }, [scanId, file.file, retry])
  const changes = records || []
  const findings = file.issues || []
  // Pending review records are current work; original scan findings stay explicitly historical.
  const attention = items.map(item => ({ ...item, kind: 'review', title: item.rule_name || sc(item.rule_id), detail: item.reason || item.description || 'Review the recorded proposal or remaining work.', page: item.page || (item.evidence?.length === 1 ? item.evidence[0]?.page : null), locator: item.locator || (item.evidence?.length === 1 ? item.evidence[0]?.locator : null) }))
  const activeTab = tab || (attention.length ? 'attention' : records?.length ? 'changes' : 'all')
  const review = reviewId == null ? null : attention.find(row => row.id === reviewId) || null
  const rows = activeTab === 'attention' ? attention : activeTab === 'changes' ? changes.map(change => ({ ...change, kind: 'change', title: sc(change.rule_id) })) : findings.map(finding => ({ ...finding, kind: 'finding', title: finding.rule_name || sc(finding.wcag || finding.rule_id) }))
  const selectLocation = row => {
    setSelected(row)
    previewRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' })
    previewRef.current?.focus({ preventScroll: true })
  }
  const located = selected && (Number.isInteger(selected.page) && selected.page > 0 || selected.locator)
  return <Drawer title={file.file} subtitle="Findings & changes" onClose={onClose}>
    <div className="graph-findings-drawer">
      <div className="graph-findings-summary"><strong>{file.remediated_at ? 'Corrected copy saved' : 'No corrected copy recorded'}</strong><span>{attention.length} loaded review items · {loadError ? 'Change evidence unavailable' : records === null ? 'Loading changes…' : `${changes.length} recorded changes`}</span></div>
      {loadError && <div role="alert" className="graph-finding"><p>{loadError}</p><button type="button" className="ghost small" onClick={() => setRetry(value => value + 1)}>Retry loading changes</button></div>}
      <section className="graph-findings-preview" aria-label="Finding preview" tabIndex={-1} ref={previewRef}>
        <Thumbnail key={`${selected?.page || 1}:${selected?.locator || ''}`} scanId={scanId} file={file.file} page={located ? selected.page || 1 : 1} locator={located ? selected.locator : null} maxHeight={440} />
        <p className="muted">{selected ? located ? 'Preview at the recorded location. Object highlights appear when geometry is available.' : 'Location unavailable for this record. Showing the document cover.' : 'Select a finding or change to inspect its recorded location.'}</p>
        <p className="muted">Document preview is visual evidence; before and after values below are the recorded change evidence.</p>
      </section>
      <div className="graph-findings-tabs" role="tablist" aria-label="Findings and changes">
        {[['attention', 'Needs your attention'], ['changes', 'Changes made'], ['all', 'All findings']].map(([key, label]) => <button type="button" key={key} role="tab" tabIndex={activeTab === key ? 0 : -1} aria-selected={activeTab === key} onKeyDown={event => { const keys = ['attention', 'changes', 'all']; const index = keys.indexOf(key); const next = event.key === 'ArrowRight' ? (index + 1) % 3 : event.key === 'ArrowLeft' ? (index + 2) % 3 : event.key === 'Home' ? 0 : event.key === 'End' ? 2 : null; if (next === null) return; event.preventDefault(); setTab(keys[next]); setSelected(null); setReviewId(null); event.currentTarget.parentElement.querySelectorAll('[role="tab"]')[next].focus() }} aria-controls="graph-findings-panel" onClick={() => { setTab(key); setSelected(null); setReviewId(null) }}>{label}</button>)}
      </div>
      <section id="graph-findings-panel" role="tabpanel" aria-label={activeTab === 'attention' ? 'Needs your attention' : activeTab === 'changes' ? 'Changes made' : 'All findings'}>
        {activeTab === 'all' && <p className="muted">Findings from the original scan. Changes and review items are shown separately.</p>}
        {activeTab === 'changes' && records === null && !loadError && <p role="status">Loading recorded changes…</p>}
        {!rows.length && records !== null && <p className="muted">{activeTab === 'attention' ? 'No pending review items were loaded. Check assessment coverage for incomplete checks.' : activeTab === 'changes' ? 'No change evidence is available.' : 'No original findings recorded.'}</p>}
        {rows.map((row, index) => {
          const rowKey = `${activeTab}:${index}`
          const guidance = row.kind === 'review' ? remediationRecoveryGuidance(row) : null
          const criterion = sc(row.rule_id || row.wcag)
          const locations = Array.isArray(row.evidence) ? row.evidence.filter(e => e && (e.locator || Number.isInteger(e.page) && e.page > 0)) : []
          return <article className={`graph-finding graph-finding--${row.kind}`} key={`${row.id || row.rule_id || row.wcag}-${index}`}>
          <button type="button" className="graph-finding-select" aria-pressed={selected?.rowKey === rowKey} onClick={() => selectLocation({ ...row, rowKey })}><strong>{criterion && <span className="graph-finding-sc">SC {criterion} · </span>}{row.title && row.title !== criterion ? row.title : row.kind === 'change' ? 'Recorded correction' : 'Finding'}</strong><span>{row.page ? `Page ${row.page}${row.locator ? ` · ${row.locator}` : ''}` : row.locator || (locations.length > 1 ? `${locations.length} recorded locations` : 'Location unavailable')}</span></button>
          <span className={`graph-finding-pill ${row.kind === 'review' && requiresPdfSourceEditing(row) ? 'is-edit-needed' : ''} ${row.kind === 'change' && row.verified === true ? 'is-verified' : ''}`}>{row.kind === 'change' ? row.verified === true ? 'Verified fix' : 'Applied · verification not recorded' : row.kind === 'review' ? requiresPdfSourceEditing(row) ? 'Edit needed' : 'Review needed' : 'Original finding'}</span>
          {(guidance?.reason || row.detail) && <p><strong>Reason: </strong>{guidance?.reason || row.detail}</p>}
          {guidance?.next && <p className="graph-finding-next"><strong>Next step: </strong>{guidance.next}</p>}
          {locations.length > 1 && <div className="graph-finding-locations" aria-label="Recorded finding locations">{locations.map((location, locationIndex) => <button type="button" className="ghost small" key={locationIndex} onClick={() => selectLocation({ ...row, ...location, rowKey })}>Show {location.page ? `page ${location.page}` : location.locator}</button>)}</div>}
          {row.kind === 'change' && <><dl><dt>Before</dt><dd>{text(row.before)}</dd><dt>After</dt><dd>{text(row.after)}</dd></dl>{row.note && <p className="muted">{row.note}</p>}</>}
          {row.kind === 'review' && <button type="button" className="ghost small" onClick={() => setReviewId(row.id)}>{requiresPdfSourceEditing(row) ? 'View editing guidance' : 'Review recorded suggestion'}</button>}
        </article>})}
        {review && <><button type="button" className="ghost small" onClick={() => setReviewId(null)}>Close suggestion</button>{notice?.id === review.id && <div role="alert" className="graph-finding graph-viewed-version"><strong>Not approved.</strong> {notice.message}</div>}{requiresPdfSourceEditing(review) ? <div className="graph-finding"><strong>Edit the source document or use a PDF accessibility editor.</strong><p>This outline is guidance; ACP cannot create the missing PDF structure tree automatically.</p><p>{text(review.suggested_value || review.proposed_value)}</p></div> : readOnly ? <div className="graph-finding"><strong>Historical record · viewing only</strong><p>{text(review.suggested_value || review.proposed_value)}</p></div> : <EvidenceCard key={viewedBindingKey(review)} item={review} onAct={onAct} onResolved={() => setReviewId(null)} />}</>}
      </section>
      <details className="graph-findings-coverage"><summary>Assessment coverage & incomplete checks</summary><AccessibilityStatus scanId={scanId} file={file.file} /></details>
      <footer className="graph-findings-footer"><button type="button" className="ghost small" disabled={!file.remediated_at} onClick={async () => { try { await downloadRemediated(scanId, file.file) } catch { setDownloadError('The corrected copy could not be downloaded. Try again.') } }}>Download corrected copy</button><button type="button" className="ghost small" disabled={!records?.length} onClick={() => { const url = URL.createObjectURL(new Blob([JSON.stringify({ scan_id: scanId, file: file.file, changes: records }, null, 2)], { type: 'application/json' })); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${file.file}-change-evidence.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000) }}>Download change evidence</button>{downloadError && <p role="alert">{downloadError}</p>}</footer>
    </div>
  </Drawer>
}
