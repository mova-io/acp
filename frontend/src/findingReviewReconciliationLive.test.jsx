// A production case, reproduced with SYNTHETIC rows on the real
// Remediate page (not the inbox alone), so the wiring — dedupe, explanation, banner, wording and
// open-item selection — is exercised exactly as the page composes it.
//
// Production (DOCX, one body image): 1.1.1 PENDING with an AI alt-text proposal for that image;
// 1.4.5 APPROVED + applied, whose OCR text replacement removed the image; 1.4.3 and 3.1.1 verified.
// The screen said "All clear — nothing needs your review" beside "Unresolved findings 1", put a blue
// "Status check: …not been admitted to automatic application" above a VERIFIED 1.4.3 item, printed
// "Written → Re-scan → Certified", repeated the saved result as "Current"/"Proposed", and counted
// progress over two denominators. Nothing named the remaining finding or opened it.
import { act, createElement, useEffect } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import Remediate from './Remediate.jsx'

const SCAN = 'scan-frr'
const BATCH = 'batch-frr'
const FILE = 'synthetic-summary.docx'
// The real writer's locator for a DOCX body image — note the space.
const IMAGE = 'image 1'
const POLICY = { enabled: true, supported: true, run_id: BATCH, source_revision: 'src', revision: 1 }

let rows = []
let diffs = []
vi.mock('./sim.js', async actual => ({ ...(await actual()), SIM: false }))
vi.mock('./useReviewQueueRefresh.js', () => ({ default: ({ onRows }) => {
  useEffect(() => { onRows(rows) }, [])
  return vi.fn()
} }))
// Automatic approval is ON for this run — the condition under which the verified row was captioned
// as an unadmitted status check.
vi.mock('./useRunAiApproval.js', () => ({ default: () => ({ enabled: true, policy: POLICY, saving: false, error: '',
  explanation: '', notice: null, dismissNotice: () => {}, change: undefined, retry: () => {} }) }))
vi.mock('./api.js', async actual => ({ ...(await actual()),
  getAppliedFixes: vi.fn(async () => []), getScanRemediationDiffs: vi.fn(async () => diffs),
  getHitlAnalytics: vi.fn(async () => ({})), getScanAiCalls: vi.fn(async () => []),
  getRemediationExceptions: vi.fn(async () => ({})), getRemediationStatus: vi.fn(async () => ({})),
}))
afterEach(() => { unmountAll(); vi.unstubAllGlobals() })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
// The live-activity canvas (mounted once a run snapshot exists) measures itself; jsdom has no observer.
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }

const base = { scan_id: SCAN, file: FILE, decision_version: 1, source_revision: 'src' }
// 1.4.3 — verified, automatic policy on, and NO admission marker (the row that got the banner).
const contrast = { ...base, id: 'item-143', rule_id: '1.4.3', rule_name: 'Text contrast', status: 'approved', applied: 1, validated: true,
  approved_value: '#595959', proposals: [{ locator: 'docx:run:4', before: '#9A9A9A', proposed_value: '#595959', approved_value: '#595959', source: 'deterministic' }] }
// 1.4.5 — approved, applied, validated: the OCR text replacement that removed the image.
const imagesOfText = { ...base, id: 'item-145', rule_id: '1.4.5', rule_name: 'Images of text', status: 'approved', applied: 1, validated: true,
  approved_value: 'Summary checklist: rest, fluids, follow-up in 7 days',
  proposals: [{ locator: IMAGE, proposed_value: 'Summary checklist: rest, fluids, follow-up in 7 days', source: 'OCR' }] }
// 3.1.1 — verified.
const language = { ...base, id: 'item-311', rule_id: '3.1.1', rule_name: 'Document language', status: 'approved', applied: 1, validated: true,
  approved_value: 'en-US', proposals: [{ locator: 'docx:settings:lang', before: 'none', proposed_value: 'en-US' }] }
// 1.1.1 — still PENDING (audit status unchanged), its target removed by the verified 1.4.5 fix (C1).
const altPending = { ...base, id: 'item-111', rule_id: '1.1.1', rule_name: 'Images have alt text', status: 'pending', applied: null, validated: false,
  proposal_snapshot_ids: ['snap-111'], proposals: [{ locator: IMAGE, proposed_value: 'A synthetic line chart', source: 'AI', model: 'vision' }] }
const altReplaced = { ...altPending, superseded: true, superseded_reason: 'target_removed_by_verified_fix',
  superseded_evidence: { removed_by_item_id: 'item-145', removed_by_rule_id: '1.4.5', targets: [IMAGE], finding_ids: ['f-111'],
    corrected_artifact_sha256: 'c'.repeat(64), source_artifact_sha256: null, verified_at: '2026-09-17T10:00:00Z',
    assessment: 'release.corrected_copy_assessed', assessment_status: 'analysed', skipped_rules: 0 } }
// Variant: the same 1.1.1, but the server says a PERSON must decide it.
const altHuman = { ...altPending, automatic_approval: { state: 'review_required', responsibility: 'human',
  reason: 'The image description could not be confirmed automatically; a person must check it.',
  scan_id: SCAN, run_id: BATCH, source_revision: 'src', proposal_snapshot_ids: ['snap-111'] } }
// The fifth row: applied-fix evidence that is a proven second representation of item-145. Exactly
// the writer's note shape (api/handlers.py: 'approved by a reviewer · {locator}'), no locator field.
const duplicateDiff = { file: FILE, sc: '1.4.5', before: '[image]', after: imagesOfText.approved_value,
  note: `approved by a reviewer · ${IMAGE}`, verified: true }

const stage = (unresolved) => ({ stage: 'remediate', scan_id: SCAN, domain_reconciliation: {
  total: 4, buckets: unresolved.length ? { resolved_verified: 3, awaiting_review: 1 } : { resolved_verified: 3, superseded: 1 },
  unresolved_findings: unresolved, unresolved_findings_truncated: false } })
const UNRESOLVED_111 = [{ finding_id: 'f-111', file: FILE, rule_id: '1.1.1', rule_name: 'Images have alt text',
  disposition: 'awaiting_review', review_item_id: 'item-111' }]

async function mount({ alt, unresolved = [], runStream = null }) {
  rows = [contrast, imagesOfText, language, alt]
  diffs = [duplicateDiff]
  history.replaceState({}, '', '/?tab=remediate&mode=review')
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })))
  const view = createTestRoot()
  await act(async () => view.root.render(createElement(Remediate, { run: { id: SCAN, status: 'completed' },
    files: [{ file: FILE, type: 'DOCX', remediated_at: 'now', issues: [] }], remediationStage: stage(unresolved), runStream })))
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return view.container
}
const click = el => act(async () => { el.click() })
const pill = (c, label) => [...c.querySelectorAll('[aria-label="Review queues"] button')].find(b => b.textContent.startsWith(label))
const row = (c, id) => c.querySelector(`#rinbox-row-${id}`)
const detail = c => c.querySelector('.remediation-detail')
const settle = () => act(async () => { await new Promise(r => setTimeout(r, 150)) })

describe('finding/review reconciliation on the real Remediate page', () => {
  it('a verified 1.4.3 carries no Status-check banner, reads Original/Corrected once, and claims no certification', async () => {
    const c = await mount({ alt: altReplaced })
    await click(pill(c, 'Results'))
    await click(row(c, 'item-143'))
    expect(row(c, 'item-143').getAttribute('aria-current')).toBe('true')
    // No owner banner of any kind on a verified result.
    expect(c.querySelector('.automatic-review-queued'), c.querySelector('.automatic-review-queued')?.textContent).toBeNull()
    expect(c.textContent).not.toContain('not been admitted to automatic application')
    const pane = detail(c)
    expect(pane.querySelectorAll('[aria-label="Original and corrected values"]')).toHaveLength(1)
    expect(pane.querySelector('[aria-label="Current and proposed values"]')).toBeNull()
    // The evidence panel does not print the same pair a second time.
    expect(pane.querySelector('[aria-label="Recorded before and after"]')).toBeNull()
    expect([...pane.querySelectorAll('b')].filter(b => b.textContent === 'Original')).toHaveLength(1)
    expect([...pane.querySelectorAll('b')].filter(b => b.textContent === 'Corrected')).toHaveLength(1)
    const copies = [...pane.querySelectorAll('.remediation-copy-value')].map(b => b.textContent)
    expect(copies).toEqual(['Copy original', 'Copy corrected'])
    expect(pane.textContent).not.toMatch(/Proposed|Copy current/)
    expect(pane.textContent).toContain('the fresh scan of the corrected copy no longer reports this item')
    expect(pane.textContent).toContain('This is not a certification of the whole document.')
    expect(c.querySelector('.rinbox-wrap').textContent).not.toContain('Certified')
    expect(pane.textContent).not.toContain('Awaiting verification')
    expect(pane.textContent).not.toContain('After approval, ACP will create a corrected copy')
    // The two production "status checks" are results now.
    expect(pill(c, 'Status checks').textContent).toContain('0')
  })

  it('a target-replaced 1.1.1 is a terminal result naming 1.4.5, with no approval or retry offered', async () => {
    const c = await mount({ alt: altReplaced })
    await click(pill(c, 'Results'))
    await click(row(c, 'item-111'))
    const pane = detail(c)
    expect(pane.querySelector('h3').textContent).toBe('Images have alt text')
    expect(pane.textContent).toContain('WCAG 1.4.5')
    expect(pane.textContent).toContain('No alt text was written for it, and no approval is needed.')
    expect(pane.textContent).toContain('Verified on the corrected copy')
    expect(pane.textContent).not.toMatch(/Certified|Proposed/)
    expect(c.querySelector('.automatic-review-queued')).toBeNull()
    const labels = [...pane.querySelectorAll('button')].map(b => b.textContent)
    for (const forbidden of ['Apply this fix', 'Review and apply', 'Retry writing the approved fix', 'Retry verification of saved copy', 'Needs manual work'])
      expect(labels.some(l => l.includes(forbidden)), forbidden).toBe(false)
  })

  it('counts every progress line over ONE deduplicated denominator (5 rows in, 4 tasks)', async () => {
    const c = await mount({ alt: altReplaced })
    const header = c.querySelector('.rem-review-progress').textContent
    const inbox = c.querySelector('.rinbox-progress').textContent
    expect(header).toContain('of 4 tasks')
    expect(inbox).toContain('of 4 tasks')
    expect(`${header} ${inbox}`).not.toMatch(/of 5\b/)
    expect(header).toBe(inbox)
    // The list and the pills read the same deduplicated tasks: the applied-fix row that re-states
    // item-145 is not a fifth Result.
    expect(pill(c, 'Results').querySelector('strong').textContent).toBe('4')
    await click(pill(c, 'Results'))
    expect(c.querySelector('[id^="rinbox-row-af:"]')).toBeNull()
  })

  it('withholds "All clear" while a finding remains, names it and its tab, and opens it', async () => {
    const c = await mount({ alt: altHuman, unresolved: UNRESOLVED_111 })
    expect(c.textContent).not.toContain('All clear')
    const explainer = c.querySelector('[aria-label="Remaining findings and tasks"]')
    expect(explainer, 'remaining-findings panel is mounted').not.toBeNull()
    const entry = explainer.querySelector('[data-item-id="item-111"]')
    expect(entry.textContent).toContain('1.1.1')
    expect(entry.textContent).toContain('Needs your input')
    // Stand somewhere else first: Results, a verified row selected.
    await click(pill(c, 'Results'))
    await click(row(c, 'item-143'))
    expect(row(c, 'item-111')).toBeNull()
    await click([...entry.querySelectorAll('button')].find(b => b.textContent === 'Open 1.1.1 item'))
    await settle()
    expect(row(c, 'item-111')?.getAttribute('aria-current')).toBe('true')
    expect(pill(c, 'Needs your input').getAttribute('aria-pressed')).toBe('true')
    expect(detail(c).querySelector('h3').textContent).toBe('Images have alt text')
    expect(document.activeElement).toBe(row(c, 'item-111'))
    expect(c.querySelector('#rem-panel-review').hidden).toBe(false)
    // A person owns this one, so its banner says so — and the pending proposal keeps Current/Proposed.
    expect(c.querySelector('.automatic-review-queued').textContent).toMatch(/^You: /)
    expect(detail(c).querySelector('[aria-label="Current and proposed values"]')).not.toBeNull()
  })

  it('with a genuine status check left, the Review workspace tile names it and nothing says All clear', async () => {
    // Pre-fix shape: the 1.1.1 proposal lacks its saved version information, so it is a status
    // check ACP is tracking — not a request for a person, and not a clear queue.
    const { source_revision, ...unversioned } = altPending // eslint-disable-line no-unused-vars
    const c = await mount({ alt: unversioned, unresolved: UNRESOLVED_111,
      runStream: { snapshot: { scan_id: SCAN, batch_id: BATCH }, events: [], status: {} } })
    expect(pill(c, 'Status checks').textContent).toContain('1')
    const tile = c.querySelector('.remediation-review-workspace')
    expect(tile, 'Review workspace tile is mounted').not.toBeNull()
    expect(tile.textContent).toContain('+ 1 status check ACP is tracking')
    expect(c.textContent).not.toContain('All clear')
    const entry = c.querySelector('[aria-label="Remaining findings and tasks"] [data-item-id="item-111"]')
    expect(entry.textContent).toContain('Status checks')
  })

  it('opens a requested item that arrives before the page mounts', async () => {
    const { requestOpenReviewItem } = await import('./openReviewItem.js')
    requestOpenReviewItem({ itemId: 'item-311', scanId: SCAN, tab: 'results' })
    const c = await mount({ alt: altHuman, unresolved: UNRESOLVED_111 })
    await settle()
    expect(row(c, 'item-311')?.getAttribute('aria-current')).toBe('true')
    expect(pill(c, 'Results').getAttribute('aria-pressed')).toBe('true')
  })
})
