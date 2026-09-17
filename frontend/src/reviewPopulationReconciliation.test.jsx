import {act} from 'react'
import {afterEach, expect, it} from 'vitest'
import {createTestRoot,unmountAll} from './testRoots.js'
import {remainingWorkStatus} from './remainingWorkStatus.js'
import {remediationReviewCounts} from './remediationCountSummary.js'
import {RemediationActivityPanel} from './RemediationOpsPanel.jsx'
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
