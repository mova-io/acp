import { it, expect, vi, afterEach } from 'vitest'
import { createElement, act } from 'react'
import { readFileSync } from 'node:fs'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationInbox from './RemediationInbox.jsx'
import BatchReviewSelection from './BatchReviewSelection.jsx'
import { progress, WORKFLOW_LABELS, WORKFLOW_TABS } from './remediationInboxModel.js'
import { remediationReviewCounts } from './remediationCountSummary.js'
import { explainReviewPopulation } from './reviewPopulationExplanation.js'

// The HITL panel's numbers, held to one rule: EVERY count on screen must be reconcilable with the
// list it sits on, by reading. The screen that prompted this had four denominators visible at once —
// a headline over one partition, a progress bar over a session tally, a tab badge over a filtered
// subset of its own list, and an exclusions count over the whole queue including finished work — and
// no two of them added up. Each number was individually true, which is what made it unfixable by
// staring at it: the reviewer's only signal that something was wrong was that it felt wrong.
//
// These tests are about arithmetic between elements, not about any one element's value, so they
// assert relationships (badge === rows in that tab; ledger === tabs; header === pane) rather than
// literals wherever the relationship is the actual claim.

afterEach(unmountAll)

// A run shaped like the reported screenshot: bulk-approvable AI drafts, an AI draft with no
// recorded proposal version (in the needs-review list, NOT bulk-approvable), an auto-applied fix
// awaiting the reviewer's confirmation, manual work, and finished work.
const draft = (id) => ({ id, file: `d-${id}.docx`, title: 'DOCX · Image needs alt text', rule_id: '1.1.1',
  severity: 'SERIOUS', hasProposal: true, after: `alt ${id}`,
  _raw: { corrected_artifact: 'none', proposal_digest: 'digest-test', proposals: [{ proposed_value: `alt ${id}` }], proposal_snapshot_ids: [`s-${id}`],
          source_revision: 'r1', decision_version: 1 } })
// Same lane, no lineage — approvable individually, never in a batch.
const unversioned = (id) => ({ id, file: `u-${id}.docx`, title: 'DOCX · Image needs alt text',
  rule_id: '1.1.1', severity: 'SERIOUS', hasProposal: true, after: `alt ${id}` })
const autoApplied = (id) => ({ id, file: `a-${id}.docx`, title: 'DOCX · Heading contrast too low',
  rule_id: '1.4.3', severity: 'SERIOUS', autoApplied: true, before: '#ccc', after: '#000' })
const manual = (id) => ({ id, file: `m-${id}.pdf`, title: 'PDF · Scanned page, no text', rule_id: '1.1.1', severity: 'SERIOUS' })
const done = (id) => ({ id, file: `c-${id}.docx`, title: 'DOCX · Document has no title', rule_id: '2.4.2', severity: 'MINOR', status: 'verified' })

const RUN = [draft(1), draft(2), draft(3), draft(4), draft(5), unversioned(6), autoApplied(7),
  manual(8), manual(9), manual(10), done(11), done(12), done(13)]

const click = async (el) => act(async () => el.tagName === 'OPTION' ? (el.parentElement.value = el.value, el.parentElement.dispatchEvent(new Event('change', { bubbles: true }))) : el.dispatchEvent(new MouseEvent('click', { bubbles: true })))
async function mount(props) {
  const { root, container } = createTestRoot()
  const render = async (next) => act(async () => root.render(createElement(RemediationInbox,
    { legacyApprovalControls: true, initialTab: 'needs-review', queue: RUN, decisions: {}, scanId: 'fixture', initialGroup: 'document', initialSort: 'document',
      onDecide: vi.fn().mockResolvedValue(undefined), ...props, ...next })))
  await render()
  const tabs = () => [...container.querySelectorAll('select[aria-label="Filter by status"] option:not([value=all])')]
  return {
    container, render, tabs,
    tab: (label) => tabs().find((t) => t.textContent.startsWith(label)),
    // The trailing number a badge renders, or 0 when the badge is blank.
    badge: (label) => Number((tabs().find((t) => t.textContent.startsWith(label))?.textContent.match(/(\d+)\s*$/) || [, 0])[1]),
    rows: () => container.querySelectorAll('.rinbox-row').length,
    button: (name) => [...container.querySelectorAll('button, select[aria-label="Filter by status"] option')].find((b) => b.textContent.includes(name)),
  }
}

// ── Fix 2 · a badge counts the rows its own tab lists ─────────────────────────────────────────
// The reported symptom exactly: "Approve AI suggestions 5" over six rows. The badge was the
// bulk-approvable count (readyAcrossScan) while the list was the workflow partition, so the applied
// fix awaiting confirmation was in the list and in no number anywhere on the screen.

it('every workflow tab badge equals the number of rows that tab lists', async () => {
  const v = await mount()
  for (const t of WORKFLOW_TABS) {
    await click(v.tab(WORKFLOW_LABELS[t]))
    expect([WORKFLOW_LABELS[t], v.badge(WORKFLOW_LABELS[t])]).toEqual([WORKFLOW_LABELS[t], v.rows()])
  }
})

it('counts applied work under awaiting verification, not approval', async () => {
  const v = await mount()
  expect(v.badge('Approve AI suggestions')).toBe(6)
  expect(v.rows()).toBe(6)
  expect(v.badge('Awaiting verification')).toBe(1)
  await click(v.tab('Awaiting verification'))
  expect(v.rows()).toBe(1)
  await v.render({ queue: RUN.filter((f) => !f.autoApplied) })
  expect(v.badge('Awaiting verification')).toBe(0)
  expect(v.rows()).toBe(0)
})

it('still says how many of those are ready to approve — on the button, where the number has a noun', async () => {
  // #1888's separation of applied inspection from approvals is the right distinction; a bare badge
  // over a list is the wrong place to make it, because the number carries no noun there.
  const v = await mount()
  expect(v.button('Approve all ready in this run (5)')).toBeTruthy()
})

// ── Fix 1 · one denominator per question ──────────────────────────────────────────────────────

it('the run-approval ledger adds up to the workflow tabs', async () => {
  const v = await mount()
  const c = remediationReviewCounts(RUN, {})
  const summary = v.container.querySelector('.run-approval-summary').textContent
  expect(summary).toContain('5 ready review items')
  expect(summary).toContain('1 need proposal information or individual review')
  expect(summary).toContain('1 applied changes available to inspect')     // was rendered nowhere before
  expect(summary).toContain('3 manual review items')
  // The ledger's terms are the needs-review and manual tabs, split by what a reviewer can do with
  // them. Reconcilable by addition, which is the whole point of printing them together.
  expect(c.ready + c.individual).toBe(v.badge('Approve AI suggestions'))
  expect(c.inspection).toBe(v.badge('Awaiting verification'))
  expect(c.manual).toBe(v.badge('Fix manually'))
})

it('the header progress and the inbox pane counter are the same two numbers', async () => {
  const decisions = { 1: { state: 'accepted' }, 8: { state: 'rejected' } }
  const v = await mount({ decisions })
  // What the pane prints, and what Remediate's header now derives — the same call on the same pair.
  const p = progress(RUN, decisions)
  expect(p).toEqual({ resolved: 5, total: 13 })    // 3 verified + 2 decided
  // Wording changed deliberately (finding/review reconciliation, the production case): "N of M
  // reviewed" and "N of M actions complete" were two definitions of done over one denominator.
  // Both surfaces now print the SAME explanation's two labels; the decided count is still `p`'s.
  const e = explainReviewPopulation({ rows: RUN, decisions })
  expect(e.progress.decided).toBe(p.resolved)
  expect(e.progress.total).toBe(p.total)
  expect(v.container.textContent).toContain(`${e.progress.decidedLabel} · ${e.progress.finishedLabel}`)
  expect(e.progress.decidedLabel).toBe('5 of 13 tasks have a recorded decision')

  const page = readFileSync('src/Remediate.jsx', 'utf8')
  expect(page).toContain('const reviewProgress = reviewExplanation.progress')
  expect(page).toContain('{reviewProgress.decidedLabel} · {reviewProgress.finishedLabel}')
  expect(page).not.toMatch(/\{reviewProgress\.resolved\} of \{reviewProgress\.total\} reviewed/)
  // The session tally that used to fill this slot answered a different question against a different
  // denominator. It survives in the Advanced block; it must not come back to the review header.
  const header = page.slice(page.indexOf('<h2 style={{ margin: 0 }}>Review queue</h2>'))
    .slice(0, page.slice(page.indexOf('<h2 style={{ margin: 0 }}>Review queue</h2>')).indexOf('rev-analytics'))
  expect(header).not.toContain('totalHitl')
  expect(header).not.toContain('hitlProgress')
})

it('says applied changes are waiting rather than dropping them from the headline', () => {
  const page = readFileSync('src/Remediate.jsx', 'utf8')
  // pendingItems deliberately excludes applied-inspection rows (#1888: 2000 of them would drown the
  // 346 that need a decision), and the nav badge still reads it. The bug was that the excluded rows
  // were then said NOWHERE while sitting in the Needs-review list.
  expect(page).toContain('applied change{reviewCounts.inspection === 1 ? \'\' : \'s\'} to confirm')
  expect(page).toContain('onHitlCount?.(reviewCounts.pendingItems)')
})

it('the not-included count is pending work, not work already finished', async () => {
  const { root, container } = createTestRoot()
  const render = async (props) => act(async () => root.render(createElement(BatchReviewSelection,
    { visible: RUN, decisions: {}, scopeKey: 'scan', onDecide: vi.fn(), ...props })))
  await render()
  // 1 unversioned + 1 applied-to-inspect + 3 manual are pending and not in this batch; the 3
  // verified rows are finished and were never candidates, so they are stated apart from the count.
  const summary = container.querySelector('.batch-review-exclusions summary').textContent
  expect(summary).toBe('5 pending review items not included · 3 already resolved')
  // Both halves stay itemised — the split is about which number leads, not about hiding a reason.
  expect(container.querySelector('.batch-review-exclusions').textContent).toContain('3 already reviewed')

  // Bite check: resolve one of the five pending exclusions and it must MOVE across the split, not
  // vanish from one side or be counted twice.
  await render({ decisions: { 8: { state: 'rejected' } } })
  expect(container.querySelector('.batch-review-exclusions summary').textContent)
    .toBe('4 pending review items not included · 4 already resolved')
})

// ── Fix 5 · leaving the bulk mode cannot destroy a selection in silence ────────────────────────

it('keeps the frozen selection when the category changes, and disarms the confirm step', async () => {
  const v = await mount()
  await click(v.button('Approve all ready in this run (5)'))
  expect(v.container.querySelector('.batch-review').textContent).toContain('5 findings selected')

  // Before: this click reset the batch scopeKey and discarded all five frozen proposals with no
  // prompt, no undo and nothing on screen to say it had happened. The tab is a deliberate way out
  // of the bulk mode — that is kept. What it may not do is take the work with it.
  await click(v.tab('Fix manually'))
  expect(v.container.querySelector('[aria-label="Select findings for approval"]').closest('[hidden]')).toBeTruthy()
  await click(v.tab('Approve AI suggestions'))
  await click(v.button('Bulk approve ready proposals'))
  const batch = v.container.querySelector('.batch-review')
  expect(batch.textContent).toContain('5 findings selected')
  // The stale ARMED CONFIRMATION is still cleared — reopening lands on the selection, never on a
  // confirm step the reviewer did not ask for this time round.
  expect(batch.querySelector('h3').textContent).toBe('Ready to approve')
  expect(v.button('Confirm approval of')).toBeUndefined()
})

it('a real scope change still invalidates the selection', async () => {
  // The bite: the selection survives a tab because the batch scope does not depend on the tab. It
  // must NOT survive something the scope does depend on, or this is a stale-approval bug wearing a
  // usability fix's clothes.
  const { root, container } = createTestRoot()
  const render = async (props) => act(async () => root.render(createElement(BatchReviewSelection,
    { visible: RUN, decisions: {}, scopeKey: 'scan-a', onDecide: vi.fn(), ...props })))
  await render()
  await click([...container.querySelectorAll('button, select[aria-label="Filter by status"] option')].find((b) => b.textContent.includes('Approve all ready')))
  expect(container.textContent).toContain('5 findings selected')
  await render({ scopeKey: 'scan-b' })
  expect(container.textContent).not.toContain('findings selected')
})
