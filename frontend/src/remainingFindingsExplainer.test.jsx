import { act, createElement } from 'react'
import { afterEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import { automaticReviewQueue } from './automaticReviewQueue.js'
import { autoFixRows } from './remediationInboxModel.js'
import { explainReviewPopulation, findingInputsFrom } from './reviewPopulationExplanation.js'
import RemainingFindingsExplainer from './RemainingFindingsExplainer.jsx'

// Synthetic, production-shaped (the production case before the target-replacement fix): 1.1.1 alt
// text pending and parked in Status checks, 1.4.5 saved and awaiting its re-check, three confirmed
// applied changes, and the server's finding ledger listing 1.1.1 as awaiting review.
afterEach(unmountAll)
const FILE = 'synthetic-summary.docx'
const policy = { enabled: true, supported: true, run_id: 'run-1', source_revision: 'rev-1' }
const raw111 = { id: 101, scan_id: 'scan', file: FILE, rule_id: 'SC_1_1_1', rule_name: 'Non-text Content', status: 'pending',
  proposals: [{ proposed_value: 'Synthetic chart', locator: 'docx:drawing:1:paragraph:32' }], proposal_snapshot_ids: ['p1'], source_revision: 'rev-1', decision_version: 1 }
const rows = automaticReviewQueue([
  { id: 101, _raw: raw111, file: FILE, rule_id: 'SC_1_1_1', ruleId: 'SC_1_1_1', title: 'DOCX · Non-text Content', aiDraftable: true, hasProposal: true, after: 'Synthetic chart', proposals: raw111.proposals },
  { id: 102, _raw: { id: 102, rule_id: 'SC_1_4_5', rule_name: 'Images of Text', status: 'approved', applied: 1 }, file: FILE, rule_id: 'SC_1_4_5', status: 'approved', applied: true, validated: false, hasProposal: true, after: 'Totals' },
  ...autoFixRows([{ file: FILE, sc: '1.4.3', verified: true }, { file: FILE, sc: '3.1.1', verified: true }, { file: FILE, sc: '1.4.5', verified: true }], sc => sc),
], policy, {})
const finding = { finding_id: 'fnd-111', file: FILE, rule_id: 'SC_1_1_1', rule_name: 'Non-text Content', disposition: 'awaiting_review', review_item_id: 101 }

async function mount(explanation, onOpenItem = vi.fn()) {
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(RemainingFindingsExplainer, { explanation, onOpenItem })))
  return { container, onOpenItem }
}

it('names each remaining task with criterion, name, reason and next action, and opens the exact item', async () => {
  const explanation = explainReviewPopulation({ rows, automatic: true, unresolvedFindings: [finding], findingTotal: 1 })
  const { container, onOpenItem } = await mount(explanation)
  expect(container.querySelector('[role=status]').textContent).toBe(explanation.headline)
  expect(container.textContent).not.toMatch(/All clear/)
  const item = container.querySelector('li[data-item-id="101"]')
  expect(item.textContent).toContain('1.1.1 Non-text Content')
  expect(item.textContent).toContain(`${FILE} · Status checks`)
  const expected = explanation.remaining.find(r => r.itemId === 101)
  expect(item.textContent).toContain(`Why it is open: ${expected.reason}`)
  expect(item.textContent).toContain(`Next: ${expected.nextAction}`)
  expect(item.textContent).toContain('Linked to 1 unresolved finding reported by the server')
  const button = [...item.querySelectorAll('button')].find(b => b.textContent === 'Open 1.1.1 item')
  await act(async () => button.click())
  expect(onOpenItem).toHaveBeenCalledWith(101, 'status-check')
  // Units stated side by side, never summed.
  expect(container.textContent).toContain('5 review tasks · 1 unresolved finding reported by the server. Findings and review tasks are different units')
  // Progress shares one denominator and prints its definition.
  const progress = container.querySelector('[aria-label="Review task progress"]').textContent
  expect(progress).toContain('4 of 5 tasks have a recorded decision')
  expect(progress).toContain('3 of 5 tasks have a final outcome · 1 awaiting outcome · 1 without a decision')
  expect(progress).toMatch(/Final outcome = confirmed by a fresh scan/)
  expect(container.textContent).not.toMatch(/certif/i)
})

it('lists a server finding with no task, and a truncated remainder, instead of dropping them', async () => {
  const explanation = explainReviewPopulation({ rows: rows.filter(r => r.id !== 101 && r.id !== 102), automatic: true,
    unresolvedFindings: [{ finding_id: 'fnd-242', file: FILE, rule_id: '2.4.2', rule_name: null, disposition: 'unchanged_no_fix', review_item_id: null }],
    unresolvedFindingsTruncated: true, findingTotal: 3 })
  expect(explanation.allClear).toBe(false)
  const { container } = await mount(explanation)
  const li = container.querySelector('li[data-finding-id="fnd-242"]')
  expect(li.textContent).toContain('2.4.2 Missing a page or document title')
  expect(li.textContent).toContain('no review task in this queue is linked to it')
  expect(li.querySelector('button')).toBeNull()   // nothing to open — and the text says so
  expect(container.textContent).toContain('The server itemised only the first 1 unresolved finding; 2 more are not listed here.')
})

it('opens the linked Results task when the ledger and the queue disagree', async () => {
  const replaced = rows.map(r => r.id === 101 ? { ...r, _raw: { ...r._raw, superseded: true, superseded_reason: 'target_removed_by_verified_fix', superseded_evidence: { removed_by_rule_id: '1.4.5' } } }
    : r.id === 102 ? { ...r, validated: true } : r)
  const explanation = explainReviewPopulation({ rows: replaced, automatic: true, unresolvedFindings: [finding], findingTotal: 1 })
  const { container, onOpenItem } = await mount(explanation)
  const button = [...container.querySelectorAll('button')].find(b => b.textContent === 'Open 1.1.1 item')
  await act(async () => button.click())
  expect(onOpenItem).toHaveBeenCalledWith(101, 'results')
})

it('renders nothing when the explanation is all clear', async () => {
  const settled = rows.map(r => r.id === 102 ? { ...r, validated: true } : r).filter(r => r.id !== 101)
  const explanation = explainReviewPopulation({ rows: settled, automatic: true, unresolvedFindings: [], findingTotal: 0 })
  expect(explanation.allClear).toBe(true)
  const { container } = await mount(explanation)
  expect(container.textContent).toBe('')
})

it('with unknown finding totals, says they are unavailable instead of rendering an empty list that reads as clear', async () => {
  const settled = rows.map(r => r.id === 102 ? { ...r, validated: true } : r).filter(r => r.id !== 101)
  const explanation = explainReviewPopulation({ rows: settled, automatic: true })   // no domain at all
  expect(explanation.allClear).toBe(false)
  for (const showHeadline of [true, false]) {
    const { root, container } = createTestRoot()
    await act(async () => root.render(createElement(RemainingFindingsExplainer, { explanation, onOpenItem: () => {}, showHeadline })))
    expect(container.textContent).not.toMatch(/All clear/)
    expect(container.querySelector('.remaining-findings-explainer__units').textContent)
      .toContain('4 review tasks · current finding totals unavailable')
    expect(container.querySelectorAll('.remaining-findings-explainer__list')).toHaveLength(0)
    expect(!!container.querySelector('.remaining-findings-explainer__headline')).toBe(showHeadline)
    if (showHeadline) expect(container.textContent).toContain('No open review tasks; current finding totals unavailable.')
  }
})

it('with an inconsistent ledger, keeps the subset count but says the totals are inconsistent', async () => {
  const settled = rows.map(r => r.id === 102 ? { ...r, validated: true } : r).filter(r => r.id !== 101)
  const explanation = explainReviewPopulation({ rows: settled, automatic: true, ...findingInputsFrom({ available: true, total: 9,
    buckets: { resolved_verified: 3, superseded: 1 }, unresolved_findings: [], unresolved_findings_total: 0, unresolved_findings_truncated: false }) })
  expect(explanation.allClear).toBe(false)
  const { container } = await mount(explanation)
  expect(container.textContent).not.toMatch(/All clear/)
  expect(container.querySelector('.remaining-findings-explainer__units').textContent)
    .toContain('4 review tasks · 0 unresolved findings reported by the server · current finding totals are inconsistent')
  expect(container.textContent).toContain('No open review tasks; current finding totals are inconsistent, so ACP cannot confirm nothing remains.')
})

it('omits its headline when the host already prints it as the lead line, keeping the item list', async () => {
  const explanation = explainReviewPopulation({ rows, automatic: true, unresolvedFindings: [finding], findingTotal: 1 })
  const { root, container } = createTestRoot()
  await act(async () => root.render(createElement(RemainingFindingsExplainer, { explanation, onOpenItem: () => {}, showHeadline: false })))
  expect(container.querySelector('.remaining-findings-explainer__headline')).toBeNull()
  expect(container.textContent).not.toContain(explanation.headline)
  expect(container.querySelector('li[data-item-id="101"]')).not.toBeNull()
  // Default stays on, so a host without its own lead line still gets the sentence.
  await act(async () => root.render(createElement(RemainingFindingsExplainer, { explanation, onOpenItem: () => {} })))
  expect(container.querySelector('.remaining-findings-explainer__headline').textContent).toBe(explanation.headline)
})
