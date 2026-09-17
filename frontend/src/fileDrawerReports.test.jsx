/**
 * FileDrawer's report menu and "Changes to confirm" panel, mounted in the real drawer (jsdom).
 *
 * The browser preview serves the SHARED checkout, not this worktree (CLAUDE.md), so this DOM-level
 * test is the verification of the wiring: the three modes reach the server PDF renderer and the
 * HTML exporter with the right mode and the live payload, a failure is visible, and the review
 * panel is actually mounted where saved changes exist.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

afterEach(unmountAll)

const SHA = 'f'.repeat(64)
const DIFFS = [
  { rule_id: '1.1.1', seq: 0, before: '(no alt)', after: 'A red barn', note: 'n', verified: true },
  { rule_id: '1.1.1', seq: 1, before: '(no alt)', after: 'A blue barn', note: 'n', verified: true },
]
// The server's report facts for barn.pdf (api/report_facts.py). `savedChanges` is the authority
// for what the review panel and the report show — it includes changes the AI applied that nothing
// re-scanned, which /remediation-diffs never returns.
const SAVED = [
  { id: 'barn.pdf::1.1.1::0', ruleId: '1.1.1', sc: '1.1.1', seq: 0, locator: null, before: '(no alt)', after: 'A red barn', note: 'n', verification: 'verified', verificationDetail: 'cleared the re-scan', artifactSha256: 'f'.repeat(64), valueClipped: false, changeDigest: 'cd0', findingIds: null, source: 'remediation_diff' },
  { id: 'barn.pdf::1.1.1::1', ruleId: '1.1.1', sc: '1.1.1', seq: 1, locator: null, before: '(no alt)', after: 'A blue barn', note: 'n', verification: 'verified', verificationDetail: 'cleared the re-scan', artifactSha256: 'f'.repeat(64), valueClipped: false, changeDigest: 'cd1', findingIds: null, source: 'remediation_diff' },
]
const FACTS = (over = {}) => ({
  factsVersion: 1, factsDigest: 'file-digest-1', generatedAt: '2026-09-17T12:00:00Z',
  identity: {
    scanId: 'scan-1', file: 'barn.pdf', sourceChecksum: 'f'.repeat(64), sourceChecksumKind: 'sha256',
    sourceSha256: null, correctedSha256: 'f'.repeat(64),
    currentArtifact: { kind: 'corrected', sha256: 'f'.repeat(64) },
    remediatedAt: null, platformVersion: '9.9.9', targetLevel: 'AA',
    scopeDigest: 'scope-1', scanScope: null, rubricHash: null,
  },
  assessment: { state: 'assessed', assessedAt: null, score: 70, engine: 'pdf', artifactAssessed: 'source', findingsComplete: true, findingsTotal: 2, stateReason: null },
  findings: [],
  savedChanges: SAVED,
  savedChangesComplete: true, savedChangesTotal: SAVED.length, savedChangesLimit: 500,
  reviews: {},
  accounting: { findingsTotal: 2, findingsOpen: 2, findingsResolvedVerified: null, resolutionLedger: 'none', savedChangesVerified: 2, savedChangesUnverified: 0, humanReviews: { pending: 2, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 } },
  previous: null, previousReason: 'No earlier assessment of this document is recorded.',
  limits: { valueMaxChars: 2000, savedChangesLimit: 500 },
  ...over,
})

const h = vi.hoisted(() => ({
  renderReportPdf: null, exportFileReportHtml: null, buildFileReportModel: null, diffs: null,
  facts: null, artifactPage: null,
}))
const inert = async () => null

vi.mock('./reportRenderClient.js', () => ({
  REPORT_MODES: { summary: 'Summary', reviewer: 'Reviewer packet', full: 'Full evidence' },
  renderReportPdf: (...a) => h.renderReportPdf(...a),
}))
vi.mock('./htmlReport.js', () => ({ exportFileReportHtml: (...a) => h.exportFileReportHtml(...a) }))
vi.mock('./reportModel.js', () => ({ buildFileReportModel: (...a) => h.buildFileReportModel(...a) }))
vi.mock('./api.js', () => ({
  SIM: false,
  listScanDecisions: inert, openTraceUrl: () => {},
  fetchChangeReviews: async () => ({ artifact: { currentSha256: SHA, correctedSha256: SHA }, reviews: {} }),
  putChangeReview: inert,
  getDecisions: async () => ({ 'barn.pdf': { assignee: 'owner@example.com' } }),
  getScan: async () => ({ run: {}, files: [] }),
  getScanDiff: async () => ({ no_baseline: true }),
  getFileRemediationDiffs: async () => h.diffs,
  getFileReportFacts: (...a) => h.facts(...a),
  getFileArtifactPage: (...a) => h.artifactPage(...a),
  getConfig: async () => ({ version: '9.9.9' }),
  aiProvenance: inert, approveDisposition: inert, assessScan: inert,
  autoPopulateHitlQueue: inert, clearDeadJobs: inert, clearMyScope: inert,
  confirmCriterion: inert, createCampaign: inert, createDispositionPolicy: inert,
  createScopeRule: inert, deleteScopeRule: inert, disposeCriterion: inert,
  downloadRemediated: inert, executeDispositionPolicy: inert, explainFinding: inert,
  fetchCodeset: inert, fetchEligibility: inert, fetchScopeRules: inert,
  fetchScopeSelectors: inert, fetchScopedEligibility: inert, getAiCosts: inert,
  getAiProviders: inert, getAiStatus: inert, getAllowlist: inert, getAppliedFixes: inert,
  getCapability: inert, getDigest: inert, getDocumentTimeline: async () => [],
  getEstate: inert, getExamined: inert, getFileContent: inert, getFileContrast: inert,
  getFileGeometry: inert, getFilePage: inert, getFilePdfContrast: inert,
  getFileRemediationState: inert, getFileResize: inert,
  getFileStatus: inert, getFileThumbnail: inert, getFileTraceData: inert,
  getHitlAnalytics: inert, getInventoryDiff: inert, getJob: inert, getJobs: inert,
  getMyScope: inert, getQueueJob: inert, getRemediationStatus: inert, getRubric: inert,
  getRules: inert, getScanAiCalls: inert, getScanLocations: inert, getScanPii: inert,
  getScanRemediationDiffs: inert, getScanStatus: inert, getScanTraces: inert, getSchedule: inert,
  getSessionTraceData: inert, getSettings: inert, getSourceStatus: inert, getTraceStatus: inert,
  inviteTester: inert, listAllHitl: inert, listCampaigns: inert, listDispositionApprovals: inert,
  listDispositionAudit: inert, listDispositionPolicies: inert, listDispositions: inert,
  listFolders: inert, listHitlQueue: inert, listSharePointDrives: inert,
  listSharePointSites: inert, listSpFolders: inert, markRemediated: inert, openReport: inert,
  previewDispositionPolicy: inert, publishAllFiles: inert, publishFile: inert,
  putAiProvider: inert, putMyScope: inert, putSchedule: inert, queueHitlReview: inert,
  queueHitlVerify: inert, refreshScanDriveToken: inert, rejectDisposition: inert,
  remediateScan: inert, rescoreFile: inert, resetDemoData: inert, setAllowlist: inert,
  setCampaignStatus: inert, setDispositionPolicyEnabled: inert, setLangfuseBase: inert,
  setScanLocations: inert, setScopeRuleEnabled: inert, setWorkers: inert, suggestFix: inert,
  getWorkerReplicas: inert, setWorkerReplicas: inert,
  updateHitlItem: inert, updateSettings: inert, uploadToDrive: inert,
  uploadToSharePoint: inert, validateAlt: inert,
}))

const { default: FileDrawer } = await import('./FileDrawer.jsx')

const doc = {
  file: 'barn.pdf', status: 'analysed', score: 70, compliant: false, engine: 'pdf', remediated_at: '2026-09-17',
  issues: [
    { wcag: 'SC_1_1_1', rule_id: 'img', severity: 'SERIOUS', detail: 'Image A has no alt', page: 1 },
    { wcag: 'SC_1_1_1', rule_id: 'img', severity: 'SERIOUS', detail: 'Image B has no alt', page: 4 },
  ],
}
const flush = async () => { await act(async () => { for (let i = 0; i < 15; i++) await new Promise((r) => setTimeout(r, 0)) }) }

async function mount() {
  const { root, container } = createTestRoot()
  await act(async () => { root.render(createElement(FileDrawer, { file: doc, scanId: 'scan-1', onClose: () => {} })) })
  await flush()
  return container
}
const btn = (c, name) => c.querySelector(`button[aria-label="${name}"]`)

beforeEach(() => {
  h.diffs = DIFFS
  h.facts = vi.fn(async () => FACTS())
  h.artifactPage = vi.fn(async () => null)
  h.renderReportPdf = vi.fn(async () => ({ ok: true, filename: 'x.pdf' }))
  h.exportFileReportHtml = vi.fn(async () => undefined)
  h.buildFileReportModel = vi.fn((d) => ({ model: true, mode: d.mode }))
})

describe('FileDrawer report menu', () => {
  it('replaces the two old export buttons with one labeled menu of three modes × PDF/HTML', async () => {
    const c = await mount()
    expect([...c.querySelectorAll('button')].some((b) => /Certification PDF|HTML report/.test(b.textContent))).toBe(false)
    const menu = [...c.querySelectorAll('details.reports-menu')].find((d) => d.querySelector('summary').textContent === 'Document report')
    expect(menu).toBeTruthy()
    const names = [...menu.querySelectorAll('button')].map((b) => b.getAttribute('aria-label'))
    expect(names).toEqual(['Summary — PDF', 'Summary — HTML', 'Reviewer packet — PDF', 'Reviewer packet — HTML', 'Full evidence — PDF', 'Full evidence — HTML'])
  })

  it('PDF goes to the server renderer with the mode and a model built from the live payload', async () => {
    const c = await mount()
    await act(async () => { btn(c, 'Full evidence — PDF').click() })
    await flush()
    expect(h.buildFileReportModel).toHaveBeenCalledTimes(1)
    const d = h.buildFileReportModel.mock.calls[0][0]
    expect(d.mode).toBe('full')
    // The saved changes come from the SERVER's facts, under the server's ids.
    expect(d.diffs.map((x) => x.id)).toEqual(['barn.pdf::1.1.1::0', 'barn.pdf::1.1.1::1'])
    expect(d.diffs.map((x) => x.after)).toEqual(['A red barn', 'A blue barn'])
    expect(d.diffsComplete).toBe(true)
    expect(d.diffsTotal).toBe(2)
    expect(d.facts).toBeTruthy()
    expect(d.identity).toMatchObject({
      scanId: 'scan-1', file: 'barn.pdf', correctedSha256: SHA, artifactVersion: SHA,
      platformVersion: '9.9.9', factsDigest: 'file-digest-1',
      currentArtifact: { kind: 'corrected', sha256: SHA },
    })
    const issues = d.rows.flatMap((r) => r.fileIssues)
    expect(issues.map((i) => i.detail).sort()).toEqual(['Image A has no alt', 'Image B has no alt'])
    expect(d.rows.find((r) => r.id === '1.1.1').verified).toBe(true)
    expect(d.reviews).toEqual({})
    expect(d.assignee).toBe('owner@example.com')
    expect(h.renderReportPdf).toHaveBeenCalledWith({ scanId: 'scan-1', kind: 'file', file: 'barn.pdf', mode: 'full', model: { model: true, mode: 'full' }, factsDigest: 'file-digest-1' })
    expect(h.exportFileReportHtml).not.toHaveBeenCalled()
  })

  it('HTML goes to the HTML exporter with the chosen mode', async () => {
    const c = await mount()
    await act(async () => { btn(c, 'Reviewer packet — HTML').click() })
    await flush()
    expect(h.exportFileReportHtml).toHaveBeenCalledTimes(1)
    expect(h.exportFileReportHtml.mock.calls[0][0].mode).toBe('reviewer')
    expect(h.renderReportPdf).not.toHaveBeenCalled()
  })

  it('a refused PDF is shown on screen', async () => {
    h.renderReportPdf = vi.fn(async () => ({ ok: false, fallback: 'none', message: 'The report service is unavailable.' }))
    const c = await mount()
    await act(async () => { btn(c, 'Summary — PDF').click() })
    await flush()
    const alert = [...c.querySelectorAll('[role="alert"]')].find((a) => /Summary \(PDF\)/.test(a.textContent))
    expect(alert?.textContent).toMatch(/The report service is unavailable/)
  })

  // An HTML copy WAS delivered — but the format that was asked for was not produced, and the
  // menu is the only thing that tells the reader which. It says so where a failure is said.
  it('a PDF replaced by an HTML copy says so without claiming the PDF', async () => {
    h.renderReportPdf = vi.fn(async () => ({ ok: false, fallback: 'html', message: 'The report service is unavailable.' }))
    const c = await mount()
    await act(async () => { btn(c, 'Summary — PDF').click() })
    await flush()
    const alert = [...c.querySelectorAll('[role="alert"]')].find((a) => /Summary \(PDF\)/.test(a.textContent))
    expect(alert?.textContent).toMatch(/was NOT generated/)
    expect(alert?.textContent).toMatch(/HTML copy of the same report was downloaded instead/)
    const status = [...c.querySelectorAll('[role="status"]')].find((s) => /Summary \(PDF\)/.test(s.textContent))
    expect(status?.textContent ?? '').not.toMatch(/generated\./)
  })

  it('a 409 tells the reader the document changed and to regenerate', async () => {
    h.renderReportPdf = vi.fn(async () => ({
      ok: false, fallback: 'none', status: 409, regenerate: true,
      message: 'The document changed since this report was prepared — regenerate the report so it describes the current version.',
    }))
    const c = await mount()
    await act(async () => { btn(c, 'Reviewer packet — PDF').click() })
    await flush()
    const alert = [...c.querySelectorAll('[role="alert"]')].find((a) => /Reviewer packet \(PDF\)/.test(a.textContent))
    expect(alert?.textContent).toMatch(/changed since this report was prepared/)
    expect(alert?.classList.contains('reportmode-regenerate')).toBe(true)
  })
})

describe('FileDrawer renders what changed since the previous assessment', () => {
  // The per-document baseline is the one stream D actually supplies (its estate endpoint answers
  // "scan-level comparison is reported per document"), so this is where a live comparison has to
  // work. buildComparison is the REAL builder, run on the REAL live payload the drawer produced.
  it('a comparable baseline comes through the live payload and compares finding by finding', async () => {
    const PREV = {
      scanId: 'scan-0', generatedAt: '2026-09-01T00:00:00Z', sha256: null,
      scopeDigest: 'scope-1',
      findings: [
        { id: 'fid-stays', ruleId: 'img-alt', sc: '1.1.1', detail: 'Image A has no alt', location: { label: 'Page 1', page: 1 } },
        { id: 'fid-gone', ruleId: 'title', sc: '2.4.2', detail: 'No document title', location: { label: 'Document', page: null } },
      ],
    }
    h.facts = vi.fn(async () => FACTS({
      previous: PREV,
      previousReason: null,
      findings: [
        { id: 'fid-stays', ruleId: 'img-alt', sc: '1.1.1', detail: 'Image A has no alt', severity: 'SERIOUS', location: { label: 'Page 1', page: 1 }, state: 'open', stateReason: null },
        { id: 'fid-new', ruleId: 'contrast', sc: '1.4.3', detail: 'Low contrast heading', severity: 'SERIOUS', location: { label: 'Page 2', page: 2 }, state: 'open', stateReason: null },
      ],
    }))
    const c = await mount()
    await act(async () => { btn(c, 'Full evidence — PDF').click() })
    await flush()
    const d = h.buildFileReportModel.mock.calls[0][0]
    expect(d.previous.scanId).toBe('scan-0')
    expect(d.previousReason).toBeNull()
    expect(d.scope).toEqual({ scopeDigest: 'scope-1', targetLevel: 'AA' })
    const { buildComparison } = await import('./reportEvidence.js')
    const cmp = buildComparison(d.previous, { file: 'barn.pdf', scope: d.scope, findings: d.currentFindings })
    expect(cmp.status).toBe('compared')
    expect(cmp.resolved.map((r) => r.id)).toEqual(['fid-gone'])
    expect(cmp.introduced.map((r) => r.id)).toEqual(['fid-new'])
    expect(cmp.persisting).toBe(1)
  })

  it('no baseline reads unknown, with the server\u2019s reason', async () => {
    const c = await mount()
    await act(async () => { btn(c, 'Summary — PDF').click() })
    await flush()
    const d = h.buildFileReportModel.mock.calls[0][0]
    expect(d.previous).toBeNull()
    expect(d.previousReason).toBe('No earlier assessment of this document is recorded.')
    const { buildComparison } = await import('./reportEvidence.js')
    expect(buildComparison(d.previous, { file: 'barn.pdf', scope: d.scope, findings: d.currentFindings }).status).toBe('unknown')
  })
})

describe('FileDrawer mounts Changes to confirm', () => {
  it('one card per saved change, with decision controls bound to the recorded version', async () => {
    const c = await mount()
    const panel = c.querySelector('section.chgreview')
    expect(panel).toBeTruthy()
    expect(panel.querySelectorAll('[data-change-id]')).toHaveLength(2)
    expect(panel.textContent).toMatch(/recorded against the saved corrected copy ffffffffffff/)
    expect([...panel.querySelectorAll('button')].map((b) => b.textContent)).toContain('Unable to verify')
  })

  it('is absent for a file with no saved changes', async () => {
    h.diffs = []
    h.facts = vi.fn(async () => FACTS({ savedChanges: [], savedChangesTotal: 0 }))
    const c = await mount()
    expect(c.querySelector('section.chgreview')).toBeNull()
  })

  it('shows the changes the AI applied that nothing re-scanned \u2014 the ones needing review', async () => {
    const unverified = {
      id: 'barn.pdf::1.3.1::u00112233445566aa', ruleId: '1.3.1', sc: '1.3.1', seq: null, locator: 'p#3',
      before: 'Heading', after: 'Heading (H2)', note: null,
      verification: 'not_verified', verificationDetail: 'Applied to the saved copy; no re-scan has confirmed it.',
      artifactSha256: SHA, valueClipped: false, changeDigest: 'cdu', findingIds: null, source: 'unverified_changes',
    }
    h.facts = vi.fn(async () => FACTS({ savedChanges: [...SAVED, unverified], savedChangesTotal: 3 }))
    const c = await mount()
    const panel = c.querySelector('section.chgreview')
    expect(panel.querySelectorAll('[data-change-id]')).toHaveLength(3)
    const card = panel.querySelector('[data-change-id="barn.pdf::1.3.1::u00112233445566aa"]')
    expect(card, 'the unverified saved change never reached the drawer').toBeTruthy()
    expect(card.querySelector('.chgunverified').textContent).toBe('AI applied \u00b7 not verified')
  })
})
