import { expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import Card from './WorkflowStageActivityCard.jsx'
it('names accounted findings and the missing outcome instead of claiming fix completion', () => {
  const html = renderToStaticMarkup(<Card snapshot={{ stage: 'remediate', state: 'processing_complete',
    reconciliation: {total:2,accounted:2,exact:true}, integrity:{ok:false},
    domain_reconciliation:{unit:'assessed findings',total:8,accounted:7,exact:false,
      buckets:{resolved_verified:4,awaiting_review:3}} }} />)
  expect(html).toContain('8 assessed findings')
  expect(html).toContain('1 assessed finding still lack a recorded outcome')
  expect(html).toContain('Finding results')
  expect(html).not.toContain('role="progressbar"')
  expect(html).not.toContain('88% complete')
})

import { alignRemediationAssessment } from './canonicalStageCard.js'
import Stack from './WorkflowStageStack.jsx'
it('shows all 32 Assess findings and makes the 29-item historical breakdown add up without inventing fixes', () => {
  const original = { stage: 'remediate', execution_id: 'rem', workflow_revision: 1, state: 'processing_complete',
    reconciliation: { total: 4, accounted: 4, exact: true }, integrity: { ok: false },
    domain_reconciliation: { unit: 'assessed findings', total: 29, accounted: 22, exact: false,
      buckets: { resolved_verified: 13, awaiting_review: 9 } } }
  const aligned = alignRemediationAssessment(original, 32)
  expect(aligned.domain_reconciliation.total).toBe(32)
  expect(Object.values(aligned.domain_reconciliation.buckets).reduce((a,b) => a+b,0)).toBe(32)
  expect(aligned.domain_reconciliation.buckets).toMatchObject({ resolved_verified: 13, awaiting_review: 9,
    awaiting_recorded_outcome: 7, not_in_remediation_breakdown: 3 })
  expect(original.domain_reconciliation.total).toBe(29)
  const html = renderToStaticMarkup(<Stack lineage={{scan_id:'scan', workflow_revision:1, stages:[original]}}
    assessmentFindings={{scanId:'scan', total:32}} />)
  expect(html).toContain('32 assessed findings')
  expect(html).toContain('Not in remediation breakdown')
  expect(html).toContain('10 assessed findings still lack a recorded outcome')
  const other = renderToStaticMarkup(<Stack lineage={{scan_id:'scan',workflow_revision:1,stages:[original]}}
    assessmentFindings={{scanId:'other-scan',total:32}} />)
  expect(other).not.toContain('32 assessed findings')
})

import { omittedAssessmentGroups } from './canonicalStageCard.js'
it('traces omitted findings to recorded file/SC evidence and refuses inconsistent evidence', () => {
  const snapshot = { domain_reconciliation: { total: 2 } }
  const audit = { findings_recorded: 2, finding_groups: [{file:'one.pdf',rule_id:'SC_2_4_2',finding_count:2}] }
  const rows = [{file:'one.pdf',findings:[{sc:'2.4.2'},{sc:'2.4.2'},{sc:'1.4.1'}]}]
  expect(omittedAssessmentGroups(snapshot,audit,rows)).toEqual([{file:'one.pdf',sc:'1.4.1',count:1}])
  expect(omittedAssessmentGroups(snapshot,audit,[{file:'different.pdf',findings:rows[0].findings}])).toEqual([])
  expect(omittedAssessmentGroups(snapshot,{...audit,findings_recorded:3},rows)).toEqual([])
  const html = renderToStaticMarkup(<Card snapshot={{stage:'remediate',state:'processing_complete',
    domain_reconciliation:{total:3,accounted:2,buckets:{resolved_verified:2}},
    omitted_assessment_groups:[{file:'one.pdf',sc:'1.4.1',count:1}]}} />)
  expect(html).toContain('Remediate · Processing finished')
  expect(html).not.toContain('Processing complete')
  expect(html).toContain('2 with recorded outcomes + 1 awaiting an outcome = 3 assessed findings')
  expect(html).toContain('one.pdf')
  expect(html).toContain('SC 1.4.1')
})


it('makes the screenshot outcome buckets sum to the same 23 assessed findings without claiming 23 fixes', () => {
  const snapshot = { stage: 'remediate', state: 'processing_complete',
    integrity: { ok: false },
    domain_reconciliation: { total: 23, accounted: 15, exact: false,
      unit: 'assessed findings', buckets: { resolved_verified: 9, awaiting_review: 6,
        approved_awaiting_verification: 0, unchanged_no_fix: 0, failed: 0, excluded: 0, superseded: 0 } } }
  const aligned = alignRemediationAssessment(snapshot, 23)
  expect(Object.values(aligned.domain_reconciliation.buckets).reduce((sum, n) => sum + n, 0)).toBe(23)
  expect(aligned.domain_reconciliation.accounted).toBe(15)
  const html = renderToStaticMarkup(<Stack lineage={{scan_id:'scan',workflow_revision:1,
    stages:[{...snapshot,workflow_revision:1}]}} assessmentFindings={{scanId:'scan',total:23}} />)
  expect(html).toContain('23 assessed findings')
  expect(html).not.toContain('15 of 23')
  expect(html).toContain('9 resolved · verified + 6 awaiting review + 8 awaiting recorded outcome = 23 assessed findings')
  expect(html).toContain('Awaiting outcome: 8')
  expect(html).not.toContain('100%')
  expect(snapshot.domain_reconciliation.buckets.awaiting_recorded_outcome).toBeUndefined()
})


it('does not invent a balancing equation when recorded buckets disagree with the ledger', () => {
  const html = renderToStaticMarkup(<Card snapshot={{stage:'remediate',state:'processing_complete',
    integrity:{ok:false}, domain_reconciliation:{total:23,accounted:15,exact:false,
      buckets:{resolved_verified:20,awaiting_review:6}}}} />)
  expect(html).toContain('23 assessed findings')
  expect(html).not.toContain('= 23 assessed findings')
  expect(html).toContain('Accounting is reconciling')
})

it('shows verified plus remaining equal to all 23 findings, including missing outcomes', () => {
  const html = renderToStaticMarkup(<Card snapshot={{stage:'remediate',state:'processing_complete',
    domain_reconciliation:{total:23,accounted:15,exact:false,buckets:{resolved_verified:9,awaiting_review:6}}}} />)
  expect(html).toContain('Verified fixes: 9')
  expect(html).toContain('Unresolved findings: 6')
  expect(html).toContain('Awaiting outcome: 8')
})

it('replaces finding KPIs with the document KPI host without mixing their units', () => {
  const html=renderToStaticMarkup(<Card progressHostId="five-status-tiles" progressScanId="scan" snapshot={{stage:'remediate',execution_id:'batch',state:'processing_complete',domain_reconciliation:{total:30,accounted:30,exact:true,buckets:{resolved_verified:13,awaiting_review:17}}}} />)
  expect(html).toContain('id="five-status-tiles"')
  expect(html).toContain('data-batch-id="batch"')
  expect(html).not.toContain('Total assessed</span>')
  expect(html).not.toContain('Verified fixed</span>')
  expect(html).toContain('30 assessed findings')
})
