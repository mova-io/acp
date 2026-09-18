import { afterEach, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationInbox from './RemediationInbox.jsx'
import { remediationRecoveryGuidance } from './remediationRecoveryGuidance.js'

afterEach(unmountAll)
const row = { id: 1, file: 'brief.docx', rule_id: '1.1.1', hasProposal: true, after: 'A meaningful image description', before: 'Missing alt text' }

it('explains missing version information without claiming automatic admission', () => {
  expect(remediationRecoveryGuidance(row).reason).toContain('saved version information')
})
it('keeps unsupported PDF structure manual even with a prose suggestion', () => {
  expect(remediationRecoveryGuidance({ ...row, file: 'brief.pdf', rule_id: '1.3.1' })).toMatchObject({ title: 'Edit the source document' })
})
it('does not offer another recovery action for admitted or verified work', () => {
  expect(remediationRecoveryGuidance({ ...row, automaticQueued: true })).toBeNull()
  expect(remediationRecoveryGuidance({ ...row, validated: true })).toBeNull()
})
it('shows the saved human decision reason before generic lineage guidance', () => {
  const result = remediationRecoveryGuidance({ ...row, _raw: { proposal_snapshot_ids: ['snapshot'], source_revision: 1, decision_version: 1, corrected_artifact: 'none' }, automaticDisposition: { responsibility: 'human', reason: 'The saved policy requires review of this criterion.' } })
  expect(result.reason).toBe('The saved policy requires review of this criterion.')
})
it('opens the real remediation plan from the selected item', async () => {
  const { root, container } = createTestRoot()
  const onOpenPlan = vi.fn()
  await act(async () => root.render(createElement(RemediationInbox, { queue: [row], decisions: {}, onOpenPlan, initialTab: 'active' })))
  const section = container.querySelector('[aria-label="What this item needs"]')
  expect(section.textContent).toContain('saved version information')
  const button = [...section.querySelectorAll('button')].find(button => button.textContent === 'Open remediation plan')
  await act(async () => button.dispatchEvent(new MouseEvent('click', { bubbles: true })))
  expect(onOpenPlan).toHaveBeenCalledOnce()
})
it('makes recovery plan actions unavailable in historical views', async () => {
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(RemediationInbox, { queue: [row], decisions: {}, onOpenPlan: vi.fn(), readOnly: true, initialTab: 'active' })))
  expect(container.querySelector('[aria-label="What this item needs"] button').disabled).toBe(true)
})
it('verifies saved bytes once and reports remaining issues without approving a finding', async () => {
  const { root, container } = createTestRoot()
  const onVerifySaved = vi.fn().mockResolvedValue({ assessment_ok: true, remaining_issues: [{rule_id:'1.1.1'}, {rule_id:'1.3.1'}] })
  const onDecide = vi.fn()
  const applied = { ...row, applied: true, validated: false }
  await act(async () => root.render(createElement(RemediationInbox, { queue: [applied], decisions: {}, onVerifySaved, onDecide, initialTab: 'all' })))
  const button = [...container.querySelectorAll('button')].find(button => button.textContent === 'Retry verification of saved copy')
  expect(button).toBeTruthy()
  await act(async () => button.dispatchEvent(new MouseEvent('click', { bubbles: true })))
  expect(onVerifySaved).toHaveBeenCalledOnce()
  expect(onDecide).not.toHaveBeenCalled()
  expect(container.textContent).toContain('Saved copy checked: 2 remaining issues.')
})
it('shows a verification refusal without changing the recorded finding', async () => {
  const { root, container } = createTestRoot()
  const onVerifySaved = vi.fn().mockRejectedValue(new Error('Corrected bytes changed.'))
  await act(async () => root.render(createElement(RemediationInbox, { queue: [{ ...row, applied: true }], decisions: {}, onVerifySaved, initialTab: 'all' })))
  const button = [...container.querySelectorAll('button')].find(button => button.textContent === 'Retry verification of saved copy')
  await act(async () => button.dispatchEvent(new MouseEvent('click', { bubbles: true })))
  expect(container.querySelector('[role="alert"]').textContent).toContain('Corrected bytes changed.')
})
