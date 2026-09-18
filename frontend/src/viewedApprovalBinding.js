// The version of a review row the reviewer is LOOKING AT, in the form PUT /hitl/queue/{id} compares
// under the row lock: `expected_version`, `expected_source_revision`, `expected_proposal_snapshot_ids`.
//
// Why this exists (audit gap 8): single approvals used to send the decision version only, and the
// server stamped whatever assessment revision was current at click time. A re-assessment between
// opening a suggestion and approving it therefore bound the approval to bytes the reviewer never
// saw. The fields below are read off the row object that is on screen — never re-fetched — so a
// server that has moved on answers 409 instead of recording an approval of something unseen.
//
// Field sources (as serialized by GET /hitl/queue, api/routes/hitl.py hitl_list):
//   source_revision        — the assessment input revision the row was listed against
//   proposal_snapshot_ids  — one immutable snapshot id per proposal, positionally aligned
//   decision_version       — the row's decision counter
//   corrected_artifact     — the file's current corrected-copy sha256, or the literal "none" when there is
//                            no corrected copy yet; the server always fills it (D -> F, phase 5)
//   proposal_digest        — an OPAQUE server digest of the row's reviewable content (every proposal and
//                            evidence entry except the reviewer-owned approved_value); echoed, never
//                            recomputed (D -> F, phase 6)
// Remediate's inbox rows carry the server row as `_raw`; FileDrawer/HitlBell hold server rows as is.

// `target_removed` is the same shape of refusal from the reviewer's side: the row they were looking at
// no longer has a target (another verified fix replaced it), so the row is re-read and nothing retried.
export const VIEWED_VERSION_CODES = Object.freeze(['stale_viewed_version', 'viewed_version_required', 'target_removed'])

// The server's sentence for a stale binding (contract P1). Used only when a 409 carries the code but
// no readable message.
export const STALE_VIEWED_VERSION_MESSAGE =
  'This suggestion or its document changed after you opened it. Review the current version, then approve again.'

// Fallback only; the server's own sentence is preferred whenever the 409 carries one.
export const TARGET_REMOVED_MESSAGE =
  'Another verified change already removed what this suggestion targets, so there is nothing left to approve.'

// Said instead of sending an approval that could not name the version it approves.
export const VIEWED_VERSION_MISSING_MESSAGE =
  'This suggestion must be refreshed before it can be approved — ACP cannot confirm which version of it you are looking at, '
  + 'so nothing was approved. The review queue is refreshing; review the current version, then approve again.'

const serverRow = (row) => (row && typeof row._raw === 'object' && row._raw ? row._raw : row)

// { expectedVersion, expectedSourceRevision, expectedProposalSnapshotIds, expectedCorrectedSha256 } for the
// viewed row, or
// null when the row does not carry a binding. The snapshot list is sent exactly as served — legacy
// null slots included — and a row served without one binds to `[]`, the empty list it was shown
// (the server compares against the same reading of its row: api/store.py complete_hitl_decision).
// The source revision has no such reading: a row listed without one cannot name what it approves.
// Neither has the corrected artifact: it is sent VERBATIM (a hash, or "none"), and a row listed without
// it cannot name which saved copy the reviewer was looking at — never guessed, never defaulted.
export function viewedApprovalBinding(row) {
  const raw = serverRow(row)
  if (!raw || typeof raw !== 'object') return null
  const revision = raw.source_revision
  if (typeof revision !== 'string' || !revision.trim()) return null
  const artifact = raw.corrected_artifact
  if (typeof artifact !== 'string' || !artifact.trim()) return null
  const digest = raw.proposal_digest
  if (typeof digest !== 'string' || !digest.trim()) return null
  const snapshots = raw.proposal_snapshot_ids
  if (snapshots != null && !Array.isArray(snapshots)) return null
  return {
    expectedVersion: raw.decision_version ?? 0,
    expectedSourceRevision: revision,
    expectedProposalSnapshotIds: Array.isArray(snapshots) ? [...snapshots] : [],
    expectedCorrectedSha256: artifact,
    expectedProposalDigest: digest,
  }
}

// A stable token for "this version of this row", so a surface can remount an editor that was seeded
// from an older version instead of letting its text ride along under a newer binding.
export function viewedBindingKey(row) {
  const raw = serverRow(row) || {}
  return JSON.stringify([raw.id ?? null, raw.decision_version ?? null, raw.source_revision ?? null,
    raw.proposal_snapshot_ids ?? null, raw.corrected_artifact ?? null, raw.proposal_digest ?? null, raw.status ?? null,
    raw.approval_recheck_required ?? null, raw.approval_recheck_reason ?? null,
    (Array.isArray(raw.proposals) ? raw.proposals : []).map((p) => [p?.locator ?? null, p?.proposed_value ?? null])])
}

// The update options ONE reviewer's decision on the row on screen sends. `approvalScope: 'single'`
// tells the server to compare the viewed binding (and to allow edited values) instead of applying the
// frozen-batch guard; a batch selection sends its own frozen binding without it. Approvals REQUIRE
// the binding (null when it is missing, so the caller refuses instead of sending an unbound
// approval). Rejections and skips never require it, and carry it when the viewed row has one.
export function viewedDecisionOptions(row, status) {
  const binding = viewedApprovalBinding(row)
  if (binding) return { ...binding, approvalScope: 'single' }
  if (status === 'approved') return null
  return { expectedVersion: serverRow(row)?.decision_version ?? 0, approvalScope: 'single' }
}

// The refusal raised in place of an unbound approval. `changes: 'none'` — nothing was sent. Status
// 409 with changes 'none' marks it a definite refusal (as batchDecision's local refusal does), so no
// caller mistakes it for an uncertain transport failure; `local` says the server was never asked.
export function viewedVersionMissingError() {
  return Object.assign(new Error(VIEWED_VERSION_MISSING_MESSAGE),
    { status: 409, code: 'viewed_version_required', changes: 'none', viewedVersionConflict: true, local: true })
}

// Normalise a 409 viewed-version refusal from updateHitlItem into an Error whose message is the
// server's sentence. api.js's reader lifts a top-level {code, message}; a FastAPI
// HTTPException(detail={code, message}) instead arrives with `detail` as an object and a message of
// "[object Object]". Anything that is not one of the two codes returns null — callers keep their
// existing handling for every other failure.
export function viewedVersionConflict(err) {
  if (!err || err.status !== 409) return null
  // The frozen-batch guard (and a reject/skip whose decision version moved) still answers with a
  // bare legacy detail string. Same outcome for the reviewer: nothing recorded, re-read, decide again.
  if (!err.code && LEGACY_STALE_DETAILS.includes(String(err.message || '').trim())) {
    return Object.assign(new Error(LEGACY_STALE_MESSAGE),
      { status: 409, code: 'stale_selection', changes: 'none', viewedVersionConflict: true, cause: err })
  }
  const nested = err.detail && typeof err.detail === 'object' ? err.detail : null
  const code = VIEWED_VERSION_CODES.includes(err.code) ? err.code
    : VIEWED_VERSION_CODES.includes(nested?.code) ? nested.code
    : VIEWED_VERSION_CODES.includes(err.detail) ? err.detail : null
  if (!code) return null
  const own = typeof err.message === 'string' && err.message && err.message !== '[object Object]'
    && !VIEWED_VERSION_CODES.includes(err.message) ? err.message : ''
  const message = (typeof nested?.message === 'string' && nested.message) || own
    || (code === 'viewed_version_required' ? VIEWED_VERSION_MISSING_MESSAGE
      : code === 'target_removed' ? TARGET_REMOVED_MESSAGE : STALE_VIEWED_VERSION_MESSAGE)
  return Object.assign(new Error(message),
    { status: 409, code, changes: 'none', viewedVersionConflict: true, cause: err })
}

// Ask every mounted review surface to re-read its queue (the existing refresh signal).
export function requestReviewQueueRefresh() {
  window.dispatchEvent(new Event('acp:hitl-changed'))
}

// The frozen-batch guard's refusals keep the server's legacy 409 detail strings (D -> F). Read as one
// honest sentence: the item moved on after it was selected, and nothing was recorded.
export const LEGACY_STALE_DETAILS = Object.freeze(['stale source revision', 'stale proposal selection', 'stale decision version'])
export const LEGACY_STALE_MESSAGE =
  'This item changed after you opened or selected it — its document, corrected copy or suggestion moved on — so '
  + 'nothing was recorded. Review the current version, then decide again.'

// An approval on record that the writer is HOLDING (listed with approval_recheck_required). The flag says
// only that ACP cannot confirm the approval still matches the current version — for every legacy approval
// recorded before bindings existed (binding_missing) nothing changed at all — so a CHANGE is stated only
// when the server's `approval_recheck_reason` names one (heldExplanation).
// One 'single' re-approval of the CURRENT row re-binds it on the server.
export const needsReapproval = (row) => {
  const raw = serverRow(row) || {}
  return raw.approval_recheck_required === true && String(raw.status || '').toLowerCase() === 'approved'
}
export const HELD_LABEL = 'Approved earlier · version needs confirmation'
export const REAPPROVE_ACTION = 'Review and approve again'
export const REAPPROVE_EXPLANATION =
  'An approval for this is on record, but ACP cannot confirm it still matches the current version of this document and '
  + 'suggestion, so it is holding it and has written nothing from it. Review the current version, then approve it again — '
  + 'your new approval replaces the held one.'

// Why a held approval is held, in words — a change is claimed ONLY for a server reason that means one
// (D -> F phase 6). binding_missing, an unknown reason and no reason at all get the generic sentence.
const HELD_CAUSE = {
  binding_missing: 'The version this approval was given for was not recorded, so ACP cannot confirm it matches the current one.',
  source_moved: 'The document’s assessed source has changed since this approval was given.',
  artifact_moved: 'The saved corrected copy of this document has changed since this approval was given.',
  values_changed: 'The values on record no longer match the ones that were approved.',
  proposals_superseded: 'This suggestion has been replaced since it was approved.',
}
export function heldExplanation(row) {
  const reason = (serverRow(row) || {}).approval_recheck_reason
  const cause = Object.prototype.hasOwnProperty.call(HELD_CAUSE, reason) ? HELD_CAUSE[reason] : null
  if (!cause) return REAPPROVE_EXPLANATION
  return `${cause} ACP is holding the approval and has written nothing from it. Review the current version, then `
    + 'approve it again — your new approval replaces the held one.'
}

// The values a re-approval of a held row approves: exactly what the writer would write for it — each
// instance's recorded approved value, else (for a proposal row that is not a described-image obligation)
// its current draft. Mirrors the server's value digest (api/store.py _approved_value_digest), so the new
// approval's digest is the one the writer will check, and re-approving cannot itself cause a value hold.
export function reapprovalValues(row) {
  const raw = serverRow(row) || {}
  const proposals = Array.isArray(raw.proposals) ? raw.proposals : []
  const instances = proposals.length ? proposals : (Array.isArray(raw.evidence) ? raw.evidence : [])
  const draftFallback = (raw.resolution || null) !== 'described_not_replaced'
  return instances.filter((i) => i && typeof i === 'object').map((i) => {
    const approved = String(i.approved_value || '').trim()
    return approved || (proposals.length && draftFallback ? String(i.proposed_value || '').trim() : '')
  })
}

// The whole request for re-approving a held row, or null when the row cannot name its version (the
// caller then refuses and refreshes, as for any unbound approval).
export function reapprovalRequest(row) {
  const raw = serverRow(row) || {}
  const bound = viewedDecisionOptions(row, 'approved')
  if (!raw.id || !bound) return null
  const values = reapprovalValues(row)
  const resolution = raw.resolution || null
  return {
    id: raw.id,
    approvedValue: values.find(Boolean) || raw.approved_value || null,
    options: { ...bound, approvedValues: values.length ? values : null, resolution },
    values,
  }
}
