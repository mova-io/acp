// Archive paths and the master index for per-file packets (reportPacketArchive.js). The names here
// are real-world hostile: SharePoint paths, traversal, Windows device names, bidi overrides, and
// names that differ only by case — each of which an unzip tool would otherwise act on.
import { describe, it, expect } from 'vitest'
import {
  safeSegment, safeStem, assignArchiveStems, packetPath, archiveHref, pathSegments,
  indexCsv, indexHtml, completeness, statusLabel, comparisonText, reviewsText, extractionConflicts,
  MAX_SEGMENT_BYTES, MAX_PATH_CHARS, PACKET_ROOT, LONG_PATH_DIR, NOT_RECORDED,
} from './reportPacketArchive.js'

const utf8 = (s) => new TextEncoder().encode(s).length
const assertSafe = (path) => {
  expect(path.startsWith('/')).toBe(false)
  expect(path).not.toMatch(/^[A-Za-z]:/)
  expect(path.split('/')).not.toContain('..')
  expect(path.split('/')).not.toContain('.')
  expect(path).not.toMatch(/\\/)
  // eslint-disable-next-line no-control-regex
  expect(path).not.toMatch(/[\u0000-\u001f\u007f<>:"|?*\u202a-\u202e\u2066-\u2069]/)
  for (const seg of path.split('/')) {
    expect(seg.length).toBeGreaterThan(0)
    expect(seg).not.toMatch(/[. ]$/)
    expect(seg).not.toMatch(/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$/i)
    expect(utf8(seg)).toBeLessThanOrEqual(MAX_SEGMENT_BYTES)
  }
}

describe('safe archive paths', () => {
  it.each([
    '../../etc/passwd',
    '..\\..\\Windows\\system32\\config',
    '/absolute/path/report.pdf',
    'C:\\Users\\me\\report.docx',
    'C:/Users/me/report.docx',
    './././a.docx',
    'a/../../b.docx',
    '....',
    '',
  ])('%j cannot escape the packet folder', (name) => {
    const stem = safeStem(name)
    assertSafe(`${PACKET_ROOT}/${stem}.pdf`)
    expect(stem.startsWith('..')).toBe(false)
  })

  it('traversal segments are dropped, not turned into directories', () => {
    expect(pathSegments('../../outside/escape.xlsx')).toEqual(['outside', 'escape.xlsx'])
    expect(safeStem('../../outside/escape.xlsx')).toBe('outside/escape.xlsx')
    expect(safeStem('C:\\Users\\me\\report.docx')).toBe('Users/me/report.docx')
  })

  it('keeps Unicode names (NFC) and nested folders', () => {
    const decomposed = 'Politik/U\u0308berblick – Richtlinie 報告.docx'   // Ü as U + combining diaeresis
    expect(safeStem(decomposed)).toBe('Politik/\u00dcberblick – Richtlinie 報告.docx')
    assertSafe(packetPath(safeStem(decomposed), 'pdf'))
  })

  it('removes control characters, bidi overrides and characters Windows forbids', () => {
    const s = safeSegment('inv\u202eoice\u0007<fdp>:"a|b?*.exe')
    expect(s).toBe('inv_oice__fdp___a_b__.exe')
  })

  it.each(['CON', 'con.pptx', 'NUL.txt', 'com1.pdf', 'LPT9', 'aux'])('prefixes reserved Windows name %s', (n) => {
    expect(safeSegment(n).startsWith('_')).toBe(true)
  })

  it('strips trailing dots and spaces (Windows would silently rename them)', () => {
    expect(safeSegment('report. . ')).toBe('report')
    expect(safeSegment(' . ')).toBe('_')
  })

  it('bounds a very long segment in UTF-8 bytes, keeps the extension, and disambiguates by hash', () => {
    const a = `${'Ü'.repeat(200)}-a.docx`
    const b = `${'Ü'.repeat(200)}-b.docx`
    const sa = safeSegment(a)
    const sb = safeSegment(b)
    expect(utf8(sa)).toBeLessThanOrEqual(MAX_SEGMENT_BYTES)
    expect(sa.endsWith('.docx')).toBe(true)
    expect(sa).not.toBe(sb)
    expect(safeSegment(a)).toBe(sa)          // deterministic
  })

  it('flattens a path too long for Windows extraction, deterministically', () => {
    const deep = Array.from({ length: 12 }, (_, i) => `Department folder number ${i}`).join('/') + '/final report.pdf'
    const stem = safeStem(deep)
    expect(stem.startsWith(`${LONG_PATH_DIR}/`)).toBe(true)
    expect(stem.endsWith('final report.pdf')).toBe(true)
    expect(`${PACKET_ROOT}/${stem}.pdf`.length).toBeLessThanOrEqual(MAX_PATH_CHARS)
    expect(safeStem(deep)).toBe(stem)
  })
})

describe('de-duplication', () => {
  it('names that collide case-insensitively get stable (2), (3) suffixes in index order', () => {
    const m = assignArchiveStems(['Reports/Q1 summary.pdf', 'reports/q1 SUMMARY.pdf', 'REPORTS/Q1 Summary.pdf'])
    expect([...m.values()]).toEqual(['Reports/Q1 summary.pdf', 'Reports/q1 SUMMARY (2).pdf', 'Reports/Q1 Summary (3).pdf'])
    const lower = [...m.values()].map((s) => s.toLowerCase())
    expect(new Set(lower).size).toBe(3)
  })

  it('names that only collide after sanitising are separated too', () => {
    const m = assignArchiveStems(['a/../b.docx', 'b.docx', 'x?.pdf', 'x*.pdf'])
    expect(m.get('a/../b.docx')).toBe('a/b.docx')
    expect(m.get('b.docx')).toBe('b.docx')
    expect(m.get('x?.pdf')).toBe('x_.pdf')
    expect(m.get('x*.pdf')).toBe('x_ (2).pdf')
  })

  it('nested duplicates in differently-cased folders share one folder', () => {
    const m = assignArchiveStems(['Board/2026/minutes.docx', 'board/2026/Minutes.docx', 'BOARD/other.docx'])
    expect(m.get('Board/2026/minutes.docx')).toBe('Board/2026/minutes.docx')
    expect(m.get('board/2026/Minutes.docx')).toBe('Board/2026/Minutes (2).docx')
    expect(m.get('BOARD/other.docx')).toBe('Board/other.docx')
  })

  // Parent review item 7, reproduced: 'a.docx' becomes the FILE packets/a.docx.pdf while
  // 'a.docx.pdf/x.docx' needs packets/a.docx.pdf as a FOLDER. Both orders, both fallback extensions.
  it.each([
    [['a.docx', 'a.docx.pdf/x.docx']],
    [['a.docx.pdf/x.docx', 'a.docx']],
    [['A.DOCX', 'a.docx.PDF/x.docx', 'a.docx.pdf/y.docx']],
    [['r.pdf', 'r.pdf.html/deep/z.pdf', 'R.PDF.html/other.docx']],
  ])('no packet path is needed as both a file and a folder: %j', (names) => {
    const m = assignArchiveStems(names)
    const paths = []
    for (const stem of m.values()) paths.push(packetPath(stem, 'pdf'), packetPath(stem, 'html'))
    // every combination of pdf/html outcomes is extraction-safe, not just the all-PDF one
    for (const stem of m.values()) {
      for (const fmt of ['pdf', 'html']) {
        const mixed = [...m.values()].map((s) => packetPath(s, s === stem ? fmt : (fmt === 'pdf' ? 'html' : 'pdf')))
        expect(extractionConflicts(mixed)).toEqual([])
      }
    }
    expect(extractionConflicts(paths.filter((p, i) => i % 2 === 0))).toEqual([])
    expect(new Set([...m.values()].map((s) => s.toLowerCase())).size).toBe(names.length)
  })

  it('siblings in a renamed folder stay together, and legitimate folders ending .pdf are kept', () => {
    const m = assignArchiveStems(['a.docx', 'a.docx.pdf/x.docx', 'a.docx.pdf/y.docx', 'Exports.pdf/q.docx'])
    expect(m.get('a.docx')).toBe('a.docx')
    expect(m.get('a.docx.pdf/x.docx')).toBe('a.docx.pdf (2)/x.docx')
    expect(m.get('a.docx.pdf/y.docx')).toBe('a.docx.pdf (2)/y.docx')
    expect(m.get('Exports.pdf/q.docx')).toBe('Exports.pdf/q.docx')    // no clash, name kept
    const rev = assignArchiveStems(['a.docx.pdf/x.docx', 'a.docx'])
    expect(rev.get('a.docx.pdf/x.docx')).toBe('a.docx.pdf/x.docx')
    expect(rev.get('a.docx')).toBe('a (2).docx')                          // the FILE yields to the folder
  })

  it('the names inside an actually generated ZIP extract without a file/folder clash', async () => {
    const { default: JSZip } = await import('jszip')
    const names = ['a.docx', 'a.docx.pdf/x.docx', 'A.docx.html/y.docx', 'Board/min.docx', 'board/MIN.docx',
      '../../etc/passwd', 'CON', 'Überblick/報告.pdf', `${'long name '.repeat(30)}.pdf`]
    const m = assignArchiveStems(names)
    const zip = new JSZip()
    names.forEach((n, i) => zip.file(packetPath(m.get(n), i % 2 ? 'html' : 'pdf'), 'x'))
    const back = await JSZip.loadAsync(await zip.generateAsync({ type: 'uint8array' }))
    const entries = Object.keys(back.files).filter((n) => !back.files[n].dir)
    expect(entries).toHaveLength(names.length)
    expect(extractionConflicts(entries)).toEqual([])
    for (const e of entries) assertSafe(e)
  })

  it('every original maps to exactly one packet path, and links are percent-encoded per segment', () => {
    const names = ['Budget #3 & notes?.xlsx', 'a%20b.pdf', 'dir/with space/x.pdf']
    const m = assignArchiveStems(names)
    expect(m.size).toBe(3)
    expect(archiveHref(packetPath(m.get('Budget #3 & notes?.xlsx'), 'pdf'))).toBe('packets/Budget%20%233%20%26%20notes_.xlsx.pdf')
    expect(archiveHref(packetPath(m.get('a%20b.pdf'), 'html'))).toBe('packets/a%2520b.pdf.html')
  })
})

describe('master index', () => {
  const row = (over = {}) => ({
    file: 'Reports/Q1.pdf', status: 'included', format: 'pdf', part: 1, archivePath: 'packets/Reports/Q1.pdf.pdf',
    assessmentState: 'assessed', findingsTotal: 3, findingsOpen: null, changesVerified: 0, changesUnverified: null,
    reviews: { pending: 2, accepted: 1, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
    comparison: { status: 'compared', introduced: 1, resolved: 2, persisting: 0, reopened: null, notComparable: { current: 0, previous: 0 } },
    scanId: 's1', factsDigest: 'a'.repeat(64), indexDigest: 'b'.repeat(64), sourceChecksum: 'md5x',
    sourceChecksumKind: 'as recorded by the source system', sourceSha256: null, correctedSha256: 'c'.repeat(64),
    generatedAt: '2026-09-17T10:00:00Z', platformVersion: '2026.9.17.1', appHref: null, appHrefNote: 'link not available',
    ...over,
  })

  it('a null count is "not recorded", never 0', () => {
    const csv = indexCsv([row()])
    const [head, line] = csv.replace(/^\ufeff/, '').trim().split('\r\n')
    const cols = head.split(',').map((c) => c.replace(/"/g, ''))
    const cells = line.match(/"(?:[^"]|"")*"/g).map((c) => c.slice(1, -1))
    const get = (h) => cells[cols.indexOf(h)]
    expect(get('Findings total')).toBe('3')
    expect(get('Findings open')).toBe(NOT_RECORDED)
    expect(get('Changes verified')).toBe('0')
    expect(get('Changes not verified')).toBe(NOT_RECORDED)
    expect(get('Source SHA-256')).toBe(NOT_RECORDED)
    expect(get('Reviews')).toBe('2 pending · 1 accepted')
    expect(get('Comparison')).toMatch(/^compared — 1 new, 2 resolved, 0 persisting/)
    expect(get('Status')).toBe('included')
    expect(get('Format')).toBe('pdf')
  })

  it('neutralises spreadsheet formulas in document names', () => {
    const csv = indexCsv([row({ file: '=HYPERLINK("http://evil","x").pdf' })])
    expect(csv).toContain('"\'=HYPERLINK(""http://evil"",""x"").pdf"')
  })

  it('status labels carry the exact reason', () => {
    expect(statusLabel(row({ status: 'failed', reason: 'HTTP 422: not accepted' }))).toBe('failed:HTTP 422: not accepted')
    expect(statusLabel(row({ status: 'cancelled' }))).toBe('skipped:cancelled')
    expect(statusLabel(row({ status: 'changed' }))).toBe('changed-during-export')
    expect(statusLabel(row({ status: 'notInIndex' }))).toBe('not in index')
    expect(reviewsText(null)).toBe(NOT_RECORDED)
    expect(comparisonText({ status: 'no_baseline', reason: 'first assessment' })).toBe('no earlier assessment — first assessment')
  })

  it('never calls an export complete when a document is missing, and says exactly why', () => {
    const header = { indexComplete: true, indexIncompleteReason: null, cancelled: false }
    expect(completeness({ rows: [row(), row({ file: 'b', format: 'html' })], ...header }).complete).toBe(true)
    const partial = completeness({ rows: [row(), row({ file: 'b', status: 'failed', reason: 'x' })], ...header })
    expect(partial.complete).toBe(false)
    expect(partial.reasons.join(' ')).toMatch(/1 document\(s\) have no packet because they failed/)
    const idx = completeness({ rows: [row()], indexComplete: false, indexIncompleteReason: 'the evidence changed while paging', cancelled: false })
    expect(idx.complete).toBe(false)
    expect(idx.reasons[0]).toMatch(/not read completely: the evidence changed while paging/)
    const cancelled = completeness({ rows: [row(), row({ file: 'b', status: 'cancelled' })], ...header, cancelled: true })
    expect(cancelled.complete).toBe(false)
    expect(cancelled.reasons[0]).toMatch(/cancelled: 1 document/)
    expect(completeness({ rows: [], ...header }).complete).toBe(false)
  })

  it('index.html is a standalone accessible page with packet links and an honest verdict', () => {
    const rows = [row(), row({ file: 'x.docx', status: 'failed', reason: 'boom', format: null, part: null, archivePath: null }),
      row({ file: 'y.docx', appHref: 'https://acp.example.com/?view=evidence&scan=s1&file=y.docx' })]
    const html = indexHtml({ rows, header: { scanId: 's1', modeLabel: 'Full evidence', indexDigest: 'b'.repeat(64), filesTotal: 3, exportedAt: 'now', platformVersion: 'v', partsTotal: 2, indexComplete: true, cancelled: false } })
    const doc = new DOMParser().parseFromString(html, 'text/html')
    expect(doc.documentElement.getAttribute('lang')).toBe('en')
    expect(doc.querySelectorAll('h1')).toHaveLength(1)
    expect(doc.querySelector('table caption').textContent).toMatch(/3 rows/)
    expect(doc.querySelectorAll('tbody tr')).toHaveLength(3)
    expect(doc.querySelectorAll('tbody th[scope="row"]')).toHaveLength(3)
    expect(doc.querySelector('.verdict').textContent).toMatch(/^INCOMPLETE/)
    const links = [...doc.querySelectorAll('tbody a')].map((a) => a.getAttribute('href'))
    expect(links).toContain('packets/Reports/Q1.pdf.pdf')
    expect(links).toContain('https://acp.example.com/?view=evidence&scan=s1&file=y.docx')
    expect(links.some((h) => h.startsWith('/'))).toBe(false)     // never a relative app link
    expect(html).toMatch(/Extract every part into the SAME folder/)
  })
})
