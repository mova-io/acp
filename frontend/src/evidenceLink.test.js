/**
 * Finding deep links (Contract 2): exact, encoded, never fabricated, never unsafe.
 *
 * The file names below are the ones that break a hand-built query string — a nested SharePoint
 * path, `#`, `%`, `&`, `+`, spaces and non-ASCII. Each must come back from parse exactly as it went
 * in, because the viewer resolves the record by these values: a `#` that truncates the file name
 * is a link to a different document.
 */
import { describe, it, expect } from 'vitest'
import { evidenceHref, parseEvidenceHref, absoluteAppHref, attachEvidenceLinks, trustedAppOrigin, fileEvidenceHref, isFileTarget, targetHref, captureEvidenceTarget } from './evidenceLink.js'
import { isSafeHref } from './reportEvidence.js'

const SHA = 'ab'.repeat(32)
const HARD_NAMES = [
  'Board/2026 Q1/minutes #2 & notes.docx',
  'a/b/c.docx',
  '100% plan+budget.xlsx',
  'Überblick – Ärzte/résumé 日本語.pdf',
  'x?view=evidence&scan=other.pdf',
  "quote's \"double\" <tag>.pptx",
]

describe('evidenceHref / parseEvidenceHref round trip', () => {
  it.each(HARD_NAMES)('round-trips %s exactly', (file) => {
    const href = evidenceHref({ scanId: 'scan/1 #x', file, findingId: 'f&1=2', sha256: SHA, version: 'source' })
    expect(href.startsWith('/?view=evidence&')).toBe(true)
    // Nothing a browser or report renderer would re-interpret survives unencoded.
    expect(href).not.toMatch(/[\s#<>"']/)
    expect(isSafeHref(href)).toBe(true)
    expect(parseEvidenceHref(href)).toEqual({
      scanId: 'scan/1 #x', file, findingId: 'f&1=2', changeId: null, sha256: SHA, version: 'source',
    })
    // location.search has the leading "?" and no path — the form App.jsx passes.
    expect(parseEvidenceHref(href.slice(1))).toEqual(parseEvidenceHref(href))
  })

  it('links a saved change by its own id', () => {
    const href = evidenceHref({ scanId: 's', file: 'd.pdf', changeId: 'd.pdf::1.1.1::0', version: 'corrected' })
    expect(parseEvidenceHref(href)).toMatchObject({ changeId: 'd.pdf::1.1.1::0', findingId: null, version: 'corrected', sha256: null })
  })

  it('never fabricates a link when the scan, the file or the record id is missing', () => {
    expect(evidenceHref({ file: 'd.pdf', findingId: 'f' })).toBeNull()
    expect(evidenceHref({ scanId: 's', findingId: 'f' })).toBeNull()
    expect(evidenceHref({ scanId: 's', file: 'd.pdf' })).toBeNull()
    expect(evidenceHref({ scanId: 's', file: '', findingId: 'f' })).toBeNull()
    expect(evidenceHref({ scanId: 's', file: 'd.pdf', findingId: '' })).toBeNull()
    expect(evidenceHref({})).toBeNull()
    expect(evidenceHref()).toBeNull()
  })

  it('refuses a link that names both a finding and a change — it would not say which', () => {
    expect(evidenceHref({ scanId: 's', file: 'd.pdf', findingId: 'f', changeId: 'c' })).toBeNull()
    expect(parseEvidenceHref('?view=evidence&scan=s&file=d.pdf&finding=f&change=c')).toBeNull()
  })

  it('drops a malformed sha or version rather than inventing one', () => {
    const href = evidenceHref({ scanId: 's', file: 'd.pdf', findingId: 'f', sha256: 'not-a-sha', version: 'latest' })
    expect(href).toBe('/?view=evidence&scan=s&file=d.pdf&finding=f')
    expect(parseEvidenceHref('?view=evidence&scan=s&file=d.pdf&finding=f&sha=zz&version=newest'))
      .toEqual({ scanId: 's', file: 'd.pdf', findingId: 'f', changeId: null, sha256: null, version: null })
    // Upper-case hex is the same digest.
    expect(parseEvidenceHref(`?view=evidence&scan=s&file=d.pdf&finding=f&sha=${SHA.toUpperCase()}`).sha256).toBe(SHA)
  })

  it('carries no token or credential, whatever it is handed', () => {
    const href = evidenceHref({ scanId: 's', file: 'd.pdf', findingId: 'f', token: 'secret', access_token: 'x' })
    expect(href).not.toMatch(/secret|token/)
  })

  it('is null for anything that is not a well-formed evidence link', () => {
    for (const bad of [null, undefined, 42, '', '?', '?view=overview&scan=s&file=d&finding=f',
      '?scan=s&file=d&finding=f', '?view=evidence&file=d&finding=f', '?view=evidence&scan=s&finding=f',
      'https://evil.example/?view=evidence&scan=s&file=d&finding=f',
      '/other/?view=evidence&scan=s&file=d&finding=f',
      '?view=evidence&scan=s&file=a.pdf&file=b.pdf&finding=f',     // which file?
      '?view=evidence&scan=s&file=d&finding=f&finding=g',           // which finding?
      '?view=evidence&scan=s&file=d%0Ax&finding=f',                 // control character in a name
    ]) expect(parseEvidenceHref(bad)).toBeNull()
  })
})

describe('fileEvidenceHref — a link to one whole document (R-C2)', () => {
  it.each(HARD_NAMES)('round-trips %s as a document-level target', (file) => {
    const href = fileEvidenceHref({ scanId: 'scan/1 #x', file, sha256: SHA, version: 'corrected' })
    expect(href.startsWith('/?view=evidence&scan=')).toBe(true)
    expect(href).not.toMatch(/[\s#<>"']|finding=|change=/)
    expect(isSafeHref(href)).toBe(true)
    const t = parseEvidenceHref(href)
    expect(t).toEqual({ scanId: 'scan/1 #x', file, findingId: null, changeId: null, sha256: SHA, version: 'corrected' })
    expect(isFileTarget(t)).toBe(true)
    expect(targetHref(t)).toBe(href)
  })

  it('is null without a scan or a file, and never stands in for a record link', () => {
    expect(fileEvidenceHref({ file: 'd.pdf' })).toBeNull()
    expect(fileEvidenceHref({ scanId: 's' })).toBeNull()
    expect(fileEvidenceHref({ scanId: 's', file: '' })).toBeNull()
    expect(fileEvidenceHref()).toBeNull()
    // evidenceHref without a record id is still null — a record link is never degraded to this
    expect(evidenceHref({ scanId: 's', file: 'd.pdf' })).toBeNull()
    expect(isFileTarget(parseEvidenceHref(evidenceHref({ scanId: 's', file: 'd.pdf', findingId: 'f' })))).toBe(false)
  })

  it('a record id that is present but unusable is refused, not read as "the whole document"', () => {
    for (const bad of ['?view=evidence&scan=s&file=d&finding=', '?view=evidence&scan=s&file=d&change=',
      '?view=evidence&scan=s&file=d&finding=a%0Ab', '?view=evidence&scan=s&file=d&change=a%0Ab']) {
      expect(parseEvidenceHref(bad)).toBeNull()
    }
  })

  it('survives sign-in like a record link does', () => {
    const store = new Map()
    const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }
    const href = fileEvidenceHref({ scanId: 's', file: 'a/b #c.docx' })
    expect(captureEvidenceTarget({ search: href.slice(1), storage, now: 1000 }).target).toMatchObject({ file: 'a/b #c.docx', findingId: null, changeId: null })
    const back = captureEvidenceTarget({ search: '', storage, now: 2000 })
    expect(back.restoredHref).toBe(href)
    expect(isFileTarget(back.target)).toBe(true)
  })

  it('lights up the packet index "Open in ACP" column with the REAL module (rf-C appLinkFor)', async () => {
    const { appLinkFor } = await import('./reportPacketLive.js')
    const out = appLinkFor('s-packets', 'Board/a #1.pdf', { origin: 'https://acp.example.com' })
    expect(out).toEqual({ href: 'https://acp.example.com/?view=evidence&scan=s-packets&file=Board%2Fa+%231.pdf', note: null })
    expect(parseEvidenceHref(new URL(out.href).search)).toMatchObject({ scanId: 's-packets', file: 'Board/a #1.pdf', findingId: null, changeId: null })
  })
})

describe('absoluteAppHref — only for a trusted origin, only for an app path', () => {
  const rel = evidenceHref({ scanId: 's', file: 'a/b #c.docx', findingId: 'f' })

  it('absolutizes against an https origin', () => {
    const abs = absoluteAppHref(rel, { origin: 'https://acp.example.org' })
    expect(abs).toBe(`https://acp.example.org${rel}`)
    expect(parseEvidenceHref(new URL(abs).search).file).toBe('a/b #c.docx')
  })

  it('allows plain http only on a loopback host', () => {
    expect(absoluteAppHref(rel, { origin: 'http://localhost:5173' })).toBe(`http://localhost:5173${rel}`)
    expect(absoluteAppHref(rel, { origin: 'http://127.0.0.1:8077' })).toBe(`http://127.0.0.1:8077${rel}`)
    expect(absoluteAppHref(rel, { origin: 'http://acp.example.org' })).toBeNull()
    expect(absoluteAppHref(rel, { origin: 'http://10.0.0.5' })).toBeNull()
  })

  it('refuses an untrusted origin — a relative link in a downloaded file resolves to the disk', () => {
    for (const origin of [null, '', 'null', 'file://', 'file:///Users/me', 'data:text/html,x', 'javascript:alert(1)', 'not a url', 'https://user:pw@acp.example.org']) {
      expect(absoluteAppHref(rel, { origin })).toBeNull()
    }
  })

  it('refuses anything that is not a same-origin "/?…" app path', () => {
    const origin = 'https://acp.example.org'
    for (const bad of ['javascript:alert(1)', 'data:text/html,<b>x</b>', '//evil.example/?view=evidence',
      'https://evil.example/?view=evidence', '/admin', '/?x=1 onmouseover=alert(1)', '/?x="y"', '/\\evil.example', null, 42]) {
      expect(absoluteAppHref(bad, { origin })).toBeNull()
    }
  })

  it('names a trusted origin without building a link', () => {
    expect(trustedAppOrigin('https://acp.example.org/some/path')).toBe('https://acp.example.org')
    expect(trustedAppOrigin('http://evil.example')).toBeNull()
  })
})

describe('attachEvidenceLinks', () => {
  const SRC = '1'.repeat(64)
  const COR = '2'.repeat(64)
  const facts = {
    factsDigest: 'd',
    identity: { scanId: 'scan 1', file: 'Board/Q1 #2.pdf', sourceSha256: SRC, correctedSha256: COR },
    findings: [
      { id: 'f1', ruleId: 'img-alt', location: { label: 'Page 3', kind: 'page', page: 3 } },
      { id: 'f2', ruleId: 'lang', location: null },
      { ruleId: 'no-id', location: { label: 'Page 1', page: 1 } },
    ],
    savedChanges: [
      { id: 'c1', artifactSha256: null, location: null },
      { id: 'c2', artifactSha256: '3'.repeat(64), location: { label: 'Image 1', kind: 'object', objectId: 'image:1' } },
    ],
  }

  it('puts the exact record link on every finding and change that has an id', () => {
    const out = attachEvidenceLinks(facts)
    expect(parseEvidenceHref(out.findings[0].location.href)).toEqual({
      scanId: 'scan 1', file: 'Board/Q1 #2.pdf', findingId: 'f1', changeId: null, sha256: SRC, version: 'source',
    })
    expect(out.findings[0].location).toMatchObject({ label: 'Page 3', kind: 'page', page: 3 })
    // No location recorded → still clickable, and the viewer says "Location not recorded".
    expect(out.findings[1].location).toEqual({ label: null, kind: null, href: expect.stringContaining('finding=f2') })
    // No id → no link at all: a link to "some finding" would be a guess.
    expect(out.findings[2].location.href).toBeUndefined()
    expect(parseEvidenceHref(out.savedChanges[0].location.href)).toMatchObject({ changeId: 'c1', sha256: COR, version: 'corrected' })
    expect(parseEvidenceHref(out.savedChanges[1].location.href)).toMatchObject({ changeId: 'c2', sha256: '3'.repeat(64) })
    expect(out.savedChanges[1].location.objectId).toBe('image:1')
  })

  it('does not mutate the server facts, and adds nothing without a scan/file identity', () => {
    const before = JSON.stringify(facts)
    attachEvidenceLinks(facts)
    expect(JSON.stringify(facts)).toBe(before)
    const anon = attachEvidenceLinks({ ...facts, identity: { file: 'x.pdf' } })
    expect(anon.findings.map((f) => f.location?.href)).toEqual([undefined, undefined, undefined])
    expect(attachEvidenceLinks(null)).toBeNull()
  })

  it('a non-sha-256 source checksum (Drive md5) is not put in the link as a version', () => {
    const out = attachEvidenceLinks({ ...facts, identity: { ...facts.identity, sourceSha256: null } })
    expect(parseEvidenceHref(out.findings[0].location.href)).toMatchObject({ sha256: null, version: null })
  })
})
