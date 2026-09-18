import { useId, useState } from 'react'
import { republishRelease } from './api.js'
import { shortDigest, digestHex } from './releaseClarityModel.js'

const when = (value) => {
  const time = Date.parse(value || '')
  return Number.isFinite(time) ? new Date(time).toLocaleString() : null
}
const confirmationKey = (item) => `${item.file}\n${item.current_artifact_digest || ''}`

// A correction saved AFTER publication makes the delivered copy and its reports out of date. This
// notice states that with exact artifact identity (published vs current digest) and offers one
// explicit action bound to those exact versions. Remaining issues are never authorized silently:
// each version that needs it gets its own unchecked-by-default confirmation naming that version.
export default function ReleaseCorrectionNotice({ scanId, publication, busy = false, readOnly = false,
  republish = null, onRepublished, onRefresh }) {
  const id = useId()
  const [confirmed, setConfirmed] = useState({})
  const [sending, setSending] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState(null)
  const [settledFailed, setSettledFailed] = useState(false)
  const outOfDate = (publication?.out_of_date || []).filter((item) => item?.file)
  const unknown = (publication?.identity_unknown || []).filter(Boolean)
  const publishing = busy || sending || publication?.state === 'publishing'
  if (!outOfDate.length && !unknown.length && !publishing) return null
  // An earlier failure is history while a new attempt runs; show it only once publication settles.
  const failures = publishing ? [] : outOfDate.filter((item) => item.last_attempt_failure && typeof item.last_attempt_failure === 'object')
  const needsConfirmation = outOfDate.filter((item) => item.requires_remaining_issue_confirmation === true)
  const identified = outOfDate.every((item) => /^[0-9a-f]{64}$/i.test(digestHex(item.current_artifact_digest)))
  const allConfirmed = needsConfirmation.every((item) => confirmed[confirmationKey(item)] === true)
  const canRepublish = publication?.can_republish === true
  const disabled = readOnly || publishing || !outOfDate.length || !canRepublish || !identified || !allConfirmed
  const submit = async () => {
    if (disabled) return
    // Exactly the files whose box is ticked; remaining issues are authorized only for those.
    const remainingIssueFiles = needsConfirmation.filter((item) => confirmed[confirmationKey(item)] === true).map((item) => item.file)
    const body = {
      expected_artifacts: Object.fromEntries(outOfDate.map((item) => [item.file, digestHex(item.current_artifact_digest)])),
      allow_remaining_issues: remainingIssueFiles.length > 0,
      remaining_issue_files: remainingIssueFiles,
    }
    setSending(true); setError(null); setSettledFailed(false)
    setMessage('Publishing the updated copy. Reports refresh after it finishes.')
    try {
      const result = await (republish || republishRelease)(scanId, body)
      const alreadyCurrent = result?.already_current === true || result?.result === 'already_current'
      setMessage(alreadyCurrent ? 'The published copy is already the current corrected copy.'
        : result?.already_publishing === true ? 'This updated copy is already being published. Release status and reports will refresh when it finishes.'
        : 'Updated copy publishing started. Release status and reports will refresh when it finishes.')
      const outcome = await onRepublished?.(result, outOfDate)
      // A settled-but-still-out-of-date outcome is a failure: its reason is rendered from
      // last_attempt_failure below (one alert), so this status line never claims progress.
      if (outcome === 'current') setMessage('Updated copy published. Reports refresh with it.')
      else if (outcome === 'failed' || outcome === 'settled' || outcome === 'unknown') { setMessage(''); setSettledFailed(outcome === 'failed') }
      else if (outcome === 'pending') setMessage('The updated copy is still publishing in the background. You may leave this page and return later.')
    } catch (failure) {
      setMessage('')
      setError({ text: failure?.message || 'The updated copy could not be published.', refresh: failure?.code === 'artifact_changed' || failure?.refreshRequired === true })
      if (failure?.code === 'artifact_changed') setConfirmed({})
      else if (failure?.code === 'remaining_issues_confirmation_required' && Array.isArray(failure.files))
        setConfirmed((old) => Object.fromEntries(Object.entries(old).filter(([key]) => !failure.files.includes(key.split('\n')[0]))))
    } finally { setSending(false) }
  }
  const heading = outOfDate.length ? 'Published copy is out of date'
    : unknown.length ? 'Published version can’t be confirmed' : 'Publishing updated copy'
  return <section className="panel release-correction-notice" aria-labelledby={`${id}-title`}
    style={{ borderLeft: '4px solid var(--warn-fg)', marginTop: 12, padding: 14 }}>
    <h3 id={`${id}-title`} style={{ margin: 0 }}><span aria-hidden="true">! </span>{heading}</h3>
    {outOfDate.length > 0 && <>
      <p>A correction was saved after publication. The delivered {outOfDate.length === 1 ? 'copy and its reports describe' : 'copies and their reports describe'} an earlier version, not the current corrected copy.</p>
      <ul aria-label="Out-of-date published copies">{outOfDate.map((item) => <li key={item.file}>
        <b>{item.file}</b>
        <span style={{ display: 'block' }}>Published version: <code>{shortDigest(item.published_artifact_digest)}</code>{when(item.published_at) ? ` · published ${when(item.published_at)}` : ''}</span>
        <span style={{ display: 'block' }}>Current corrected copy: <code>{shortDigest(item.current_artifact_digest)}</code>{when(item.current_remediated_at) ? ` · saved ${when(item.current_remediated_at)}` : ''}</span>
      </li>)}</ul>
    </>}
    {unknown.length > 0 && <p>ACP can’t confirm which version was published for {unknown.join(', ')}, or whether {unknown.length === 1 ? 'it is' : 'they are'} the current corrected copy. A version fingerprint is missing, so {unknown.length === 1 ? 'it is' : 'they are'} not shown as current and can’t be republished from here. Check the delivered copy and reconcile that delivery before publishing again.</p>}
    {failures.length > 0 && <div role="alert">{failures.map((item) => <p key={item.file}>
      <b>{item.file}:</b> The last attempt to publish version <code>{shortDigest(item.last_attempt_failure.attempted_artifact_digest || item.current_artifact_digest)}</code>{when(item.last_attempt_failure.at) ? ` (${when(item.last_attempt_failure.at)})` : ''} did not complete: {item.last_attempt_failure.explanation || item.last_attempt_failure.failure_category || 'no reason was recorded'}. The earlier published copy is unchanged.{canRepublish ? ' You can try again.' : ''}
    </p>)}</div>}
    {!publishing && settledFailed && !failures.length && outOfDate.length > 0 && <p role="alert">The updated copy was not published. The earlier published copy is unchanged; no reason was recorded.</p>}
    {needsConfirmation.map((item) => <label key={confirmationKey(item)} style={{ display: 'block', marginTop: 8 }}>
      <input type="checkbox" checked={confirmed[confirmationKey(item)] === true} disabled={readOnly || publishing}
        onChange={(event) => setConfirmed((old) => ({ ...old, [confirmationKey(item)]: event.target.checked }))} />
      {' '}Publish {item.file} version <code>{shortDigest(item.current_artifact_digest)}</code> with its remaining issues recorded. Publishing does not certify accessibility.
    </label>)}
    {outOfDate.length > 0 && !canRepublish && <p>{publication?.republish_blocked_reason || 'The updated copy can’t be published right now.'}</p>}
    {outOfDate.length > 0 && !identified && <p>ACP can’t identify the current corrected copy exactly, so it will not publish it. Refresh release status.</p>}
    {outOfDate.length > 0 && <button type="button" disabled={disabled} aria-busy={publishing} onClick={submit}>
      {publishing ? 'Publishing updated copy…' : 'Publish updated copy and refresh reports'}
    </button>}
    <p role="status" aria-live="polite">{publishing && !message ? 'Publishing the updated copy. Reports refresh after it finishes.' : message}</p>
    {error && <p role="alert">{error.text}{error.refresh && onRefresh && <> <button type="button" className="linklike" onClick={() => { setError(null); onRefresh() }}>Refresh release status</button></>}</p>}
  </section>
}
