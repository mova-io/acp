import { act, createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { createTestRoot, unmountAll } from './testRoots.js'
import { autoFixRows } from './remediationInboxModel.js'
import { automaticReviewQueue } from './automaticReviewQueue.js'
import { explainReviewPopulation, findingInputsFrom, progressLabelsOf, FINDING_NOTE } from './reviewPopulationExplanation.js'
import { reviewLeadLine } from './reviewQueueCopy.js'
import ReviewQueueTabs from './ReviewQueueTabs.jsx'

// Synthetic reproduction of scan b3eba56d4d5d (DOCX, one body image). No customer content: the
// file name is the shape label existing tests already use, and every value below is invented.
//
// Production screen: Unresolved findings 1 · Review workspace 0 · Needs your review 0 · "All clear —
// nothing needs your review" · 4 of 5 reviewed vs 3 of 5 actions complete / 2 awaiting outcome ·
// pills Needs your input 0 / Processing 0 / Status checks 2 / Results 3.

afterEach(unmountAll)

const FILE = 'UTSW_Discharge_Summary.docx'
const SCAN = 'scan-synthetic'
const policy = { enabled: true, supported: true, run_id: 'run-1', source_revision: 'rev-1' }

// The hitl rows, in the shape Remediate's dbItemToUi produces (row fields + `_raw`).
const altTextRaw = { id: 101, scan_id: SCAN, file: FILE, rule_id: 'SC_1_1_1', rule_name: 'Non-text Content', status: 'pending',
  proposals: [{ proposed_value: 'Synthetic chart of weekly totals', locator: 'docx:drawing:1:paragraph:32' }],
  proposal_snapshot_ids: ['snap-111'], source_revision: 'rev-1', decision_version: 1 }
const altText = (raw = altTextRaw) => ({ id: 101, _raw: raw, file: FILE, scanId: SCAN, ruleId: raw.rule_id, rule_id: raw.rule_id,
  title: 'DOCX · Non-text Content', status: undefined, validated: false, applied: false, aiDraftable: true,
  hasProposal: true, after: 'Synthetic chart of weekly totals', proposals: raw.proposals })
const imageTextRaw = { id: 102, scan_id: SCAN, file: FILE, rule_id: 'SC_1_4_5', rule_name: 'Images of Text', status: 'approved',
  applied: 1, proposals: [{ proposed_value: 'Weekly totals: 12, 14, 9', locator: 'docx:drawing:1:paragraph:32' }] }
const imageText = (validated) => ({ id: 102, _raw: { ...imageTextRaw, validated }, file: FILE, scanId: SCAN, ruleId: 'SC_1_4_5',
  rule_id: 'SC_1_4_5', title: 'DOCX · Images of Text', status: 'approved', validated, applied: true, aiDraftable: false,
  hasProposal: true, after: 'Weekly totals: 12, 14, 9', proposals: imageTextRaw.proposals })
// Applied-change evidence rows (remediation diffs), all confirmed by the fresh assessment.
const applied = autoFixRows([
  { file: FILE, sc: '1.4.3', before: '#999999', after: '#333333', verified: true },
  { file: FILE, sc: '3.1.1', before: '', after: 'en-US', verified: true },
  { file: FILE, sc: '1.4.5', before: 'image', after: 'Weekly totals: 12, 14, 9', verified: true },
], (sc) => sc)

const superseded = { ...altTextRaw, superseded: true, superseded_reason: 'target_removed_by_verified_fix',
  superseded_evidence: { removed_by_item_id: '102', removed_by_rule_id: '1.4.5', targets: ['docx:drawing:1:paragraph:32'],
    finding_ids: ['fnd-111'], corrected_artifact_sha256: 'c'.repeat(64), source_artifact_sha256: 's'.repeat(64),
    verified_at: '2026-09-17T00:00:00Z', assessment: 'release.corrected_copy_assessed', assessment_status: 'analysed', skipped_rules: 0 } }

const queue = (rows) => automaticReviewQueue(rows, policy, {})
const PRE_FIX = queue([altText(), imageText(false), ...applied])
const POST_FIX = queue([altText(superseded), imageText(true), ...applied])

const serverFinding = { finding_id: 'fnd-111', file: FILE, rule_id: 'SC_1_1_1', rule_name: 'Non-text Content',
  disposition: 'awaiting_review', review_item_id: 101 }
const domainWith = (unresolved, buckets) => ({ available: true, total: 4, buckets, unresolved_findings: unresolved, unresolved_findings_truncated: false })
const PRE_DOMAIN = domainWith([serverFinding], { resolved_verified: 3, awaiting_review: 1 })
const POST_DOMAIN = domainWith([], { resolved_verified: 3, superseded: 1 })

const explain = (rows, domain, extra = {}) => explainReviewPopulation({ rows, decisions: {}, automatic: true, ...findingInputsFrom(domain), ...extra })

describe('the production population, before the target-replacement fix', () => {
  const e = explain(PRE_FIX, PRE_DOMAIN)

  it('matches the pills exactly and withholds All clear while status checks and an unresolved finding remain', () => {
    expect([e.humanCount, e.processingCount, e.statusCheckCount, e.resultsCount]).toEqual([0, 0, 2, 3])
    expect(e.taskTotal).toBe(5)
    expect(e.findingTotal).toBe(1)
    expect(e.allClear).toBe(false)
    expect(e.headline).not.toMatch(/All clear/)
    expect(e.headline).toContain('Nothing needs your decision. 2 items are status checks ACP is tracking — see Status checks.')
    expect(reviewLeadLine([], 0, e)).toBe(e.headline)
  })

  it('names the remaining 1.1.1 item with its reason, next action, tab, and the server finding it carries', () => {
    const item = e.remaining.find(r => r.criterion === '1.1.1')
    expect(item).toMatchObject({ itemId: 101, file: FILE, name: 'Non-text Content', tab: 'status-check', queueTab: 'status-check',
      tabLabel: 'Status checks', findingIds: ['fnd-111'] })
    expect(item.reason.length).toBeGreaterThan(10)
    expect(item.nextAction.length).toBeGreaterThan(5)
    // Every server finding is accounted for by an open task — nothing unmatched, nothing dropped.
    expect(e.unmatchedFindings).toEqual([])
    expect(e.unlistedFindings).toBeNull()
    expect(e.remaining.map(r => r.criterion).sort()).toEqual(['1.1.1', '1.4.5'])
  })

  it('keeps findings and tasks as separate, non-additive units', () => {
    expect(e.findingNote).toBe(FINDING_NOTE)
    expect(e.findingNote).toMatch(/never added together/)
    // The one finding is carried by a task already counted; no figure anywhere is 5 + 1.
    expect(JSON.stringify(e)).not.toContain('"6')
  })

  it('reports progress over one denominator, with saved-but-unverified work awaiting its outcome', () => {
    expect(e.progress).toMatchObject({ total: 5, decided: 4, finished: 3, awaitingOutcome: 1, open: 1 })
    expect(e.progress.finished + e.progress.awaitingOutcome + e.progress.open).toBe(e.progress.total)
    expect(e.progress.decidedLabel).toBe('4 of 5 tasks have a recorded decision')
    expect(e.progress.finishedLabel).toBe('3 of 5 tasks have a final outcome · 1 awaiting outcome · 1 without a decision')
    expect(e.progress.definition).toMatch(/Same tasks in both counts/)
  })

  it('an empty filtered view describes the filter, never the whole queue', () => {
    const line = e.emptyFilterLine('review')
    expect(line).toBe('No tasks in Needs your input. This is a filtered view — 2 status checks still remain in other tabs.')
    expect(line).not.toMatch(/All clear|nothing needs your review/i)
    expect(e.emptyFilterLine('awaiting-validation')).toMatch(/^No tasks in Processing\. This is a filtered view/)
    expect(e.emptyFilterLine('CRITICAL')).toMatch(/^No tasks match this filter\. This is a filtered view/)
    expect(e.emptyFilterLine('status-check')).toBeNull()   // not empty — nothing for this line to say
    expect(e.emptyFilterLine(null)).toBeNull()
  })

  it('the pills rendered by ReviewQueueTabs are the explanation’s counts', async () => {
    const { root, container } = createTestRoot()
    await act(async () => root.render(createElement(ReviewQueueTabs, { queue: PRE_FIX, decisions: {}, scanId: SCAN, value: 'review', onChange: () => {}, automatic: true })))
    const pills = [...container.querySelectorAll('.review-queue-pill')].map(b => [b.querySelector('.review-queue-label').textContent, Number(b.querySelector('strong').textContent)])
    expect(pills).toEqual([['Needs your input', e.humanCount], ['Processing', e.processingCount], ['Status checks', e.statusCheckCount], ['Results', e.resultsCount]])
  })
})

describe('the production population, after the target-replacement fix', () => {
  it('is all clear only once the server also reports no unresolved finding', () => {
    const e = explain(POST_FIX, POST_DOMAIN)
    expect([e.humanCount, e.processingCount, e.statusCheckCount, e.resultsCount]).toEqual([0, 0, 0, 5])
    expect(e.remaining).toEqual([])
    expect(e.progress).toMatchObject({ total: 5, finished: 5, awaitingOutcome: 0, open: 0 })
    expect(e.allClear).toBe(true)
    expect(e.headline).toBe('All clear — nothing needs your review.')
    expect(e.emptyFilterLine('review')).toBe('No tasks in Needs your input. Nothing else remains in this queue.')
  })

  it('a server ledger that still lists 1.1.1 is shown as a disagreement, opens the task, and blocks All clear', () => {
    const e = explain(POST_FIX, PRE_DOMAIN)
    expect(e.allClear).toBe(false)
    expect(e.unmatchedFindings).toHaveLength(1)
    expect(e.unmatchedFindings[0]).toMatchObject({ findingId: 'fnd-111', criterion: '1.1.1', name: 'Non-text Content',
      disposition: 'awaiting_review', matchedItemId: 101, matchedTab: 'results' })
    expect(e.unmatchedFindings[0].reason).toMatch(/disagree/)
    expect(e.headline).toMatch(/still reports 1 unresolved finding that no open task covers/)
    expect(reviewLeadLine([], 0, e)).not.toMatch(/All clear/)
  })
})

describe('allClear gating — every reason to withhold it', () => {
  const clear = { rows: POST_FIX, decisions: {}, automatic: true, unresolvedFindings: [], findingTotal: 0 }
  const cases = {
    'a status check': { rows: [...POST_FIX.slice(1), ...queue([altText()])] },
    'a saved change awaiting verification': { rows: [...POST_FIX.filter(r => r.id !== 102), ...queue([imageText(false)]).map(r => ({ ...r, automaticDisposition: null }))] },
    'an unresolved finding with no task': { unresolvedFindings: [{ finding_id: 'fnd-242', file: FILE, rule_id: '2.4.2', disposition: 'unchanged_no_fix', review_item_id: null }], findingTotal: 1 },
    'a server count with no list': { unresolvedFindings: null, findingTotal: 2 },
    'a truncated list': { unresolvedFindings: [serverFinding], unresolvedFindingsTruncated: true, findingTotal: null },
    'an unreadable document': { files: [{ file: 'b.docx', status: 'error' }] },
  }
  it('is true for the settled baseline', () => {
    expect(explainReviewPopulation(clear).allClear).toBe(true)
  })
  for (const [name, change] of Object.entries(cases)) {
    it(`is false with ${name}`, () => {
      const e = explainReviewPopulation({ ...clear, ...change })
      expect(e.allClear).toBe(false)
      expect(e.headline).not.toMatch(/All clear/)
    })
  }

  it('lists an unmatched server finding honestly rather than dropping it', () => {
    const e = explainReviewPopulation({ ...clear, ...cases['an unresolved finding with no task'] })
    expect(e.unmatchedFindings[0]).toMatchObject({ findingId: 'fnd-242', criterion: '2.4.2', disposition: 'unchanged_no_fix' })
    expect(e.unmatchedFindings[0].reason).toMatch(/no review task in this queue is linked to it/)
    expect(e.unmatchedFindings[0].matchedItemId).toBeUndefined()
  })

  it('says how many findings a truncated or missing list leaves unnamed', () => {
    const truncated = explainReviewPopulation({ ...clear, unresolvedFindings: [serverFinding], unresolvedFindingsTruncated: true, findingTotal: 3 })
    expect(truncated.unlistedFindings).toMatchObject({ count: 2, reason: 'truncated' })
    expect(truncated.unlistedFindings.text).toMatch(/itemised only the first 1 unresolved finding; 2 more are not listed/)
    const missing = explainReviewPopulation({ ...clear, unresolvedFindings: null, findingTotal: 2 })
    expect(missing.unlistedFindings).toMatchObject({ count: 2, reason: 'not-provided' })
    expect(missing.headline).toContain('did not itemise them')
  })

  it('a human task makes the headline ask for the decision, and the Review workspace population agrees', () => {
    const human = queue([{ ...altText(), automaticDisposition: undefined, manual: true }])
    const e = explainReviewPopulation({ ...clear, rows: [...POST_FIX.slice(1), ...human] })
    expect(e.humanCount).toBe(1)
    expect(e.headline).toMatch(/^1 task needs your input — see Needs your input\./)
    expect(reviewLeadLine([], e.humanCount, e)).toBeNull()   // the populated state keeps its own count sentence
  })
})

describe('the server list total (domain_reconciliation.unresolved_findings_total)', () => {
  const base = { rows: POST_FIX, decisions: {}, automatic: true }
  it('is read from the snapshot next to the truncation flag', () => {
    expect(findingInputsFrom({ ...PRE_DOMAIN, unresolved_findings_truncated: true, unresolved_findings_total: 5 }))
      .toMatchObject({ unresolvedFindings: [serverFinding], unresolvedFindingsTruncated: true, unresolvedFindingsTotal: 5, findingTotal: 1 })
    expect(findingInputsFrom(PRE_DOMAIN).unresolvedFindingsTotal).toBeNull()
  })
  it('counts exactly how many a capped list leaves unnamed', () => {
    const e = explainReviewPopulation({ ...base, ...findingInputsFrom({ ...PRE_DOMAIN, unresolved_findings_truncated: true, unresolved_findings_total: 5 }) })
    expect(e.unlistedFindings).toMatchObject({ count: 4, reason: 'truncated' })
    expect(e.allClear).toBe(false)
  })
  it('an uncapped list shorter than its total is a mismatch, not all clear', () => {
    const e = explainReviewPopulation({ ...base, unresolvedFindings: [], unresolvedFindingsTotal: 2, findingTotal: 0 })
    expect(e.unlistedFindings).toMatchObject({ count: 2, reason: 'count-mismatch' })
    expect(e.allClear).toBe(false)
    expect(e.headline).not.toMatch(/All clear/)
  })
  it('a zero total with an empty list stays all clear', () => {
    expect(explainReviewPopulation({ ...base, unresolvedFindings: [], unresolvedFindingsTotal: 0, findingTotal: 0 }).allClear).toBe(true)
  })
})

// All clear needs KNOWN-zero finding evidence. Every case below has a terminal-only queue (all five
// tasks in Results), so the only open question is whether the server's finding totals are known.
describe('unknown finding totals never read as All clear', () => {
  const settled = { rows: POST_FIX, decisions: {}, automatic: true }
  const UNKNOWN = 'No open review tasks; current finding totals unavailable.'
  const unknownCases = {
    'a null domain': findingInputsFrom(null),
    'a missing list and no count': findingInputsFrom({ available: true, total: 4 }),
    'unbalanced buckets with an empty list': findingInputsFrom({ available: true, total: 9, buckets: { resolved_verified: 3, superseded: 1 }, unresolved_findings: [] }),
  }
  for (const [name, inputs] of Object.entries(unknownCases)) {
    it(`withholds All clear for ${name}`, () => {
      expect(inputs.findingTotal).toBeNull()
      const e = explainReviewPopulation({ ...settled, ...inputs })
      expect([e.humanCount, e.processingCount, e.statusCheckCount, e.remaining.length]).toEqual([0, 0, 0, 0])
      expect(e.findingTotalsKnown).toBe(false)
      expect(e.allClear).toBe(false)
      expect(e.findingTotal).toBeNull()        // an empty list is not reported as "0 findings"
      expect(e.headline).toBe(UNKNOWN)
      expect(reviewLeadLine([], 0, e)).toBe(UNKNOWN)
      expect(e.emptyFilterLine('review')).toBe('No tasks in Needs your input. No other open review tasks; current finding totals unavailable.')
    })
  }
  it('still says All clear on known-zero evidence (balanced tile at 0, or an uncapped list total of 0)', () => {
    const balanced = explainReviewPopulation({ ...settled, ...findingInputsFrom(POST_DOMAIN) })
    expect(balanced.findingTotalsKnown).toBe(true)
    expect(balanced.allClear).toBe(true)
    expect(balanced.headline).toBe('All clear — nothing needs your review.')
    const byListTotal = explainReviewPopulation({ ...settled, unresolvedFindings: [], unresolvedFindingsTotal: 0 })
    expect(byListTotal.allClear).toBe(true)
    // A balanced tile at 0 is itself known-zero evidence, list or no list.
    const tileOnly = explainReviewPopulation({ ...settled, ...findingInputsFrom({ ...POST_DOMAIN, unresolved_findings: undefined }) })
    expect(tileOnly.allClear).toBe(true)
  })
  it('adds the unavailable sentence when tasks remain and totals are unknown', () => {
    const e = explainReviewPopulation({ rows: PRE_FIX, decisions: {}, automatic: true })
    expect(e.allClear).toBe(false)
    expect(e.headline).toBe('Nothing needs your decision. 2 items are status checks ACP is tracking — see Status checks. Current finding totals are unavailable.')
  })
})

describe('progress labels', () => {
  it('share one denominator and read correctly in the singular', () => {
    const p = progressLabelsOf({ total: 1, decided: 1, finished: 0, awaitingOutcome: 1, open: 0 })
    expect(p.decidedLabel).toBe('1 of 1 task has a recorded decision')
    expect(p.finishedLabel).toBe('0 of 1 task has a final outcome · 1 awaiting outcome · 0 without a decision')
  })
})

it('makes no certification or legal-compliance claim anywhere in its wording', () => {
  for (const e of [explain(PRE_FIX, PRE_DOMAIN), explain(POST_FIX, POST_DOMAIN), explain(POST_FIX, PRE_DOMAIN)]) {
    const text = JSON.stringify({ ...e, emptyFilterLine: [e.emptyFilterLine('review'), e.emptyFilterLine('x')] })
    expect(text).not.toMatch(/certif|legally|compliant|conformance/i)
  }
})

// Integration guard for Agent D's wiring (finding/review reconciliation). Red until Remediate passes
// the explanation to reviewLeadLine — without it the lead keeps the old human-only "All clear" gate.
it('Remediate passes the population explanation to the lead line', () => {
  const page = readFileSync('src/Remediate.jsx', 'utf8')
  expect(page).toMatch(/reviewLeadLine\(\s*files\s*,[^,)]+,\s*\w+/)
})
