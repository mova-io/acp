// "Changes to confirm" — one card per saved change, with a recorded, versioned human decision:
// Accept / Edit / Reject / Unable to verify.
//
// Every status on a card comes from a record: the technical status from the server's own
// verification field, the human status from api/routes/change_review.py. No record → "No decision
// recorded", never a default "accepted". A decision whose document or change moved on reads
// "Needs recheck"; one whose freshness could not be established reads "Freshness unknown —
// recheck", which is NOT acceptance.
//
// Both kinds of saved change appear here. A VERIFIED change cleared the post-fix re-scan. An
// applied-but-UNVERIFIED one was written into the corrected copy and never re-checked — those are
// precisely the ones a human has to look at, so hiding them (which the remediation-diff route did,
// by never returning them) left the review panel showing only the work that needed it least.
import { useEffect, useState, useCallback } from 'react'
import { criterionName, locationOf, locationLabel, fmtOfFile, scOfValue } from './reportEvidence.js'
import {
  changeId, hasStableId, changeDigest, loadChangeReviews, saveChangeReview, freshnessOf,
  VERDICT_LABEL, VERDICT_DETAIL, SIM_NOTICE, NOTE_MAX, EDITED_VALUE_MAX,
} from './changeReview.js'

const fmtWhen = (iso) => {
  const t = Date.parse(iso || '')
  return Number.isFinite(t) ? new Date(t).toLocaleString() : null
}

const shortSha = (s) => (s ? String(s).slice(0, 12) : null)

// A preview entry carries its own provenance (fileReportData.collectPreviews). A bare data-URL is
// the legacy shape and carries none, so it is shown as an explicitly UNVERIFIED version rather
// than being captioned as though somebody knew which document it is a picture of.
export function previewFor(previews, id, loc) {
  if (!previews || typeof previews !== 'object') return null
  const page = loc && loc.page != null ? loc.page : null
  const hit = previews[id] ?? (page != null ? previews[page] : undefined)
  if (!hit) return null
  if (typeof hit === 'string') {
    return {
      src: hit, provenance: 'unverified', verified: false, page,
      caption: 'Document preview \u2014 version not verified',
      alt: page != null ? `Page ${page} of this document; which version is shown is not verified` : 'Document preview; which version is shown is not verified',
      note: 'This preview could not be tied to a recorded document version, so it is not evidence of what changed.',
    }
  }
  return typeof hit.src === 'string' ? hit : null
}

function ChangeCard({ scanId, fileName, diff, index, review, artifact, sim, readOnly, preview, onSaved }) {
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
  // A decision binds ONLY to the SAVED COPY's sha-256 (ratified 2026-09-17). Not to
  // currentArtifact.sha256, which falls back to the source checksum when no corrected copy is
  // recorded: binding a decision about a saved change to the ORIGINAL's checksum would record
  // "this edit is correct" against bytes that do not contain the edit. Where correctedSha256 is
  // null the route answers 409 and no decision can be recorded here at all.
  const bindTo = artifact?.correctedSha256 || null
  const unbindable = !sim && !bindTo
  const canDecide = stable && !unbindable && !readOnly && !!(scanId || sim)
  const fresh = review ? freshnessOf(review, artifact, sim ? undefined : digest) : null
  // 'verified' | 'not_verified' | 'unknown' — the server's word, not an inference from `verified`.
  const verification = diff.verification
    || (diff.verified === true ? 'verified' : diff.verified === false ? 'not_verified' : 'unknown')

  const decide = async (verdict) => {
    if (!canDecide || busy) return
    setBusy(true); setMsg(null)
    const res = await saveChangeReview(scanId, fileName, diff,
      { verdict, note, editedValue: verdict === 'edited' ? edited : null, expectedSha256: bindTo || undefined })
    setBusy(false)
    if (res.ok) {
      setEditing(false)
      setMsg({ ok: true, text: res.sim ? `${VERDICT_LABEL[verdict]} — shown for this session only (demo mode, not saved).` : `${VERDICT_LABEL[verdict]} — decision recorded.` })
      onSaved(id, res.review, res.artifact)
    } else {
      setMsg({ ok: false, text: `Not saved: ${res.error}` })
    }
  }

  const technical = diff.verificationDetail
    || (verification === 'verified' ? 'Verified — the re-scan after this edit no longer reported the finding.'
      : verification === 'not_verified' ? 'AI applied this change to the saved copy; no re-scan has confirmed it.'
        : 'Technical check not recorded.')

  let human
  if (!review) human = 'No decision recorded.'
  else {
    const who = review.reviewer || 'Reviewer not recorded'
    const when = fmtWhen(review.at) || 'time not recorded'
    human = `${VERDICT_LABEL[review.verdict] || 'Unknown decision'} by ${who}, ${when}${review.sim ? ' (demo — not saved)' : ''}.`
  }
  const verdictDetail = review ? VERDICT_DETAIL[review.verdict] : null

  return (
    <article className="chgcard" aria-labelledby={`${domId}-title`} data-change-id={id}
             style={{ border: '1px solid var(--line, #ddd)', borderRadius: 8, padding: '10px 12px', margin: '8px 0' }}>
      <h5 id={`${domId}-title`} style={{ margin: '0 0 4px', fontSize: 13 }}>
        {sc || 'Change'}{name ? ` · ${name}` : ''}
        {/* An applied-but-unverified change is the one that needs a person. Say so on the card
            itself, not only in the technical-check line further down. */}
        {verification === 'not_verified' && (
          <span className="badge chgunverified"
                style={{ marginLeft: 6, fontSize: 11, background: 'var(--warning-bg, #FEF0C7)', color: 'var(--warning-fg, #7A2E0E)', borderRadius: 4, padding: '1px 5px' }}>
            AI applied · not verified
          </span>
        )}
        {verification === 'verified' && (
          <span className="badge chgverified" style={{ marginLeft: 6, fontSize: 11 }}>Verified by re-scan</span>
        )}
      </h5>
      <dl className="chgmeta" style={{ margin: 0, fontSize: 12, display: 'grid', gridTemplateColumns: 'max-content 1fr', gap: '2px 10px' }}>
        <dt className="muted">Location</dt><dd style={{ margin: 0 }}>{locationLabel(loc)}</dd>
        <dt className="muted">Reason</dt><dd style={{ margin: 0 }}>{diff.note || 'Reason not recorded'}</dd>
        <dt className="muted">Technical check</dt><dd style={{ margin: 0 }}>{technical}</dd>
        <dt className="muted">Human decision</dt>
        <dd style={{ margin: 0 }}>
          {human}
          {verdictDetail && <span className="chgverdictdetail muted"> {verdictDetail}</span>}
          {review?.note && <span className="muted"> Note: {review.note}</span>}
          {review?.verdict === 'edited' && review.edited_value && (
            <span className="muted"> Requested wording (not applied): {review.edited_value}</span>
          )}
          {fresh && fresh.stale !== false && (
            <>
              <span className={`badge ${fresh.stale === true ? 'chgstale' : 'chgfreshunknown'}`}
                    style={{ marginLeft: 6, background: 'var(--warning-bg, #FEF0C7)', color: 'var(--warning-fg, #7A2E0E)', borderRadius: 4, padding: '1px 5px' }}>
                {fresh.label}
              </span>
              <span className="muted"> {fresh.detail}</span>
            </>
          )}
        </dd>
      </dl>
      <div className="diffbox before" style={{ marginTop: 6, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        <span className="difftag">before</span>{diff.before ? diff.before : <i className="muted">(empty)</i>}
      </div>
      <div className="diffbox after" style={{ marginTop: 4, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
        <span className="difftag">after</span>{diff.after ? diff.after : <i className="muted">(empty)</i>}
      </div>
      {/* The store clipped these values when it recorded them; the rest of the text is not held
          anywhere, so "see the full report" would send the reader somewhere it also is not. */}
      {diff.valueClipped && (
        <p className="muted chgclipped" style={{ fontSize: 11, margin: '4px 0 0' }}>
          This before/after text was clipped by the store when it was recorded. The full text was not kept and is not available in any report.
        </p>
      )}
      {/* A preview is only ever labelled with the version it provably is. The generic page route
          may serve either the original or the corrected copy, so an image from it says exactly
          that — it is never captioned "after the edit". */}
      {preview
        ? (
          <figure className={`chgpreview chgpreview-${preview.provenance}`} style={{ margin: '6px 0 0' }}>
            <img src={preview.src} alt={preview.alt || `Preview of ${locationLabel(loc)}`} style={{ maxWidth: '100%' }} />
            <figcaption className="muted" style={{ fontSize: 11 }}>
              {preview.caption}{preview.note ? ` ${preview.note}` : ''}
            </figcaption>
          </figure>
        )
        : <p className="muted" style={{ fontSize: 12, margin: '6px 0 0' }}>Visual preview not available for this change.</p>}

      {!stable && <p className="muted" style={{ fontSize: 12 }}>Cannot record a decision: this change has no stable id.</p>}
      {stable && unbindable && (
        <p className="chgunbindable" style={{ fontSize: 12 }}>
          Cannot record a decision: the saved copy&rsquo;s identity is not recorded. ACP has no sha-256 for the corrected
          document, so a decision could not be tied to the version that was actually written. This has to be resolved
          before these changes can be signed off.
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
      {/* A list that is missing the changes awaiting review is not a short list, it is a
          MISLEADING one: what is left is the work that needs nothing from the reader. Said as an
          alert, above the cards, in its own words rather than prefixed "could not be loaded". */}
      {diffsError && (
        <p role="alert" className="chgincomplete" style={{ color: 'var(--error-fg)' }}>
          {/^The changes awaiting review/.test(diffsError) ? diffsError : `Saved changes could not be loaded: ${diffsError}`}
        </p>
      )}
      {state.error && <p role="alert" style={{ color: 'var(--error-fg)' }}>Recorded decisions could not be loaded: {state.error}</p>}
      {!state.sim && art && list.length > 0 && (
        <p className="muted" style={{ fontSize: 12 }}>
          {art.correctedSha256
            ? <>Decisions are recorded against the saved corrected copy <code>{shortSha(art.correctedSha256)}</code>.</>
            : <>The saved copy&rsquo;s identity is not recorded{art.currentSha256 ? <> (the only checksum on file is the original&rsquo;s, <code>{shortSha(art.currentSha256)}</code>)</> : ''}, so no decision can be recorded against these changes, and any decision already on file cannot be shown to be current.</>}
        </p>
      )}
      {list.map((d, i) => {
        const id = changeId(file, d, i)
        const loc = locationOf(d, { fmt: fmtOfFile(file) })
        return (
          <ChangeCard key={id} scanId={scanId} fileName={file} diff={d} index={i}
                      review={state.loading ? null : state.reviews[id] || null}
                      artifact={art} sim={state.sim} readOnly={readOnly}
                      preview={previewFor(previews, id, loc)} onSaved={onSaved} />
        )
      })}
    </section>
  )
}
