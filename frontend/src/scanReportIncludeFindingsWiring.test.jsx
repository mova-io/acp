/**
 * The two LIVE scan-report menus (Overview's Reports menu, Assess's RuleBreakdown header) ask the
 * server for each row's finding records when — and only when — the report prints finding cards,
 * and the report they send to the renderer links each card to its exact record (contract 8).
 *
 * Only api.js is replaced. Its getScanReportFacts answers from the pages
 * tests/test_report_scan_finding_links.py recorded from the real store (with `include=findings`),
 * and — like the real route — leaves the records out when the request did not ask for them.
 * DOM-level: the browser preview serves the shared checkout, not this worktree (CLAUDE.md).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import real from './__fixtures__/reportScanFindings.real.json'

const h = vi.hoisted(() => ({ calls: [], renders: [] }))

vi.mock('./api.js', async (importOriginal) => {
  const orig = await importOriginal()
  const clone = (v) => JSON.parse(JSON.stringify(v))
  return {
    ...orig,
    SIM: false,
    getScanReportFacts: async (sid, opts = {}) => {
      h.calls.push(opts)
      const rows = real.scanPages.flatMap((p) => p.files)
        .map((r) => (opts.includeFindings ? r : (({ findings, remediatedAt, ...rest }) => rest)(r)))
      return { ...clone(real.scanPages[0]), offset: opts.offset || 0, files: clone(rows.slice(opts.offset || 0)), complete: true }
    },
    getScan: async () => null,
    getScanInventory: async () => null,
    // RuleBreakdown renders (and offers its menu) only for a scan with per-rule traces.
    getScanTraces: async (sid) => [{ file: Object.keys(real.files)[0], rule_id: '1.1.1', plain_name: 'Non-text Content', level: 'A', outcome: 'FAIL', finding_count: 2 }],
    listHitlQueue: async () => [],
    getConfig: async () => ({ version: '2026.9.18.1' }),
    getScanRemediationDiffs: async () => ({ items: [], total: 0, documents: 0, complete: true }),
    getTraceStatus: async () => null,
    openTraceUrl: () => null,
    postReportRender: async (sid, body) => {
      h.renders.push(body)
      return { ok: true, status: 200, headers: new Headers(), blob: async () => new Blob(['%PDF-1.7'], { type: 'application/pdf' }) }
    },
  }
})

const { default: Overview } = await import('./Overview.jsx')
const { RuleBreakdown } = await import('./Transparency.jsx')
const { parseEvidenceHref } = await import('./evidenceLink.js')

const SID = real.scanId
const FILES = Object.keys(real.files).map((file) => ({ file, score: 70, department: 'Ops', issues: [] }))

beforeEach(() => {
  h.calls = []; h.renders = []
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:x')
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  try { sessionStorage.clear() } catch { /* jsdom */ }
})
afterEach(async () => { await unmountAll(); vi.restoreAllMocks() })

const waitFor = async (pred, tries = 400) => {
  for (let i = 0; i < tries; i++) {
    if (pred()) return true
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 5)) })
  }
  return pred()
}
const exportAs = async (c, mode) => {
  const btn = c.querySelector(`button[aria-label="${mode} — PDF"]`)
  expect(btn, `no "${mode} — PDF" button`).toBeTruthy()
  await act(async () => { btn.click() })
  expect(await waitFor(() => h.renders.length > 0)).toBe(true)
  return h.renders[0].model
}
const linkedCards = (model) => model.blocks.filter((b) => b.k === 'findingCard')

function expectExactLinks(model) {
  const cards = linkedCards(model)
  expect(cards.length).toBeGreaterThan(0)
  for (const card of cards) {
    const t = parseEvidenceHref(card.location.href)
    expect(t.scanId).toBe(SID)
    expect(t.file).toBe(card.file)
    expect(real.files[card.file].findings.some((f) => f.id === t.findingId && f.id === card.id)).toBe(true)
  }
}

const mounts = {
  Overview: () => createElement(Overview, { run: { id: SID, files: FILES.length, certifiable: 0, avg_score: 70, status: 'done', scope: {} }, files: FILES }),
  'Assess RuleBreakdown': () => createElement(RuleBreakdown, { scanId: SID, files: FILES }),
}

describe.each(Object.keys(mounts))('%s scan report', (name) => {
  const mount = async () => {
    const { container, root } = createTestRoot()
    await act(async () => { root.render(mounts[name]()) })
    await waitFor(() => !!container.querySelector('button[aria-label="Reviewer packet — PDF"]'))
    return container
  }

  it('Reviewer packet asks for the finding records and links each card to its exact record', async () => {
    const model = await exportAs(await mount(), 'Reviewer packet')
    expect(h.calls.length).toBeGreaterThan(0)
    expect(h.calls.every((o) => o.includeFindings === true)).toBe(true)
    expectExactLinks(model)
  })

  it('Summary prints no cards and does not ask for the records', async () => {
    await exportAs(await mount(), 'Summary')
    expect(h.calls.length).toBeGreaterThan(0)
    expect(h.calls.some((o) => 'includeFindings' in o)).toBe(false)
  })
})
