import React,{act} from 'react'
import {createRoot} from 'react-dom/client'
import {afterEach,expect,it} from 'vitest'
import {Activity} from './RemediationOpsPanel.jsx'
import {addRemediationEvent,remediationEventLine} from './remediationEventFeed.js'
let root,host
const rows=n=>Array.from({length:n},(_,i)=>({key:String(n-i),id:String(n-i),kind:'remediate.document_completed',tone:'success',line:`Update ${n-i}`,documentKey:'same'}))
afterEach(async()=>{if(root)await act(()=>root.unmount());host?.remove();root=null})
async function render(events){if(!root){host=document.createElement('div');document.body.append(host);root=createRoot(host)}await act(()=>root.render(<Activity events={events}/>))}
it('keeps all 140 updates and prepends without replacing existing rows',async()=>{
 await render(rows(140));const list=host.querySelector('ol'),oldest=list.lastElementChild
 expect(list.children).toHaveLength(140);await render(rows(141))
 expect(host.querySelector('ol')).toBe(list);expect(list.lastElementChild).toBe(oldest)
 expect(list.firstElementChild.textContent).toContain('Update 141')
 expect(host.querySelector('input[aria-label="Scroll activity history"]')).not.toBeNull()
})
it('anchors older activity when a new update arrives',async()=>{
 await render(rows(5));const list=host.querySelector('ol');let size=5
 list.getBoundingClientRect=()=>({top:0})
 Object.defineProperty(list,'clientHeight',{value:120});Object.defineProperty(list,'scrollHeight',{get:()=>size*60})
 for(const row of list.children)row.getBoundingClientRect=()=>{const i=[...list.children].indexOf(row);return{top:i*60-list.scrollTop,bottom:(i+1)*60-list.scrollTop}}
 list.scrollTop=120;await act(()=>list.dispatchEvent(new Event('scroll',{bubbles:true})))
 size=6;await render(rows(6));expect(list.scrollTop).toBe(180)
 await act(()=>[...host.querySelectorAll('button')].find(b=>b.textContent.includes('Latest')).click());expect(list.scrollTop).toBe(0)
})
it('retains merged history and deduplicates sequence IDs',()=>{
 let history=[];for(let id=1;id<=240;id++)history=addRemediationEvent(history,{kind:'remediate.document_completed',document:'a.docx'},id)
 expect(history).toHaveLength(240);expect(addRemediationEvent(history,{kind:'remediate.document_completed'},40)).toBe(history)
})
it('shows the recorded AI model and zone without a verification claim',()=>{
 const event={kind:'remediate.ai_request_started',document:'a.docx',detail:{processing_zone:'local',provider:'ollama',model:'moondream:latest',prompt:'private'}}
 expect(remediationEventLine(event)).toBe('Local AI · moondream:latest (ollama) · Request sent for a.docx')
 expect(remediationEventLine({...event,kind:'remediate.ai_request_finished',detail:{processing_zone:'cloud',model:'gpt-4.1',status:'failed'}})).toContain('Cloud AI · gpt-4.1 · Request failed')
 expect(remediationEventLine({...event,detail:{}})).toContain('Model not recorded')
})
it('offers compact optional filters without changing recorded history totals',async()=>{
 const events=[{...rows(1)[0],key:'local',line:'Local AI caption for report.pptx',kind:'remediate.ai_request_started',processingZone:'local',tone:'neutral'}, {...rows(1)[0],key:'cloud',line:'Cloud AI caption for other.pptx',kind:'remediate.ai_request_finished',processingZone:'cloud',tone:'attention'}]
 await render(events);expect(host.querySelector('select')).toBeNull()
 await act(()=>[...host.querySelectorAll('button')].find(b=>b.textContent.startsWith('Filter')).click())
 const location=[...host.querySelectorAll('select')][1]
 await act(()=>{location.value='local';location.dispatchEvent(new Event('change',{bubbles:true}))})
 expect(host.querySelector('ol').children).toHaveLength(1);expect(host.textContent).toContain('1 of 2 recorded updates')
 await render([...events,{...events[1],key:'cloud2'}]);expect(host.textContent).toContain('1 of 3 recorded updates')
 await act(()=>[...host.querySelectorAll('button')].find(b=>b.textContent==='Clear filters').click())
 expect(host.querySelector('ol').children).toHaveLength(3)
})
it('uses an image frame for recovered descriptions without a verified checkmark',async()=>{
 await render([{...rows(1)[0],kind:'remediate.vision_retry_recovered',tone:'neutral'}])
 expect(host.querySelector('[data-activity-icon="image-description"]')).not.toBeNull()
 expect(host.querySelector('.remops-activity-event>span').textContent).not.toContain('✓')
})
it('keeps a cancelled obsolete retry in history, neutral, and out of the needs-attention filter',async()=>{
 const obsolete=addRemediationEvent([],{kind:'remediate.vision_retry_obsolete',document:'a.docx',document_ref:'ref-a',activity_stage:'draft_generation',detail:{retry:2,reason_code:'vision_retry_input_changed',no_ai_request:true}},9)[0]
 const paused=addRemediationEvent([],{kind:'remediate.vision_retry_blocked',document:'b.docx',document_ref:'ref-b',detail:{reason_code:'vision_budget_exhausted'}},8)[0]
 await render([obsolete,paused])
 const [first,second]=host.querySelector('ol').children
 expect(first.className).toContain('remops-activity-neutral');expect(first.textContent).toContain('No AI request was made.')
 expect(first.dataset.activityStage).toBe('draft_generation');expect(first.textContent).not.toContain('!')
 expect(second.className).toContain('remops-activity-attention');expect(second.textContent).toContain('paused')
 await act(()=>[...host.querySelectorAll('button')].find(b=>b.textContent.startsWith('Filter')).click())
 const outcome=[...host.querySelectorAll('select')][0]
 await act(()=>{outcome.value='attention';outcome.dispatchEvent(new Event('change',{bubbles:true}))})
 expect(host.querySelector('ol').children).toHaveLength(1);expect(host.querySelector('ol').textContent).toContain('paused')
})
it('mounts criterion and exact-version evidence details for a durable event binding',async()=>{
 const sha='a'.repeat(64)
 const event=addRemediationEvent([],{kind:'remediate.verified',document:'report.docx',detail:{fixes:1,evidence_id:'123456abcdef',artifact_sha256:sha,criteria:['1.3.1']}},8)[0]
 await render([event]);expect(host.textContent).toContain('SC 1.3.1');expect(host.textContent).toContain('Location Unavailable')
 expect(host.querySelector('.activity-event-details details summary').textContent).toBe('View Changes and Evidence')
})
