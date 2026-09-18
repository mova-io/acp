// HTML companion to the report PDF. Renders the SAME report model (reportModel.js /
// scanReport.js) as a single self-contained, WCAG-conformant HTML document — so the exports can
// never drift.
//
// Why HTML as well as PDF: HTML is itself screen-reader-friendly (fitting for an accessibility
// product), linkable, and trivially embeddable/shareable. This output dogfoods the product on its
// own report: it passes every Level A/AA rule in frontend/src/rules (see htmlReport.test.js) —
// semantic headings, table headers + captions, a skip link + main landmark, a declared language,
// a zoom-friendly viewport, visible focus, and 4.5:1-safe ink on white.
//
// Safety: every model string is escaped. Links render only for https: or app-relative hrefs;
// images only for base64 PNG/JPEG data URLs. Anything else degrades to plain text.
//
// Self-contained: all CSS is inlined in a <style> block and the logo is embedded as a data URI —
// the downloaded file makes ZERO external requests (links are the reader's choice to follow).

import { buildFileReportModel, INK, MUTED, GREEN, AMBER, RED, PLUM, BLUE, CW } from './reportModel.js'
import {
  isSafeHref, isSafeImageSrc, clampText, stableHash, locationLabel, technicalText, humanText,
  verificationText, clippedNote, RESPONSE_OPTIONS, RESPONSE_NOTICE, LOCATION_NOT_RECORDED,
} from './reportEvidence.js'
import { absoluteAppHref } from './evidenceLink.js'

const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;')

const slug = (s) => 'sec-' + String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '')
const anchorOf = (id) => `rec-${stableHash(id)}`
const NR = 'Not recorded'
const val = (v) => (v == null || v === '' ? NR : v)

// Map a model ink (hex) to a CSS class so palette colours live in the <style> block,
// not inline — keeping the 1.4.3 / 1.4.11 inline-style scanners quiet regardless.
const COLOR_CLASS = { [INK]: 'c-ink', [MUTED]: 'c-muted', [GREEN]: 'c-good', [AMBER]: 'c-warn', [RED]: 'c-bad', [PLUM]: 'c-plum', [BLUE]: 'c-info' }
const colorClass = (hex) => COLOR_CLASS[hex] || 'c-ink'
// Only palette hexes (validated) may reach an inline style — a model colour is data, not CSS.
const safeColor = (hex) => (/^#[0-9A-Fa-f]{3,8}$/.test(String(hex || '')) ? hex : PLUM)

// A text block's options → a class list. Size < 9 → small print; explicit bold → lead.
const textClasses = (o = {}) => {
  const cls = [colorClass(o.color)]
  if (o.bold) cls.push('lead')
  if ((o.size || 10) <= 8.5) cls.push('fine')
  else if ((o.size || 10) < 9.5) cls.push('small')
  return cls.join(' ')
}

// A DOWNLOADED file has no app origin of its own: an href like "/?view=evidence&…" in it resolves
// against file:// — the reader's own disk — not against ACP. So the model's app-relative hrefs are
// made ABSOLUTE here with evidenceLink.absoluteAppHref (a trusted https origin, or http on
// localhost), and when no trusted origin exists the location prints as text with no link at all.
// External https links pass through unchanged; any other relative href is text.
// `renderOrigin` is set per render by reportHtmlFromModel; null means "no trusted origin".
let renderOrigin = null
export function exportHref(href, origin = renderOrigin) {
  if (typeof href !== 'string' || !isSafeHref(href)) return null
  if (/^https:\/\//i.test(href)) return href
  if (href.startsWith('/?')) return absoluteAppHref(href, { origin })
  return null
}
const linkOrText = (text, href) => {
  const abs = exportHref(href)
  return abs ? `<a href="${esc(abs)}">${esc(text || abs)}</a>` : esc(text || '')
}

// ── Ring (score dial) as inline SVG — only when a legacy model still carries one ──
function ringSvg(score, hex) {
  const s = Math.max(0, Math.min(100, Math.round(score || 0)))
  const R = 34, C = 2 * Math.PI * R, on = (s / 100) * C
  const cls = colorClass(hex)
  return `<svg class="ring ${cls}" viewBox="0 0 84 84" role="img" aria-label="Accessibility score ${s} out of 100">
  <circle cx="42" cy="42" r="${R}" fill="none" stroke="#E9E5EE" stroke-width="8"></circle>
  <circle cx="42" cy="42" r="${R}" fill="none" stroke="currentColor" stroke-width="8" stroke-linecap="round"
    stroke-dasharray="${on.toFixed(2)} ${(C - on).toFixed(2)}" transform="rotate(-90 42 42)"></circle>
  <text x="42" y="42" text-anchor="middle" dominant-baseline="central" class="ring-num">${s}</text>
  <text x="42" y="57" text-anchor="middle" dominant-baseline="central" class="ring-den">/ 100</text>
</svg>`
}

// ── Donut as inline SVG + a text legend (never colour-only: 1.4.1) ──
function donutSvg(items) {
  const data = (items || []).filter((it) => (it.value || 0) > 0)
  const total = data.reduce((s, it) => s + it.value, 0)
  const R = 34, Cc = 2 * Math.PI * R
  let offset = 0
  const rings = total > 0 ? data.map((it) => {
    const frac = it.value / total
    const seg = `<circle cx="42" cy="42" r="${R}" fill="none" stroke="${esc(safeColor(it.color))}" stroke-width="16"
      stroke-dasharray="${(frac * Cc).toFixed(2)} ${((1 - frac) * Cc).toFixed(2)}"
      stroke-dashoffset="${(-offset * Cc).toFixed(2)}" transform="rotate(-90 42 42)"></circle>`
    offset += frac
    return seg
  }).join('\n') : `<circle cx="42" cy="42" r="${R}" fill="none" stroke="#EEECF0" stroke-width="16"></circle>`
  const label = total > 0
    ? `<text x="42" y="40" text-anchor="middle" dominant-baseline="central" class="donut-num">${total}</text><text x="42" y="52" text-anchor="middle" dominant-baseline="central" class="ring-den">criteria</text>`
    : `<text x="42" y="42" text-anchor="middle" dominant-baseline="central" class="ring-den">No data</text>`
  const svg = `<svg class="donut" viewBox="0 0 84 84" role="img" aria-label="Coverage: ${esc((items || []).map((it) => `${it.value} ${it.label}`).join(', '))}">
${rings}
${label}
</svg>`
  const legend = `<ul class="legend">` + (items || []).map((it) =>
    `<li><span class="swatch" style="background:${esc(safeColor(it.color))}" aria-hidden="true"></span><span class="legend-label">${esc(it.label)}</span><span class="legend-val">${esc(it.value)}</span></li>`
  ).join('') + `</ul>`
  return `<div class="donut-wrap">${svg}${legend}</div>`
}

// ── Horizontal bar chart — value shown as text, not colour-only ──
function barChart(items) {
  const mx = Math.max(1, ...(items || []).map((i) => Number(i.value) || 0))
  const rows = (items || []).map((it) => {
    const pct = Math.max(2, Math.round(((Number(it.value) || 0) / mx) * 100))
    return `<li class="bar-row">
      <span class="bar-label">${esc(it.label)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${pct}%;background:${esc(safeColor(it.color || PLUM))}"></span></span>
      <span class="bar-val">${esc(it.value)}</span>
    </li>`
  }).join('')
  return `<ul class="bars">${rows}</ul>`
}

function metricGrid(cards) {
  const cells = (cards || []).map((c) =>
    `<div class="metric"><dt>${esc(c.label)}</dt><dd class="${colorClass(c.color)}">${esc(c.value)}</dd></div>`
  ).join('')
  return `<dl class="metrics">${cells}</dl>`
}

// A table cell is text, or { text, href } — a link cell (scanReport.docCell). The href goes through
// exportHref like every other link: absolute against a trusted origin, or the text alone.
const cellHtml = (cell) => (cell && typeof cell === 'object' ? linkOrText(cell.text, cell.href) : esc(cell))

// Table with caption, column-group widths, a row header on the first column, and
// scoped column headers — everything ACP's 1.3.1 check wants to see.
function table(b, { id = null, visibleCaption = false } = {}) {
  const headers = b.headers || []
  const widths = b.widths && b.widths.length === headers.length ? b.widths : headers.map(() => CW / Math.max(1, headers.length))
  const tot = widths.reduce((s, w) => s + w, 0) || 1
  const cols = widths.map((w) => `<col style="width:${((w / tot) * 100).toFixed(1)}%">`).join('')
  const thead = `<thead><tr>` + headers.map((h) => `<th scope="col">${esc(h)}</th>`).join('') + `</tr></thead>`
  const body = (b.rows || []).length
    ? (b.rows || []).map((r) => `<tr>` + r.map((cell, i) =>
      i === 0 ? `<th scope="row">${cellHtml(cell)}</th>` : `<td>${cellHtml(cell)}</td>`
    ).join('') + `</tr>`).join('')
    : `<tr><td colspan="${headers.length || 1}">No records.</td></tr>`
  const cap = `<caption class="${visibleCaption ? 'cap' : 'sr-only'}">${esc(b.caption || 'Table')}</caption>`
  return `<div class="table-wrap"><table${id ? ` id="${esc(id)}"` : ''}><colgroup>${cols}</colgroup>${cap}${thead}<tbody>${body}</tbody></table></div>`
}

function appendixTable(b) {
  const loaded = (b.rows || []).length
  const partial = b.complete === false
    ? `<p class="partial" role="note"><strong>Partial:</strong> ${esc(loaded)} of ${esc(b.totalRecords ?? 'an unknown number of')} records (source limited to ${esc(b.limitNote || 'what was available when this report was generated')}).</p>`
    : `<p class="fine c-muted">${esc(loaded)} record${loaded === 1 ? '' : 's'}${b.totalRecords != null ? ` of ${esc(b.totalRecords)}` : ''}.</p>`
  return partial + table(b, { id: b.id ? `tbl-${String(b.id).replace(/[^A-Za-z0-9_-]/g, '-')}` : null, visibleCaption: true })
}

const STATUS_TXT = { done: 'Done', pending: 'Pending', not_started: 'Not started', unknown: 'Unknown' }

function decisionSummary(b) {
  const rows = (b.items || []).map((it) => `<tr><th scope="row">${esc(it.label)}</th><td class="num">${esc(val(it.value))}</td><td>${esc(it.detail || '')}</td></tr>`).join('')
  return `<div class="table-wrap"><table class="decision"><caption class="cap">${esc(b.caption || 'Decision summary')}</caption><thead><tr><th scope="col">Measure</th><th scope="col">Value</th><th scope="col">Basis</th></tr></thead><tbody>${rows}</tbody></table></div>`
}

function stageStrip(b) {
  const items = (b.items || []).map((it) => `<li class="stage st-${esc(it.status)}"><span class="stage-label">${esc(it.label)}</span>`
    + `<span class="stage-status">${esc(STATUS_TXT[it.status] || 'Unknown')}</span>`
    + `<span class="stage-val">${esc(val(it.value))}</span>`
    + `<span class="stage-detail">${esc(it.detail || '')}</span></li>`).join('')
  return `<ol class="stages" aria-label="Stages from suggestion to publication">${items}</ol>`
}

function locationHtml(loc) {
  if (!loc) return esc(LOCATION_NOT_RECORDED)
  return loc.href ? linkOrText(locationLabel(loc), loc.href) : esc(locationLabel(loc))
}

function band(tag, value, truncated, fullRef, fullAnchor) {
  if (value == null) return `<div class="ba-band ba-${tag.toLowerCase()}"><span class="ba-tag">${tag}</span><span class="c-muted">${NR}</span></div>`
  const shown = truncated ? clampText(value).text : String(value)
  const clip = truncated
    ? `<p class="clip">${tag} text clipped here. ${fullAnchor ? `<a href="#${esc(fullAnchor)}">Full text in the evidence appendix</a>` : `Full text: ${esc(fullRef || 'Full evidence report')}`}.</p>`
    : ''
  return `<div class="ba-band ba-${tag.toLowerCase()}"><span class="ba-tag">${tag}</span><div class="ba-body"><code>${esc(shown)}</code>${clip}</div></div>`
}

function changeCard(b, hl, ctx) {
  const hid = `h-${anchorOf(b.id)}`
  const fullAnchor = ctx.appendixIds.has(b.id) ? `ba-${anchorOf(b.id)}` : null
  const human = b.human || {}
  const humanLine = [humanText(human.status), human.reviewer ? `by ${human.reviewer}` : null, human.at ? `at ${human.at}` : null].filter(Boolean).join(' ')
  const staleLine = human.status === 'stale'
    ? `<p class="c-warn">This decision was recorded against file version ${esc(val(human.boundSha256))}; the current version is ${esc(val(human.currentSha256))}. It must be made again.</p>`
    : human.status === 'freshness_unknown'
      ? `<p class="c-warn">This acceptance cannot be tied to a file version: it was bound to ${esc(val(human.boundSha256))} and the current version is ${esc(val(human.currentSha256))}. Unknown freshness is not a confirmation — recheck it.</p>`
      : human.status === 'correction_requested'
        ? `<p class="c-warn">The reviewer asked for a different value. It was recorded as a PROPOSAL and has <strong>not</strong> been applied to the document${human.editedValue ? `: “${esc(human.editedValue)}”` : ''}. This is outstanding work, not a confirmation.</p>`
        : ''
  const img = b.image && isSafeImageSrc(b.image.src)
    ? `<figure class="preview"><img src="${esc(b.image.src.replace(/\s+/g, ''))}" alt="${esc(b.image.alt || 'Preview of the changed content')}"><figcaption>${esc(b.image.caption || '')}</figcaption></figure>`
    : `<p class="c-muted">Visual preview not available.</p>`
  const opts = (b.responseOptions || RESPONSE_OPTIONS).map((o) => `<li><span aria-hidden="true">☐</span> ${esc(o)}</li>`).join('')
  return `<article class="card change" id="${esc(anchorOf(b.id))}" aria-labelledby="${hid}">
<h${hl} id="${hid}">${esc(b.title)}</h${hl}>
<dl class="facts">
<div><dt>Record</dt><dd><code>${esc(b.id)}</code></dd></div>
<div><dt>Location</dt><dd>${locationHtml(b.location)}</dd></div>
<div><dt>Reason</dt><dd>${esc(b.reason || 'Reason not recorded')}</dd></div>
<div><dt>Technical verification</dt><dd>${esc(b.verification ? verificationText(b.verification) : technicalText(b.technical?.status))}${b.verificationDetail || b.technical?.detail ? ` — ${esc(b.verificationDetail || b.technical.detail)}` : ''}</dd></div>
<div><dt>Human confirmation</dt><dd>${esc(humanLine)}${human.loaded === false ? ' (decisions not loaded)' : ''}${human.note ? ` — “${esc(human.note)}”` : ''}</dd></div>
</dl>
${staleLine}
${band('Before', b.before, b.beforeTruncated, b.fullRef, fullAnchor)}
${band('After', b.after, b.afterTruncated, b.fullRef, fullAnchor)}
${b.valueClipped || b.beforeStoredClipped || b.afterStoredClipped ? `<p class="clip">${esc(clippedNote(b.valueMaxChars))}</p>` : ''}
${img}
<div class="respond"><p class="lead">Response</p><ul class="options">${opts}</ul><p>Notes: ______________________________</p><p class="fine c-muted">${esc(b.responseNotice || RESPONSE_NOTICE)}</p></div>
</article>`
}

const PRIORITY_TXT = { high: 'High', medium: 'Medium', low: 'Low' }
const FSTATUS_TXT = { open: 'Open finding', human_check: 'Human check needed', not_checked: 'Not checked' }

function findingCard(b, hl) {
  const hid = `h-${anchorOf(b.id)}`
  const steps = (b.steps || []).map((s) => `<li>${esc(s)}</li>`).join('')
  return `<article class="card finding pr-${esc(b.priority)}" id="${esc(anchorOf(b.id))}" aria-labelledby="${hid}">
<h${hl} id="${hid}">${esc(b.rank)}. ${esc(b.title)}</h${hl}>
<dl class="facts">
<div><dt>Status</dt><dd>${esc(FSTATUS_TXT[b.status] || b.status)}</dd></div>
<div><dt>Priority</dt><dd>${esc(PRIORITY_TXT[b.priority] || b.priority)}</dd></div>
<div><dt>Severity</dt><dd>${esc(b.severity ? b.severity.toLowerCase() : NR)}</dd></div>
<div><dt>Location</dt><dd>${locationHtml(b.location)}</dd></div>
<div><dt>Owner</dt><dd>${esc(b.owner || 'Unassigned')}</dd></div>
<div><dt>Record</dt><dd><code>${esc(b.id)}</code></dd></div>
</dl>
<p>${esc(b.description)}</p>
${b.recommendedAction ? `<p><strong>Recommended action (from the assessment):</strong> ${esc(b.recommendedAction)}</p>` : ''}
<p><strong>Who is affected:</strong> ${esc(b.impact)}</p>
${steps ? `<p class="lead">Steps</p><ol class="steps">${steps}</ol>` : ''}
<p><strong>Done when:</strong> ${esc(b.recheck)}</p>
</article>`
}

// Why a comparison was not made, by the server's own status (contract 4) — each is a different
// fact about the evidence and none of them is "nothing changed".
const NOT_COMPARED_TXT = {
  no_baseline: 'No earlier assessment to compare with.',
  baseline_unusable: 'The earlier assessment is not a usable baseline.',
  not_comparable: 'This assessment is not complete enough to compare.',
}

function comparison(b) {
  const p = b.previous || {}
  const baselineLine = p.scanId || p.generatedAt
    ? `<p class="small c-muted">Previous assessment: ${esc(val(p.scanId))} · ${esc(val(p.generatedAt))}${p.file ? ` · recorded as ${esc(p.file)}` : ''}${b.baselineStatus ? ` · status ${esc(b.baselineStatus)}` : ''}${b.baselineRunStatus ? ` (scan ${esc(b.baselineRunStatus)})` : ''} · SHA-256 ${esc(val(p.sha256))}</p>`
    : ''
  if (b.status !== 'compared') {
    const head = NOT_COMPARED_TXT[b.serverStatus] || 'Not compared.'
    return `<p class="callout warn"><strong>${esc(head)}</strong> ${esc(b.reason || 'No comparable earlier snapshot exists.')}</p>${baselineLine}`
  }
  const list = (items, empty) => (items || []).length
    ? `<ul class="bullets">${items.map((x) => `<li><code>${esc(x.id)}</code> ${esc(x.title)} — ${esc(x.location || LOCATION_NOT_RECORDED)}</li>`).join('')}</ul>`
    : `<p class="c-muted">${esc(empty)}</p>`
  const nc = b.notComparable
  const reopened = Array.isArray(b.reopened)
    ? `<p class="lead">Reported again after being recorded as resolved (${esc(b.reopened.length)})</p>${list(b.reopened, 'None.')}`
    : b.source === 'server' ? `<p class="small c-muted">Reopened: not recorded${b.reopenedReason ? ` — ${esc(b.reopenedReason)}` : ''}.</p>` : ''
  const ncLine = nc && (nc.current || nc.previous)
    ? `<p class="small">Not matched one by one (no detector location): ${esc(nc.current)} now, ${esc(nc.previous)} before${(b.notComparableByCriterion || []).length ? ` — ${esc(b.notComparableByCriterion.map((g) => `${g.sc || g.ruleId}: ${g.previous} before, ${g.current} now`).join('; '))}` : ''}. These are neither new nor resolved.</p>`
    : ''
  return `<p>${esc(b.reason)}</p>
${baselineLine}
<p class="lead">Resolved since then (${esc((b.resolved || []).length)})</p>${list(b.resolved, 'None.')}
<p class="lead">New since then (${esc((b.introduced || []).length)})</p>${list(b.introduced, 'None.')}
${reopened}
<p>Still present: ${esc(val(b.persisting))}</p>
${ncLine}`
}

function beforeAfterItems(items) {
  return (items || []).map((it) => {
    const aid = it.id ? ` id="ba-${esc(anchorOf(it.id))}"` : ''
    const status = it.technical || it.human
      ? `<p class="ba-note">Technical: ${esc(technicalText(it.technical))} · Human: ${esc(humanText(it.human))}</p>` : ''
    return `<div class="ba-item"${aid}>`
      + `<p class="ba-label">${esc(it.label)}</p>`
      + (it.id ? `<p class="ba-note">Record <code>${esc(it.id)}</code></p>` : '')
      + (it.note ? `<p class="ba-note">${esc(it.note)}</p>` : '')
      + status
      + `<div class="ba-band ba-before"><span class="ba-tag">Before</span><code>${esc(it.before == null ? NR : String(it.before))}</code></div>`
      + `<div class="ba-band ba-after"><span class="ba-tag">After</span><code>${esc(it.after == null ? NR : String(it.after))}</code></div>`
      + `</div>`
  }).join('')
}

// Render the ordered block list into <section> groups keyed by level-1 headings.
function renderBlocks(blocks) {
  const list = blocks || []
  const ctx = { appendixIds: new Set() }
  list.forEach((b) => { if (b.k === 'beforeAfter') (b.items || []).forEach((it) => { if (it.id) ctx.appendixIds.add(it.id) }) })
  const used = new Map()
  const uniqueId = (text) => {
    const base = slug(text)
    const n = (used.get(base) || 0) + 1
    used.set(base, n)
    return n === 1 ? base : `${base}-${n}`
  }
  let out = ''
  let open = false
  let level = 1          // current model heading level (h2 = level 1)
  const closeSection = () => { if (open) { out += '\n</section>'; open = false } }
  for (let idx = 0; idx < list.length; idx++) {
    const b = list[idx]
    switch (b.k) {
      case 'heading': {
        const lvl = Math.min(3, Math.max(1, Number(b.level) || 1))
        const id = b.id ? String(b.id).replace(/[^A-Za-z0-9_-]/g, '-') : uniqueId(b.text)
        if (lvl === 1) {
          closeSection()
          out += `\n<section aria-labelledby="${id}">\n<h2 id="${id}">${esc(b.text)}</h2>`
          open = true
        } else {
          out += `\n<h${lvl + 1} id="${id}">${esc(b.text)}</h${lvl + 1}>`
        }
        level = lvl
        break
      }
      case 'pageBreak': {
        // Soft section break: only before the evidence appendix, never a near-empty page.
        const next = list.slice(idx + 1).find((x) => x.k === 'heading')
        if (next && /appendix/i.test(next.text || '')) { closeSection(); out += `\n<div class="page-break" aria-hidden="true"></div>` }
        break
      }
      case 'text':
        out += `\n<p class="${textClasses(b.o)}">${esc(b.text)}</p>`
        break
      case 'callout': {
        const tone = b.o?.color === GREEN ? 'good' : b.o?.color === AMBER ? 'warn' : 'plum'
        out += `\n<p class="callout ${tone}">${esc(b.text)}</p>`
        break
      }
      case 'bullets':
        out += `\n<ul class="bullets">` + (b.items || []).map((it) => `<li>${esc(it)}</li>`).join('') + `</ul>`
        break
      case 'metricGrid': out += `\n${metricGrid(b.cards)}`; break
      case 'donut': out += `\n${donutSvg(b.items)}`; break
      case 'barChart': out += `\n${barChart(b.items)}`; break
      case 'table': out += `\n${table(b)}`; break
      case 'appendixTable': out += `\n${appendixTable(b)}`; break
      case 'decisionSummary': out += `\n${decisionSummary(b)}`; break
      case 'stageStrip': out += `\n${stageStrip(b)}`; break
      case 'changeCard': out += `\n${changeCard(b, Math.min(6, level + 2), ctx)}`; break
      case 'findingCard': out += `\n${findingCard(b, Math.min(6, level + 2))}`; break
      case 'comparison': out += `\n${comparison(b)}`; break
      case 'link': out += `\n<p class="link">${linkOrText(b.text, b.href)}</p>`; break
      case 'beforeAfter': out += '\n' + beforeAfterItems(b.items); break
      default: break
    }
  }
  closeSection()
  return out
}

const STYLE = `
  :root { --ink:${INK}; --muted:${MUTED}; --line:${'#E4E0E8'}; --plum:${PLUM}; --paper:#fff; --wash:#F7F5F9; }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body { margin: 0; background: #EDEAF0; color: var(--ink);
    font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main, p, li, dd, td, th, code, h1, h2, h3, h4, h5 { overflow-wrap: anywhere; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
  .skip-link { position: absolute; left: 8px; top: -48px; background: var(--plum); color: #fff;
    padding: 10px 16px; border-radius: 6px; z-index: 10; transition: top .15s; }
  .skip-link:focus { top: 8px; }
  a { color: ${BLUE}; }
  a:focus-visible, .skip-link:focus-visible, [tabindex]:focus-visible { outline: 3px solid var(--plum); outline-offset: 2px; }
  .page { max-width: 900px; margin: 24px auto; background: var(--paper); padding: 40px 48px;
    box-shadow: 0 1px 4px rgba(43,35,48,.12); border-radius: 8px; }
  .cover { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px;
    border-bottom: 2px solid var(--plum); padding-bottom: 20px; margin-bottom: 8px; }
  .cover-main { min-width: 0; }
  .logo { height: 34px; width: auto; margin-bottom: 14px; }
  h1 { font-size: 27px; line-height: 1.2; margin: 0 0 6px; letter-spacing: -.01em; }
  .subtitle { font-size: 16px; color: var(--muted); margin: 0 0 10px; word-break: break-word; }
  .cover-meta { list-style: none; padding: 0; margin: 0; color: var(--muted); font-size: 13px; }
  .cover-meta li { margin: 2px 0; }
  h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .04em; color: var(--plum);
    margin: 30px 0 4px; padding-bottom: 6px; border-bottom: 1px solid var(--line); }
  h3 { font-size: 15px; margin: 22px 0 6px; color: var(--ink); }
  h4, h5, h6 { font-size: 14px; margin: 0 0 6px; color: var(--ink); }
  section > :first-child { margin-top: 0; }
  p { margin: 9px 0; }
  .lead { font-weight: 700; }
  .small { font-size: 13px; }
  .fine { font-size: 12px; }
  .c-ink { color: var(--ink); } .c-muted { color: var(--muted); }
  .c-good { color: ${GREEN}; } .c-warn { color: ${AMBER}; } .c-bad { color: ${RED}; }
  .c-plum { color: ${PLUM}; } .c-info { color: ${BLUE}; }
  .callout { border: 1px solid var(--plum); background: var(--wash); border-left-width: 5px;
    border-radius: 6px; padding: 14px 16px; margin: 12px 0; }
  .callout.good { border-color: ${GREEN}; background: #EEF5E8; }
  .callout.warn { border-color: ${AMBER}; background: #FBF1DF; }
  .partial { border-left: 4px solid ${AMBER}; background: #FBF1DF; padding: 8px 12px; }
  .clip { font-size: 12px; color: ${AMBER}; margin: 4px 0 0; }
  .bullets { margin: 8px 0; padding-left: 22px; }
  .bullets li { margin: 4px 0; }
  .ring { width: 84px; height: 84px; flex: 0 0 auto; }
  .ring-num { font-size: 22px; font-weight: 700; fill: currentColor; }
  .ring-den, .donut .ring-den { font-size: 8px; fill: var(--muted); }
  .donut-num { font-size: 16px; font-weight: 700; fill: var(--ink); }
  .donut-wrap { display: flex; align-items: center; gap: 24px; flex-wrap: wrap; margin: 12px 0; }
  .donut { width: 120px; height: 120px; flex: 0 0 auto; }
  .legend { list-style: none; margin: 0; padding: 0; }
  .legend li { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  .swatch { width: 12px; height: 12px; border-radius: 3px; flex: 0 0 auto; }
  .legend-val { font-weight: 700; margin-left: 4px; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 14px 0; }
  .metric { border: 1px solid var(--line); background: #FBFAFC; border-radius: 6px; padding: 10px 12px; }
  .metric dt { font-size: 11px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); }
  .metric dd { margin: 4px 0 0; font-size: 22px; font-weight: 700; }
  .stages { list-style: none; margin: 14px 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
  .stage { border: 1px solid var(--line); border-top-width: 4px; border-radius: 6px; padding: 8px 10px; display: flex; flex-direction: column; gap: 2px; }
  .stage-label { font-weight: 700; } .stage-status { font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
  .stage-val { font-size: 18px; font-weight: 700; } .stage-detail { font-size: 12px; color: var(--muted); }
  .st-done { border-top-color: ${GREEN}; } .st-pending { border-top-color: ${AMBER}; }
  .st-not_started, .st-unknown { border-top-color: #B6B0BC; }
  .card { border: 1px solid var(--line); border-left-width: 4px; border-radius: 6px; padding: 12px 14px; margin: 14px 0; break-inside: avoid; }
  .card.change { border-left-color: ${PLUM}; }
  .card.pr-high { border-left-color: ${RED}; } .card.pr-medium { border-left-color: ${AMBER}; } .card.pr-low { border-left-color: #8A8490; }
  .facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 4px 16px; margin: 6px 0; }
  .facts dt { font-size: 11px; text-transform: uppercase; letter-spacing: .03em; color: var(--muted); }
  .facts dd { margin: 0; }
  .options { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 4px 0; }
  .steps { padding-left: 22px; }
  .preview { margin: 8px 0; } .preview img { max-width: 100%; height: auto; border: 1px solid var(--line); }
  .preview figcaption { font-size: 12px; color: var(--muted); }
  .bars { list-style: none; margin: 12px 0; padding: 0; }
  .bar-row { display: grid; grid-template-columns: 150px 1fr 40px; align-items: center; gap: 10px; margin: 6px 0; }
  .bar-track { background: #EEECF0; border-radius: 3px; height: 14px; overflow: hidden; }
  .bar-fill { display: block; height: 100%; border-radius: 3px; }
  .bar-val { font-weight: 700; text-align: right; }
  .table-wrap { overflow-x: auto; margin: 12px 0; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  caption { text-align: left; }
  caption.cap { font-weight: 700; padding: 0 0 6px; }
  thead th { background: var(--plum); color: #fff; text-align: left; padding: 8px 10px; font-size: 12px; }
  tbody th, tbody td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
    vertical-align: top; font-weight: 400; }
  td.num { font-weight: 700; white-space: nowrap; }
  tbody th { color: var(--ink); }
  tbody tr:nth-child(even) { background: var(--wash); }
  .doc-footer { margin-top: 28px; padding-top: 14px; border-top: 1px solid var(--line);
    color: var(--muted); font-size: 12px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; }
  @media (max-width: 640px) {
    .page { padding: 24px 18px; margin: 0; border-radius: 0; }
    .bar-row { grid-template-columns: 110px 1fr 34px; }
    .cover { flex-direction: column; }
  }
  @media print {
    body { background: #fff; } .page { box-shadow: none; margin: 0; max-width: none; padding: 0; }
    .skip-link { display: none; }
    .page-break { break-before: page; }
    thead { display: table-header-group; }
    tr, .card, .ba-item, figure { break-inside: avoid; }
    h2, h3, h4 { break-after: avoid; }
    .table-wrap { overflow: visible; }
  }
  .ba-item { margin: 0 0 16px; }
  .ba-label { font-weight: 700; color: #4B3460; margin: 0 0 3px; }
  .ba-note { color: #55505A; font-size: 13px; margin: 0 0 6px; }
  .ba-band { display: flex; gap: 10px; align-items: flex-start; border: 1px solid #E4E0E8;
    border-left-width: 3px; border-radius: 4px; padding: 7px 9px; margin: 4px 0; }
  .ba-body { min-width: 0; }
  .ba-band code { font-family: ui-monospace, "Courier New", monospace; font-size: 12.5px;
    white-space: pre-wrap; overflow-wrap: anywhere; color: #2B2330; }
  .ba-tag { font-size: 11px; font-weight: 700; letter-spacing: .04em; flex: 0 0 52px; padding-top: 1px; }
  .ba-before { background: #FBF2F1; border-left-color: #A32D2D; }
  .ba-before .ba-tag { color: #8A2A24; }
  .ba-after { background: #F0F5EA; border-left-color: #3B6D11; }
  .ba-after .ba-tag { color: #345F0F; }
`

// Build the complete standalone HTML document string from a report model.
// `logo` (optional) is a data: URI for the mova.io mark; omitted in tests.
export function reportHtmlFromModel(model, { logo, origin = (typeof window !== 'undefined' ? window.location?.origin : null) } = {}) {
  const previousOrigin = renderOrigin
  renderOrigin = origin ?? null
  try { return renderDocument(model, { logo }) } finally { renderOrigin = previousOrigin }
}

function renderDocument(model, { logo } = {}) {
  const c = model.cover || {}
  const coverMeta = (c.meta || []).map((m) => `<li>${esc(m)}</li>`).join('')
  const logoImg = logo && isSafeImageSrc(logo) ? `<img class="logo" src="${esc(logo)}" alt="mova.io Accessibility Platform">` : ''
  const ring = c.ring ? ringSvg(c.ring.score, c.ring.color) : ''
  const id = model.identity || {}
  const sha = id.correctedSha256 || id.sourceSha256
  const brand = 'mova.io · Accessibility Platform' + (model.footerVersion ? ` · v${esc(model.footerVersion)}` : '')
  const footer = `<footer class="doc-footer">
    <span>${brand}</span>
    ${sha ? `<span>Artifact ${esc(String(sha).slice(0, 12))}</span>` : ''}
    <span>Confidential</span>
    ${model.footerGenerated || id.generatedAt ? `<span>Generated ${esc(model.footerGenerated || id.generatedAt)}</span>` : ''}
  </footer>`

  return `<!doctype html>
<html lang="${esc(model.lang || 'en-US')}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(model.docTitle)}</title>
<style>${STYLE}</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to main content</a>
<div class="page">
<header class="cover">
  <div class="cover-main">
    ${logoImg}
    <h1>${esc(c.title || 'Accessibility Assessment Report')}</h1>
    ${c.subtitle ? `<p class="subtitle">${esc(c.subtitle)}</p>` : ''}
    <ul class="cover-meta">${coverMeta}</ul>
  </div>
  ${ring}
</header>
<main id="main-content">${renderBlocks(model.blocks)}
</main>
${footer}
</div>
</body>
</html>`
}
export const certificationHtmlFromModel = reportHtmlFromModel

// Pure entry points (no I/O) — used by the dogfood test.
export function fileReportHtml(d, opts = {}) {
  return reportHtmlFromModel(buildFileReportModel(d), opts)
}
// Compatibility: the old certification HTML was the whole record → Full evidence.
export function certificationHtml(d, opts = {}) {
  return reportHtmlFromModel(buildFileReportModel({ ...d, mode: 'full' }), opts)
}

async function logoDataUrl() {
  try {
    const url = (import.meta.env.BASE_URL || '/') + 'mova-logo.png'
    const blob = await (await fetch(url)).blob()
    return await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => res(null); r.readAsDataURL(blob) })
  } catch { return null }
}

export async function downloadModelHtml(model) {
  const logo = await logoDataUrl()
  const html = reportHtmlFromModel(model, { logo })
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${model.filename}.html`
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

// Build + download the self-contained HTML report. d.mode selects Summary / Reviewer packet /
// Full evidence (default Full evidence).
export async function exportFileReportHtml(d) {
  await downloadModelHtml(buildFileReportModel(d))
}
// Compatibility alias (FileDrawer's existing button): the full record.
export async function exportFileCertificationHtml(d) {
  await downloadModelHtml(buildFileReportModel({ ...d, mode: 'full' }))
}
