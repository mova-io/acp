// The pure half of the P1 viewed-version binding. The mounted callers are covered by
// viewedApprovalRemediate / viewedApprovalDrawer / viewedApprovalBell; this pins the edge rules.
import { describe, expect, it } from 'vitest'
import { STALE_VIEWED_VERSION_MESSAGE, VIEWED_VERSION_MISSING_MESSAGE, viewedApprovalBinding, viewedBindingKey,
  viewedDecisionOptions, viewedVersionConflict } from './viewedApprovalBinding.js'

const row = { id: 'r', decision_version: 4, source_revision: 'rev-1', proposal_snapshot_ids: ['s1', 's2'],
  proposals: [{ locator: 'a', proposed_value: 'x' }, { locator: 'b', proposed_value: 'y' }] }

describe('viewedApprovalBinding', () => {
  it('reads the server row, directly or through an inbox row\'s _raw, and copies the ids', () => {
    const b = viewedApprovalBinding(row)
    expect(b).toEqual({ expectedVersion: 4, expectedSourceRevision: 'rev-1', expectedProposalSnapshotIds: ['s1', 's2'] })
    expect(b.expectedProposalSnapshotIds).not.toBe(row.proposal_snapshot_ids)
    expect(viewedApprovalBinding({ id: 'r', after: 'x', _raw: row })).toEqual(b)
  })
  it('is null when the revision is missing or blank', () => {
    expect(viewedApprovalBinding({ ...row, source_revision: null })).toBeNull()
    expect(viewedApprovalBinding({ ...row, source_revision: '  ' })).toBeNull()
  })
  it('sends the snapshot list exactly as served — legacy null slots included — and [] when none was served', () => {
    expect(viewedApprovalBinding({ ...row, proposal_snapshot_ids: ['s1', null] }).expectedProposalSnapshotIds).toEqual(['s1', null])
    expect(viewedApprovalBinding({ ...row, proposal_snapshot_ids: null }).expectedProposalSnapshotIds).toEqual([])
    expect(viewedApprovalBinding({ id: 'j', decision_version: 0, source_revision: 'rev-1' }))
      .toEqual({ expectedVersion: 0, expectedSourceRevision: 'rev-1', expectedProposalSnapshotIds: [] })
  })
  it('is null for a snapshot field that is not a list', () => {
    expect(viewedApprovalBinding({ ...row, proposal_snapshot_ids: '["s1"]' })).toBeNull()
  })
  it('single decisions say so; approvals require the binding; rejections and skips fall back to the decision version', () => {
    const unbound = { ...row, source_revision: undefined }
    expect(viewedDecisionOptions(unbound, 'approved')).toBeNull()
    expect(viewedDecisionOptions(unbound, 'rejected')).toEqual({ expectedVersion: 4, approvalScope: 'single' })
    expect(viewedDecisionOptions(unbound, 'skipped')).toEqual({ expectedVersion: 4, approvalScope: 'single' })
    expect(viewedDecisionOptions(row, 'approved')).toEqual({ ...viewedApprovalBinding(row), approvalScope: 'single' })
    expect(viewedDecisionOptions(row, 'rejected')).toEqual({ ...viewedApprovalBinding(row), approvalScope: 'single' })
  })
  it('the version key changes when the revision, snapshots or proposal text change', () => {
    const k = viewedBindingKey(row)
    expect(viewedBindingKey({ ...row })).toBe(k)
    expect(viewedBindingKey({ ...row, source_revision: 'rev-2' })).not.toBe(k)
    expect(viewedBindingKey({ ...row, proposal_snapshot_ids: ['s1', 's3'] })).not.toBe(k)
    expect(viewedBindingKey({ ...row, proposals: [{ locator: 'a', proposed_value: 'z' }, row.proposals[1]] })).not.toBe(k)
  })
})

describe('viewedVersionConflict', () => {
  const err = (props, message = 'x') => Object.assign(new Error(message), props)
  it('reads a top-level {code, message} body', () => {
    const c = viewedVersionConflict(err({ status: 409, code: 'stale_viewed_version' }, 'Server sentence.'))
    expect(c).toMatchObject({ message: 'Server sentence.', code: 'stale_viewed_version', changes: 'none', viewedVersionConflict: true })
  })
  it('reads a FastAPI detail object instead of printing [object Object]', () => {
    const c = viewedVersionConflict(err({ status: 409, detail: { code: 'viewed_version_required', message: 'Refresh first.' } }, '[object Object]'))
    expect(c).toMatchObject({ message: 'Refresh first.', code: 'viewed_version_required' })
  })
  it('falls back to the contract sentence when the body carries only the code', () => {
    expect(viewedVersionConflict(err({ status: 409, detail: 'stale_viewed_version' }, 'stale_viewed_version')).message)
      .toBe(STALE_VIEWED_VERSION_MESSAGE)
    expect(viewedVersionConflict(err({ status: 409, detail: { code: 'viewed_version_required' } }, '[object Object]')).message)
      .toBe(VIEWED_VERSION_MISSING_MESSAGE)
  })
  it('treats a target_removed refusal the same way — refresh, no retry — keeping the server sentence', () => {
    expect(viewedVersionConflict(err({ status: 409, code: 'target_removed' }, 'Removed by a verified fix.')))
      .toMatchObject({ code: 'target_removed', message: 'Removed by a verified fix.', viewedVersionConflict: true })
  })
  it('leaves every other failure alone', () => {
    expect(viewedVersionConflict(err({ status: 409 }, 'stale decision version'))).toBeNull()
    expect(viewedVersionConflict(err({ status: 500, code: 'stale_viewed_version' }))).toBeNull()
    expect(viewedVersionConflict(null)).toBeNull()
  })
})
