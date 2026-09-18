import { expect, it } from 'vitest'
import { automaticReviewQueue } from './automaticReviewQueue.js'
import { workflowCounts } from './remediationInboxModel.js'
import { remediationReviewCounts } from './remediationCountSummary.js'
const policy={enabled:true,supported:true,run_id:'run',source_revision:'source'}
const row={id:'proposal',file:'a.docx',status:'pending',hasProposal:true,after:'Alternative text',proposals:[{proposed_value:'Alternative text',source:'AI',model:'vision',model_call_id:'call'}],_raw:{corrected_artifact:'none',finding_count:1,proposal_snapshot_ids:['snapshot'],source_revision:'source',decision_version:0,automatic_approval:{run_id:'run',source_revision:'source',state:'queued'}}}
it('moves only exact admitted automatic proposals out of human review into Processing, never Completed',()=>{
 const queue=automaticReviewQueue([row],policy)
 expect(queue[0].automaticQueued).toBe(true)
 expect(workflowCounts(queue)).toMatchObject({'needs-review':0,'awaiting-validation':1,completed:0})
 expect(remediationReviewCounts(queue).pendingItems).toBe(0)
 expect(row.automaticQueued).toBeUndefined()
 expect(row.status).toBe('pending')
})
it('retains manual, failed, rejected, stale and unadmitted work requiring input',()=>{
 const rows=[{...row,id:'manual',hasProposal:false,after:'',proposals:[]},{...row,id:'failed',status:'verification_failed'},{...row,id:'stale',stale:true},{...row,id:'missing',_raw:{corrected_artifact:'none',...row._raw,proposal_snapshot_ids:[]}},{...row,id:'other',_raw:{corrected_artifact:'none',...row._raw,automatic_approval:{...row._raw.automatic_approval,run_id:'other'}}}]
 expect(automaticReviewQueue(rows,policy).every(row=>!row.automaticQueued)).toBe(true)
 expect(automaticReviewQueue([row],{...policy,enabled:false})[0].automaticQueued).toBeUndefined()
 expect(automaticReviewQueue([row],null)[0].automaticQueued).toBeUndefined()
 expect(automaticReviewQueue([row],policy,{proposal:{state:'rejected'}})[0].automaticQueued).toBeUndefined()
})
it('does not put optional auto/verify inspections back into the actionable human queue',()=>{
 const queue=automaticReviewQueue([{...row,rule_id:'auto/verify',inspectionOnly:true}],policy)
 expect(workflowCounts(queue)).toMatchObject({completed:1,'needs-review':0,'awaiting-validation':0})
})
it('keeps server-owned deferral reasons visible without claiming queued or verified work',()=>{
 const deferred={...row,_raw:{corrected_artifact:'none',...row._raw,automatic_approval:{run_id:'run',source_revision:'source',state:'review_required',owner:'You',reason:'This change requires individual judgment'}}}
 const queue=automaticReviewQueue([deferred],policy)
 expect(queue[0].automaticQueued).toBeUndefined()
 expect(queue[0].automaticReason).toBe('This change requires individual judgment')
 expect(workflowCounts(queue)).toMatchObject({'needs-review':1,completed:0})
 expect(automaticReviewQueue([deferred],{...policy,run_id:'other'})[0].automaticDisposition).toBeNull()
})
it('routes exact-bound jobless approvals to Blocked while retaining terminal and active outcomes',()=>{
 const blocked={...row,status:'approved',_raw:{corrected_artifact:'none',...row._raw,automatic_approval:{run_id:'run',source_revision:'source',proposal_snapshot_ids:['snapshot'],state:'blocked',owner:'You',reason:'No active application or verification job is recorded.'}}}
 const queue=automaticReviewQueue([blocked,{...blocked,id:'written',applied:true}],policy)
 expect(workflowCounts(queue)).toMatchObject({blocked:2,'awaiting-validation':0,completed:0})
 expect(workflowCounts(automaticReviewQueue([{...blocked,validated:true,applied:true}],policy))).toMatchObject({blocked:0,completed:1})
 const stale={...blocked,_raw:{corrected_artifact:'none',...blocked._raw,automatic_approval:{...blocked._raw.automatic_approval,proposal_snapshot_ids:['old']}}}
 expect(workflowCounts(automaticReviewQueue([stale],policy))).toMatchObject({blocked:0,'awaiting-validation':1})
 const active={...blocked,_raw:{corrected_artifact:'none',...blocked._raw,automatic_approval:{...blocked._raw.automatic_approval,state:'queued'}}}
 expect(workflowCounts(automaticReviewQueue([active],policy))).toMatchObject({blocked:0,'awaiting-validation':1})
})
it('rejects responsibility from another scan or missing bound snapshot manifest',()=>{
 const marker={state:'review_required',responsibility:'human',run_id:'run',source_revision:'source',scan_id:'other',proposal_snapshot_ids:['snapshot']}
 const r={...row,scanId:'scan',_raw:{corrected_artifact:'none',...row._raw,scan_id:'scan',automatic_approval:marker}}
 expect(automaticReviewQueue([r],policy)[0].automaticDisposition).toBeNull()
 expect(automaticReviewQueue([{...r,_raw:{corrected_artifact:'none',...r._raw,automatic_approval:{...marker,scan_id:'scan',proposal_snapshot_ids:undefined}}}],policy)[0].automaticDisposition).toBeNull()
})
