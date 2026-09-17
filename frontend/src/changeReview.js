// Versioned reviewer decisions on individual saved changes (remediation_diff rows).
//
// A decision is bound to the artifact the reviewer looked at (`artifact_sha256`, the file's
// corrected_sha256 or else its source checksum) and to the change itself (`change_digest`). When
// either has moved on, the decision is STALE — shown as "Needs recheck", never as accepted.
// Server: api/routes/change_review.py. Nothing here invents a decision: no record → pending.
//
// SIM (demo) mode has no server. Decisions are kept in memory for the session only and every
// record carries `sim: true` so the UI can say "not saved".
import { SIM, fetchChangeReviews, putChangeReview } from './api.js'
import { changeIdOf } from './reportEvidence.js'

export const VERDICTS = Object.freeze(['accepted', 'edited', 'rejected', 'unable'])
// `edited` records a CORRECTION THE REVIEWER IS ASKING FOR. The PUT writes the proposed value into
// the decision log; it does not touch the document, does not re-run the fixer, and is not a
// confirmation that the change is right. It used to read "Accepted with edits", which says the
// opposite: that the edit was accepted and (by implication) written. Nothing counts it as
// confirmed until a corrected copy exists and has been re-reviewed.
export const VERDICT_LABEL = Object.freeze({
  accepted: 'Accepted', edited: 'Correction requested', rejected: 'Rejected', unable: 'Unable to verify',
})
export const VERDICT_DETAIL = Object.freeze({
  accepted: null,
  edited: 'Correction requested — the proposed wording is recorded for someone to apply. It has NOT been applied to the document and does not count as a confirmation.',
  rejected: null,
  unable: null,
})
export const STALE_TEXT = 'Needs recheck — the document changed since this decision'
// Freshness UNKNOWN is not acceptance. Without the current artifact identity or the current change
// digest there is no way to tell whether this decision still describes what is in the document, and
// "we could not check" must never render as "checked and confirmed".
export const FRESHNESS_UNKNOWN_TEXT = 'Freshness unknown — recheck'
export const FRESHNESS_UNKNOWN_DETAIL =
  'ACP could not establish whether the document or this change moved on since the decision was recorded, so the decision is not treated as current. Recheck it.'
export const SIM_NOTICE = 'Demo mode — decisions are kept in this browser tab only and are not saved.'
export const NOTE_MAX = 2000
export const EDITED_VALUE_MAX = 4000

// The one change id. The SERVER supplies it on every saved change (api/report_facts.py):
// `{file}::{ruleId}::{seq}` for a verified change and `{file}::{ruleId}::u{16hex}` for one that was
// applied but not re-scanned. The client cannot reproduce the second form — its seq is null — so
// the server's id is used verbatim wherever it exists, and changeIdOf is only the legacy fallback
// for records that came from the remediation-diff route.
export const changeId = (file, diff, index = 0) => (
  typeof diff?.id === 'string' && diff.id ? diff.id : changeIdOf(file, diff, index)
)

// A decision can only be recorded against a change with a stable SERVER identity. The facts
// endpoint supplies one on every saved change (verified and unverified alike); without it the only
// other stable form is rule id + a numeric remediation_diff seq. The `i<index>` fallback id is
// display-only — it moves when the list does.
export const hasStableId = (diff) => diff != null && (
  (typeof diff.id === 'string' && diff.id.length > 0)
  || ((diff.rule_id ?? diff.ruleId) != null && diff.seq != null && /^\d+$/.test(String(diff.seq)))
)

const hex = (buf) => Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('')

// sha256 of `${rule_id}\n${before}\n${after}` — identical to the server's change_digest.
// When the facts endpoint already computed it (`changeDigest` on every saved change) that value is
// used: it is the one the server will compare against, and recomputing it from a before/after the
// store CLIPPED at 2000 characters would produce a different hash and a permanent 409.
export async function changeDigest(diff) {
  if (typeof diff?.changeDigest === 'string' && diff.changeDigest) return diff.changeDigest
  const rule = String(diff?.rule_id ?? diff?.ruleId ?? '')
  const text = `${rule}\n${diff?.before ?? ''}\n${diff?.after ?? ''}`
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return null
  return hex(await subtle.digest('SHA-256', new TextEncoder().encode(text)))
}

// true = needs recheck, false = still bound to what is on record now, null = cannot tell.
// The server's own re-evaluation (`review.stale`) wins when it made one: it compares inside the
// same transaction that reads the artifact. `null` from the server means IT could not tell either,
// and stays null here — it is never narrowed to false.
export function isStale(review, artifact, currentDigest = undefined) {
  if (!review) return null
  // The server evaluates staleness inside the transaction that reads the artifact, so when it
  // answered at all, its answer wins — INCLUDING an explicit null, which means it could not tell.
  // Narrowing that to false here is exactly the "unknown freshness read as confirmed" defect.
  if (review.stale === true) return true
  if (review.stale === false) return false
  if (review.stale === null && Object.prototype.hasOwnProperty.call(review, 'stale')) return null
  // Local fallback. A decision binds to the SAVED COPY's sha-256 (ratified 2026-09-17), never to
  // the source checksum: a decision about a saved change tied to the original's checksum would
  // claim an edit is correct against bytes that do not contain it. No corrected sha => unknown.
  const current = artifact?.correctedSha256 ?? null
  if (!current || !review.artifact_sha256) return null
  if (review.artifact_sha256 !== current) return true
  // The digest check was ASKED FOR and could not be computed: unknown, not fresh and not stale.
  // It used to answer "stale" here, which reads to a reviewer as "the document changed" when in
  // fact nothing was compared.
  if (currentDigest !== undefined) return currentDigest == null ? null : review.change_digest !== currentDigest
  // The artifact identity the decision was bound to is the one on record now, and no finer check
  // was requested: the decision still describes the version that was reviewed.
  return review.stale === true
}

// One sentence a reviewer can act on, for each of the three freshness answers.
export function freshnessOf(review, artifact, currentDigest = undefined) {
  const stale = isStale(review, artifact, currentDigest)
  if (stale === true) {
    return { stale: true, label: STALE_TEXT, detail: review?.staleReason || 'The document or this change moved on after the decision was recorded. Look at the current version and record the decision again.' }
  }
  if (stale === false) return { stale: false, label: 'Current', detail: null }
  return { stale: null, label: FRESHNESS_UNKNOWN_TEXT, detail: review?.staleReason || FRESHNESS_UNKNOWN_DETAIL }
}

// Reading SIM defensively: a test's partial api.js mock without it must not break the drawer.
const isSim = () => { try { return !!SIM } catch { return false } }
export const simMode = isSim

const EMPTY_ARTIFACT = Object.freeze({ sourceSha256: null, sourceChecksum: null, correctedSha256: null, currentSha256: null, identityKind: null })

// ── SIM store (session memory only) ────────────────────────────────────────────────────────
const simStore = new Map()   // `${scanId}::${file}` -> { [changeId]: review }
const simKey = (scanId, file) => `${scanId || 'sim'}::${file}`
export const _resetSimReviews = () => simStore.clear()

// Load recorded decisions for one file. Never throws: returns
// { ok, sim, artifact, reviews: {changeId: review}, error }.
export async function loadChangeReviews(scanId, file) {
  if (isSim()) {
    return { ok: true, sim: true, artifact: { ...EMPTY_ARTIFACT }, reviews: { ...(simStore.get(simKey(scanId, file)) || {}) }, error: null }
  }
  if (!scanId || !file) return { ok: false, sim: false, artifact: { ...EMPTY_ARTIFACT }, reviews: {}, error: 'No assessment selected.' }
  try {
    const res = await fetchChangeReviews(scanId, file)
    return {
      ok: true, sim: false,
      artifact: { ...EMPTY_ARTIFACT, ...(res?.artifact || {}) },
      reviews: res?.reviews && typeof res.reviews === 'object' ? res.reviews : {},
      error: null,
    }
  } catch (e) {
    return { ok: false, sim: false, artifact: { ...EMPTY_ARTIFACT }, reviews: {}, error: e?.message || 'Recorded decisions could not be loaded.' }
  }
}

// What the server's refusals mean, said in what a reviewer can do about them. The route answers
// 422 when a required binding token is absent and 409 when one no longer matches what is stored —
// both are about the VERSION the decision would be tied to, not about the verdict.
export const REFUSAL_TEXT = Object.freeze({
  missingTokens: 'Not saved: this decision has to be tied to the exact document version and the exact change you reviewed, and ACP could not establish both. Reload the document and try again.',
  moved: 'Not saved: the document or this change moved on while you were reviewing it, so the decision would have been recorded against something you did not see. Reload the document, look at the current version, and record the decision again.',
  noSavedCopyIdentity: 'Not saved: ACP has no recorded checksum for the saved corrected copy, so a decision cannot be tied to the version that was written. This has to be resolved before these changes can be signed off.',
})
export function refusalFor(status, detail) {
  const text = String(detail || '')
  if (status === 409 && /saved copy|corrected copy/i.test(text)) return REFUSAL_TEXT.noSavedCopyIdentity
  if (status === 409) return REFUSAL_TEXT.moved
  if (status === 422) return REFUSAL_TEXT.missingTokens
  return `Not saved: ${text || 'the decision could not be recorded.'}`
}

// Save one decision. Resolves { ok, sim, review, artifact, error, status } — never throws.
//
// BOTH binding tokens are REQUIRED by the route and are checked here first, so a client that
// cannot supply them refuses in words rather than sending a PUT the server would bind to bytes
// nobody reviewed (the route answers 422, but by then the reader has been told "try again" with
// nothing to change).
export async function saveChangeReview(scanId, file, diff, { verdict, note = '', editedValue = null, expectedSha256 } = {}, { reviewer = null } = {}) {
  const id = changeId(file, diff)
  if (!VERDICTS.includes(verdict)) return { ok: false, error: 'Choose Accept, Edit, Reject or Unable to verify.' }
  if (!hasStableId(diff)) return { ok: false, error: 'Cannot record a decision: this change has no stable id.' }
  const cleanNote = String(note || '').trim()
  if (cleanNote.length > NOTE_MAX) return { ok: false, error: `The note is longer than ${NOTE_MAX} characters.` }
  const edited = verdict === 'edited' ? String(editedValue || '') : null
  if (verdict === 'edited' && !edited.trim()) return { ok: false, error: 'Enter the corrected value before saving an edit.' }
  if (edited && edited.length > EDITED_VALUE_MAX) return { ok: false, error: `The edited value is longer than ${EDITED_VALUE_MAX} characters.` }
  const digest = await changeDigest(diff)
  if (isSim()) {
    const review = {
      change_id: id, verdict, note: cleanNote || null, edited_value: edited,
      artifact_sha256: null, change_digest: digest, reviewer: reviewer || 'Demo user',
      at: new Date().toISOString(), stale: null, sim: true,
    }
    const key = simKey(scanId, file)
    simStore.set(key, { ...(simStore.get(key) || {}), [id]: review })
    return { ok: true, sim: true, review, artifact: { ...EMPTY_ARTIFACT }, error: null }
  }
  if (!digest || !expectedSha256) {
    return { ok: false, sim: false, status: 422, error: REFUSAL_TEXT.missingTokens }
  }
  try {
    const body = {
      verdict, note: cleanNote || null, edited_value: edited,
      change_digest: digest, expected_sha256: expectedSha256,
    }
    const res = await putChangeReview(scanId, file, id, body)
    return { ok: true, sim: false, review: res?.review || null, artifact: res?.artifact || null, error: null }
  } catch (e) {
    return { ok: false, sim: false, status: e?.status ?? null, error: refusalFor(e?.status, e?.message) }
  }
}

// The `reviews` input for the report model: keyed by changeId, only real recorded decisions.
export function reviewsForReport(reviews, artifact = null) {
  const out = {}
  Object.entries(reviews || {}).forEach(([id, r]) => {
    if (!r || !VERDICTS.includes(r.verdict)) return
    out[id] = {
      verdict: r.verdict, note: r.note ?? null, edited_value: r.edited_value ?? null,
      reviewer: r.reviewer ?? null, at: r.at ?? null,
      artifact_sha256: r.artifact_sha256 ?? null, change_digest: r.change_digest ?? null,
      current_change_digest: r.current_change_digest ?? null,
      stale: artifact ? isStale(r, artifact) : (r.stale ?? null),
      // Why, in words, so "unknown" can never be rendered as "confirmed".
      staleReason: r.staleReason ?? null,
      sim: r.sim === true,
    }
  })
  return out
}
