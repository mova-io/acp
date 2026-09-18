import { it, expect, vi, afterEach, describe } from 'vitest'
import { createElement, act } from 'react'
globalThis.IS_REACT_ACT_ENVIRONMENT = true
import { createTestRoot, unmountAll } from './testRoots.js'

// A correction saved AFTER publication must make the delivered copy and its reports visibly out of
// date by exact artifact identity, and offer one explicitly authorized republish action.
const V1 = '02701e61' + 'a'.repeat(56)
const V2 = '8c84bfca' + 'b'.repeat(56)

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
const { default: ReleaseReports } = await import('./ReleaseReports.jsx')
const { default: ReleaseHistory } = await import('./ReleaseHistory.jsx')
const { deliveryIsCurrent, releaseReadiness } = await import('./releaseClarityModel.js')

afterEach(async () => {
  await unmountAll(); vi.useRealTimers()
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

const staleItem = (over = {}) => ({ file: 'report.docx', published_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}`,
  published_at: '2026-09-17T10:00:00Z', current_remediated_at: '2026-09-17T11:00:00Z', requires_remaining_issue_confirmation: false, ...over })
const publication = (over = {}) => ({ state: 'out_of_date', out_of_date: [staleItem()], identity_unknown: [], can_republish: true, republish_blocked_reason: null, ...over })

describe('ReleaseCorrectionNotice', () => {
  it('names both exact versions and requires an unchecked-by-default confirmation for remaining issues', async () => {
    const republish = vi.fn().mockResolvedValue({ queued: true })
    const onRepublished = vi.fn()
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', republish, onRepublished,
      publication: publication({ out_of_date: [staleItem({ requires_remaining_issue_confirmation: true })] }) })
    expect(c.textContent).toContain('Published copy is out of date')
    expect(c.textContent).toContain('sha256:02701e61…')
    expect(c.textContent).toContain('sha256:8c84bfca…')
    const action = button(c, 'Publish updated copy and refresh reports')
    expect(action.tagName).toBe('BUTTON')
    expect(action.disabled).toBe(true)
    const box = c.querySelector('input[type="checkbox"]')
    expect(box.checked).toBe(false)
    expect(box.closest('label').textContent).toContain('report.docx version sha256:8c84bfca…')
    await click(action)
    expect(republish).not.toHaveBeenCalled()
    await click(box)
    expect(action.disabled).toBe(false)
    await click(action)
    expect(republish).toHaveBeenCalledExactlyOnceWith('scan1', { expected_artifacts: { 'report.docx': V2 }, allow_remaining_issues: true, remaining_issue_files: ['report.docx'] })
    expect(onRepublished).toHaveBeenCalledTimes(1)
    expect(c.querySelector('[role="status"]').textContent).toContain('Publishing started for report.docx.')
  })

  it('never authorizes remaining issues when no confirmation is required', async () => {
    const republish = vi.fn().mockResolvedValue({ queued: true })
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', republish, publication: publication() })
    expect(c.querySelector('input[type="checkbox"]')).toBeNull()
    await click(button(c, 'Publish updated copy and refresh reports'))
    expect(republish).toHaveBeenCalledExactlyOnceWith('scan1', { expected_artifacts: { 'report.docx': V2 }, allow_remaining_issues: false, remaining_issue_files: [] })
  })

  it('disables the action and shows progress while publishing', async () => {
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', publication: publication(), busy: true })
    const action = button(c, 'Publishing updated copy…')
    expect(action.disabled).toBe(true)
    expect(action.getAttribute('aria-busy')).toBe('true')
    expect(c.querySelector('[role="status"]').textContent).toContain('Publishing the updated copy')
    const server = await render(ReleaseCorrectionNotice, { scanId: 'scan1', publication: publication({ state: 'publishing' }) })
    expect(button(server, 'Publishing updated copy…').disabled).toBe(true)
  })

  it('reports an idempotent already-current answer without claiming a new publication started', async () => {
    const republish = vi.fn().mockResolvedValue({ already_current: true })
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', republish, publication: publication() })
    await click(button(c, 'Publish updated copy and refresh reports'))
    expect(c.querySelector('[role="status"]').textContent).toBe('The published copy is already the current corrected copy.')
  })

  it('names only the ticked files and sends allow_remaining_issues only when that list is non-empty', async () => {
    const republish = vi.fn().mockResolvedValue({ result: 'republished' })
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', republish, publication: publication({ out_of_date: [
      staleItem({ file: 'a.docx', requires_remaining_issue_confirmation: true }),
      staleItem({ file: 'b.docx', requires_remaining_issue_confirmation: false })] }) })
    const boxes = c.querySelectorAll('input[type="checkbox"]')
    expect(boxes).toHaveLength(1)
    expect(boxes[0].closest('label').textContent).toContain('a.docx version')
    await click(boxes[0])
    await click(button(c, 'Publish updated copy and refresh reports'))
    expect(republish).toHaveBeenCalledExactlyOnceWith('scan1', { expected_artifacts: { 'a.docx': V2, 'b.docx': V2 },
      allow_remaining_issues: true, remaining_issue_files: ['a.docx'] })
  })

  it('surfaces which file still needs a remaining-issues confirmation after a 409', async () => {
    const failure = Object.assign(new Error('b.docx still has remaining issues. Refresh release status, confirm publishing that version with remaining issues recorded, then try again.'),
      { status: 409, code: 'remaining_issues_confirmation_required', files: ['b.docx'], refreshRequired: true })
    const onRefresh = vi.fn()
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', onRefresh, republish: vi.fn().mockRejectedValue(failure),
      publication: publication({ out_of_date: [staleItem({ file: 'b.docx' })] }) })
    await click(button(c, 'Publish updated copy and refresh reports'))
    expect(c.querySelector('[role="alert"]').textContent).toContain('b.docx still has remaining issues')
    await click(button(c, 'Refresh release status'))
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })

  it('explains the last failed attempt once, in one alert, and keeps the retry available', async () => {
    const republish = vi.fn().mockResolvedValue({ result: 'republished' })
    const failure = { failure_category: 'destination_unavailable', explanation: 'The SharePoint folder could not be reached', attempted_artifact_digest: `sha256:${V2}`, at: '2026-09-17T12:00:00Z' }
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', republish, publication: publication({ out_of_date: [staleItem({ last_attempt_failure: failure })] }) })
    const alerts = [...c.querySelectorAll('[role="alert"]')]
    expect(alerts).toHaveLength(1)
    expect(alerts[0].textContent).toContain('The last attempt to publish version sha256:8c84bfca…')
    expect(alerts[0].textContent).toContain('did not complete: The SharePoint folder could not be reached')
    expect(c.querySelector('[role="status"]').textContent).toBe('')
    const action = button(c, 'Publish updated copy and refresh reports')
    expect(action.disabled).toBe(false)
    await click(action)
    expect(republish).toHaveBeenCalledTimes(1)
    const busy = await render(ReleaseCorrectionNotice, { scanId: 'scan1', busy: true, publication: publication({ out_of_date: [staleItem({ last_attempt_failure: failure })] }) })
    expect(busy.querySelector('[role="alert"]')).toBeNull()
  })

  it('shows the blocked reason and keeps the action disabled when republish is not allowed', async () => {
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1',
      publication: publication({ can_republish: false, republish_blocked_reason: 'Approved changes are still applying.' }) })
    expect(c.textContent).toContain('Approved changes are still applying.')
    expect(button(c, 'Publish updated copy and refresh reports').disabled).toBe(true)
  })

  it('reads a legacy receipt without a digest as unconfirmed, never current, and offers no action', async () => {
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1',
      publication: { state: 'identity_unknown', out_of_date: [], identity_unknown: ['legacy.pdf'], can_republish: false, republish_blocked_reason: null } })
    expect(c.textContent).toContain('can’t confirm which version was published for legacy.pdf, or whether it is the current corrected copy')
    expect(c.textContent).toContain('reconcile that delivery')
    expect(c.textContent).not.toMatch(/\bcurrent copy\b|is current/i)
    expect(c.querySelectorAll('button')).toHaveLength(0)
    const file = { file: 'legacy.pdf', compliant: true, remediated_at: '2026-01-01T00:00:00Z', corrected_sha256: V2 }
    const result = { status: 'published', publication_state: 'identity_unknown', published_at: '2026-02-01T00:00:00Z' }
    expect(deliveryIsCurrent(file, result, { 'legacy.pdf': true })).toBe(false)
    expect(releaseReadiness(file, { results: { 'legacy.pdf': result } })).toMatchObject({ status: 'unconfirmed', label: 'Published version unconfirmed' })
    expect(releaseReadiness(file, { results: { 'legacy.pdf': result } }).reason).toMatch(/whether it is the current copy.*Reconcile that delivery/)
  })

  it('explains a 409 artifact_changed as a newer version and prompts a refresh', async () => {
    const failure = Object.assign(new Error('A newer corrected copy was saved after this page loaded. Refresh release status and review the latest version before publishing.'), { status: 409, code: 'artifact_changed' })
    const republish = vi.fn().mockRejectedValue(failure)
    const onRefresh = vi.fn()
    const c = await render(ReleaseCorrectionNotice, { scanId: 'scan1', republish, onRefresh, publication: publication() })
    await click(button(c, 'Publish updated copy and refresh reports'))
    const alert = c.querySelector('[role="alert"]')
    expect(alert.textContent).toContain('A newer corrected copy was saved')
    await click(button(c, 'Refresh release status'))
    expect(onRefresh).toHaveBeenCalledTimes(1)
  })
})

describe('republishRelease API', () => {
  afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals() })
  it('posts the exact body with provider headers and turns coded 409s into readable messages', async () => {
    vi.stubEnv('VITE_SIM', 'false'); vi.resetModules()
    const fetch = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ queued: true }) })
      .mockResolvedValueOnce({ ok: false, status: 409, statusText: 'Conflict', url: 'http://x/scans/s/release/republish', headers: new Headers(),
        json: async () => ({ detail: { code: 'artifact_changed', message: 'artifact changed' } }) })
      .mockResolvedValueOnce({ ok: false, status: 409, statusText: 'Conflict', url: 'http://x/scans/s/release/republish', headers: new Headers(),
        json: async () => ({ detail: { code: 'remaining_issues_confirmation_required', files: ['b.docx'], message: 'server text' } }) })
    vi.stubGlobal('fetch', fetch)
    const actual = await vi.importActual('./api.js')
    actual.setDriveToken('drive-token'); actual.setSPToken('sp-token')
    await actual.republishRelease('scan/1', { expected_artifacts: { 'report.docx': V2 }, allow_remaining_issues: true, remaining_issue_files: ['report.docx'] })
    const [url, init] = fetch.mock.calls[0]
    expect(new URL(url).pathname).toBe('/scans/scan%2F1/release/republish')
    expect(init.method).toBe('POST')
    expect(init.headers).toMatchObject({ 'X-Drive-Token': 'drive-token', 'X-SP-Token': 'sp-token', 'Content-Type': 'application/json' })
    expect(JSON.parse(init.body)).toEqual({ expected_artifacts: { 'report.docx': V2 }, allow_remaining_issues: true, remaining_issue_files: ['report.docx'] })
    const error = await actual.republishRelease('s', { expected_artifacts: {}, allow_remaining_issues: false }).catch((e) => e)
    expect(error.status).toBe(409)
    expect(error.code).toBe('artifact_changed')
    expect(error.refreshRequired).toBe(true)
    expect(error.message).toMatch(/newer corrected copy was saved/i)
    const confirm = await actual.republishRelease('s', { expected_artifacts: {}, allow_remaining_issues: false, remaining_issue_files: [] }).catch((e) => e)
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ expected_artifacts: {}, allow_remaining_issues: false, remaining_issue_files: [] })
    expect(confirm.code).toBe('remaining_issues_confirmation_required')
    expect(confirm.files).toEqual(['b.docx'])
    expect(confirm.refreshRequired).toBe(true)
    expect(confirm.message).toMatch(/^b\.docx still has remaining issues/)
    actual.setDriveToken(null); actual.setSPToken(null)
  })
})

describe('ReleaseReports currency', () => {
  it('says out-of-date reports are out of date and never "saved alongside the published files"', async () => {
    const read = vi.fn().mockResolvedValue({ status: 'completed', scan_id: 'scan1', release_id: 'rel1', can_regenerate: false,
      regeneration_blocked: 'Reports for this release are immutable.', currency: 'out_of_date', currency_reason: 'copy_changed_after_publication',
      out_of_date_files: [{ file: 'report.docx', reported_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}` }],
      reports: [{ name: 'report-checklist.pdf', url: 'https://example.com/r.pdf', report_kind: 'checklist', file: 'report.docx' }] })
    const c = await render(ReleaseReports, { scanId: 'scan1', read })
    const status = c.querySelector('[role="status"]').textContent
    expect(status).toContain('Reports are out of date')
    expect(c.textContent).not.toContain('Reports saved alongside the published files.')
    expect(c.textContent).toContain('sha256:02701e61…')
    expect(c.textContent).toContain('sha256:8c84bfca…')
    expect(c.textContent).toContain('Reports for this release are immutable.')
  })
  it('shows currency when the latest bundle failed, but not while a new bundle is being prepared', async () => {
    const stale = { scan_id: 'scan1', currency: 'out_of_date', currency_reason: 'copy_changed_after_publication',
      out_of_date_files: [{ file: 'report.docx', reported_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}` }], reports: [] }
    const failed = await render(ReleaseReports, { scanId: 'scan1', read: vi.fn().mockResolvedValue({ ...stale, status: 'failed' }) })
    expect(failed.querySelector('[role="status"]').textContent).toContain('Reports are out of date')
    expect(failed.querySelector('[role="status"]').textContent).toContain('report delivery also needs attention')
    const unknown = await render(ReleaseReports, { scanId: 'scan1', read: vi.fn().mockResolvedValue({ ...stale, status: 'failed', currency: 'unknown', out_of_date_files: [] }) })
    expect(unknown.querySelector('[role="status"]').textContent).toContain('can’t confirm which document version')
    const queued = await render(ReleaseReports, { scanId: 'scan1', read: vi.fn().mockResolvedValue({ ...stale, status: 'queued' }) })
    expect(queued.querySelector('[role="status"]').textContent).toBe('Preparing and saving reports alongside the published files…')
  })

  it('keeps the existing wording when the reports are current', async () => {
    const read = vi.fn().mockResolvedValue({ status: 'completed', currency: 'current', out_of_date_files: [],
      reports: [{ name: 'a.pdf', url: 'https://example.com/a.pdf', report_kind: 'scan_summary' }] })
    const c = await render(ReleaseReports, { scanId: 'scan1', read })
    expect(c.querySelector('[role="status"]').textContent).toBe('Reports saved alongside the published files.')
  })
})

describe('ReleaseHistory', () => {
  it('prefers the publication verdict over the raw execution status when present', async () => {
    const loadHistory = vi.fn().mockResolvedValue({ releases: [{ release_id: 'rel1', status: 'completed', publication: { state: 'out_of_date' },
      documents: [{ file: 'report.docx', status: 'published', publication_state: 'out_of_date', created: true, verification: 'passed' }] }] })
    const c = await render(ReleaseHistory, { loadHistory, loadManifest: vi.fn() })
    expect(c.querySelector('.release-history__facts').textContent).toContain('Published copy out of date')
    expect(c.querySelector('.release-history__facts').textContent).not.toContain('completed')
    expect(c.querySelector('.release-history__document').textContent).toContain('Published copy out of date')
  })
})

describe('Publish (Release tab) after a post-publication correction', () => {
  const file = { file: 'report.docx', compliant: true, remediated_at: '2026-09-17T11:00:00Z', corrected_sha256: V2, score: 100 }
  const row = (over = {}) => ({ file: 'report.docx', status: 'published', artifact_digest: `sha256:${V1}`, published_at: '2026-09-17T10:00:00Z',
    released_document_url: 'https://drive.example/report.docx', publication_state: 'out_of_date',
    published_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}`, ...over })
  const staleStatus = (pub = publication({ out_of_date: [staleItem({ requires_remaining_issue_confirmation: true })] })) => ({
    release_id: 'rel1', roots: [], documents: [row()], publication: pub })
  const currentStatus = { release_id: 'rel1', roots: [], publication: { state: 'current', out_of_date: [], identity_unknown: [], can_republish: false, republish_blocked_reason: null },
    documents: [row({ artifact_digest: `sha256:${V2}`, published_artifact_digest: `sha256:${V2}`, publication_state: 'current', published_at: '2026-09-17T12:00:00Z' })] }
  const staleReports = { status: 'completed', scan_id: 'scan1', release_id: 'rel1', currency: 'out_of_date', currency_reason: 'copy_changed_after_publication',
    out_of_date_files: [{ file: 'report.docx', reported_artifact_digest: `sha256:${V1}`, current_artifact_digest: `sha256:${V2}` }],
    reports: [{ name: 'summary.pdf', url: 'https://drive.example/summary.pdf', report_kind: 'scan_summary' }] }
  const publicationCells = (c) => [...c.querySelectorAll('.release-completion-documents tbody td strong')].map((el) => el.textContent)

  it('shows the out-of-date copy by digest, hides Published, and republishes then reloads status and reports', async () => {
    getAutomaticRelease.mockResolvedValue({ authorization: null })
    getReleaseStatus.mockResolvedValueOnce(staleStatus()).mockResolvedValue(currentStatus)
    getReleaseReports.mockResolvedValue(staleReports)
    republishRelease.mockResolvedValue({ queued: true, release_id: 'rel1', published: [{ file: 'report.docx', status: 'queued' }] })
    const onPublish = vi.fn()
    const c = await render(Publish, { run: { id: 'scan1', source: 'drive' }, me: { email: 'a@example.com' }, files: [file], onPublish })
    expect(c.textContent).toContain('sha256:02701e61…')
    expect(c.textContent).toContain('sha256:8c84bfca…')
    expect(publicationCells(c)).toEqual(['Published copy out of date'])
    expect(c.textContent).not.toContain('Current corrected copy confirmed')
    expect(c.textContent).not.toContain('Reports saved alongside the published files.')
    expect(c.textContent).toContain('Reports are out of date')
    expect(c.textContent).toContain('1 published copy is out of date')
    expect(c.textContent).toContain('Open earlier published copy')
    expect(button(c, 'Open published copy')).toBeUndefined()
    const statusCalls = getReleaseStatus.mock.calls.length
    const reportCalls = getReleaseReports.mock.calls.length
    getReleaseReports.mockResolvedValue({ ...staleReports, currency: 'current', out_of_date_files: [] })
    const action = button(c, 'Publish updated copy and refresh reports')
    expect(action.disabled).toBe(true)
    await click(c.querySelector('.release-correction-notice input[type="checkbox"]'))
    await click(action)
    expect(republishRelease).toHaveBeenCalledExactlyOnceWith('scan1', { expected_artifacts: { 'report.docx': V2 }, allow_remaining_issues: true, remaining_issue_files: ['report.docx'] })
    expect(getReleaseStatus.mock.calls.length).toBeGreaterThan(statusCalls)
    expect(getReleaseReports.mock.calls.length).toBeGreaterThan(reportCalls)
    expect(onPublish).toHaveBeenCalledExactlyOnceWith('report.docx')
    expect(c.querySelector('.release-correction-notice')).toBeNull()
    expect(publicationCells(c)).toEqual(['Delivered'])
    expect(c.textContent).toContain('Reports saved alongside the published files.')
  })

  it('settles a failed republish as out of date: no endless "publishing", the failure shown, the retry available', async () => {
    const failure = { failure_category: 'artifact_changed', explanation: 'The authorized corrected artifact changed; confirm again', attempted_artifact_digest: `sha256:${V2}`, at: '2026-09-17T12:00:00Z' }
    const pub = publication()
    const failedStatus = { release_id: 'rel1', roots: [], documents: [row()], publication: { ...pub, out_of_date: [staleItem({ last_attempt_failure: failure })] } }
    getAutomaticRelease.mockResolvedValue({ authorization: null })
    getReleaseReports.mockResolvedValue(staleReports)
    getReleaseStatus.mockResolvedValueOnce({ release_id: 'rel1', roots: [], documents: [row()], publication: pub }).mockResolvedValue(failedStatus)
    republishRelease.mockResolvedValue({ result: 'republished', results: [{ file: 'report.docx', status: 'failed', failure_category: 'artifact_changed' }] })
    const onPublish = vi.fn()
    const c = await render(Publish, { run: { id: 'scan1', source: 'drive' }, me: { email: 'a@example.com' }, files: [file], onPublish })
    const before = getReleaseStatus.mock.calls.length
    await click(button(c, 'Publish updated copy and refresh reports'))
    // Settled on the first poll: the delivered V1 receipt is unchanged, and that is the answer.
    expect(getReleaseStatus.mock.calls.length).toBe(before + 1)
    expect(button(c, 'Publishing updated copy…')).toBeUndefined()
    expect(button(c, 'Publish updated copy and refresh reports').disabled).toBe(false)
    expect(c.textContent).not.toMatch(/still publishing/i)
    expect(c.textContent).not.toContain('Publishing started for report.docx.')
    expect(c.querySelector('.release-correction-notice [role="alert"]').textContent).toContain('did not complete: The authorized corrected artifact changed; confirm again')
    expect(publicationCells(c)).toEqual(['Published copy out of date'])
    expect(onPublish).not.toHaveBeenCalled()
  })

  it('does not let automatic batch membership say "published" over an out-of-date receipt', async () => {
    const batch = { available: true, authorization_id: 'auto', run_id: 'run', scope_id: 'auto', revision: 1, total: 1, delivered: 1, remaining: 0,
      status: 'completed', buckets: { waiting: 0, processing: 0, published: 1, failed: 0, skipped: 0, unclassified: 0 }, file_membership: { 'report.docx': 'published' } }
    const authorization = { id: 'auto', status: 'completed', run_id: 'run', files: ['report.docx'], destination: { provider: 'drive' }, batch_progress: batch }
    getReleaseReports.mockResolvedValue(staleReports)
    for (const [status, expected] of [[staleStatus(), 'Published copy out of date'], [currentStatus, 'Published']]) {
      getAutomaticRelease.mockResolvedValue({ authorization, run_id: 'run' })
      getReleaseStatus.mockResolvedValue(status)
      const c = await render(Publish, { run: { id: 'scan1', source: 'drive' }, me: { email: 'a@example.com' }, files: [file] })
      expect(publicationCells(c)).toEqual([expected])
      if (expected !== 'Published') expect(c.textContent).not.toContain('Current corrected copy confirmed')
      else expect(c.textContent).toContain('Current corrected copy confirmed')
      await unmountAll()
    }
  })
})
