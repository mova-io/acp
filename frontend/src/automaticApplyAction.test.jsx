import { act } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationInbox from './RemediationInbox.jsx'
globalThis.IS_REACT_ACT_ENVIRONMENT = true
beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
afterEach(async () => { await unmountAll() })
const policy={enabled:true,supported:true,run_id:'run',source_revision:'source'}
const proposal={id:'proposal',file:'a.docx',status:'pending',hasProposal:true,after:'Alternative text',proposals:[{proposed_value:'Alternative text',source:'AI',model:'vision',model_call_id:'call'}],_raw:{corrected_artifact:'none',finding_count:1,proposal_snapshot_ids:['snapshot'],source_revision:'source',decision_version:0}}
const admitted=state=>({...proposal,_raw:{corrected_artifact:'none',...proposal._raw,automatic_approval:{run_id:'run',source_revision:'source',state}}})
async function mount(row,overrides={}) {
 const {container,root}=createTestRoot(),onDecide=vi.fn(async()=>{})
 const render=async next=>act(async()=>root.render(<RemediationInbox queue={[next]} initialTab="all" autoApprove automaticApprovalPolicy={policy} onDecide={onDecide} {...overrides}/>))
 await render(row);return {container,render,onDecide}
}
const action=container=>[...container.querySelectorAll('button')].find(button=>['Apply this fix','Review and apply','Queued automatically','Checking automatic eligibility','Applying…','Verifying…'].includes(button.textContent))
it.each([['queued','Queued automatically'],['checking','Checking automatic eligibility'],['processing','Applying…']])('disables duplicate manual approval for server-admitted %s work',async(state,label)=>{
 const {container,onDecide}=await mount(admitted(state)),button=action(container)
 expect(button.textContent).toBe(label);expect(button.disabled).toBe(true)
 await act(async()=>button.click());expect(onDecide).not.toHaveBeenCalled()
})
it('does not disable approval merely because the switch is on',async()=>{
 const {container,onDecide}=await mount(proposal),button=action(container)
 expect(button.textContent).toBe('Review and apply');expect(button.disabled).toBe(false)
 await act(async()=>button.click());expect(onDecide).toHaveBeenCalledTimes(1)
})
it.each([{enabled:false},{supported:false},{run_id:'other'},{source_revision:'changed'}])('keeps unadmitted proposals actionable with mismatched policy %j',async change=>{
 const {container}=await mount(admitted('queued'),{automaticApprovalPolicy:{...policy,...change}})
 expect(action(container).textContent).toBe(change.enabled === false ? 'Apply this fix' : 'Review and apply');expect(action(container).disabled).toBe(false)
})
it('blocks an old callback after the current server row becomes automatically admitted',async()=>{
 const {container,render,onDecide}=await mount(proposal),button=action(container)
 // Reproduce a click callback queued before the server refresh using the actual mounted handler.
 const oldClick=button[Object.keys(button).find(key=>key.startsWith('__reactProps$'))].onClick
 await render(admitted('queued'));expect(action(container).disabled).toBe(true)
 await act(async()=>oldClick({}));expect(onDecide).not.toHaveBeenCalled()
})
