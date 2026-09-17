// Native PDF report generator — composes a real, paginated document (selectable text,
// the mova.io logo, a clean grid layout, VECTOR bar charts), NOT a DOM screenshot. This
// is what makes the downloads read as governance documents instead of webpage printouts.
// jspdf is lazy-loaded so it stays out of the main bundle.

import { statusFor } from './exportDeliverables.js'
import { WCAG } from './wcagCatalog.js'
import { buildFileCertificationModel, buildRemediationModel, VERIFY_GUIDE } from './reportModel.js'
// AcroForm gives the remediation checklist REAL checkboxes — focusable, announced by a screen
// reader, tickable in any conformant viewer. See checklist() for why a drawn square will not do.
import { AcroFormCheckBox } from 'jspdf'

const INK = '#2B2330', MUTED = '#6B6670', LINE = '#E4E0E8', PLUM = '#4B3460', GREEN = '#3B6D11', AMBER = '#854F0B', RED = '#A32D2D'
const rgb = (h) => { h = h.replace('#', ''); return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)] }

async function logoDataUrl() {
  try {
    const url = (import.meta.env.BASE_URL || '/') + 'mova-logo.png'
    const blob = await (await fetch(url)).blob()
    return await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => res(null); r.readAsDataURL(blob) })
  } catch { return null }
}

async function makeDoc({ title = 'mova.io Accessibility Report', lang = 'en-US', footerVersion, footerGenerated } = {}) {
  const { jsPDF } = await import('jspdf')
  const doc = new jsPDF('p', 'pt', 'a4')
  // The platform must not ship inaccessible PDFs: set a document title (2.4.2), the
  // document language (3.1.1), and ask viewers to show the title in the window bar.
  doc.setProperties({ title, creator: 'mova.io Accessibility Platform', author: 'mova.io' })
  doc.setLanguage(lang)
  doc.viewerPreferences({ DisplayDocTitle: true })
  const W = doc.internal.pageSize.getWidth(), H = doc.internal.pageSize.getHeight()
  const M = 50, CW = W - 2 * M, FOOT = 50
  const logo = await logoDataUrl()
  const st = { y: M }
  const fill = (h) => doc.setFillColor(...rgb(h))
  const ink = (h) => doc.setTextColor(...rgb(h))
  const draw = (h) => doc.setDrawColor(...rgb(h))
  const ensure = (h) => { if (st.y + h > H - FOOT) { doc.addPage(); st.y = M } }

  return {
    doc, W, H, M, CW,
    gap(h) { st.y += h },
    pageBreak() { if (st.y > M + 1) { doc.addPage(); st.y = M } },
    bullets(items, { size = 10, color = INK, lh = 14, gap = 5 } = {}) {
      doc.setFontSize(size)
      items.forEach((it) => {
        const lines = doc.splitTextToSize(String(it), CW - 16); ensure(lines.length * lh + gap)
        ink(PLUM); doc.setFont('helvetica', 'bold'); doc.text('\u2022', M + 2, st.y + size)
        ink(color); doc.setFont('helvetica', 'normal'); doc.text(lines, M + 14, st.y + size)
        st.y += lines.length * lh + gap
      })
      st.y += 4
    },
    // A review checklist. Each row is a REAL AcroForm checkbox, not a drawn square.
    //
    // That distinction matters more here than anywhere else in this file: this is an
    // accessibility product shipping a document a human is asked to work through. A vector
    // rectangle is invisible to a screen reader and cannot be ticked without printing the page.
    // An AcroForm field is focusable, announced, and fillable in any conformant reader.
    //
    // jsPDF's AcroForm support varies by build, so a failure falls back to a drawn box rather
    // than losing the checklist — and the LABEL carries the whole item either way, so the text
    // layer alone still tells a reader what they are confirming.
    checklist(items, { size = 9.5, lh = 13 } = {}) {
      const BOX = 11
      items.forEach((it, i) => {
        const label = String(it.label)
        const metaTxt = it.meta ? String(it.meta) : null
        doc.setFontSize(size)
        const lines = doc.splitTextToSize(label, CW - BOX - 18)
        const metaL = metaTxt ? doc.splitTextToSize(metaTxt, CW - BOX - 18) : []
        const h = Math.max(BOX, lines.length * lh) + metaL.length * 11 + 7
        ensure(h)
        const boxY = st.y + 1
        let placed = false
        try {
          // eslint-disable-next-line no-undef
          const cb = new AcroFormCheckBox()
          cb.fieldName = `chk_${it.id || i}`
          cb.Rect = [M, boxY, BOX, BOX]
          cb.appearanceState = 'Off'
          cb.caption = 'x'
          doc.addField(cb)
          placed = true
        } catch { /* no AcroForm in this build — fall through to a drawn box */ }
        if (!placed) { draw(MUTED); doc.setLineWidth(0.9); doc.roundedRect(M, boxY, BOX, BOX, 2, 2, 'S') }
        ink(INK); doc.setFont('helvetica', 'normal'); doc.setFontSize(size)
        doc.text(lines, M + BOX + 8, st.y + size)
        st.y += Math.max(BOX, lines.length * lh)
        if (metaL.length) {
          ink(MUTED); doc.setFontSize(8); doc.text(metaL, M + BOX + 8, st.y + 6); st.y += metaL.length * 11
        }
        st.y += 7
      })
      st.y += 6
    },
    // Before → After evidence blocks: for each remediated finding, the original
    // text/markup and the remediated version, in a monospace card so markup, hex
    // colours and attributes read literally. Long values truncate + wrap; a finding
    // is kept on one page when it fits.
    beforeAfter(items) {
      const MONO = 'courier', VS = 8.5, VLH = 11, PAD = 8, MAXCH = 600, MAXLINES = 7
      const clamp = (s) => { s = String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); return s.length > MAXCH ? s.slice(0, MAXCH - 1) + '…' : s }
      const wrap = (s) => { const l = doc.splitTextToSize(clamp(s), CW - 4 * PAD - 44); return l.length > MAXLINES ? [...l.slice(0, MAXLINES - 1), l[MAXLINES - 1].replace(/.{1,3}$/, '…')] : l }
      items.forEach((it) => {
        const bl = wrap(it.before), al = wrap(it.after)
        const noteL = it.note ? doc.splitTextToSize(String(it.note), CW - 24) : []
        const rowH = (lines) => lines.length * VLH + 2 * PAD
        const total = 17 + noteL.length * 11 + rowH(bl) + 4 + rowH(al) + 12
        if (st.y + Math.min(total, H - 2 * M) > H - FOOT) { doc.addPage(); st.y = M }
        ink(PLUM); doc.setFont('helvetica', 'bold'); doc.setFontSize(10.5)
        doc.text(String(it.label), M, st.y + 10); st.y += 15
        if (noteL.length) { ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(8.5); doc.text(noteL, M, st.y + 8); st.y += noteL.length * 11 + 2 }
        const band = (tag, tagClr, bg, lines) => {
          const h = rowH(lines)
          draw(LINE); fill(bg); doc.setLineWidth(0.6); doc.roundedRect(M, st.y, CW, h, 4, 4, 'FD')
          fill(tagClr); doc.rect(M, st.y, 3, h, 'F')
          ink(tagClr); doc.setFont('helvetica', 'bold'); doc.setFontSize(7)
          doc.text(tag, M + PAD, st.y + PAD + 4)
          ink(INK); doc.setFont(MONO, 'normal'); doc.setFontSize(VS)
          doc.text(lines, M + PAD + 40, st.y + PAD + 4)
          st.y += h
        }
        band('BEFORE', RED, '#FBF2F1', bl); st.y += 4
        band('AFTER', GREEN, '#F0F5EA', al); st.y += 12
      })
    },
    heading(t) {
      ensure(34); ink(PLUM); doc.setFont('helvetica', 'bold'); doc.setFontSize(12.5)
      doc.text(t.toUpperCase(), M, st.y + 12); st.y += 18
      draw(LINE); doc.setLineWidth(0.8); doc.line(M, st.y, M + CW, st.y); st.y += 14
    },
    // A section heading that preserves the string VERBATIM. For filenames: heading() upper-cases
    // for style, which is fine for prose labels and wrong for an identifier — a reviewer told to
    // open "CLINICAL-FAQ-39.DOCX" is being given a name that does not exist on a case-sensitive
    // store. Wraps, because paths get long.
    docTitle(t) {
      doc.setFont('helvetica', 'bold'); doc.setFontSize(12.5)
      const lines = doc.splitTextToSize(String(t), CW)
      ensure(lines.length * 16 + 18)
      ink(PLUM); doc.text(lines, M, st.y + 12); st.y += lines.length * 16 + 4
      draw(LINE); doc.setLineWidth(0.8); doc.line(M, st.y, M + CW, st.y); st.y += 14
    },
    text(t, { size = 10, color = INK, bold = false, lh = 14, gapAfter = 9 } = {}) {
      doc.setFont('helvetica', bold ? 'bold' : 'normal'); doc.setFontSize(size); ink(color)
      const lines = doc.splitTextToSize(t, CW); ensure(lines.length * lh)
      doc.text(lines, M, st.y + size); st.y += lines.length * lh + gapAfter
    },
    // logoRight puts the mark in the top-right corner and runs the title alongside it, rather
    // than stacking it above. `logo` is the real brand asset (public/mova-logo.png, 800x264) —
    // fetched, never redrawn — so the proportions come from the file, not from a guess.
    cover({ title, subtitle, meta = [], logoRight = false }) {
      const lw = 124, lh = lw * 264 / 800
      let titleW = CW
      if (logo && logoRight) {
        ensure(lh + 12)
        doc.addImage(logo, 'PNG', M + CW - lw, st.y, lw, lh)
        // The title shares this band with the logo, so it must wrap before reaching it.
        titleW = CW - lw - 18
      } else if (logo) {
        ensure(lh + 12); doc.addImage(logo, 'PNG', M, st.y, lw, lh); st.y += lh + 20
      }
      ink(INK); doc.setFont('helvetica', 'bold'); doc.setFontSize(21)
      const tl = doc.splitTextToSize(title, titleW); doc.text(tl, M, st.y + 17); st.y += tl.length * 24 + 6
      // Keep the header band tall enough that the logo never collides with the subtitle below.
      if (logo && logoRight) st.y = Math.max(st.y, lh + M - 6)
      ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(11); doc.text(subtitle, M, st.y + 10); st.y += 17
      doc.setFontSize(9.5); meta.forEach((m) => { doc.text(m, M, st.y + 8); st.y += 13 })
      st.y += 10; draw(PLUM); doc.setLineWidth(2); doc.line(M, st.y, M + CW, st.y); st.y += 22
    },
    // Embed a raster image (e.g. a page-1 document preview, ADR 0015) scaled to a capped
    // width, page-break aware, in a subtle frame. Best-effort: silently does nothing if the
    // blob can't be decoded — a certification PDF must render with or without a preview.
    async imageFromBlob(blob, { maxW = 300, caption = null } = {}) {
      try {
        const dataUrl = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = () => res(null); r.readAsDataURL(blob) })
        if (!dataUrl) return
        const props = doc.getImageProperties(dataUrl)
        const w = Math.min(CW, maxW), h = w * props.height / props.width
        ensure(h + (caption ? 20 : 8))
        doc.addImage(dataUrl, props.fileType || 'PNG', M, st.y, w, h)
        draw(LINE); doc.setLineWidth(0.8); doc.rect(M, st.y, w, h); st.y += h + 6
        if (caption) { ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(8.5); doc.text(caption, M, st.y + 6); st.y += 14 }
      } catch { /* undecodable image — omit, never throw out of report generation */ }
    },
    // A score dial drawn at the top-right of the cover — a real ring, not just a number.
    ring(score, color) {
      const R = 30, cx = W - M - R - 2, cy = M + R + 2
      const s = Math.max(0, Math.min(100, Math.round(score || 0)))
      doc.setLineCap('round'); draw('#E9E5EE'); doc.setLineWidth(8); doc.circle(cx, cy, R, 'S')
      if (s > 0) {
        draw(color); doc.setLineWidth(8)
        const start = -90, end = start + (s / 100) * 360, steps = Math.max(2, Math.round((s / 100) * 64))
        let prev = null
        for (let i = 0; i <= steps; i++) { const a = (start + (end - start) * (i / steps)) * Math.PI / 180, x = cx + R * Math.cos(a), y = cy + R * Math.sin(a); if (prev) doc.line(prev[0], prev[1], x, y); prev = [x, y] }
      }
      doc.setLineCap('butt')
      ink(color); doc.setFont('helvetica', 'bold'); doc.setFontSize(20); doc.text(String(s), cx, cy + 3, { align: 'center' })
      ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(7); doc.text('/ 100', cx, cy + 14, { align: 'center' })
    },
    // A highlighted, bordered callout — for the executive summary and the
    // conformance statement, so they read as report sections, not plain body text.
    callout(t, { color = PLUM, bg = '#F7F5F9' } = {}) {
      doc.setFont('helvetica', 'normal'); doc.setFontSize(10)
      const lines = doc.splitTextToSize(t, CW - 24)
      const h = lines.length * 14 + 20
      ensure(h + 10)
      draw(color); fill(bg); doc.setLineWidth(1.2)
      doc.roundedRect(M, st.y, CW, h, 6, 6, 'FD')
      ink(INK); doc.text(lines, M + 12, st.y + 18)
      st.y += h + 14
    },
    // A real filled donut chart (fan of thin triangles per jsPDF's fill primitive —
    // the same technique ring() uses for a stroked arc, just filled per-segment) with
    // a swatch legend — not a screenshot, not ASCII bars pretending to be a chart.
    donut(items, { R = 42, centerLabel = 'criteria' } = {}) {
      const data = (items || []).filter((it) => (it.value || 0) > 0)
      const total = data.reduce((s, it) => s + it.value, 0)
      ensure(R * 2 + 16)
      const cx = M + R + 4, cy = st.y + R
      if (total <= 0) {
        draw(LINE); fill('#F7F5F9'); doc.setLineWidth(1); doc.circle(cx, cy, R, 'FD')
        ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(9); doc.text('No data', cx, cy + 3, { align: 'center' })
      } else {
        let angle = -90
        data.forEach((it) => {
          const sweep = (it.value / total) * 360
          const steps = Math.max(1, Math.round(sweep / 6))
          fill(it.color || PLUM)
          for (let i = 0; i < steps; i++) {
            const a0 = (angle + sweep * (i / steps)) * Math.PI / 180
            const a1 = (angle + sweep * ((i + 1) / steps)) * Math.PI / 180
            doc.triangle(cx, cy, cx + R * Math.cos(a0), cy + R * Math.sin(a0), cx + R * Math.cos(a1), cy + R * Math.sin(a1), 'F')
          }
          angle += sweep
        })
        fill('#FFFFFF'); doc.circle(cx, cy, R * 0.55, 'F')
        ink(INK); doc.setFont('helvetica', 'bold'); doc.setFontSize(13); doc.text(String(total), cx, cy + 4, { align: 'center' })
        ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(7); doc.text(centerLabel, cx, cy + 13, { align: 'center' })
      }
      const lx = cx + R + 22
      let ly = cy - R + 4
      ink(INK); doc.setFont('helvetica', 'normal'); doc.setFontSize(9)
      ;(items || []).forEach((it) => {
        fill(it.color || PLUM); doc.roundedRect(lx, ly - 7, 9, 9, 2, 2, 'F')
        ink(INK); doc.text(`${it.label} — ${it.value}`, lx + 14, ly)
        ly += 15
      })
      st.y = Math.max(cy + R, ly - 15) + 18
    },
    metricGrid(cards) {
      const n = cards.length, gp = 10, cw = (CW - (n - 1) * gp) / n, ch = 56
      ensure(ch + 6)
      cards.forEach((c, i) => {
        const x = M + i * (cw + gp); draw(LINE); fill('#FBFAFC'); doc.setLineWidth(0.8); doc.roundedRect(x, st.y, cw, ch, 6, 6, 'FD')
        ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(8); doc.text(String(c.label).toUpperCase(), x + 10, st.y + 16)
        ink(c.color || INK); doc.setFont('helvetica', 'bold'); doc.setFontSize(19); doc.text(String(c.value), x + 10, st.y + 42)
      })
      st.y += ch + 16
    },
    barChart(items, { labelW = 130, max, suffix = '' } = {}) {
      if (!items || !items.length) { this.text('No data.', { color: MUTED, size: 9.5 }); return }
      const mx = max || Math.max(1, ...items.map((i) => i.value)); const barH = 12, gp = 9, trackW = CW - labelW - 50
      items.forEach((it) => {
        ensure(barH + gp); const cy = st.y
        ink(INK); doc.setFont('helvetica', 'normal'); doc.setFontSize(9.5); doc.text(doc.splitTextToSize(String(it.label), labelW - 6)[0], M, cy + 9.5)
        const tx = M + labelW; fill('#EEECF0'); doc.roundedRect(tx, cy, trackW, barH, 3, 3, 'F')
        const w = Math.max(2, trackW * (it.value / mx)); fill(it.color || PLUM); doc.roundedRect(tx, cy, w, barH, 3, 3, 'F')
        ink(INK); doc.setFont('helvetica', 'bold'); doc.setFontSize(9.5); doc.text(String(it.value) + suffix, tx + trackW + 6, cy + 9.5)
        st.y += barH + gp
      })
      st.y += 8
    },
    table(headers, rows, widths) {
      const cols = widths || headers.map(() => CW / headers.length); const headH = 19
      ensure(headH); fill(PLUM); doc.rect(M, st.y, CW, headH, 'F'); ink('#FFFFFF'); doc.setFont('helvetica', 'bold'); doc.setFontSize(9)
      let hx = M; headers.forEach((h, i) => { doc.text(String(h), hx + 6, st.y + 12.5); hx += cols[i] }); st.y += headH
      rows.forEach((r, ri) => {
        doc.setFont('helvetica', 'normal'); doc.setFontSize(8.8)
        const cellLines = r.map((c, i) => doc.splitTextToSize(String(c), cols[i] - 10)); const hh = Math.max(headH, ...cellLines.map((l) => l.length * 11 + 7))
        ensure(hh); if (ri % 2) { fill('#F7F5F9'); doc.rect(M, st.y, CW, hh, 'F') }
        ink(INK); let cx = M; cellLines.forEach((lines, i) => { doc.text(lines, cx + 6, st.y + 12); cx += cols[i] })
        draw(LINE); doc.setLineWidth(0.5); doc.line(M, st.y + hh, M + CW, st.y + hh); st.y += hh
      })
      st.y += 14
    },
    save(name) {
      const pages = doc.getNumberOfPages()
      const brand = 'mova.io · Accessibility Platform' + (footerVersion ? ` · v${footerVersion}` : '')
      for (let p = 1; p <= pages; p++) {
        doc.setPage(p); draw(LINE); doc.setLineWidth(0.8); doc.line(M, H - FOOT + 10, M + CW, H - FOOT + 10)
        ink(MUTED); doc.setFont('helvetica', 'normal'); doc.setFontSize(8)
        doc.text(brand, M, H - FOOT + 25)
        doc.text('Confidential', W / 2, H - FOOT + 25, { align: 'center' })
        doc.text(`Page ${p} of ${pages}`, M + CW, H - FOOT + 25, { align: 'right' })
        if (footerGenerated) { doc.setFontSize(7); doc.text(`Generated ${footerGenerated}`, M, H - FOOT + 34) }
      }
      doc.save(name)
    },
  }
}

const ACTION_LABEL = { auto: 'Auto-fix (deterministic)', assisted: 'AI-assisted + review', review: 'Human review', manual: 'Manual rebuild', archive: 'Archive', keep: 'Keep as-is' }

// Quarterly governance report (Overview) — a detailed, board-ready document.
export async function exportGovernanceReport(d) {
  const p = await makeDoc({ title: 'Quarterly Accessibility Governance Report' })
  if (d.score != null) p.ring(d.score, d.score >= 90 ? GREEN : AMBER)
  p.cover({
    title: 'Quarterly Accessibility Governance Report',
    subtitle: `${d.org} · ${d.quarter} · WCAG 2.1 AA`,
    meta: [`Prepared for leadership · ${d.date}`, `Scope: ${d.scope || 'full document estate'}`],
  })

  p.heading('Executive summary')
  if (d.verdict) p.text(`Risk verdict — ${d.verdict[0]}`, { bold: true, color: d.verdict[1], size: 13, gapAfter: 7 })
  p.text(d.summary)
  p.metricGrid([
    { label: 'Documents', value: Number(d.total).toLocaleString() },
    // '—' for an unmeasured estate: `null + '%'` printed a literal "null%" into a report a
    // customer hands to an auditor, and `0%` would have been a claim nothing supports.
    { label: 'No blocking findings', value: d.certifiable ?? '—', color: GREEN },
    { label: 'Remediation backlog', value: d.needFix, color: AMBER },
    { label: 'Audit-ready', value: d.auditReady == null ? '—' : d.auditReady + '%' },
  ])

  p.heading('Compliance posture')
  p.text(`Estate accessibility score ${d.score ?? '—'} / 100. Open findings by severity:`, { gapAfter: 11 })
  p.barChart(d.severity)

  if (d.byLevel && d.byLevel.length) {
    p.heading('WCAG conformance by level')
    p.text('Findings by conformance level. Level AA is the legal target (ADA Title II · EAA · Section 508); Level A is the floor; AAA is enhanced.', { size: 9, color: MUTED, gapAfter: 11 })
    p.barChart(d.byLevel, { labelW: 170 })
  }

  if (d.rec && d.rec.buckets && d.rec.buckets.length) {
    // No effort or savings figures here. They came from a fixed per-finding constant
    // (sim.js recommendFor), not from measurement — and this is a conformance artifact a
    // customer relies on. Document counts per action are real; hours were never observed.
    p.heading('Remediation roadmap')
    p.table(['Remediation action', 'Documents'],
      d.rec.buckets.map((b) => [ACTION_LABEL[b.action] || b.action, String(b.n)]),
      [p.CW - 110, 110])
    if (d.lift) p.text(`Projected estate score after the queued remediation is approved and re-validated: ${d.lift.before} → ${d.lift.after} (+${d.lift.after - d.lift.before} points).`, { size: 9.5, color: GREEN })
  }

  if (d.topRisk && d.topRisk.length) {
    p.heading('Highest-risk documents')
    p.text('Ranked by finding severity weighted by public exposure — remediate these first to cut the most risk.', { size: 9, color: MUTED, gapAfter: 11 })
    p.table(['Document', 'Department', 'Score', 'Findings', 'Action', 'Effort'],
      d.topRisk.map((r) => [r.file, r.dept, String(r.score), String(r.findings), ACTION_LABEL[r.action] || r.action, r.eta]),
      [p.CW - 110 - 46 - 60 - 110 - 52, 110, 46, 60, 110, 52])
  }

  if (d.legal) {
    p.heading('Legal exposure & compliance deadlines')
    p.text(`${d.legal.publicCritical} public-facing or high-traffic document(s) carry a critical Level-A barrier — the highest-exposure items under accessibility law, where remediation should begin immediately.`, { gapAfter: 8 })
    p.text('Statutory deadlines in scope:', { bold: true, size: 9.5, gapAfter: 5 })
    p.text('•  ADA Title II (DOJ web/mobile rule): public entities with 50,000+ population by April 24, 2026; smaller entities by April 24, 2027.\n•  European Accessibility Act / EN 301 549: in force June 28, 2025.\n•  Section 508: applies to U.S. federal agencies and recipients on an ongoing basis.', { size: 9.5, lh: 14 })
  }

  if (d.deptScores && d.deptScores.length) {
    p.heading('Performance by department')
    p.barChart(d.deptScores, { labelW: 160, max: 100, suffix: ' / 100' })
  }

  if (d.ontology && d.ontology.classified) {
    p.heading('Business ontology')
    p.text(`Ontology v${d.ontology.ver ?? 1} is active. ${d.ontology.classified} of ${Number(d.total).toLocaleString()} documents are classified by your organization's own rules — ${d.ontology.crit} Critical and ${d.ontology.high} High elevated in the remediation queue ahead of generic severity.`)
  }

  if (d.criteria && d.criteria.length) {
    p.heading('WCAG criteria — findings & platform status')
    p.text('Every success criterion with findings in the estate, and how the platform handles it today (matches the coverage matrix & method deck).', { size: 9, color: MUTED, gapAfter: 11 })
    p.table(['Criterion', 'Findings', 'Platform status'],
      d.criteria.slice(0, 28).map((c) => [`${c.sc}  ${c.label}`, String(c.count), statusFor(c.sc)]),
      [p.CW - 70 - 150, 70, 150])
  } else if (d.topViolations && d.topViolations.length) {
    p.heading('Top barriers')
    p.text(d.topViolations.map((v, i) => `${i + 1}.  ${v.label} — ${v.value} document${v.value === 1 ? '' : 's'}`).join('\n'), { lh: 15 })
  }

  p.save(d.filename || 'mova-quarterly-governance-report.pdf')
}

const SEV_RANK = { critical: 0, serious: 1, moderate: 2, minor: 3 }
const SEV_CLR = { critical: '#1F5FA8', serious: '#2A5E9E', moderate: '#854F0B', minor: '#888780' }
const PRINCIPLE = { 1: 'Perceivable', 2: 'Operable', 3: 'Understandable', 4: 'Robust' }
const PRIN_CLR = { 1: '#1F5FA8', 2: '#3B6D11', 3: '#854F0B', 4: '#4B3460' }

// Map WCAG SC number prefix → affected disability group
const SC_IMPACT = [
  { prefix: '1.1', group: 'Blind / low vision', detail: 'Cannot perceive non-text content without alt text' },
  { prefix: '1.2', group: 'Deaf / hard-of-hearing', detail: 'Cannot access audio/video content without captions or transcripts' },
  { prefix: '1.3', group: 'Screen reader users', detail: 'Lose structural meaning without programmatic relationships (headings, tables, reading order)' },
  { prefix: '1.4', group: 'Low vision', detail: 'Struggle with insufficient color contrast or text that cannot be resized' },
  { prefix: '2.1', group: 'Keyboard-only users', detail: 'Cannot reach or operate controls without a mouse' },
  { prefix: '2.4', group: 'Cognitive / navigation', detail: 'Disoriented without descriptive titles, headings, and clear link labels' },
  { prefix: '3.1', group: 'Screen reader users', detail: 'Mispronunciation when document language is not declared' },
  { prefix: '3.3', group: 'Cognitive', detail: 'Cannot recover from errors without clear labels and guidance' },
]
function impactGroups(findings) {
  const seen = new Set()
  const out = []
  findings.forEach((f) => {
    const sc = (f.wcag || '').replace(/^(\d+\.\d+).*/, '$1')
    const hit = SC_IMPACT.find((x) => sc.startsWith(x.prefix))
    if (hit && !seen.has(hit.group)) { seen.add(hit.group); out.push(hit) }
  })
  return out
}
// Rough per-finding manual effort estimate (minutes), weighted by severity

// Per-document conformance report (Upload) — a detailed, beautiful, LLM-narrated PDF.
export async function exportDocumentReport(d) {
  const before = d.score ?? 0
  const after = d.finalScore ?? (d.status && /pending/i.test(d.status) ? before : 100)
  const findings = d.findings || []
  const p = await makeDoc({ title: `Accessibility Assessment Report — ${d.file}` })
  p.ring(after, after >= 90 ? GREEN : AMBER)
  p.cover({
    title: 'Accessibility Assessment Report',
    subtitle: d.file,
    meta: [`Assessed ${d.date}${d.engine ? ` · ${d.engine}` : ''}${d.assignee ? ` · Assigned to ${d.assignee}` : ''}`, `WCAG ${d.wcagVersion || '2.1'} AA · ${d.status || 'Remediated'}`],
  })

  // Executive summary — model-narrated when available, otherwise a deterministic fallback.
  // The attribution names the model that actually produced it. It previously read
  // "generated by Claude (Anthropic Opus 4.8)" in a customer's conformance report; ACP has
  // no Anthropic client and every AI call goes to a local Ollama model.
  p.heading('Executive summary')
  const ins = d.insight
  if (ins && ins.summary) {
    if (ins.headline) p.text(ins.headline, { bold: true, color: after >= 90 ? GREEN : AMBER, size: 13, gapAfter: 8 })
    p.text(ins.summary)
    if (ins.impact) p.text(`User impact — ${ins.impact}`, { size: 9.5, gapAfter: 7 })
    if (ins.recommendation) p.text(`Recommendation — ${ins.recommendation}`, { size: 9.5, color: PLUM, gapAfter: 6 })
    // `provider` is set only when a THIRD-PARTY service answered (aiRemediate.generateInsights).
    // The no-third-party sentence is printed only when nothing external was called.
    p.text(`Executive summary generated from this document’s findings by ${ins.model || 'the configured model'}`
           + (ins.provider ? ` via ${ins.provider}.` : '. No content was sent to a third-party AI service.'),
           { size: 8, color: MUTED, lh: 11 })
  } else {
    p.text(`As received, ${d.file} scored ${before} / 100 against WCAG 2.1 AA with ${findings.length} finding(s). After automated remediation and human review it was certified at ${after} / 100 and re-validated. This report documents the assessment and the fixes applied for audit and evidence.`)
  }

  // Result + compliance lift
  p.heading('Result')
  p.metricGrid([
    { label: 'As received', value: `${before}/100`, color: before >= 90 ? GREEN : AMBER },
    { label: 'After remediation', value: `${after}/100`, color: after >= 90 ? GREEN : AMBER },
    { label: 'Findings', value: findings.length },
    { label: 'Auto-fixed', value: d.autoFix ?? 0, color: GREEN },
  ])
  p.barChart([
    { label: 'As received', value: before, color: '#1F5FA8' },
    { label: 'After remediation', value: after, color: after >= 90 ? GREEN : AMBER },
  ], { labelW: 150, max: 100, suffix: ' / 100' })

  if (findings.length) {
    // severity + principle breakdowns
    const sevCount = {}; findings.forEach((f) => { const s = (f.sev || '').toLowerCase(); if (SEV_RANK[s] != null) sevCount[s] = (sevCount[s] || 0) + 1 })
    const sevItems = Object.keys(SEV_RANK).filter((s) => sevCount[s]).map((s) => ({ label: s, value: sevCount[s], color: SEV_CLR[s] }))
    const prinCount = {}; findings.forEach((f) => { const k = (f.wcag || '').match(/(\d)/)?.[1]; if (PRINCIPLE[k]) prinCount[k] = (prinCount[k] || 0) + 1 })
    const prinItems = Object.keys(PRINCIPLE).filter((k) => prinCount[k]).map((k) => ({ label: PRINCIPLE[k], value: prinCount[k], color: PRIN_CLR[k] }))

    p.heading('Findings by severity')
    p.barChart(sevItems, { labelW: 110 })
    if (prinItems.length) {
      p.heading('Findings by WCAG principle')
      p.text('The four WCAG principles — content must be Perceivable, Operable, Understandable, and Robust.', { size: 9, color: MUTED, gapAfter: 11 })
      p.barChart(prinItems, { labelW: 150 })
    }

    p.heading('Findings & remediation')
    const sorted = [...findings].sort((a, b) => (SEV_RANK[(a.sev || '').toLowerCase()] ?? 9) - (SEV_RANK[(b.sev || '').toLowerCase()] ?? 9))
    p.table(['WCAG criterion', 'Severity', 'Finding', 'Remediation applied'],
      sorted.map((f) => [f.wcag, (f.sev || '').toLowerCase(), f.detail, f.fix || 'remediated & re-validated']),
      [115, 60, (p.CW - 175) * 0.46, (p.CW - 175) * 0.54])
  } else {
    p.heading('Findings')
    p.text('No accessibility findings — the document meets WCAG 2.1 AA.', { color: GREEN })
  }

  // Affected user groups
  const groups = impactGroups(findings)
  if (groups.length) {
    p.heading('Affected user groups')
    p.text('The following groups of users with disabilities were unable to access this document as received. Each barrier has been remediated.', { size: 9.5, color: MUTED, gapAfter: 11 })
    groups.forEach((g) => {
      p.text(`${g.group}`, { bold: true, size: 10, gapAfter: 2 })
      p.text(g.detail, { size: 9.5, color: MUTED, gapAfter: 8 })
    })
    p.gap(4)
  }

  // What the platform actually did to this document.
  const autoN = d.autoFix ?? 0
  const humanN = d.humanReview ?? 0
  const totalN = findings.length || autoN + humanN
  // The "manual effort" / "effort saved" figures that used to live here were derived from a
  // per-severity minutes constant nobody measured, then printed as a quantified savings claim
  // in a customer-facing report. Removed. What follows is only what actually happened.
  p.heading('Remediation outcome')
  p.metricGrid([
    { label: 'Findings', value: totalN, color: AMBER },
    { label: 'Auto-fixed', value: autoN, color: GREEN },
    { label: 'Human reviewed', value: humanN, color: '#1F5FA8' },
  ])
  p.text(`Of ${totalN} finding(s), the mova.io platform auto-fixed ${autoN} deterministically and routed ${humanN} to human review.`, { size: 9.5, gapAfter: 7 })
  p.text('Auto-fix methods applied: structured alt-text injection, document title + language tagging, table header row markup, link-text rewriting. Each fix was written into the file at the byte level — not a CSS overlay or metadata tag.', { size: 9, color: MUTED, lh: 13 })

  // Conformance statement (audit evidence page)
  p.heading('Methodology & audit trail')
  p.text(`Engine: ${d.engine || 'mova.io WCAG 2.1 AA static analysis'}. Scan initiated: ${d.date}. ${autoN} finding(s) auto-remediated deterministically; ${humanN} routed to human review and approved before certification. Document re-validated after all fixes were applied.`, { size: 9.5, gapAfter: 7 })
  p.text('Standards in scope: WCAG 2.1 Level AA · ADA Title II (DOJ web/mobile rule, 28 CFR Part 35) · EN 301 549 v3.2.1 (EAA) · Section 508 of the Rehabilitation Act.', { size: 9, color: MUTED, gapAfter: 7 })
  const auditRows = [
    ['Discovered', 'mova.io agent', 'Document ingested from source', d.file],
    ['Assessed', `mova.io · ${d.engine || 'WCAG 2.1 AA'}`, `${findings.length} finding(s) detected · score ${before}/100`, d.file],
    ['Auto-remediated', 'mova.io auto-fix', `${autoN} finding(s) fixed deterministically`, d.file],
    ...(humanN > 0 ? [['Human reviewed', 'Human reviewer', `${humanN} finding(s) approved after HITL review`, d.file]] : []),
    ['Re-validated', `mova.io · ${d.engine || 'WCAG 2.1 AA'}`, `Zero open findings · score ${after}/100`, d.file],
    ['Certified', 'mova.io Platform', `WCAG 2.1 AA · ${after}/100`, d.file],
  ]
  p.table(['Step', 'Actor', 'Action', 'Document'], auditRows, [62, 108, p.CW - 62 - 108 - 140, 140])

  p.heading('Conformance statement')
  p.text(`"${d.file}" has been assessed and remediated to conform with WCAG 2.1 Level AA success criteria as required by the Americans with Disabilities Act (ADA) Title II, the European Accessibility Act (EN 301 549), and Section 508 of the Rehabilitation Act.`, { size: 10, gapAfter: 8 })
  p.text(`All ${findings.length} accessibility barrier(s) identified during the automated assessment have been resolved and independently re-validated. The document score was raised from ${before}/100 to ${after}/100. This report and the remediated document together constitute an audit-ready evidence package.`, { size: 9.5, gapAfter: 10 })
  p.text('Certified by the mova.io Accessibility Platform', { bold: true, size: 9.5, gapAfter: 4 })
  p.text(`Date: ${d.date}`, { size: 9, color: MUTED, gapAfter: 4 })
  p.text('Authorised signatory: ___________________________', { size: 9, color: MUTED, gapAfter: 4 })
  p.text('Title / Role: ___________________________', { size: 9, color: MUTED, gapAfter: 16 })
  p.text('This report was generated by the mova.io Accessibility Platform and is intended as evidence for ADA, EAA, and Section 508 compliance audits. Retain for a minimum of three years.', { size: 8, color: MUTED, lh: 12 })

  p.save(d.filename || `mova-${(d.file || 'document').replace(/\.[^.]+$/, '')}-report.pdf`)
}

// Per-file WCAG certification (FileDrawer, any file, any state) — built ONLY from the
// same honest per-criterion coverage rows shown on screen (outcome PASS/FAIL/FIXED/
// HUMAN/UNCHECKED — never a fabricated per-finding narrative). The report CONTENT
// lives in reportModel.js so this PDF export and the HTML export (htmlReport.js)
// share one source and can never drift. This renderer just maps each model block
// onto the matching makeDoc primitive.
function renderModelToPdf(p, model) {
  const c = model.cover
  if (c) {
    if (c.ring) p.ring(c.ring.score, c.ring.color)
    p.cover({ title: c.title, subtitle: c.subtitle, meta: c.meta })
  }
  for (const b of model.blocks) {
    switch (b.k) {
      case 'heading': p.heading(b.text); break
      case 'text': p.text(b.text, b.o || {}); break
      case 'callout': p.callout(b.text, b.o || {}); break
      case 'bullets': p.bullets(b.items, b.o || {}); break
      case 'metricGrid': p.metricGrid(b.cards); break
      case 'donut': p.donut(b.items); break
      case 'barChart': p.barChart(b.items, b.o || {}); break
      case 'beforeAfter': p.beforeAfter(b.items); break
      case 'table': p.table(b.headers, b.rows, b.widths); break
      case 'pageBreak': p.pageBreak(); break
      case 'gap': p.gap(b.h || 8); break
      default: break
    }
  }
}

export async function exportFileCertification(d) {
  const model = buildFileCertificationModel(d)
  const p = await makeDoc({
    title: model.docTitle,
    footerVersion: model.footerVersion,
    footerGenerated: model.footerGenerated,
  })
  renderModelToPdf(p, model)
  // Page-1 preview (ADR 0015) — best-effort: rendered server-side, appended only if the
  // document can be previewed (unsupported type / source unreachable → omitted). Never blocks the cert.
  if (d.scanId && d.file) {
    try {
      const { getFileThumbnail } = await import('./api.js')
      const blob = await getFileThumbnail(d.scanId, d.file)
      if (blob) { p.heading('Document preview'); await p.imageFromBlob(blob, { caption: `Page 1 of “${d.file}”` }) }
    } catch { /* preview unavailable — continue without it */ }
  }
  p.save(`${model.filename}.pdf`)
}

// ── Scan-level (estate) accessibility report ──────────────────────────────────
// Whole-scan governance PDF: executive summary, WCAG failure heatmap by criterion,
// per-department/source breakdown, remediation throughput, HITL queue status, top
// failing criteria, and a per-document appendix. Built ONLY from real scan data
// (per-rule traces + the file list + the HITL queue) — no fabricated confidence %.
// Aggregation happens in scanReport.js (the same client-side rollup RuleBreakdown
// does); this function only renders. "No blocking findings" means exactly that: zero open
// WCAG findings among the criteria checked at the assessed target level — a real count, never
// estimated, and never a claim that the document conforms.
const STATUS_TXT = { certifiable: 'No blocking findings', issues: 'Open findings', uncertain: 'Uncertain', unanalysable: 'Unanalysable' }

export async function exportScanReport(d) {
  const pct = d.conformantPct ?? 0
  const good = pct >= 80
  const p = await makeDoc({
    title: `Estate Accessibility Report — ${d.org}`,
    footerVersion: d.platformVersion,
    footerGenerated: d.timestamp || d.date,
  })
  // No ring when nothing was scored — same guard the per-document report uses above. `?? 0`
  // drew a full red 0/100 dial on the cover of a report whose own summary said "n/a".
  if (d.avgScore != null) p.ring(d.avgScore, d.avgScore >= 90 ? GREEN : AMBER)
  p.cover({
    title: 'Estate Accessibility Report',
    subtitle: `${d.org} · scan-level conformance`,
    meta: [
      `Generated ${d.timestamp || d.date}`,
      `${Number(d.totalFiles).toLocaleString()} documents · WCAG 2.1 Level ${d.targetLevel}`,
    ],
  })

  p.heading('Executive summary')
  p.callout(
    `${d.conformantN} of ${Number(d.totalFiles).toLocaleString()} documents (${pct}%) came back with no blocking findings among the WCAG 2.1 Level ${d.targetLevel} criteria ACP checked. Estate average score ${d.avgScore ?? 'n/a'}/100. ${d.criteriaAutomated} of ${d.inScopeCount} in-scope success criteria are automatically evaluated across the estate.`,
    { color: good ? GREEN : AMBER, bg: good ? '#EEF5E8' : '#FBF1DF' }
  )
  p.metricGrid([
    { label: 'Documents', value: Number(d.totalFiles).toLocaleString() },
    { label: 'No blocking findings', value: `${pct}%`, color: good ? GREEN : AMBER },
    { label: 'Avg score', value: d.avgScore != null ? `${d.avgScore}/100` : 'n/a' },
    { label: 'Target', value: `Level ${d.targetLevel}` },
  ])
  const sc = d.statusCounts || {}
  p.bullets([
    `${sc.certifiable || 0} document(s) came back with no blocking findings at Level ${d.targetLevel}`,
    sc.issues ? `${sc.issues} document(s) with open findings to remediate` : null,
    sc.uncertain ? `${sc.uncertain} document(s) need review before a verdict` : null,
    sc.unanalysable ? `${sc.unanalysable} document(s) could not be analysed (unreadable source)` : null,
  ].filter(Boolean))
  p.text(`Measured against the assessed target (Level ${d.targetLevel}). This is a record of what ACP checked, changed and re-verified — not a determination that these documents conform to WCAG. Every figure is a real count from this scan; no confidence percentages are fabricated.`, { size: 8.5, color: MUTED, lh: 12 })

  p.heading('Outcome by document')
  p.donut([
    { label: 'No blocking findings', value: sc.certifiable || 0, color: GREEN },
    { label: 'Open findings', value: sc.issues || 0, color: '#A32D2D' },
    { label: 'Uncertain', value: sc.uncertain || 0, color: AMBER },
    { label: 'Unanalysable', value: sc.unanalysable || 0, color: '#B6B0BC' },
  ], { centerLabel: 'documents' })

  if (d.topFailing && d.topFailing.length) {
    p.heading('Top failing WCAG criteria')
    p.text('Success criteria with the most failing documents across the estate — remediate these first for the widest lift.', { size: 9, color: MUTED, gapAfter: 11 })
    p.barChart(d.topFailing.map((r) => ({ label: `${r.id}  ${r.name}`, value: r.fail, color: '#C9742B' })), { labelW: 200 })
  }

  if (d.heatmap && d.heatmap.depts.length && d.heatmap.rows.length) {
    p.heading('Where failures concentrate')
    p.text('Failing documents per criterion, by department — the estate WCAG heatmap. Each cell is a count of documents failing that criterion in that department.', { size: 9, color: MUTED, gapAfter: 11 })
    const depts = d.heatmap.depts
    const colW = Math.max(38, Math.min(72, (p.CW - 150) / depts.length))
    p.table(['Criterion', ...depts],
      d.heatmap.rows.map((r) => [`${r.id}  ${r.name}`, ...r.cells.map((n) => n || '·')]),
      [p.CW - colW * depts.length, ...depts.map(() => colW)])
    if (d.heatmap.moreDepts) p.text(`+${d.heatmap.moreDepts} more department(s) with failures not shown here — the full grid is in the Assess view.`, { size: 8.5, color: MUTED })
  }

  if (d.byDept && d.byDept.length) {
    p.pageBreak()
    p.heading('By department')
    p.table(['Department', 'Docs', 'Avg score', 'Conformant', 'Findings'],
      d.byDept.map((r) => [r.label, String(r.docs), r.avg != null ? `${r.avg}/100` : 'n/a', `${r.conformant}/${r.docs}`, String(r.findings)]),
      [p.CW - 60 - 90 - 90 - 70, 60, 90, 90, 70])
    const bars = d.byDept.filter((r) => r.avg != null).map((r) => ({ label: r.label, value: r.avg, color: r.avg >= 90 ? GREEN : r.avg >= 50 ? AMBER : '#1F5FA8' })).sort((a, b) => a.value - b.value)
    if (bars.length) { p.text('Average score by department (lowest first):', { size: 9.5, gapAfter: 8 }); p.barChart(bars, { labelW: 150, max: 100, suffix: ' / 100' }) }
  }

  if (d.bySource && d.bySource.length) {
    p.heading('By source system')
    p.table(['Source', 'Docs', 'Avg score', 'Conformant'],
      d.bySource.map((r) => [r.label, String(r.docs), r.avg != null ? `${r.avg}/100` : 'n/a', `${r.conformant}/${r.docs}`]),
      [p.CW - 60 - 90 - 90, 60, 90, 90])
  }

  p.pageBreak()
  p.heading('Remediation throughput')
  p.text('How the platform routes each document: deterministic auto-fix, routed to a human, or deferred (kept as-is or archived). Routing is derived from each document’s findings and format.', { size: 9, color: MUTED, gapAfter: 10 })
  const rt = d.routing || {}
  p.metricGrid([
    { label: 'Auto-fixed', value: rt.fixed || 0, color: GREEN },
    { label: 'Human-routed', value: rt.humanRouted || 0, color: '#1F5FA8' },
    { label: 'Deferred', value: rt.deferred || 0, color: MUTED },
    { label: 'Automation rate', value: `${d.effort?.autoPct ?? 0}%`, color: GREEN },
  ])
  if (rt.buckets && rt.buckets.length) {
    p.table(['Routing', 'Documents'],
      rt.buckets.map((b) => [ACTION_LABEL[b.action] || b.action, String(b.n)]),
      [p.CW - 130, 130])
  }

  p.heading('Human-in-the-loop review queue')
  const h = d.hitl || {}
  if ((h.total || 0) === 0) {
    p.text('No documents from this scan are currently in the human review queue.', { color: MUTED })
  } else {
    p.metricGrid([
      { label: 'Pending', value: h.pending || 0, color: h.pending ? AMBER : GREEN },
      { label: 'Approved', value: h.approved || 0, color: GREEN },
      { label: 'Rejected', value: h.rejected || 0, color: h.rejected ? '#A32D2D' : MUTED },
      { label: 'Total routed', value: h.total || 0 },
    ])
    p.text(`${h.pending || 0} item(s) await a reviewer; ${h.approved || 0} approved and ${h.rejected || 0} rejected${h.skipped ? `, ${h.skipped} skipped` : ''}. Items route here only when a fix needs human judgement or a human-authored value (alt text, captions, contrast, link purpose).`, { size: 9.5 })
  }

  // Manual remediation checklist — where to find and fix each broken item by hand, on Mac and PC.
  const cl = d.manualChecklist || []
  if (cl.length) {
    p.pageBreak()
    p.heading('Manual remediation checklist — where to fix each item')
    p.text('For every WCAG criterion the scan found failing, this is where to find it and how to fix it by hand in the file’s own app — on macOS and on Windows. Tick each item as you complete it. These are the manual steps for items that need human judgement; the platform auto-fixes the deterministic ones separately.', { size: 9, color: MUTED, lh: 13, gapAfter: 6 })
    p.text('One row per criterion × file type. “In:” names the app to open (Word / Excel / PowerPoint / Acrobat Pro) and how many documents are affected.', { size: 8.5, color: MUTED, lh: 12, gapAfter: 11 })
    const cbW = 26
    const itemW = (p.CW - cbW) * 0.30
    const stepW = (p.CW - cbW - itemW) / 2
    const rows = cl.map((it) => {
      const lvl = it.level ? ` (${it.level})` : ''
      const item = `${it.sc}  ${it.name}${lvl}\nIn: ${it.app} · ${it.docs} file${it.docs === 1 ? '' : 's'}`
                 + (it.where ? `\n${it.where}` : '')
      return ['[  ]', item, it.mac || '', it.win || '']
    })
    p.table(['Done', 'Broken item — where to find it', 'Fix on Mac', 'Fix on Windows / PC'],
      rows, [cbW, itemW, stepW, stepW])
    if (d.checklistTruncated) {
      p.text(`Showing the ${cl.length} most actionable of ${d.checklistTotal} criterion×format items. The full list is in the Assess view.`, { size: 8.5, color: MUTED })
    }
    p.text('App menu paths reflect current Microsoft 365 and Adobe Acrobat Pro. Wording can vary slightly by version; the built-in Accessibility Checker (Review → Check Accessibility) will always locate the flagged item.', { size: 8, color: MUTED, lh: 11 })
  }

  p.pageBreak()
  p.heading('Appendix · documents & outcomes')
  const ap = d.appendix || []
  p.text(`Every document in the scan with its conformance outcome and remediation routing${ap.length < d.totalFiles ? ` (lowest-scoring ${ap.length} of ${Number(d.totalFiles).toLocaleString()} shown)` : ''}.`, { size: 9, color: MUTED, gapAfter: 10 })
  p.table(['Document', 'Department', 'Score', 'Status', 'Routing'],
    ap.map((r) => [r.file, r.dept, r.score != null ? String(r.score) : 'n/a', STATUS_TXT[r.status] || r.status, ACTION_LABEL[r.action] || r.action || '—']),
    [p.CW - 96 - 44 - 78 - 96, 96, 44, 78, 96])

  p.save(d.filename || 'mova-estate-accessibility-report.pdf')
}

// Immutable evidence package (Monitor) — the who/when/what/which-engine audit trail.
export async function exportEvidenceReport(d) {
  const p = await makeDoc({ title: 'Accessibility Evidence Package' })
  p.cover({
    title: 'Accessibility Evidence Package',
    subtitle: `${d.org} · immutable audit trail`,
    meta: [`Generated ${d.date}`, 'Standards: WCAG 2.1 AA · ADA Title II · EN 301 549 (EAA)'],
  })
  p.heading('Continuous monitoring')
  p.text(d.summary)
  if (d.metrics && d.metrics.length) p.metricGrid(d.metrics)
  p.heading('Audit trail')
  p.text('Every remediation, review, publish and re-scan is logged with the actor, the change, the document and the engine — append-only for audit.', { size: 9, color: MUTED, gapAfter: 11 })
  p.table(['Action', 'Actor', 'Change', 'Document'], d.events.map((e) => [e.action, e.actor, e.change, e.document]), [78, 92, p.CW - 78 - 92 - 150, 150])
  p.gap(2)
  p.text(`This evidence package was generated on ${d.date} from the continuous-monitoring log. Entries are immutable and timestamped for ADA / EAA audit and investigation.`, { size: 8.5, color: MUTED, lh: 12 })
  p.save(d.filename || 'mova-evidence-package.pdf')
}

// Accessibility Conformance Report (VPAT-style ACR) for the platform UI itself.
const ACR = [
  ['Perceivable', [
    ['1.1.1', 'Non-text Content', 'A', 'Supports', 'Icons/images labeled or decorative; charts use role="img" + descriptive aria-label.'],
    ['1.3.1', 'Info & Relationships', 'A', 'Supports', 'Headings, lists, tables, form labels and landmarks (header/main/nav).'],
    ['1.3.2', 'Meaningful Sequence', 'A', 'Supports', 'DOM order matches the visual order.'],
    ['1.4.1', 'Use of Color', 'A', 'Supports', 'Graph status uses glyphs + colour; legends carry text labels.'],
    ['1.4.3', 'Contrast (Minimum)', 'AA', 'Partially Supports', 'Known failure: the September 2026 self-assessment reported insufficient contrast on the update banner text. A fix is implemented locally; it is not yet deployed or verified in a deployed build.'],
    ['1.4.4 / 1.4.10', 'Resize / Reflow', 'AA', 'Supports', 'Responsive; zoom is not blocked.'],
    ['1.4.11', 'Non-text Contrast', 'AA', 'Partially Supports', 'UI marks and graph dots corrected to at least 3:1. On the update banner, the dismiss glyph and focus outline computed below 3:1 from source; a fix is pending deployed-build verification.'],
    ['1.4.12', 'Text Spacing', 'AA', 'Supports', 'No clipping when spacing is overridden.'],
    ['1.4.13', 'Content on Hover or Focus', 'AA', 'Needs verification', 'Applies: tooltips open on hover and focus. Dismissible, hoverable and persistent behaviour not yet tested.'],
  ]],
  ['Operable', [
    ['2.1.1', 'Keyboard', 'A', 'Supports', 'All controls operable; graph uses roving tabindex (arrows / Enter / Escape).'],
    ['2.1.2', 'No Keyboard Trap', 'A', 'Supports', 'Dialogs trap intentionally; Escape always exits.'],
    ['2.4.1', 'Bypass Blocks', 'A', 'Supports', 'Skip-to-main link.'],
    ['2.4.2', 'Page Titled', 'A', 'Supports', 'Document title set.'],
    ['2.4.3', 'Focus Order', 'A', 'Supports', 'Logical order; no positive tabindex.'],
    ['2.4.4', 'Link Purpose (In Context)', 'A', 'Supports', 'Link text is meaningful.'],
    ['2.4.6', 'Headings & Labels', 'AA', 'Supports', 'Descriptive headings and labels.'],
    ['2.4.7', 'Focus Visible', 'AA', 'Supports', ':focus-visible outline on all controls.'],
    ['2.5.3', 'Label in Name', 'A', 'Supports', 'Visible labels match accessible names.'],
  ]],
  ['Understandable', [
    ['3.1.1', 'Language of Page', 'A', 'Supports', 'html lang attribute set.'],
    ['3.2.1 / 3.2.2', 'On Focus / On Input', 'A', 'Supports', 'No unexpected change of context.'],
    ['3.2.3 / 3.2.4', 'Consistent Navigation / Identification', 'AA', 'Supports', 'Consistent navigation and component identity.'],
    ['3.3.1 / 3.3.2', 'Error Identification / Labels', 'A', 'Supports', 'Inputs labeled; forms are minimal.'],
  ]],
  ['Robust', [
    ['4.1.2', 'Name, Role, Value', 'A', 'Partially Supports', 'Correct roles and accessible names on custom controls. DOM-level tests found tab-pattern gaps in several in-page tab sets (fix pending deployment); not verified with a screen reader.'],
    ['4.1.3', 'Status Messages', 'AA', 'Needs verification', 'aria-live / role=status markup is present on scan, chat, monitor and assess results; announcement has not been verified with a screen reader.'],
  ]],
]
export async function exportConformanceReport(d = {}) {
  const date = d.date || new Date().toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' })
  const p = await makeDoc({ title: 'Accessibility Conformance Report — mova.io Platform' })
  p.cover({
    title: 'Accessibility Conformance Report',
    subtitle: `${d.org || 'mova.io Accessibility Platform'} · UI conformance + document coverage`,
    meta: [`WCAG 2.1 + 2.2 · ${date}`, 'Evaluation: partial automated checks (axe-core) + code / DOM-level review — no screen-reader testing'],
  })
  p.heading('Summary')
  p.text('This report covers two things: (1) the conformance of the mova.io Accessibility Platform’s own user interface, and (2) the WCAG coverage the platform provides for the documents it processes.')
  p.text('Conformance of the platform UI to WCAG 2.1 Level AA is not established. A known 1.4.3 contrast failure on the update banner has a locally implemented fix that is not yet deployed or verified, several criteria lack assistive-technology evidence, and no screen-reader evaluation has been performed. Two issues found during an earlier manual review (an unannounced status update and a missing navigation landmark) were remediated.')
  p.heading('Part 1 · Platform UI conformance (WCAG 2.1 AA)')
  p.text('Conformance key:  Supports · Partially Supports · Not Applicable · Needs verification (evidence insufficient to claim either way). Rows without recorded manual or assistive-technology evidence should be read as needing verification.', { size: 9, color: MUTED, gapAfter: 4 })
  for (const [principle, rows] of ACR) {
    p.heading(principle)
    p.table(['Criterion', 'Lvl', 'Conformance', 'Notes'],
      rows.map((r) => [`${r[0]}  ${r[1]}`, r[2], r[3], r[4]]),
      [148, 28, 92, p.CW - 268])
  }

  // Part 2 — what the platform does for customer documents
  const cov = { live: 0, hitl: 0, partner: 0, road: 0 }
  const byLevel = { A: { tot: 0, cov: 0 }, AA: { tot: 0, cov: 0 }, AAA: { tot: 0, cov: 0 } }
  WCAG.forEach((c) => {
    const s = c.source
    if (s === 'Shipped (demo)') cov.live++
    else if (s === 'MDK HITL') cov.hitl++
    else if (s === 'Partner baseline') cov.partner++
    else cov.road++
    const lv = byLevel[c.level]; if (lv) { lv.tot++; if (s !== 'MDK net-new') lv.cov++ }
  })
  p.heading('Part 2 · Document remediation coverage (WCAG 2.1 + 2.2)')
  p.text('Beyond its own conformance, the platform detects and remediates accessibility issues in the documents it processes. Coverage across all 87 success criteria:', { gapAfter: 10 })
  p.metricGrid([
    { label: 'Live · auto/AI', value: cov.live, color: GREEN },
    { label: 'Covered · HITL', value: cov.hitl, color: '#1F5FA8' },
    { label: 'Partner (web)', value: cov.partner, color: PLUM },
    { label: 'Roadmap', value: cov.road, color: AMBER },
  ])
  p.table(['Conformance level', 'Criteria', 'Covered', 'Status'],
    [
      ['Level A · must-have', String(byLevel.A.tot), `${byLevel.A.cov} / ${byLevel.A.tot}`, byLevel.A.cov === byLevel.A.tot ? 'Fully covered' : `${byLevel.A.tot - byLevel.A.cov} in progress`],
      ['Level AA · legal target', String(byLevel.AA.tot), `${byLevel.AA.cov} / ${byLevel.AA.tot}`, byLevel.AA.cov === byLevel.AA.tot ? 'Every criterion has a coverage method' : `${byLevel.AA.tot - byLevel.AA.cov} in progress`],
      ['Level AAA · optional', String(byLevel.AAA.tot), `${byLevel.AAA.cov} / ${byLevel.AAA.tot}`, `${byLevel.AAA.tot - byLevel.AAA.cov} optional (human-produced media) remaining`],
    ],
    [p.CW - 70 - 70 - 210, 70, 70, 210])
  p.text('"Covered" means the platform has a capability for the criterion — deterministic auto-fix, AI, the partner web scanner, or a human-in-the-loop review workflow — as classified in the capability matrix. It is capability coverage, not conformance: it does not mean any individual output document conforms to WCAG at Level A or AA, which depends on that document\'s own assessment and review. The full per-criterion matrix is available as the accompanying coverage matrix (Excel) and method deck (PowerPoint).', { size: 9.5, gapAfter: 6 })

  p.heading('Evaluation method & scope')
  p.text('Part 1 (UI assessment): automated axe-core checks and source / DOM-level review provide partial evidence only; automated checks do not establish conformance. The September 2026 self-assessment reported a 1.4.3 contrast failure on the update banner, which requires deployed-build retesting. Screen-reader testing has not been performed, and complete workflow, keyboard, focus and screen-reader announcement evidence remains pending. Part 2 (document coverage): each success criterion is classified by what the platform’s detect-and-remediate engines do today — deterministic auto-fix, AI, partner web scanner, or human-in-the-loop review.', { size: 9.5, gapAfter: 6 })
  p.text('Not yet performed: formal screen-reader user testing (NVDA / JAWS / VoiceOver) — recommended to finalize a signed conformance statement.', { size: 9.5, color: AMBER })
  p.save(d.filename || 'mova-accessibility-conformance-report.pdf')
}

// Remediation report — the record of WORK DONE, and the checklist a human signs off against.
//
// Deliberately not the certification report. That one answers "does this estate conform?"; this
// one answers "what did you change to my documents, when, and how do I check it myself?" —
// which is the question asked by the person who has to defend the estate, and the one a
// screenshot of a dashboard cannot answer.
export async function exportRemediationReport(d = {}) {
  const scNames = Object.fromEntries(WCAG.map((w) => [w.sc, w.name]))
  const m = buildRemediationModel({ ...d, scNames })
  const p = await makeDoc({
    title: `Remediation Report${m.org ? ' — ' + m.org : ''}`,
    footerVersion: d.platformVersion,
    footerGenerated: m.generatedAt,
  })

  p.cover({
    logoRight: true,
    title: 'Accessibility Remediation Report',
    subtitle: `${m.org || 'Document estate'} · WCAG 2.1 Level ${m.level}`,
    meta: [
      `Generated ${m.generatedAt}`,
      m.scanId ? `Scan ${m.scanId}` : null,
      `${m.totals.documents} document${m.totals.documents === 1 ? '' : 's'} remediated · ${m.totals.items} change${m.totals.items === 1 ? '' : 's'} across ${m.totals.criteria} success criteri${m.totals.criteria === 1 ? 'on' : 'a'}`,
    ].filter(Boolean),
  })

  p.heading('What this document is')
  p.callout('Every change listed here has already been written to the document. This report is the record of that work and the checklist for confirming it: tick each item once you have verified it in the application named in the “How to verify” section. Nothing here asks you to make a change — only to confirm one.')

  p.metricGrid([
    { label: 'Documents', value: m.totals.documents },
    { label: 'Changes', value: m.totals.items },
    { label: 'Criteria', value: m.totals.criteria },
    { label: 'Awaiting review', value: m.totals.awaiting, color: m.totals.awaiting ? AMBER : GREEN },
  ])

  // Two honesty notes, both of which a report is tempted to omit.
  if (m.totals.unstamped) {
    p.text(`${m.totals.unstamped} of ${m.totals.items} changes carry no recorded time. The remediation record stores what changed, and a separate table stores when a value was written; where the two do not join, this report says “time not recorded” rather than substituting the document’s own timestamp.`,
           { size: 9, color: MUTED })
  }
  if (m.partial) {
    p.callout(`This report is PARTIAL. The server returned the maximum of ${m.partial} fix records, so changes beyond that limit are not listed. Re-run against a narrower scan to see the remainder.`,
              { color: AMBER, bg: '#FBF1DF' })
  }
  if (!m.documents.length) {
    p.text('No remediation has been applied to this scan yet, so there is nothing to confirm.', { color: MUTED })
    p.save(d.filename || 'mova-remediation-report.pdf')
    return
  }

  for (const doc of m.documents) {
    p.pageBreak()
    // NOT p.heading(): it upper-cases, and a filename is a case-sensitive identifier.
    // "CLINICAL-FAQ-39.DOCX" is not the name of the file this section is about, and on a
    // case-sensitive store it is a different file entirely — which defeats the one job this
    // report has, namely telling a reviewer exactly which document to open.
    p.docTitle(doc.name)
    const meta = [
      doc.dir ? `Folder: ${doc.dir}` : 'Folder: (root)',
      `Format: .${doc.fmt}`,
      doc.remediatedAt ? `Remediated: ${doc.remediatedAt}` : 'Remediated: time not recorded',
      doc.awaiting ? `${doc.awaiting} item${doc.awaiting === 1 ? '' : 's'} still awaiting review in the app` : null,
    ].filter(Boolean)
    p.text(meta.join('   ·   '), { size: 9, color: MUTED, gapAfter: 12 })

    if (!doc.items.length) {
      p.text('This document was remediated, but no per-change record was stored for it.', { size: 9.5, color: MUTED })
      continue
    }

    p.text('Confirm each change:', { size: 10, bold: true, gapAfter: 8 })
    p.checklist(doc.items.map((it) => ({
      id: `${doc.file}_${it.sc}`.replace(/[^A-Za-z0-9_]/g, '_'),
      label: `${it.sc} ${it.name}${it.note ? ' — ' + it.note : ''}`,
      meta: it.at ? `Applied ${it.at}` : 'Applied — time not recorded',
    })))

    const evidence = doc.items.filter((it) => it.before != null || it.after != null)
    if (evidence.length) {
      p.text('What changed', { size: 10, bold: true, gapAfter: 6 })
      p.beforeAfter(evidence.map((it) => ({ label: `${it.sc} ${it.name}`, note: it.note, before: it.before, after: it.after })))
    }
  }

  // How to verify, per format actually present — Mac AND Windows, because the reviewer is on one
  // of them and a report that guesses wrong sends them hunting through the wrong menus.
  p.pageBreak()
  p.heading('How to verify these changes yourself')
  p.text('Open each document in the application below and confirm the items you ticked. The menu paths differ between macOS and Windows, so both are given.', { size: 9.5, color: MUTED })
  for (const fmt of m.formats) {
    const g = VERIFY_GUIDE[fmt]
    if (!g) continue
    p.text(`.${fmt} — ${g.app}`, { size: 11, bold: true, color: PLUM, gapAfter: 6 })
    p.text('On macOS', { size: 9.5, bold: true, gapAfter: 4 })
    p.bullets(g.mac || [], { size: 9.5 })
    p.text('On Windows', { size: 9.5, bold: true, gapAfter: 4 })
    p.bullets(g.win || [], { size: 9.5 })
    if (g.checks?.length) {
      p.text('What to look for', { size: 9.5, bold: true, gapAfter: 4 })
      p.bullets(g.checks, { size: 9.5 })
    }
    if (g.sr?.length) {
      p.text('Screen-reader spot check', { size: 9.5, bold: true, gapAfter: 4 })
      p.bullets(g.sr, { size: 9.5 })
    }
  }

  p.pageBreak()
  p.heading('Reviewer sign-off')
  p.text('By signing, you confirm that you have opened each document listed above and verified the ticked changes in the application named. Unticked items remain outstanding.', { size: 9.5, color: MUTED })
  p.table(['', 'Name', 'Role', 'Date'], [['Reviewed by', '', '', ''], ['Approved by', '', '', '']],
          [90, 165, 130, 110])

  p.save(d.filename || `mova-remediation-report-${(m.scanId || 'scan')}.pdf`)
}
