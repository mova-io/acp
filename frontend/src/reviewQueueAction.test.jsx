import { describe, it, expect, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react'
import { createTestRoot, unmountAll } from './testRoots.js'
import RemediationInbox from './RemediationInbox.jsx'
import { reviewQueueAction, reviewQueueActions } from './reviewQueueAction.js'
import { automaticReviewQueue } from './automaticReviewQueue.js'

afterEach(unmountAll)
describe('Review queue action pills', () => {
  it('distinguishes scope-bound PDF map editing from native-tag approval without trusting stale reasons', () => {
    const policy={enabled:true,supported:true,run_id:'r',source_revision:1}
    const base={id:71,file:'document.pdf',rule_id:'1.3.1',status:'pending',hasProposal:true,after:'draft',
      _raw:{scan_id:'s',proposal_snapshot_ids:['p'],source_revision:1,decision_version:0},automatic_approval:{state:'blocked',responsibility:'human',
        scan_id:'s',proposal_snapshot_ids:['p'],run_id:'r',source_revision:1}}
    const mapReason='This PDF needs headings or table structure added in the original document. ACP cannot apply this draft automatically.'
    const nativeReason='This PDF structure draft was created from document rules and needs your review before ACP applies it.'
    const native={...base,proposals:[{kind:'pdf-tag-heading',locator:'pdf:struct:1',proposed_value:JSON.stringify({op:'heading',role:'H1'})}]}
    const projected=(reason,row=base)=>automaticReviewQueue([{...row,automatic_approval:{...row.automatic_approval,reason}}],policy)[0]
    expect(reviewQueueAction(projected(mapReason),{},true).label).toBe('Edit needed')
    expect(reviewQueueAction(projected(nativeReason,native),{},true).label).toBe('Review needed')
    const stale=automaticReviewQueue([{...native,automatic_approval:{...native.automatic_approval,reason:mapReason,run_id:'old'}}],policy)[0]
    expect(reviewQueueAction(stale,{},true).key).not.toBe('edit')
  })
  it('uses action ownership rather than severity and preserves uncertain automatic work', () => {
    expect(reviewQueueAction({ id:1, severity:'MODERATE' }).key).toBe('edit')
    expect(reviewQueueAction({ id:2, severity:'CRITICAL',hasProposal:true, after:'draft' }).key).toBe('review')
    expect(reviewQueueAction({ id:3,rule_id:'1.1.1',status:'blocked' },{},true).key).toBe('check')
    expect(reviewQueueAction({ id:4,automaticQueued:true },{},true).key).toBe('processing')
    expect(reviewQueueAction({ id:5,applied:true,validated:false },{},true).key).toBe('check')
    expect(reviewQueueAction({ id:6,rule_id:'2.4.2' },{},true).key).toBe('edit')
    expect(reviewQueueAction({ id:7,status:'blocked',hasProposal:true,after:'draft',automaticDisposition:{responsibility:'human',reason:'Manual work or no supported proposal writer'} },{},true).key).toBe('edit')
    expect(reviewQueueActions([{hasProposal:true,after:'draft'},{}]).map(a=>a.label)).toEqual(['Review needed','Edit needed'])
  })
  it('renders admitted automatic work and unknown verification as blue status pills', async () => {
    const {container,root}=createTestRoot()
    await act(async()=>root.render(createElement(RemediationInbox,{queue:[
      {id:81,file:'queued.docx',scanId:'s',rule_id:'1.1.1',hasProposal:true,after:'draft',aiAssisted:true,_raw:{scan_id:'s',source_revision:1,decision_version:0,proposal_snapshot_ids:['p']},proposal_snapshot_ids:['p'],automatic_approval:{state:'queued',run_id:'r',scan_id:'s',source_revision:1,proposal_snapshot_ids:['p'],responsibility:'acp'}},
      {id:82,file:'checking.docx',rule_id:'1.1.1',applied:true,validated:false},
    ],autoApprove:true,automaticApprovalPolicy:{enabled:true,supported:true,run_id:'r',source_revision:1},initialTab:'all',initialGroup:'document',decisions:{}})))
    expect(container.querySelector('#rinbox-row-81 .rinbox-action-chip--processing')?.textContent).toBe('Processing')
    expect(container.querySelector('#rinbox-row-82 .rinbox-action-chip--check')?.textContent).toBe('Status check')
    expect(container.querySelector('.rinbox-row .rinbox-action-chip--edit')).toBeNull()
  })
  it('renders manual red and review amber without severity pills, while retaining priority filters', async () => {
    const {container,root}=createTestRoot()
    await act(async()=>root.render(createElement(RemediationInbox,{ queue:[
      {id:91,file:'manual.pdf',rule_id:'2.4.2',severity:'MODERATE'},
      {id:92,file:'draft.docx',rule_id:'1.1.1',severity:'CRITICAL',hasProposal:true,after:'draft'},
    ],initialTab:'all',initialGroup:'document',decisions:{} })))
    expect(container.querySelector('#rinbox-row-91 .rinbox-action-chip--edit')?.textContent).toBe('Edit needed')
    expect(container.querySelector('#rinbox-row-92 .rinbox-action-chip--review')?.textContent).toBe('Review needed')
    expect(container.querySelector('.rinbox-row .revcard-sev')).toBeNull()
    const priority=container.querySelector('select[aria-label="Filter by priority"]')
    expect([...priority.options].some(o=>o.value==='critical')).toBe(true)
    await act(async()=>{priority.value='critical';priority.dispatchEvent(new Event('change',{bubbles:true}))})
    expect(container.querySelector('#rinbox-row-91')).toBeNull()
    expect(container.querySelector('#rinbox-row-92')).not.toBeNull()
  })
})
