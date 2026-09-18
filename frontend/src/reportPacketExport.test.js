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

// A paged server over `names`, in the contract-3 shape. `state` is live: a test may move the
// evidence (digest, document list) mid-export, or make the final fresh read fail or hang.
function server(initialNames, { indexDigest = 'f'.repeat(64), failPageAt = null } = {}) {
  const calls = []
  const state = { names: initialNames, digest: indexDigest, finalRead: null }
  const getScanReportFacts = async (sid, { offset = 0, limit = 200, digest, signal } = {}) => {
    calls.push({ offset, limit, digest, signal: signal ?? null })
    const names = state.names
    const indexDigest = state.digest
    if (limit === 1 && state.finalRead) return state.finalRead({ signal })
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
  return { getScanReportFacts, calls, state }
}
// The one fresh final read (contract 7): offset 0, limit 1, no digest.
const finalReads = (srv) => srv.calls.filter((c) => c.offset === 0 && c.limit === 1 && !c.digest)

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
    getScanReportFacts: srv.getScanReportFacts,
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
    const paging = h.srv.calls.filter((c) => c.limit === 200)
    expect(paging.map((c) => c.offset)).toEqual([0, 200, 400])
    expect(paging.slice(1).every((c) => c.digest === 'f'.repeat(64))).toBe(true)
    expect(finalReads(h.srv)).toHaveLength(1)                  // …and exactly one fresh final read
    expect(h.srv.calls.at(-1)).toMatchObject({ offset: 0, limit: 1 })
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

// ── Contract 7: ONE fresh, uncached read of the scan index after every packet is made ─────────
// Parent review item 9: the exporter read the index once, packed, and then called the archive
// complete with no look at whether the evidence was still the snapshot it packed. These pin the
// final check and what the master index says about it.
describe('exportScanPackets — the final fresh check (contract 7)', () => {
  const three = ['a.pdf', 'b.docx', 'c.xlsx']
  const SNAP = 'f'.repeat(64)
  const MOVED = 'e'.repeat(64)
  const clock = () => { let t = Date.parse('2026-09-18T09:00:00Z'); return () => new Date((t += 1000)) }
  const indexOf = async (h) => {
    const zip = await readZip(h.downloads.at(-1).blob)
    return { csv: csvRows(await zip.file('index.csv').async('string')), html: await zip.file('index.html').async('string') }
  }

  it('(a) unchanged evidence: COMPLETE, verified current at the check time', async () => {
    const h = harness(three)
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, now: clock() })
    expect(finalReads(h.srv)).toHaveLength(1)
    expect(finalReads(h.srv)[0].signal).toBeInstanceOf(AbortSignal)      // bounded, abortable
    expect(h.srv.calls.at(-1)).toMatchObject({ offset: 0, limit: 1 })   // after every per-document read
    expect(res.finalCheck).toMatchObject({ status: 'verified', digest: SNAP, snapshotDigest: SNAP })
    expect(res.finalCheck.checkedAt).toMatch(/^2026-09-18T09:00:\d\d\.\d{3}Z$/)
    expect(res.complete).toBe(true)
    expect(res.ok).toBe(true)
    expect(res.message).toContain(`verified current at ${res.finalCheck.checkedAt}`)
    const { csv, html } = await indexOf(h)
    expect(html).toMatch(/COMPLETE \(verified current\)/)
    expect(html).toMatch(/Final check status<\/dt><dd>verified current/)
    expect(html).toContain(res.finalCheck.checkedAt)
    expect(csv.every((r) => r['Final check'] === 'verified current' && r['Final check digest'] === SNAP)).toBe(true)
    expect(csv.every((r) => r['Export verdict'] === 'COMPLETE (verified current)')).toBe(true)
  })

  it('(b) evidence moved after the first packet while the last was pending: not complete, "changed during export", both digests', async () => {
    const h = harness(three)
    const inner = h.deps.renderBlob
    let n = 0
    h.deps.renderBlob = async (a) => {
      n += 1
      // a reviewer decision on a.pdf, recorded after its packet was drawn: the scan digest moves,
      // but no per-file check can see it any more (a.pdf is done; b and c are untouched)
      if (n === 1) h.srv.state.digest = MOVED
      return inner(a)
    }
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, concurrency: 1, now: clock() })
    expect(res.rows.every((r) => r.status === 'included')).toBe(true)   // every packet is still there
    expect(res.finalCheck).toMatchObject({ status: 'changed', digest: MOVED, snapshotDigest: SNAP })
    expect(res.complete).toBe(false)
    expect(res.ok).toBe(false)
    expect(res.incomplete).toBe(true)
    expect(res.snapshotOnly).toBe(true)
    expect(res.message).toMatch(/SNAPSHOT ONLY/)
    expect(res.message).toMatch(/changed during the export/)
    expect(res.message).toContain(SNAP)
    expect(res.message).toContain(MOVED)
    const { csv, html } = await indexOf(h)
    expect(html).toMatch(/SNAPSHOT ONLY — EVIDENCE CHANGED DURING EXPORT/)
    expect(html).not.toMatch(/COMPLETE \(verified current\)/)
    expect(html).toContain(SNAP)
    expect(html).toContain(MOVED)
    expect(html).toContain('2026-09-17T10:00:00Z')                       // the snapshot's own time
    expect(csv.every((r) => r['Final check'] === 'evidence changed during export' && r['Final check digest'] === MOVED)).toBe(true)
    expect(csv[0]['Snapshot built at']).toBe('2026-09-17T10:00:00Z')
    // the standalone index says the same
    expect(await res.downloads[0].blob.text()).toMatch(/SNAPSHOT ONLY — EVIDENCE CHANGED DURING EXPORT/)
  })

  it('(c) a document added or deleted after the snapshot: the new document count is stated', async () => {
    const cases = [
      [(s) => { s.names = [...three, 'late.pdf'] }, /now lists 4 documents; the snapshot listed 3/],
      [(s) => { s.names = three.slice(0, 2) }, /now lists 2 documents; the snapshot listed 3/],
    ]
    for (const [change, want] of cases) {
      const h = harness(three)
      const inner = h.deps.renderBlob
      h.deps.renderBlob = async (a) => { change(h.srv.state); h.srv.state.digest = MOVED; return inner(a) }
      const res = await exportScanPackets({ scanId: SID, deps: h.deps, now: clock() })
      expect(res.finalCheck.status).toBe('changed')
      expect(res.complete).toBe(false)
      expect(res.message).toMatch(want)
      expect((await indexOf(h)).html).toMatch(want)
    }
  })

  it('(d) the final read fails on the network: "final check failed", and the archive is still downloaded', async () => {
    const h = harness(three)
    h.srv.state.finalRead = async () => { throw new TypeError('Failed to fetch') }
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, now: clock() })
    expect(res.finalCheck).toMatchObject({ status: 'failed', digest: null })
    expect(res.finalCheck.reason).toMatch(/Failed to fetch/)
    expect(res.complete).toBe(false)
    expect(res.message).toMatch(/SNAPSHOT ONLY/)
    expect(res.message).toMatch(/final check failed/i)
    expect(h.downloads).toHaveLength(1)
    expect(res.rows.every((r) => r.status === 'included')).toBe(true)
    expect(res.downloads).toHaveLength(2)
    const { html, csv } = await indexOf(h)
    expect(html).toMatch(/SNAPSHOT ONLY — FINAL CHECK FAILED/)
    expect(csv[0]['Final check']).toBe('final check failed')
    expect(csv[0]['Final check digest']).toBe('not recorded')
  })

  it('(d) the final read hangs: it is abandoned after the bounded timeout (20 s), never polled', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      const h = harness(three)
      let seen = null
      h.srv.state.finalRead = ({ signal }) => { seen = signal; return new Promise(() => {}) }
      const res = await exportScanPackets({
        scanId: SID, deps: h.deps, now: clock(),
        onProgress: (p) => { if (p.phase === 'final-check') queueMicrotask(() => vi.advanceTimersByTime(20000)) },
      })
      expect(res.finalCheck.status).toBe('failed')
      expect(res.finalCheck.reason).toMatch(/did not answer within 20 s/)
      expect(seen?.aborted).toBe(true)                                   // the request itself was aborted
      expect(finalReads(h.srv)).toHaveLength(1)                          // one attempt, no retry loop
      expect(res.complete).toBe(false)
      expect(h.downloads).toHaveLength(1)
    } finally { vi.useRealTimers() }
  })

  it('(e) HTML fallback after a transport failure never stands in for the final check', async () => {
    const htmlOnly = async (a) => ({ ok: false, status: 0, fallback: 'html', format: 'html', htmlBlob: new Blob([`<html>${a.file}</html>`], { type: 'text/html' }), message: 'The PDF service could not be reached, so the report was produced as accessible HTML instead of PDF.' })
    // the network is down for the final read too
    const h = harness(three, { deps: { renderBlob: htmlOnly } })
    h.srv.state.finalRead = async () => { throw new TypeError('Failed to fetch') }
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, now: clock() })
    expect(res.rows.every((r) => r.status === 'included' && r.format === 'html')).toBe(true)
    expect(res.complete).toBe(false)
    expect(res.finalCheck.status).toBe('failed')
    const { csv } = await indexOf(h)
    for (const r of csv) {
      expect(r['Server re-verified at render']).toMatch(/^no — HTML fallback/)
      expect(r['Facts read at']).toMatch(/^2026-09-18T09:/)             // when THIS document's facts were read
      expect(r['Per-file facts digest']).toBe(digestOf(r['Original name']))
    }
    // and an exporter with no way to re-read the index cannot call HTML-only packets current either
    const h2 = harness(three, { deps: { renderBlob: htmlOnly, getScanReportFacts: undefined } })
    const res2 = await exportScanPackets({ scanId: SID, deps: h2.deps, now: clock() })
    expect(res2.finalCheck.status).toBe('not_performed')
    expect(res2.complete).toBe(false)
    expect((await indexOf(h2)).html).toMatch(/SNAPSHOT ONLY — FINAL CHECK NOT PERFORMED/)
    // PDFs the server accepted say so
    const h3 = harness(three)
    await exportScanPackets({ scanId: SID, deps: h3.deps, now: clock() })
    expect((await indexOf(h3)).csv[0]['Server re-verified at render']).toMatch(/^yes/)
  })

  it('(f) a cancelled export does not run (or wait on) the final check, and says not_performed', async () => {
    const names = Array.from({ length: 12 }, (_, i) => `doc-${i}.pdf`)
    const ac = new AbortController()
    const h = harness(names)
    const inner = h.deps.renderBlob
    let n = 0
    h.deps.renderBlob = async (a) => { if (++n === 3) ac.abort(); return inner(a) }
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, signal: ac.signal, concurrency: 1, now: clock() })
    expect(res.cancelled).toBe(true)
    expect(res.finalCheck.status).toBe('not_performed')
    expect(res.finalCheck.reason).toMatch(/cancelled/)
    expect(finalReads(h.srv)).toHaveLength(0)
    expect(res.message).toMatch(/final check not performed/i)
    expect((await indexOf(h)).html).toMatch(/Final check status<\/dt><dd>final check not performed/)
  })

  it('(f) cancel while the final read is in flight: abandoned at once, not awaited', async () => {
    const ac = new AbortController()
    const h = harness(three)
    h.srv.state.finalRead = () => new Promise(() => {})                // would hang forever
    const res = await exportScanPackets({
      scanId: SID, deps: h.deps, signal: ac.signal, now: clock(),
      onProgress: (p) => { if (p.phase === 'final-check') queueMicrotask(() => ac.abort()) },
    })
    expect(res.finalCheck.status).toBe('not_performed')
    expect(res.finalCheck.reason).toMatch(/cancelled while the final check was running/)
    expect(res.complete).toBe(false)
    expect(h.downloads).toHaveLength(1)
  })

  it('parts downloaded before the final check are named in the final index as snapshot evidence', async () => {
    const names = Array.from({ length: 5 }, (_, i) => `p${i}.pdf`)
    const h = harness(names)
    const inner = h.deps.renderBlob
    h.deps.renderBlob = async (a) => { h.srv.state.digest = MOVED; return inner(a) }
    const res = await exportScanPackets({ scanId: SID, deps: h.deps, partMaxFiles: 2, concurrency: 1, now: clock() })
    expect(res.parts.map((p) => p.n)).toEqual([1, 2, 3])
    expect(res.finalCheck.partsBeforeFinalCheck).toEqual([1, 2])
    const { html } = await indexOf(h)
    expect(html).toMatch(/Parts 1–2 were generated and downloaded before the final check/)
    expect(html).toMatch(/reflect the snapshot/)
    const first = await readZip(h.downloads[0].blob)
    expect(await first.file('README-part-1.txt').async('string')).toMatch(/master index .*states whether/i)
  })
})
