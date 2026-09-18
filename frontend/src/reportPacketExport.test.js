// The per-file packet exporter's orchestration (reportPacketExport.exportScanPackets): every
// document in the SERVER's index gets a row; drift, fallbacks, failures and cancellation are
// recorded exactly; parts are generated and released one at a time. The real paged loader
// (fileReportData.loadScanReportFacts) and real JSZip run here; the per-document builder and the
// renderer are stand-ins so hundreds of documents stay fast. reportPacketRealStore.test.js runs
// the real builders over real server responses.
import { describe, it, expect, vi } from 'vitest'
import JSZip from 'jszip'
import { exportScanPackets, packetProgressText, CHANGED_REASON, UNANALYSABLE_REASON } from './reportPacketExport.js'
import { loadScanReportFacts } from './fileReportData.js'
import { statusOf } from './docStatus.js'

const SID = 'scan-big'
const digestOf = (name, v = 1) => {
  let h = 0
  for (const ch of `${name}#${v}`) h = (h * 31 + ch.codePointAt(0)) >>> 0
  return h.toString(16).padStart(8, '0').repeat(8)
}

// A paged server over `names`, in the contract-3 shape.
function server(names, { indexDigest = 'f'.repeat(64), failPageAt = null } = {}) {
  const calls = []
  const getScanReportFacts = async (sid, { offset = 0, limit = 200, digest } = {}) => {
    calls.push({ offset, limit, digest })
    if (failPageAt != null && offset >= failPageAt) throw Object.assign(new Error('HTTP 502'), { status: 502 })
    const files = names.slice(offset, offset + limit).map((file) => ({
      file, factsDigest: digestOf(file), assessment: { state: 'assessed' }, score: 70,
      findingsTotal: 2, findingsOpen: null, savedChangesVerified: 1, savedChangesUnverified: 0,
      humanReviews: { pending: 1, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
      comparison: { status: 'no_baseline', reason: 'no earlier assessment', introduced: 0, resolved: 0, persisting: 0 },
    }))
    return {
      factsDigest: indexDigest, identity: { scanId: sid, platformVersion: '2026.9.17.1' },
      snapshot: { factsDigest: indexDigest, filesTotal: names.length, builtAt: '2026-09-17T10:00:00Z' },
      filesTotal: names.length, offset, limit, files, complete: offset + files.length >= names.length,
    }
  }
  return { getScanReportFacts, calls }
}

const factsFor = (file, v = 1) => ({
  factsDigest: digestOf(file, v), generatedAt: '2026-09-17T10:00:01Z',
  identity: { scanId: SID, file, sourceChecksum: 'md5', sourceChecksumKind: 'as recorded by the source system', sourceSha256: null, correctedSha256: null, platformVersion: '2026.9.17.1' },
})

function harness(names, over = {}) {
  const srv = server(names, over.server)
  const downloads = []
  const renders = []
  const deps = {
    loadIndex: ({ onProgress }) => loadScanReportFacts(SID, { getScanReportFacts: srv.getScanReportFacts, limit: 200, onProgress }),
    loadScanFiles: async () => names.map((file) => ({ file, status: 'uncertain', score: 70, issues: [] })),
    buildFileData: async ({ file, mode }) => ({ mode, file: file.file, facts: factsFor(file.file), factsDigest: digestOf(file.file) }),
    buildModel: (d) => ({ docTitle: d.file, blocks: [{ k: 'heading', text: d.file }], identity: { factsDigest: d.factsDigest } }),
    renderBlob: async (args) => { renders.push(args); return { ok: true, format: 'pdf', blob: new Blob([`%PDF ${args.file}`]), filename: 'x.pdf' } },
    download: (blob, filename) => downloads.push({ blob, filename }),
    JSZip,
    appLink: () => ({ href: null, note: 'no link' }),
    statusOf,
    ...over.deps,
  }
  return { deps, downloads, renders, srv }
}

const readZip = async (blob) => {
  const z = await JSZip.loadAsync(await blob.arrayBuffer())
  return z
}
const csvRows = (csv) => {
  const lines = csv.replace(/^﻿/, '').trim().split('\r\n')
  const parse = (l) => l.match(/"(?:[^"]|"")*"/g).map((c) => c.slice(1, -1).replace(/""/g, '"'))
  const head = parse(lines[0])
  return lines.slice(1).map((l) => Object.fromEntries(parse(l).map((v, i) => [head[i], v])))
}

describe('exportScanPackets — a large scan across index pages and ZIP parts', () => {
  const names = Array.from({ length: 450 }, (_, i) => `Dept ${i % 7}/Report ${String(i).padStart(3, '0')}.pdf`)

  it('reads all three index pages and gives every one of 450 documents a row and a packet', async () => {
    const h = harness(names)
    const progress = []
    const res = await exportScanPackets({ scanId: SID, mode: 'full', files: [], deps: h.deps, onProgress: (p) => progress.push(p), partMaxFiles: 200 })
    expect(h.srv.calls.map((c) => c.offset)).toEqual([0, 200, 400])
    expect(h.srv.calls.slice(1).every((c) => c.digest === 'f'.repeat(64))).toBe(true)
    expect(res.complete).toBe(true)
    expect(res.rows).toHaveLength(450)
    expect(res.rows.every((r) => r.status === 'included' && r.format === 'pdf')).toBe(true)
    // parts of at most 200, generated in order, master index only in the last
    expect(res.parts.map((p) => p.packets)).toEqual([200, 200, 50])
    expect(h.downloads.map((d) => d.filename)).toEqual([
      'accessibility-packets-full-scan-big-part-1.zip', 'accessibility-packets-full-scan-big-part-2.zip', 'accessibility-packets-full-scan-big-part-3.zip'])
    const first = await readZip(h.downloads[0].blob)
    expect(first.file('index.html')).toBeNull()
    expect(first.file('README-part-1.txt')).toBeTruthy()
    const last = await readZip(h.downloads[2].blob)
    const rows = csvRows(await last.file('index.csv').async('string'))
    expect(rows).toHaveLength(450)
    expect(new Set(rows.map((r) => r.Part))).toEqual(new Set(['1', '2', '3']))
    // every row points at a packet that exists in the part it names
    const zips = await Promise.all(h.downloads.map((d) => readZip(d.blob)))
    for (const r of rows) expect(zips[Number(r.Part) - 1].file(r['Packet (in archive)'])).toBeTruthy()
    // the digest sent to the server for each document is the one the index and the fresh read agree on
    expect(h.renders.every((a) => a.factsDigest === digestOf(a.file) && a.kind === 'file' && a.mode === 'full')).toBe(true)
    expect(progress.at(-1)).toMatchObject({ phase: 'zipping' })
    expect(packetProgressText({ phase: 'packets', done: 12, total: 450, part: 1, counts: { html: 1, failed: 2 } }))
      .toBe('Per-file packets: 12 of 450 document(s) processed (1 as HTML, 2 failed) · part 1')
  })

  it('never renders more than `concurrency` documents at once', async () => {
    let live = 0; let peak = 0
    const h = harness(names.slice(0, 40), { deps: { renderBlob: async (a) => { live++; peak = Math.max(peak, live); await new Promise((r) => setTimeout(r, 1)); live--; return { ok: true, format: 'pdf', blob: new Blob(['%PDF']) } } } })
    await exportScanPackets({ scanId: SID, deps: h.deps, concurrency: 3 })
    expect(peak).toBeLessThanOrEqual(3)
    expect(peak).toBeGreaterThan(1)
  })
})

describe('exportScanPackets — every outcome is recorded, none dropped', () => {
  const names = ['a.pdf', 'b.docx', 'c.xlsx', 'd.pptx', 'e.pdf', 'f.pdf', 'broken.docx']

  it('records html fallback, per-file read failure, render refusal, drift, 409, unanalysable and not-in-index', async () => {
    const h = harness(names, {
      deps: {
        loadScanFiles: async () => names.map((file) => ({ file, status: file === 'broken.docx' ? 'error' : 'uncertain', score: 70, issues: [] })),
        buildFileData: async ({ file }) => {
          if (file.file === 'b.docx') return { facts: null, factsError: 'HTTP 503 from report-facts' }
          if (file.file === 'd.pptx') return { facts: factsFor('d.pptx', 2), factsDigest: digestOf('d.pptx', 2) }   // moved since the index
          return { file: file.file, facts: factsFor(file.file), factsDigest: digestOf(file.file) }
        },
        renderBlob: async (a) => {
          if (a.file === 'a.pdf') return { ok: false, status: 503, fallback: 'html', format: 'html', htmlBlob: new Blob(['<html>a</html>'], { type: 'text/html' }), message: 'The PDF service was not available, so the report was produced as accessible HTML instead of PDF.' }
          if (a.file === 'c.xlsx') return { ok: false, status: 422, fallback: 'none', message: 'The report could not be rendered because its content was not accepted.' }
          if (a.file === 'e.pdf') return { ok: false, status: 409, regenerate: true, fallback: 'none', message: 'stale' }
          return { ok: true, format: 'pdf', blob: new Blob(['%PDF']) }
        },
      },
    })
    const res = await exportScanPackets({ scanId: SID, files: [{ file: 'on-screen-only.pdf' }], deps: h.deps })
    const by = Object.fromEntries(res.rows.map((r) => [r.file, r]))
    expect(by['a.pdf']).toMatchObject({ status: 'included', format: 'html', archivePath: 'packets/a.pdf.html' })
    expect(by['b.docx']).toMatchObject({ status: 'failed' })
    expect(by['b.docx'].reason).toMatch(/evidence could not be read \(HTTP 503 from report-facts\)/)
    expect(by['c.xlsx'].reason).toBe('HTTP 422: The report could not be rendered because its content was not accepted.')
    expect(by['d.pptx']).toMatchObject({ status: 'changed', archivePath: null })
    expect(by['d.pptx'].reason.startsWith(CHANGED_REASON)).toBe(true)
    expect(by['e.pdf']).toMatchObject({ status: 'changed', archivePath: null })
    expect(by['f.pdf']).toMatchObject({ status: 'included', format: 'pdf' })
    expect(by['broken.docx']).toMatchObject({ status: 'failed', reason: UNANALYSABLE_REASON })
    expect(by['on-screen-only.pdf']).toMatchObject({ status: 'notInIndex' })
    expect(h.renders.map((r) => r.file)).not.toContain('d.pptx')        // drift is never rendered
    expect(res.complete).toBe(false)
    expect(res.ok).toBe(false)
    expect(res.message).toMatch(/^Exported 2 of 8 documents\. INCOMPLETE:/)

    const zip = await readZip(h.downloads[0].blob)
    expect(Object.keys(zip.files).filter((n) => n.startsWith('packets/') && !zip.files[n].dir).sort()).toEqual(['packets/a.pdf.html', 'packets/f.pdf.pdf'])
    const rows = csvRows(await zip.file('index.csv').async('string'))
    expect(rows.map((r) => r['Original name'])).toEqual([...names, 'on-screen-only.pdf'])
    const status = Object.fromEntries(rows.map((r) => [r['Original name'], r.Status]))
    expect(status['a.pdf']).toBe('included')
    expect(rows.find((r) => r['Original name'] === 'a.pdf').Format).toBe('html-fallback')
    expect(status['c.xlsx']).toMatch(/^failed:HTTP 422/)
    expect(status['d.pptx']).toBe('changed-during-export')
    expect(status['on-screen-only.pdf']).toBe('not in index')
    expect(rows.find((r) => r['Original name'] === 'a.pdf')['Findings open']).toBe('not recorded')
    const html = await zip.file('index.html').async('string')
    expect(html).toMatch(/INCOMPLETE\./)
    // the index is also offered on its own
    expect(res.downloads.map((d) => d.label)).toEqual(['Master index (HTML)', 'Master index (CSV)'])
  })

  it('an index that could not be read produces no archive and says why', async () => {
    const h = harness(names, { server: { failPageAt: 0 } })
    const res = await exportScanPackets({ scanId: SID, deps: h.deps })
    expect(res.ok).toBe(false)
    expect(res.message).toMatch(/report index could not be read/)
    expect(h.downloads).toHaveLength(0)
  })

  it('an index that stopped part-way still exports what it has, and the verdict names the gap', async () => {
    const many = Array.from({ length: 5 }, (_, i) => `d${i}.pdf`)
    const h = harness(many, { server: { failPageAt: 2 }, deps: { loadIndex: ({ onProgress }) => loadScanReportFacts(SID, { getScanReportFacts: server(many, { failPageAt: 2 }).getScanReportFacts, limit: 2, onProgress }) } })
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, files: many.map((file) => ({ file })) })
    expect(res.rows.filter((r) => r.status === 'included')).toHaveLength(2)
    // the three documents on screen that the index never reached are listed, and not as "absent"
    const unread = res.rows.filter((r) => r.status === 'notInIndex')
    expect(unread.map((r) => r.file)).toEqual(['d2.pdf', 'd3.pdf', 'd4.pdf'])
    expect(unread[0].reason).toMatch(/may be in the part not read/)
    expect(res.complete).toBe(false)
    expect(res.message).toMatch(/scan index was not read completely: The per-document index stopped after 2 of 5 documents/)
  })
})

describe('exportScanPackets — cancellation', () => {
  it('stops, names exactly which documents are in the archive and which are not, and never says complete', async () => {
    const names = Array.from({ length: 30 }, (_, i) => `doc-${i}.pdf`)
    const ac = new AbortController()
    let rendered = 0
    const h = harness(names, {
      deps: {
        renderBlob: async (a) => {
          rendered += 1
          if (rendered === 5) ac.abort()
          await new Promise((r) => setTimeout(r, 1))
          if (a.signal?.aborted) return { ok: false, aborted: true, fallback: 'none', message: 'cancelled' }
          return { ok: true, format: 'pdf', blob: new Blob(['%PDF']) }
        },
      },
    })
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, signal: ac.signal, concurrency: 2 })
    expect(res.cancelled).toBe(true)
    expect(res.ok).toBe(false)
    expect(res.complete).toBe(false)
    const included = res.rows.filter((r) => r.status === 'included')
    const skipped = res.rows.filter((r) => r.status === 'cancelled')
    expect(included.length + skipped.length).toBe(30)
    expect(skipped.length).toBeGreaterThan(20)
    expect(res.message).toMatch(new RegExp(`^Cancelled\\. ${included.length} of 30 documents were exported; ${skipped.length} were not`))
    expect(res.message).toMatch(/INCOMPLETE/)
    const zip = await readZip(h.downloads.at(-1).blob)
    const rows = csvRows(await zip.file('index.csv').async('string'))
    expect(rows.filter((r) => r.Status === 'skipped:cancelled')).toHaveLength(skipped.length)
    const packets = Object.keys(zip.files).filter((n) => n.startsWith('packets/') && !zip.files[n].dir)
    expect(packets.sort()).toEqual(included.map((r) => r.archivePath).sort())
    expect(await zip.file('index.html').async('string')).toMatch(/The export was cancelled/)
  })

  it('cancelled before the index is read: nothing is downloaded', async () => {
    const ac = new AbortController(); ac.abort()
    const h = harness(['a.pdf'])
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, signal: ac.signal })
    expect(res).toMatchObject({ ok: false, cancelled: true })
    expect(h.downloads).toHaveLength(0)
  })
})
