import { act, useState } from 'react'
import { beforeEach, afterEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationInbox from './RemediationInbox.jsx'
import useReviewQueueRefresh from './useReviewQueueRefresh.js'
import useRunAiApproval from './useRunAiApproval.js'
import { authEpoch, noteAuthChange, _resetAuthEpoch } from './apiIdentity.js'
const list=vi.fn(), get=vi.fn(), save=vi.fn()
vi.mock('./api.js',()=>({listHitlQueue:(...a)=>list(...a),getRunAiApproval:(...a)=>get(...a),setRunAiApproval:(...a)=>save(...a)}))
globalThis.IS_REACT_ACT_ENVIRONMENT=true
const policy={enabled:false,supported:true,run_id:'batch',source_revision:'source',revision:0}
const proposal={id:'ai',file:'a.docx',status:'pending',hasProposal:true,after:'Alternative text',proposals:[{proposed_value:'Alternative text',source:'AI',model:'vision',model_call_id:'call'}],_raw:{corrected_artifact:'none',proposal_digest:'digest-test',finding_count:1,proposal_snapshot_ids:['snapshot'],source_revision:'source',decision_version:0}}
const manual={id:'manual',file:'b.docx',rule_id:'1.3.1',status:'pending',manual:true,hasProposal:false,title:'Requires human input'}
const admitted={...proposal,_raw:{corrected_artifact:'none',proposal_digest:'digest-test',...proposal._raw,automatic_approval:{run_id:'batch',source_revision:'source',state:'checking'}}}
let queued, latestRefresh
function View({scan='scan',batch='batch',revision=0,active=true}){
 const approval=useRunAiApproval(scan,batch)
 const [state,setState]=useState(null), [error,setError]=useState('')
 const identity=`${authEpoch()}:${scan}:${batch}`
 const rows=state?.identity===identity?state.rows:[]
 queued=rows
 latestRefresh=useReviewQueueRefresh({scanId:scan,batchId:batch,approval:approval.policy,progressKey:revision,active:active&&approval.enabled===true,onRows:rows=>setState({identity,rows}),onError:()=>setError('Refresh unavailable')})
 return <><span>{error}</span><RemediationInbox scanId={scan} queue={rows} autoApprove={approval.enabled} automaticApprovalPolicy={approval.policy} onAutoApproveChange={approval.change} autoApproveSaving={approval.saving}/></>
}
let root,container
beforeEach(()=>{vi.useFakeTimers();list.mockReset().mockResolvedValue([proposal,manual]);get.mockReset().mockResolvedValue(policy);save.mockReset().mockImplementation(async(s,b,v)=>({...policy,run_id:b,enabled:v.enabled,revision:1}));({root,container}=createTestRoot())})
afterEach(async()=>{await unmountAll();vi.useRealTimers();_resetAuthEpoch()})
const render=props=>act(async()=>{root.render(<View {...props}/>);await Promise.resolve()})
const text=()=>container.textContent.replace(/\s+/g,' ')
it('drains eligible review into Processing then Results using server rows, while manual work remains',async()=>{
 await render()
 expect(text()).toContain('Needs review')
 expect(queued).toHaveLength(2)
 list.mockResolvedValue([admitted,manual])
 await act(async()=>container.querySelector('[role=switch]').click())
 expect(queued[0]._raw.automatic_approval.state).toBe('checking')
 expect(text()).toMatch(/Processing\s*1/)
 expect(text()).toMatch(/Results\s*0/)
 list.mockResolvedValue([{...proposal,status:'approved',validated:true},manual])
 await render({revision:2})
 await act(async()=>vi.advanceTimersByTimeAsync(800))
 expect(text()).toMatch(/Results\s*1/)
 expect(queued.find(r=>r.id==='manual')).toEqual(manual)
 expect(save).toHaveBeenCalledTimes(1)
})
it('switching off does not retain projected automatic work or replay approval',async()=>{
 get.mockResolvedValue({...policy,enabled:true});list.mockResolvedValue([admitted,manual])
 await render();expect(text()).toMatch(/Processing\s*1/)
 list.mockResolvedValue([proposal,manual])
 await act(async()=>container.querySelector('[role=switch]').click())
 expect(container.querySelector('[role=switch]').checked).toBe(false)
 expect(text()).toMatch(/Processing\s*0/)
 const calls=list.mock.calls.length
 await act(async()=>vi.advanceTimersByTimeAsync(15000))
 // Only the already scheduled progress refresh may finish after disabling.
 expect(list.mock.calls.length-calls).toBeLessThanOrEqual(1)
 expect(save).toHaveBeenCalledTimes(1)
})
it('coalesces multiple progress updates behind one in-flight read and preserves counts on errors',async()=>{
 await render({active:false})
 await act(async()=>vi.advanceTimersByTimeAsync(800))
 let finish;list.mockImplementationOnce(()=>new Promise(r=>{finish=r})).mockResolvedValue([proposal,manual])
 await act(async()=>{latestRefresh()})
 const calls=list.mock.calls.length
 await render({revision:1,active:false});await act(async()=>vi.advanceTimersByTimeAsync(800))
 await render({revision:2,active:false});await act(async()=>vi.advanceTimersByTimeAsync(800))
 expect(list.mock.calls.length).toBe(calls)
 await act(async()=>finish([admitted,manual]))
 expect(list.mock.calls.length).toBe(calls+1)
 list.mockRejectedValue(new Error('offline'))
 await act(async()=>{latestRefresh()})
 expect(queued).toEqual([proposal,manual]);expect(text()).toContain('Refresh unavailable')
})
it.each(['scan','batch','account'])('ignores a late queue response after %s navigation',async kind=>{
 await render({active:false});await act(async()=>vi.advanceTimersByTimeAsync(800))
 let finish;list.mockImplementationOnce(()=>new Promise(r=>{finish=r})).mockResolvedValue([manual])
 await act(async()=>{latestRefresh()})
 if(kind==='account')noteAuthChange('old','new')
 await render({scan:kind==='scan'?'newscan':'scan',batch:kind==='batch'?'newbatch':'batch',active:false})
 await act(async()=>finish([proposal]))
 expect(queued).toEqual([manual])
})
it('bounds a hung refresh and stops retrying terminal owner denial',async()=>{
 await render({active:false});await act(async()=>vi.advanceTimersByTimeAsync(800))
 list.mockImplementationOnce(()=>new Promise(()=>{}))
 await act(async()=>{latestRefresh()})
 const signal=list.mock.calls.at(-1)[2].signal
 await act(async()=>vi.advanceTimersByTimeAsync(20000))
 expect(signal.aborted).toBe(true);expect(queued).toEqual([proposal,manual])
 list.mockRejectedValue(Object.assign(new Error('forbidden'),{status:403}))
 await act(async()=>{latestRefresh()})
 const calls=list.mock.calls.length
 await act(async()=>{latestRefresh()})
 expect(list.mock.calls.length).toBe(calls)
})
it('cancels an ignored-abort request on unmount without receiving late rows',async()=>{
 await render({active:false});await act(async()=>vi.advanceTimersByTimeAsync(800))
 let finish;list.mockImplementationOnce(()=>new Promise(r=>{finish=r}))
 await act(async()=>{latestRefresh()})
 const signal=list.mock.calls.at(-1)[2].signal
 await act(async()=>root.unmount())
 expect(signal.aborted).toBe(true)
 const previous=queued
 await act(async()=>finish([admitted]))
 expect(queued).toBe(previous)
})
