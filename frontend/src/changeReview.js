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
export const VERDICT_LABEL = Object.freeze({
  accepted: 'Accepted', edited: 'Accepted with edits', rejected: 'Rejected', unable: 'Unable to verify',
})
export const STALE_TEXT = 'Needs recheck — the document changed since this decision'
export const SIM_NOTICE = 'Demo mode — decisions are kept in this browser tab only and are not saved.'
export const NOTE_MAX = 2000
export const EDITED_VALUE_MAX = 4000

// The one change id, shared with the report model (stream A's changeIdOf).
export const changeId = (file, diff, index = 0) => changeIdOf(file, diff, index)

// A decision can only be recorded against a change with a stable server identity: rule id + a
// numeric seq from remediation_diff. The `i<index>` fallback id is display-only.
export const hasStableId = (diff) => diff != null && (diff.rule_id ?? diff.ruleId) != null
  && diff.seq != null && /^\d+$/.test(String(diff.seq))

const hex = (buf) => Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('')

// sha256 of `${rule_id}\n${before}\n${after}` — identical to the server's change_digest.
export async function changeDigest(diff) {
  const rule = String(diff?.rule_id ?? diff?.ruleId ?? '')
  const text = `${rule}\n${diff?.before ?? ''}\n${diff?.after ?? ''}`
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return null
  return hex(await subtle.digest('SHA-256', new TextEncoder().encode(text)))
}

// true = needs recheck, false = still bound to what is on record now, null = cannot tell.
export function isStale(review, artifact, currentDigest = undefined) {
  if (!review) return null
  const current = artifact?.currentSha256 ?? null
  if (!current || !review.artifact_sha256) return review.stale === true ? true : null
  if (review.artifact_sha256 !== current) return true
  if (currentDigest !== undefined) return currentDigest == null || review.change_digest !== currentDigest
  return review.stale === true
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

// Save one decision. Resolves { ok, sim, review, artifact, error } — never throws.
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
  try {
    const body = { verdict, note: cleanNote || null, edited_value: edited }
    if (digest) body.change_digest = digest
    if (expectedSha256) body.expected_sha256 = expectedSha256
    const res = await putChangeReview(scanId, file, id, body)
    return { ok: true, sim: false, review: res?.review || null, artifact: res?.artifact || null, error: null }
  } catch (e) {
    return { ok: false, sim: false, error: e?.message || 'The decision was not saved.' }
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
      stale: artifact ? isStale(r, artifact) : (r.stale ?? null),
      sim: r.sim === true,
    }
  })
  return out
}
