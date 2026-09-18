import { afterEach, beforeEach, expect, it, vi } from 'vitest'

// The review workspace must be able to show a row whose target another verified fix removed
// (a pending 1.1.1 alt-text suggestion for an image a 1.4.5 replacement turned into text). The
// server drops superseded rows unless asked, so the workspace asks — and must then keep ONLY
// that kind: every other superseded row stays out, exactly as the default read leaves it.

beforeEach(() => { vi.resetModules(); vi.stubEnv('VITE_SIM', 'false') })
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals() })
const ok = body => ({ ok: true, status: 200, json: async () => body })

const rows = [
  { id: 'open', status: 'pending', rule_id: '1.1.1' },
  { id: 'replaced', status: 'pending', rule_id: '1.1.1', superseded: true, superseded_reason: 'target_removed_by_verified_fix',
    superseded_evidence: { removed_by_rule_id: '1.4.5', removed_by_item_id: 'r145' } },
  { id: 'reassessed', status: 'pending', rule_id: '2.4.2', superseded: true, superseded_reason: 'criterion_reassessed' },
  { id: 'legacy', status: 'pending', rule_id: '1.3.1', superseded: true },
]

it('asks for superseded rows only when the workspace opts in, and keeps only target-replaced ones', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ok(rows)))
  const { listHitlQueue } = await import('./api.js')
  const items = await listHitlQueue('scan 1', null, { includeTargetReplaced: true })
  expect(fetch.mock.calls[0][0]).toContain('scan_id=scan%201&include_superseded=true')
  expect(items.map(row => row.id)).toEqual(['open', 'replaced'])
})

it('keeps the default read unchanged for every other caller', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ok(rows.filter(row => !row.superseded))))
  const { listHitlQueue } = await import('./api.js')
  const items = await listHitlQueue('scan-1')
  expect(fetch.mock.calls[0][0]).not.toContain('include_superseded')
  expect(items.map(row => row.id)).toEqual(['open'])
})

it('the one queue reader behind the review workspace opts in', async () => {
  const source = (await import('./useReviewQueueRefresh.js?raw')).default
  expect(source).toContain('includeTargetReplaced:true')
})
