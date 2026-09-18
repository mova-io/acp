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

// { expectedVersion, expectedSourceRevision, expectedProposalSnapshotIds } for the viewed row, or
// null when the row does not carry a binding. The snapshot list is sent exactly as served — legacy
// null slots included — and a row served without one binds to `[]`, the empty list it was shown
// (the server compares against the same reading of its row: api/store.py complete_hitl_decision).
// The source revision has no such reading: a row listed without one cannot name what it approves.
export function viewedApprovalBinding(row) {
  const raw = serverRow(row)
  if (!raw || typeof raw !== 'object') return null
  const revision = raw.source_revision
  if (typeof revision !== 'string' || !revision.trim()) return null
  const snapshots = raw.proposal_snapshot_ids
  if (snapshots != null && !Array.isArray(snapshots)) return null
  return {
    expectedVersion: raw.decision_version ?? 0,
    expectedSourceRevision: revision,
    expectedProposalSnapshotIds: Array.isArray(snapshots) ? [...snapshots] : [],
  }
}

// A stable token for "this version of this row", so a surface can remount an editor that was seeded
// from an older version instead of letting its text ride along under a newer binding.
export function viewedBindingKey(row) {
  const raw = serverRow(row) || {}
  return JSON.stringify([raw.id ?? null, raw.decision_version ?? null, raw.source_revision ?? null,
    raw.proposal_snapshot_ids ?? null,
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
