import { describe, it, expect, vi, afterEach } from 'vitest'
import { createElement } from 'react'
import { getAutomaticRelease, resumeAutomaticRelease, getReleaseReports, downloadReleaseReport } from './api.js'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import { noteAuthChange, _resetAuthEpoch } from './apiIdentity.js'

// W5 — Publish surfaces the set-level certification status and offers a one-action graduation
// from conditional → full without a re-scan. Verified at the DOM level (per the repo's rule that
// worktree changes are proven in vitest, not the shared-checkout preview server).

const publishAllFiles = vi.fn(() => Promise.resolve({ published: [] }))
const listHitlQueue = vi.fn(() => Promise.resolve([]))
const getReleaseStatus = vi.fn(() => Promise.resolve({ release_id: null }))
const getSourceStatus = vi.fn(() => Promise.resolve({ files: [], stale_count: 0 }))
const previewReleaseDestination = vi.fn(() => Promise.resolve({ can_release: true, folder_name: 'Delivery', documents: [] }))
const getSettings = vi.fn(() => Promise.resolve({ drive_mirror_enabled: false, drive_mirror_folder: 'Remediated' }))
const putMyReleaseTemplates = vi.fn((templates) => Promise.resolve({ release_templates: templates }))
vi.mock('./api.js', () => ({
  resumeAutomaticRelease: vi.fn().mockResolvedValue({}), setDriveToken: vi.fn(),
  getAutomaticRelease: vi.fn().mockResolvedValue({authorization:null}),
  getReleaseReports: vi.fn().mockResolvedValue({status:'not_started',reports:[]}), retryReleaseReports: vi.fn(), downloadReleaseReport: vi.fn(),
  getReleaseAiProvenance: vi.fn(() => Promise.resolve({ calls: [] })),
  openReport: vi.fn(), publishFile: vi.fn(() => Promise.resolve({})),
  publishAllFiles: (...a) => publishAllFiles(...a),
  getReleaseStatus: (...args) => getReleaseStatus(...args),
  listReleaseHistory: vi.fn(() => Promise.resolve({ releases: [] })),
  getReleaseManifest: vi.fn(() => Promise.resolve({ manifest: {} })),
  listHitlQueue: (...args) => listHitlQueue(...args),
  getSettings: (...a) => getSettings(...a),
  putMyReleaseTemplates: (...a) => putMyReleaseTemplates(...a),
  getSourceStatus: (...args) => getSourceStatus(...args),
  rescoreFile: vi.fn(() => Promise.resolve({})),
  previewReleaseDestination: (...args) => previewReleaseDestination(...args),
  downloadReleasePackage: vi.fn(() => Promise.resolve()),
}))
// Keep the heavy children out of the mount — this test is about the Publish graduation surface.
vi.mock('./FileDrawer.jsx', () => ({ default: () => null }))
vi.mock('./ScopeBanner.jsx', () => ({ default: () => null }))
vi.mock('./SearchFilterBar.jsx', () => ({
  default: () => null,
  useSearchFilter: () => ({ active: false, clear: () => {} }),
  matchesFilters: () => () => true,
}))
vi.mock('./remediableScope.js', () => ({
  documentSelection: () => ({}),
  documentScopeSentence: () => '',
  documentsInSelection: (files) => files || [],
}))

vi.mock('./driveAuth.js', () => ({reconnectDriveForRelease: vi.fn().mockResolvedValue('new-grant')}))

const { default: Publish } = await import('./Publish.jsx')

afterEach(async () => { await unmountAll(); vi.useRealTimers(); _resetAuthEpoch(); vi.clearAllMocks(); getAutomaticRelease.mockResolvedValue({authorization:null}); getReleaseReports.mockResolvedValue({status:'not_started',reports:[]}); getReleaseStatus.mockResolvedValue({ release_id: null }); getSourceStatus.mockResolvedValue({ files: [], stale_count: 0 }); publishAllFiles.mockResolvedValue({ published: [] }); listHitlQueue.mockResolvedValue([]) })
const flush = async () => { for (let k = 0; k < 5; k++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }
const mount = async (props) => {
  const { container, root } = createTestRoot()
  await act(async () => { root.render(createElement(Publish, props)) })
  await flush()
  container.rerender = async (next) => { await act(async () => root.render(createElement(Publish, next))); await flush() }
  return container
}
const verified = (file, over = {}) => ({ file, compliant: true, remediated_at: '2026-07-31T00:00:00Z', score: 100, department: 'D', sourceName: 'S', ...over })
const held = (file, over = {}) => ({ file, compliant: false, score: 40, issues: [{ wcag: 'SC_1_1_1' }], department: 'D', sourceName: 'S', ...over })
const run = { id: 'scan1', files: 3, certifiable: 2 }

it('mounts compact scan and matching per-file report downloads beside the default publication documents', async()=>{
 getReleaseStatus.mockResolvedValue({release_id:'receipt',documents:[{
   file:'a.pdf',status:'published',artifact_digest:'sha256:current',published_at:'2026-09-12T00:00:00Z',released_document_url:'https://example.test/copy'}]})
 getReleaseReports.mockResolvedValue({status:'completed',scan_id:'scan1',release_id:'receipt',bundle_id:'bundle',reports:[
   {name:'Scan summary.pdf',report_kind:'scan_summary',download_url:'/download/0'},
   {name:'a-checklist.pdf',report_kind:'checklist',file:'a.pdf',artifact_digest:'sha256:current',download_url:'/download/1'},
 ]})
 const c=await mount({run,files:[verified('a.pdf')]})
 const panel=c.querySelector('#release-panel-manage')
 expect(panel.hidden).toBe(false)
 const outcomes=panel.querySelector('[aria-label="Publication outcomes"]')
 expect(outcomes.textContent).not.toContain('Saved copies, verification, remaining work')
 expect(outcomes.textContent).not.toContain('Scan summary and per-file checklists')
 const summary=outcomes.querySelector('[aria-label="Release reports"]')
 expect(summary.textContent).toContain('Scan summary.pdf')
 const row=[...outcomes.querySelectorAll('tbody tr')].find(r=>r.textContent.includes('a.pdf'))
 expect(row.textContent).toContain('a-checklist.pdf')
 await click(summary.querySelector('button'))
 await click(row.querySelector('.release-file-reports button'))
 expect(downloadReleaseReport.mock.calls).toEqual([
   ['scan1','bundle',0,'Scan summary.pdf'],['scan1','bundle',1,'a-checklist.pdf']])
})


Element.prototype.scrollIntoView = vi.fn()
const button = (c, text) => [...c.querySelectorAll('button')].find((b) => b.textContent === text && !b.closest('[hidden]'))
const click = async (node) => { expect(node).toBeTruthy(); await act(async () => node.click()); await flush() }
const row = (c, name) => c.querySelector(`[aria-label="Select ${name}"]`).closest('.release-selection__row')
const review = async (c) => { await click(button(c, 'Choose delivery')); await click(button(c, 'Review release')); }

describe('Release clarity and execution boundaries', () => {
  it('shows unknown, uncorrected and review-blocked files beside ready copies', async () => {
    listHitlQueue.mockResolvedValue([{ file: 'review.pdf' }])
    const c = await mount({ run, files: [verified('ready.pdf'), { file: 'unknown.pdf' }, verified('uncorrected.pdf', { remediated_at: null }), held('review.pdf')] })
    expect(row(c, 'ready.pdf').textContent).toContain('awaiting Release')
    expect(row(c, 'unknown.pdf').textContent).toContain('Readiness unknown')
    expect(row(c, 'uncorrected.pdf').textContent).toContain('No verified corrected copy')
    expect(row(c, 'review.pdf').textContent).toContain('1 review items pending')
    for (const name of ['unknown.pdf', 'uncorrected.pdf', 'review.pdf']) expect(c.querySelector(`[aria-label="Select ${name}"]`).disabled).toBe(true)
    expect(publishAllFiles).not.toHaveBeenCalled()
  })

  it('does not call uploaded certification a delivery or hide an unknown file from the scope', async () => {
    const c = await mount({ run, files: [verified('a.pdf'), { file: 'unknown.pdf' }], certified: [{ file: 'a.pdf' }] })
    expect(c.querySelector('[aria-label="Delivery receipt"]')).toBeNull()
    expect(c.querySelector('[aria-label="Release complete"]')).toBeNull()
    expect(row(c, 'a.pdf').textContent).toContain('awaiting Release')
  })

  it('restores a durable partial receipt and retries only failed files through fresh review', async () => {
    getReleaseStatus.mockResolvedValue({ release_id: 'receipt-1', roots: [{ folder_id: 'd', folder_name: 'Delivery', folder_url: 'https://example.test/delivery' }], documents: [
      { file: 'a.pdf', status: 'published', published_at: '2026-08-02T00:00:00Z', released_document_url: 'https://example.test/a' },
      { file: 'b.pdf', status: 'failed', explanation: 'Destination permission denied' },
    ] })
    const c = await mount({ run, files: [verified('a.pdf'), verified('b.pdf')] })
    const receipt = c.querySelector('[aria-label="Delivery receipt"]')
    expect(receipt.textContent).toContain('Partial delivery receipt')
    expect(receipt.textContent).toContain('1 delivered · 1 failed')
    expect(receipt.textContent).toContain('receipt-1')
    expect(row(c, 'a.pdf').textContent).toContain('Delivered')
    expect(row(c, 'b.pdf').textContent).toContain('Destination permission denied')
    await click(button(c, 'Review and retry failed (1)'))
    expect(publishAllFiles).not.toHaveBeenCalled()
    expect(c.querySelector('#release-delivery-step')).toBeTruthy()
    await click(button(c, 'Review release'))
    expect(previewReleaseDestination.mock.calls.at(-1)[1]).toEqual(['b.pdf'])
    expect(c.querySelector('.release-plan__actions').textContent).toContain('1 selected · 1 awaiting Release')
    await click(button(c, 'Publish 1 copy'))
    expect(publishAllFiles).not.toHaveBeenCalled()
    publishAllFiles.mockResolvedValue({ release_id: 'receipt-1', published: [{ file: 'b.pdf', status: 'published', published_at: '2026-08-03T00:00:00Z' }] })
    await click(button(c, 'Publish 1'))
    expect(publishAllFiles.mock.calls.at(-1)[1]).toEqual(['b.pdf'])
  })

  it('keeps a newer corrected copy out of Delivered despite an old receipt', async () => {
    getReleaseStatus.mockResolvedValue({ release_id: 'old', documents: [{ file: 'a.pdf', status: 'published', published_at: '2026-08-01' }] })
    const c = await mount({ run, files: [verified('a.pdf', { remediated_at: '2026-08-02', published_at: '2026-08-01' })] })
    expect(row(c, 'a.pdf').textContent).toContain('awaiting Release')
    expect(c.querySelector('[aria-label="Release complete"]')).toBeNull()
    expect(c.querySelector('[aria-label="Delivery receipt"]').textContent).toContain('0 delivered')
  })

  it('never infers success from an empty publish response', async () => {
    const onPublish = vi.fn()
    const c = await mount({ run, files: [verified('a.pdf')], onPublish })
    await review(c); await click(button(c, 'Publish 1 copy')); await click(button(c, 'Publish 1'))
    expect(onPublish).not.toHaveBeenCalled()
    expect(row(c, 'a.pdf').textContent).toContain('awaiting Release')
    expect(c.textContent).toContain('Delivery has not been confirmed')
  })

  it('keeps hidden selections explicit and preserves clear selection after freshness refresh', async () => {
    getSourceStatus.mockResolvedValue({ files: [{ file: 'b.pdf', state: 'conflict' }], stale_count: 1 })
    const c = await mount({ run, files: [verified('a.pdf'), verified('b.pdf')] })
    const status = c.querySelectorAll('.release-selection__toolbar select')[1]
    await act(async () => { status.value = 'attention'; status.dispatchEvent(new Event('change', { bubbles: true })) })
    expect(c.textContent).toContain('1 outside these filters')
    expect(c.querySelector('[aria-label="Select b.pdf"]').disabled).toBe(true)
  })

  it('traps confirmation focus, supports Escape, and restores the invoking control', async () => {
    const c = await mount({ run, files: [verified('a.pdf')] })
    await review(c)
    const trigger = button(c, 'Publish 1 copy'); trigger.focus(); await click(trigger)
    await act(async () => new Promise((resolve) => setTimeout(resolve, 30)))
    const cancel = button(c, 'Cancel'); const submit = button(c, 'Publish 1')
    expect(document.activeElement).toBe(cancel)
    await act(async () => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true })))
    expect(document.activeElement).toBe(submit)
    await act(async () => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true })))
    expect(document.activeElement).toBe(cancel)
    await act(async () => window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    await act(async () => new Promise((resolve) => setTimeout(resolve, 30)))
    expect(c.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(publishAllFiles).not.toHaveBeenCalled()
  })

  it('opens file details with focus and returns it on Escape; honors reduced motion', async () => {
    window.matchMedia = vi.fn(() => ({ matches: true }))
    Element.prototype.scrollIntoView = vi.fn()
    const c = await mount({ run, files: [verified('a.pdf')] })
    const details = row(c, 'a.pdf').querySelector('.release-selection__details')
    await click(details)
    expect(document.activeElement).toBe(c.querySelector('aside'))
    await act(async () => document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })))
    expect(document.activeElement).toBe(details)
    expect(c.querySelector('aside').hidden).toBe(true)
    await click(button(c, 'More delivery options'))
    await act(async () => new Promise((resolve) => setTimeout(resolve, 30)))
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' })
  })

  it('retains removed summaries deliberately hidden and cannot mount the direct graduate control', async () => {
    const c = await mount({ run, files: [verified('a.pdf', { published_at: '2026-08-01' })] })
    expect(c.querySelector('[data-retired="release-accounting"]').hidden).toBe(true)
    expect(c.querySelector('[data-retired="release-plan-summary"]').hidden).toBe(true)
    expect(c.querySelector('[data-retired="release-audit-summary"]').hidden).toBe(true)
    const { readFileSync } = await import('node:fs')
    const source = readFileSync('src/Publish.jsx', 'utf8')
    expect(source).toContain('const graduate = async')
    expect(source).not.toMatch(/onClick=\{graduate\}/)
  })
})

describe('Release selection changes', () => {
  it('invalidates a preview when the selection changes and never submits the old plan', async () => {
    const c = await mount({ run, files: [verified('a.pdf'), verified('b.pdf')] })
    await review(c)
    expect(button(c, 'Publish 2 copies').disabled).toBe(false)
    await click(c.querySelector('[aria-label="Select b.pdf"]'))
    expect(button(c, 'Publish 1 copy').disabled).toBe(true)
    expect(publishAllFiles).not.toHaveBeenCalled()
  })
  it('preserves an explicit empty selection when eligible data refreshes', async () => {
    const c = await mount({ run, files: [verified('a.pdf')] })
    await click(c.querySelector('[aria-label="Select a.pdf"]'))
    await c.rerender({ run, files: [verified('a.pdf'), verified('b.pdf')] })
    expect(button(c, 'Choose delivery').disabled).toBe(true)
    expect(c.querySelector('[aria-label="Select b.pdf"]').checked).toBe(false)
  })
  it('clears the old scan receipt when navigating to another scan with the same file name', async () => {
    getReleaseStatus.mockResolvedValueOnce({ release_id: 'first', documents: [{ file: 'a.pdf', status: 'published', published_at: '2026-08-01' }] })
    const c = await mount({ run, files: [verified('a.pdf')] })
    expect(row(c, 'a.pdf').textContent).toContain('Delivered')
    await c.rerender({ run: { ...run, id: 'second' }, files: [verified('a.pdf')] })
    expect(row(c, 'a.pdf').textContent).toContain('awaiting Release')
    expect(c.querySelector('[aria-label="Delivery receipt"]')).toBeNull()
  })
  it('keeps history replay read-only even after preview', async () => {
    const c = await mount({ run, files: [verified('a.pdf')], readOnly: true })
    await review(c)
    expect(button(c, 'Publish 1 copy').disabled).toBe(true)
    expect(publishAllFiles).not.toHaveBeenCalled()
  })
})

describe('Exact corrected-copy changes', () => {
  it('refreshes open details from current file evidence instead of its earlier selected object', async () => {
    getReleaseStatus.mockResolvedValue({ release_id: 'versioned', documents: [{ file: 'a.pdf', status: 'published', artifact_digest: `sha256:${'a'.repeat(64)}`, published_at: '2026-08-01' }] })
    const c = await mount({ run, files: [verified('a.pdf', { corrected_sha256: 'a'.repeat(64) })] })
    await click(row(c, 'a.pdf').querySelector('.release-selection__details'))
    expect(c.querySelector('aside').textContent).toContain('Delivered')
    await c.rerender({ run, files: [verified('a.pdf', { corrected_sha256: 'b'.repeat(64) })] })
    expect(c.querySelector('aside').textContent).toContain('Ready')
    expect(c.querySelector('aside').textContent).not.toContain('Delivered')
  })
  it('invalidates delivery review when bytes change within the same timestamp', async () => {
    const c = await mount({ run, files: [verified('a.pdf', { corrected_sha256: 'a'.repeat(64) })] })
    await review(c)
    expect(button(c, 'Publish 1 copy').disabled).toBe(false)
    await c.rerender({ run, files: [verified('a.pdf', { corrected_sha256: 'b'.repeat(64) })] })
    expect(button(c, 'Publish 1 copy').disabled).toBe(true)
    expect(publishAllFiles).not.toHaveBeenCalled()
  })
})


it('ready-only action excludes recorded deliveries and binds exact corrected artifacts', async () => {
  getReleaseStatus.mockResolvedValue({ release_id: 'receipt', documents: [{ file: 'delivered.pdf', status: 'published', artifact_digest: 'sha256:done' }] })
  const c = await mount({ run, files: [verified('ready.pdf', { corrected_sha256: 'current' }), verified('delivered.pdf', { corrected_sha256: 'done' }), held('manual.pdf')] })
  expect(JSON.stringify([...c.querySelectorAll('button')].map(b => b.textContent))).toContain('Publish batch (1)')
  await click(button(c, 'Publish batch (1)'))
  expect(publishAllFiles).toHaveBeenCalledWith('scan1', ['ready.pdf'], '', {
    destination: null, expectedArtifacts: { 'ready.pdf': 'current' },
  })
})


it('mounts both top-level Release actions for a run with no ready copies', async () => {
  const c = await mount({ run, files: [held('review.pdf')] })
  const quick = c.querySelector('.release-quick')
  expect(quick).not.toBeNull()
  expect(quick.closest('details')).toBeNull()
  expect(quick.textContent).toContain('Verification incomplete')
  for (const name of ['Publish batch (0)', 'Approve eligible changes and publish when ready']) {
    expect(button(quick, name).disabled).toBe(true)
  }
  expect(publishAllFiles).not.toHaveBeenCalled()
})

it('publishes only the ready subset while the same run still has processing and review files', async () => {
  listHitlQueue.mockResolvedValue([{ file: 'review.pdf', status: 'pending' }])
  const c = await mount({ run: { ...run, status: 'running' }, files: [verified('ready.pdf', { corrected_sha256: 'exact-ready' }), held('processing.pdf', { status: 'running' }), held('review.pdf')] })
  expect(button(c, 'Publish batch (1)').disabled).toBe(false)
  await click(button(c, 'Publish batch (1)'))
  expect(publishAllFiles).toHaveBeenCalledWith('scan1', ['ready.pdf'], '', {
    destination: null, expectedArtifacts: { 'ready.pdf': 'exact-ready' },
  })
})

it('publishes a saved partial copy only after explicit opt-in without approving pending work', async () => {
  listHitlQueue.mockResolvedValue([{ id: 42, file: 'partial.docx', status: 'pending' }])
  const files = [held('partial.docx', { remediated_at: '2026-09-09T12:00:00Z', corrected_sha256: 'saved-digest' }), held('draft.docx')]
  const c = await mount({ run, files })
  expect(c.querySelector('[aria-label="Select partial.docx"]').disabled).toBe(true)
  const optIn = [...c.querySelectorAll('label')].find(el => el.textContent.includes('Publish with remaining issues')).querySelector('input')
  expect(optIn.checked).toBe(false)
  await click(optIn)
  expect(c.querySelector('[aria-label="Select partial.docx"]').disabled).toBe(false)
  expect(c.querySelector('[aria-label="Select draft.docx"]').disabled).toBe(true)
  expect(c.textContent).toContain('Ready with remaining issues')
  await review(c)
  expect(previewReleaseDestination).toHaveBeenLastCalledWith(run.id, ['partial.docx'], '', true, null,
    { allowRemainingIssues: true, expectedArtifacts: { 'partial.docx': 'saved-digest' } })
  await click(button(c, 'Publish 1 copy'))
  expect(c.querySelector('[role="dialog"]').textContent).toContain('does not mark them approved, inspected, verified or compliant')
  await click(button(c, 'Publish 1'))
  expect(publishAllFiles).toHaveBeenCalledWith(run.id, ['partial.docx'], 'Delivery',
    { destination: null, allowRemainingIssues: true, expectedArtifacts: { 'partial.docx': 'saved-digest' } })
  expect(files[0].compliant).toBe(false)
  await c.rerender({ run: { id: 'other' }, files })
  expect([...c.querySelectorAll('label')].find(el => el.textContent.includes('Publish with remaining issues')).querySelector('input').checked).toBe(false)
})

it('offers partial publication in optional publishing settings', async () => {
  const files = [held('one.pdf', { remediated_at: '2026-09-09', corrected_sha256: 'one-digest' }),
    held('two.pdf', { remediated_at: '2026-09-09', corrected_sha256: 'two-digest' }), held('draft.pdf')]
  listHitlQueue.mockResolvedValue(files.map((f, i) => ({ id: i, file: f.file, status: 'pending' })))
  const c = await mount({ run, files })
  const optIn = [...c.querySelectorAll('label')].find(el => el.textContent.includes('Publish with remaining issues')).querySelector('input')
  expect(optIn.closest('details').open).toBe(false)
  expect(optIn.closest('.release-quick')).not.toBeNull()
  await click(optIn)
  const publish = button(c, 'Publish batch with remaining issues (2)')
  expect(publish.disabled).toBe(false)
  expect(publish.closest('details')).toBeNull()
  await click(publish)
  expect(publishAllFiles).toHaveBeenCalledWith(run.id, ['one.pdf', 'two.pdf'], '', {
    destination: null, allowRemainingIssues: true,
    expectedArtifacts: { 'one.pdf': 'one-digest', 'two.pdf': 'two-digest' },
  })
  expect(previewReleaseDestination).not.toHaveBeenCalled()
  expect(files.every(f => f.compliant === false)).toBe(true)
})

it('restores accepted automatic plan permission so saved incomplete files are publishable', async () => {
 const files=[held('one.pdf',{remediated_at:'2026-09-09',corrected_sha256:'digest'})]
 listHitlQueue.mockResolvedValue([{id:1,file:'one.pdf',status:'pending'}])
 getAutomaticRelease.mockResolvedValue({authorization:{status:'active',allow_remaining_issues:true,files:['one.pdf']}})
 const c=await mount({run,files})
 const choice=[...c.querySelectorAll('label')].find(el=>el.textContent.includes('Publish with remaining issues')).querySelector('input')
 expect(choice.checked).toBe(true);expect(button(c,'Publish batch with remaining issues (1)').disabled).toBe(false)
 expect(publishAllFiles).not.toHaveBeenCalled()
 await click(choice);expect(choice.checked).toBe(false)
})
it('does not extend saved publication permission to files outside the accepted scope', async () => {
 getAutomaticRelease.mockResolvedValue({authorization:{status:'active',allow_remaining_issues:true,files:['one.pdf']}})
 const c=await mount({run,files:[held('other.pdf',{remediated_at:'2026-09-09',corrected_sha256:'digest'})]})
 expect([...c.querySelectorAll('label')].find(el=>el.textContent.includes('Publish with remaining issues')).querySelector('input').checked).toBe(false)
})

it('restores the saved destination instead of a changed preference and keeps it locked', async () => {
  getSettings.mockResolvedValueOnce({ release_destination: { provider: 'sharepoint', folder_id: 'new-preference', folder_name: 'New preference' } })
  getReleaseStatus.mockResolvedValue({ release_id: 'existing-release', parent_folder_id: 'original-parent', parent_folder_name: 'Original destination', release_folder_name: '2026-09-09 - owner@example.test', documents: [] })
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  const main = c.querySelector('.release-quick')
  expect(main.textContent).toContain('Original destination')
  expect(main.textContent).toContain('2026-09-09 - owner@example.test')
  expect(main.textContent).not.toContain('New preference')
  expect([...main.querySelectorAll('summary')].some(s => s.textContent === 'Change destination')).toBe(false)
  await click(button(c, 'Publish batch (1)'))
  expect(publishAllFiles.mock.calls.at(-1)[3].destination.folder_id).toBe('original-parent')
})

it('restores a frozen default destination even when preferences arrive later', async () => {
  let resolveSettings
  getSettings.mockImplementationOnce(() => new Promise(resolve => { resolveSettings = resolve }))
  getReleaseStatus.mockResolvedValue({ release_id: 'existing-release', parent_folder_id: null, parent_folder_name: null, release_folder_name: 'Saved release', documents: [] })
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  await act(async () => resolveSettings({ release_destination: { provider: 'sharepoint', folder_id: 'new-parent', folder_name: 'Wrong preference' } }))
  await flush()
  expect(c.querySelector('.release-quick').textContent).not.toContain('Wrong preference')
  await click(button(c, 'Publish batch (1)'))
  expect(publishAllFiles.mock.calls.at(-1)[3].destination).toBeNull()
})

it('waits for a new release destination preference before enabling publish', async () => {
  let resolveSettings
  getSettings.mockImplementationOnce(() => new Promise(resolve => { resolveSettings = resolve }))
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  expect(button(c, 'Publish batch (1)').disabled).toBe(true)
  await act(async () => resolveSettings({ release_destination: { provider: 'sharepoint', folder_id: 'preferred-parent', folder_name: 'Preferred location' } }))
  await flush()
  expect(button(c, 'Publish batch (1)').disabled).toBe(false)
  await click(button(c, 'Publish batch (1)'))
  expect(publishAllFiles.mock.calls.at(-1)[3].destination.folder_id).toBe('preferred-parent')
})

it('recovers a release created after the page loaded without retrying the stale destination', async () => {
  getSettings.mockResolvedValueOnce({ release_destination: { provider: 'sharepoint', folder_id: 'other/parent', folder_name: 'Remediated' } })
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  getReleaseStatus.mockResolvedValue({ release_id: 'started-in-background', parent_folder_id: null, release_folder_name: 'Saved timestamp', documents: [] })
  publishAllFiles.mockRejectedValueOnce(Object.assign(new Error('The authorized Release destination changed; confirm again'), { status: 409 }))
  await click(button(c, 'Publish batch (1)'))
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
  expect(c.querySelector('.release-quick').textContent).toContain('Saved timestamp')
  expect(c.querySelector('.release-quick').textContent).not.toContain('Change destination')
  expect(c.textContent).toContain('Saved release destination restored')
  await click(button(c, 'Publish to saved destination'))
  expect(publishAllFiles).toHaveBeenCalledTimes(2)
  expect(publishAllFiles.mock.calls.at(-1)[3].destination).toBeNull()
  expect(publishAllFiles.mock.calls.at(-1)[3].expectedArtifacts).toBeDefined()
})

it('recovers structured conflicts and waits for a successful destination refresh', async () => {
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  getReleaseStatus.mockRejectedValueOnce(new Error('Connection lost'))
  publishAllFiles.mockRejectedValueOnce(Object.assign(new Error('[object Object]'), { status: 409, detail: { code: 'release_destination_changed' } }))
  await click(button(c, 'Publish batch (1)'))
  expect(button(c, 'Publish batch (1)').disabled).toBe(true)
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
  getReleaseStatus.mockResolvedValue({ release_id: 'background', parent_folder_id: 'library/saved', parent_folder_name: 'Saved parent', release_folder_name: 'Saved timestamp', documents: [] })
  await click(button(c, 'Refresh saved destination'))
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
  expect(c.querySelector('.release-quick').textContent).toContain('Saved parent')
  await click(button(c, 'Publish to saved destination'))
  expect(publishAllFiles.mock.calls.at(-1)[3].destination.folder_id).toBe('library/saved')
})
it('restores a changed saved destination when stale-parent preflight fails', async () => {
  getSettings.mockResolvedValueOnce({ release_destination: { provider: 'sharepoint', folder_id: 'stale/parent', folder_name: 'Stale' } })
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  getReleaseStatus.mockResolvedValue({ release_id: 'background', parent_folder_id: null, release_folder_name: 'Saved', documents: [] })
  publishAllFiles.mockRejectedValueOnce(Object.assign(new Error('Unavailable'), { status: 409, detail: { code: 'release_destination_not_ready' } }))
  await click(button(c, 'Publish batch (1)'))
  expect(c.textContent).toContain('Saved release destination restored')
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
})

it('keeps a new-release permission failure out of saved-destination recovery', async () => {
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  publishAllFiles.mockRejectedValueOnce(Object.assign(new Error('[object Object]'), { status: 409, detail: { code: 'release_destination_not_ready', preflight: { message: 'Folder permission denied' } } }))
  await click(button(c, 'Publish batch (1)'))
  expect(c.textContent).toContain('release service did not complete the request')
  expect(c.textContent).not.toContain('Folder permission denied')
  expect(c.textContent).not.toContain('Refresh the saved release destination')
  expect(button(c, 'Publish batch (1)').disabled).toBe(false)
})
it('does not expose or blindly retry a missing saved destination', async () => {
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  const raw = "HTTPStatusError: 404 Not Found https://graph.microsoft.com/v1.0/drives/b!sensitive/items/01secret"
  publishAllFiles.mockRejectedValueOnce(Object.assign(new Error(raw), { status: 404 }))
  await click(button(c, 'Publish batch (1)'))
  const recovery = c.querySelector('.release-recovery')
  expect(recovery.textContent).toMatch(/moved or deleted/i)
  expect(recovery.textContent).toContain('destination_not_found')
  expect(recovery.textContent).not.toMatch(/graph\.microsoft\.com|b!sensitive|01secret|HTTPStatusError/)
  expect(button(recovery, 'Retry')).toBeUndefined()
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
})
it('shows completion if the background release already delivered the requested copies', async () => {
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  getReleaseStatus.mockResolvedValue({ release_id: 'background', parent_folder_id: null, release_folder_name: 'Saved', documents: [{ file: 'ready.pdf', status: 'published', published_at: '2026-09-10T00:00:00Z' }] })
  publishAllFiles.mockRejectedValueOnce(Object.assign(new Error('The authorized Release destination changed; confirm again'), { status: 409 }))
  await click(button(c, 'Publish batch (1)'))
  expect(c.querySelector('.release-quick').textContent).toContain('Publishing complete')
  expect(c.querySelector('.release-recovery')).toBeNull()
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
})


it('locks repeated publish clicks and confirms the delivered copies beside the action', async () => {
  let finish
  publishAllFiles.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const c = await mount({ run, files: [verified('ready.pdf', { corrected_sha256: 'current' })] })
  const publish = button(c, 'Publish batch (1)')
  await act(async () => { publish.click(); publish.click() })
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
  expect(button(c, 'Publishing copies…').disabled).toBe(true)
  expect(c.querySelector('.release-quick-action [role="status"]').textContent).toContain('Please wait for confirmation')
  await act(async () => finish({ release_id: 'release', published: [{ file: 'ready.pdf', status: 'published', artifact_digest: 'sha256:current', published_at: '2026-09-10T10:00:00Z' }] }))
  await flush()
  expect(button(c, 'All files published ✓').disabled).toBe(true)
  await click(button(c, 'All files published ✓'))
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
  expect(c.querySelector('[aria-label="Publication outcomes"] [aria-label="Release reports"]')).not.toBeNull()
  expect(c.querySelector('[aria-label="Delivery receipt"] [aria-label="Release reports"]')).toBeNull()
})


it('waits for the selected SharePoint file instead of mistaking another delivery for completion', async () => {
  const c = await mount({ run: { ...run, source: 'sharepoint' }, files: [verified('ready.pdf')] })
  publishAllFiles.mockResolvedValueOnce({ release_id: 'release', queued: 1, published: [] })
  getReleaseStatus.mockResolvedValueOnce({ release_id: 'release', documents: [{ file: 'other.pdf', status: 'published' }] })
    .mockResolvedValue({ release_id: 'release', documents: [{ file: 'ready.pdf', status: 'published' }] })
  await act(async () => button(c, 'Publish batch (1)').click())
  await flush()
  expect(button(c, 'Publishing copies…').disabled).toBe(true)
  await act(async () => new Promise(resolve => setTimeout(resolve, 2100)))
  await flush()
  expect(button(c, 'All files published ✓').disabled).toBe(true)
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
})


it('does not apply an old publish response after changing scans', async () => {
  let finish
  publishAllFiles.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const props = { run, files: [verified('ready.pdf')], onPublish: vi.fn() }
  const c = await mount(props)
  await act(async () => button(c, 'Publish batch (1)').click())
  await c.rerender({ ...props, run: { ...run, id: 'another' } })
  await act(async () => finish({ release_id: 'old-release', published: [{ file: 'ready.pdf', status: 'published' }] }))
  await flush()
  expect(button(c, 'Publish batch (1)').disabled).toBe(false)
  expect(c.querySelector('[aria-label="Delivery receipt"]')).toBeNull()
  expect(props.onPublish).not.toHaveBeenCalled()
})

it('automatically recovers the durable exact saved Release without publishing or approving again', async () => {
 const saved={id:'saved-auth',run_id:'accepted-run',revision:1,status:'blocked',can_resume:true,
   requires_reconnect:false,files:['one.pdf'],allow_remaining_issues:true,
   destination:{provider:'drive',folder_id:'folder'}}
 getAutomaticRelease.mockResolvedValue({run_id:'accepted-run',authorization:saved})
 const c=await mount({run:{...run,source:'drive',owner_email:'owner'},files:[held('one.pdf')]})
 expect(resumeAutomaticRelease).toHaveBeenCalledWith('scan1','saved-auth')
 expect(publishAllFiles).not.toHaveBeenCalled()
 expect(button(c,'Resume delivery')).toBeUndefined()
 expect(document.querySelector('.release-recovery-banner').textContent).toContain('Checking saved delivery status')
 expect(document.querySelector('.release-recovery-banner button')).toBeNull()
})

 it('retires redundant assessment details from the publishing path', async () => {
  const c = await mount({ run: { id: 'simpler-release', status: 'completed' }, files: [verified('ready.pdf', { corrected_sha256: 'current' })] })
  const details = [...c.querySelectorAll('details')].find(el => el.querySelector(':scope > summary')?.textContent === 'Assessment findings and saved changes (optional)')
  expect(details).toBeUndefined()
  expect(c.querySelector('.remediation-live-documents')).toBeNull()
  const publish = button(c, 'Publish batch (1)')
  expect(publish.closest('details')).toBeNull()
  await click(publish)
  expect(publishAllFiles).toHaveBeenCalledTimes(1)
  expect(c.querySelector('.release-confirm')).toBeNull()
 })

it('defaults to Manage publication and keeps reports in a separate keyboard accessible tab', async () => {
  const c = await mount({run, files:[verified('a.pdf')]})
  const manage = c.querySelector('#release-tab-manage')
  const reports = c.querySelector('#release-tab-reports')
  expect(manage.getAttribute('aria-selected')).toBe('true')
  expect(c.querySelector('#release-panel-reports').hidden).toBe(true)
  expect(c.querySelector('#release-panel-manage').textContent).toContain('Publish your documents')
  await click(reports)
  expect(c.querySelector('#release-panel-manage').hidden).toBe(true)
  expect(c.querySelector('#release-panel-reports [aria-label="Publication outcomes"]')).toBeNull()
  expect(c.querySelector('#release-panel-manage').textContent).toContain('Search filenames')
  expect(c.querySelectorAll('#release-panel-manage [aria-label="Release reports"]')).toHaveLength(1)
  expect(c.querySelector('#release-panel-reports').textContent).not.toContain('Scan summary and per-file checklists')
  expect(c.querySelector('#release-panel-reports').textContent).toContain('Assessment reports')
  await click(manage)
  expect(c.querySelector('#release-panel-manage').hidden).toBe(false)
})

it('shows approved unapplied changes as Processing without another approval barrier', async () => {
  listHitlQueue.mockResolvedValue([{file:'a.pdf',status:'approved',applied:null}])
  const c = await mount({run, files:[held('a.pdf')]})
  expect(row(c,'a.pdf').textContent).toContain('No further approval needed')
  expect(c.querySelector('[aria-label="Release status overview"]').textContent).not.toContain('1 Needs attention')
})
it('retires duplicate document-stage tiles and filters the default document list without changing selection', async () => {
 const c=await mount({run,files:[verified('ready.pdf',{corrected_sha256:'exact'}),held('remaining.pdf')]})
 expect(c.querySelector('.remediation-progress-summary')).toBeNull()
 expect(c.querySelector('.progress-ready')).toBeNull()
 const before=[...c.querySelectorAll('input[type="checkbox"]')].map(input=>input.checked)
 const documents=c.querySelector('#release-panel-manage [aria-label="Publication outcomes"]')
 expect(documents.querySelectorAll('tbody tr')).toHaveLength(2)
 const search=documents.querySelector('input[type="search"]')
 const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set
 await act(async()=>{setter.call(search,'ready');search.dispatchEvent(new Event('input',{bubbles:true}))})
 expect(documents.querySelectorAll('tbody tr')).toHaveLength(1)
 expect([...c.querySelectorAll('input[type="checkbox"]')].map(input=>input.checked)).toEqual(before)
 expect(c.querySelector('[role="dialog"]')).toBeNull()
 await click(button(documents,'Clear filters'))
 expect(documents.querySelectorAll('tbody tr')).toHaveLength(2)
})
it('shows an honest empty filtered document list without restoring retired stage tiles', async () => {
 const c=await mount({run,files:[held('remaining.pdf')]})
 const documents=c.querySelector('#release-panel-manage [aria-label="Publication outcomes"]')
 const search=documents.querySelector('input[type="search"]')
 const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set
 await act(async()=>{setter.call(search,'no-such-file');search.dispatchEvent(new Event('input',{bubbles:true}))})
 expect(documents.textContent).toContain('No documents match these filters.')
 expect(documents.textContent).toContain('0 of 1 documents shown')
 expect(c.querySelector('.progress-processing')).toBeNull()
 expect(c.querySelector('.progress-attention')).toBeNull()
})
it('restores current durable automatic publication on reload and prevents a duplicate manual batch', async () => {
 getSettings.mockResolvedValue({drive_mirror_enabled:false,drive_mirror_folder:'Remediated'})
 getAutomaticRelease.mockResolvedValue({run_id:'batch-a',authorization:{id:'accepted',run_id:'batch-a',status:'active',destination:{provider:'local'},files:['one.pdf'],allow_remaining_issues:true}})
 const c = await mount({run:{...run,source:'local'},files:[held('one.pdf',{remediated_at:'2026-09-09',corrected_sha256:'digest'})]})
 expect(button(c,'Publish batch with remaining issues (1)')).toBeUndefined()
 expect(c.textContent).toContain('Automatic publishing is on')
 expect(c.querySelector('.release-advanced')).toBeNull()
 expect(publishAllFiles).not.toHaveBeenCalled()
})
it.each(['completed','stopped','expired','failed'])('keeps covered copies out of generic manual publication for a saved %s plan', async status => {
 getSettings.mockResolvedValue({drive_mirror_enabled:false,drive_mirror_folder:'Remediated'})
 getAutomaticRelease.mockResolvedValue({run_id:'batch-a',authorization:{id:'accepted',run_id:'batch-a',status,destination:{provider:'local'},files:['one.pdf'],allow_remaining_issues:true}})
 const c=await mount({run:{...run,source:'local'},files:[verified('one.pdf',{corrected_sha256:'digest'})]})
 expect([...c.querySelectorAll('button')].filter(b=>b.textContent.startsWith('Publish batch')&&!b.closest('[hidden]'))).toHaveLength(0)
 expect(c.querySelector('.release-advanced')).toBeNull()
 expect(publishAllFiles).not.toHaveBeenCalled()
})
it('does not let an old remediation batch disable manual publishing', async () => {
 getSettings.mockResolvedValue({drive_mirror_enabled:false,drive_mirror_folder:'Remediated'})
 getAutomaticRelease.mockResolvedValue({run_id:'new-batch',authorization:{id:'old',run_id:'old-batch',status:'active',destination:{provider:'local'},files:['one.pdf'],allow_remaining_issues:true}})
 const c = await mount({run:{...run,source:'local'},files:[held('one.pdf',{remediated_at:'2026-09-09',corrected_sha256:'digest'})]})
 expect(button(c,'Publish batch with remaining issues (1)').disabled).toBe(false)
})
it('does not expose duplicate publication when the saved permission has expired', async () => {
 getAutomaticRelease.mockResolvedValue({run_id:'batch-a',authorization:{id:'accepted',run_id:'batch-a',status:'active',expires_at:'2000-01-01T00:00:00Z',destination:{provider:'local'},files:['one.pdf'],allow_remaining_issues:true}})
 const c=await mount({run:{...run,source:'local'},files:[verified('one.pdf',{corrected_sha256:'digest'})]})
 expect(button(c,'Publish batch with remaining issues (1)')).toBeUndefined()
 expect(c.querySelector('.release-advanced')).toBeNull()
 expect(publishAllFiles).not.toHaveBeenCalled()
})
it('bounds an unresponsive initial automatic publication check and offers an explicit refresh', async () => {
 vi.useFakeTimers()
 getSettings.mockResolvedValue({drive_mirror_enabled:false,drive_mirror_folder:'Remediated'})
 getAutomaticRelease.mockImplementation(()=>new Promise(()=>{}))
 const {root,container}=createTestRoot()
 await act(async()=>root.render(createElement(Publish,{run:{...run,source:'local'},files:[verified('one.pdf',{corrected_sha256:'digest'})]})))
 expect(button(container,'Publish batch (1)')).toBeUndefined()
 expect(container.textContent).toContain('Checking automatic publication…')
 await act(async()=>vi.advanceTimersByTimeAsync(20000))
 expect(container.textContent).not.toContain('Checking automatic publication…')
 expect(button(container,'Refresh automatic publication status')).toBeTruthy()
 expect(button(container,'Publish batch (1)')).toBeUndefined()
 expect(container.querySelector('.release-workspace')).toBeNull()
 expect(publishAllFiles).not.toHaveBeenCalled()
 getAutomaticRelease.mockResolvedValue({authorization:null})
 await act(async()=>button(container,'Refresh automatic publication status').click())
 expect(button(container,'Publish batch (1)').disabled).toBe(false)
 vi.useRealTimers()
})

it('keeps the unconfirmed banner retired on reload without another delivery request',async()=>{
 const saved={id:'unconfirmed-auth',run_id:'accepted-run',revision:7,status:'blocked',can_resume:true,
   requires_reconnect:false,files:['one.pdf'],allow_remaining_issues:true,
   destination:{provider:'drive',folder_id:'folder'}}
 // The resume request failed before any accepted job was observed; subsequent durable
 // GETs still report the identical blocked revision. That cannot establish delivery.
 getAutomaticRelease.mockResolvedValue({run_id:'accepted-run',authorization:saved})
 resumeAutomaticRelease.mockRejectedValueOnce(new Error('Connection lost before confirmation'))
 const props={run:{...run,source:'drive',owner_email:'owner'},files:[held('one.pdf')]}
 await mount(props)
 expect(resumeAutomaticRelease).toHaveBeenCalledOnce()
 expect(document.querySelector('.release-recovery-banner')).toBeNull()
 await unmountAll()
 const { _resetRecoveryAttempts }=await import('./useAutomaticDeliveryRecovery.js')
 _resetRecoveryAttempts() // Simulate a freshly loaded page retaining session storage.
 await mount(props)
 expect(resumeAutomaticRelease).toHaveBeenCalledOnce()
 expect(document.querySelector('.release-recovery-banner')).toBeNull()
 expect(publishAllFiles).not.toHaveBeenCalled()
})

it('retries a transient automatic-status GET without a click and keeps the confirmed authorization',async()=>{
 vi.useFakeTimers()
 const saved={id:'get-recovery',run_id:'accepted-run',revision:4,status:'blocked',can_resume:false,
   requires_reconnect:false,files:['one.pdf'],allow_remaining_issues:true,destination:{provider:'drive',folder_id:'folder'}}
 getAutomaticRelease.mockRejectedValueOnce(new Error('Temporary network failure')).mockResolvedValue({run_id:'accepted-run',authorization:saved})
 const {container,root}=createTestRoot()
 const props={run:{...run,source:'drive',owner_email:'owner'},files:[held('one.pdf')]}
 await act(async()=>root.render(createElement(Publish,props)))
 expect(getAutomaticRelease).toHaveBeenCalledOnce()
 await act(async()=>vi.advanceTimersByTimeAsync(5000))
 expect(getAutomaticRelease).toHaveBeenCalledTimes(2)
 expect(container.textContent).toContain('Automatic publishing is on')
 getAutomaticRelease.mockRejectedValueOnce(new Error('Another transient failure'))
 await act(async()=>vi.advanceTimersByTimeAsync(5000))
 expect(container.textContent).toContain('Automatic publishing is on')
 const failedSignal=getAutomaticRelease.mock.calls.at(-1)[2].signal
 await act(async()=>vi.advanceTimersByTimeAsync(5000))
 expect(getAutomaticRelease.mock.calls.at(-1)[2].signal).not.toBe(failedSignal)
 expect(resumeAutomaticRelease).not.toHaveBeenCalled()
 expect(publishAllFiles).not.toHaveBeenCalled()
 await act(async()=>root.unmount());vi.useRealTimers()
})
it('replaces a timed-out GET controller with a fresh controller without a click',async()=>{
 vi.useFakeTimers()
 const signals=[]
 getAutomaticRelease.mockImplementationOnce((sid,files,options)=>{signals.push(options.signal);return new Promise(()=>{})})
 getAutomaticRelease.mockResolvedValue({authorization:null})
 const {root}=createTestRoot()
 await act(async()=>root.render(createElement(Publish,{run:{...run,owner_email:'owner'},files:[held('one.pdf')]})))
 await act(async()=>vi.advanceTimersByTimeAsync(25000))
 expect(signals[0].aborted).toBe(true)
 expect(getAutomaticRelease).toHaveBeenCalledTimes(2)
 expect(getAutomaticRelease.mock.calls[1][2].signal.aborted).toBe(false)
 await act(async()=>root.unmount());vi.useRealTimers()
})
it('bounds consecutive GET retries and stops immediately on terminal authorization failure',async()=>{
 vi.useFakeTimers()
 getAutomaticRelease.mockRejectedValue(new Error('Network unavailable'))
 const {root}=createTestRoot()
 await act(async()=>root.render(createElement(Publish,{run:{...run,owner_email:'owner'},files:[held('one.pdf')]})))
 await act(async()=>vi.advanceTimersByTimeAsync(120000))
 expect(getAutomaticRelease).toHaveBeenCalledTimes(5)
 await act(async()=>root.unmount());getAutomaticRelease.mockClear()
 getAutomaticRelease.mockRejectedValue(Object.assign(new Error('Sign-in required'),{status:401}))
 const second=createTestRoot()
 await act(async()=>second.root.render(createElement(Publish,{run:{...run,id:'another',owner_email:'owner'},files:[held('one.pdf')]})))
 await act(async()=>vi.advanceTimersByTimeAsync(120000))
 expect(getAutomaticRelease).toHaveBeenCalledOnce()
 await act(async()=>second.root.unmount());getAutomaticRelease.mockResolvedValue({authorization:null});vi.useRealTimers()
})

it('does not dispatch a delayed GET after account change or unmount',async()=>{
 vi.useFakeTimers();getAutomaticRelease.mockRejectedValue(new Error('Temporary error'))
 const first=createTestRoot()
 await act(async()=>first.root.render(createElement(Publish,{run:{...run,owner_email:'owner'},files:[held('one.pdf')]})))
 noteAuthChange('owner','other')
 await act(async()=>vi.advanceTimersByTimeAsync(5000))
 expect(getAutomaticRelease).toHaveBeenCalledOnce()
 await act(async()=>first.root.unmount());_resetAuthEpoch();getAutomaticRelease.mockClear()
 const second=createTestRoot()
 await act(async()=>second.root.render(createElement(Publish,{run:{...run,id:'new',owner_email:'owner'},files:[held('one.pdf')]})))
 await act(async()=>second.root.unmount())
 await act(async()=>vi.advanceTimersByTimeAsync(120000))
 expect(getAutomaticRelease).toHaveBeenCalledOnce()
 getAutomaticRelease.mockResolvedValue({authorization:null})
})

it('keeps explicit manual publication for copies outside the saved automatic plan', async () => {
 getAutomaticRelease.mockResolvedValue({run_id:'batch-a',authorization:{id:'accepted',run_id:'batch-a',status:'active',destination:{provider:'local'},files:['automatic.pdf'],allow_remaining_issues:true}})
 const c=await mount({run:{...run,source:'local'},files:[verified('automatic.pdf',{corrected_sha256:'auto'}),verified('manual.pdf',{corrected_sha256:'manual'})]})
 expect(c.querySelector('.release-advanced')).toBeTruthy()
 const publish=button(c,'Publish batch (1)')
 expect(publish.disabled).toBe(false)
 await click(publish)
 expect(publishAllFiles.mock.calls.at(-1)[1]).toEqual(['manual.pdf'])
})
