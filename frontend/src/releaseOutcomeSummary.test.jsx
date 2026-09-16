import { describe, expect, it } from 'vitest'
import { act, createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createRoot } from 'react-dom/client'
import { readFileSync } from 'node:fs'
import ReleaseOutcomeSummary from './ReleaseOutcomeSummary.jsx'
import { releaseOutcomeSummary, savedDestinationLinks } from './releaseOutcomeSummary.js'

const sha = 'a'.repeat(64)
const files = [{ file: 'one.docx', corrected_sha256: sha }, { file: 'two.pdf', corrected_sha256: sha }]
const rec = { exact:true, assessed:10, resolved_verified:4, awaiting_review:2, approved_pending_verification:1,
  unchanged_no_fix:1, failed:0, excluded:1, superseded:1,
  original_assessment:[{file:'one.docx',rule_id:'1.1.1',fix_mode:'assisted',finding_count:5},{file:'two.pdf',rule_id:'1.1.1',fix_mode:'assisted',finding_count:5}] }
function evidence(delivered = 2, status = 'completed') {
  return { scanId:'scan', files, snapshot:{scan_id:'scan', batch_id:'execution', total_documents:2, finding_reconciliation:rec},
    authorization:{ id:'approval', run_id:'execution', files:files.map(f => f.file),
      batch_progress:{ available:true, authorization_id:'approval', scope_id:'approval', run_id:'execution', revision:2,
        total:2, delivered, remaining:2-delivered, status,
        buckets:{published:delivered, processing:2-delivered},
        file_membership:{'one.docx':'published', 'two.pdf':delivered === 2 ? 'published' : 'processing'} } },
    results:Object.fromEntries(files.slice(0,delivered).map(f => [f.file,{status:'published',artifact_digest:`sha256:${sha}`}])) }
}
describe('Release outcome evidence', () => {
  it('summarizes the whole saved authorization with exact findings and current-copy receipts', () => {
    expect(releaseOutcomeSummary(evidence())).toMatchObject({complete:true,total:2,delivered:2,fixed:4,open:6,excluded:1,superseded:1})
  })
  it('keeps partial delivery in progress and never borrows a completed one-file request', () => {
    expect(releaseOutcomeSummary({...evidence(1,'publishing'), latestRequest:{status:'completed',total:1}}))
      .toMatchObject({complete:false,state:'processing',total:2,delivered:1,remainingCopies:1})
  })
  it('requires final authorization settlement after all uploads', () => {
    expect(releaseOutcomeSummary(evidence(2,'publishing'))).toMatchObject({complete:false,state:'processing'})
  })
  it('rejects stale or absent current-artifact receipts even when saved status says complete', () => {
    const stale=evidence();stale.results['two.pdf'].artifact_digest=`sha256:${'b'.repeat(64)}`
    expect(releaseOutcomeSummary(stale)).toMatchObject({complete:false,delivered:null,state:'unavailable'})
    expect(releaseOutcomeSummary({...evidence(),results:{}}).complete).toBe(false)
  })
  it('rejects malformed membership, authorization identity, missing files, and unknown status reads', () => {
    const malformed=evidence();malformed.authorization.batch_progress.file_membership['two.pdf']='absent'
    expect(releaseOutcomeSummary(malformed).total).toBe(null)
    const other=evidence();other.authorization.batch_progress.run_id='previous'
    expect(releaseOutcomeSummary(other).complete).toBe(false)
    expect(releaseOutcomeSummary({...evidence(), files:files.slice(0,1)}).complete).toBe(false)
    expect(releaseOutcomeSummary({...evidence(),pending:true}).delivered).toBe(null)
    expect(releaseOutcomeSummary({...evidence(),error:'offline'}).complete).toBe(false)
  })
  it('shows unavailable rather than inventing zero findings from approval or compliance', () => {
    expect(releaseOutcomeSummary({...evidence(),snapshot:null})).toMatchObject({fixed:null,open:null})
    const wrong=evidence();wrong.snapshot.batch_id='previous'
    expect(releaseOutcomeSummary(wrong).fixed).toBe(null)
    const wrongScan=evidence();wrongScan.snapshot.scan_id='other'
    expect(releaseOutcomeSummary(wrongScan).open).toBe(null)
    const inconsistent=evidence();inconsistent.snapshot.finding_reconciliation={...rec,failed:9}
    expect(releaseOutcomeSummary(inconsistent).fixed).toBe(null)
    const otherScope=evidence();otherScope.snapshot.finding_reconciliation={...rec,original_assessment:[{file:'other.docx',rule_id:'1.1.1',fix_mode:'assisted',finding_count:10}]}
    expect(releaseOutcomeSummary(otherScope).open).toBe(null)
  })
  it('keeps exact remediation findings visible when a blocked release has no complete delivery aggregate', () => {
    const blocked=evidence(0,'blocked')
    blocked.authorization={...blocked.authorization,status:'blocked',requires_reconnect:true,
      batch_progress:{available:false,scope:'automatic'}}
    expect(releaseOutcomeSummary(blocked)).toMatchObject({state:'attention',title:'Delivery needs attention',
      delivered:null,fixed:4,open:6,deliveryReason:'Saved delivery evidence is incomplete for this authorization.'})
  })
  it('keeps findings and delivery as separate evidence during loading and errors', () => {
    expect(releaseOutcomeSummary({...evidence(),pending:true})).toMatchObject({fixed:4,open:6,delivered:null,
      deliveryReason:'Automatic publication evidence is still loading.'})
    expect(releaseOutcomeSummary({...evidence(),error:'offline'})).toMatchObject({fixed:4,open:6,delivered:null,
      deliveryReason:'Automatic publication evidence could not be loaded.'})
  })
  it('matches the Remediate finding equation for a blocked 4,771-finding release', () => {
    const blocked=evidence(0,'blocked')
    blocked.authorization.status='blocked'
    blocked.authorization.requires_reconnect=true
    blocked.authorization.batch_progress={available:false,scope:'automatic'}
    blocked.snapshot.finding_reconciliation={...rec,assessed:4771,resolved_verified:2002,
      awaiting_review:0,approved_pending_verification:0,unchanged_no_fix:0,failed:0,
      excluded:2769,superseded:0,
      original_assessment:[{file:'one.docx',rule_id:'1.1.1',fix_mode:'assisted',finding_count:2400},
        {file:'two.pdf',rule_id:'1.1.1',fix_mode:'assisted',finding_count:2371}]}
    expect(releaseOutcomeSummary(blocked)).toMatchObject({fixed:2002,open:2769,delivered:null,state:'attention'})
  })
  it('renders a confirmed delivery count of zero instead of treating it as unavailable', () => {
    const none=evidence(0,'blocked')
    none.authorization.status='blocked'
    none.authorization.batch_progress.file_membership['one.docx']='processing'
    expect(releaseOutcomeSummary(none)).toMatchObject({state:'attention',total:2,delivered:0,remainingCopies:2})
    const html=renderToStaticMarkup(createElement(ReleaseOutcomeSummary,none))
    expect(html).toContain('Copies delivered</dt><dd>0<small> of 2 authorized copies')
    expect(html).not.toContain('Copies delivered</dt><dd>Unavailable')
  })
  it('does not borrow findings from a different remediation run or release scope', () => {
    const run=evidence();run.snapshot.batch_id='other-run'
    expect(releaseOutcomeSummary(run)).toMatchObject({fixed:null,open:null})
    const scope=evidence();scope.authorization.files=['one.docx']
    expect(releaseOutcomeSummary(scope)).toMatchObject({fixed:null,open:null,delivered:null,
      deliveryReason:'The saved authorization scope does not match this Release view.'})
  })
  it('explains unavailable delivery evidence and offers an explicit retry', () => {
    const retry=()=>{}
    const html=renderToStaticMarkup(createElement(ReleaseOutcomeSummary,{...evidence(),pending:true,onRetry:retry}))
    expect(html).toContain('Automatic publication evidence is still loading.')
    expect(html).toContain('Refresh release evidence')
  })
  it('delivery complete is explicit about remaining open findings and does not claim conformance', () => {
    const html=renderToStaticMarkup(createElement(ReleaseOutcomeSummary,{...evidence(),folders:[{name:'Saved folder',url:'https://example.sharepoint.com/saved'}]}))
    expect(html).toContain('Delivery complete');expect(html).toContain('Findings still open')
    expect(html).toContain('Remaining findings stay in the follow-up checklist')
    expect(html).toContain('Delivery does not certify accessibility')
    expect(html).toContain('href="https://example.sharepoint.com/saved"')
  })
  it('keeps unavailable findings visually explicit without delivery permission', () => {
    const html=renderToStaticMarkup(createElement(ReleaseOutcomeSummary,{scanId:'scan',files}))
    expect(html).toContain('Unavailable');expect(html).not.toContain('Delivery complete')
  })
  it('only renders saved secure links, drops credentials and deduplicates destinations', () => {
    expect(savedDestinationLinks([{url:'javascript:alert(1)'},{url:'http://example.com'},{url:'https://secret@example.com'},
      {url:'https://example.com/folder',name:'Saved'}, {url:'https://example.com/folder'}])).toEqual([{url:'https://example.com/folder',name:'Saved'}])
  })
  it('updates the actual DOM from partial to confirmed delivery and retracts completion for changed corrected bytes', async () => {
    const host=document.createElement('div');document.body.append(host);const root=createRoot(host)
    try {
      await act(async () => root.render(createElement(ReleaseOutcomeSummary,evidence(1,'publishing'))))
      expect(host.textContent).toContain('Delivery in progress');expect(host.textContent).toContain('1 authorized copy still awaits')
      await act(async () => root.render(createElement(ReleaseOutcomeSummary,evidence())))
      expect(host.textContent).toContain('Delivery complete');expect(host.querySelector('dt')?.textContent).toContain('findings')
      const changed=evidence();changed.files=files.map(file => ({...file,corrected_sha256:'b'.repeat(64)}))
      await act(async () => root.render(createElement(ReleaseOutcomeSummary,changed)))
      expect(host.textContent).not.toContain('Delivery complete');expect(host.textContent).toContain('Checking delivery confirmation')
    } finally {await act(async () => root.unmount());host.remove()}
  })
  it('is wired with the current remediation snapshot at both workflow entry points', () => {
    const app=readFileSync('src/App.jsx','utf8')
    expect(app.match(/<Publish[^>]+remediationSnapshot=\{remRun\?\.snapshot\}/g)).toHaveLength(2)
    const publish=readFileSync('src/Publish.jsx','utf8')
    expect(publish).toContain('<ReleaseOutcomeSummary scanId={run?.id} authorization={automaticCoveredFiles.length ? currentAutomaticAuthorization : null}')
    expect(publish).toContain('snapshot={remediationSnapshot}')
    expect(publish).toContain('onRetry={() => setAutomaticStatusRefresh(value => value + 1)}')
  })
})
