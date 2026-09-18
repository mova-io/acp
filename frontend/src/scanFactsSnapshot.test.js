/**
 * Contract 3, client side: the pages of one scan index come from ONE snapshot, or the index stops
 * and says why. Never mixed.
 *
 * The failure this pins: paging a big estate while a re-assessment lands. Page 1 is built from
 * snapshot X, page 2 from snapshot Y, and a document that moved across the page boundary is
 * counted twice or not at all — a report describing a state the estate was never in. The server
 * answers 409 when `digest` no longer matches; the client must also check each page's own digest
 * and offset, because a server (or proxy cache) that ignores the parameter would otherwise go
 * unnoticed.
 */
import { describe, it, expect, vi } from 'vitest'
import { loadScanReportFacts, FACTS_CHANGED_WHILE_PAGING } from './fileReportData.js'

const pageOf = (offset, limit, total, digest = 'X') => ({
  factsVersion: 1, factsDigest: digest, filesTotal: total, offset, limit,
  complete: offset + limit >= total,
  snapshot: { factsDigest: digest, filesTotal: total, builtAt: '2026-09-17T12:00:00Z' },
  files: Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) => ({ file: `doc-${offset + i}.pdf`, factsDigest: `f-${offset + i}` })),
})

describe('loadScanReportFacts — one snapshot or a stated stop', () => {
  it('sends the first page digest with every later page, and none with the first', async () => {
    const get = vi.fn(async (_s, { offset, limit }) => pageOf(offset, limit, 25))
    const got = await loadScanReportFacts('s1', { getScanReportFacts: get, limit: 10 })
    expect(get.mock.calls.map((c) => c[1])).toEqual([
      { offset: 0, limit: 10 },
      { offset: 10, limit: 10, digest: 'X' },
      { offset: 20, limit: 10, digest: 'X' },
    ])
    expect(got.complete).toBe(true)
    expect(got.files.map((f) => f.file)).toEqual(Array.from({ length: 25 }, (_, i) => `doc-${i}.pdf`))
    expect(got.incompleteReason).toBeNull()
  })

  it('a 409 from the server stops the index with the changed-evidence reason', async () => {
    const get = vi.fn(async (_s, { offset, limit, digest }) => {
      if (digest) { const e = new Error('report evidence changed while paging; restart the export'); e.status = 409; throw e }
      return pageOf(offset, limit, 25)
    })
    const got = await loadScanReportFacts('s1', { getScanReportFacts: get, limit: 10 })
    expect(got.complete).toBe(false)
    expect(got.incompleteReason).toContain(FACTS_CHANGED_WHILE_PAGING)
    expect(got.incompleteReason).toMatch(/Stopped after 10 of 25 documents/)
    expect(got.files).toHaveLength(10)
  })

  it('a later page with a different digest is DISCARDED, not appended', async () => {
    // A server that ignored `digest` and answered from a newer snapshot.
    const get = vi.fn(async (_s, { offset, limit }) => pageOf(offset, limit, 25, offset >= 10 ? 'Y' : 'X'))
    const got = await loadScanReportFacts('s1', { getScanReportFacts: get, limit: 10 })
    expect(got.complete).toBe(false)
    expect(got.incompleteReason).toContain(FACTS_CHANGED_WHILE_PAGING)
    expect(got.files.map((f) => f.file)).toEqual(Array.from({ length: 10 }, (_, i) => `doc-${i}.pdf`))
    expect(got.facts.factsDigest).toBe('X')
    expect(get).toHaveBeenCalledTimes(2)   // stopped, did not keep paging the other snapshot
  })

  it('a page that answers for a different offset is refused — no gaps, no repeats', async () => {
    const get = vi.fn(async (_s, { offset, limit }) => pageOf(offset === 10 ? 5 : offset, limit, 25))
    const got = await loadScanReportFacts('s1', { getScanReportFacts: get, limit: 10 })
    expect(got.complete).toBe(false)
    expect(got.incompleteReason).toMatch(/answered for offset 5 instead of 10/)
    expect(got.files).toHaveLength(10)
    expect(new Set(got.files.map((f) => f.file)).size).toBe(10)
  })

  it('a first page without a digest cannot be joined to anything, and says so', async () => {
    const get = vi.fn(async (_s, { offset, limit }) => ({ ...pageOf(offset, limit, 25), factsDigest: null }))
    const got = await loadScanReportFacts('s1', { getScanReportFacts: get, limit: 10 })
    expect(get).toHaveBeenCalledTimes(1)
    expect(got.complete).toBe(false)
    expect(got.incompleteReason).toMatch(/did not name the evidence snapshot/)
  })

  it('a single complete page needs no digest to be whole', async () => {
    const get = vi.fn(async (_s, { offset, limit }) => ({ ...pageOf(offset, limit, 3), factsDigest: null }))
    const got = await loadScanReportFacts('s1', { getScanReportFacts: get, limit: 10 })
    expect(got.complete).toBe(true)
    expect(got.incompleteReason).toBeNull()
  })

  it('api.getScanReportFacts puts the digest on the wire only when given', async () => {
    vi.resetModules()
    vi.doMock('./sim.js', async (importActual) => ({ ...(await importActual()), SIM: false }))
    const seen = []
    const realFetch = globalThis.fetch
    globalThis.fetch = vi.fn(async (url) => { seen.push(String(url)); return new Response(JSON.stringify(pageOf(0, 10, 3)), { status: 200, headers: { 'Content-Type': 'application/json' } }) })
    try {
      const api = await import('./api.js')
      await api.getScanReportFacts('s 1', { offset: 0, limit: 10 })
      await api.getScanReportFacts('s 1', { offset: 10, limit: 10, digest: 'a&b=c' })
    } finally {
      globalThis.fetch = realFetch
      vi.doUnmock('./sim.js')
    }
    expect(seen[0]).toMatch(/\/scans\/s%201\/report-facts\?offset=0&limit=10$/)
    expect(seen[1]).toMatch(/\/scans\/s%201\/report-facts\?offset=10&limit=10&digest=a%26b%3Dc$/)
  })

  it('api.getScanReportFacts asks for finding records only when told to, and forwards an abort signal', async () => {
    vi.resetModules()
    vi.doMock('./sim.js', async (importActual) => ({ ...(await importActual()), SIM: false }))
    const seen = []
    const realFetch = globalThis.fetch
    globalThis.fetch = vi.fn(async (url, init) => { seen.push({ url: String(url), init }); return new Response(JSON.stringify(pageOf(0, 10, 3)), { status: 200, headers: { 'Content-Type': 'application/json' } }) })
    const ctl = new AbortController()
    try {
      const api = await import('./api.js')
      await api.getScanReportFacts('s1', { offset: 0, limit: 10, includeFindings: true })
      await api.getScanReportFacts('s1', { offset: 10, limit: 10, digest: 'd', includeFindings: true, signal: ctl.signal })
      await api.getScanReportFacts('s1', { offset: 0, limit: 1 })
    } finally {
      globalThis.fetch = realFetch
      vi.doUnmock('./sim.js')
    }
    expect(seen[0].url).toMatch(/\/report-facts\?offset=0&limit=10&include=findings$/)
    expect(seen[1].url).toMatch(/\/report-facts\?offset=10&limit=10&digest=d&include=findings$/)
    expect(seen[1].init.signal).toBe(ctl.signal)
    expect(seen[2].url).toMatch(/\/report-facts\?offset=0&limit=1$/)
    expect('signal' in seen[2].init).toBe(false)
  })
})
