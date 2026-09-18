// C8 DOM → API regression on the REAL Remediate page. A run that finished before target
// reconciliation existed is corrected only when someone asks, so the ask has to be a visible
// control that reaches the owner-scoped endpoint and then re-reads what it changed. The queue
// reader here is the real useReviewQueueRefresh → api.listHitlQueue → fetch; only fetch is stubbed.
import { act, createElement } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import Remediate from './Remediate.jsx'
import { setGoogleToken } from './api.js'
import { STAGE_LINEAGE_REFRESH_EVENT } from './useCanonicalStageLineage.js'

const SCAN = 'scan-c8'
const BATCH = 'batch-c8'
const FILE = 'synthetic-leaflet.docx'
const POLICY = { enabled: true, supported: true, run_id: BATCH, source_revision: 'src', revision: 1 }
vi.mock('./sim.js', async actual => ({ ...(await actual()), SIM: false }))
vi.mock('./useRunAiApproval.js', () => ({ default: () => ({ enabled: true, policy: POLICY, saving: false, error: '',
  explanation: '', notice: null, dismissNotice: () => {}, change: undefined, retry: () => {} }) }))
globalThis.IS_REACT_ACT_ENVIRONMENT = true
globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }

const base = { scan_id: SCAN, file: FILE, decision_version: 1, source_revision: 'src' }
const verified = { ...base, id: 'item-145', rule_id: '1.4.5', rule_name: 'Images of text', status: 'approved', applied: 1, validated: true,
  approved_value: 'Opening hours: 9 to 5', proposals: [{ locator: 'image 1', proposed_value: 'Opening hours: 9 to 5', source: 'OCR' }] }
const pending = { ...base, id: 'item-111', rule_id: '1.1.1', rule_name: 'Images have alt text', status: 'pending', applied: null, validated: false,
  proposal_snapshot_ids: ['snap-111'], proposals: [{ locator: 'image 1', proposed_value: 'A synthetic clock', source: 'AI', model: 'vision' }],
  automatic_approval: { state: 'review_required', responsibility: 'human', reason: 'A person must confirm the description.',
    scan_id: SCAN, run_id: BATCH, source_revision: 'src', proposal_snapshot_ids: ['snap-111'] } }
const replaced = { ...pending, superseded: true, superseded_reason: 'target_removed_by_verified_fix',
  superseded_evidence: { removed_by_item_id: 'item-145', removed_by_rule_id: '1.4.5', targets: ['image 1'], finding_ids: ['f-111'],
    corrected_artifact_sha256: 'd'.repeat(64), source_artifact_sha256: null, verified_at: '2026-09-17T10:00:00Z',
    assessment: 'release.corrected_copy_assessed', assessment_status: 'analysed', skipped_rules: 0 } }

let calls, reconciled, reconcileResponse, closes
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
beforeEach(() => {
  calls = []; reconciled = false; closes = true
  reconcileResponse = () => json({ scan_id: SCAN, superseded_count: 1,
    files: [{ file: FILE, superseded: ['item-111'], unchanged: [], skipped: [] }] })
  setGoogleToken('synthetic-token')
  vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
    const u = String(url)
    calls.push({ url: u, method: options.method || 'GET', headers: options.headers || {}, cache: options.cache })
    if (u.includes(`/hitl/queue/${SCAN}/reconcile-targets`)) { const r = reconcileResponse(); if (r.ok && closes) reconciled = true; return r }
    if (u.includes(`/hitl/queue?scan_id=${SCAN}`)) return json(reconciled ? [verified, replaced] : [verified, pending])
    return json({})
  }))
})
afterEach(() => { unmountAll(); vi.unstubAllGlobals(); setGoogleToken(null) })

async function mount(props = {}) {
  history.replaceState({}, '', '/?tab=remediate&mode=review')
  const view = createTestRoot()
  await act(async () => view.root.render(createElement(Remediate, { run: { id: SCAN, status: 'completed' },
    files: [{ file: FILE, type: 'DOCX', remediated_at: 'now', issues: [] }], ...props })))
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  return view.container
}
const action = c => [...c.querySelectorAll('button')].find(b => b.textContent === 'Re-check remaining items against the saved copy')
const queueReads = () => calls.filter(call => call.url.includes(`/hitl/queue?scan_id=${SCAN}`)).length

it('re-checks against the saved copy through the owner-scoped endpoint, then re-reads the queue and the stage', async () => {
  const lineageRequests = []
  const onLineage = event => lineageRequests.push(event.detail)
  window.addEventListener(STAGE_LINEAGE_REFRESH_EVENT, onLineage)
  try {
    const c = await mount()
    expect(c.querySelector('[aria-label="Remaining findings and tasks"] [data-item-id="item-111"]')).not.toBeNull()
    const button = action(c)
    expect(button, 'the explicit action is on the live page').toBeTruthy()
    expect(c.textContent).toContain('Uses the recorded check of the saved copy. No AI calls and no change to the document.')
    const readsBefore = queueReads()
    await act(async () => { button.click() })
    await act(async () => { await new Promise(r => setTimeout(r, 30)) })
    const posts = calls.filter(call => call.url.endsWith(`/hitl/queue/${SCAN}/reconcile-targets`))
    expect(posts).toHaveLength(1)
    expect(posts[0].method).toBe('POST')
    expect(posts[0].headers.Authorization).toBe('Bearer synthetic-token')
    expect(posts[0].cache).toBe('no-store')
    expect(c.querySelector('.reconcile-review-targets [role="status"]')?.textContent)
      .toBe('1 item closed: another verified change replaced its target.')
    // The hitl-changed refresh ran the real queue reader again, and the stage was asked to re-read.
    expect(queueReads()).toBeGreaterThan(readsBefore)
    expect(lineageRequests).toEqual([{ scanId: SCAN }])
    // …and the page now shows the server's answer: nothing left open, so the action is gone while
    // its result stays readable.
    expect(c.querySelector('[aria-label="Remaining findings and tasks"] [data-item-id="item-111"]')).toBeNull()
    expect(action(c)).toBeUndefined()
    expect(c.querySelector('.reconcile-review-targets [role="status"]').textContent).toContain('1 item closed')
  } finally { window.removeEventListener(STAGE_LINEAGE_REFRESH_EVENT, onLineage) }
})

it('reports skipped items in plain language and a failed request without claiming anything', async () => {
  closes = false
  reconcileResponse = () => json({ scan_id: SCAN, superseded_count: 0,
    files: [{ file: FILE, superseded: [], unchanged: [], skipped: [{ item_id: 'item-111', reason: 'target_remains' }] }] })
  const c = await mount()
  await act(async () => { action(c).click() })
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
  expect(c.querySelector('.reconcile-review-targets [role="status"]').textContent)
    .toBe('Nothing changed: its target is still in the saved copy.')
  reconcileResponse = () => json({ detail: 'temporarily unavailable' }, 503)
  await act(async () => { action(c).click() })
  await act(async () => { await new Promise(r => setTimeout(r, 30)) })
  const alert = c.querySelector('.reconcile-review-targets [role="alert"]')
  expect(alert.textContent).toContain('The re-check did not complete')
  expect(alert.textContent).not.toMatch(/closed|Nothing changed/)
  expect(c.querySelector('.reconcile-review-targets [role="status"]')).toBeNull()
})

it('is not offered on a read-only (historical) scan', async () => {
  const c = await mount({ readOnly: true })
  expect(action(c)).toBeUndefined()
  expect(calls.some(call => call.url.includes('reconcile-targets'))).toBe(false)
})
