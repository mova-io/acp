/**
 * "Per-file packets (ZIP)" in the two LIVE scan report menus — Overview's Reports menu and the
 * Assess RuleBreakdown header — at the DOM level. The browser preview serves the shared checkout
 * (CLAUDE.md), so this is where the worktree's menus are exercised.
 *
 * Only api.js is replaced, and it answers with the responses tests/test_report_packet_real_store.py
 * recorded from the real store and routes. Everything between the click and the download is the
 * live code: ReportModeMenu → reportPacketExport.packetZipFormat → reportPacketLive (FileDrawer's
 * coverage rows, buildFileReportData, buildFileReportModel, renderReportBlob) → JSZip.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import JSZip from 'jszip'
import { createTestRoot, unmountAll } from './testRoots.js'

const here = dirname(fileURLToPath(import.meta.url))
const REC = JSON.parse(readFileSync(join(here, '..', '..', 'tests', 'fixtures', 'report_packet_real_store.json'), 'utf8'))
const SID = REC.scanId
const h = vi.hoisted(() => ({ render: null, renders: [], hold: null }))

vi.mock('./api.js', async (importOriginal) => {
  const orig = await importOriginal()
  const clone = (v) => JSON.parse(JSON.stringify(v))
  return {
    ...orig,
    SIM: false,
    getScanReportFacts: async (sid, { offset = 0 } = {}) => {
      const page = REC.scanFactsPages.find((p) => p.offset === offset)
      if (!page) throw new Error(`no page at ${offset}`)
      // The live loader asks with limit 200, so the recorded 3-row pages are re-joined into one.
      const all = REC.scanFactsPages.flatMap((p) => p.response.files)
      return { ...clone(page.response), offset, files: clone(all.slice(offset)), complete: true }
    },
    getFileReportFacts: async (sid, f) => clone(REC.fileFacts[f]),
    getScan: async () => clone(REC.scan),
    getRules: async () => clone(REC.rules),
    getRubric: async () => clone(REC.rubric),
    getCapability: async () => clone(REC.capability),
    getConfig: async () => ({ version: '2026.9.17.9' }),
    getDecisions: async () => clone(REC.decisions),
    getFileRemediationState: async (sid, f) => clone(REC.remediationState[f] || []),
    listDispositions: async (sid, f) => clone(REC.dispositions[f] || []),
    getFileArtifactPage: async () => null,
    getFilePage: async () => null,
    getScanDiff: async () => null,
    fetchChangeReviews: async () => ({ reviews: {}, artifact: null }),
    getScanInventory: async () => null,
    getScanTraces: async () => [
      { file: 'CON.pptx', rule_id: '1.1.1', plain_name: 'Non-text Content', level: 'A', outcome: 'FAIL', finding_count: 1 },
      { file: 'Reports/Q1 summary.pdf', rule_id: '1.4.3', plain_name: 'Contrast', level: 'AA', outcome: 'FAIL', finding_count: 1 },
    ],
    getTraceStatus: async () => null,
    openTraceUrl: () => null,
    postReportRender: async (sid, body) => {
      h.renders.push(body)
      if (h.hold) await h.hold
      return { ok: true, status: 200, headers: new Headers(), blob: async () => new Blob([`%PDF-1.7 ${body.file}`], { type: 'application/pdf' }) }
    },
  }
})

const { default: Overview } = await import('./Overview.jsx')
const { RuleBreakdown } = await import('./Transparency.jsx')

let saved
beforeEach(() => {
  h.renders = []; h.hold = null
  saved = []
  globalThis.URL.createObjectURL = vi.fn((blob) => { saved.push({ blob }); return `blob:${saved.length}` })
  globalThis.URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    const n = Number(String(this.href).split(':').pop()) - 1
    if (saved[n]) saved[n].filename = this.download
  })
})
afterEach(() => { unmountAll(); vi.restoreAllMocks() })

const FILES = REC.scan.files
const waitFor = async (pred, tries = 400) => {
  for (let i = 0; i < tries; i++) {
    if (pred()) return true
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 5)) })
  }
  return pred()
}
const zipButton = (c, mode) => c.querySelector(`button[aria-label="${mode} — Per-file packets (ZIP)"]`)

describe('Per-file packets in the Overview Reports menu', () => {
  const mount = async () => {
    const { container, root } = createTestRoot()
    await act(async () => {
      root.render(createElement(Overview, { run: { id: SID, files: FILES.length, certifiable: 0, avg_score: 70, status: 'done', scope: {} }, files: FILES }))
    })
    return container
  }

  it('offers the ZIP beside every mode, explains each export, and exports every indexed document', async () => {
    const c = await mount()
    for (const m of ['Summary', 'Reviewer packet', 'Full evidence']) expect(zipButton(c, m)).toBeTruthy()
    const menuText = c.querySelector('.reports-menu-items').textContent
    expect(menuText).toMatch(/For a decision: one page/)
    expect(menuText).toMatch(/For the person confirming the work/)
    expect(menuText).toMatch(/For audit: every recorded finding/)
    expect(menuText).toMatch(/not tagged for assistive technology/)          // governance PDF, honestly labelled
    expect(menuText).toMatch(/one row per finding on the documents shown here, with rule, page and location/)

    await act(async () => { zipButton(c, 'Full evidence').click() })
    expect(await waitFor(() => !zipButton(c, 'Full evidence').disabled && saved.some((s) => s.filename?.endsWith('.zip')))).toBe(true)
    const zipSave = saved.find((s) => s.filename?.endsWith('.zip'))
    expect(zipSave.filename).toBe(`accessibility-packets-full-${SID}.zip`)
    const zip = await JSZip.loadAsync(await zipSave.blob.arrayBuffer())
    const csv = await zip.file("index.csv").async("string")
    for (const f of REC.scanFactsPages.flatMap((p) => p.response.files)) expect(csv).toContain(`"${f.file}"`)
    // every render carried the per-file digest it was checked against
    for (const body of h.renders) expect(body.factsDigest).toBe(REC.fileFacts[body.file].factsDigest)
    // one document is unanalysable, so the menu reports an INCOMPLETE export and offers the index alone
    expect(c.querySelector('[role="alert"]').textContent).toMatch(/Full evidence \(Per-file packets \(ZIP\)\) was downloaded but is INCOMPLETE\. Exported 7 of 8 documents\. INCOMPLETE/)
    const extra = [...c.querySelectorAll('.reportmode-downloads button')].map((b) => b.textContent)
    expect(extra).toEqual(['Download Master index (HTML)', 'Download Master index (CSV)'])
  })

  it('shows progress in the live region, and Cancel stops the export and says it is incomplete', async () => {
    let release
    h.hold = new Promise((r) => { release = r })
    const c = await mount()
    await act(async () => { zipButton(c, 'Reviewer packet').click() })
    expect(await waitFor(() => h.renders.length > 0)).toBe(true)
    const status = c.querySelector('[role="status"]')
    expect(status.textContent).toMatch(/Per-file packets: \d+ of 8 document\(s\) processed/)
    const cancel = c.querySelector('button.reportmode-cancel')
    expect(cancel.textContent).toBe('Cancel Per-file packets (ZIP)')
    await act(async () => { cancel.click() })
    release()
    expect(await waitFor(() => !!c.querySelector('[role="alert"]'))).toBe(true)
    const alert = c.querySelector('[role="alert"]').textContent
    expect(alert).toMatch(/Reviewer packet \(Per-file packets \(ZIP\)\) was cancelled\. Cancelled\. \d+ of 8 documents were exported; \d+ were not/)
    expect(alert).toMatch(/INCOMPLETE/)
    expect(c.querySelector('button.reportmode-cancel')).toBeNull()
  })
})

describe('Findings (CSV) from the Overview Reports menu', () => {
  it('carries rule id, page and a readable location for every finding on screen — and invents no id or status', async () => {
    const { container, root } = createTestRoot()
    await act(async () => {
      root.render(createElement(Overview, { run: { id: SID, files: FILES.length, certifiable: 0, avg_score: 70, status: 'done', scope: {} }, files: FILES }))
    })
    const btn = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Findings (CSV)')
    await act(async () => { btn.click() })
    const csvSave = saved.at(-1)
    const text = (await csvSave.blob.text()).replace(/^﻿/, '')
    const [head, ...lines] = text.split('\r\n')
    const cols = head.split(',').map((c) => c.replace(/"/g, ''))
    expect(cols).toEqual(['Document', 'Department', 'Owner', 'Type', 'Score', 'WCAG', 'Rule id', 'Level', 'Severity', 'Page',
      'Location', 'Location (as recorded)', 'Detail', 'Auto-fixable', 'Recommended action', 'Effort (min)'])
    expect(cols).not.toContain('Finding id')
    expect(cols).not.toContain('Status')
    const issues = FILES.flatMap((f) => (f.issues || []).map((i) => ({ f, i })))
    expect(lines).toHaveLength(issues.length)
    const byRow = lines.map((l) => l.match(/"(?:[^"]|"")*"/g).map((c) => c.slice(1, -1)))
    const xlsx = byRow.find((r) => r[cols.indexOf('Location (as recorded)')] === 'xlsx:sheet:Budget:cell:B3')
    expect(xlsx[cols.indexOf('Location')]).toMatch(/Budget/)
    expect(xlsx[cols.indexOf('Location')]).not.toMatch(/^xlsx:/)
    expect(xlsx[cols.indexOf('Rule id')]).toBe('XLSX-HEADER-001')
    const pdfPage = byRow.find((r) => r[cols.indexOf('Rule id')] === 'PDF-CONTRAST-001')
    expect(pdfPage[cols.indexOf('Page')]).toBe('3')
    const noLoc = byRow.find((r) => r[cols.indexOf('Rule id')] === 'PDF-LANG-001')
    expect(noLoc[cols.indexOf('Location')]).toBe('Location not recorded')
    expect(noLoc[cols.indexOf('Page')]).toBe('')                 // never a page-1 default
  })
})

describe('Per-file packets in the Assess RuleBreakdown menu', () => {
  it('is offered in the live "Export scan report" menu and runs the same export', async () => {
    const { container, root } = createTestRoot()
    await act(async () => { root.render(createElement(RuleBreakdown, { scanId: SID, files: FILES })) })
    expect(await waitFor(() => !!zipButton(container, 'Full evidence'))).toBe(true)
    await act(async () => { zipButton(container, 'Summary').click() })
    expect(await waitFor(() => saved.some((s) => s.filename?.endsWith('.zip')))).toBe(true)
    expect(saved.find((s) => s.filename?.endsWith('.zip')).filename).toBe(`accessibility-packets-summary-${SID}.zip`)
    expect(h.renders.every((b) => b.mode === 'summary' && b.kind === 'file')).toBe(true)
  })
})
