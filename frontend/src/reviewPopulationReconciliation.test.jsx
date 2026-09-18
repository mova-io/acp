import {act} from 'react'
import {afterEach, expect, it} from 'vitest'
import {createTestRoot,unmountAll} from './testRoots.js'
import {remainingWorkStatus} from './remainingWorkStatus.js'
import {remediationReviewCounts} from './remediationCountSummary.js'
import {RemediationActivityPanel} from './RemediationOpsPanel.jsx'
import {automaticReviewQueue} from './automaticReviewQueue.js'
import {autoFixRows} from './remediationInboxModel.js'
import {explainReviewPopulation} from './reviewPopulationExplanation.js'
import {unresolvedWorkSummary} from './unresolvedWorkSummary.js'
import {reviewWorkBreakdown} from './reviewWorkBreakdown.js'
const rows=[
 ...Array.from({length:112},(_,id)=>({id,file:'automatic.docx',rule_id:'1.1.1',status:'pending',aiDraftable:true,hasProposal:false})),
 ...Array.from({length:10},(_,i)=>({id:200+i,file:'instructions.docx',rule_id:'1.4.5',status:'pending',aiDraftable:true,hasProposal:false})),
 ...Array.from({length:176},(_,i)=>({id:300+i,file:'review.docx',rule_id:'1.4.5',status:'pending',hasProposal:true,after:'Recovered text'})),
 ...Array.from({length:4},(_,i)=>({id:600+i,file:'manual.pdf',rule_id:'1.4.1',status:'pending',manual:true})),
]
afterEach(unmountAll)
it('reconciles screenshot populations without counting status checks as human input',()=>{
 const result=remainingWorkStatus({rows,automatic:true})
 expect(result.humanTotal).toBe(4)
 expect(result.humanTotal).toBe(remediationReviewCounts(rows,{}, {},true).pendingItems)
 expect(result.statusTotal).toBe(298)
 expect(result.humanTotal+result.statusTotal).toBe(302)
 expect(result.notices.filter(n=>n.population==='human').reduce((s,n)=>s+n.count,0)).toBe(4)
 expect(result.notices.find(n=>n.key==='status:missing-proposals').count).toBe(122)
 expect(result.notices.find(n=>n.key==='missing-proposals')).toBeUndefined()
})
it('keeps manual assignments human and excludes optional applied-change inspection',()=>{
 const changed=[...rows,{id:900,file:'caption.docx',rule_id:'1.1.1',status:'pending',manual:true},
 {id:901,file:'done.docx',rule_id:'1.1.1',status:'pending',autoApplied:true,hasProposal:true,after:'Saved change'}]
 const decisions={0:{state:'assigned'}}
 const result=remainingWorkStatus({rows:changed,automatic:true,decisions})
 expect(result.humanTotal).toBe(6)
 expect(result.humanTotal).toBe(remediationReviewCounts(changed,decisions,{},true).pendingItems)
 expect(result.statusTotal).toBe(297)
})
it('renders the live panel with separate reconciled groups under automatic approval',async()=>{
 const {root,container}=createTestRoot()
 await act(async()=>root.render(<RemediationActivityPanel snapshot={{scan_id:'scan',batch_id:'batch',terminal:true}} rows={rows} automatic/>))
 const summary=container.querySelector('[aria-label="Remaining work item counts"]')
 expect(summary.textContent).toContain('4 need your input')
 expect(summary.textContent).toContain('298 status checks')
 expect(container.textContent).not.toContain('122 review items')
 expect(container.textContent).toContain('122 status-check items')
 const details=container.querySelector('.remaining-work-details')
 expect(details).not.toBeNull()
 expect(details.open).toBe(false)
 expect(container.querySelector('[aria-label="Remaining work item counts"]').textContent).toContain('4 need your input')
})

// Scan b3eba56d4d5d, synthetic: the ops panel, the Review workspace tile, the pills and the
// explanation must all describe the same two status checks and the same zero human tasks.
const FILE='UTSW_Discharge_Summary.docx'
const policy={enabled:true,supported:true,run_id:'run-1',source_revision:'rev-1'}
const raw111={id:101,scan_id:'scan',file:FILE,rule_id:'SC_1_1_1',rule_name:'Non-text Content',status:'pending',proposals:[{proposed_value:'Synthetic chart',locator:'docx:drawing:1:paragraph:32'}],proposal_snapshot_ids:['p1'],source_revision:'rev-1',decision_version:1}
const production=(fixed)=>automaticReviewQueue([
 {id:101,_raw:fixed?{...raw111,superseded:true,superseded_reason:'target_removed_by_verified_fix',superseded_evidence:{removed_by_rule_id:'1.4.5'}}:raw111,file:FILE,rule_id:'SC_1_1_1',aiDraftable:true,hasProposal:true,after:'Synthetic chart',proposals:raw111.proposals},
 {id:102,_raw:{id:102,rule_id:'SC_1_4_5',status:'approved',applied:1},file:FILE,rule_id:'SC_1_4_5',status:'approved',applied:true,validated:fixed,hasProposal:true,after:'Totals'},
 ...autoFixRows([{file:FILE,sc:'1.4.3',verified:true},{file:FILE,sc:'3.1.1',verified:true},{file:FILE,sc:'1.4.5',verified:true}],sc=>sc),
],policy,{})
it('reconciles the production population across the ops panel, workspace tile and explanation',()=>{
 const rows=production(false)
 const status=remainingWorkStatus({rows,automatic:true})
 const e=explainReviewPopulation({rows,automatic:true})
 expect(status.humanTotal).toBe(0)
 expect(status.humanTotal).toBe(remediationReviewCounts(rows,{},{},true).pendingItems)
 expect(status.humanTotal).toBe(e.humanCount)
 // Was 1 before: the saved-but-unchecked 1.4.5 change was in the Status checks pill and not here.
 expect(status.statusTotal).toBe(2)
 expect(status.statusTotal).toBe(e.statusCheckCount)
 expect(status.notices.find(n=>n.key==='status:processing')?.label).toBe('Saved change awaiting its check')
})
it('a target replaced by a verified fix is a result everywhere, never pending or unresolved work',()=>{
 const rows=production(true)
 const replaced=rows.find(r=>r.id===101)
 expect(reviewWorkBreakdown([replaced]).results).toBe(1)
 expect(unresolvedWorkSummary(rows).total).toBe(0)
 expect(remediationReviewCounts(rows,{},{},true).pendingItems).toBe(0)
 const status=remainingWorkStatus({rows,automatic:true})
 expect([status.humanTotal,status.statusTotal]).toEqual([0,0])
 expect(explainReviewPopulation({rows,automatic:true,unresolvedFindings:[],findingTotal:0}).allClear).toBe(true)
})
