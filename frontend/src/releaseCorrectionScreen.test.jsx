import { it, expect, vi, afterEach, describe } from 'vitest'
import { createElement, act } from 'react'
import { readFileSync } from 'node:fs'
globalThis.IS_REACT_ACT_ENVIRONMENT = true
import { createTestRoot, unmountAll } from './testRoots.js'

// The live production screen the user reported (Release completion surface): the receipt names
// 02701e61… published 13:52:35, the current corrected copy is 8c84bfca… saved 13:53:05, no later
// publish, nothing failed at Graph. Plus contract v3 refused/partial republish and digest-movement
// success.
const V1 = '02701e61' + 'a'.repeat(56)
const V2 = '8c84bfca' + 'b'.repeat(56)
const V3 = 'c0ffee00' + 'c'.repeat(56)

const republishRelease = vi.fn()
const getReleaseStatus = vi.fn()
const getReleaseReports = vi.fn()
const getAutomaticRelease = vi.fn()
vi.mock('./api.js', () => ({
  getAutomaticRelease: (...a) => getAutomaticRelease(...a),
  getReleaseReports: (...a) => getReleaseReports(...a), retryReleaseReports: vi.fn(), downloadReleaseReport: vi.fn(),
  openReport: vi.fn(), publishFile: vi.fn(() => Promise.resolve({})),
  publishAllFiles: vi.fn(() => Promise.resolve({ published: [] })),
  getReleaseStatus: (...a) => getReleaseStatus(...a),
  republishRelease: (...a) => republishRelease(...a),
  listReleaseHistory: vi.fn(() => Promise.resolve({ releases: [] })),
  getReleaseManifest: vi.fn(() => Promise.resolve({ manifest: {} })),
  listHitlQueue: vi.fn(() => Promise.resolve([])),
  getSettings: vi.fn(() => Promise.resolve({ drive_mirror_enabled: false })),
  putMyReleaseTemplates: vi.fn(() => Promise.resolve({ release_templates: [] })),
  getSourceStatus: vi.fn(() => Promise.resolve({ files: [], stale_count: 0 })),
  rescoreFile: vi.fn(() => Promise.resolve({})),
  previewReleaseDestination: vi.fn(() => Promise.resolve({ can_release: true, documents: [] })),
  downloadReleasePackage: vi.fn(() => Promise.resolve()),
}))
vi.mock('./ScopeBanner.jsx', () => ({ default: () => null }))
vi.mock('./remediableScope.js', () => ({
  documentSelection: () => ({}), documentScopeSentence: () => '', documentsInSelection: (files) => files || [],
}))
vi.mock('./ReleaseQuickActions.jsx', () => ({ default: () => null }))
vi.mock('./RemediationLiveDocuments.jsx', () => ({ default: () => null }))

const { default: Publish } = await import('./Publish.jsx')
const { default: ReleaseCorrectionNotice } = await import('./ReleaseCorrectionNotice.jsx')

afterEach(async () => {
  await unmountAll(); vi.useRealTimers(); vi.unstubAllEnvs(); vi.unstubAllGlobals()
  for (const mock of [republishRelease, getReleaseStatus, getReleaseReports, getAutomaticRelease]) mock.mockReset()
})
const flush = async () => { for (let k = 0; k < 6; k++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) }) }
const render = async (component, props) => {
  const { container, root } = createTestRoot()
  await act(async () => { root.render(createElement(component, props)) })
  await flush()
  return container
}
const button = (c, text) => [...c.querySelectorAll('button')].find((b) => b.textContent === text)
const click = async (el) => { await act(async () => { el.click() }); await flush() }
const publicationCells = (c) => [...c.querySelectorAll('.release-completion-documents tbody td strong')].map((el) => el.textContent)

const staleItem = (over = {}) => ({ file: 'report.docx', published_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}`,
  published_at: '2026-09-17T10:00:00Z', current_remediated_at: '2026-09-17T11:00:00Z', requires_remaining_issue_confirmation: false, last_attempt_failure: null, ...over })
const publication = (over = {}) => ({ state: 'out_of_date', out_of_date: [staleItem()], identity_unknown: [], can_republish: true, republish_blocked_reason: null, ...over })

describe('Contract v3: refused and partial republish', () => {
  it('a zero-admitted 409 shows each file reason and never starts settlement polling', async () => {
    const failure = Object.assign(new Error('None of the selected copies were published. The reason for each is shown below; earlier published copies are unchanged.'), {
      status: 409, code: 'republish_refused', results: [{ file: 'report.docx', status: 'failed', failure_category: 'artifact_changed', explanation: 'The authorized corrected artifact changed; confirm again' }] })
    const onRepublished = vi.fn()
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', onRepublished, republish: vi.fn().mockRejectedValue(failure), publication: publication() })
    await click(button(c, 'Publish updated copy and refresh reports'))
    expect(onRepublished).not.toHaveBeenCalled()
    expect(c.textContent).toContain('report.docx was not published: The authorized corrected artifact changed; confirm again')
    expect(c.textContent).not.toMatch(/publishing started|Publishing updated copy…/i)
  })

  it('a partial 200 follows only the admitted files and names each refused file', async () => {
    const onRepublished = vi.fn().mockResolvedValue('current')
    const republish = vi.fn().mockResolvedValue({ result: 'republished', republished: ['a.docx'],
      refused: [{ file: 'b.docx', failure_category: 'artifact_changed', explanation: 'A newer copy was saved' }] })
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', onRepublished, republish,
      publication: publication({ out_of_date: [staleItem({ file: 'a.docx' }), staleItem({ file: 'b.docx' })] }) })
    await click(button(c, 'Publish updated copy and refresh reports'))
    expect(onRepublished).toHaveBeenCalledTimes(1)
    expect(onRepublished.mock.calls[0][1].map((item) => item.file)).toEqual(['a.docx'])
    expect(c.querySelector('[aria-label="Copies not published"]').textContent).toContain('b.docx was not published: A newer copy was saved')
  })

  it('never says "publishing" for a file the server did not admit', async () => {
    let resolveOutcome
    const onRepublished = vi.fn(() => new Promise((resolve) => { resolveOutcome = resolve }))
    const republish = vi.fn().mockResolvedValue({ result: 'republished', republished: ['a.docx'],
      refused: [{ file: 'b.docx', failure_category: 'not_ready', explanation: 'Not ready' }] })
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', onRepublished, republish,
      publication: publication({ out_of_date: [staleItem({ file: 'a.docx' }), staleItem({ file: 'b.docx' })] }) })
    await click(button(c, 'Publish updated copy and refresh reports'))
    const status = c.querySelector('[role="status"]').textContent
    expect(status).toContain('Publishing started for a.docx.')
    expect(status).not.toContain('b.docx')
    await act(async () => resolveOutcome('current')); await flush()
  })

  it('reads detail.results from a coded 409 in the API layer', async () => {
    vi.stubEnv('VITE_SIM', 'false'); vi.resetModules()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 409, statusText: 'Conflict', url: 'http://x/scans/s/release/republish', headers: new Headers(),
      json: async () => ({ detail: { code: 'republish_refused', files: ['a.docx'], results: [{ file: 'a.docx', status: 'failed', failure_category: 'not_ready', explanation: 'Not ready' }], message: 'server' } }) }))
    const actual = await vi.importActual('./api.js')
    const error = await actual.republishRelease('s', { expected_artifacts: { 'a.docx': V2 }, allow_remaining_issues: false, remaining_issue_files: [] }).catch((e) => e)
    expect(error.code).toBe('republish_refused')
    expect(error.results).toEqual([{ file: 'a.docx', status: 'failed', failure_category: 'not_ready', explanation: 'Not ready' }])
    expect(error.message).toMatch(/None of the selected copies were published/)
  })
})

describe('Production screen: UTSW_Discharge_Summary.docx published 02701e61, current 8c84bfca', () => {
  const name = 'UTSW_Discharge_Summary.docx'
  const doc = { file: name, compliant: true, remediated_at: '2026-09-18T13:53:05Z', corrected_sha256: V2, score: 100 }
  const receipt = (over = {}) => ({ file: name, status: 'published', artifact_digest: `sha256:${V1}`, published_at: '2026-09-18T13:52:35Z',
    released_document_url: 'https://tenant.sharepoint.com/doc', publication_state: 'out_of_date',
    published_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}`, ...over })
  const stale = { file: name, published_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}`, published_at: '2026-09-18T13:52:35Z',
    current_remediated_at: '2026-09-18T13:53:05Z', requires_remaining_issue_confirmation: false, last_attempt_failure: null }
  const staleStatus = { release_id: 'rel1', roots: [], documents: [receipt()],
    publication: { state: 'out_of_date', out_of_date: [stale], identity_unknown: [], can_republish: true, republish_blocked_reason: null } }
  // The saved automatic plan never classified the copy: this is what rendered "Classification unavailable".
  const batch = { available: true, authorization_id: 'auto', scope_id: 'auto', run_id: 'run', revision: 1, total: 1, delivered: 0, remaining: 1, status: 'blocked',
    buckets: { waiting: 0, processing: 0, published: 0, failed: 0, skipped: 0, unclassified: 1 }, file_membership: { [name]: 'unclassified' } }
  const authorization = { id: 'auto', run_id: 'run', status: 'completed', files: [name], destination: { provider: 'sharepoint' }, batch_progress: batch }
  // 3 findings fixed and verified; 1 (the retired 1.1.1 target) replaced by reassessment; 0 open.
  const snapshot = { scan_id: 'scan1', batch_id: 'run', total_documents: 1, finding_reconciliation: { exact: true, assessed: 4, resolved_verified: 3,
    awaiting_review: 0, approved_pending_verification: 0, unchanged_no_fix: 0, failed: 0, excluded: 0, superseded: 1,
    original_assessment: [{ file: name, rule_id: '1.1.1', fix_mode: 'assisted', finding_count: 4 }] } }
  const reports = { status: 'completed', scan_id: 'scan1', release_id: 'rel1', currency: 'out_of_date', currency_reason: 'copy_changed_after_publication',
    out_of_date_files: [{ file: name, reported_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}` }],
    reports: [{ name: 'scan-summary-d21d09603f.pdf', url: 'https://tenant.sharepoint.com/summary.pdf', report_kind: 'scan_summary' }] }
  const mountScreen = async (onPublish = vi.fn()) => {
    getAutomaticRelease.mockResolvedValue({ authorization, run_id: 'run' })
    getReleaseReports.mockResolvedValue(reports)
    return render(Publish, { run: { id: 'scan1', source: 'sharepoint' }, me: { email: 'jeremy_acp@example.com' }, files: [doc], remediationSnapshot: snapshot, onPublish })
  }
  const tile = (c, label) => [...c.querySelectorAll('.release-outcome-summary .rap-tile')]
    .find((el) => el.querySelector('dt').textContent === label).querySelector('dd').firstChild.textContent

  it('explains the out-of-date copy by digest, offers the working action, and counts findings honestly', async () => {
    getReleaseStatus.mockResolvedValue(staleStatus)
    const c = await mountScreen()
    const summary = c.querySelector('.release-outcome-summary')
    expect(summary.querySelector('h3').textContent).toBe('Published copy is out of date')
    expect(summary.textContent).toContain(`${name}: the published copy is out of date (published sha256:02701e61… vs current sha256:8c84bfca…)`)
    const planLine = c.querySelector('[aria-label="Automatic publication status"]').textContent
    expect(planLine).toContain('0 of 1 authorized copies delivered · 1 published copy out of date')
    expect(planLine).not.toContain('awaiting confirmation')
    expect(tile(c, 'Fixed and verified · findings')).toBe('3')
    expect(tile(c, 'Findings still open')).toBe('0')
    expect(summary.textContent).toContain('1 replaced by reassessment (the original target no longer exists; not counted as open or as verified fixes)')
    expect(summary.textContent).not.toContain('These are included in findings still open')
    expect(publicationCells(c)).toEqual(['Published copy out of date'])
    expect(c.textContent).not.toContain('Classification unavailable')
    expect(c.textContent).not.toContain('Reports saved alongside the published files.')
    const action = button(c, 'Publish updated copy and refresh reports')
    expect(action.disabled).toBe(false)
    getReleaseStatus.mockResolvedValue({ release_id: 'rel1', roots: [],
      documents: [receipt({ artifact_digest: `sha256:${V2}`, published_artifact_digest: `sha256:${V2}`, publication_state: 'current', published_at: '2026-09-18T14:40:00Z' })],
      publication: { state: 'current', out_of_date: [], identity_unknown: [], can_republish: false, republish_blocked_reason: null } })
    republishRelease.mockResolvedValue({ result: 'republished', republished: [name], refused: [] })
    await click(action)
    expect(republishRelease).toHaveBeenCalledExactlyOnceWith('scan1', { expected_artifacts: { [name]: V2 }, allow_remaining_issues: false, remaining_issue_files: [] })
    expect(c.querySelector('.release-correction-notice')).toBeNull()
    expect(c.textContent).toContain('1 corrected copy released')
    expect(publicationCells(c)).toEqual(['Delivered'])
  })

  it('treats a settle onto the SENT digest as published even when a newer correction arrived meanwhile', async () => {
    getReleaseStatus.mockResolvedValue(staleStatus)
    const onPublish = vi.fn()
    const c = await mountScreen(onPublish)
    getReleaseStatus.mockResolvedValue({ release_id: 'rel1', roots: [],
      documents: [receipt({ artifact_digest: `sha256:${V2}`, published_artifact_digest: `sha256:${V2}`, current_artifact_digest: `sha256:${V3}`, published_at: '2026-09-18T14:40:00Z' })],
      publication: { state: 'out_of_date', out_of_date: [{ ...stale, published_artifact_digest: `sha256:${V2}`, current_artifact_digest: `sha256:${V3}` }],
        identity_unknown: [], can_republish: true, republish_blocked_reason: null } })
    republishRelease.mockResolvedValue({ result: 'republished', republished: [name], refused: [] })
    await click(button(c, 'Publish updated copy and refresh reports'))
    const notice = c.querySelector('.release-correction-notice')
    expect(notice.querySelector('[role="status"]').textContent).toBe('The updated copy was published; a newer correction was saved since. Publish again to deliver the newest version.')
    expect(notice.textContent).not.toMatch(/was not published|did not complete/)
    expect(notice.textContent).toContain('sha256:c0ffee00…')
    expect(onPublish).toHaveBeenCalledExactlyOnceWith(name)
  })

  it('a refused republish on this screen shows the reason without polling or "publishing"', async () => {
    getReleaseStatus.mockResolvedValue(staleStatus)
    const c = await mountScreen()
    const calls = getReleaseStatus.mock.calls.length
    republishRelease.mockRejectedValue(Object.assign(new Error('A newer corrected copy was saved after this page loaded. Refresh release status and review the latest version before publishing.'),
      { status: 409, code: 'artifact_changed', results: [{ file: name, status: 'failed', failure_category: 'artifact_changed', explanation: 'The corrected copy changed since you confirmed it' }] }))
    await click(button(c, 'Publish updated copy and refresh reports'))
    expect(getReleaseStatus.mock.calls.length).toBe(calls)
    expect(c.textContent).toContain(`${name} was not published: The corrected copy changed since you confirmed it`)
    expect(button(c, 'Publishing updated copy…')).toBeUndefined()
  })

  it('keeps the table header whole and the retired counts hidden (CSS source; jsdom applies no layout)', () => {
    const table = readFileSync('src/release-completion-documents.css', 'utf8')
    expect(table).toMatch(/\.release-completion-documents thead th \{[^}]*overflow-wrap:normal/)
    expect(readFileSync('src/release-clarity.css', 'utf8')).toContain('.release-clarity-counts[hidden] { display: none; }')
  })
})
