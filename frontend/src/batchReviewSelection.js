import { isPdfStructuralRow, pdfStructuralSummary, proposalsFor, requiresPdfSourceEditing } from './pdfStructuralProposal.js'
import { isResolved, isTargetReplaced, laneOf } from './remediationInboxModel.js'

// A row whose target was removed by a verified fix. Named first because `isResolved` is also true
// for it, and "Already reviewed" would claim a person decided something nobody was asked about.
export const TARGET_REPLACED_EXCLUSION = 'Replaced by a verified change — nothing left to approve'

export function proposalValues(f) {
  const proposals = f.proposals || f._raw?.proposals || []
  return proposals.length ? proposals.map(p => p.proposed_value) : [f.after]
}
export function exclusionReason(f, decisions = {}, drafts = {}) {
  if (isTargetReplaced(f)) return TARGET_REPLACED_EXCLUSION
  if (isResolved(f, decisions)) return 'Already reviewed'
  if (requiresPdfSourceEditing(f)) return 'Manual PDF tagging — use a source or PDF accessibility editor'
  if (f.stale || f.superseded || f._raw?.superseded) return 'Stale — refresh and review'
  if (isPdfStructuralRow(f) && !proposalsFor(f).every(pdfStructuralSummary)) return 'Invalid structural proposal — refresh suggestions'
  const lane = laneOf(f).key
  if (lane === 'review') return 'Already applied — review individually'
  if (lane === 'manual' || lane === 'handoff') return 'Manual work'
  if (lane !== 'apply' || f.canApprove === false) return 'Blocked or unavailable'
  if (drafts[f.id] != null && drafts[f.id] !== f.after) return 'Unsaved edit — review individually'
  if ((f._raw?.finding_count || 0) > proposalValues(f).length || !proposalValues(f).every(v => typeof v === 'string' && v.trim())) return 'Missing proposal'
  // Production rows require persisted lineage; legacy/demo rows may be inspected individually.
  if (!f._raw?.proposal_snapshot_ids?.length || f._raw.proposal_snapshot_ids.length !== proposalValues(f).length
    || f._raw.proposal_snapshot_ids.some(id => !id) || f._raw?.source_revision == null || f._raw?.decision_version == null
    // The corrected copy the selection was made against (a sha256, or "none"): frozen with the rest, and
    // required — a batch that cannot name it is not sent (D -> F, phase 5).
    || typeof f._raw?.corrected_artifact !== 'string' || !f._raw.corrected_artifact.trim()
    // …and the reviewable content it was made against (D -> F phase 6).
    || typeof f._raw?.proposal_digest !== 'string' || !f._raw.proposal_digest.trim())
    return 'Version unavailable — review individually'
  return null
}
// Deliberately include every proposal and locator, not just the first displayed value.
export function selectionFingerprint(f) {
  return JSON.stringify([f.id, f.scanId, f.file, f.ruleId || f.rule_id,
    f.before, f.after, f.proposals || f._raw?.proposals,
    f._raw?.decision_version, f._raw?.proposal_snapshot_ids, f._raw?.source_revision, f._raw?.corrected_artifact, f._raw?.proposal_digest])
}
export function snapshotFinding(f) {
  return { finding: JSON.parse(JSON.stringify(f)), fingerprint: selectionFingerprint(f),
    requestId: globalThis.crypto?.randomUUID?.() || `batch-${Date.now()}-${Math.random().toString(16).slice(2)}` }
}
export function selectionProblem(entry, visible, decisions, drafts) {
  const current = visible.find(f => f.id === entry.finding.id)
  if (!current) return 'Outside this view or no longer available'
  return exclusionReason(current, decisions, drafts)
    || (selectionFingerprint(current) !== entry.fingerprint ? 'Proposal or source changed — select again' : null)
}
export function batchDecision(entry) {
  const f = entry.finding
  // Last gate before a request is built: a superseded or target-replaced row can never be approved,
  // even if it reached here without passing selectionProblem.
  if (isTargetReplaced(f) || f.superseded || f._raw?.superseded || f.stale) {
    // status/changes mark it a definite refusal (nothing sent), not an uncertain transport failure.
    throw Object.assign(new Error(isTargetReplaced(f) ? TARGET_REPLACED_EXCLUSION : 'Stale — refresh and review'),
      { status: 409, changes: 'none' })
  }
  // Never send a batch approval that cannot name the corrected copy it was frozen against.
  if (typeof f._raw?.corrected_artifact !== 'string' || !f._raw.corrected_artifact.trim()
      || typeof f._raw?.proposal_digest !== 'string' || !f._raw.proposal_digest.trim()) {
    throw Object.assign(new Error('Version unavailable — review individually'), { status: 409, changes: 'none' })
  }
  return { state: 'accepted', value: proposalValues(f)[0], approvedValues: proposalValues(f),
    requestId: entry.requestId, expectedVersion: f._raw.decision_version,
    expectedProposalSnapshotIds: [...f._raw.proposal_snapshot_ids], expectedSourceRevision: f._raw.source_revision,
    expectedCorrectedSha256: f._raw.corrected_artifact,
    expectedProposalDigest: f._raw.proposal_digest,
    selectionFingerprint: entry.fingerprint }
}
