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
const h = vi.hoisted(() => ({
  renderReportPdf: null, exportFileReportHtml: null, buildFileReportModel: null, diffs: null,
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
    expect(d.diffs).toEqual(DIFFS)
    expect(d.diffsComplete).toBe(true)
    expect(d.diffsTotal).toBe(2)
    expect(d.identity).toMatchObject({ scanId: 'scan-1', file: 'barn.pdf', correctedSha256: SHA, artifactVersion: SHA, platformVersion: '9.9.9' })
    const issues = d.rows.flatMap((r) => r.fileIssues)
    expect(issues.map((i) => i.detail).sort()).toEqual(['Image A has no alt', 'Image B has no alt'])
    expect(d.rows.find((r) => r.id === '1.1.1').verified).toBe(true)
    expect(d.reviews).toEqual({})
    expect(d.assignee).toBe('owner@example.com')
    expect(h.renderReportPdf).toHaveBeenCalledWith({ scanId: 'scan-1', kind: 'file', file: 'barn.pdf', mode: 'full', model: { model: true, mode: 'full' } })
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

  // An HTML copy WAS delivered, so this is a note, not an error — but it must never read as
  // "PDF generated" either.
  it('a PDF replaced by an HTML copy says so without claiming the PDF', async () => {
    h.renderReportPdf = vi.fn(async () => ({ ok: false, fallback: 'html', message: 'The report service is unavailable.' }))
    const c = await mount()
    await act(async () => { btn(c, 'Summary — PDF').click() })
    await flush()
    const status = [...c.querySelectorAll('[role="status"]')].find((s) => /Summary \(PDF\)/.test(s.textContent))
    expect(status?.textContent).toMatch(/was not generated; an HTML copy was downloaded instead/)
    expect(status?.textContent).not.toMatch(/\(PDF\) generated\./)
    expect([...c.querySelectorAll('[role="alert"]')].some((a) => /Summary \(PDF\)/.test(a.textContent))).toBe(false)
  })
})

describe('FileDrawer mounts Changes to confirm', () => {
  it('one card per saved change, with decision controls bound to the recorded version', async () => {
    const c = await mount()
    const panel = c.querySelector('section.chgreview')
    expect(panel).toBeTruthy()
    expect(panel.querySelectorAll('[data-change-id]')).toHaveLength(2)
    expect(panel.textContent).toMatch(/recorded against document version ffffffffffff/)
    expect([...panel.querySelectorAll('button')].map((b) => b.textContent)).toContain('Unable to verify')
  })

  it('is absent for a file with no saved changes', async () => {
    h.diffs = []
    const c = await mount()
    expect(c.querySelector('section.chgreview')).toBeNull()
  })
})
