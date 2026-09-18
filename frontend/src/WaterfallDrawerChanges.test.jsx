import { act, createElement } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import WaterfallDrawerChanges from './WaterfallDrawerChanges.jsx'
import useRemediationAttemptStory from './useRemediationAttemptStory.js'
vi.mock('./useRemediationAttemptStory.js', () => ({ default: vi.fn() }))
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const record = {
  attempts: [{ attempt_id: 'one', provider: 'cloud', model: 'primary', actual_cost_units: 0 }, { attempt_id: 'two', provider: 'cloud', model: 'fallback' }],
  proposals: [
    { snapshot_id: 'p1', file: 'one.pdf', attempt_id: 'one', rule_id: '1.1.1', proposal: { before: '', proposed_value: 'Useful description', locator: 'page-1/image-1' }, system_approvals: [{ action: 'standing_approve', approval_evidence: { current_source: 'passed', application: 'pending', post_change_verification: 'pending' } }] },
    { snapshot_id: 'p2', file: 'two.pdf', attempt_id: 'two', proposal: { before: 'Original', proposed_value: 'Updated' }, version_verified: true },
    { snapshot_id: 'unlinked', file: 'one.pdf', proposal: { before: 'Other source', proposed_value: 'Unlinked suggestion' } },
  ], pagination: { limit: 100, has_more: true }, coverage: 'complete',
}
async function mount(props = {}) {
  const { root, container } = createTestRoot()
  const render = overrides => act(async () => root.render(createElement(WaterfallDrawerChanges, { scanId: 'scan', batchId: 'batch', ...props, ...overrides })))
  await render()
  return { container, render }
}
beforeEach(() => { vi.clearAllMocks(); useRemediationAttemptStory.mockReturnValue({ data: record, refresh: vi.fn() }) })
afterEach(unmountAll)
it('shows only exact-attempt linked proposals and distinguishes automatic approval from verification', async () => {
  const { container } = await mount({ modelFilter: { attemptIds: ['one'] } })
  expect(container.textContent).toContain('primary · cloud')
  expect(container.textContent).toContain('$0.000000 USD')
  expect(container.textContent).toContain('One call may cover several findings')
  expect(container.textContent).toContain('Useful description')
  expect(container.textContent).toContain('Empty recorded value')
  expect(container.textContent).toContain('Automatically approved')
  expect(container.textContent).toContain('verification unconfirmed')
  expect(container.textContent).toContain('Checks recorded at approval time')
  expect(container.querySelector('[aria-label="Automatic approval checks"]').textContent).toContain('Source is current✓ Passed')
  expect(container.querySelector('[aria-label="Automatic approval checks"]').textContent).toContain('Post-change checks○ Pending')
  expect(container.textContent).not.toContain('Unlinked suggestion')
  expect(container.querySelectorAll('[aria-label="Recorded change locations"] button')).toHaveLength(1)
})
it('navigates recorded documents and locations without inferring a page or losing run scope', async () => {
  const { container, render } = await mount()
  const select = container.querySelector('select')
  await act(async () => { select.value = 'two.pdf'; select.dispatchEvent(new Event('change', { bubbles: true })) })
  expect(container.textContent).toContain('Corrected · verified value')
  expect(container.textContent).toContain('Document location not recorded')
  expect(container.textContent).toContain('Updated')
  await render({ batchId: 'different' })
  expect(container.querySelector('select').value).toBe('one.pdf')
  expect(container.textContent).toContain('Useful description')
})
it('pages saved records and warns that an empty model page is not an empty run', async () => {
  const { container } = await mount({ modelFilter: { attemptIds: ['missing'] } })
  expect(container.textContent).toContain('Other record pages may contain changes')
  const next = [...container.querySelectorAll('button')].find(button => button.textContent === 'Next records')
  await act(async () => next.click())
  expect(useRemediationAttemptStory).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 100 }))
  expect(container.textContent).toContain('Record page 2')
})
it('reads full-document results from the saved response and does not attribute them to one model', async () => {
  useRemediationAttemptStory.mockReturnValue({ data: { ...record, document_wide: { enabled: true, complete: true, files: [{ file: 'wide.pdf', suggestions: 3, reasons: [] }] } }, refresh: vi.fn() })
  const { container, render } = await mount()
  expect(container.textContent).toContain('wide.pdf · 3 suggestions')
  await render({ modelFilter: { attemptIds: ['one'] } })
  expect(container.textContent).not.toContain('wide.pdf')
})

it('matches exact proposal attempt IDs even when attempts are on a different record page', async () => {
  useRemediationAttemptStory.mockReturnValue({ data: { ...record, attempts: [] }, refresh: vi.fn() })
  const { container } = await mount({ modelFilter: { attemptIds: ['one'] } })
  expect(container.textContent).toContain('Useful description')
  expect(container.textContent).not.toContain('Unlinked suggestion')
})
it('highlights removed and added text while preserving full recorded values', async () => {
  useRemediationAttemptStory.mockReturnValue({ data: { ...record, proposals: [{ ...record.proposals[0], proposal: { before: 'A blurry tree beside the road.', proposed_value: 'A green tree beside the road.' } }] }, refresh: vi.fn() })
  const { container } = await mount()
  const comparison = container.querySelector('.waterfall-change-comparison')
  expect(comparison.querySelector('del').textContent).toBe('blurry')
  expect(comparison.querySelector('ins').textContent).toBe('green')
  const full = container.querySelector('.waterfall-change-full-values')
  expect(full.textContent).toContain('A blurry tree beside the road.')
  expect(full.textContent).toContain('A green tree beside the road.')
  expect(container.querySelector('.waterfall-change-approval').className).not.toContain('verified')
})
it('puts verification and comparison before model details and keeps confidence distinct from proof', async () => {
  const { container } = await mount({ modelFilter: { attemptIds: ['one'] } })
  const comparison = container.querySelector('.waterfall-change-comparison')
  const model = container.querySelector('.waterfall-change-model')
  expect(comparison.compareDocumentPosition(model) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(model.open).toBe(false)
  expect(model.textContent).toContain('Model confidence is an estimate, not proof')
  expect(container.querySelector('.waterfall-change-verification').textContent).toContain('verification are not recorded')
  expect(container.textContent).toContain('Proposed value')
  expect(container.textContent).not.toContain('Corrected · verified value')
})
