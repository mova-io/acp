import { it, expect, vi, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react'
import { createTestRoot, unmountAll } from './testRoots.js'
import BatchReviewSelection from './BatchReviewSelection.jsx'
import RemediationInbox from './RemediationInbox.jsx'
import { dbItemToUi } from './Remediate.jsx'
import { exclusionReason } from './batchReviewSelection.js'
afterEach(unmountAll)
const ready = id => ({ id, file: `z-${id}.docx`, ruleId: '1.1.1', hasProposal: true, after: `alt ${id}`,
  proposals: [{ proposed_value: `alt ${id}` }], _raw: { decision_version: 0, source_revision: 'source', proposal_snapshot_ids: [`snapshot-${id}`], corrected_artifact: 'none', proposal_digest: 'digest-test' } })
const applied = Array.from({ length: 119 }, (_, i) => ({ id: `applied-${i}`, file: `a-${i}.docx`, autoApplied: true, after: 'Applied change' }))
const click = async el => act(async () => el.tagName === 'OPTION' ? (el.parentElement.value = el.value, el.parentElement.dispatchEvent(new Event('change', { bubbles: true }))) : el.dispatchEvent(new MouseEvent('click', { bubbles: true })))
async function mount(Component, props) {
  const { root, container } = createTestRoot()
  const render = async next => act(async () => root.render(createElement(Component, { legacyApprovalControls: true, ...props, ...next })))
  await render()
  return { container, render, button: name => [...container.querySelectorAll('button, select[aria-label="Filter by status"] option')].find(b => b.textContent.includes(name)) }
}
it('keeps 119 already-applied rows out of the selectable pages and surfaces later ready proposals', async () => {
  const v = await mount(BatchReviewSelection, { visible: [...applied, ready('one'), ready('two')], onDecide: vi.fn(), scopeKey: 'scan' })
  expect(v.container.querySelectorAll('input[type=checkbox]')).toHaveLength(2)
  expect(v.button('Approve all ready (2 proposals)')).toBeTruthy()
  expect(v.container.querySelectorAll('input[disabled]')).toHaveLength(0)
  expect(v.container.textContent).not.toContain('Select only proposals you have reviewed')
})
it('freezes the complete cross-page ready set with optional inspection; refresh never adds new proposals', async () => {
  const onDecide = vi.fn().mockResolvedValue(undefined)
  const visible = [...applied, ...Array.from({ length: 25 }, (_, i) => ready(i))]
  const v = await mount(BatchReviewSelection, { visible, onDecide, scopeKey: 'scan', scopeLabel: 'All documents in this scan' })
  await click(v.button('Approve all ready (25 proposals)'))
  expect(onDecide).not.toHaveBeenCalled()
  expect(v.container.querySelector('details[open]')).toBeNull()
  await v.render({ visible: [...visible, ready('new')] })
  await click(v.button('Confirm approval'))
  expect(onDecide).toHaveBeenCalledTimes(25)
  expect(onDecide.mock.calls.map(c => c[0].id)).toEqual(Array.from({ length: 25 }, (_, i) => i))
})
it('opens scan-wide ready approval even when the individual queue tab has only applied work', async () => {
  const v = await mount(RemediationInbox, { queue: [...applied, ready('pending')], decisions: {}, initialTab: 'needs-review', scanId: 'scan', onDecide: vi.fn() })
  await click(v.button('Bulk approve ready proposals'))
  expect(v.button('Approve all ready (1 proposals)')).toBeTruthy()
  expect(v.container.textContent).toContain('All documents in this scan')
})
it('shows a truthful no-ready explanation and useful action instead of disabled checkboxes', async () => {
  const onReviewExcluded = vi.fn()
  const v = await mount(BatchReviewSelection, { visible: applied, onReviewExcluded, onDecide: vi.fn(), scopeKey: 'scan' })
  expect(v.container.querySelectorAll('input[type=checkbox]')).toHaveLength(0)
  expect(v.container.textContent).toContain('No proposals are ready for approval')
  await click(v.button('Open individual review'))
  expect(onReviewExcluded).toHaveBeenCalledOnce()
})

it.each([
  ['Fix manually', 'Manual issue', { id: 'manual', file: 'manual.docx', title: 'Manual issue' }],
  ['Awaiting verification', 'Writing issue', { ...ready('writing'), title: 'Writing issue', status: 'rechecking' }],
  ['Blocked', 'Blocked issue', { ...ready('blocked'), title: 'Blocked issue', status: 'blocked' }],
  ['Results', 'Completed issue', { ...ready('done'), title: 'Completed issue', status: 'verified' }],
])('exits bulk selection when switching to %s and clears stale approval intent', async (category, title, other) => {
  const onDecide = vi.fn()
  const v = await mount(RemediationInbox, { queue: [ready('pending'), other], decisions: {}, scanId: 'scan', onDecide })
  expect(v.container.querySelector('.rem-wsfoot')).not.toBeNull()
  await click(v.button('Bulk approve ready proposals'))
  await click(v.button('Approve all ready (1 proposals)'))
  const progress = v.container.querySelector('.rem-wsfoot')
  expect(progress).toBeNull()
  await click(v.button(category))
  const panel = v.container.querySelector('[aria-label="Select findings for approval"]')
  expect(panel.closest('[hidden]')).toBeTruthy()
  expect(v.container.querySelector('.remediation-detail')?.textContent).toContain(title)
  expect(v.container.querySelector('.rinbox').style.display).not.toBe('none')
  await click(v.button('Approve AI suggestions'))
  await click(v.button('Bulk approve ready proposals'))
  expect(v.button('Confirm approval')).toBeUndefined()
  expect(onDecide).not.toHaveBeenCalled()
})

it('explains dynamic missing proposal information and opens focused individual review without a decision', async () => {
  const onDecide = vi.fn()
  const unversioned = ['one', 'two'].map(id => ({ ...ready(id), title: 'Legacy proposal', _raw: {} }))
  const missing = { ...ready('missing'), proposals: [{ proposed_value: '' }] }
  const v = await mount(RemediationInbox, { queue: [...unversioned, missing], decisions: {}, initialTab: 'needs-review', scanId: 'scan', onDecide })
  await click(v.button('Bulk approve ready proposals'))
  const empty = v.container.querySelector('.batch-review-empty')
  expect(v.container.querySelector('.run-approval-summary')).toBeNull()
  expect(empty.querySelector('.batch-review-why').open).toBe(false)
  expect(v.container.querySelector('.batch-review').textContent).not.toContain('Approve the ready proposals together, or inspect them')
  expect(empty.textContent).toContain('ACP does not have approval-ready fixes')
  // Every reason is named and the counts sum to the scope, so "0 ready" is fully explained
  // rather than partly explained. Two of the eight reasons used to be named in prose and
  // the rest hidden in a collapsed disclosure.
  expect(empty.textContent).toContain('Why nothing can be approved here')
  expect(empty.textContent).toContain('all 3 review items')
  expect(empty.textContent).toContain('2 version unavailable')
  expect(empty.textContent).toContain('no recorded proposal version to approve against')
  expect(empty.textContent).toContain('1 missing proposal')
  expect(empty.textContent).toContain('no drafted value yet')
  expect(empty.textContent).toContain('Generating fresh proposals requires a separately approved run.')
  expect(empty.textContent).toContain('cover the whole run')
  expect(empty.textContent).not.toContain('refresh outdated proposals')
  await click(v.button('Open individual review'))
  expect(v.container.querySelector('[aria-label="Select findings for approval"]').closest('[hidden]')).toBeTruthy()
  expect(document.activeElement.textContent).toContain('Legacy proposal')
  expect(document.activeElement.tagName).toMatch(/^H[1-6]$/)
  expect(onDecide).not.toHaveBeenCalled()
})

it('opens a frozen whole-run confirmation from a selected issue scope without inspecting items', async () => {
  const onDecide = vi.fn().mockResolvedValue(undefined)
  const queue = [ready('one'), ready('two'), { ...ready('other-issue'), ruleId: '2.4.2', title: 'Different issue' }, { id: 'manual', file: 'manual.docx', title: 'Manual issue' }]
  const v = await mount(RemediationInbox, { queue, decisions: {}, scanId: 'whole-run', onDecide })
  await click(v.button('Select matching proposals'))
  await click(v.button('Approve all ready in this run (3)'))
  expect(v.container.textContent).toContain('All documents in this scan')
  expect(v.button('Confirm approval of 3 findings')).toBeTruthy()
  expect(v.container.querySelector('.batch-review-inspection').open).toBe(false)
  expect(onDecide).not.toHaveBeenCalled()
  await v.render({ queue: [...queue, ready('later')] })
  await click(v.button('Confirm approval of 3 findings'))
  expect(onDecide.mock.calls.map(call => call[0].id)).toEqual(['one', 'two', 'other-issue'])
})

it('shows preparing only from supplied active-job state and waits without suggesting another run', async () => {
  const onOpenPlan = vi.fn(), onDecide = vi.fn()
  const v = await mount(RemediationInbox, { queue: [{ ...ready('one'), _raw: {} }], decisions: {}, scanId: 'processing', preparingProposals: true, onOpenPlan, onDecide })
  await click(v.button('View run readiness'))
  expect(v.container.querySelector('.batch-review h3').textContent).toBe('Preparing proposals')
  expect(v.container.querySelector('.batch-review-empty').textContent).not.toContain('separately approved run')
  expect(v.button('Open remediation plan')).toBeUndefined()
  await v.render({ preparingProposals: false })
  expect(v.container.querySelector('.batch-review h3').textContent).toBe('No proposals ready')
  await click(v.button('Open remediation plan'))
  expect(onOpenPlan).toHaveBeenCalledOnce()
  expect(onDecide).not.toHaveBeenCalled()
})

it('consumes whole-run confirmation intent before reopening individual matching selection', async () => {
  const v = await mount(RemediationInbox, { queue: [ready('one'), ready('two')], decisions: {}, scanId: 'reopen', onDecide: vi.fn() })
  await click(v.button('Approve all ready in this run'))
  expect(v.button('Confirm approval')).toBeTruthy()
  await click(v.button('Return to individual review'))
  await click(v.button('Select matching proposals'))
  expect(v.button('Confirm approval')).toBeUndefined()
})
it.each([{ readOnly: true, onDecide: vi.fn() }, {}])('does not enable whole-run approval without write access', async extra => {
  const v = await mount(RemediationInbox, { queue: [ready('one')], decisions: {}, ...extra })
  expect(v.button('Approve all ready in this run').disabled).toBe(true)
})

it('includes ready HITL rows beyond the separate 2000 applied-inspection cap', async () => {
  const inspections = Array.from({ length: 2000 }, (_, i) => ({ id: `inspection-${i}`, file: 'inspected.docx', autoApplied: true, after: 'Applied' }))
  const v = await mount(RemediationInbox, { queue: [...inspections, ready('last')], decisions: {}, initialTab: 'manual', scanId: 'capped-inspection', onDecide: vi.fn() })
  await click(v.button('Approve all ready in this run (1)'))
  expect(v.button('Confirm approval of 1 findings')).toBeTruthy()
  // Still 2000, now said as what they are: pending work this batch does not cover. Finished work is
  // counted apart from it, so the headline is not padded with rows that were never candidates.
  expect(v.container.querySelector('.batch-review').textContent).toContain('2000 pending review items not included')
})

it('preserves server proposal lineage through the completion-refresh mapper without inventing missing evidence', () => {
  const raw = { id: 'persisted', file: 'document.docx', rule_id: '1.1.1', proposals: [{ proposed_value: 'Actual proposal' }], decision_version: 0, source_revision: 'source', proposal_snapshot_ids: ['snapshot'], corrected_artifact: 'none', proposal_digest: 'digest-test' }
  const before = JSON.stringify(raw)
  Object.freeze(raw)
  const mapped = dbItemToUi(raw, [])
  expect(mapped._raw).toBe(raw)
  expect(JSON.stringify(raw)).toBe(before)
  expect(raw._raw).toBeUndefined()
  expect(exclusionReason(mapped)).toBeNull()
  expect(exclusionReason(dbItemToUi({ ...raw, proposal_snapshot_ids: [] }, []))).toContain('Version unavailable')
})
