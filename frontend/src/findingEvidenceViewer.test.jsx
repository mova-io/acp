/**
 * A report's finding link, opened in the REAL App: sign-in, then exactly that record.
 *
 * DOM-LEVEL, NOT BROWSER-LEVEL, and not by choice: the preview server runs vite rooted at the
 * shared checkout whatever worktree you are in (CLAUDE.md), so a screenshot would be evidence
 * about `main`, not about this branch.
 *
 * What is under test is the live path, end to end on the client: a link built by
 * attachEvidenceLinks from the server's facts → the URL → App.jsx → sign-in → the viewer → the
 * owner-scoped facts request → the exact record by id. And the refusals, each of which was the
 * easy way to make a link "work": a missing record is said to be missing (never the first finding
 * of the same criterion), a changed document is said to have changed, a Word paragraph gets no
 * page picture, and an out-of-range page is the server's own refusal, not the last page.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

globalThis.__BUILD_TIME__ = '2026-09-17T00:00:00.000Z'
globalThis.__BUILD_VERSION__ = '2026.9.17'

const SRC = 'a'.repeat(64)
const NEWER = 'b'.repeat(64)
const COR = 'c'.repeat(64)

// Two findings for the SAME criterion on the same document — the case where "show the first
// finding of this criterion" would look right and be wrong.
const pdfFacts = (over = {}) => ({
  factsVersion: 1, factsDigest: 'd'.repeat(64),
  identity: { scanId: 'scan 1', file: 'Board/Q1 #2 & notes.pdf', sourceSha256: SRC, correctedSha256: COR, currentArtifact: { kind: 'corrected', sha256: COR } },
  findings: [
    { id: 'f-first', ruleId: 'PDF-ALT-001', sc: '1.1.1', detail: 'Figure on the cover has no description', severity: 'SERIOUS', recommendedAction: 'Describe the cover photo', location: { label: 'Page 1 · figure 1', kind: 'object', page: 1, slide: null, sheet: null, cell: null, objectId: 'figure:1:0', element: 'pdf:fig:1:0', raw: 'pdf:fig:1:0' }, state: 'open', stateReason: 'recorded by the current assessment' },
    { id: 'f-second', ruleId: 'PDF-ALT-001', sc: '1.1.1', detail: 'Chart on page 12 has no description', severity: 'SERIOUS', recommendedAction: 'Summarise the chart trend', location: { label: 'Page 12 · figure 3', kind: 'object', page: 12, slide: null, sheet: null, cell: null, objectId: 'figure:12:2', element: 'pdf:fig:12:2', raw: 'pdf:fig:12:2' }, state: 'open', stateReason: 'recorded by the current assessment' },
    { id: 'f-nowhere', ruleId: 'PDF-LANG', sc: '3.1.1', detail: 'No document language', severity: 'MODERATE', recommendedAction: null, location: null, state: 'open', stateReason: null },
  ],
  savedChanges: [
    { id: 'Board/Q1 #2 & notes.pdf::1.1.1::0', ruleId: '1.1.1', sc: '1.1.1', before: '', after: 'A bar chart of revenue', verification: null, artifactSha256: COR, location: null, locator: null },
  ],
  savedChangesComplete: true,
  ...over,
})

let FACTS = pdfFacts()
const getFileReportFacts = vi.fn(async () => FACTS)
const getExactArtifactPage = vi.fn(async (_s, _f, _sha, page) => ({ ok: true, blob: new Blob(['png'], { type: 'image/png' }), page, renderedPage: page, pageCount: 12, sha256: SRC }))

vi.mock('./api.js', async (importActual) => {
  const actual = await importActual()
  return {
    ...actual,
    getConfig: vi.fn(async () => ({ auth: 'demo' })),
    getRubric: vi.fn(async () => ({ target: 'WCAG 2.1 AA', hash: 'abcdef0123' })),
    getSources: vi.fn(async () => []),
    listScans: vi.fn(async () => []),
    getActiveScan: vi.fn(async () => null),
    getSettings: vi.fn(async () => ({ scan_scope: '' })),
    getDecisions: vi.fn(async () => ({})),
    getMyAccess: vi.fn(async () => null),
    getWorkspaceBootstrap: vi.fn(async () => ({
      me: { email: 'rev@hosp.org', is_admin: false, is_scope_owner: false, access: null },
      scan_id: null, scan_status: null, revision: 0, overview: null, scans: [], active_job: {},
    })),
    getFileReportFacts,
    getExactArtifactPage,
  }
})

const { default: App } = await import('./App.jsx')
const { default: FindingEvidenceViewer, RECORD_MISSING } = await import('./FindingEvidenceViewer.jsx')
const { attachEvidenceLinks, evidenceHref, parseEvidenceHref, EVIDENCE_TARGET_KEY } = await import('./evidenceLink.js')

URL.createObjectURL = vi.fn(() => 'blob:acp/evidence-page')
URL.revokeObjectURL = vi.fn()

const flush = async () => { for (let i = 0; i < 6; i++) await act(async () => { await Promise.resolve() }) }
const byText = (c, sel, re) => [...c.querySelectorAll(sel)].find((e) => re.test(e.textContent))
const setUrl = (href) => window.history.replaceState(null, '', href)

beforeEach(() => {
  sessionStorage.clear(); FACTS = pdfFacts()
  getFileReportFacts.mockClear(); getExactArtifactPage.mockClear()
  setUrl('/')
})
afterEach(async () => { await unmountAll(); sessionStorage.clear(); setUrl("/") })

async function mountApp() {
  const { container: c, root } = createTestRoot()
  await act(async () => { root.render(createElement(App)) })
  await flush()
  return { c, root }
}
async function signIn(c) {
  await act(async () => { byText(c, 'button', /Sign in with SSO/).click() })
  await flush()
}
async function mountViewer(target) {
  const { container: c, root } = createTestRoot()
  const onClose = vi.fn()
  await act(async () => { root.render(createElement(FindingEvidenceViewer, { target, onClose })) })
  await flush()
  return { c, onClose }
}
const linkFor = (facts, i) => attachEvidenceLinks(facts).findings[i].location.href

// ── the live mount, and sign-in ───────────────────────────────────────────────

describe('App opens the exact record a report links to', () => {
  it('holds the target through sign-in, then shows exactly that finding', async () => {
    setUrl(linkFor(FACTS, 1))
    const { c } = await mountApp()
    // Signed out: the sign-in screen, and no evidence request has been made for anybody.
    expect(byText(c, 'button', /Sign in with SSO/)).toBeTruthy()
    expect(getFileReportFacts).not.toHaveBeenCalled()
    await signIn(c)
    expect(getFileReportFacts).toHaveBeenCalledWith('scan 1', 'Board/Q1 #2 & notes.pdf')
    expect(c.textContent).toContain('Chart on page 12 has no description')
    expect(c.textContent).toContain('Page 12 · figure 3')
    expect(c.textContent).toContain('Summarise the chart trend')
    // ...and NOT the other finding of the same criterion
    expect(c.textContent).not.toContain('Figure on the cover')
  })

  it('link → parse → viewer: every finding link resolves to its own record', async () => {
    for (const [i, detail] of [[0, 'Figure on the cover'], [1, 'Chart on page 12'], [2, 'No document language']]) {
      await unmountAll()
      const href = linkFor(FACTS, i)
      const target = parseEvidenceHref(href)
      expect(target.findingId).toBe(FACTS.findings[i].id)
      const { c } = await mountViewer(target)
      expect(c.textContent).toContain(detail)
    }
  })

  it('survives a sign-in that drops the query string (redirect back to "/")', async () => {
    const href = linkFor(FACTS, 1)
    setUrl(href)
    await mountApp()                      // first load: signed out, target captured
    expect(JSON.parse(sessionStorage.getItem(EVIDENCE_TARGET_KEY)).href).toBe(href)
    await unmountAll()
    setUrl('/')                            // the identity provider returns to the bare origin
    const { c } = await mountApp()
    await signIn(c)
    expect(c.textContent).toContain('Chart on page 12 has no description')
    expect(window.location.search).toBe(href.slice(1))   // put back in the address bar
    expect(sessionStorage.getItem(EVIDENCE_TARGET_KEY)).toBeNull()   // consumed once shown
  })

  it('a URL that says something else wins over a saved target', async () => {
    setUrl(linkFor(FACTS, 1))
    await mountApp()
    await unmountAll()
    setUrl('/?tab=assess')
    const { c } = await mountApp()
    await signIn(c)
    expect(getFileReportFacts).not.toHaveBeenCalled()
    expect(c.querySelector('.evview')).toBeNull()
    expect(sessionStorage.getItem(EVIDENCE_TARGET_KEY)).toBeNull()
  })

  it('"Back to ACP" leaves the viewer and strips only the evidence parameters', async () => {
    setUrl(`${linkFor(FACTS, 0)}&a11y=1`)
    const { c } = await mountApp()
    await signIn(c)
    await act(async () => { byText(c, 'button', /Back to ACP/).click() })
    await flush()
    expect(c.querySelector('.evview')).toBeNull()
    expect(window.location.search).toBe('?a11y=1')
    expect(c.querySelectorAll('[role="tab"]').length).toBeGreaterThan(3)
  })
})

// ── what the viewer refuses to do ─────────────────────────────────────────────

describe('the viewer states what it cannot show', () => {
  it('a record that is no longer in the evidence is said to be missing — never a lookalike', async () => {
    const { c } = await mountViewer({ scanId: 'scan 1', file: 'Board/Q1 #2 & notes.pdf', findingId: 'f-reassessed-away', changeId: null, sha256: SRC, version: 'source' })
    expect(c.textContent).toContain(RECORD_MISSING)
    expect(c.querySelector('[role="alert"]')).toBeTruthy()
    // the identity the link named, so the reader can tell what was asked for
    expect(c.textContent).toContain('f-reassessed-away')
    expect(c.textContent).toContain('Board/Q1 #2 & notes.pdf')
    expect(c.textContent).toContain('scan 1')
    // and none of the current findings, not even the same criterion's
    expect(c.textContent).not.toMatch(/Figure on the cover|Chart on page 12/)
    expect(getExactArtifactPage).not.toHaveBeenCalled()
  })

  it('evidence the account cannot see reads as not available, with the target named', async () => {
    getFileReportFacts.mockImplementationOnce(async () => { const e = new Error('scan or file not found'); e.status = 404; throw e })
    const { c } = await mountViewer({ scanId: 'someone-elses', file: 'x.pdf', findingId: 'f1', changeId: null, sha256: null, version: null })
    expect(c.textContent).toMatch(/not available to your account/)
    expect(c.textContent).toContain('someone-elses')
  })

  it('says the document changed since the report when the link names another version', async () => {
    FACTS = pdfFacts({ identity: { ...pdfFacts().identity, sourceSha256: NEWER } })
    const { c } = await mountViewer({ ...parseEvidenceHref(evidenceHref({ scanId: 'scan 1', file: 'Board/Q1 #2 & notes.pdf', findingId: 'f-second', sha256: SRC, version: 'source' })) })
    expect(c.textContent).toMatch(/The document changed since the report was generated/)
    expect(c.textContent).toContain(SRC.slice(0, 12))
    expect(c.textContent).toContain(NEWER.slice(0, 12))
  })

  it('a matching version says so, and a link without one says it cannot tell', async () => {
    let { c } = await mountViewer(parseEvidenceHref(linkFor(FACTS, 1)))
    expect(c.textContent).toMatch(/This is the version the report described/)
    await unmountAll()
    ;({ c } = await mountViewer({ scanId: 'scan 1', file: 'Board/Q1 #2 & notes.pdf', findingId: 'f-second', changeId: null, sha256: null, version: null }))
    expect(c.textContent).toMatch(/does not name a document version/)
  })

  it('fetches the exact page by digest and shows it', async () => {
    const { c } = await mountViewer(parseEvidenceHref(linkFor(FACTS, 1)))
    expect(getExactArtifactPage).toHaveBeenCalledWith('scan 1', 'Board/Q1 #2 & notes.pdf', SRC, 12)
    const img = c.querySelector('.evview-record img')
    expect(img).toBeTruthy()
    expect(img.getAttribute('alt')).toMatch(/page 12$/)
    expect(c.textContent).toMatch(/Original document, page 12/)
  })

  it('an out-of-range page is the server\'s refusal, shown — never a substitute page', async () => {
    FACTS = pdfFacts()
    FACTS.findings[1].location = { ...FACTS.findings[1].location, page: 99, label: 'Page 99 · figure 3' }
    getExactArtifactPage.mockImplementationOnce(async () => ({ ok: false, status: 404, detail: "page 99 is beyond this document's 12 pages" }))
    const { c } = await mountViewer(parseEvidenceHref(linkFor(FACTS, 1)))
    expect(getExactArtifactPage).toHaveBeenCalledWith('scan 1', 'Board/Q1 #2 & notes.pdf', SRC, 99)
    expect(c.textContent).toContain("Preview unavailable — page 99 is beyond this document's 12 pages")
    expect(c.querySelector('.evview-record img')).toBeNull()
  })

  it('a server that draws a different page than asked is not shown as that page', async () => {
    getExactArtifactPage.mockImplementationOnce(async (_s, _f, _sha, page) => ({ ok: true, blob: new Blob(['x']), page, renderedPage: 1 }))
    const { c } = await mountViewer(parseEvidenceHref(linkFor(FACTS, 1)))
    expect(c.textContent).toMatch(/Preview unavailable — the preview service drew page 1, not page 12/)
    expect(c.querySelector('.evview-record img')).toBeNull()
  })

  it('a Word paragraph gets no page picture, and says why', async () => {
    FACTS = pdfFacts({
      identity: { ...pdfFacts().identity, file: 'policy.docx' },
      findings: [{ id: 'w1', ruleId: 'DOCX-HEADING', sc: '1.3.1', detail: 'Fake heading', severity: 'MODERATE', location: { label: 'Paragraph 15', kind: 'paragraph', page: null, slide: null, sheet: null, cell: null, objectId: 'paragraph:14', element: 'docx:paragraph:14', raw: 'docx:paragraph:14' }, state: 'open' }],
    })
    const { c } = await mountViewer({ scanId: 'scan 1', file: 'policy.docx', findingId: 'w1', changeId: null, sha256: SRC, version: 'source' })
    expect(c.textContent).toContain('Paragraph 15')
    expect(c.textContent).toContain('Preview unavailable — Word paragraphs do not map to rendered pages')
    expect(getExactArtifactPage).not.toHaveBeenCalled()
    expect(c.textContent).not.toMatch(/Open in Word|edit in/i)
  })

  it('an Excel cell gets no page picture either', async () => {
    FACTS = pdfFacts({
      identity: { ...pdfFacts().identity, file: 'budget.xlsx' },
      findings: [{ id: 'x1', ruleId: 'XLSX-HDR', sc: '1.3.1', detail: 'No header row', location: { label: 'Sheet Budget · cell B3', kind: 'cell', page: null, slide: null, sheet: 'Budget', cell: 'B3', objectId: null, element: 'xlsx:sheet:Budget:cell:B3', raw: 'xlsx:sheet:Budget:cell:B3' }, state: 'open' }],
    })
    const { c } = await mountViewer({ scanId: 'scan 1', file: 'budget.xlsx', findingId: 'x1', changeId: null, sha256: null, version: null })
    expect(c.textContent).toContain('Sheet Budget · cell B3')
    expect(c.textContent).toContain('Preview unavailable — Excel cells are not rendered as pages')
  })

  it('a PowerPoint slide IS previewed, through the exact route, as a slide', async () => {
    FACTS = pdfFacts({
      identity: { ...pdfFacts().identity, file: 'deck.pptx' },
      findings: [{ id: 'p1', ruleId: 'PPTX-ALT', sc: '1.1.1', detail: 'Picture without alt', location: { label: 'Slide 4 · shape 7', kind: 'object', page: 4, slide: 4, sheet: null, cell: null, objectId: 'shape:7', element: 'pptx:slide:3:element:7', raw: 'pptx:slide:3:element:7' }, state: 'open' }],
    })
    const { c } = await mountViewer({ scanId: 'scan 1', file: 'deck.pptx', findingId: 'p1', changeId: null, sha256: SRC, version: 'source' })
    expect(getExactArtifactPage).toHaveBeenCalledWith('scan 1', 'deck.pptx', SRC, 4)
    expect(c.textContent).toMatch(/Original document, slide 4/)
  })

  it('no recorded location or page: "Location not recorded", "no page was recorded", no page 1', async () => {
    const { c } = await mountViewer(parseEvidenceHref(linkFor(FACTS, 2)))
    expect(c.textContent).toContain('Location not recorded')
    expect(c.textContent).toMatch(/Preview unavailable — no page was recorded/)
    expect(c.textContent).toContain('Recommended actionNot recorded')
    expect(getExactArtifactPage).not.toHaveBeenCalled()
  })

  it('an open finding and an unverified change both say "Verification not recorded"', async () => {
    let { c } = await mountViewer(parseEvidenceHref(linkFor(FACTS, 1)))
    expect(c.textContent).toContain('Verification not recorded')
    await unmountAll()
    const change = attachEvidenceLinks(FACTS).savedChanges[0].location.href
    ;({ c } = await mountViewer(parseEvidenceHref(change)))
    expect(c.textContent).toContain('Saved change evidence')
    expect(c.textContent).toContain('A bar chart of revenue')
    expect(c.textContent).toContain('Verification not recorded')
    expect(c.textContent).toContain('Location not recorded')
  })

  it('a missing sha-256 for the version means no preview is requested, and says so', async () => {
    FACTS = pdfFacts({ identity: { ...pdfFacts().identity, sourceSha256: null, sourceChecksumKind: 'md5' } })
    const { c } = await mountViewer({ scanId: 'scan 1', file: 'Board/Q1 #2 & notes.pdf', findingId: 'f-second', changeId: null, sha256: null, version: null })
    expect(c.textContent).toMatch(/Preview unavailable — no sha-256 is recorded for this version/)
    expect(getExactArtifactPage).not.toHaveBeenCalled()
  })
})
