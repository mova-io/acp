/**
 * Contract 8, end to end on the client: a SCAN report's finding cards link each EXACT record.
 *
 * `__fixtures__/reportScanFindings.real.json` is written by tests/test_report_scan_finding_links.py
 * from a real SQLite store through the real routes (`include=findings`, three pages of one
 * snapshot, plus each file's per-file facts). No id below is typed by hand. The path under test:
 *
 *   recorded pages → loadScanReportFacts (a transport that replays them, checking what it was
 *   asked) → aggregateScanReport / buildScanReportModel → reportHtmlFromModel → every link parsed →
 *   parseEvidenceHref → the evidence viewer, mounted on the recorded per-file facts.
 *
 * This test also WRITES (regeneration) or CHECKS (default) the model fixture the Python test renders
 * with the server renderer: `__fixtures__/reportScanFindings.model.real.json`. Regenerate with
 *   ACP_REGEN_REPORT_FIXTURES=1 npx vitest run src/scanReportFindingLinks.test.jsx
 * after regenerating the facts fixture (see the Python module's docstring for the order).
 *
 * DOM-level, not browser-level: the preview server serves the shared checkout (CLAUDE.md).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { createTestRoot, unmountAll } from './testRoots.js'
import real from './__fixtures__/reportScanFindings.real.json'

let FACTS_BY_FILE = real.files
vi.mock('./api.js', async (importActual) => {
  const actual = await importActual()
  return {
    ...actual,
    getFileReportFacts: vi.fn(async (_scan, file) => FACTS_BY_FILE[file] ?? null),
    getExactArtifactPage: vi.fn(async () => null),
    getFilePage: vi.fn(async () => null),
  }
})

const { loadScanReportFacts } = await import('./fileReportData.js')
const { aggregateScanReport, buildScanReportModel, serverFindingCards } = await import('./scanReport.js')
const { reportHtmlFromModel } = await import('./htmlReport.js')
const { parseEvidenceHref } = await import('./evidenceLink.js')
const { default: FindingEvidenceViewer, RECORD_MISSING } = await import('./FindingEvidenceViewer.jsx')

const SID = real.scanId
const ORIGIN = 'https://acp.example.com'
const NOW = new Date('2026-09-18T12:00:00Z')
const BOARD = 'Board/Q1 #2 – Überblick.docx'
const FIXED = 'remediated/fixed.docx'
// Relative to frontend/ (vitest's root), as the other file-reading tests here do.
const MODEL_FIXTURE = 'src/__fixtures__/reportScanFindings.model.real.json'

// Replays the recorded pages, and refuses anything the real server would not have answered
// the same way: the wrong offset, a missing snapshot digest, or no include=findings.
function replay({ pages = real.scanPages, calls = [] } = {}) {
  return async (scanId, opts) => {
    calls.push(opts)
    expect(scanId).toBe(SID)
    expect(opts.includeFindings).toBe(true)
    const page = pages.find((p) => p.offset === opts.offset)
    if (!page) throw new Error(`no recorded page at offset ${opts.offset}`)
    if (opts.offset > 0) expect(opts.digest).toBe(pages[0].factsDigest)
    expect(opts.limit).toBe(real.pageLimit)
    return JSON.parse(JSON.stringify(page))
  }
}

async function scanModel(mode = 'full', { transport = replay(), files = [] } = {}) {
  const got = await loadScanReportFacts(SID, { getScanReportFacts: transport, limit: real.pageLimit, includeFindings: true })
  // `timestamp`/`date` are locale- and time-zone-formatted; pinned so the model fixture is the
  // same on every machine.
  const data = { ...aggregateScanReport({ scanId: SID, files, facts: got.facts, now: NOW, org: 'Hospital' }),
    timestamp: 'September 18, 2026, 12:00 PM UTC', date: 'September 18, 2026' }
  const model = buildScanReportModel({ ...data, mode, factsIndexComplete: got.complete, factsIncompleteReason: got.incompleteReason || got.factsError || null })
  return { got, model }
}
const cardsOf = (m) => m.blocks.filter((b) => b.k === 'findingCard')
const textOf = (m) => m.blocks.filter((b) => b.k === 'text').map((b) => b.text).join('\n')
// Every outstanding finding the server recorded, on documents without a corrected copy.
const expected = () => Object.entries(real.files)
  .filter(([, f]) => !f.identity.remediatedAt && f.identity.currentArtifact?.kind !== 'corrected')
  .flatMap(([file, f]) => f.findings.filter((x) => x.state !== 'resolved_verified').map((x) => `${file}␟${x.id}`))
  .sort()

afterEach(async () => { await unmountAll(); FACTS_BY_FILE = real.files })

describe('the recorded pages come back whole, with their finding records', () => {
  it('reads every page of one snapshot with include=findings, and says the records were included', async () => {
    const calls = []
    const { got } = await scanModel('full', { transport: replay({ calls }) })
    expect(calls.map((c) => c.offset)).toEqual([0, 2, 4])
    expect(got.complete).toBe(true)
    expect(got.findingsIncluded).toBe(true)
    expect(got.files.map((r) => r.file)).toEqual(Object.keys(real.files).sort())
  })

  it('without the option the transport is asked exactly what it was asked before (packet exporter)', async () => {
    const calls = []
    const got = await loadScanReportFacts(SID, { getScanReportFacts: async (_s, o) => { calls.push(o); return real.scanPages.find((p) => p.offset === o.offset) }, limit: 2 })
    expect(calls[0]).toEqual({ offset: 0, limit: 2 })
    expect(calls[1]).toEqual({ offset: 2, limit: 2, digest: real.scanPages[0].factsDigest })
    expect('findingsIncluded' in got).toBe(false)
  })
})

describe('each finding card links its exact record', () => {
  it('one card per recorded finding — every occurrence — each naming its own server id', async () => {
    const { model } = await scanModel('full')
    const cards = cardsOf(model)
    expect(cards.map((c) => `${c.file}␟${c.id}`).sort()).toEqual(expected())
    for (const c of cards) {
      const t = parseEvidenceHref(c.location.href)
      expect(t).toEqual({ scanId: SID, file: c.file, findingId: c.id, changeId: null, sha256: null, version: null })
      const record = real.files[c.file].findings.find((f) => f.id === t.findingId)
      expect(record, `${c.file} has no finding ${t.findingId}`).toBeTruthy()
      expect(c.location.label).toBe(record.location?.label ?? null)
    }
  })

  it('two findings of one criterion with the same detail on two objects are two exact links, never the first', async () => {
    const { model } = await scanModel('full')
    const alt = cardsOf(model).filter((c) => c.file === BOARD && c.criterion === '1.1.1')
    expect(alt).toHaveLength(2)
    expect(new Set(alt.map((c) => c.description)).size).toBe(1)
    expect(alt.map((c) => c.location.label).sort()).toEqual(['Image 1', 'Image 2'])
    expect(new Set(alt.map((c) => c.location.href)).size).toBe(2)
    const byLabel = Object.fromEntries(real.files[BOARD].findings.filter((f) => f.location).map((f) => [f.location.label, f.id]))
    for (const c of alt) expect(parseEvidenceHref(c.location.href).findingId).toBe(byLabel[c.location.label])
  })

  it('a finding with no recorded location still links its own record, and says so', async () => {
    const { model } = await scanModel('full')
    const lang = cardsOf(model).find((c) => c.file === BOARD && c.criterion === '3.1.1')
    expect(lang.location.label).toBeNull()
    const record = real.files[BOARD].findings.find((f) => f.sc === '3.1.1')
    expect(record.location).toBeNull()
    expect(parseEvidenceHref(lang.location.href).findingId).toBe(record.id)
    const html = reportHtmlFromModel(model, { origin: ORIGIN })
    const href = `${ORIGIN}${lang.location.href}`.replace(/&/g, '&amp;')
    expect(html).toContain(`Location not recorded · <a href="${href}">open this record in ACP</a>`)
  })

  it('the corrected document is not a card; it is counted as awaiting re-assessment', async () => {
    const { model } = await scanModel('full')
    expect(cardsOf(model).some((c) => c.file === FIXED)).toBe(false)
    expect(textOf(model)).toMatch(/1 document\(s\) with a saved corrected copy are not listed here/)
  })

  it('every link in the downloaded HTML names a finding that exists in that file’s per-file facts', async () => {
    const { model } = await scanModel('full')
    const doc = new DOMParser().parseFromString(reportHtmlFromModel(model, { origin: ORIGIN }), 'text/html')
    const links = [...doc.querySelectorAll('article.card.finding a[href]')].map((a) => new URL(a.getAttribute('href')))
    expect(links).toHaveLength(cardsOf(model).length)
    for (const u of links) {
      expect(u.origin).toBe(ORIGIN)
      const t = parseEvidenceHref(u.search)
      expect(t.scanId).toBe(SID)
      expect(real.files[t.file].findings.some((f) => f.id === t.findingId)).toBe(true)
    }
    expect(new Set(links.map((u) => u.search)).size).toBe(links.length)
  })
})

describe('the viewer opens exactly the record the link names', () => {
  const flush = async () => { for (let i = 0; i < 5; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }
  async function open(href) {
    const { container, root } = createTestRoot()
    await act(async () => { root.render(createElement(FindingEvidenceViewer, { target: parseEvidenceHref(href), onClose: () => {} })) })
    await flush()
    return container
  }

  it('each Board link (two same-criterion objects and the unlocated one) resolves to its own record', async () => {
    const { model } = await scanModel('full')
    const board = cardsOf(model).filter((c) => c.file === BOARD)
    expect(board).toHaveLength(3)
    for (const card of board) {
      const c = await open(card.location.href)
      const text = c.textContent
      expect(text).not.toContain(RECORD_MISSING)
      expect(text).toContain(`WCAG ${card.criterion}`)
      expect(text).toContain(`Location${card.location.label || 'Location not recorded'}`)
      // …and not the other occurrence of the same criterion.
      for (const other of board.filter((o) => o !== card && o.location.label)) {
        expect(text).not.toContain(`Location${other.location.label}`)
      }
      await unmountAll()
    }
  })

  it('bite check: a link whose id is not in the evidence says so — it never lands on the same criterion', async () => {
    const { model } = await scanModel('full')
    const card = cardsOf(model).find((c) => c.file === BOARD && c.location.label === 'Image 2')
    const other = cardsOf(model).find((c) => c.file === BOARD && c.location.label === 'Image 1')
    FACTS_BY_FILE = { ...real.files, [BOARD]: { ...real.files[BOARD], findings: real.files[BOARD].findings.filter((f) => f.id !== card.id) } }
    const c = await open(card.location.href)
    expect(c.textContent).toContain(RECORD_MISSING)
    expect(c.textContent).not.toContain(other.id)
  })
})

describe('without the server records there are no links, and the report says why', () => {
  const SCREEN = [{ file: BOARD, score: 60, issues: [
    { wcag: 'SC_1_1_1', severity: 'SERIOUS', detail: 'Image has no description', location: 'docx:image:1' },
    { wcag: 'SC_1_1_1', severity: 'SERIOUS', detail: 'Image has no description', location: 'docx:image:2' },
  ] }]

  it('the facts read failed: cards from the screen list, none linked, and the reason is printed', async () => {
    const { model } = await scanModel('reviewer', { files: SCREEN, transport: async () => { throw new Error('503 service unavailable') } })
    const cards = cardsOf(model)
    expect(cards).toHaveLength(2)
    for (const c of cards) expect(c.location?.href ?? null).toBeNull()
    expect(textOf(model)).toMatch(/Exact finding links are unavailable in this report: the server's report evidence was not read \(.*503 service unavailable.*\)/)
  })

  it('a server that ignored include=findings: findingsIncluded is false, and no card links', async () => {
    const pages = real.scanPages.map((p) => ({ ...p, files: p.files.map(({ findings, remediatedAt, ...row }) => row) }))
    const { got, model } = await scanModel('reviewer', { files: SCREEN, transport: replay({ pages }) })
    expect(got.findingsIncluded).toBe(false)
    expect(cardsOf(model).every((c) => !c.location?.href)).toBe(true)
    expect(textOf(model)).toMatch(/did not include its finding records/)
  })

  it('demo mode (no report server): no links', () => {
    const r = serverFindingCards(null, { scanId: SID, data: { factsIncompleteReason: 'Demo mode has no report server.' } })
    expect(r.available).toBe(false)
    expect(r.reason).toMatch(/Demo mode/)
  })
})

describe('bounds', () => {
  it('Reviewer shows 50 cards and says how many more; Full shows every one', async () => {
    // 60 copies of the real Board records in one row: the ids are made unique ONLY to exceed the cap.
    const base = real.scanPages[0]
    const many = Array.from({ length: 20 }, (_, n) => real.scanPages[0].files[0].findings.map((f) => ({ ...f, id: `${f.id}-${n}` }))).flat()
    const pages = [{ ...base, filesTotal: 1, complete: true, files: [{ ...base.files[0], findings: many }] }]
    const transport = async () => JSON.parse(JSON.stringify(pages[0]))
    const reviewer = (await scanModel('reviewer', { transport })).model
    expect(cardsOf(reviewer)).toHaveLength(50)
    expect(textOf(reviewer)).toMatch(/10 more findings \(of 60\) are listed in the Full evidence report/)
    const full = (await scanModel('full', { transport })).model
    expect(cardsOf(full)).toHaveLength(60)
  })
})

describe('the model the server renderer is checked against (tests/test_report_scan_finding_links.py)', () => {
  it('is the model built from the recorded pages', async () => {
    const { model } = await scanModel('full')
    const text = `${JSON.stringify(model, null, 1)}\n`
    if (process.env.ACP_REGEN_REPORT_FIXTURES === '1') writeFileSync(MODEL_FIXTURE, text, 'utf-8')
    expect(existsSync(MODEL_FIXTURE), 'run with ACP_REGEN_REPORT_FIXTURES=1 to write the model fixture').toBe(true)
    expect(JSON.parse(readFileSync(MODEL_FIXTURE, 'utf-8'))).toEqual(JSON.parse(text))
  })
})
