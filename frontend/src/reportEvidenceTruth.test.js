// Evidence truth for the report model (reportModel.js / reportEvidence.js / scanReport.js /
// htmlReport.js). Each test pins one claim a report may or may not make, against the contract in
// the report-review-quality work: a report says what ACP checked, changed and verified — no more.
import { describe, it, expect } from 'vitest'
import { buildFileReportModel, buildFileCertificationModel, remediationReportModel, buildRemediationModel } from './reportModel.js'
import {
  fileIssuesOf, fileIssuesForCriterion, attachFileIssues, buildComparison, locationOf, clampText,
  isSafeHref, isSafeImageSrc, changeIdOf,
} from './reportEvidence.js'
import { aggregateScanReport, buildScanReportModel, normaliseDiffSummary } from './scanReport.js'
import { reportHtmlFromModel, fileReportHtml } from './htmlReport.js'
import { allRules } from './rules/index.js'

const PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='

const row = (id, outcome, extra = {}) => ({ id, name: `Criterion ${id}`, plain: `Plain ${id}`, level: 'A', fix: '⚡ auto', outcome, count: 0, ...extra })
const base = (rows, extra = {}) => ({ file: 'deck.pptx', score: 90, targetLevel: 'AA', rows, date: 'Sep 1, 2026', timestamp: 'Sep 1, 2026 10:00', ...extra })

// All model text, flattened across block kinds, for wording assertions.
const text = (model) => JSON.stringify([model.cover, model.blocks])
const blocksOf = (model, k) => model.blocks.filter((b) => b.k === k)
const decision = (model) => Object.fromEntries(blocksOf(model, 'decisionSummary')[0].items.map((i) => [i.key, i.value]))
const READY = /No outstanding items for|all criteria passing|Ready to certify|meets all/i
const parse = (html) => new DOMParser().parseFromString(html, 'text/html')
const aaRules = allRules.filter((m) => m.meta.level === 'A' || m.meta.level === 'AA')

describe('ready wording is earned, never assumed', () => {
  it('a FIXED row with no verification record is outstanding, not passing', () => {
    const m = buildFileReportModel(base([row('1.1.1', 'PASS'), row('3.1.1', 'FIXED', { count: 1 })]))
    expect(m.ready).toBe(false)
    expect(m.fullyConformant).toBe(false)
    expect(text(m)).not.toMatch(READY)
    expect(text(m)).toMatch(/awaiting re-validation/)
    expect(text(m)).not.toMatch(/certified/i)
  })

  it('an UNCHECKED row blocks ready wording', () => {
    const m = buildFileReportModel(base([row('1.1.1', 'PASS'), row('1.2.2', 'UNCHECKED')]))
    expect(m.ready).toBe(false)
    expect(text(m)).not.toMatch(READY)
    expect(decision(m).checksNotPerformed).toBe(1)
  })

  it('a verified criterion is technically complete, but its FINDINGS are not credited without a ledger', () => {
    const m = buildFileReportModel(base([row('1.1.1', 'PASS'), row('3.1.1', 'FIXED', { count: 1 })], {
      diffs: [{ rule_id: '3.1.1', seq: 0, before: '(none)', after: 'en-US', verified: true }],
    }))
    expect(m.technicalReady).toBe(true)
    // Was: ready true and findingsVerifiedResolved 1. A criterion-wide verification record is not a
    // per-finding one, and nobody has confirmed the change, so neither claim is available.
    expect(m.ready).toBe(false)
    expect(decision(m).findingsVerifiedResolved).toBeNull()
    expect(m.findingsAccounting).toMatchObject({ ledger: 'none', attributed: false })
    expect(text(m)).not.toMatch(READY)
  })

  it('a verified criterion with NO findings and a confirmed reviewer decision does read as nothing outstanding', () => {
    const diff = { rule_id: '3.1.1', seq: 0, before: '(none)', after: 'en-US', verified: true }
    const m = buildFileReportModel(base([row('1.1.1', 'PASS'), row('3.1.1', 'FIXED', { count: 0 })], {
      diffs: [diff], artifact: { currentSha256: 'cur' },
      reviews: { [changeIdOf('deck.pptx', diff)]: { verdict: 'accepted', reviewer: 'qa', artifact_sha256: 'cur', stale: false } },
    }))
    expect(m.ready).toBe(true)
    expect(blocksOf(m, 'callout')[0].text).toMatch(/^No outstanding items for "deck\.pptx"/)
    // Technical completion and human confirmation are said separately, never merged into one word.
    expect(blocksOf(m, 'callout')[0].text).toMatch(/Technical checks: .*Human confirmation: /s)
  })

  it('row.verified is honoured for the criterion, and its two findings are still unaccounted', () => {
    const m = buildFileReportModel(base([row('3.1.1', 'FIXED', { count: 2, verified: true })], { diffs: [], diffsTotal: 1 }))
    expect(m.technicalReady).toBe(true)
    expect(m.ready).toBe(false)
    expect(decision(m).findingsVerifiedResolved).toBeNull()
    expect(decision(m).findingsRemaining).toBeNull()
  })

  it('the checklist never prints Pass over an unchecked or unverified criterion', () => {
    const m = buildFileReportModel(base([row('1.1.1', 'PASS'), row('1.1.1x', 'UNCHECKED'), row('3.1.1', 'FIXED', { count: 1 })], { mode: 'reviewer' }))
    const tbl = m.blocks.find((b) => b.k === 'table' && b.caption === 'Checklist by area')
    const cells = Object.fromEntries(tbl.rows)
    expect(cells.Images).toMatch(/not checked/)
    expect(cells.Language).toMatch(/awaiting re-validation/)
    expect(JSON.stringify(tbl.rows)).not.toMatch(/✓ Pass/)
  })

  it('makes no legal-compliance promise and no certification claim', () => {
    const m = buildFileCertificationModel(base([row('1.1.1', 'PASS')]))
    const t = text(m)
    expect(t).not.toMatch(/as required by|Certified by|Authorised signatory|meets the requirements/i)
    expect(t).toMatch(/not a conformance determination/)
  })
})

describe('missing evidence is Not recorded, never 0', () => {
  it('no diffs supplied → edits saved is null', () => {
    const m = buildFileReportModel(base([row('3.1.1', 'FIXED', { count: 1 })]))
    expect(decision(m).editsSaved).toBeNull()
    const html = reportHtmlFromModel(m)
    const doc = parse(html)
    const tr = [...doc.querySelectorAll('table.decision tbody tr')].find((r) => /Edits saved/.test(r.textContent))
    expect(tr.querySelector('td').textContent).toBe('Not recorded')
  })

  it('an empty diff list with FIXED rows is not "0 edits saved"', () => {
    const m = buildFileReportModel(base([row('3.1.1', 'FIXED', { count: 1 })], { diffs: [] }))
    expect(decision(m).editsSaved).toBeNull()
  })

  it('reviewer decisions not loaded → human confirmation unknown, not pending-zero', () => {
    const m = buildFileReportModel(base([row('3.1.1', 'FIXED')], { diffs: [{ rule_id: '3.1.1', seq: 0, before: 'a', after: 'b', verified: true }] }))
    const stage = blocksOf(m, 'stageStrip')[0].items.find((s) => s.key === 'humanConfirmation')
    expect(stage.status).toBe('unknown')
    expect(stage.value).toBeNull()
    expect(blocksOf(m, 'stageStrip')[0].items.find((s) => s.key === 'publication').status).toBe('unknown')
  })

  it('a diff list that could not be read is not "0 edits saved"', () => {
    const m = buildFileReportModel(base([row('1.1.1', 'PASS')], { diffs: [], diffsError: 'HTTP 503.', mode: 'full' }))
    expect(decision(m).editsSaved).toBeNull()
    expect(text(m)).toMatch(/could not be read: HTTP 503/)
    expect(m.blocks.find((b) => b.id === 'appendix-changes')).toMatchObject({ complete: false, totalRecords: null })
  })

  it('a review read failure is named, and values at the storage cap are flagged', () => {
    const long = 'y'.repeat(2000)
    const m = buildFileReportModel(base([row('1.1.1', 'FIXED')], {
      mode: 'full', reviews: null, reviewsError: 'HTTP 500', diffValueCap: 2000,
      diffs: [{ rule_id: '1.1.1', seq: 0, before: long, after: 'short', verified: true }],
    }))
    expect(blocksOf(m, 'stageStrip')[0].items.find((s) => s.key === 'humanConfirmation').detail).toMatch(/could not be read: HTTP 500/)
    expect(blocksOf(m, 'changeCard')[0].beforeStoredClipped).toBe(true)
    expect(blocksOf(m, 'changeCard')[0].valueClipped).toBe(true)
    // The store keeps no untruncated copy, so the note discloses the clip and points at the
    // corrected copy — never at a "full evidence report" that does not have the text either.
    expect(text(m)).toMatch(/clipped by the store to 2,000 characters when it was recorded/)
    expect(text(m)).not.toMatch(/clipped[^.]*Full evidence report/)
    expect(m.blocks.find((b) => b.id === 'appendix-changes').rows[0][7]).toMatch(/Clipped by the store/)
  })

  it('a missing location says so', () => {
    expect(locationOf({ detail: 'x' })).toBeNull()
    const m = buildFileReportModel(base([row('1.1.1', 'FAIL', { count: 1, fileIssues: [{ detail: 'no alt' }] })], { mode: 'reviewer' }))
    const doc = parse(reportHtmlFromModel(m))
    expect(doc.querySelector('article.finding').textContent).toMatch(/Location not recorded/)
  })
})

describe('every finding and every change survives', () => {
  const big = 'x'.repeat(5000)
  const issues = Array.from({ length: 40 }, (_, i) => ({ wcag: 'SC_1_1_1', rule_id: 'IMG_ALT', severity: 'SERIOUS', detail: `Image ${i} missing alt`, page: (i % 5) + 1 }))
  const diffs = Array.from({ length: 60 }, (_, i) => ({ rule_id: '1.1.1', seq: i, before: `${big}-b${i}`, after: `${big}-a${i}`, note: `note ${i}`, verified: true }))
  const file = { file: 'deck.pptx', type: 'pptx', issues }
  const rows = attachFileIssues([row('1.1.1', 'FAIL', { count: 40 }), row('2.4.2', 'PASS')], file)

  it('full mode carries all 40 findings and all 60 diffs with 5,000-char values intact', () => {
    const m = buildFileReportModel(base(rows, { diffs, mode: 'full' }))
    expect(blocksOf(m, 'findingCard')).toHaveLength(40)
    expect(blocksOf(m, 'changeCard')).toHaveLength(60)
    const ba = blocksOf(m, 'beforeAfter')[0].items
    expect(ba).toHaveLength(60)
    ba.forEach((it, i) => { expect(it.before).toBe(`${big}-b${i}`); expect(it.after).toBe(`${big}-a${i}`) })
    const findings = m.blocks.find((b) => b.k === 'appendixTable' && b.id === 'appendix-findings')
    expect(findings.rows).toHaveLength(40)
    expect(new Set(findings.rows.map((r) => r[0])).size).toBe(40)
    const html = reportHtmlFromModel(m)
    for (let i = 0; i < 60; i++) expect(html).toContain(`${big}-a${i}`)
    for (let i = 0; i < 40; i++) expect(html).toContain(`Image ${i} missing alt`)
  })

  it('reviewer mode flags clipped values and points at the full record', () => {
    const m = buildFileReportModel(base(rows, { diffs, mode: 'reviewer' }))
    const card = blocksOf(m, 'changeCard')[0]
    expect(card.beforeTruncated).toBe(true)
    expect(card.afterTruncated).toBe(true)
    expect(card.fullRef).toMatch(/Full evidence report · record deck\.pptx::1\.1\.1::0/)
    expect(blocksOf(m, 'beforeAfter')).toHaveLength(0)
    const html = reportHtmlFromModel(m)
    expect(html).toContain('Before text clipped here')
    expect(html).not.toContain(`${big}-b0`)
  })

  it('reviewer mode bounds cards explicitly, never silently', () => {
    const many = Array.from({ length: 130 }, (_, i) => ({ rule_id: '1.1.1', seq: i, before: 'a', after: `b${i}`, verified: true }))
    const m = buildFileReportModel(base(rows, { diffs: many, mode: 'reviewer' }))
    expect(blocksOf(m, 'changeCard')).toHaveLength(100)
    expect(text(m)).toMatch(/30 more saved changes \(of 130\) are listed in the Full evidence report/)
  })

  it('a partial diff list says so in the appendix', () => {
    const m = buildFileReportModel(base(rows, { diffs: diffs.slice(0, 10), diffsComplete: false, diffsTotal: 60 }))
    const t = m.blocks.find((b) => b.k === 'appendixTable' && b.id === 'appendix-changes')
    expect(t.complete).toBe(false)
    expect(t.totalRecords).toBe(60)
    expect(reportHtmlFromModel(m)).toMatch(/Partial:<\/strong> 10 of 60 records/)
    expect(decision(m).editsSaved).toBe(60)
  })

  it('ids are stable and distinct for identical findings', () => {
    const dup = { file: 'a.docx', issues: [{ wcag: 'SC_1_1_1', detail: 'same' }, { wcag: 'SC_1_1_1', detail: 'same' }, { wcag: 'SC_1_1_1', detail: 'same', id: 'srv-9' }] }
    const a = fileIssuesOf(dup).map((i) => i.id)
    expect(a).toEqual(fileIssuesOf(dup).map((i) => i.id))
    expect(new Set(a).size).toBe(3)
    expect(a[2]).toBe('srv-9')
    expect(fileIssuesForCriterion(dup, '1.1.1')).toHaveLength(3)
  })

  it('structured locations survive: page, slide, sheet, cell, element', () => {
    expect(locationOf({ page: 3 }, { fmt: 'pptx' })).toMatchObject({ slide: 3, page: null, label: 'Slide 3' })
    expect(locationOf({ location: "Budget!B4" })).toMatchObject({ sheet: 'Budget', cell: 'B4' })
    expect(locationOf({ page: 7, location: 'word/header1.xml#Picture 1' })).toMatchObject({ page: 7, element: 'word/header1.xml#Picture 1' })
    const href = locationOf({ page: 2 }, { locationHref: () => '/scans/s1/files/a.pdf?page=2' })
    expect(href.href).toBe('/scans/s1/files/a.pdf?page=2')
    expect(locationOf({ page: 2 }, { locationHref: () => 'javascript:alert(1)' }).href).toBeNull()
  })

  it('remediation model keeps every change, not one per criterion', () => {
    const rm = buildRemediationModel({ files: [{ file: 'a.docx', remediated_at: '2026-09-01T00:00:00Z' }], diffsByFile: { 'a.docx': diffs.slice(0, 5).map((d) => ({ ...d, file: 'a.docx' })) } })
    expect(rm.documents[0].items).toHaveLength(1)
    expect(rm.documents[0].changes).toHaveLength(5)
    const m = remediationReportModel(rm, { mode: 'full' })
    expect(m.kind).toBe('remediation')
    expect(blocksOf(m, 'changeCard')).toHaveLength(5)
    expect(m.blocks.find((b) => b.k === 'appendixTable').rows).toHaveLength(5)
    expect(decision(m).humanChecksPending).toBeNull()
  })
})

describe('human confirmation is recorded, never invented', () => {
  const diff = { rule_id: '1.1.1', seq: 4, before: '', after: 'A chart', verified: true }
  const id = changeIdOf('deck.pptx', diff)

  it('uses the contract change id', () => { expect(id).toBe('deck.pptx::1.1.1::4') })

  it('shows a stale decision as stale, with both versions', () => {
    const m = buildFileReportModel(base([row('1.1.1', 'FIXED')], {
      diffs: [diff], artifact: { currentSha256: 'new' },
      reviews: { [id]: { verdict: 'accepted', reviewer: 'qa@x', at: '2026-09-01', artifact_sha256: 'old', stale: true } },
    }))
    const card = blocksOf(m, 'changeCard')[0]
    expect(card.human).toMatchObject({ status: 'stale', boundSha256: 'old', currentSha256: 'new', reviewer: 'qa@x' })
    expect(card.technical.status).toBe('verified')
    const html = reportHtmlFromModel(m)
    expect(html).toMatch(/Stale — the file changed after this decision/)
  })

  it('a loaded review map without an entry is pending; an accepted one needs KNOWN freshness', () => {
    const d2 = { ...diff, seq: 5 }
    const m = buildFileReportModel(base([row('1.1.1', 'FIXED')], {
      diffs: [diff, d2], reviews: { [changeIdOf('deck.pptx', d2)]: { verdict: 'accepted', reviewer: 'r' } },
    }))
    // Was ['pending', 'accepted']. The second decision names no artifact and the server did not
    // evaluate staleness, so its freshness is UNKNOWN — which is not a confirmation.
    expect(blocksOf(m, 'changeCard').map((c) => c.human.status)).toEqual(['pending', 'freshness_unknown'])
    expect(blocksOf(m, 'changeCard')[1].human.confirmed).toBe(false)
    const fresh = buildFileReportModel(base([row('1.1.1', 'FIXED')], {
      diffs: [d2], artifact: { currentSha256: 'cur' },
      reviews: { [changeIdOf('deck.pptx', d2)]: { verdict: 'accepted', reviewer: 'r', artifact_sha256: 'cur', stale: false } },
    }))
    expect(blocksOf(fresh, 'changeCard')[0].human).toMatchObject({ status: 'accepted', confirmed: true, freshness: 'known' })
    expect(blocksOf(m, 'changeCard')[0].responseNotice).toMatch(/does not record a decision/)
    expect(blocksOf(m, 'changeCard')[0].responseOptions).toEqual(['Accept', 'Edit', 'Reject', 'Unable to verify'])
  })

  it('uses a supplied preview only when it is a PNG/JPEG data URL', () => {
    const good = buildFileReportModel(base([row('1.1.1', 'FIXED')], { diffs: [diff], previews: { [id]: PNG } }))
    expect(blocksOf(good, 'changeCard')[0].imageStatus).toBe('available')
    const bad = buildFileReportModel(base([row('1.1.1', 'FIXED')], { diffs: [diff], previews: { [id]: 'data:text/html;base64,PHNjcmlwdD4=' } }))
    expect(blocksOf(bad, 'changeCard')[0].imageStatus).toBe('unavailable')
    expect(reportHtmlFromModel(bad)).toContain('Visual preview not available')
  })
})

describe('comparison only from a comparable snapshot', () => {
  const issues = [{ wcag: 'SC_1_1_1', detail: 'still here', page: 1 }, { wcag: 'SC_1_1_1', detail: 'brand new', page: 2 }]
  const current = fileIssuesOf({ file: 'a.pdf', issues })
  const prevIssues = fileIssuesOf({ file: 'a.pdf', issues: [issues[0], { wcag: 'SC_2_4_2', detail: 'gone now' }] })
  const scope = { scanScope: 'agreed', rubricHash: 'r1', targetLevel: 'AA' }

  it('is unknown with no snapshot, and says why', () => {
    const c = buildComparison(null, { file: 'a.pdf', scope, findings: current })
    expect(c.status).toBe('unknown')
    expect(c.persisting).toBeNull()
    expect(c.reason).toMatch(/No earlier assessment snapshot/)
  })

  it('is unknown for a different scope or file', () => {
    expect(buildComparison({ file: 'a.pdf', scope: { ...scope, rubricHash: 'r2' }, findings: prevIssues }, { file: 'a.pdf', scope, findings: current }).status).toBe('unknown')
    expect(buildComparison({ file: 'b.pdf', scope, findings: prevIssues }, { file: 'a.pdf', scope, findings: current }).status).toBe('unknown')
    expect(buildComparison({ file: 'a.pdf', scope: null, findings: prevIssues }, { file: 'a.pdf', scope, findings: current }).status).toBe('unknown')
  })

  it('matches finding by finding when comparable', () => {
    const c = buildComparison({ scanId: 'old', generatedAt: '2026-08-01', sha256: 'aa', file: 'a.pdf', scope, findings: prevIssues },
      { file: 'a.pdf', scope, findings: current })
    expect(c.status).toBe('compared')
    expect(c.resolved.map((x) => x.title)).toEqual(['2.4.2 — gone now'])
    expect(c.introduced.map((x) => x.title)).toEqual(['1.1.1 — brand new'])
    expect(c.persisting).toBe(1)
  })

  it('the file model uses d.currentFindings and d.previousReason', () => {
    const m = buildFileReportModel(base([row('1.1.1', 'FAIL', { count: 2 })], {
      file: 'a.pdf', scope, currentFindings: current,
      previous: { scanId: 'old', file: 'a.pdf', scope, findings: prevIssues },
    }))
    expect(blocksOf(m, 'comparison')[0]).toMatchObject({ status: 'compared', persisting: 1 })
    const m2 = buildFileReportModel(base([row('1.1.1', 'PASS')], { previous: null, previousReason: 'The baseline scan has no record of this Drive file.' }))
    expect(blocksOf(m2, 'comparison')[0]).toMatchObject({ status: 'unknown', reason: 'The baseline scan has no record of this Drive file.' })
  })
})

describe('modes', () => {
  const rows = [row('1.1.1', 'FAIL', { count: 1, fileIssues: [{ detail: 'no alt', severity: 'CRITICAL' }] }), row('2.5.3', 'HUMAN'), row('1.2.2', 'UNCHECKED')]
  const d = (mode) => base(rows, { mode, diffs: [{ rule_id: '1.1.1', seq: 0, before: 'a', after: 'b', verified: true }] })

  it('summary is the decision page only', () => {
    const m = buildFileReportModel(d('summary'))
    expect(m.mode).toBe('summary')
    expect(blocksOf(m, 'decisionSummary')).toHaveLength(1)
    expect(blocksOf(m, 'decisionSummary')[0].items.map((i) => i.key)).toEqual(['documentsAssessed', 'editsSaved', 'findingsVerifiedResolved', 'findingsRemaining', 'humanChecksPending', 'checksNotPerformed'])
    // The stage strip is reviewer-and-up: summary is ONE page, and it repeats the counts above.
    expect(blocksOf(m, 'stageStrip')).toHaveLength(0)
    expect(blocksOf(buildFileReportModel(d('reviewer')), 'stageStrip')[0].items.map((i) => i.key))
      .toEqual(['suggestions', 'savedEdits', 'technicalChecks', 'humanConfirmation', 'publication'])
    expect(blocksOf(m, 'changeCard')).toHaveLength(0)
    expect(blocksOf(m, 'findingCard')).toHaveLength(0)
    expect(blocksOf(m, 'appendixTable')).toHaveLength(0)
  })

  it('reviewer adds change and finding cards, prioritised', () => {
    const m = buildFileReportModel(d('reviewer'))
    expect(blocksOf(m, 'changeCard')).toHaveLength(1)
    const cards = blocksOf(m, 'findingCard')
    expect(cards.map((c) => c.status)).toEqual(['open', 'human_check', 'not_checked'])
    expect(cards[0]).toMatchObject({ rank: 1, priority: 'high', owner: null })
    expect(cards[0].steps.length).toBeGreaterThan(0)
    expect(blocksOf(m, 'appendixTable')).toHaveLength(0)
  })

  it('full adds the appendix; the compatibility alias is full', () => {
    const m = buildFileReportModel(d('full'))
    expect(blocksOf(m, 'appendixTable').map((t) => t.id)).toEqual(['appendix-changes', 'appendix-findings', 'appendix-reviews'])
    expect(buildFileCertificationModel(d('summary')).mode).toBe('full')
    const legacy = buildFileCertificationModel(d())
    for (const k of ['docTitle', 'filename', 'lang', 'targetLevel', 'fullyConformant', 'footerVersion', 'footerGenerated', 'cover', 'blocks']) expect(legacy).toHaveProperty(k)
  })
})

describe('scan report: eligible ≠ saved ≠ verified', () => {
  const files = [
    { file: 'a.pdf', score: 60, status: 'analysed', issues: [{ wcag: 'SC_1_1_1', severity: 'SERIOUS', detail: 'x' }], rec: { action: 'auto' }, remediated_at: null },
    { file: 'b.pdf', score: 70, status: 'analysed', issues: [{ wcag: 'SC_1_1_1', severity: 'SERIOUS', detail: 'y' }], rec: { action: 'auto' }, remediated_at: '2026-09-01' },
    { file: 'c.pdf', score: 80, status: 'analysed', issues: [{ wcag: 'SC_2_4_2', severity: 'MINOR', detail: 'z' }], rec: { action: 'auto' }, remediated_at: null },
  ]
  const summary = { items: [{ file: 'b.pdf', rule_id: '1.1.1', seq: 0, before: '', after: 'A', verified: true }], total: 1, documents: 1, complete: true }

  it('separates classification from executed and verified work', () => {
    const data = aggregateScanReport({ scanId: 's1', files, traces: [], hitlItems: [], diffSummary: summary })
    expect(data.routing.eligibleAuto).toBe(3)
    expect(data.routing).not.toHaveProperty('fixed')
    expect(data.execution.documentsWithSavedEdits).toBe(1)
    expect(data.execution.verifiedFixes).toBe(1)
    const m = buildScanReportModel({ ...data, mode: 'full' })
    expect(m.kind).toBe('scan')
    const dv = decision(m)
    expect(dv.editsSaved).toBe(1)
    // Was 1 — but that 1 is a CHANGE that cleared the re-scan, not a FINDING known to be resolved.
    // Changes, findings and criteria are three counts; the key named findings holds only findings.
    expect(dv.findingsVerifiedResolved).toBeNull()
    const basis = blocksOf(m, 'decisionSummary')[0].items.find((i) => i.key === 'findingsVerifiedResolved').detail
    expect(basis).toMatch(/1 saved change\(s\) across 1 document\(s\) cleared the re-scan/)
    expect(basis).toMatch(/a change count, not a finding count/)
    expect(dv.findingsRemaining).toBe(2)          // b.pdf's pre-remediation finding is not "open"
    expect(text(m)).not.toMatch(/Auto-fixed/i)
    expect(text(m)).toMatch(/eligible for automatic fixing — a recommendation, not work performed/)
  })

  it('a failed diff fetch and an unreadable queue are Not recorded, not zero', () => {
    expect(normaliseDiffSummary([])).toBeNull()
    expect(normaliseDiffSummary(null)).toBeNull()
    const data = aggregateScanReport({ files: files.map(({ remediated_at, ...f }) => f), traces: [], hitlItems: null, diffSummary: [] })
    const m = buildScanReportModel(data)
    const dv = decision(m)
    expect(dv.editsSaved).toBeNull()
    expect(dv.findingsVerifiedResolved).toBeNull()
    expect(dv.humanChecksPending).toBeNull()
  })

  it('modes: summary < reviewer (bounded) < full (every file)', () => {
    const many = Array.from({ length: 200 }, (_, i) => ({ file: `d${i}.docx`, score: 50, status: 'analysed', issues: [{ wcag: 'SC_1_1_1', severity: 'SERIOUS', detail: `f${i}` }], remediated_at: null }))
    const data = aggregateScanReport({ files: many, traces: [], hitlItems: [], diffSummary: null })
    expect(blocksOf(buildScanReportModel({ ...data, mode: 'summary' }), 'findingCard')).toHaveLength(0)
    const rv = buildScanReportModel({ ...data, mode: 'reviewer' })
    expect(blocksOf(rv, 'findingCard')).toHaveLength(50)
    expect(text(rv)).toMatch(/150 more findings \(of 200\) are listed in the Full evidence report/)
    const full = buildScanReportModel({ ...data, mode: 'full' })
    expect(blocksOf(full, 'findingCard')).toHaveLength(200)
    const idx = full.blocks.find((b) => b.k === 'appendixTable' && b.id === 'appendix-documents')
    expect(idx.rows).toHaveLength(200)
    expect(idx.complete).toBe(true)
    expect(data.appendixTotal).toBe(200)
  })

  it('estate comparison is unknown without a snapshot', () => {
    const m = buildScanReportModel(aggregateScanReport({ files, traces: [] }))
    expect(blocksOf(m, 'comparison')[0].status).toBe('unknown')
  })
})

describe('HTML output is safe and accessible', () => {
  const hostile = '<script>alert(1)</script>'
  const d = base([row('1.1.1', 'FAIL', { count: 1, fileIssues: [{ detail: hostile, severity: 'CRITICAL', page: 2 }] }), row('3.1.1', 'FIXED')], {
    file: `evil${hostile}.pptx`,
    mode: 'full',
    diffs: [{ rule_id: '3.1.1', seq: 0, before: hostile, after: '"><img src=x onerror=alert(1)>', note: hostile, verified: true }],
    reviews: {},
    previews: { [`evil${hostile}.pptx::3.1.1::0`]: PNG },
    links: [{ text: 'bad', href: 'javascript:alert(1)' }, { text: 'Corrected copy', href: '/scans/s1/files/a/corrected' }, { text: 'proto', href: '//evil.example' }],
    locationHref: () => 'javascript:alert(2)',
  })
  const html = fileReportHtml(d)
  const doc = parse(html)

  it('escapes every string and drops unsafe hrefs and images', () => {
    expect(doc.querySelectorAll('script')).toHaveLength(0)
    expect(html).not.toContain('<script>alert')
    expect(doc.querySelectorAll('[onerror]')).toHaveLength(0)
    const hrefs = [...doc.querySelectorAll('a[href]')].map((a) => a.getAttribute('href'))
    expect(hrefs.some((h) => /javascript:|^\/\//i.test(h))).toBe(false)
    expect(hrefs).toContain('/scans/s1/files/a/corrected')
    expect(doc.body.textContent).toContain('bad')
    for (const img of doc.querySelectorAll('img')) expect(img.getAttribute('src')).toMatch(/^data:image\/(png|jpeg);base64,/)
    expect(doc.querySelector('figure.preview figcaption')).toBeTruthy()
  })

  it('rejects unsafe values at the predicate level too', () => {
    expect(isSafeHref('https://ok.example/x')).toBe(true)
    expect(isSafeHref('/app/x')).toBe(true)
    for (const h of ['javascript:alert(1)', 'http://x', '//x', 'data:text/html,1', ' /x']) expect(isSafeHref(h)).toBe(false)
    expect(isSafeImageSrc(PNG)).toBe(true)
    for (const s of ['data:text/html;base64,PHNjcmlwdD4=', 'data:image/svg+xml;base64,PHN2Zz4=', 'https://x/y.png']) expect(isSafeImageSrc(s)).toBe(false)
    expect(clampText('a\n'.repeat(20)).truncated).toBe(true)
  })

  it('the full evidence report passes ACP’s own A/AA rules', () => {
    const failures = aaRules.flatMap((r) => r.check(doc).map((f) => `${r.meta.id}: ${f.detail}`))
    expect(failures).toEqual([])
    for (const t of doc.querySelectorAll('table')) {
      expect(t.querySelector('caption')).toBeTruthy()
      expect(t.querySelectorAll('thead th[scope="col"]').length).toBeGreaterThan(0)
    }
    // Heading levels never skip.
    const levels = [...doc.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((h) => Number(h.tagName[1]))
    levels.forEach((l, i) => { if (i) expect(l - levels[i - 1]).toBeLessThanOrEqual(1) })
    expect(html).toMatch(/thead \{ display: table-header-group; \}/)
    expect(html).toMatch(/overflow-wrap: anywhere/)
  }, 20000)

  it('the scan and remediation models render too', () => {
    const scan = buildScanReportModel({ ...aggregateScanReport({ files: [{ file: 'a.pdf', score: 1, status: 'analysed', issues: [{ wcag: 'SC_1_1_1', detail: hostile }] }], traces: [] }), mode: 'full' })
    const sdoc = parse(reportHtmlFromModel(scan))
    expect(sdoc.querySelectorAll('script')).toHaveLength(0)
    expect(aaRules.flatMap((r) => r.check(sdoc))).toEqual([])
    const rem = remediationReportModel(buildRemediationModel({ files: [{ file: 'a.docx', remediated_at: 'x' }], diffsByFile: { 'a.docx': [{ rule_id: '1.1.1', before: hostile, after: 'ok' }] } }), { mode: 'reviewer' })
    const rdoc = parse(reportHtmlFromModel(rem))
    expect(rdoc.querySelectorAll('script')).toHaveLength(0)
    expect(aaRules.flatMap((r) => r.check(rdoc))).toEqual([])
  }, 20000)
})
