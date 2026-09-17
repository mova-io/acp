// Evidence layer for the report model (reportModel.js / scanReport.js).
//
// PURE: no I/O, no DOM. Everything here turns records ACP actually holds — a file's findings, the
// remediation_diff rows, versioned reviewer decisions, a previous snapshot — into the plain-text
// shapes the report contract names (fileIssues, changeCard, findingCard, comparison). Renderers
// (htmlReport.js, the server PDF) escape and lay these out; nothing here emits markup.
//
// Three rules the whole module keeps, because each was broken somewhere before it existed:
//
//   1. EVERY record survives. No `slice(0, 2)`, no collapsing distinct findings under one criterion,
//      no silent cap. When a caller must bound output it says so with a number (see `boundList`).
//   2. Missing evidence is `null`, never 0 and never a guess. A location nobody recorded is
//      "Location not recorded"; a verification nobody ran is 'unknown' or 'not_run'.
//   3. Identity is stable. A finding keeps the server id when there is one, otherwise a
//      deterministic hash of (file, rule, location, detail) plus an occurrence index — so the same
//      assessment always produces the same ids, and a later snapshot can be compared against it.

import { fixSteps, hasGuidance } from './remediationGuide.js'
import { WCAG } from './wcagCatalog.js'

const SC_NAME = Object.fromEntries(WCAG.map((c) => [c.sc, c.name]))
const SC_PRINCIPLE = Object.fromEntries(WCAG.map((c) => [c.sc, c.principle]))

export const scOfValue = (w) => ((String(w || '')).replace(/^SC[_ ]?/i, '').replace(/_/g, '.').match(/\d+\.\d+\.\d+/) || [])[0] || null
export const criterionName = (sc) => SC_NAME[sc] || null

export const RESPONSE_OPTIONS = Object.freeze(['Accept', 'Edit', 'Reject', 'Unable to verify'])
export const RESPONSE_NOTICE = 'Marking a printed or downloaded copy does not record a decision. '
  + 'Only a decision entered in ACP (Remediate → Changes to confirm) is part of the record.'

// ── Hashing (deterministic, non-cryptographic — an identifier, not a digest) ─────────────────
function fnv1a(str, seed) {
  let h = seed >>> 0
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h >>> 0
}
export function stableHash(str) {
  const s = String(str)
  return fnv1a(s, 0x811c9dc5).toString(16).padStart(8, '0') + fnv1a(s, 0x9747b28c).toString(16).padStart(8, '0')
}

// ── Safety predicates shared by every renderer ───────────────────────────────────────────────
// Links: https: or app-relative ("/scans/…", never protocol-relative "//host").
export const isSafeHref = (href) => typeof href === 'string'
  && (/^https:\/\/[^\s<>"']+$/i.test(href) || /^\/(?!\/)[^\s<>"']*$/.test(href))
// Images: base64 PNG/JPEG data URLs only. data:text/html, svg (scriptable) and remote URLs are refused.
export const isSafeImageSrc = (src) => typeof src === 'string'
  && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/]+=*$/.test(src.replace(/\s+/g, ''))

// ── Text clamping for concise reviewer cards ─────────────────────────────────────────────────
export const CARD_MAX_CHARS = 600
export const CARD_MAX_LINES = 7
export function clampText(value, { maxChars = CARD_MAX_CHARS, maxLines = CARD_MAX_LINES } = {}) {
  if (value == null) return { text: null, truncated: false }
  const s = String(value)
  let out = s
  const lines = out.split('\n')
  if (lines.length > maxLines) out = lines.slice(0, maxLines).join('\n')
  if (out.length > maxChars) out = out.slice(0, maxChars)
  const truncated = out.length < s.length
  return { text: truncated ? `${out.replace(/\s+$/, '')}…` : s, truncated }
}
export const needsClamp = (value) => value != null && clampText(value).truncated

// Bound a list for a concise mode WITHOUT hiding that it was bounded.
export function boundList(items, cap) {
  const list = items || []
  if (cap == null || list.length <= cap) return { shown: list, omitted: 0, total: list.length }
  return { shown: list.slice(0, cap), omitted: list.length - cap, total: list.length }
}

// ── Locations ───────────────────────────────────────────────────────────────────────────────
const posInt = (v) => { const n = Number(v); return Number.isInteger(n) && n > 0 ? n : null }
const str = (v) => (v == null || v === '' ? null : String(v))

// A structured location from whatever a detector attached. Returns null when nothing was recorded —
// never a page-1 default (scanner.py _issue_with_loc makes the same promise at the source).
export function locationOf(rec, { fmt = null, locationHref = null } = {}) {
  if (!rec) return null
  const nested = rec.location && typeof rec.location === 'object' ? rec.location : null
  const raw = nested ? null : str(rec.location) || str(rec.locator)
  const src = nested || rec
  let page = posInt(src.page ?? src.pageNumber)
  let slide = posInt(src.slide ?? src.slideNumber)
  if (fmt === 'pptx' && page != null && slide == null) { slide = page; page = null }
  let sheet = str(src.sheet ?? src.sheetName)
  let cell = str(src.cell)
  if (raw && sheet == null) {
    const m = raw.match(/sheet[:=]([^:!/#]+)/i) || raw.match(/^'?([^'!:/#]+)'?!\$?[A-Z]{1,3}\$?\d+/)
    if (m) sheet = m[1]
  }
  if (raw && cell == null) {
    const m = raw.match(/!\$?([A-Z]{1,3})\$?(\d+)/) || raw.match(/cell[:=]\$?([A-Z]{1,3})\$?(\d+)/i)
    if (m) cell = `${m[1]}${m[2]}`
  }
  const element = str(src.element) || str(src.selector) || str(src.xpath) || str(src.xPath)
    || (nested ? str(nested.label) : null) || raw
  if (page == null && slide == null && sheet == null && cell == null && element == null) return null
  const parts = []
  if (page != null) parts.push(`Page ${page}`)
  if (slide != null) parts.push(`Slide ${slide}`)
  if (sheet != null) parts.push(`Sheet ${sheet}`)
  if (cell != null) parts.push(`Cell ${cell}`)
  if (element != null && !(nested && nested.label && element === nested.label && parts.length)) parts.push(element)
  const loc = { label: parts.join(' · '), page, slide, sheet, cell, element, href: null }
  let href = nested && isSafeHref(nested.href) ? nested.href : null
  if (!href && typeof locationHref === 'function') {
    try { const h = locationHref({ ...rec, location: loc }); if (isSafeHref(h)) href = h } catch { /* no link */ }
  }
  loc.href = href
  return loc
}
export const LOCATION_NOT_RECORDED = 'Location not recorded'
export const locationLabel = (loc) => (loc && loc.label) || LOCATION_NOT_RECORDED

export const fmtOfFile = (name) => {
  const ext = String(name || '').split('.').pop().toLowerCase()
  if (/^html?$/.test(ext)) return 'html'
  return ['docx', 'xlsx', 'pptx', 'pdf'].includes(ext) ? ext : null
}

// ── Findings ────────────────────────────────────────────────────────────────────────────────
const fileNameOf = (file) => (typeof file === 'string' ? file : file?.file) || ''
const issuesOf = (file) => (typeof file === 'object' && Array.isArray(file?.issues) ? file.issues : [])

// Every finding of a file in the contract shape, ids assigned over the WHOLE list so an id never
// depends on which criterion asked for it.
export function fileIssuesOf(file, { locationHref = null } = {}) {
  const name = fileNameOf(file)
  const fmt = fmtOfFile(name) || (typeof file === 'object' ? String(file?.type || '').toLowerCase() || null : null)
  const seen = new Map()
  return issuesOf(file).map((i) => {
    const ruleId = str(i.rule_id ?? i.ruleId)
    const sc = scOfValue(i.wcag) || scOfValue(i.sc) || scOfValue(ruleId)
    const detail = str(i.detail ?? i.message)
    const location = locationOf(i, { fmt, locationHref })
    const serverId = str(i.id ?? i.finding_id ?? i.issue_id)
    let id = serverId
    if (!id) {
      const base = `f-${stableHash([name, ruleId || '', sc || '', location ? location.label : '', detail || ''].join('␟'))}`
      const n = (seen.get(base) || 0) + 1
      seen.set(base, n)
      id = n === 1 ? base : `${base}-${n}`
    }
    return {
      id, sc, ruleId, file: name,
      detail,
      severity: str(i.severity) ? String(i.severity).toUpperCase() : null,
      location,
      action: str(i.fix ?? i.action),
      impact: str(i.impact),
      auto: typeof i.auto === 'boolean' ? i.auto : null,
      owner: str(i.assignee?.name ?? i.assignee ?? i.owner),
      page: location?.page ?? null, slide: location?.slide ?? null, sheet: location?.sheet ?? null,
      cell: location?.cell ?? null, element: location?.element ?? null,
      evidence: i.evidence && typeof i.evidence === 'object' ? i.evidence : null,
    }
  })
}

// The findings of one criterion. Advisory (severity REVIEW) findings are included — the row's
// outcome decides how they are presented, not whether they exist.
export function fileIssuesForCriterion(file, sc, opts = {}) {
  return fileIssuesOf(file, opts).filter((i) => i.sc === sc)
}

// Copy of `rows` with `fileIssues` set from the file, for every row. Callers that already hold a
// filtered per-row list (FileDrawer's blocking/review split) keep the split: when a row carries
// fileIssues, they are re-mapped by identity into the contract shape rather than replaced by the
// criterion's whole list.
export function attachFileIssues(rows, file, opts = {}) {
  const all = fileIssuesOf(file, opts)
  const bySc = {}
  all.forEach((i) => { if (i.sc) (bySc[i.sc] = bySc[i.sc] || []).push(i) })
  const raw = issuesOf(file)
  return (rows || []).map((r) => {
    let list = bySc[r.id] || []
    if (Array.isArray(r.fileIssues) && r.fileIssues.length && r.fileIssues.every((x) => raw.includes(x))) {
      const keep = new Set(r.fileIssues.map((x) => raw.indexOf(x)))
      list = all.filter((_, idx) => keep.has(idx))
    } else if (Array.isArray(r.fileIssues) && r.fileIssues.length && r.fileIssues.every((x) => x && x.id)) {
      list = r.fileIssues
    }
    return { ...r, fileIssues: list }
  })
}

// Normalise a row's fileIssues into the contract shape even when a caller supplied raw engine
// findings (older callers / tests). Raw findings get ids computed within the row.
export function normaliseRowIssues(row, fileName, opts = {}) {
  const list = Array.isArray(row?.fileIssues) ? row.fileIssues : []
  if (list.every((x) => x && x.id && 'location' in x && 'ruleId' in x)) return list
  return fileIssuesOf({ file: fileName, issues: list.map((x) => ({ wcag: row.id, ...x })) }, opts)
    .map((x) => ({ ...x, sc: x.sc || row.id }))
}

// ── Change cards (one per saved change = remediation_diff row) ──────────────────────────────
export const changeIdOf = (file, diff, index = 0) =>
  `${fileNameOf(file)}::${diff?.rule_id ?? diff?.ruleId ?? 'unknown'}::${diff?.seq ?? `i${index}`}`

const VERDICT_STATUS = { accepted: 'accepted', edited: 'edited', rejected: 'rejected', unable: 'unable' }

export function humanStatusOf(review, currentSha256 = null) {
  if (!review || typeof review !== 'object') {
    return { status: 'pending', reviewer: null, at: null, note: null, boundSha256: null, currentSha256 }
  }
  const verdict = VERDICT_STATUS[review.verdict] || null
  const bound = str(review.artifact_sha256 ?? review.boundSha256)
  const stale = review.stale === true || (bound != null && currentSha256 != null && bound !== currentSha256)
  return {
    status: stale && verdict ? 'stale' : verdict || 'pending',
    reviewer: str(review.reviewer?.name ?? review.reviewer),
    at: str(review.at),
    note: str(review.note),
    editedValue: str(review.edited_value),
    verdict,
    boundSha256: bound,
    currentSha256,
  }
}

export function technicalStatusOf(diff) {
  if (diff?.verified === true) {
    return { status: 'verified', detail: 'The re-scan after this edit no longer reported the finding (remediation record).' }
  }
  if (diff?.verified === false) return { status: 'pending', detail: 'Edit saved; re-validation has not been recorded.' }
  return { status: 'unknown', detail: 'Verification status was not recorded with this change.' }
}

const TECH_TXT = { verified: 'Verified by re-scan', pending: 'Re-validation pending', not_run: 'Not run', unknown: 'Not recorded' }
const HUMAN_TXT = { pending: 'Awaiting confirmation', accepted: 'Accepted', edited: 'Accepted with edits', rejected: 'Rejected', unable: 'Unable to verify', stale: 'Stale — the file changed after this decision' }
export const technicalText = (s) => TECH_TXT[s] || 'Not recorded'
export const humanText = (s) => HUMAN_TXT[s] || 'Not recorded'

// diffs: the FULL list of remediation_diff rows for this file.
export function buildChangeCards({ file, diffs = [], reviews = null, previews = null, currentSha256 = null,
  names = {}, locationHref = null, clamp = false, fullRefPrefix = '' } = {}) {
  const name = fileNameOf(file)
  const fmt = fmtOfFile(name)
  return (diffs || []).filter((x) => x && (x.before != null || x.after != null)).map((x, idx) => {
    const id = changeIdOf(name, x, idx)
    const sc = scOfValue(x.rule_id ?? x.ruleId)
    const cname = names[sc] || criterionName(sc)
    const location = locationOf(x, { fmt, locationHref })
    const before = x.before == null ? null : String(x.before)
    const after = x.after == null ? null : String(x.after)
    const bT = clamp && needsClamp(before)
    const aT = clamp && needsClamp(after)
    let src = null
    if (previews && typeof previews === 'object') {
      const cand = previews[id] ?? (location?.page != null ? previews[location.page] : undefined)
        ?? (location?.slide != null ? previews[location.slide] : undefined)
      if (isSafeImageSrc(cand)) src = cand
    }
    const review = reviews && typeof reviews === 'object' ? reviews[id] : null
    return {
      k: 'changeCard',
      id,
      title: `${sc || x.rule_id || 'Change'}${cname ? ` · ${cname}` : ''}`,
      criterion: sc || str(x.rule_id),
      criterionName: cname,
      location,
      before, after,
      beforeTruncated: bT, afterTruncated: aT,
      fullRef: (bT || aT) ? `${fullRefPrefix}${id}` : null,
      reason: str(x.note),
      image: src ? { src, alt: `Preview of ${location ? location.label : 'the changed content'} after the edit`, caption: location ? `${location.label} — after the edit` : 'After the edit' } : null,
      imageStatus: src ? 'available' : 'unavailable',
      technical: technicalStatusOf(x),
      human: reviews == null ? { ...humanStatusOf(null, currentSha256), status: 'pending', loaded: false } : humanStatusOf(review, currentSha256),
      responseOptions: [...RESPONSE_OPTIONS],
      responseNotice: RESPONSE_NOTICE,
      seq: x.seq ?? null,
    }
  })
}

// ── Finding cards (one per finding, not per criterion) ──────────────────────────────────────
const SEV_RANK = { CRITICAL: 0, SERIOUS: 1, MODERATE: 2, MINOR: 3, REVIEW: 4 }
const STATUS_RANK = { open: 0, human_check: 1, not_checked: 2 }

// Why the barrier matters to a person, by criterion. Short, factual, no invented statistics.
const IMPACT = {
  '1.1.1': 'Screen-reader users hear nothing, or a file name, where this image or object carries meaning.',
  '1.3.1': 'Screen-reader users cannot tell headings, lists or table headers apart from body text, so structure is lost.',
  '1.3.2': 'Assistive technology reads the content in an order that does not match what sighted readers see.',
  '1.4.3': 'Readers with low vision or colour-vision differences may be unable to read low-contrast text.',
  '1.4.1': 'Readers who cannot distinguish the colours miss information that is conveyed by colour alone.',
  '2.4.2': 'Users of assistive technology cannot identify the document from its title.',
  '2.4.4': 'Link text does not say where the link goes when it is read out of context.',
  '2.4.6': 'Headings or labels do not describe their section, so navigating by heading is unreliable.',
  '3.1.1': 'Screen readers may pronounce the content in the wrong language.',
  '1.4.5': 'Text inside an image cannot be resized, recoloured or read by a screen reader.',
  '4.1.2': 'Controls do not expose their name, role or state to assistive technology.',
}
const PRINCIPLE_IMPACT = {
  Perceivable: 'Some readers may be unable to perceive this content as presented.',
  Operable: 'Some readers may be unable to operate or navigate this content.',
  Understandable: 'Some readers may be unable to understand this content or how to use it.',
  Robust: 'Assistive technology may not interpret this content reliably.',
}
export const impactFor = (sc, issue) => (issue && issue.impact) || IMPACT[sc] || PRINCIPLE_IMPACT[SC_PRINCIPLE[sc]]
  || 'Impact not recorded for this criterion.'

const HUMAN_STEPS = {
  '1.1.1': ['Open each flagged image', 'Confirm the description states the image’s meaning, not just its contents'],
  '1.2.1': ['Play the media', 'Confirm the transcript captures all meaningful content'],
  '1.2.2': ['Play the video with captions on', 'Confirm captions match the audio and identify speakers and sounds'],
  '1.2.3': ['Play the video', 'Confirm every meaningful visual event is described in narration or a text alternative'],
  '1.4.1': ['Find where colour signals meaning (for example red = error)', 'Confirm a label, icon or text also communicates it'],
  '1.3.5': ['Check name, email and address fields', 'Confirm the correct input purpose is set'],
  '2.1.1': ['Tab through all interactive controls', 'Confirm each is reachable and operable by keyboard alone, with no trap'],
  '2.5.3': ['For each labelled control, confirm the spoken name includes the visible label text'],
  '3.3.1': ['Trigger a form error', 'Confirm the message names the field and the problem'],
  '4.1.2': ['Navigate custom controls with a screen reader', 'Confirm each announces its name, role and state'],
}
export const humanStepsFor = (sc) => HUMAN_STEPS[sc] || ['Review the flagged content against the WCAG success criterion']

function stepsFor(sc, fmt, issue) {
  const out = []
  if (issue && issue.action) out.push(issue.action)
  const g = fixSteps(sc, fmt)
  if (g.where) out.push(`Where to look: ${g.where}`)
  if (g.mac && g.win && g.mac === g.win) out.push(g.mac)
  else {
    if (g.mac) out.push(`macOS: ${g.mac}`)
    if (g.win) out.push(`Windows: ${g.win}`)
  }
  return out
}

const ownerOf = (issue, fallback) => (issue && issue.owner) || str(fallback?.name ?? fallback) || null

// rows: coverage rows ({id, name, plain, outcome, count, fileIssues, disposition}).
export function buildFindingCards({ file, rows = [], assignee = null, locationHref = null, clamp = false } = {}) {
  const name = fileNameOf(file)
  const fmt = fmtOfFile(name)
  const cards = []
  for (const r of rows) {
    if (r.disposition && r.disposition.kind) continue
    const sc = r.id
    const cname = r.plain || r.name || criterionName(sc)
    const issues = normaliseRowIssues(r, name, { locationHref })
    if (r.outcome === 'FAIL') {
      const list = issues.length ? issues : [null]
      list.forEach((i, n) => {
        const sev = i?.severity || null
        const g = fixSteps(sc, fmt)
        cards.push({
          k: 'findingCard',
          id: i ? i.id : `${name}::${sc}::unitemised`,
          status: 'open',
          priority: sev === 'CRITICAL' || sev === 'SERIOUS' ? 'high' : 'medium',
          severity: sev,
          title: `${sc} · ${cname}`,
          criterion: sc, criterionName: criterionName(sc) || r.name || null,
          location: i ? i.location : null,
          description: i ? (i.detail || 'Finding recorded without a description.')
            : `${r.count || 'An unknown number of'} finding${r.count === 1 ? '' : 's'} recorded for this criterion; the individual findings were not itemised in this export.`,
          impact: impactFor(sc, i),
          steps: stepsFor(sc, fmt, i),
          owner: ownerOf(i, assignee),
          recheck: `Re-run the ACP assessment on the corrected copy: this finding must no longer be reported for WCAG ${sc}.${g.completion ? ` ${g.completion}` : ''}`,
          specific: hasGuidance(sc),
          _order: n,
        })
      })
    } else if (r.outcome === 'HUMAN') {
      const list = issues.length ? issues : [null]
      list.forEach((i, n) => cards.push({
        k: 'findingCard',
        id: i ? i.id : `${name}::${sc}::human`,
        status: 'human_check',
        priority: 'medium',
        severity: i?.severity || null,
        title: `${sc} · ${cname}`,
        criterion: sc, criterionName: criterionName(sc) || r.name || null,
        location: i ? i.location : null,
        description: i?.detail || 'Automated checks cannot decide this criterion; a person must verify it.',
        impact: impactFor(sc, i),
        steps: humanStepsFor(sc),
        owner: ownerOf(i, assignee),
        recheck: 'A reviewer records the outcome in ACP — an attestation with notes, or a finding to fix.',
        specific: true,
        _order: n,
      }))
    } else if (r.outcome === 'UNCHECKED') {
      cards.push({
        k: 'findingCard',
        id: `${name}::${sc}::not-checked`,
        status: 'not_checked',
        priority: 'low',
        severity: null,
        title: `${sc} · ${cname}`,
        criterion: sc, criterionName: criterionName(sc) || r.name || null,
        location: null,
        description: 'ACP has no automated check for this criterion on this file type. It was not checked — this is not a pass.',
        impact: impactFor(sc, null),
        steps: [...humanStepsFor(sc), 'Record the result in ACP as an attestation, or mark the criterion out of scope with a reason.'],
        owner: ownerOf(null, assignee),
        recheck: 'A recorded attestation or out-of-scope disposition in ACP.',
        specific: false,
        _order: 0,
      })
    }
  }
  cards.sort((a, b) => (STATUS_RANK[a.status] - STATUS_RANK[b.status])
    || ((SEV_RANK[a.severity] ?? 5) - (SEV_RANK[b.severity] ?? 5))
    || (Number(b.specific) - Number(a.specific))
    || String(a.criterion).localeCompare(String(b.criterion), undefined, { numeric: true })
    || (a._order - b._order))
  return cards.map((c, idx) => {
    const { _order, specific, ...rest } = c
    if (clamp) {
      const d = clampText(rest.description)
      rest.descriptionTruncated = d.truncated
    }
    return { ...rest, rank: idx + 1 }
  })
}

// Re-rank finding cards gathered from several files with the same ordering buildFindingCards uses
// within one file (status, then severity, then criterion) — stable for equal keys.
export function rankFindingCards(cards) {
  return (cards || []).map((c, i) => ({ c, i })).sort((a, b) => (STATUS_RANK[a.c.status] - STATUS_RANK[b.c.status])
    || ((SEV_RANK[a.c.severity] ?? 5) - (SEV_RANK[b.c.severity] ?? 5))
    || (a.c.priority === b.c.priority ? 0 : a.c.priority === 'high' ? -1 : b.c.priority === 'high' ? 1 : 0)
    || String(a.c.criterion).localeCompare(String(b.c.criterion), undefined, { numeric: true })
    || (a.i - b.i)).map(({ c }, idx) => ({ ...c, rank: idx + 1 }))
}

// Coverage-like rows from a file's raw findings, for estate-level cards: blocking findings are
// open (FAIL), advisory REVIEW findings are human checks.
export function rowsFromFindings(file, opts = {}) {
  const bySc = new Map()
  for (const i of fileIssuesOf(file, opts)) {
    const sc = i.sc || i.ruleId || 'unknown'
    const kind = i.severity === 'REVIEW' ? 'HUMAN' : 'FAIL'
    const key = `${sc}::${kind}`
    if (!bySc.has(key)) bySc.set(key, { id: sc, name: criterionName(sc) || sc, outcome: kind, count: 0, fileIssues: [] })
    const r = bySc.get(key)
    r.count++
    r.fileIssues.push(i)
  }
  return [...bySc.values()]
}

// ── Comparison with a previous snapshot ─────────────────────────────────────────────────────
const canon = (v) => {
  if (Array.isArray(v)) return `[${v.map(canon).sort().join(',')}]`
  if (v && typeof v === 'object') return `{${Object.keys(v).sort().map((k) => `${JSON.stringify(k)}:${canon(v[k])}`).join(',')}}`
  return JSON.stringify(v ?? null)
}

// previous: { scanId, generatedAt, sha256, file?, scope, findings:[{id, ruleId, location}] }
// current:  { file, scope, findings:[{id, ruleId|sc, location}] | null }
export function buildComparison(previous, current) {
  const unknown = (reason, prev = null) => ({ k: 'comparison', status: 'unknown', reason, previous: prev, resolved: [], introduced: [], persisting: null })
  if (!previous || typeof previous !== 'object') return unknown('No earlier assessment snapshot of this document was supplied, so no change is reported.')
  const prevRef = { scanId: str(previous.scanId), generatedAt: str(previous.generatedAt), sha256: str(previous.sha256) }
  if (previous.file != null && current.file != null && previous.file !== current.file) {
    return unknown('The earlier snapshot is of a different document, so it is not comparable.', prevRef)
  }
  if (previous.scope == null || current.scope == null) return unknown('The assessment scope was not recorded for one of the two snapshots, so they are not comparable.', prevRef)
  if (canon(previous.scope) !== canon(current.scope)) return unknown('The earlier snapshot used a different assessment scope, so a difference in findings would not mean a change in the document.', prevRef)
  if (!Array.isArray(previous.findings)) return unknown('The earlier snapshot did not record its individual findings.', prevRef)
  if (!Array.isArray(current.findings)) return unknown('The current findings were not itemised, so they cannot be matched one by one.', prevRef)
  const cur = new Map(current.findings.map((f) => [f.id, f]))
  const prev = new Map(previous.findings.filter((f) => f && f.id != null).map((f) => [String(f.id), f]))
  const label = (f) => {
    const loc = f.location && typeof f.location === 'object' ? f.location : locationOf(f)
    return { id: String(f.id), title: `${f.ruleId || f.sc || 'Finding'}${f.detail ? ` — ${f.detail}` : ''}`, location: loc ? loc.label : LOCATION_NOT_RECORDED }
  }
  const resolved = [...prev.values()].filter((f) => !cur.has(String(f.id))).map(label)
  const introduced = [...cur.values()].filter((f) => !prev.has(String(f.id))).map(label)
  const persisting = [...cur.keys()].filter((id) => prev.has(String(id))).length
  return {
    k: 'comparison',
    status: 'compared',
    reason: `Matched finding by finding against the assessment of ${prevRef.generatedAt || 'an earlier date'} with the same document and scope.`,
    previous: prevRef,
    resolved, introduced, persisting,
  }
}
