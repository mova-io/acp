import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import RemainingWorkStatus from './RemainingWorkStatus.jsx'
import { remainingWorkStatus } from './remainingWorkStatus.js'
import { addRemediationEvent, visionSettles } from './remediationEventFeed.js'
const event = (id, kind, reasonCode) => ({id:String(id),key:String(id),kind,documentKey:'private-ref',reasonCode})
describe('remaining work responsibility', () => {
  it('describes legacy warnings using frozen local policy without asserting an endpoint diagnosis', () => {
    const legacy = event(1, 'remediate.vision_retry_blocked', 'vision_permission_or_budget_blocked')
    legacy.documentName = 'report.docx'
    const notices = remainingWorkStatus({ events: [legacy], snapshot: { ai_policy: { zone: 'local' } },
      rows: [{ id: 1, file: 'report.docx', rule_id: '1.1.1', aiDraftable: true, status: 'pending', hasProposal: false }] }).notices
    expect(notices[0].label).toBe('Local AI setup needs attention')
    expect(notices[0].responsibility).toContain('does not identify the exact local failure')
    expect(notices[0].responsibility).toContain('Ask an administrator')
    expect(notices[0].responsibility).toContain('before starting a new remediation plan')
    expect(notices[0].responsibility).not.toContain('retry the failed generation')
    expect(notices[0].label).not.toMatch(/spending|endpoint unavailable/i)
    expect(legacy.reasonCode).toBe('vision_permission_or_budget_blocked')
    expect(notices.find(n => n.key === 'blocked-ai').responsibility).not.toMatch(/pricing|spending/)
    expect(remainingWorkStatus({ events: [legacy], snapshot: { ai_policy: { zone: 'any' } } }).notices[0].label).toBe('AI permission or spending limit needs attention')
    expect(remainingWorkStatus({ events: [legacy] }).notices[0].label).toBe('AI permission or spending limit needs attention')
  })
  it('reports rejected generated output without a false spending or permission blocker', () => {
    const [row] = addRemediationEvent([], {kind:'remediate.vision_retry_blocked', document:'report.docx', document_ref:'private-ref', detail:{reason_code:'vision_generated_output_unusable', output:'private provider text'}}, 7)
    expect(row.reasonCode).toBe('vision_generated_output_unusable')
    expect(row.line).toContain('automatic generation attempts stopped')
    expect(JSON.stringify(row)).not.toContain('private provider text')
    const result = remainingWorkStatus({events:[row], rows:[{id:1,file:'report.docx',rule_id:'1.1.1',aiDraftable:true,status:'pending',hasProposal:false}]})
    expect(result.notices[0].label).toBe('AI response could not be used')
    expect(result.notices[0].responsibility).not.toMatch(/permission|spending limit/)
    expect(result.counts['blocked-ai']).toBe(0)
    expect(result.counts['missing-proposals']).toBe(1)
  })
  it('separates missing drafts, failed checks and genuine decisions instead of merging them as human review', () => {
    const result=remainingWorkStatus({rows:[
      {id:1,file:'a.docx',rule_id:'1.1.1',aiDraftable:true,status:'pending',hasProposal:false},
      {id:2,file:'b.pdf',rule_id:'2.4.2',status:'verification_failed',hasProposal:true,after:'Title'},
      {id:3,file:'c.docx',rule_id:'1.3.3',status:'pending',hasProposal:true,after:'Use button A'},
    ]})
    expect(result.notices.find(n=>n.key==='missing-proposals')?.count).toBe(1)
    expect(result.notices.find(n=>n.key==='failed-checks')?.count).toBe(1)
    expect(result.notices.find(n=>n.key==='review')?.count).toBe(1)
  })
  it('replay order cannot resurrect recovered waits', () => {
    const run = e => ({...e, runId:'run-1'})
    const complete = {...run(event(9,'remediate.vision_retry_recovered')), coverageComplete:true}
    expect(remainingWorkStatus({events:[run(event(4,'remediate.vision_retry_pending')),complete,run(event(7,'remediate.vision_retry_blocked','vision_spending_reconciliation_required'))]}).notices).toEqual([])
    // A recovery that does not PROVE complete coverage (historical, or missing 0 alone) settles no block.
    expect(remainingWorkStatus({events:[run(event(4,'remediate.vision_retry_pending')),run(event(9,'remediate.vision_retry_recovered')),run(event(7,'remediate.vision_retry_blocked','vision_spending_reconciliation_required'))]}).notices).toHaveLength(1)
    // Without a run binding nothing proves the recovered record belongs to the same retry chain.
    expect(remainingWorkStatus({events:[event(9,'remediate.vision_retry_recovered'),event(7,'remediate.vision_retry_blocked','vision_spending_reconciliation_required')]}).notices).toHaveLength(1)
  })
  it('clears a current image notice only with a LATER event bound to the same run and item', () => {
    const row = (id, kind, detail = {}, extra = {}) => addRemediationEvent([], {kind, document_ref:'private-ref', correlation_id:'run-1', detail, ...extra}, id)[0]
    const pending = row(7, 'remediate.vision_retry_pending', {retry:2, item_id:'item-1'})
    expect(pending.runId).toBe('run-1'); expect(pending.itemId).toBe('item-1')
    expect(remainingWorkStatus({events:[pending]}).notices[0].label).toBe('AI retry queued')
    for (const later of [
      row(8, 'remediate.vision_retry_obsolete', {reason_code:'vision_retry_input_changed', no_ai_request:true, item_id:'item-1'}),
      row(8, 'remediate.review_target_replaced', {rule_id:'1.1.1', removed_by_rule_id:'1.4.5', finding_count:1, item_id:'item-1'}),
      row(8, 'remediate.review_target_replaced', {rule_id:'SC_1_1_1', removed_by_rule_id:'1.4.5', finding_count:1, item_id:'item-1'}),
      row(8, 'remediate.vision_retry_recovered', {drafts:1, awaiting_review:1, missing:0, item_id:'item-1'}),
    ]) {
      expect(remainingWorkStatus({events:[pending, later]}).notices).toEqual([])
      expect(remainingWorkStatus({events:[pending, {...later, id:'6', key:'6'}]}).notices[0].label).toBe('AI retry queued')
      expect(remainingWorkStatus({events:[pending, {...later, runId:'run-2'}]}).notices[0].label).toBe('AI retry queued')
      expect(remainingWorkStatus({events:[pending, {...later, runId:null}]}).notices[0].label).toBe('AI retry queued')
      expect(remainingWorkStatus({events:[pending, {...later, itemId:'item-2'}]}).notices[0].label).toBe('AI retry queued')
    }
    expect(remainingWorkStatus({events:[pending, row(8, 'remediate.review_target_replaced', {rule_id:'1.4.3', removed_by_rule_id:'1.4.5', item_id:'item-1'})]}).notices[0].label).toBe('AI retry queued')
    expect(remainingWorkStatus({events:[pending, row(8, 'remediate.vision_retry_obsolete', {reason_code:'vision_retry_run_inactive', item_id:'item-1'}, {document_ref:'other-ref'})]}).notices[0].label).toBe('AI retry queued')
    expect(row(8, 'remediate.review_target_replaced', {rule_id:'SC_1_1_1', removed_by_rule_id:'SC_1_4_5'}).line).toBe('WCAG 1.1.1 review for Document no longer applies · its target was removed by a verified WCAG 1.4.5 fix')
    expect(row(8, 'remediate.review_target_replaced', {rule_id:'<script>', removed_by_rule_id:'x'}).line).toBe('Review item for Document no longer applies · its target was removed by a verified fix')
    expect(row(8, 'remediate.vision_retry_pending', {item_id:'bad item <x>'}).itemId).toBeNull()
  })
  it('a genuine block (which records no item) is settled by its own later same-run recovery, never by another run', () => {
    const row = (id, kind, detail = {}, extra = {}) => addRemediationEvent([], {kind, document_ref:'private-ref', correlation_id:'run-1', detail, ...extra}, id)[0]
    const blocked = row(5, 'remediate.vision_retry_blocked', {reason_code:'vision_spending_reconciliation_required'})
    expect(blocked.itemId).toBeNull()
    const pending = row(6, 'remediate.vision_retry_pending', {retry:1, item_id:'item-1'})
    const recovered = row(7, 'remediate.vision_retry_recovered', {drafts:1, awaiting_review:0, uncertain:0, missing:0, item_id:'item-1'})
    expect(remainingWorkStatus({events:[blocked, pending, recovered]}).notices).toEqual([])
    // Directly after the block, only a recovery that records coverage_complete === true settles it.
    expect(remainingWorkStatus({events:[blocked, recovered]}).notices[0].label).toBe('AI usage confirmation needs attention')
    const proven = row(7, 'remediate.vision_retry_recovered', {drafts:1, awaiting_review:0, uncertain:0, missing:0, coverage_complete:true, item_id:'item-1'})
    expect(proven.coverageComplete).toBe(true)
    expect(remainingWorkStatus({events:[blocked, proven]}).notices).toEqual([])
    const unknown = row(7, 'remediate.vision_retry_recovered', {drafts:1, awaiting_review:0, uncertain:1, missing:null, coverage_complete:false, item_id:'item-1'})
    expect(remainingWorkStatus({events:[blocked, unknown]}).notices.map(n => n.label)).toEqual(['Image description coverage not confirmed', 'AI usage confirmation needs attention'])
    expect(remainingWorkStatus({events:[blocked, {...recovered, runId:'run-2'}]}).notices[0].label).toBe('AI usage confirmation needs attention')
    // A verified replacement cannot bind to a block that names no item: the notice stays.
    expect(remainingWorkStatus({events:[blocked, row(8, 'remediate.review_target_replaced', {rule_id:'1.1.1', removed_by_rule_id:'1.4.5', item_id:'item-1'})]}).notices[0].label).toBe('AI usage confirmation needs attention')
  })
  it('delivery settles no image notice (publication may deliver with issues remaining)', () => {
    expect(visionSettles({id:'1', documentKey:'doc', runId:'run', itemId:'a', kind:'remediate.vision_retry_blocked'},
      {id:'2', documentKey:'doc', runId:'run', kind:'remediate.delivered'})).toBe(false)
    const blocked = {...event(1, 'remediate.vision_retry_blocked', 'vision_budget_exhausted'), runId:'run'}
    expect(remainingWorkStatus({events:[blocked, {...event(2, 'remediate.delivered'), runId:'run'}]}).notices[0].label).toBe('AI spending allowance exhausted')
  })
  it('an obsolete retry, in either wire form, is never a current notice', () => {
    const projected = addRemediationEvent([], {kind:'remediate.vision_retry_blocked', document_ref:'private-ref', correlation_id:'run-1', detail:{reason_code:'vision_retry_input_changed', recorded_reason_code:'vision_recovery_unresolved', projection:'historical_obsolete_retry'}}, 3)[0]
    expect(projected.obsolete).toBe(true)
    expect(projected.reasonCode).toBe('vision_retry_input_changed')
    expect(remainingWorkStatus({events:[projected]}).notices).toEqual([])
    expect(remainingWorkStatus({events:[{...event(2,'remediate.vision_retry_pending'), runId:'run-1'}, projected]}).notices).toEqual([])
  })
  it('a recovered retry with images still missing keeps an actionable notice', () => {
    const recovered = addRemediationEvent([], {kind:'remediate.vision_retry_recovered', document_ref:'ref', detail:{drafts:1, awaiting_review:1, missing:1}}, 4)[0]
    const [notice] = remainingWorkStatus({events:[recovered]}).notices
    expect(notice.label).toBe('Image description still needed')
    expect(notice.responsibility).toContain('1 image in this document still needs a description')
    expect(notice.responsibility).toContain('provide the description in Review')
    expect(notice.presentation).toBe('action')
  })
  it('a finished document attempt does not supersede separately queued vision recovery', () => {
    expect(remainingWorkStatus({events:[event(1,'remediate.vision_retry_pending'),event(2,'remediate.document_completed')]}).notices[0].label).toBe('AI retry queued')
  })
  it('only allowlists safe reason codes and never retains raw detail', () => {
    const [row] = addRemediationEvent([], {kind:'remediate.vision_retry_blocked',document_ref:'private-ref',detail:{reason_code:'vision_spending_reconciliation_required',secret:'private prompt'}},1)
    expect(row.reasonCode).toBe('vision_spending_reconciliation_required')
    expect(JSON.stringify(row)).not.toContain('private prompt')
    const [unknown] = addRemediationEvent([], {kind:'remediate.vision_retry_blocked',detail:{reason_code:'private error'}},2)
    expect(unknown.reasonCode).toBeNull()
  })
  it.each([
    ['vision_provider_access_denied','AI provider access denied',/will not repeat/],
    ['vision_budget_admission_denied','AI request not admitted by budget',/will not send another paid request/],
    ['vision_budget_exhausted','AI spending allowance exhausted',/new approved plan/],
    ['vision_run_permission_unavailable','Saved run does not permit another AI request',/will not broaden/],
    ['vision_pricing_not_verified','AI model pricing not verified',/verified pricing/],
  ])('keeps %s distinct with its actual required action',(reason,label,action)=>{
    const [row]=addRemediationEvent([], {kind:'remediate.vision_retry_blocked',document_ref:'ref',detail:{reason_code:reason,raw:'private'}},1)
    const notice=remainingWorkStatus({events:[row]}).notices[0]
    expect(row.reasonCode).toBe(reason)
    expect(notice.label).toBe(label)
    expect(notice.responsibility).toMatch(action)
    expect(JSON.stringify({row,notice})).not.toContain('private')
  })
  it('explains waiting separately from stopped automatic attempts', () => {
    const waiting = remainingWorkStatus({events:[{...event(1,'remediate.vision_retry_blocked','vision_spending_reconciliation_required'),occurredAt:'2026-09-13T20:00:00Z'}],snapshot:{generated_at:'2026-09-13T20:05:00Z'}}).notices[0]
    expect(waiting.label).toBe('AI usage confirmation pending')
    expect(waiting.responsibility).not.toContain('will settle')
    expect(remainingWorkStatus({events:[event(1,'remediate.vision_retry_blocked')]}).notices[0].label).toBe('Your review needed')
  })
  it('admitted automatic work does not become human review', () => {
    expect(remainingWorkStatus({rows:[{id:1,automaticQueued:true,status:'pending',hasProposal:true,after:'A caption',proposals:[{proposed_value:'A caption',source:'AI',model:'vision',model_call_id:'call'}]}]}).notices).toEqual([])
  })
  it('manual handoff is clearly owed to a person', () => {
    const notices=remainingWorkStatus({rows:[{id:2,status:'pending'}],decisions:{2:{state:'assigned'}}}).notices
    expect(notices[0].label).toBe('Manual document edit needed')
    expect(notices[0].responsibility).toContain('do not drain through AI automatically')
  })
  it('recent spending evidence separates its matching row from human review, and bounded checks never promise forever', () => {
    const e={...event(1,'remediate.vision_retry_blocked','vision_spending_reconciliation_required'),documentName:'a.docx',occurredAt:'2026-09-13T20:00:00Z'}
    const rows=[{id:1,file:'a.docx',rule_id:'1.1.1',status:'pending',hasProposal:false}]
    const recent=remainingWorkStatus({events:[e],rows,snapshot:{generated_at:'2026-09-13T20:05:00Z'}})
    expect(recent.notices.some(n=>n.key==='review')).toBe(false)
    const old=remainingWorkStatus({events:[e],rows,snapshot:{generated_at:'2026-09-13T20:45:00Z'}})
    expect(old.notices[0].label).toBe('AI usage confirmation needs attention')
    expect(old.notices.find(n=>n.key==='blocked-ai')?.count).toBe(1)
  })
  it('a spending pause never hides manual crop or sensory review in the same document', () => {
    const e={...event(1,'remediate.vision_retry_blocked','vision_spending_reconciliation_required'),documentName:'a.docx',occurredAt:'2026-09-13T20:00:00Z'}
    const rows=[{id:1,file:'a.docx',rule_id:'1.1.1',status:'pending',hasProposal:false},
      {id:2,file:'a.docx',rule_id:'1.4.5',status:'pending'},
      {id:3,file:'a.docx',rule_id:'1.3.3',status:'pending',hasProposal:true,after:'Use control A'}]
    const notices=remainingWorkStatus({events:[e],rows,snapshot:{generated_at:'2026-09-13T20:05:00Z'}}).notices
    expect(notices.find(n=>n.key==='manual').responsibility).toContain('1 review item')
    expect(notices.find(n=>n.key==='review').responsibility).toContain('1 review item')
  })
  it('unknown progress is omitted and a stall never promises recovery', () => {
    expect(remainingWorkStatus({snapshot:{progress:{material_age_s:null}}}).checkpoint).toBeNull()
    const html=renderToStaticMarkup(<RemainingWorkStatus snapshot={{state:'stalled',progress:{material_age_s:125}}} />)
    expect(html).toContain('Last saved progress 2m 5s ago')
    expect(html).toContain('role="alert"')
    expect(html).toContain('automatic recovery is not yet confirmed')
  })
})


it('consolidates repeated operational warnings without combining review counts',()=>{
 const result=remainingWorkStatus({events:[{id:'1',key:'1',documentKey:'a',kind:'remediate.vision_retry_blocked',reasonCode:'vision_permission_or_budget_blocked'},{id:'2',key:'2',documentKey:'b',kind:'remediate.vision_retry_blocked',reasonCode:'vision_permission_or_budget_blocked'}]})
 expect(result.notices).toHaveLength(1)
 expect(result.notices[0].responsibility).toContain('2 documents affected')
})
it('shows local endpoint failures without a spending warning',()=>{
 const [row]=addRemediationEvent([], {kind:'remediate.vision_retry_blocked',document_ref:'a',detail:{reason_code:'vision_local_endpoint_required'}},1)
 const result=remainingWorkStatus({events:[row]})
 expect(result.notices[0].label).toBe('Local AI endpoint unavailable')
 expect(result.notices[0].responsibility).toContain('Increasing the budget alone will not fix')
 expect(result.notices.some(n=>n.label.includes('spending limit'))).toBe(false)
})

it('retains the safe empty-response reason and shows a concrete next action', () => {
  const [row] = addRemediationEvent([], {kind:'remediate.vision_retry_blocked',document:'A.pptx',document_ref:'ref',detail:{reason_code:'vision_response_empty',response:'private text'}}, 8)
  expect(row.reasonCode).toBe('vision_response_empty')
  const notices = remainingWorkStatus({events:[row],rows:[]}).notices
  expect(notices[0].label).toBe('AI returned no image description')
  expect(notices[0].responsibility).toContain('Provide the missing description')
  expect(JSON.stringify(notices)).not.toContain('private text')
})

it('keeps blockers visible while collapsing routine preparation and status groups', () => {
  const html = renderToStaticMarkup(<RemainingWorkStatus automatic rows={[
    {id:1,file:'draft.docx',rule_id:'1.1.1',status:'pending',aiDraftable:true,hasProposal:false},
    {id:2,file:'failed.docx',rule_id:'2.4.2',status:'verification_failed',hasProposal:true,after:'Title'},
    {id:3,file:'status-a.docx',rule_id:'1.3.3',status:'pending',hasProposal:true,after:'Use button'},
    {id:4,file:'status-b.docx',rule_id:'1.3.3',status:'pending'},
  ]} />)
  expect(html).toContain('Saved fix needs recovery')
  expect(html).toContain('Status and recovery details')
  expect(html).toContain('Recorded status needs checking')
  expect(html).toContain('separate recorded groups')
  expect(html).toContain('A recorded status is not a verified fix')
  expect(html).not.toContain('<details class="remaining-work-details" open=""')
})
