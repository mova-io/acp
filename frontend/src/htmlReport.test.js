// Dogfood: the HTML certification report must itself pass ACP's own WCAG engine.
// We render the report, parse it, and run every Level A/AA rule from ./rules over it,
// asserting zero findings — the same bar the product holds customer documents to.
// (AAA rules such as 3.1.4 Abbreviations are above the AA certification target and are
// excluded, exactly as the on-screen coverage table scopes them out.)
import { describe, it, expect } from 'vitest'
import { certificationHtml, certificationHtmlFromModel } from './htmlReport.js'
import { buildFileCertificationModel } from './reportModel.js'
import { allRules } from './rules/index.js'

// A file exercising every report section: FIXED (→ "What ACP changed"), FAIL (→ open
// items), HUMAN (→ human-review guide), UNCHECKED, and PASS rows.
const sampleData = () => ({
  file: 'benefits-guide.pdf',
  score: 82,
  targetLevel: 'AA',
  date: 'July 9, 2026',
  timestamp: 'July 9, 2026, 9:15 AM CDT',
  engine: 'mova.io WCAG 2.1 AA',
  sourceName: 'SharePoint',
  department: 'Human Resources',
  platformVersion: '1.4.2',
  rows: [
    { id: '1.1.1', name: 'Non-text Content', plain: 'Images have alt text', level: 'A', fix: '⚡ auto', outcome: 'FIXED', count: 3, fileIssues: [] },
    { id: '3.1.1', name: 'Language of Page', plain: 'Document language declared', level: 'A', fix: '⚡ auto', outcome: 'FIXED', count: 1, fileIssues: [] },
    { id: '1.4.3', name: 'Contrast (Minimum)', plain: 'Text has enough contrast', level: 'AA', fix: '✎ AI', outcome: 'FAIL', count: 2, fileIssues: [{ detail: 'Body text #999 on white is 2.8:1' }, { detail: 'Caption #aaa is 2.3:1' }] },
    { id: '2.4.2', name: 'Page Titled', plain: 'Document has a title', level: 'A', fix: '⚡ auto', outcome: 'PASS', count: 0, fileIssues: [] },
    { id: '1.3.1', name: 'Info and Relationships', plain: 'Tables & structure', level: 'A', fix: '⚡ auto', outcome: 'PASS', count: 0, fileIssues: [] },
    { id: '2.5.3', name: 'Label in Name', plain: 'Labels match spoken names', level: 'A', fix: '✋ human', outcome: 'HUMAN', count: 0, fileIssues: [] },
    { id: '1.1.1b', name: 'Complex images', plain: 'Complex images described', level: 'A', fix: '✋ human', outcome: 'UNCHECKED', count: 0, fileIssues: [] },
  ],
})

// Full document parse — gives documentElement[lang], <head>/<title>, <body>, etc.
const parseDoc = (html) => new DOMParser().parseFromString(html, 'text/html')

// Level A + AA rules only — the certification target ACP claims.
const aaRules = allRules.filter((m) => m.meta.level === 'A' || m.meta.level === 'AA')

describe('HTML certification report dogfoods ACP’s own checks', () => {
  it('passes every Level A/AA rule with zero findings', () => {
    const html = certificationHtml(sampleData())
    const doc = parseDoc(html)
    const failures = []
    for (const rule of aaRules) {
      const found = rule.check(doc)
      if (found.length) failures.push(`${rule.meta.id} ${rule.meta.name}: ${found.map((f) => f.detail).join(' | ')}`)
    }
    expect(failures).toEqual([])
  }, 20000) // generous: absorbs jsdom cold-start on the first test in a fresh CI run

  it('is a complete, language-tagged, titled document', () => {
    const doc = parseDoc(certificationHtml(sampleData()))
    expect(doc.documentElement.getAttribute('lang')).toBe('en-US')
    expect(doc.querySelector('title')?.textContent).toContain('benefits-guide.pdf')
    expect(doc.querySelector('main#main-content')).toBeTruthy()
    expect(doc.querySelector('a.skip-link[href="#main-content"]')).toBeTruthy()
    expect(doc.querySelector('meta[name="viewport"]')).toBeTruthy()
    expect(doc.querySelectorAll('h1')).toHaveLength(1)
  })

  it('gives every table a caption and column headers', () => {
    const doc = parseDoc(certificationHtml(sampleData()))
    const tables = [...doc.querySelectorAll('table')]
    expect(tables.length).toBeGreaterThan(0)
    for (const t of tables) {
      expect(t.querySelector('caption')).toBeTruthy()
      expect(t.querySelectorAll('thead th[scope="col"]').length).toBeGreaterThan(0)
    }
  })

  it('an embedded logo still carries alt text (1.1.1 stays clean)', () => {
    const model = buildFileCertificationModel(sampleData())
    const html = certificationHtmlFromModel(model, { logo: 'data:image/png;base64,iVBORw0KGgo=' })
    const doc = parseDoc(html)
    const img = doc.querySelector('img.logo')
    expect(img).toBeTruthy()
    expect(img.getAttribute('alt')).toBeTruthy()
    expect(allRules.find((m) => m.meta.id === '1.1.1').check(doc)).toEqual([])
  })

  it('renders the same section headings the PDF produces', () => {
    const doc = parseDoc(certificationHtml(sampleData()))
    // Section names follow the evidence-truth vocabulary: "Executive summary" became the
    // Decision summary, "Compliance checklist" became "Checklist by area" (it no longer prints
    // "Pass" over unchecked rows), and "Conformance statement" became "What this report is, and
    // is not" (ACP does not certify — a64142c1). Sub-sections render as h3.
    const headings = [...doc.querySelectorAll('h2, h3')].map((h) => h.textContent)
    expect(headings).toEqual(expect.arrayContaining([
      'Decision summary', 'Document identity', 'Since the previous assessment', 'Changes to confirm',
      'Remaining work', 'Result', 'Coverage at a glance', 'What ACP changed',
      'Checklist by area', 'Human review — how to verify', 'Manual verification guide',
      'Complete evidence appendix', 'Full WCAG coverage', 'Audit trail', 'What this report is, and is not',
    ]))
  })

  it('reflects a file with nothing outstanding in the closing statement', () => {
    const clean = sampleData()
    clean.rows = clean.rows.filter((r) => r.outcome === 'PASS')
    clean.score = 100
    const doc = parseDoc(certificationHtml(clean))
    const failures = aaRules.flatMap((m) => m.check(doc))
    expect(failures).toEqual([])
    // Was toContain('meets all') — a conformance claim the report is not in a position to make.
    expect(doc.body.textContent).toContain('No outstanding items')
    expect(doc.body.textContent).not.toMatch(/certified|meets the requirements/i)
  })
})
