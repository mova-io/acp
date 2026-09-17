// "Changes to confirm" — one card per saved change (remediation_diff row) with a recorded,
// versioned human decision: Accept / Edit / Reject / Unable to verify.
//
// Every status on a card comes from a record: technical status from the remediation_diff row
// (those rows are only written for fixes that cleared the re-scan), the human status from
// api/routes/change_review.py. No record → "No decision recorded", never a default "accepted".
// A decision whose document or change moved on is shown as "Needs recheck".
import { useEffect, useState, useCallback } from 'react'
import { criterionName, locationOf, locationLabel, fmtOfFile, scOfValue } from './reportEvidence.js'
import {
  changeId, hasStableId, isStale, changeDigest, loadChangeReviews, saveChangeReview,
  VERDICT_LABEL, STALE_TEXT, SIM_NOTICE, NOTE_MAX, EDITED_VALUE_MAX,
} from './changeReview.js'

const fmtWhen = (iso) => {
  const t = Date.parse(iso || '')
  return Number.isFinite(t) ? new Date(t).toLocaleString() : null
}

const shortSha = (s) => (s ? String(s).slice(0, 12) : null)

function ChangeCard({ scanId, fileName, diff, index, review, artifact, sim, readOnly, previewSrc, onSaved }) {
  const id = changeId(fileName, diff, index)
  const domId = `chg-${index}`
  const sc = scOfValue(diff.rule_id) || diff.rule_id || null
  const name = criterionName(sc)
  const loc = locationOf(diff, { fmt: fmtOfFile(fileName) })
  const [note, setNote] = useState(review?.note || '')
  const [editing, setEditing] = useState(false)
  const [edited, setEdited] = useState(review?.edited_value || diff.after || '')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState(null)   // { ok, text }
  const [digest, setDigest] = useState(undefined)
  useEffect(() => {
    let on = true
    changeDigest(diff).then((d) => { if (on) setDigest(d) }).catch(() => { if (on) setDigest(null) })
    return () => { on = false }
  }, [diff.rule_id, diff.before, diff.after])

  // A decision that arrives after mount (async load) or is replaced by a save seeds the fields.
  useEffect(() => {
    if (editing) return
    setNote(review?.note || '')
    setEdited(review?.edited_value || diff.after || '')
  }, [review?.at])

  const stable = hasStableId(diff)
  const unbindable = !sim && !artifact?.currentSha256
  const canDecide = stable && !unbindable && !readOnly && !!(scanId || sim)
  const stale = review ? isStale(review, artifact, sim ? undefined : digest) : null

  const decide = async (verdict) => {
    if (!canDecide || busy) return
    setBusy(true); setMsg(null)
    const res = await saveChangeReview(scanId, fileName, diff,
      { verdict, note, editedValue: verdict === 'edited' ? edited : null, expectedSha256: artifact?.currentSha256 || undefined })
    setBusy(false)
    if (res.ok) {
      setEditing(false)
      setMsg({ ok: true, text: res.sim ? `${VERDICT_LABEL[verdict]} — shown for this session only (demo mode, not saved).` : `${VERDICT_LABEL[verdict]} — decision recorded.` })
      onSaved(id, res.review, res.artifact)
    } else {
      setMsg({ ok: false, text: `Not saved: ${res.error}` })
    }
  }

  const technical = diff.verified === true
    ? 'Verified — the re-scan after this edit no longer reported the finding.'
    : diff.verified === false ? 'Edit saved — re-validation has not been recorded.'
      : 'Technical check not recorded.'

  let human
  if (!review) human = 'No decision recorded.'
  else {
    const who = review.reviewer || 'Reviewer not recorded'
    const when = fmtWhen(review.at) || 'time not recorded'
    human = `${VERDICT_LABEL[review.verdict] || 'Unknown decision'} by ${who}, ${when}${review.sim ? ' (demo — not saved)' : ''}.`
  }

  return (
    <article className="chgcard" aria-labelledby={`${domId}-title`} data-change-id={id}
             style={{ border: '1px solid var(--line, #ddd)', borderRadius: 8, padding: '10px 12px', margin: '8px 0' }}>
      <h5 id={`${domId}-title`} style={{ margin: '0 0 4px', fontSize: 13 }}>
        {sc || 'Change'}{name ? ` · ${name}` : ''}
      </h5>
      <dl className="chgmeta" style={{ margin: 0, fontSize: 12, display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '2px 10px' }}>
        <dt className="muted">Location</dt><dd style={{ margin: 0 }}>{locationLabel(loc)}</dd>
        <dt className="muted">Reason</dt><dd style={{ margin: 0 }}>{diff.note || 'Reason not recorded'}</dd>
        <dt className="muted">Technical check</dt><dd style={{ margin: 0 }}>{technical}</dd>
        <dt className="muted">Human decision</dt>
        <dd style={{ margin: 0 }}>
          {human}
          {review?.note && <span className="muted"> Note: {review.note}</span>}
          {review?.verdict === 'edited' && review.edited_value && <span className="muted"> Edited value: {review.edited_value}</span>}
          {stale === true && (
            <span className="badge chgstale" style={{ marginLeft: 6, background: 'var(--warning-bg, #FEF0C7)', color: 'var(--warning-fg, #7A2E0E)' }}>
              {STALE_TEXT}
            </span>
          )}
          {review && stale === null && !sim && (
            <span className="muted"> Whether the document changed since this decision cannot be determined.</span>
          )}
        </dd>
      </dl>
      <div className="diffbox before" style={{ marginTop: 6, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        <span className="difftag">before</span>{diff.before ? diff.before : <i className="muted">(empty)</i>}
      </div>
      <div className="diffbox after" style={{ marginTop: 4, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        <span className="difftag">after</span>{diff.after ? diff.after : <i className="muted">(empty)</i>}
      </div>
      {previewSrc
        ? <img src={previewSrc} alt={`Preview of ${locationLabel(loc)}`} style={{ maxWidth: '100%', marginTop: 6 }} />
        : <p className="muted" style={{ fontSize: 12, margin: '6px 0 0' }}>Preview not available</p>}

      {!stable && <p className="muted" style={{ fontSize: 12 }}>Cannot record a decision: this change has no stable id.</p>}
      {stable && unbindable && (
        <p className="muted" style={{ fontSize: 12 }}>
          Cannot record a decision: ACP has no recorded identity (checksum) for this document, so a decision could not be tied to the version you are looking at.
        </p>
      )}
      {canDecide && (
        <div className="chgdecide" style={{ marginTop: 8 }}>
          <label htmlFor={`${domId}-note`} style={{ display: 'block', fontSize: 12 }}>Note (optional)</label>
          <textarea id={`${domId}-note`} value={note} maxLength={NOTE_MAX} rows={2} style={{ width: '100%' }}
                    onChange={(e) => setNote(e.target.value)} />
          {editing && (
            <>
              <label htmlFor={`${domId}-edit`} style={{ display: 'block', fontSize: 12, marginTop: 4 }}>Corrected value</label>
              <textarea id={`${domId}-edit`} value={edited} maxLength={EDITED_VALUE_MAX} rows={3} style={{ width: '100%' }}
                        onChange={(e) => setEdited(e.target.value)} />
            </>
          )}
          <div role="group" aria-label={`Decision for ${sc || 'this change'}`} style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
            <button type="button" className="ghost small" disabled={busy} onClick={() => decide('accepted')}>Accept</button>
            {editing
              ? <button type="button" className="ghost small" disabled={busy} onClick={() => decide('edited')}>Save edit</button>
              : <button type="button" className="ghost small" disabled={busy} onClick={() => setEditing(true)}>Edit</button>}
            {editing && <button type="button" className="ghost small" disabled={busy} onClick={() => setEditing(false)}>Cancel edit</button>}
            <button type="button" className="ghost small" disabled={busy} onClick={() => decide('rejected')}>Reject</button>
            <button type="button" className="ghost small" disabled={busy} onClick={() => decide('unable')}>Unable to verify</button>
          </div>
        </div>
      )}
      <p role="status" aria-live="polite" style={{ fontSize: 12, margin: '4px 0 0', color: msg && !msg.ok ? 'var(--error-fg)' : undefined }}>
        {busy ? 'Saving…' : msg?.text || ''}
      </p>
    </article>
  )
}

/**
 * Props:
 *   scanId, file (name), diffs (remediation_diff rows for this file, full list; null = not loaded),
 *   diffsError (string|null), readOnly, previews ({[changeId|page]: dataUrl}),
 *   onReviews(reviews, artifact) — reported up so report exports use the same records.
 */
export default function ChangeReviewPanel({ scanId, file, diffs, diffsError = null, readOnly = false, previews = null, onReviews = null }) {
  const [state, setState] = useState({ loading: true, sim: false, artifact: null, reviews: {}, error: null })
  const load = useCallback(() => {
    let on = true
    setState((s) => ({ ...s, loading: true }))
    loadChangeReviews(scanId, file).then((r) => {
      if (!on) return
      setState({ loading: false, sim: r.sim, artifact: r.artifact, reviews: r.reviews, error: r.error })
    })
    return () => { on = false }
  }, [scanId, file])
  useEffect(() => load(), [load])
  useEffect(() => {
    if (!state.loading && onReviews) onReviews(state.reviews, state.artifact)
  }, [state.loading, state.reviews, state.artifact])

  const onSaved = (id, review, artifact) => {
    setState((s) => ({
      ...s,
      artifact: artifact ? { ...s.artifact, ...artifact } : s.artifact,
      reviews: review ? { ...s.reviews, [id]: review } : s.reviews,
    }))
  }

  const list = Array.isArray(diffs) ? diffs : []
  if (diffs != null && list.length === 0 && !diffsError) return null

  const art = state.artifact
  return (
    <section className="chgreview" aria-labelledby="chgreview-h">
      <h4 className="drawerh" id="chgreview-h">
        Changes to confirm {list.length > 0 && <span className="muted">({list.length})</span>}
      </h4>
      {state.sim && <p className="muted" role="note" style={{ fontSize: 12 }}>{SIM_NOTICE}</p>}
      {diffs == null && !diffsError && <p className="muted">Loading saved changes…</p>}
      {diffsError && <p role="alert" style={{ color: 'var(--error-fg)' }}>Saved changes could not be loaded: {diffsError}</p>}
      {state.error && <p role="alert" style={{ color: 'var(--error-fg)' }}>Recorded decisions could not be loaded: {state.error}</p>}
      {!state.sim && art && list.length > 0 && (
        <p className="muted" style={{ fontSize: 12 }}>
          {art.currentSha256
            ? <>Decisions are recorded against document version <code>{shortSha(art.currentSha256)}</code>{art.identityKind === 'corrected_sha256' ? ' (corrected copy)' : ' (source checksum)'}.</>
            : 'No document identity is recorded for this file, so decisions cannot be recorded against a version.'}
        </p>
      )}
      {list.map((d, i) => {
        const id = changeId(file, d, i)
        const loc = locationOf(d, { fmt: fmtOfFile(file) })
        const src = previews ? (previews[id] || (loc?.page != null ? previews[loc.page] : null)) : null
        return (
          <ChangeCard key={id} scanId={scanId} fileName={file} diff={d} index={i}
                      review={state.loading ? null : state.reviews[id] || null}
                      artifact={art} sim={state.sim} readOnly={readOnly}
                      previewSrc={src} onSaved={onSaved} />
        )
      })}
    </section>
  )
}
