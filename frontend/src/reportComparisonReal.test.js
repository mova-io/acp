// The report models, fed ONLY server-generated evidence.
//
// `__fixtures__/reportFacts.comparison.real.json` is written by tests/test_report_facts_real_fixture.py
// from a real SQLite store through the real routes (and that test fails in check mode when the
// server's output drifts from the committed file). No id below is typed by hand: finding ids, change
// ids and digests are the server's. The audit's point: every earlier comparison test here used
// invented ids, so none could notice the server's ids and the browser's classification disagreeing.
//
// The three reproduced defects are in it: C1 (language-not-set, synthetic key, both runs), C2 (an
// errored baseline), C3 (a rename matched by provider id).
import { describe, it, expect } from 'vitest'
import real from './__fixtures__/reportFacts.comparison.real.json'
import { buildFileReportModel } from './reportModel.js'
import { buildScanReportModel, factsIndexState } from './scanReport.js'
import { reportHtmlFromModel } from './htmlReport.js'
import { buildComparisonFromFacts, buildComparison, factsFindings } from './reportEvidence.js'
import { attachEvidenceLinks } from './evidenceLink.js'

const F = real.files
const fileModel = (name, mode = 'full', facts = F[name]) => buildFileReportModel({ file: name, mode, facts, rows: [] })
const cmpOf = (m) => m.blocks.find((b) => b.k === 'comparison')
const allRows = [...real.scanPages[0].files, ...real.scanPages[1].files]
const scanFacts = { ...real.scanPages[0], files: allRows }
const scanModel = (mode, extra = {}) => buildScanReportModel({ mode, facts: scanFacts, files: [], totalFiles: scanFacts.filesTotal, ...extra })
const textOf = (m) => m.blocks.map((b) => [b.text, b.caption, ...(b.rows || []).flat(), ...(b.items || []).map((i) => (typeof i === 'string' ? i : i.detail))].filter(Boolean).join(' ')).join('\n')

describe('the fixture is the server’s', () => {
  it('carries one snapshot across both pages, and every row digest equals its per-file digest (contract 3a)', () => {
    const [p1, p2] = real.scanPages
    expect(p1.factsDigest).toBe(p2.factsDigest)
    expect(p1.snapshot).toMatchObject({ factsDigest: p1.factsDigest, filesTotal: 5 })
    expect(p2.snapshot.servedFrom).toBe('memo')
    for (const row of allRows) expect(row.factsDigest).toBe(F[row.file].factsDigest)
  })
})

describe('C1 — language not set, in both runs', () => {
  const name = 'policies/language-not-set.docx'
  it('is neither new nor resolved, and is counted as not matchable', () => {
    const c = cmpOf(fileModel(name))
    expect(c.status).toBe('compared')
    expect(c.source).toBe('server')
    expect(c.introduced).toEqual([])
    expect(c.resolved).toEqual([])
    expect(c.persisting).toBe(1)
    expect(c.notComparable).toEqual({ current: 1, previous: 1 })
    expect(c.reason).toMatch(/3\.1\.1: 1 before, 1 now/)
  })
  it('bite check: the pre-fix client classification (ids only, comparable ignored) reproduces the defect on the SAME data', () => {
    const facts = F[name]
    const stripped = (list) => list.map(({ comparable, ...rest }) => rest)
    const old = buildComparison(
      { ...facts.previous, sameDocument: true, scope: 's', findings: stripped(facts.previous.findings) },
      { file: facts.identity.file, scope: 's', findings: stripped(factsFindings(facts)) })
    expect(old.introduced.map((x) => x.title).join()).toMatch(/Document language is not set/)
    expect(old.resolved.map((x) => x.title).join()).toMatch(/Document language is not set/)
  })
  it('the client fallback (no server comparison) honours comparable:false on the same data', () => {
    const { comparison, ...legacy } = F[name]
    const c = buildComparisonFromFacts(legacy)
    expect(c.status).toBe('compared')
    expect(c.introduced).toEqual([])
    expect(c.resolved).toEqual([])
    expect(c.notComparable).toEqual({ current: 1, previous: 1 })
  })
})

describe('C2 — an errored baseline', () => {
  const name = 'policies/analyser-failed.docx'
  it('is not compared, says why, and reports nothing as new', () => {
    const c = cmpOf(fileModel(name))
    expect(c.status).toBe('unknown')
    expect(c.serverStatus).toBe('baseline_unusable')
    expect(c.reasonCode).toBe('baseline_error')
    expect(c.introduced).toEqual([])
    expect(c.reason).toMatch(/did not produce a result/)
    const html = reportHtmlFromModel(fileModel(name), { origin: null })
    expect(html).toContain('The earlier assessment is not a usable baseline.')
    expect(html).not.toMatch(/New since then \(1\)/)
  })
})

describe('C3 — a rename matched by provider id', () => {
  const name = 'policies/renamed.docx'
  it('is compared, and names the earlier file', () => {
    const c = cmpOf(fileModel(name))
    expect(c.status).toBe('compared')
    expect(c.renamed).toBe(true)
    expect(c.previous.file).toBe('policies/was-called-this.docx')
    expect(c.introduced.map((x) => x.location)).toEqual(['Image 3'])
    expect(c.resolved.map((x) => x.location)).toEqual(['Image 2'])
    expect(c.persisting).toBe(1)
  })
  it('still compares on the client fallback path (the pre-fix code said "different document")', () => {
    const { comparison, ...legacy } = F[name]
    expect(buildComparisonFromFacts(legacy).status).toBe('compared')
  })
})

describe('contract 1 locations reach the report as words', () => {
  it('pptx shape and xlsx cell', () => {
    const deck = fileModel('decks/quarterly.pptx').blocks.filter((b) => b.k === 'findingCard').map((c) => c.location.label)
    expect(deck).toEqual(expect.arrayContaining(['Slide 2 · shape 5', 'Slide 4 · shape 9']))
    const sheet = fileModel('sheets/brand-new.xlsx').blocks.find((b) => b.k === 'findingCard')
    expect(sheet.location).toMatchObject({ label: 'Sheet Budget 2026 · cell B3', page: null, sheet: 'Budget 2026', cell: 'B3' })
  })
  it('a verified change with no recorded locator reads "Location not recorded", never page 1', () => {
    const card = fileModel('policies/renamed.docx').blocks.find((b) => b.k === 'changeCard' && b.ruleId === '1.1.1')
    expect(card.location).toBeNull()
    expect(reportHtmlFromModel(fileModel('policies/renamed.docx'), { origin: null })).toContain('Location not recorded')
  })
})

// R1 from the REAL store (schema v59): the writer's recorded locator, and a legacy note the store
// read back — the latter always flagged, neither ever given a page Word or a slide part lacks.
describe('R1 — verified-change locations, as the real store returns them', () => {
  const cards = (name) => fileModel(name).blocks.filter((b) => b.k === 'changeCard')
  it('a recorded Word paragraph token reads as words and never as a page', () => {
    const para = cards('policies/renamed.docx').find((c) => c.ruleId === '1.3.1')
    expect(para.location).toMatchObject({ label: 'Paragraph 4', page: null, source: 'recorded' })
    expect(para.locationSource).toBe('recorded')
  })
  it('a legacy-note location is qualified exactly once, in the label every renderer prints', () => {
    const legacy = cards('policies/renamed.docx').find((c) => c.ruleId === '1.4.5')
    expect(legacy.location.label).toBe("Image (relationship rId7) · document body (from the saving step's note, not a recorded location)")
    expect(legacy.location.page).toBeNull()
    const html = reportHtmlFromModel(fileModel('policies/renamed.docx'), { origin: null })
    // Printed wherever the location is printed (card, appendix …) — each time once, never doubled,
    // and never without the qualifier.
    const n = (s) => html.split(s).length - 1
    expect(n('not a recorded location')).toBeGreaterThan(0)
    expect(n('document body (from the saving step&#39;s note, not a recorded location)') + n("document body (from the saving step's note, not a recorded location)"))
      .toBe(n('Image (relationship rId7) · document body'))
    expect(n('not a recorded location)') ).toBe(n('not a recorded location'))
    expect(html).not.toMatch(/not a recorded location\)[^<]*not a recorded location/)
  })
  it('a slide part is never called a slide number', () => {
    const [card] = cards('decks/quarterly.pptx')
    expect(card.location).toMatchObject({ label: 'Object “Picture 9” · slide file slide3.xml', page: null, slide: null })
  })
})

// CONTRACT-SHAPE section of the same fixture: real store and routes, stand-in R-B2/R-B3 readers
// (not on this base yet) returning the owner response's exact shapes.
describe('R-B2 / R-B3 — contract-shape facts through the models', () => {
  const CS = real.contractShape.files
  const model = (name, mode = 'full') => buildFileReportModel({ file: name, mode, facts: CS[name], rows: [] })
  it('a same-scan comparison is stated with the server’s counts from the Reviewer packet up', () => {
    const text = textOf(model('policies/renamed.docx', 'reviewer'))
    expect(text).toContain('Within this scan: this document was re-assessed. Against the assessment it replaced, 1 finding(s) are new, 1 no longer reported and 1 still reported')
    expect(textOf(model('policies/renamed.docx', 'summary'))).not.toContain('Within this scan')
  })
  it('a snapshot whose context was not recorded reads "not recorded", never "different scope"', () => {
    const text = textOf(model('policies/language-not-set.docx', 'reviewer'))
    expect(text).toMatch(/Within this scan: an earlier assessment of this document was replaced, but it is not compared — the replaced assessment's rubric and scope were not recorded/)
    expect(text).not.toMatch(/different (rubric|scope)/)
  })
  it('"none recorded" is stated only in Full evidence, as absence of a record', () => {
    expect(textOf(model('sheets/brand-new.xlsx', 'reviewer'))).not.toContain('Within this scan')
    expect(textOf(model('sheets/brand-new.xlsx', 'full'))).toContain('not evidence that there was no earlier one')
  })
  it('a truncated earlier-decision list says showing N of TOTAL; an empty one says none', () => {
    const text = textOf(model('policies/renamed.docx'))
    expect(text).toContain('showing 2 of 5')
    expect(text).toContain('Showing 2 of 5 earlier decisions (newest first)')
    expect(textOf(model('policies/language-not-set.docx'))).toContain('no decision is recorded against an earlier scan of this document')
  })
  it('the scan comparison carries the same-scan note (one line) and keeps totals one population', () => {
    const cmp = real.contractShape.scanComparison
    expect(cmp.sameScan).toMatchObject({ filesCompared: 1, filesBaselineUnusable: 1 })
    expect(cmp.notes.filter((n) => n.startsWith('Re-assessments inside this scan'))).toHaveLength(1)
    expect(cmp.totals.sameScan).toBeUndefined()
  })
})

// REAL R-B2 / R-B3: the owner's readers (0f0fd520) on a real store — a re-assessment inside one
// scan captured by save_file_result, and a decision recorded on the renamed document's earlier scan.
describe('R-B2 / R-B3 — the owner’s real readers through the models', () => {
  const RH = real.realHistory.files
  const model = (facts, name, mode = 'reviewer') => buildFileReportModel({ file: name, mode, facts, rows: [] })
  it('a real same-scan re-assessment is stated with the server’s counts', () => {
    const text = textOf(model(RH['board/minutes.docx'], 'board/minutes.docx'))
    expect(text).toContain('Within this scan: this document was re-assessed. Against the assessment it replaced, 1 finding(s) are new, 1 no longer reported and 1 still reported; 1 without a detector location cannot be matched one by one')
  })
  it('a real earlier-scan decision is listed as not carried forward', () => {
    const facts = F['policies/renamed.docx']
    expect(facts.priorDecisions.map((d) => [d.verdict, d.status])).toEqual([['rejected', 'not_carried_forward']])
    expect(textOf(model(facts, 'policies/renamed.docx', 'full'))).toMatch(/earlier scan/i)
  })
})

describe('downloaded HTML never carries a relative app link', () => {
  const linked = attachEvidenceLinks(F['decks/quarterly.pptx'])
  const model = buildFileReportModel({ file: 'decks/quarterly.pptx', mode: 'full', facts: linked, rows: [] })
  it('absolutizes evidence links against a trusted origin', () => {
    const html = reportHtmlFromModel(model, { origin: 'https://acp.example.org' })
    const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map((m) => m[1]).filter((h) => !h.startsWith('#'))
    expect(hrefs.length).toBeGreaterThan(0)
    for (const h of hrefs) expect(h).toMatch(/^https:\/\/acp\.example\.org\/\?view=evidence&amp;scan=/)
  })
  it('prints the location as text when no trusted origin exists', () => {
    for (const origin of [null, 'http://evil.example', 'file://']) {
      const html = reportHtmlFromModel(model, { origin })
      const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map((m) => m[1]).filter((h) => !h.startsWith('#'))
      expect(hrefs).toEqual([])
      expect(html).toContain('Slide 2 · shape 5')
    }
  })
})

describe('C4 — the scan report renders the REAL estate comparison', () => {
  it('states the totals in words in the one-page summary', () => {
    const text = textOf(scanModel('summary'))
    const t = scanFacts.comparison.totals
    expect(text).toContain(`${t.introduced} newly reported finding(s), ${t.resolved} no longer reported, ${t.persisting} still present`)
    expect(text).toMatch(/reopened not recorded for 3 document\(s\)/)
    expect(text).toMatch(/1 with no earlier assessment \(new to the record\)/)
    expect(text).toMatch(/1 whose earlier assessment did not finish/)
    expect(text).toMatch(/not a verified fix/)
    expect(text).not.toMatch(/Change since the previous report: Unknown/)
    expect(scanModel('summary').blocks.find((b) => b.k === 'comparison')).toBeUndefined()
  })
  it('lists the documents that changed or could not be compared, with their own status', () => {
    const m = scanModel('reviewer')
    const table = m.blocks.find((b) => b.caption === 'Documents that changed, or could not be compared')
    const byFile = Object.fromEntries(table.rows.map((r) => [r[0]?.text ?? r[0], r[1]]))
    expect(byFile['policies/analyser-failed.docx']).toMatch(/^Earlier assessment not usable/)
    expect(byFile['policies/renamed.docx']).toMatch(/renamed \(was policies\/was-called-this\.docx\)/)
    expect(byFile['policies/language-not-set.docx']).toBeUndefined()   // nothing moved there
  })
})

describe('S2 — the master index comes from the server index', () => {
  it('lists every document the server indexed, with state, findings, comparison', () => {
    const m = scanModel('full')
    const t = m.blocks.find((b) => b.id === 'appendix-documents')
    expect(t.source).toBe('server-index')
    expect(t.complete).toBe(true)
    const name = (cell) => cell?.text ?? cell
    expect(t.rows.map((r) => name(r[0])).sort()).toEqual(allRows.map((r) => r.file).sort())
    // Every document name opens THAT document's evidence view in ACP — the scan id and the exact
    // file, and no finding id (the scan report cannot name one it did not read).
    for (const [cell] of t.rows) {
      const q = new URLSearchParams(cell.href.slice(1))
      expect(Object.fromEntries(q)).toEqual({ view: 'evidence', scan: real.scanPages[0].identity.scanId, file: cell.text })
    }
    const lang = t.rows.find((r) => name(r[0]) === 'policies/language-not-set.docx')
    expect(lang[7]).toMatch(/0 new, 0 no longer reported, 1 still present, 1 not matchable/)
  })
  it('a partial index says so where the reader decides, and in the table', () => {
    const partialFacts = { ...real.scanPages[0] }        // page one only: 3 of 5
    const reason = 'The per-document index stopped after 3 of 5 documents because the next page could not be read (HTTP 500).'
    const m = buildScanReportModel({ mode: 'full', facts: partialFacts, files: [], factsIndexComplete: false, factsIncompleteReason: reason })
    expect(factsIndexState(partialFacts, { factsIndexComplete: false, factsIncompleteReason: reason })).toMatchObject({ partial: true, loaded: 3, total: 5 })
    const text = textOf(m)
    expect(text).toContain('PARTIAL: 3 of 5 documents were loaded')
    expect(text).toContain(reason)
    const t = m.blocks.find((b) => b.id === 'appendix-documents')
    expect(t).toMatchObject({ complete: false, totalRecords: 5, limitNote: reason })
    expect(t.rows).toHaveLength(3)
  })
  it('without the caller’s verdict, a short index is still never called complete', () => {
    expect(factsIndexState({ ...real.scanPages[0] }).partial).toBe(true)
    expect(factsIndexState(scanFacts).partial).toBe(false)
  })
})

describe('the scan report links documents and keeps its comparison headline on the one-page Summary', () => {
  it('the comparison headline is bold, the only text the Summary keeps under page pressure', () => {
    // report_render.summary_blocks drops plain text at trim >= 2 and keeps bold. The PDF QA found
    // the scan Summary printing no "since the previous assessment" line at all because of it.
    const m = scanModel('summary')
    const head = m.blocks.find((b) => b.k === 'text' && /^(Across the documents compared|Change since the previous assessment)/.test(b.text))
    expect(head).toBeTruthy()
    expect(head.o.bold).toBe(true)
  })
  it('a downloaded HTML master index links each document through a trusted origin, and prints text without one', () => {
    const m = scanModel('full')
    const html = reportHtmlFromModel(m, { origin: 'https://acp.example.com' })
    const sid = real.scanPages[0].identity.scanId
    const href = `https://acp.example.com/?${new URLSearchParams({ view: 'evidence', scan: sid, file: 'policies/language-not-set.docx' })}`
    expect(html).toContain(`<a href="${href.replace(/&/g, '&amp;')}">policies/language-not-set.docx</a>`)
    const plain = reportHtmlFromModel(m, { origin: null })
    expect(plain).not.toContain('view=evidence')
    expect(plain).not.toContain('[object Object]')
    expect(plain).toContain('policies/language-not-set.docx')
  })
})
