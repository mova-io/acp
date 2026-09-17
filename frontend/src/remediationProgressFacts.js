import { integrityAffects } from './remediationSnapshot.js'

const count = value => Number.isSafeInteger(value) && value >= 0
const stamp = value => typeof value === 'string' && value.trim() && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null
export function elapsedText(ms) {
  if (!Number.isFinite(ms) || ms < 0) return 'Unavailable'
  const seconds = Math.floor(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  return minutes < 60 ? `${minutes}m ${seconds % 60}s` : `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

// These are document dispositions, never worker/job counts. A partial partition is unknown.
export function remediationProgressFacts(snapshot, now = Date.now()) {
  const docs = snapshot?.documents || {}
  const values = ['completed', 'failed', 'processing', 'waiting', 'review', 'skipped'].map(key => docs[key])
  const known = !integrityAffects(snapshot, 'documents') && count(snapshot?.total_documents)
    && values.every(count) && values.reduce((sum, value) => sum + value, 0) === snapshot.total_documents
  const started = stamp(snapshot?.started_at)
  const material = stamp(snapshot?.progress?.material_at ?? snapshot?.latest_progress_at)
  const timeKnown = !integrityAffects(snapshot, 'freshness')
  const startValid = timeKnown && started !== null && started <= now
  const materialValid = timeKnown && material !== null && material <= now && (started === null || material >= started)
  return {
    finished: known ? docs.completed + docs.failed : null,
    processing: known ? docs.processing : null,
    waiting: known ? docs.waiting : null,
    routed: known ? docs.review + docs.skipped : null,
    elapsed: !snapshot?.terminal && startValid ? elapsedText(now - started) : null,
    started: startValid ? snapshot.started_at : null,
    material: materialValid ? (snapshot?.progress?.material_at ?? snapshot.latest_progress_at) : null,
    progressAge: materialValid ? elapsedText(now - material) : null,
  }
}
