// Pure pieces of the per-file packet ZIP (reportPacketExport.js): safe archive paths, part
// thresholds, and the master index (index.html + index.csv). No I/O here, so every rule below is
// testable directly.
//
// SAFE PATHS. Scan file names are real repository paths — SharePoint folders, Drive names typed by
// people, sometimes hostile. A ZIP entry name is a path the reader's unzip tool will create, so:
//   - no "..", no absolute paths, no drive letters, no backslash separators;
//   - no control characters, no bidi overrides (a name that displays differently from what it
//     is), none of <>:"|?* (Windows refuses them), no trailing dots/spaces (Windows strips them);
//   - no reserved Windows device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9, with or without an
//     extension) — extracting "con.pdf" on Windows fails or writes to a device;
//   - Unicode is kept (NFC), because "Überblick.docx" and "報告.pdf" are the names people know;
//   - each segment is bounded in UTF-8 bytes and the whole path in characters; a name that has to
//     be shortened gets a short hash of the ORIGINAL so two shortened names cannot collide;
//   - names are de-duplicated CASE-INSENSITIVELY (Windows and macOS extract "A.pdf" and "a.pdf"
//     onto one file) with stable " (2)", " (3)" suffixes in index order;
//   - the index maps every original name to its archive name, so nothing depends on the reader
//     reverse-engineering the sanitising.
// Folder structure is preserved (sanitised). A path too long even after bounding each segment is
// flattened deterministically into `_long-paths/` — still one entry per document, still mapped.

export const PACKET_ROOT = 'packets'
export const LONG_PATH_DIR = '_long-paths'
export const MAX_SEGMENT_BYTES = 120        // well under the 255-byte component limit of every FS
export const MAX_PATH_CHARS = 180           // leaves ~80 chars of MAX_PATH (260) for the extract dir

// Part thresholds — a part is generated, downloaded and RELEASED before the next one starts, so
// browser memory is bounded by one part (JSZip holds the inputs plus the generated blob: roughly
// 2x the part size at the moment it is generated).
//   - 100 MB: per-file packet PDFs measured 60 KB – 2 MB (Full evidence, 300-document remediation
//     report: 6.7 MB total), so a part holds hundreds of packets while its peak (~200-300 MB) stays
//     inside what a browser tab reliably gets.
//   - 500 packets: keeps every part far below the ZIP entry limit (65,535 without ZIP64) and keeps
//     one extraction a size people can check by eye; a 4,000-document scan is 8 parts.
export const PART_MAX_BYTES = 100 * 1024 * 1024
export const PART_MAX_FILES = 500

const RESERVED = /^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(\..*)?$/i
// C0/C1 controls, DEL, bidi embedding/override/isolate marks, zero-width joiners used for spoofing,
// the BOM, and characters Windows forbids in a path segment.
// eslint-disable-next-line no-control-regex
const UNSAFE_CHARS = /[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff<>:"|?*]/g

const utf8Len = (s) => new TextEncoder().encode(s).length

// FNV-1a 32-bit → 8 hex. A disambiguator for shortened names, not a security property.
export function shortHash(s) {
  let h = 0x811c9dc5
  for (const ch of String(s)) {
    h ^= ch.codePointAt(0)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h.toString(16).padStart(8, '0')
}

// Cut `s` to at most `maxBytes` UTF-8 bytes without splitting a code point.
function cutBytes(s, maxBytes) {
  let out = ''
  let used = 0
  for (const ch of s) {
    const n = utf8Len(ch)
    if (used + n > maxBytes) break
    out += ch
    used += n
  }
  return out
}

// One path segment, made safe. Never returns '', '.', or '..'.
export function safeSegment(seg, { maxBytes = MAX_SEGMENT_BYTES } = {}) {
  let s = String(seg ?? '').normalize('NFC').replace(UNSAFE_CHARS, '_')
  s = s.replace(/[. ]+$/g, '').replace(/^ +/g, '')
  if (!s || /^\.+$/.test(s)) s = '_'
  if (RESERVED.test(s)) s = `_${s}`
  if (utf8Len(s) > maxBytes) {
    const tag = `~${shortHash(seg)}`
    const dot = s.lastIndexOf('.')
    const ext = dot > 0 && s.length - dot <= 12 ? s.slice(dot) : ''
    const stem = ext ? s.slice(0, dot) : s
    s = cutBytes(stem, maxBytes - utf8Len(tag) - utf8Len(ext)).replace(/[. ]+$/g, '') + tag + ext
  }
  return s
}

// The segments of an original name: separators are / and \, a leading drive letter is dropped,
// and "." / ".." / empty segments are removed (a ".." is never a directory in the archive).
export function pathSegments(original) {
  let s = String(original ?? '').normalize('NFC')
  s = s.replace(/^[A-Za-z]:(?=[\\/]|$)/, '')           // C:\… or C:/…
  return s.split(/[\\/]+/).filter((p) => p && p !== '.' && p !== '..')
}

/**
 * Archive-safe stem (no packet extension yet) for one original name, relative to PACKET_ROOT.
 * Deterministic: the same original always yields the same stem.
 */
export function safeStem(original, { maxPathChars = MAX_PATH_CHARS } = {}) {
  // The file segment leaves room for the packet extension (".html") and a " (nn)" suffix, so the
  // FINAL name stays within MAX_SEGMENT_BYTES too.
  const raw = pathSegments(original)
  const segs = raw.map((p, i) => safeSegment(p, i === raw.length - 1 ? { maxBytes: MAX_SEGMENT_BYTES - 12 } : {}))
  const safe = segs.length ? segs : ['_unnamed']
  // +6 for the longest packet extension and a " (NN)" suffix are accounted for by the budget below.
  const joined = safe.join('/')
  if (`${PACKET_ROOT}/${joined}`.length + 12 <= maxPathChars) return joined
  const base = safeSegment(safe[safe.length - 1], { maxBytes: 80 })
  return `${LONG_PATH_DIR}/${shortHash(original)}-${base}`
}

const splitExt = (stem) => {
  const slash = stem.lastIndexOf('/')
  const dot = stem.lastIndexOf('.')
  return dot > slash + 1 ? [stem.slice(0, dot), stem.slice(dot)] : [stem, '']
}

/**
 * Assign every original name a unique archive stem, in the order given. Case-insensitive
 * de-duplication with stable " (2)"-style suffixes; directory segments take the casing first seen,
 * so "A/x" and "a/y" land in one folder rather than two that collide on extraction.
 * Returns Map(original → stem).
 */
export function assignArchiveStems(originals) {
  // One namespace for everything extraction will create, compared case-insensitively:
  //   stems   — each document's stem;
  //   finals  — BOTH packet files a stem can become (".pdf", and ".html" if it falls back), since
  //             which one is written is only known after rendering;
  //   dirs    — every folder any packet sits in.
  // A folder may not share a name with a packet file, in either order of arrival: "a.docx" makes
  // "a.docx.pdf", and "a.docx.pdf/x.docx" would need "a.docx.pdf" as a FOLDER (parent review
  // item 7) — an unzip tool cannot create both. Folders are renamed consistently for every
  // document inside them; files take a " (n)" suffix.
  const out = new Map()
  const stems = new Set()
  const finals = new Set()
  const dirs = new Set()
  const dirMap = new Map()        // sanitised folder path (lower-case) → the folder path used
  const low = (s) => s.toLowerCase()
  const fileClashes = (c) => stems.has(low(c)) || dirs.has(low(`${c}.pdf`)) || dirs.has(low(`${c}.html`))
  for (const original of originals) {
    if (out.has(original)) continue
    const parts = safeStem(original).split('/')
    let parent = ''
    for (let i = 0; i < parts.length - 1; i++) {
      const key = low(parts.slice(0, i + 1).join('/'))
      let mapped = dirMap.get(key)
      if (mapped == null) {
        const at = (name) => (parent ? `${parent}/${name}` : name)
        let n = 1
        mapped = at(parts[i])
        while (finals.has(low(mapped))) { n += 1; mapped = at(`${parts[i]} (${n})`) }
        dirMap.set(key, mapped)
        dirs.add(low(mapped))
      }
      parent = mapped
    }
    const base = parts[parts.length - 1]
    const at = (name) => (parent ? `${parent}/${name}` : name)
    let candidate = at(base)
    let n = 1
    while (fileClashes(candidate)) {
      n += 1
      const [b, e] = splitExt(base)
      candidate = at(`${b} (${n})${e}`)
    }
    stems.add(low(candidate))
    finals.add(low(`${candidate}.pdf`))
    finals.add(low(`${candidate}.html`))
    out.set(original, candidate)
  }
  return out
}

// The folders and files a set of packet paths would create on extraction, and whether any path
// is needed as both (the check the tests run over an actual generated ZIP).
export function extractionConflicts(paths) {
  const files = new Set(paths.map((p) => p.toLowerCase()))
  const conflicts = []
  const seen = new Map()
  for (const p of paths) {
    const segs = p.split('/')
    for (let i = 1; i < segs.length; i++) {
      const dir = segs.slice(0, i).join('/').toLowerCase()
      if (files.has(dir)) conflicts.push(`${dir} is both a file and a folder`)
    }
    const k = p.toLowerCase()
    if (seen.has(k) && seen.get(k) !== p) conflicts.push(`${p} collides with ${seen.get(k)}`)
    seen.set(k, p)
  }
  return conflicts
}

export const packetPath = (stem, format) => `${PACKET_ROOT}/${stem}.${format === 'pdf' ? 'pdf' : 'html'}`

// An archive-relative href from the index (at the archive root) to a packet: every segment
// percent-encoded, so "#", "%", "?" and spaces in a name cannot change what the link points at.
export const archiveHref = (path) => path.split('/').map((p) => encodeURIComponent(p)).join('/')

// ── Master index ──────────────────────────────────────────────────────────────────────────────

export const NOT_RECORDED = 'not recorded'
const nr = (v) => (v == null || v === '' ? NOT_RECORDED : String(v))
const num = (v) => (Number.isFinite(v) ? String(v) : NOT_RECORDED)

export const STATUS_TEXT = {
  included: 'included',
  failed: 'failed',
  cancelled: 'skipped:cancelled',
  changed: 'changed-during-export',
  notInIndex: 'not in index',
}
export function statusLabel(row) {
  if (row.status === 'failed') return `failed:${row.reason || 'reason not recorded'}`
  if (row.status === 'notInIndex' && row.reason) return `not in index (${row.reason})`
  return STATUS_TEXT[row.status] || row.status
}
export const formatLabel = (row) => (row.format === 'pdf' ? 'pdf' : row.format === 'html' ? 'html-fallback' : 'none')

export function reviewsText(h) {
  if (!h || typeof h !== 'object') return NOT_RECORDED
  const parts = [['pending', 'pending'], ['accepted', 'accepted'], ['correctionRequested', 'correction requested'],
    ['rejected', 'rejected'], ['unable', 'unable to verify'], ['stale', 'stale']]
    .filter(([k]) => Number.isFinite(h[k]) && h[k] > 0).map(([k, label]) => `${h[k]} ${label}`)
  if (parts.length) return parts.join(' · ')
  return Object.values(h).some((v) => Number.isFinite(v)) ? 'none recorded' : NOT_RECORDED
}

export function comparisonText(c) {
  if (!c || typeof c !== 'object') return ''
  const status = c.status === 'compared' ? 'compared'
    : c.status === 'no_baseline' ? 'no earlier assessment'
      : c.status === 'baseline_unusable' ? 'earlier assessment not usable' : (c.status || '')
  const counts = []
  const n = (v) => (Array.isArray(v) ? v.length : Number.isFinite(v) ? v : null)
  for (const [k, label] of [['introduced', 'new'], ['reopened', 'reopened'], ['resolved', 'resolved'], ['persisting', 'persisting']]) {
    const v = n(c[k])
    if (v != null) counts.push(`${v} ${label}`)
  }
  const nc = c.notComparable
  if (nc && (Number.isFinite(nc.current) || Number.isFinite(nc.previous))) counts.push(`not comparable: ${num(nc.current)} now / ${num(nc.previous)} before`)
  return [status, counts.join(', '), c.status !== 'compared' && c.reason ? c.reason : ''].filter(Boolean).join(' — ')
}

// The index columns, in order. `value(row)` is plain text; the HTML adds a link for two of them.
export const INDEX_COLUMNS = [
  ['Original name', (r) => r.file],
  ['Packet (in archive)', (r) => r.archivePath || ''],
  ['Part', (r) => (r.part != null ? String(r.part) : '')],
  ['Format', formatLabel],
  ['Status', statusLabel],
  ['Assessment state', (r) => nr(r.assessmentState)],
  ['Findings total', (r) => num(r.findingsTotal)],
  ['Findings open', (r) => num(r.findingsOpen)],
  ['Changes verified', (r) => num(r.changesVerified)],
  ['Changes not verified', (r) => num(r.changesUnverified)],
  ['Reviews', (r) => reviewsText(r.reviews)],
  ['Comparison', (r) => comparisonText(r.comparison)],
  ['Scan id', (r) => nr(r.scanId)],
  ['Per-file facts digest', (r) => nr(r.factsDigest)],
  ['Index snapshot digest', (r) => nr(r.indexDigest)],
  ['Source checksum', (r) => nr(r.sourceChecksum)],
  ['Source checksum kind', (r) => nr(r.sourceChecksumKind)],
  ['Source SHA-256', (r) => nr(r.sourceSha256)],
  ['Corrected SHA-256', (r) => nr(r.correctedSha256)],
  ['Generated at', (r) => nr(r.generatedAt)],
  ['Platform version', (r) => nr(r.platformVersion)],
  ['Open in ACP', (r) => r.appHref || (r.appHrefNote || 'link not available')],
]

// CSV: RFC 4180 quoting, and a leading ' on anything a spreadsheet would run as a formula.
const csvCell = (v) => {
  let s = String(v ?? '')
  if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`
  return `"${s.replace(/"/g, '""')}"`
}
export function indexCsv(rows) {
  const lines = [INDEX_COLUMNS.map(([h]) => csvCell(h)).join(',')]
  for (const r of rows) lines.push(INDEX_COLUMNS.map(([, f]) => csvCell(f(r))).join(','))
  return `\ufeff${lines.join('\r\n')}\r\n`
}

const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')

export function summaryCounts(rows) {
  const c = { total: rows.length, included: 0, pdf: 0, html: 0, failed: 0, cancelled: 0, changed: 0, notInIndex: 0 }
  for (const r of rows) {
    if (r.status in c) c[r.status] += 1
    if (r.status === 'included' && r.format === 'pdf') c.pdf += 1
    if (r.status === 'included' && r.format === 'html') c.html += 1
  }
  return c
}

/**
 * Is this export complete, and if not, EXACTLY why. Complete means: the scan index was read to
 * the end, every document in it has a packet, and none of them fell back or was skipped. An HTML
 * fallback still counts as included — it is the same evidence — but is named.
 */
export function completeness({ rows, indexComplete, indexIncompleteReason, cancelled }) {
  const c = summaryCounts(rows)
  const reasons = []
  if (!indexComplete) reasons.push(`The scan index was not read completely: ${indexIncompleteReason || 'reason not recorded'}.`)
  if (cancelled) reasons.push(`The export was cancelled: ${c.cancelled} document(s) were not exported.`)
  if (c.failed) reasons.push(`${c.failed} document(s) have no packet because they failed (see Status).`)
  if (c.changed) reasons.push(`${c.changed} document(s) changed while the export ran, so no packet was made for them — export again to include them.`)
  if (c.notInIndex) reasons.push(`${c.notInIndex} document(s) listed on screen are not in the scan index, so no packet was made for them.`)
  const complete = reasons.length === 0 && c.included === c.total && c.total > 0
  if (!complete && !reasons.length) reasons.push(c.total === 0 ? 'The scan index lists no documents.' : 'Not every document has a packet.')
  return { complete, reasons, counts: c }
}

/**
 * index.html — an accessible, standalone page: lang, title, one h1, a status section that says
 * COMPLETE or INCOMPLETE with the exact reasons, and one data table (caption, column headers,
 * each row headed by its document name). Packet links are archive-relative; "Open in ACP" links
 * are ABSOLUTE and only present when a trusted origin was available.
 */
export function indexHtml({ rows, header }) {
  const { complete, reasons, counts } = completeness({ rows, ...header })
  const meta = [
    ['Scan', header.scanId],
    ['Report mode', header.modeLabel],
    ['Index snapshot digest', header.indexDigest],
    ['Index snapshot built', header.snapshotBuiltAt],
    ['Documents in the scan index', header.filesTotal],
    ['Exported', header.exportedAt],
    ['Platform version', header.platformVersion],
    ['Parts', header.partsTotal],
  ]
  const th = INDEX_COLUMNS.map(([h]) => `<th scope="col">${esc(h)}</th>`).join('')
  const body = rows.map((r) => {
    const cells = INDEX_COLUMNS.map(([h, f], i) => {
      const text = f(r)
      if (i === 0) return `<th scope="row">${esc(text)}</th>`
      if (h === 'Packet (in archive)' && r.archivePath && r.status === 'included') {
        return `<td><a href="${esc(archiveHref(r.archivePath))}">${esc(text)}</a></td>`
      }
      if (h === 'Open in ACP' && r.appHref) return `<td><a href="${esc(r.appHref)}">Open ${esc(r.file)} in ACP</a></td>`
      return `<td>${esc(text)}</td>`
    }).join('')
    return `<tr>${cells}</tr>`
  }).join('\n')
  const verdict = complete
    ? `<p class="verdict ok"><strong>COMPLETE.</strong> Every one of the ${counts.total} documents in the scan index has a packet in this export${counts.html ? ` (${counts.html} as HTML because the PDF could not be produced)` : ''}.</p>`
    : `<p class="verdict bad"><strong>INCOMPLETE.</strong> ${counts.included} of ${counts.total} documents have a packet in this export.</p><ul>${reasons.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`
  const multi = header.partsTotal > 1
    ? `<p>This export is split into ${esc(header.partsTotal)} ZIP parts. Extract every part into the SAME folder so the packet links below resolve; the Part column says which ZIP holds each packet.</p>`
    : ''
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Per-file report packets — scan ${esc(header.scanId)}</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:#2B2330;margin:24px;line-height:1.45}
table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #C9C3CF;padding:4px 6px;vertical-align:top;text-align:left}
thead th{background:#F1EEF4;position:sticky;top:0}.verdict{padding:8px 12px;border-left:4px solid}
.ok{border-color:#3B6D11;background:#EEF5E8}.bad{border-color:#8A2A24;background:#FBEDEC}
dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px}dt{font-weight:600}
a{color:#4B3460}a:focus{outline:3px solid #4B3460;outline-offset:2px}
</style>
</head>
<body>
<main>
<h1>Per-file report packets</h1>
<section aria-labelledby="st"><h2 id="st">Export status</h2>
${verdict}
${multi}
<p>Counts: ${counts.included} included (${counts.pdf} PDF, ${counts.html} HTML fallback) · ${counts.failed} failed · ${counts.changed} changed during export · ${counts.cancelled} cancelled · ${counts.notInIndex} not in index.</p>
<p>Numbers read "${NOT_RECORDED}" where the evidence does not record them; they are never shown as 0. Each packet is bound to its per-file facts digest, which the server re-verified when it rendered the PDF.</p>
</section>
<section aria-labelledby="id"><h2 id="id">Identity</h2>
<dl>${meta.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(nr(v))}</dd>`).join('')}</dl>
</section>
<section aria-labelledby="ix"><h2 id="ix">Documents</h2>
<table>
<caption>One row per document in the scan index (${counts.total} rows)</caption>
<thead><tr>${th}</tr></thead>
<tbody>
${body}
</tbody>
</table>
</section>
</main>
</body>
</html>
`
}
