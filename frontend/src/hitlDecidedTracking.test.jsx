/**
 * A review item that has been DECIDED must stay accounted for.
 *
 * Reported from production: a run's logs named 13 review items and the Remediate HITL inbox
 * showed 12. The missing one was not lost server-side — `hitl_queue` still held it. It was
 * dropped by the client:
 *
 *   - Remediate.jsx asked for `listHitlQueue(runId, 'pending')`, so a row the reviewer had
 *     already approved/rejected/skipped never reached the page at all, and
 *   - `dbItemToUi` did not carry the row's durable `status`, so even a decided row that did
 *     reach the inbox was classified from an empty status and read as outstanding work.
 *
 * The consequence is the one the report describes: the total shrinks by every decision taken
 * (13 → 12), and the Results tab — the place an approved AI suggestion is supposed to be
 * tracked — is empty after a reload, because the only completed rows it could ever show were
 * the ones decided in the current browser session.
 */
import { act, createElement } from 'react'
import { afterEach, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { dbItemToUi } from './Remediate.jsx'
import { workflowCounts, workflowStatusOf, isResolved } from './remediationInboxModel.js'
import { remediationReviewCounts } from './remediationCountSummary.js'
import Inbox from './RemediationInbox.jsx'
import { createTestRoot, unmountAll } from './testRoots.js'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
afterEach(unmountAll)

// A hitl_queue row as the API returns it, with the AI proposal these items carry.
const row = (i, extra = {}) => ({
  id: `row-${i}`, scan_id: 'scan-1', file: `doc-${i}.docx`, rule_id: '1.1.1',
  rule_name: 'Images need a text alternative', finding_count: 1, status: 'pending',
  decision_version: 0, source_revision: 'source', proposal_snapshot_ids: [`snap-${i}`], corrected_artifact: 'none',
  proposals: [{ proposed_value: `alt text ${i}`, source: 'ai vision', snapshot_id: `snap-${i}` }],
  ...extra,
})

// The production shape: thirteen queued items, one of them already approved and verified.
const SERVER_ROWS = [
  ...Array.from({ length: 12 }, (_, i) => row(i)),
  row(12, { status: 'approved', applied: 1, validated: 1, approved_value: 'alt text 12' }),
]
const ui = (rows) => rows.map((r) => dbItemToUi(r, []))

it('keeps a decided AI suggestion in the run total and tracks it under Results', () => {
  const rows = ui(SERVER_ROWS)
  const counts = workflowCounts(rows)
  expect(rows).toHaveLength(13)
  expect(counts.completed).toBe(1)
  expect(counts['needs-review']).toBe(12)
  // The nav badge counts OUTSTANDING work, so it stays at 12 — the decided item is accounted
  // for, not re-queued.
  expect(remediationReviewCounts(rows).pendingItems).toBe(12)
})

it.each([
  ['approved', { status: 'approved', applied: 1 }, 'awaiting-validation'],
  ['approved and verified', { status: 'approved', applied: 1, validated: 1 }, 'completed'],
  ['rejected', { status: 'rejected' }, 'completed'],
  ['skipped', { status: 'skipped' }, 'completed'],
  ['pending', {}, 'needs-review'],
])('places a %s row from its durable decision, not from this session only', (_label, extra, stage) => {
  const item = dbItemToUi(row(99, extra), [])
  expect(workflowStatusOf(item)).toBe(stage)
  expect(isResolved(item)).toBe(stage === 'completed' || stage === 'awaiting-validation')
})

it('shows the decided item in the Results tab of the inbox', async () => {
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(Inbox, {
    queue: ui(SERVER_ROWS), decisions: {}, scanId: 'scan-1', initialTab: 'completed',
  })))
  const tab = [...container.querySelectorAll('select[aria-label="Filter by status"] option:not([value=all])')].find((el) => el.textContent.includes('Results'))
  expect(tab.textContent).toBe('Results 1')
  expect(container.textContent).toContain('doc-12.docx')
  // The recorded outcome of an approved item, not a second approval prompt.
  expect(container.textContent).not.toContain('Save and continue')
})

it.each([
  // Contract C6: the verified line says the fresh scan no longer reports this item — never "Certified".
  ['a verified approval', { status: 'approved', applied: 1, validated: 1 }, 'the fresh scan of the corrected copy no longer reports this item'],
  ['an approval awaiting its re-scan', { status: 'approved', applied: 1 }, 'a fresh scan of the corrected copy has not confirmed it yet'],
  ['a rejection', { status: 'rejected' }, 'nothing was written to the document'],
])('states the recorded outcome of %s without claiming a write that did not happen', async (_l, extra, copy) => {
  const { root, container } = createTestRoot()
  const item = dbItemToUi(row(0, extra), [])
  await act(async () => root.render(createElement(Inbox, {
    queue: [item], decisions: {}, scanId: 'scan-1',
    initialTab: workflowStatusOf(item) === 'completed' ? 'completed' : 'awaiting-validation',
  })))
  expect(container.textContent).toContain(copy)
  expect(container.textContent).not.toMatch(/Certified/)
  if (extra.status === 'rejected') expect(container.textContent).not.toContain('Written → Re-scan')
})

it('never offers an item that already carries a decision for bulk approval', async () => {
  const { root, container } = createTestRoot()
  // Twelve outstanding rows, one approved and one deferred — neither of the latter is work the
  // run-wide "approve all ready" may act on, and a second PUT on an approved row is not a no-op.
  await act(async () => root.render(createElement(Inbox, {
    queue: ui([...SERVER_ROWS, row(13, { status: 'skipped' })]),
    decisions: {}, scanId: 'scan-1', initialTab: 'needs-review', onDecide: () => {}, legacyApprovalControls: true,
  })))
  const button = [...container.querySelectorAll('button, select[aria-label="Filter by status"] option')].find((b) => b.textContent.includes('Approve all ready in this run'))
  expect(button.textContent).toBe('Approve all ready in this run (12)')
})

it('reconciles review items against the findings they cover', () => {
  // A run whose logs report 13 findings can legitimately have 12 review items: one row covers a
  // criterion, and a criterion can fail twice in one document. The inbox says both numbers so
  // the difference reads as grouping rather than as a lost finding.
  const rows = ui([...Array.from({ length: 11 }, (_, i) => row(i)),
    row(11, { finding_count: 2 })])
  const counts = remediationReviewCounts(rows)
  expect(counts.pendingItems).toBe(12)
  expect(counts.findings).toBe(13)
  const page = readFileSync('src/Remediate.jsx', 'utf8')
  expect(page).toContain('reviewCounts.findings > reviewCounts.pendingItems')
})

it('loads every status for the run and folds decided items into the inbox and its total', () => {
  const page = readFileSync('src/Remediate.jsx', 'utf8')
  // The page reads the durable queue for the run, not only its pending slice.
  expect(page).not.toMatch(/listHitlQueue\(runId, 'pending'\)/)
  expect(page).toContain('applyHitlRows')
  // Every review task once: proven af:/HITL duplicates are collapsed before classification.
  expect(page).toContain('const reviewTasks = dedupeReviewTasks(reviewQueue)')
  expect(page).toContain('const inboxQueue = automaticReviewQueue(reviewTasks, runAiApproval.policy')
  expect(page).toContain('const reviewQueue = reviewableRemediationItems(dedupeById([...queue, ...rejectedItems, ...decidedItems, ...autoFixItems])')
  expect(page).toContain('const totalHitl = queue.length + decidedItems.length + selfOnly.length')
})


it('treats exact automatic inspection records as optional completed tasks without verification',async()=>{
 const record={id:'optional',scan_id:'scan-1',file:'auto.docx',rule_id:'auto/verify',rule_name:'Automatic fix applied — verify the result',status:'pending'}
 const item=dbItemToUi(record,[])
 expect(item.inspectionOnly).toBe(true)
 expect(workflowCounts([item])).toMatchObject({completed:1,'needs-review':0,manual:0})
 expect(remediationReviewCounts([item]).pendingItems).toBe(0)
 expect(item.validated).toBe(false)
 const {root,container}=createTestRoot()
 await act(async()=>root.render(createElement(Inbox,{queue:[item],initialTab:'completed'})))
 expect(container.textContent).toContain('Saved changes · optional inspection')
 expect(container.textContent).toContain('no approval or inspection is required')
 expect(container.textContent).not.toContain('Manual remediation')
 expect(container.textContent).not.toContain('Human confirmation required')
 expect(container.textContent).not.toContain('✓ Verified')
 expect([...container.querySelectorAll('button')].some(button=>['Defer','Not applicable'].includes(button.textContent.trim()))).toBe(false)
})
it.each([
 ['Deferred',{status:'skipped'},'Remaining work stays recorded'],
 ['Not applicable',{status:'approved',resolution:'out_of_scope'},'This criterion was excluded'],
])('preserves historical %s decisions as complete review tasks after a server reread',async(label,decision,copy)=>{
 const item=dbItemToUi(row(44,decision),[])
 expect(workflowStatusOf(item)).toBe('completed')
 expect(item.validated).toBe(false)
 const {root,container}=createTestRoot()
 await act(async()=>root.render(createElement(Inbox,{queue:[item],initialTab:'completed'})))
 expect(container.textContent).toContain(label)
 expect(container.textContent).toContain(copy)
 expect(container.textContent).not.toContain('Written → Re-scan')
 expect(container.textContent).not.toContain('✓ Verified')
})
