import { useEffect, useRef, useState } from 'react'
import { getReleaseReports, retryReleaseReports, downloadReleaseReport } from './api.js'
import { shortDigest } from './releaseClarityModel.js'

const FILE_REPORT_KINDS = new Set(['changes', 'checklist', 'tracked_changes', 'tracked_changes_evidence'])
const NATIVE_LABEL = { tracked_changes: 'Tracked changes (Word companion)', tracked_changes_evidence: 'Change evidence (JSON)' }
export const printableLegacyReport = report => !NATIVE_LABEL[report?.report_kind] && (String(report?.content_type || '').split(';')[0].trim() === 'text/html' || /\.html?$/i.test(report?.name || ''))

export default function ReleaseReports({ scanId, publishedCount = 0, readOnly = false, read = getReleaseReports, retry = retryReleaseReports, download = downloadReleaseReport, files = [], results = {}, releaseId, children, compact = false, refreshKey = '' }) {
  const [state, setState] = useState(null)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [busy, setBusy] = useState(false)
  const currentScan = useRef(scanId)
  currentScan.current = scanId
  useEffect(() => {
    let live = true, timer, attempts = 0
    const controller = new AbortController()
    setState(null); setError(''); setBusy(false)
    if (!scanId) return () => controller.abort()
    const load = async () => {
      try {
        const result = await read(scanId, { signal: controller.signal })
        if (!live) return
        setState(result)
        attempts += 1
        if (['queued', 'publishing'].includes(result?.status) || (result?.status === 'not_started' && publishedCount > 0 && attempts < 12)) timer = setTimeout(load, 10000)
      } catch { if (live) setError('Reports could not be loaded.') }
    }
    load()
    return () => { live = false; clearTimeout(timer); controller.abort() }
  }, [scanId, releaseId, publishedCount, refresh, read, refreshKey])
  const retryDelivery = async () => {
    setBusy(true); setError('')
    try { await retry(scanId); if (currentScan.current === scanId) setRefresh(n => n + 1) } catch { if (currentScan.current === scanId) setError('Report delivery could not be restarted.') } finally { if (currentScan.current === scanId) setBusy(false) }
  }
  const reportsByFile = {}
  const headerReports = []
  const sameRelease = state?.scan_id === scanId && !!releaseId && state?.release_id === releaseId
  for (const [index, report] of (state?.reports || []).entries()) {
    const result = results[report.file]
    const matches = sameRelease && FILE_REPORT_KINDS.has(report.report_kind) && files.some(file => file.file === report.file)
      && result?.status === 'published' && !!report.artifact_digest && report.artifact_digest === result.artifact_digest
    if (matches && children) (reportsByFile[report.file] ||= []).push({ report, index })
    else headerReports.push({ report, index, unassigned: report.report_kind !== 'scan_summary' })
  }
  // Report currency (GET release/reports `currency`): a completed bundle can still describe an
  // earlier version of a copy that was corrected after publication. Never call that current.
  const staleFiles = (state?.out_of_date_files || []).filter(item => item?.file)
  const currency = state?.currency && state.currency !== 'current' && !['queued', 'publishing'].includes(state?.status) ? state.currency : null
  const staleByFile = Object.fromEntries(staleFiles.map(item => [item.file, item]))
  const currencyText = currency === 'out_of_date'
    ? (state.currency_reason === 'release_changed' ? 'Reports are out of date: the release changed after they were generated.'
      : 'Reports are out of date: they describe an earlier published version, not the current corrected copy.')
    : currency ? 'ACP can’t confirm which document version these reports describe, so they are not shown as current.' : ''
  const reportLink = ({ report, index, unassigned }) => <li key={`${report.name}-${index}`} style={{ overflowWrap: 'anywhere' }}>
    {/^(https?):\/\//i.test(report.url || '') ? <a href={report.url} target="_blank" rel="noopener noreferrer">{report.name}</a> : <span>{report.name}</span>}
    {NATIVE_LABEL[report.report_kind] && <small style={{ display: 'block', color: 'var(--muted)' }}>{NATIVE_LABEL[report.report_kind]}</small>}
    {state?.bundle_id && report.download_url && <button type="button" className="linklike" style={{ marginLeft: 10 }} onClick={async () => { try { await download(scanId, state.bundle_id, index, report.name) } catch { if (currentScan.current === scanId) setError('The report could not be downloaded.') } }}>{NATIVE_LABEL[report.report_kind] ? `Download ${report.report_kind === 'tracked_changes' ? 'Word companion' : 'change evidence'}` : 'Download'}</button>}
    {children && unassigned && <small>Document version could not be matched to the current delivery receipt.</small>}
    {currency === 'out_of_date' && staleByFile[report.file] && <small style={{ display: 'block' }}>Describes the earlier version {shortDigest(staleByFile[report.file].reported_artifact_digest)}, not the current corrected copy.</small>}
  </li>
  const reportSummary = <section aria-label="Release reports" className={compact ? 'release-report-actions' : undefined} style={compact ? undefined : { marginTop: 16, borderTop: '1px solid var(--line)', paddingTop: 12 }}>
    {!compact && <strong>Scan summary and per-file checklists</strong>}
    {children && state?.release_id && state.release_id !== releaseId && <p className="muted">Reports describe release {state.release_id}; document actions below describe the current release.</p>}
    {!compact && <p>Verified fixes, applied but unverified changes, remaining issues, and incomplete checks are recorded separately. Remaining work is a follow-up checklist; publication does not certify accessibility.</p>}
    <p role="status">{!state ? (error ? '' : 'Checking reports…') : currency ? `${currencyText}${state.status === 'failed' ? ' The latest report delivery also needs attention.' : ''}` : state.status === 'completed' ? (state.reports?.length && state.reports.every(report => /^https?:\/\//i.test(report.url || '')) ? 'Reports saved alongside the published files.' : 'Reports are ready to download.') : state.status === 'failed' ? 'Files may be published, but report delivery needs attention.' : ['queued', 'publishing'].includes(state.status) ? 'Preparing and saving reports alongside the published files…' : 'Reports are generated after files are published with reporting enabled.'}</p>
    {currency === 'out_of_date' && staleFiles.length > 0 && <ul aria-label="Out-of-date reports">{staleFiles.map(item => <li key={item.file}>{item.file}: reports describe <code>{shortDigest(item.reported_artifact_digest)}</code>; current corrected copy <code>{shortDigest(item.current_artifact_digest)}</code></li>)}</ul>}
    {!!headerReports.length && <ul>{headerReports.map(reportLink)}</ul>}
    {state?.status === 'not_started' && <button type="button" className="linklike" onClick={() => setRefresh(n => n + 1)}>Refresh reports</button>}
    {state?.status === 'completed' && state.reports?.some(printableLegacyReport) && !state.regeneration_blocked && <button type="button" disabled={busy || readOnly} onClick={retryDelivery}>Generate PDF reports</button>}
    {state?.status === 'completed' && state.can_regenerate && !state.reports?.some(printableLegacyReport) && <button type="button" disabled={busy || readOnly} onClick={retryDelivery}>Refresh reports</button>}
    {state?.regeneration_blocked && <p className="muted">{state.regeneration_blocked}</p>}
    {state?.status === 'failed' && <button type="button" className="ghost small" disabled={busy || readOnly} onClick={retryDelivery}>Retry report delivery</button>}
    {error && <p role="alert">{error} <button type="button" className="linklike" onClick={() => setRefresh(n => n + 1)}>Refresh reports</button></p>}
  </section>
  if (typeof children === 'function') return children({ reportSummary, reportsByFile: Object.fromEntries(Object.entries(reportsByFile).map(([file, entries]) => [file, <ul key={file} className="release-file-reports" aria-label={`Reports for ${file}`}>{entries.map(reportLink)}</ul>])) })
  return reportSummary
}
