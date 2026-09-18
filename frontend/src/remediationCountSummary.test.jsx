import { act, createElement } from 'react'
import { afterEach, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { autoFixRows } from './remediationInboxModel.js'
import { remediationReviewCounts, remediationDiffPage, reviewBadgeTitle } from './remediationCountSummary.js'
import Header from './RemediationRunHeader.jsx'
import Inbox from './RemediationInbox.jsx'
import { createTestRoot, unmountAll } from './testRoots.js'
globalThis.IS_REACT_ACT_ENVIRONMENT = true
afterEach(unmountAll)
const applied = autoFixRows(Array.from({ length: 2000 }, (_, i) => ({ file: `doc-${i % 177}.pptx`, rule_id: '2.4.6', after: 'Applied title' })))
const pending = Array.from({ length: 299 }, (_, i) => ({ id: `pending-${i}`, file: `doc-${i % 177}.pptx`, hasProposal: true, after: 'Proposed title', proposals: [{ proposed_value: 'Proposed title' }], _raw: { corrected_artifact: 'none', decision_version: 0, proposal_snapshot_ids: [`snapshot-${i}`], source_revision: 'source' } }))
const manual = Array.from({ length: 47 }, (_, i) => ({ id: `manual-${i}`, file: `doc-${i}.pptx`, title: 'Manual work' }))
const rows = [...pending, ...manual, ...applied]
it('counts 299 eligible approvals and 47 manual items across 177 documents, excluding 2000 inspection rows', async () => {
  const counts = remediationReviewCounts(rows)
  // `findings` matches `pendingItems` here: every fixture row covers a single finding.
  expect(counts).toEqual({ ready: 299, manual: 47, individual: 0, pendingItems: 346, findings: 346, documents: 177, inspection: 2000 })
  expect(reviewBadgeTitle(counts.pendingItems)).toBe('346 review items requiring attention')
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(Header, { counts: { automaticOnly: false, autoFixed: 2400, documents: 177, needsApproval: counts.ready, manual: counts.manual, inspection: counts.inspection } })))
  expect(container.textContent).toContain('299 review tasks need approval')
  expect(container.textContent).toContain('47 review tasks require manual work')
  expect(container.textContent).toContain('2000 loaded changes available to inspect')
  expect(container.textContent).not.toContain('2299')
  expect(container.textContent).toContain('2400 change records applied across 177 documents')
  expect(container.textContent).not.toContain('applied automatically')
})
it('the approval category counts all eligible proposals while manual work is open', async () => {
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(Inbox, { legacyApprovalControls: true, queue: rows, decisions: {}, scanId: 'fixture', initialTab: 'manual' })))
  const tab = [...container.querySelectorAll('select[aria-label="Filter by status"] option:not([value=all])')].find(el => el.textContent.includes('Approve AI suggestions'))
  // 299 approvals + 2000 applied changes awaiting confirmation, which is what the tab lists. The
  // badge used to read 299 over 2299 rows — the separation this file exists to protect, made on the
  // one element that cannot express it, because a bare number beside a label carries no noun.
  expect(tab.textContent).toBe('Approve AI suggestions 299')
  // The separation itself is intact, and now stated where each number has a noun to go with it.
  const summary = container.querySelector('.run-approval-summary').textContent
  expect(summary).toContain('299 ready review items')
  expect(summary).toContain('2000 applied changes available to inspect')
  expect(summary).toContain('47 manual review items')
  expect([...container.querySelectorAll('select[aria-label="Filter by status"] option:not([value=all])')].find(el => el.textContent.includes('Fix manually')).selected).toBe(true)
})
it('wires the actual shell and page to review-item counts, with separate eligible approvals and server totals', () => {
  const app = readFileSync('src/App.jsx', 'utf8'), page = readFileSync('src/Remediate.jsx', 'utf8')
  expect(app).toContain('title={reviewBadgeTitle(hitlCount)}')
  expect(page).toContain('onHitlCount?.(reviewCounts.pendingItems)')
  expect(page).toContain('reviewCount={reviewCounts.pendingItems}')
  expect(page).toContain('needsApproval: reviewCounts.ready')
  expect(page).toContain('getScanRemediationDiffs(runId, true)')
  expect(page).toContain('autoFixed: fixTotal ?? undefined')
})
it('uses authoritative full totals and labels legacy or malformed completeness as unknown', async () => {
  expect(remediationDiffPage({ items: applied, total: 2400, documents: 177, loaded: 2000, complete: false })).toMatchObject({ total: 2400, complete: false })
  for (const value of [applied, { items: applied, total: 12, documents: 1, loaded: 2000, complete: true }]) {
    const page = remediationDiffPage(value)
    expect(page.total).toBeNull(); expect(page.complete).toBe(false)
  }
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(Header, { counts: { autoFixedLoaded: 2000 } })))
  expect(container.textContent).toContain('2000 applied-change records loaded · total unavailable')
  expect(container.textContent).not.toContain('2000 fixes applied automatically')
})

it('labels actual already-applied rows as inspection without changing the decision contract', async () => {
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(Inbox, { queue: applied.slice(0, 1), initialTab: 'awaiting-validation', decisions: {}, scanId: 'fixture' })))
  expect(container.textContent).toContain('Inspecting this applied change does not approve or apply another change')
  expect([...container.querySelectorAll('button')].some(button => button.textContent === 'Mark inspected →')).toBe(true)
  expect(container.textContent).not.toContain('then approve it')
})
