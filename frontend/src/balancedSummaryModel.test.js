import { describe, it, expect } from 'vitest'
import { balancedSummaryModel, reviewAge, treemapRects, activityMonths, criterionBreakdown } from './balancedSummaryModel.js'

const now = Date.parse('2026-09-14T12:00:00Z')
const issue = extra => ({ wcag: '1.3.1', severity: 'SERIOUS', ...extra })
const file = (name, issues = [], extra = {}) => ({ file: name, type: 'DOCX', status: 'analysed', score: 50, issues, ...extra })
const run = { id: 's1', files: 8, scope: { scan_scope: { '1.3.1': true, '1.1.1': true, '1.4.3': true }, inventory: { discovered: 8, assessment_eligible: 6, by_format: { docx: 6, image: 2 } } } }
const cap = { docx: { '1.3.1': 'auto', '1.1.1': 'assisted', '1.4.3': 'auto' } }

describe('balanced summary accounting', () => {
  it('keeps measured, pending, ineligible, error and missing inventory disjoint', () => {
    const files = [file('a.docx', [issue()]), file('broken.docx', [], { score: null, status: 'error' })]
    const inventory = { rows: [{ file: 'a.docx', format: 'docx', status: 'assessable' }, { file: 'broken.docx', format: 'docx', status: 'assessable' }, { file: 'waiting.docx', format: 'docx', status: 'assessable' }, { file: 'photo.png', format: 'image', status: 'metadata_only' }] }
    const m = balancedSummaryModel({ run, files, inventory, cap, now })
    expect(m.assessed).toBe(1); expect(m.rate).toBe(1 / 6)
    expect(m.formats[0]).toMatchObject({ total: 6, assessed: 1, pending: 1, blocked: 1, unknown: 3 })
    expect(m.formats[1]).toMatchObject({ total: 2, ineligible: 1, unknown: 1 })
    for (const r of m.formats) expect(r.assessed + r.pending + r.ineligible + r.blocked + r.unknown).toBe(r.total)
  })
  it('does not turn a failed inventory read or an unassessed file into a pass or pending eligibility', () => {
    const m = balancedSummaryModel({ run, files: [file('waiting.docx', [], { score: null, status: 'discovered' })], cap, now })
    expect(m.findingsKnown).toBe(false); expect(m.assessed).toBe(0)
    expect(m.formats[0]).toMatchObject({ total: 6, assessed: 0, pending: 0, unknown: 6 })
    expect(balancedSummaryModel().discovered).toBeNull()
  })
  it('reuses remediation classification and splits AI-applied presentation without double counting', () => {
    const files = [file('a.docx', [issue(), issue({ has_proposal: true }), issue({ wcag: '1.1.1' }), issue({ human_only: true }), issue({ remediation_supported: false }), issue({ processing_blocked: true }), issue({ applied: true }), issue({ applied: true, proposals: [{ model_call_id: 'm1', model: 'recorded-model' }] })])]
    const m = balancedSummaryModel({ run, files, cap, now })
    expect(m.tags).toMatchObject({ automatic: 1, approval: 1, suggestion: 1, manual: 1, unsupported: 1, blocked: 1, applied: 1, ai_applied: 1, verified: 0 })
    expect(Object.values(m.tags).reduce((a, b) => a + b)).toBe(m.findings.length)
    expect(m.reviewCount).toBe(3); expect(m.age.unknown).toHaveLength(3)
    expect(m.departments[0][0]).toBe('Not recorded')
  })
  it('uses frozen criteria and excludes resolved findings from the assessment population', () => {
    const m = balancedSummaryModel({ run: { ...run, scope: { ...run.scope, scan_scope: { '1.1.1': true } } }, files: [file('a.docx', [issue(), issue({ wcag: '1.1.1' }), issue({ wcag: '1.1.1', resolution: 'fixed' })])], cap, now })
    expect(m.findings).toHaveLength(1); expect(m.groups['Text alternatives']).toHaveLength(1)
  })
  it('breaks Other down by actual in-scope criterion, summing exactly to the parent', () => {
    const scope = { ...run.scope, scan_scope: { '1.3.1': true, '2.4.2': true, '1.4.4': true, '3.1.1': true } }
    const files = [file('a.docx', [issue(), issue({ wcag: '2.4.2' }), issue({ wcag: '1.4.4' }), issue({ wcag: 'SC_3_1_1' }), issue({ wcag: '2.4.4' })]),
      file('b.docx', [issue({ wcag: '2.4.2 Page Titled' }), issue({ wcag: '3.1.1' }), issue({ wcag: '2.4.2', resolution: 'fixed' })])]
    const m = balancedSummaryModel({ run: { ...run, scope }, files, cap, now })
    expect(m.otherCriteria.map(r => [r.label, r.rows.length])).toEqual([['2.4.2 Page Titled', 2], ['3.1.1 Language of Page', 2], ['1.4.4 Resize Text', 1]])
    expect(m.otherCriteria.reduce((sum, r) => sum + r.rows.length, 0)).toBe(m.groups.Other.length)
    expect(m.otherCriteria.flatMap(r => r.rows).every(r => m.groups.Other.includes(r))).toBe(true)
    expect(balancedSummaryModel().otherCriteria).toEqual([])
  })
  it('labels missing and uncatalogued criteria honestly and orders ties deterministically', () => {
    const rows = criterionBreakdown([{ sc: '9.9.9' }, {}, { sc: '1.4.10' }, { sc: '1.4.4' }, { sc: ' ' }])
    expect(rows.map(r => [r.label, r.rows.length])).toEqual([['Criterion not recorded', 2], ['1.4.4 Resize Text', 1], ['1.4.10 Reflow', 1], ['9.9.9 · title not in catalog', 1]])
  })
  it('uses finding first-seen dates, keeps missing/future dates unknown, and handles age boundaries', () => {
    const ago = days => new Date(now - days * 86400000).toISOString()
    expect(reviewAge({ first_seen_at: ago(6.9) }, now)).toBe('week')
    expect(reviewAge({ first_seen_at: ago(7) }, now)).toBe('month')
    expect(reviewAge({ first_seen_at: ago(30) }, now)).toBe('month')
    expect(reviewAge({ first_seen_at: ago(30.1) }, now)).toBe('older')
    for (const f of [{ created_at: ago(100) }, { first_seen_at: ago(-1) }, { first_seen_at: 'bad' }, {}]) expect(reviewAge(f, now)).toBe('unknown')
  })
  it('flags inventory totals that cannot reconcile and does not produce over-100% coverage', () => {
    const m = balancedSummaryModel({ run: { ...run, scope: { inventory: { discovered: 1, assessment_eligible: 1, by_format: { docx: 1 }, truncated: true } } }, files: [file('a.docx'), file('b.docx')], cap })
    expect(m.inconsistent).toBe(true); expect(m.rate).toBeNull(); expect(m.truncated).toBe(true)
  })
  it('keeps a dominant format finite and exactly proportional in the treemap', () => {
    const rects = treemapRects([{ key: 'pdf', total: 99 }, { key: 'html', total: 1 }])
    expect(rects).toHaveLength(2)
    expect(rects.reduce((sum, r) => sum + r.w * r.h, 0)).toBeCloseTo(10000)
    expect(rects[0].w * rects[0].h).toBeCloseTo(9900)
    expect(treemapRects([{ total: 0 }])).toEqual([])
  })
  it('groups calendar activity by actual UTC month and rejects invalid calendar dates', () => {
    expect(activityMonths([{ date: '2026-09-01', attempts: 2 }, { date: '2026-08-31', attempts: 1 }, { date: '2026-02-30', attempts: 1 }, { date: 'bad', attempts: 1 }]).map(m => m.month)).toEqual(['2026-08', '2026-09'])
  })
})
