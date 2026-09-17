import { describe, it, expect } from 'vitest'
import { WCAG } from './wcagCatalog.js'
import { LEGACY_JSPDF_RENDERERS } from './pdfReport.js'
// The retired jsPDF renderer, still checked headlessly; the UI now renders on the server.
const { exportFileCertification } = LEGACY_JSPDF_RENDERERS

// Records copied verbatim from what the real Office/HTML remediators emit (api/
// remediate_office.py, api/remediate.py) when run on the oracle test corpus — the
// shape the /remediation-diffs endpoint returns. This guards the PDF's Before → After
// rendering (label join, note, BEFORE/AFTER bands, mono values, truncation) against
// real data without needing the backend running.
const REAL_DIFFS = [
  { rule_id: '1.1.1', before: '(no alt text — image was skipped by screen readers)', after: 'Quarterly revenue by region', note: 'set from the adjacent caption' },
  { rule_id: '1.4.3', before: 'color:#bbbbbb (too light on a light background — fails AA)', after: 'color:#111111 (passes AA and AAA)', note: 'text "faint"' },
  { rule_id: '1.4.3', before: '#7A7A7A on #FFFFFF (2.9:1 — fails AA)', after: '#000000 on #FFFFFF (21.0:1 — passes AA)', note: 'text run "Apply by March 31"' },
  { rule_id: '3.1.1', before: '(no language declared — screen readers guessed pronunciation)', after: 'en-US', note: 'read by assistive tech from docProps/core.xml (dc:language)' },
  { rule_id: '2.4.2', before: '(no document title — could not be identified in assistive tech)', after: 'docx all violations', note: 'read by assistive tech from docProps/core.xml (dc:title)' },
  { rule_id: '2.4.2', before: '(no title — assistive tech announced the slide as “Untitled”)', after: 'Middle (created second)', note: 'programmatic title added to slide1.xml' },
  { rule_id: '1.3.1', before: 'first row was ordinary data cells (<w:tr>)', after: 'first row marked as a repeating header row (<w:tblHeader/>)', note: 'so a screen reader announces the column heading for every data cell' },
  { rule_id: '1.3.1', before: 'table defined with headerRowCount="0" (no header row)', after: 'headerRowCount="1"', note: 'so the first row is announced as column headers' },
  { rule_id: '1.3.2', before: "shapes read in the slide's stored (authoring) order", after: '4 shapes re-sequenced top-to-bottom, then left-to-right', note: 'so a screen reader follows the visual reading order' },
]

function rowsFor(diffs) {
  const fixed = new Set(diffs.map((d) => d.rule_id))
  return WCAG.filter((c) => c.docApplies !== false && (c.level === 'A' || c.level === 'AA'))
    .map((c) => ({
      id: c.sc, name: c.name, plain: c.name, level: c.level, fix: '⚡ auto',
      outcome: fixed.has(c.sc) ? 'FIXED' : 'PASS', count: fixed.has(c.sc) ? 1 : 0,
    }))
}

async function render(diffs) {
  const { jsPDF } = await import('jspdf')
  const captured = []
  // Intercept the download: capture the real PDF bytes jsPDF would save.
  jsPDF.API.save = function () { captured.push(Buffer.from(this.output('arraybuffer'))); return this }
  await exportFileCertification({
    file: 'quarterly-report.docx', score: 100, targetLevel: 'AA',
    rows: rowsFor(diffs), diffs,
    date: 'July 9, 2026', timestamp: 'July 9, 2026, 10:00 AM PDT',
    engine: 'mova.io WCAG 2.1 AA', platformVersion: 'test',
  })
  expect(captured.length).toBe(1)
  return captured[0].toString('latin1')
}

describe('certification PDF — Before → After section', () => {
  it('renders real remediation diffs into the PDF', async () => {
    const raw = await render(REAL_DIFFS)
    // Section + band labels
    for (const t of ['BEFORE', 'AFTER', 'CHANGED']) expect(raw).toContain(t)
    // Real "after" values pulled straight from the remediators
    expect(raw).toContain('en-US')             // language declared
    expect(raw).toContain('color:#111111')     // html contrast after
    expect(raw).toContain('headerRowCount')    // xlsx table-header markup
    expect(raw).toContain('tblHeader')         // docx repeating header row markup
    expect(raw).toContain('re-sequenced')      // pptx reading-order note
    expect(raw).toContain('quarterly')         // filename-derived title (row-join label)
  })

  it('omits the section entirely when there are no verified diffs', async () => {
    const raw = await render([])
    expect(raw).not.toContain('CHANGED')       // no "Before → After — what changed" heading
  })
})
