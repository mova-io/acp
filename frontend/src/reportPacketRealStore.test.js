// REAL store → scan pages → file facts → ZIP (parent review item 5).
//
// tests/test_report_packet_real_store.py builds a multi-file scan in the real store, serves it
// through the production routes, and records every response the packet exporter reads into
// tests/fixtures/report_packet_real_store.json. This replays those exact responses through the
// REAL exporter: the real paged loader, the drawer's own coverage rows (FileDrawer
// computeCoverageRows), the real buildFileReportData and buildFileReportModel, and real JSZip. Only
// the renderer is a stand-in (it cannot run WeasyPrint); it records what it was asked to render,
// and the pytest proves the server accepts those digests.
import { describe, it, expect, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import JSZip from 'jszip'
import { exportScanPackets, makeFileDataBuilder } from './reportPacketExport.js'
import { loadScanReportFacts, buildFileReportData } from './fileReportData.js'
import { buildFileReportModel } from './reportModel.js'
import { computeCoverageRows } from './FileDrawer.jsx'
import { isDispositionable, normalizeDisposition } from './disposition.js'
import { statusOf } from './docStatus.js'
import { SCOPE_SCS } from './activeScope.js'
import { scOf } from './BeforeAfter.jsx'
import { CAPABILITY_FALLBACK } from './capability.js'
import { appLinkFor } from './reportPacketLive.js'
import * as evidenceLink from './evidenceLink.js'

const here = dirname(fileURLToPath(import.meta.url))
const REC = JSON.parse(readFileSync(join(here, '..', '..', 'tests', 'fixtures', 'report_packet_real_store.json'), 'utf8'))
const SID = REC.scanId
const scOfWcag = (v) => ((v || '').replace(/^SC_/, '').replace(/_/g, '.').match(/^\d+\.\d+\.\d+/) || [''])[0]

// The recorded server, answering exactly what it answered, and checking it is asked the same way.
function replay() {
  const asked = []
  const api = {
    getScanReportFacts: async (sid, { offset = 0, limit, digest } = {}) => {
      asked.push({ offset, limit, digest })
      const page = REC.scanFactsPages.find((p) => p.offset === offset)
      if (!page) throw new Error(`no recorded page at offset ${offset}`)
      expect(digest ?? null).toBe(page.digest)
      return structuredClone(page.response)
    },
    getFileReportFacts: async (sid, file) => {
      expect(sid).toBe(SID)
      if (!(file in REC.fileFacts)) throw new Error(`no recorded facts for ${file}`)
      return structuredClone(REC.fileFacts[file])
    },
    getScan: async () => structuredClone(REC.scan),
    getRules: async () => structuredClone(REC.rules),
    getRubric: async () => structuredClone(REC.rubric),
    getCapability: async () => structuredClone(REC.capability),
    getConfig: async () => ({ version: '2026.9.17.9' }),
    getDecisions: async () => structuredClone(REC.decisions),
    getFileRemediationState: async (sid, f) => structuredClone(REC.remediationState[f] ?? []),
    listDispositions: async (sid, f) => structuredClone(REC.dispositions[f] ?? []),
    getFileRemediationDiffs: async () => { throw new Error('facts carry the saved changes; this must not be read') },
    getFileArtifactPage: async () => null,
    getFilePage: async () => null,
    getScanDiff: async () => null,
    loadReviews: async () => ({ ok: false, error: 'change reviews were not recorded; facts.reviews is used' }),
  }
  return { api, asked }
}

async function run({ renderBlob, files = [] } = {}) {
  const { api, asked } = replay()
  const downloads = []
  const renders = []
  const buildFileData = makeFileDataBuilder({
    scanId: SID, api, computeCoverageRows, buildFileReportData, isDispositionable, normalizeDisposition,
    scOf, inScope: (i) => SCOPE_SCS.has(scOfWcag(i.wcag)), capabilityFallback: CAPABILITY_FALLBACK,
  })
  const res = await exportScanPackets({
    scanId: SID, mode: 'full', files,
    deps: {
      loadIndex: ({ onProgress }) => loadScanReportFacts(SID, { getScanReportFacts: api.getScanReportFacts, limit: REC.pageLimit, onProgress }),
      loadScanFiles: async () => (await api.getScan())?.files || [],
      buildFileData,
      buildModel: buildFileReportModel,
      renderBlob: renderBlob || (async (a) => { renders.push(a); return { ok: true, format: 'pdf', blob: new Blob([`%PDF-1.7 ${a.file}`], { type: 'application/pdf' }) } }),
      download: (blob, filename) => downloads.push({ blob, filename }),
      JSZip,
      appLink: (file) => appLinkFor(SID, file, { origin: 'https://acp.example.com' }),
      statusOf,
    },
  })
  return { res, downloads, renders, asked }
}

const indexRows = () => REC.scanFactsPages.flatMap((p) => p.response.files)
const csvRows = (csv) => {
  const lines = csv.replace(/^﻿/, '').trim().split('\r\n')
  const parse = (l) => l.match(/"(?:[^"]|"")*"/g).map((c) => c.slice(1, -1).replace(/""/g, '"'))
  const head = parse(lines[0])
  return lines.slice(1).map((l) => Object.fromEntries(parse(l).map((v, i) => [head[i], v])))
}

describe('per-file packets over the recorded real-store responses', () => {
  it('the recording is multi-page and its index rows carry the per-file digests (contract 3a)', () => {
    expect(REC.scanFactsPages.length).toBeGreaterThanOrEqual(3)
    for (const r of indexRows()) expect(r.factsDigest).toBe(REC.fileFacts[r.file].factsDigest)
  })

  it('every document in the index gets a row, with the digest the index and a fresh read agree on', async () => {
    const { res, downloads, renders, asked } = await run()
    expect(asked.map((a) => a.offset)).toEqual(REC.scanFactsPages.map((p) => p.offset))
    const names = indexRows().map((r) => r.file)
    expect(res.rows.map((r) => r.file)).toEqual(names)

    // The unanalysable document gets no report — as in the drawer — and says why; every other one is packed.
    const by = Object.fromEntries(res.rows.map((r) => [r.file, r]))
    expect(by['broken.docx'].status).toBe('failed')
    for (const n of names.filter((x) => x !== 'broken.docx')) expect(by[n]).toMatchObject({ status: 'included', format: 'pdf' })

    // What was sent to the renderer: the per-file digest, the real model built by the drawer's builder.
    expect(renders.map((r) => r.file).sort()).toEqual(names.filter((x) => x !== 'broken.docx').sort())
    for (const r of renders) {
      expect(r.factsDigest).toBe(REC.fileFacts[r.file].factsDigest)
      expect(r.model.identity.factsDigest).toBe(REC.fileFacts[r.file].factsDigest)
      expect(r.model.mode).toBe('full')
      expect(Array.isArray(r.model.blocks) && r.model.blocks.length).toBeTruthy()
    }
    // A real finding reaches the model: the Word paragraph finding of the Unicode document.
    const uber = renders.find((r) => r.file.startsWith('Policies/'))
    expect(JSON.stringify(uber.model.blocks)).toMatch(/Paragraph styled as a heading is not a heading/)

    // The ZIP: sanitised, de-duplicated paths; an index row per document with exact provenance.
    expect(downloads).toHaveLength(1)
    expect(downloads[0].filename).toBe(`accessibility-packets-full-${SID}.zip`)
    const zip = await JSZip.loadAsync(await downloads[0].blob.arrayBuffer())
    const packets = Object.keys(zip.files).filter((n) => n.startsWith('packets/') && !zip.files[n].dir).sort()
    expect(packets).toEqual([
      'packets/Budget & notes (draft).xlsx.pdf',
      'packets/Policies/2026/Überblick – Richtlinie.docx.pdf',
      'packets/Reports/Q1 summary.pdf.pdf',
      'packets/Reports/q1 SUMMARY (2).pdf.pdf',
      'packets/_CON.pptx.pdf',
      'packets/not-yet.pdf.pdf',
      'packets/outside/escape.xlsx.pdf',
    ])
    const rows = csvRows(await zip.file('index.csv').async('string'))
    expect(rows.map((r) => r['Original name'])).toEqual(names)
    for (const row of rows) {
      const facts = REC.fileFacts[row['Original name']]
      const idx = indexRows().find((r) => r.file === row['Original name'])
      expect(row['Scan id']).toBe(SID)
      expect(row['Index snapshot digest']).toBe(REC.scanFactsPages[0].response.factsDigest)
      expect(row['Per-file facts digest']).toBe(facts.factsDigest)
      expect(row['Assessment state']).toBe(idx.assessment.state)
      expect(row['Findings total']).toBe(idx.findingsTotal == null ? 'not recorded' : String(idx.findingsTotal))
      expect(row['Findings open']).toBe(idx.findingsOpen == null ? 'not recorded' : String(idx.findingsOpen))
      expect(row['Corrected SHA-256']).toBe(facts.identity.correctedSha256 ?? 'not recorded')
      expect(row['Source checksum']).toBe(facts.identity.sourceChecksum ?? 'not recorded')
      // no document-level route exists in ACP yet, so no link is fabricated
      expect(row['Open in ACP']).not.toMatch(/^\//)
    }
    const html = await zip.file('index.html').async('string')
    expect(html).toMatch(/INCOMPLETE\./)            // broken.docx has no packet, and it says so
    expect(html).toMatch(/1 document\(s\) have no packet because they failed/)
  })

  it('evidence that moved after the index was read is marked changed, not mixed', async () => {
    const moved = 'Reports/Q1 summary.pdf'
    const saved = REC.fileFacts[moved]
    // the per-file read now returns different evidence (a reviewer note, a new corrected copy …)
    REC.fileFacts[moved] = { ...saved, factsDigest: '0'.repeat(64) }
    try {
      const { res, renders } = await run()
      const row = res.rows.find((r) => r.file === moved)
      expect(row.status).toBe('changed')
      expect(row.archivePath).toBeNull()
      expect(renders.map((r) => r.file)).not.toContain(moved)
      expect(res.complete).toBe(false)
    } finally {
      REC.fileFacts[moved] = saved
    }
  })

  it('a PDF service outage packs the same model as HTML, labelled html-fallback', async () => {
    const html = vi.fn(async (a) => ({ ok: false, status: 503, fallback: 'html', format: 'html', htmlBlob: new Blob([`<!doctype html><title>${a.file}</title>`], { type: 'text/html' }), message: 'The PDF service was not available, so the report was produced as accessible HTML instead of PDF.' }))
    const { res, downloads } = await run({ renderBlob: html })
    const zip = await JSZip.loadAsync(await downloads[0].blob.arrayBuffer())
    const rows = csvRows(await zip.file('index.csv').async('string'))
    const packed = rows.filter((r) => r.Status === 'included')
    expect(packed.length).toBe(7)
    expect(packed.every((r) => r.Format === 'html-fallback' && r['Packet (in archive)'].endsWith('.html'))).toBe(true)
    expect(Object.keys(zip.files).some((n) => n.endsWith('.pdf.pdf'))).toBe(false)
    expect(res.message).toMatch(/7 of them as HTML|INCOMPLETE/)
  })

  it('the "Open in ACP" column links only when a document-level evidence route exists', () => {
    const none = appLinkFor(SID, 'a.pdf', { origin: 'https://acp.example.com', links: { ...evidenceLink, fileEvidenceHref: undefined } })
    expect(none.href).toBeNull()
    expect(none.note).toMatch(/no document link could be built/)
    const withRoute = appLinkFor(SID, 'Board/a #1.pdf', {
      origin: 'https://acp.example.com',
      links: { ...evidenceLink, fileEvidenceHref: ({ scanId, file }) => `/?${new URLSearchParams({ view: 'evidence', scan: scanId, file })}` },
    })
    expect(withRoute.href).toBe('https://acp.example.com/?view=evidence&scan=s-packets&file=Board%2Fa+%231.pdf')
    const untrusted = appLinkFor(SID, 'a.pdf', { origin: 'http://intranet.example', links: { ...evidenceLink, fileEvidenceHref: () => '/?view=evidence&scan=s&file=a' } })
    expect(untrusted.href).toBeNull()
    expect(untrusted.note).toMatch(/without a trusted ACP web address/)
  })
})
