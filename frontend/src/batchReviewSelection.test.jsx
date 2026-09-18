import { describe, it, expect, afterEach, vi } from 'vitest'
import { createElement } from 'react'
import { act } from 'react'
import { createTestRoot, unmountAll } from './testRoots.js'
import BatchReviewSelection from './BatchReviewSelection.jsx'
import { batchDecision, exclusionReason, snapshotFinding, selectionProblem } from './batchReviewSelection.js'
afterEach(unmountAll)
const finding = (id, overrides = {}) => ({ id, file: `${String(id).padStart(3, '0')}.docx`, scanId: 'scan', ruleId: '1.1.1', hasProposal: true,
  after: `draft ${id}`, proposals: [{ proposed_value: `draft ${id}`, before: 'old' }],
  _raw: { decision_version: 2, proposal_snapshot_ids: [`snapshot-${id}`], source_revision: 'source-1', corrected_artifact: 'none' }, ...overrides })
const click = async el => act(async () => el.dispatchEvent(new MouseEvent('click', { bubbles: true })))
async function mount(props) {
  const { container, root } = createTestRoot()
  const render = async extra => act(async () => root.render(createElement(BatchReviewSelection, { scopeKey: 'scan', ...props, ...extra })))
  await render()
  return { container, render, button: text => [...container.querySelectorAll('button')].find(b => b.textContent.includes(text)) }
}
describe('explicit batch selection', () => {
  it('starts empty, paging and preview never write; one checkbox binds only that exact finding', async () => {
    const onDecide = vi.fn().mockResolvedValue(undefined)
    const view = await mount({ visible: Array.from({ length: 23 }, (_, i) => finding(i)), onDecide })
    expect(view.button('Approve selected')).toBeUndefined()
    await click(view.button('Next batch page'))
    await click(view.container.querySelector('input'))
    await click(view.button('Approve selected'))
    expect(onDecide).not.toHaveBeenCalled()
    expect(view.container.textContent).toContain('1 findings selected (1 review items) · 1 proposals · 1 files')
    expect(view.container.textContent).toContain('old')
    await click(view.button('Confirm approval'))
    expect(onDecide).toHaveBeenCalledTimes(1)
    expect(onDecide.mock.calls[0][0].id).toBe(10)
    expect(onDecide.mock.calls[0][1]).toMatchObject({ expectedVersion: 2, expectedSourceRevision: 'source-1', expectedProposalSnapshotIds: ['snapshot-10'], approvedValues: ['draft 10'] })
    expect(view.container.textContent).toContain('Approval finished: 1 approved')
  })
  it('never broadens selection with refresh or filters and blocks changed proposal versions', async () => {
    const a = finding(1), onDecide = vi.fn()
    const v = await mount({ visible: [a], onDecide })
    await click(v.container.querySelector('input'))
    await v.render({ visible: [a, finding(2)] })
    expect(v.container.textContent).toContain('1 findings selected')
    await v.render({ visible: [finding(2)] })
    expect(v.button('Approve selected').disabled).toBe(true)
    await v.render({ visible: [finding(1, { _raw: { ...a._raw, proposal_snapshot_ids: ['new'] } })] })
    expect(v.container.textContent).toContain('Proposal or source changed')
    expect(onDecide).not.toHaveBeenCalled()
    await v.render({ scopeKey: 'other-scan' })
    expect(v.button('Approve selected')).toBeUndefined()
  })
  it('counts every proposal and excludes manual, missing, applied and edited work', async () => {
    const multi = finding(1, { proposals: [{ proposed_value: 'A' }, { proposed_value: 'B' }], _raw: { ...finding(1)._raw, finding_count: 2, proposal_snapshot_ids: ['a', 'b'] } })
    const missing = finding(2, { proposals: [{ proposed_value: 'A' }, {}] })
    const v = await mount({ visible: [multi, missing, finding(3, { autoApplied: true }), finding(4)], drafts: { 4: 'unsaved' }, onDecide: vi.fn() })
    await click(v.button('Select all ready'))
    expect(v.container.textContent).toContain('2 findings selected (1 review items) · 2 proposals · 1 files')
    expect(v.container.textContent).toContain('missing proposal')
    expect(exclusionReason(finding(9, { hasProposal: false, after: null, proposals: [] }))).toBe('Manual work')
  })
  it('retains failed IDs with the same request ID and never retries successes or ambiguous writes', async () => {
    const onDecide = vi.fn(async f => {
      if (f.id === 2) throw Object.assign(new Error('conflict'), { status: 409 })
      if (f.id === 3) throw new TypeError('connection lost')
    })
    const v = await mount({ visible: [finding(1), finding(2), finding(3)], onDecide })
    await click(v.button('Select all ready')); await click(v.button('Approve selected')); await click(v.button('Confirm approval'))
    expect(v.container.textContent).toContain('Approval finished: 1 approved, 1 failed, 1 uncertain.')
    const requestId = onDecide.mock.calls[1][1].requestId
    await click(v.button('Approve selected')); await click(v.button('Confirm approval'))
    expect(onDecide.mock.calls.map(c => c[0].id)).toEqual([1, 2, 3, 2])
    expect(onDecide.mock.calls[3][1].requestId).toBe(requestId)
  })
  it('stops unsent work after a scope change during a write and suppresses double submit', async () => {
    let release
    const onDecide = vi.fn(() => new Promise(resolve => { release = resolve }))
    const v = await mount({ visible: [finding(1), finding(2)], onDecide })
    await click(v.button('Select all ready')); await click(v.button('Approve selected'))
    const confirm = v.button('Confirm approval')
    await click(confirm); await click(confirm)
    expect(onDecide).toHaveBeenCalledTimes(1)
    await v.render({ scopeKey: 'changed' })
    await act(async () => release())
    expect(onDecide).toHaveBeenCalledTimes(1)
    expect(v.container.textContent).toContain('Review scope changed')
    expect(v.container.querySelector('.batch-sr-only[role=status]').textContent).toBe('')
  })
  it('freezes all values and detects locator and source replacement', () => {
    const f = finding(1), entry = snapshotFinding(f)
    f.proposals[0].proposed_value = 'replacement'
    expect(batchDecision(entry).approvedValues).toEqual(['draft 1'])
    expect(selectionProblem(entry, [f], {}, {})).toContain('changed')
  })
})

it('keeps confirmation compact and shows only acknowledged success deltas with quiet announcements', async () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true }))
  const calls = []
  const onDecide = vi.fn(() => new Promise((resolve, reject) => calls.push({ resolve, reject })))
  const multi = finding(1, { proposals: [{ proposed_value: 'A' }, { proposed_value: 'B' }], _raw: { ...finding(1)._raw, finding_count: 2, proposal_snapshot_ids: ['a', 'b'] } })
  const v = await mount({ visible: [multi, finding(2), finding(3)], onDecide })
  v.container.querySelector('.batch-review-inspection').open = true
  await click(v.button('Approve all ready'))
  expect(v.container.querySelector('.batch-review-inspection').open).toBe(false)
  expect(v.container.querySelector('.batch-approval-summary').textContent).toContain('3 review items · 4 proposals · 3 files')
  expect(onDecide).not.toHaveBeenCalled()
  await click(v.button('Confirm approval'))
  const counter = () => v.container.querySelector('.batch-approved .livecounter-n').textContent
  expect(counter()).toBe('0')
  expect(v.container.querySelector('.livecounter-delta')).toBeNull()
  const announcement = v.container.querySelector('.batch-sr-only[role=status]').textContent
  await act(async () => calls[0].resolve())
  expect(counter()).toBe('1')
  expect(v.container.querySelector('.livecounter-delta').textContent).toBe('+1')
  expect(v.container.querySelector('.batch-sr-only[role=status]').textContent).toBe(announcement)
  await act(async () => calls[1].reject(Object.assign(new Error('Conflict'), { status: 409 })))
  expect(counter()).toBe('1')
  await act(async () => calls[2].reject(new TypeError('Lost connection')))
  expect(counter()).toBe('1')
  expect(v.container.querySelector('.batch-approval-errors').open).toBe(false)
  expect(v.container.textContent).toContain('2 review items need attention')
  expect(v.container.querySelector('.batch-sr-only[role=status]').textContent).toContain('1 approved, 1 failed, 1 uncertain')
  await v.render({})
  expect(counter()).toBe('1')
  expect(onDecide).toHaveBeenCalledTimes(3)
  await v.render({ scopeKey: 'different-run' })
  expect(v.container.querySelector('.batch-sr-only[role=status]').textContent).toBe('')
  vi.unstubAllGlobals()
})

it('explains stale source recovery without automatically retrying approval', async () => {
  const onDecide = vi.fn().mockRejectedValue(Object.assign(new Error('stale source revision'), { status: 409 }))
  const v = await mount({ visible: [finding(1)], onDecide })
  await click(v.button('Approve all ready'))
  await click(v.button('Confirm approval'))
  expect(v.container.textContent).toContain('refresh this page, then select the current proposals and confirm again')
  expect(onDecide).toHaveBeenCalledOnce()
})

it('distinguishes proposal values from covered findings in the primary approval action', async () => {
  const multi = finding(1, { proposals: [{ proposed_value: 'A' }, { proposed_value: 'B' }], _raw: { ...finding(1)._raw, finding_count: 1, proposal_snapshot_ids: ['a', 'b'] } })
  const v = await mount({ visible: [multi], onDecide: vi.fn() })
  expect(v.button('Approve all ready').textContent).toBe('Approve all ready (2 proposals)')
  expect(v.container.textContent).toContain('1 findings covered by 2 ready proposals')
  await click(v.button('Approve all ready'))
  expect(v.container.querySelector('[aria-label="Approval summary"]').textContent).toContain('1 findings · 1 review item · 2 proposals · 1 file')
})

it('a row whose target a verified fix removed is settled work: never selectable, explained, not "not included"', async () => {
  const replaced = finding(7, { _raw: { ...finding(7)._raw, superseded: true, superseded_reason: 'target_removed_by_verified_fix',
    superseded_evidence: { removed_by_rule_id: '1.4.5', targets: ['docx:drawing:1:paragraph:32'] } } })
  expect(exclusionReason(replaced)).toBe('Replaced by a verified change — nothing left to approve')
  expect(() => batchDecision(snapshotFinding(replaced))).toThrow(/Replaced by a verified change/)
  const onDecide = vi.fn()
  const v = await mount({ visible: [replaced, finding(8)], onDecide })
  const summary = v.container.querySelector('.batch-review-exclusions summary').textContent
  expect(summary).toBe('0 pending review items not included · 1 already resolved')
  expect(v.container.querySelector('.batch-review-why')?.textContent ?? '')
    .not.toContain('pending review items')
  expect(onDecide).not.toHaveBeenCalled()
})

it('explains a target-replaced exclusion when it is the reason nothing can be approved', async () => {
  const replaced = finding(9, { _raw: { ...finding(9)._raw, superseded: true, superseded_reason: 'target_removed_by_verified_fix' } })
  const v = await mount({ visible: [replaced] })
  expect(v.container.querySelector('.batch-review-why').textContent)
    .toContain('replaced by a verified change — nothing left to approve — another verified fix removed what this item described')
})

// D -> F phase 5: a frozen selection names the corrected copy it was made against.
describe('the frozen selection binds the corrected artifact', () => {
  const sha = (c) => c.repeat(64)
  const withArtifact = (artifact) => finding(7, { _raw: { decision_version: 2, proposal_snapshot_ids: ['snapshot-7'], source_revision: 'source-1', corrected_artifact: artifact } })
  it('freezes it into the decision verbatim (a hash, or "none")', () => {
    expect(batchDecision(snapshotFinding(withArtifact(sha('a')))).expectedCorrectedSha256).toBe(sha('a'))
    expect(batchDecision(snapshotFinding(withArtifact('none'))).expectedCorrectedSha256).toBe('none')
  })
  it('a corrected copy changed by another approved write invalidates the frozen entry', () => {
    const entry = snapshotFinding(withArtifact(sha('a')))
    expect(selectionProblem(entry, [withArtifact(sha('a'))], {}, {})).toBeNull()
    expect(selectionProblem(entry, [withArtifact(sha('c'))], {}, {})).toBe('Proposal or source changed — select again')
  })
  it('a row without it is never offered for, or sent in, a batch', () => {
    const { corrected_artifact, ...raw } = withArtifact(sha('a'))._raw // eslint-disable-line no-unused-vars
    const unbound = finding(7, { _raw: raw })
    expect(exclusionReason(unbound)).toBe('Version unavailable — review individually')
    expect(() => batchDecision(snapshotFinding(unbound))).toThrow(/Version unavailable/)
  })
})
