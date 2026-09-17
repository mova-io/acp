import { act } from 'react'
import { afterEach, expect, it } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import Summary from './RemediationCompletionSummary.jsx'
afterEach(unmountAll)
async function mount(snapshot, view, exact = false) {
  const { root, container } = createTestRoot()
  await act(async () => root.render(<Summary snapshot={snapshot} view={view} exact={exact} reviewHref="/?tab=remediate&mode=review" releaseHref="/?tab=publish" />))
  return container
}
it('stays absent while processing', async () => { expect((await mount({ terminal: false })).textContent).toBe('') })
it('separates verified changes, review items, retained charges, and undelivered copies', async () => {
  const c = await mount({ terminal: true, fixes: { verified: 12 }, review: { items: 3 }, documents: { failed: 0 }, delivery: { awaiting_release: 2 } }, { available: true, spending: { spent_units: 120000, held_units: 30000, blocked: true } })
  expect(c.textContent).toContain('Verified changes · all origins12')
  expect(c.textContent).toContain('Review queue items · not findings3')
  expect(c.textContent).toContain('$0.12')
  expect(c.textContent).toContain('$0.03 remains reserved')
  expect(c.textContent).toContain('2 corrected copies awaiting Release')
  expect([...c.querySelectorAll('a')].map(a => a.textContent)).toEqual(['Open review workspace'])
})
it('never turns unknown or invalidated evidence into zero or a completion claim', async () => {
  const c = await mount({ terminal: true, state: 'cancelled', fixes: { verified: 20 }, review: { items: 10 }, delivery: { awaiting_release: 4 }, integrity: { ok: false, affected: ['fixes', 'review', 'delivery'] } })
  expect(c.textContent).toContain('Run stopped')
  expect(c.textContent).toContain('Unavailable')
  expect(c.querySelector('a')).toBeNull()
  expect(c.textContent).not.toContain('Automatic processing finished')
})

it('does not call a fully charged budget breach an unsettled reservation', async () => {
  const c = await mount({ terminal: true }, { available: true, spending: { spent_units: 2000000, held_units: 0, blocked: true } })
  expect(c.textContent).toContain('Further AI spending is on hold')
  expect(c.textContent).not.toContain('remains reserved')
  expect(c.textContent).not.toContain('not settled')
})

it('separates processed files from successful fixes and published copies', async () => {
  const c = await mount({ terminal: true, total_documents: 8, documents: { waiting: 0, processing: 0, failed: 2 }, fixes: { verified: 12 }, delivery: { delivered: 3 }, review: { items: 2 } })
  expect(c.textContent).toContain('Files processed · attempt finished8')
  expect(c.textContent).toContain('Copies published · delivery confirmed3')
  expect(c.textContent).toContain('2 failed documents')
})
it('does not calculate processed files from incomplete counters', async () => {
  const c = await mount({ terminal: true, total_documents: 8, documents: { failed: 2 }, delivery: {} })
  expect(c.textContent).toContain('Files processed · attempt finishedUnavailable')
  expect(c.textContent).toContain('Copies published · delivery confirmedUnavailable')
})
it('does not count a cancelled attempt as a processed file', async () => {
  const c = await mount({ terminal: true, total_documents: 8, documents: { waiting: 0, processing: 0 }, outcome_reasons: { cancelled: 2 } })
  expect(c.textContent).toContain('Files processed · attempt finished6')
})

it('keeps finished processing, verified findings and partial publication distinct in the DOM', async () => {
  const c = await mount({ terminal: true, state: 'completed', total_documents: 8,
    documents: { waiting: 0, processing: 0, failed: 0 }, delivery: { delivered: 3, awaiting_release: 5 },
    finding_reconciliation: { assessed: 100, resolved_verified: 40, awaiting_review: 60 } }, null, true)
  expect(c.querySelector('h4').textContent).toBe('Automatic processing finished')
  expect(c.textContent).toContain('Files processed · attempt finished8')
  expect(c.textContent).toContain('Fixes verified · findings40')
  expect(c.textContent).toContain('Copies published · delivery confirmed3')
  expect(c.textContent).toContain('Remaining findings · not verified60')
  expect(c.textContent).toContain('Findings awaiting resolution60')
  expect(c.textContent).not.toContain('Awaiting your review')
  expect(c.textContent).not.toContain('provide any missing content')
  expect(c.textContent).toContain('use Status checks for recovery and Needs your input for decisions or manual edits')
})
