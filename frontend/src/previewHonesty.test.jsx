/**
 * Page previews say which page they show, or say why they show none (audit gaps L4–L9).
 *
 * Every test here pins a way a preview used to state something it did not know:
 *   - Thumbnail rendered NOTHING when the backend had no render, so "Image 2 of 5" sat above a
 *     blank space; and `page={card.page || 1}` captioned an unplaced finding "Page 1".
 *   - The generic page route CLAMPS (render.render_page_png on a one-page PDF returns the same
 *     bytes for page 1 and page 999), so "Page 7" under an image from it was an assumption.
 *   - The review card's "Open in SharePoint" was fetched once with `item.page || 1`, so paging to
 *     image 3 on slide 7 still opened slide 1.
 *   - FileDrawer kept one page list per grouped finding: Word paragraphs and Excel cells showed no
 *     location at all, and no occurrence linked to its own record.
 *   - Nothing pinned PagePreview's "Preview unavailable" or FileDrawer's "Show page" (L9).
 *
 * DOM-level, against the live components — the preview server serves the shared checkout, not
 * this worktree (CLAUDE.md).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

const png = () => new Blob(['png'], { type: 'image/png' })
const api = vi.hoisted(() => ({}))

vi.mock('./api.js', async (importActual) => {
  const actual = await importActual()
  return {
    ...actual,
    getFilePage: (...a) => api.getFilePage(...a),
    getFileThumbnail: (...a) => api.getFileThumbnail(...a),
    getFileGeometry: (...a) => api.getFileGeometry(...a),
    getExactArtifactPage: (...a) => api.getExactArtifactPage(...a),
    getSourceLink: (...a) => api.getSourceLink(...a),
    getFileReportFacts: (...a) => api.getFileReportFacts(...a),
    getFileRemediationState: async () => [],
    getFileRemediationDiffs: async () => [],
    getDocumentTimeline: async () => [],
    listHitlQueue: async () => [],
    getScanAiCalls: async () => [],
    validateAlt: async () => ({}),
  }
})

URL.createObjectURL = vi.fn(() => 'blob:acp/preview')
URL.revokeObjectURL = vi.fn()

const { default: PagePreview } = await import('./PagePreview.jsx')
const { default: Thumbnail } = await import('./Thumbnail.jsx')
const { default: FileDrawer } = await import('./FileDrawer.jsx')
const { default: EvidenceCard } = await import('./EvidenceCard.jsx')
const { parseEvidenceHref } = await import('./evidenceLink.js')

beforeEach(() => {
  api.getFilePage = vi.fn(async () => null)
  api.getFileThumbnail = vi.fn(async () => png())
  api.getFileGeometry = vi.fn(async () => null)
  api.getExactArtifactPage = vi.fn(async () => ({ ok: false, status: 404, detail: 'not held' }))
  api.getSourceLink = vi.fn(async () => ({ url: null }))
  api.getFileReportFacts = vi.fn(async () => null)
})
afterEach(unmountAll)

const flush = async () => { await act(async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setTimeout(r, 0)) }) }
async function mount(el) {
  const { container, root } = createTestRoot()
  await act(async () => { root.render(el) })
  await flush()
  return container
}
const btnText = (c, re) => [...c.querySelectorAll('button')].find((b) => re.test(b.textContent))

// ── PagePreview (L9) ──────────────────────────────────────────────────────────

describe('PagePreview', () => {
  it('no recorded page: says so, and requests nothing — no page-1 default', async () => {
    const c = await mount(createElement(PagePreview, { scanId: 's', file: 'a.pdf', page: null }))
    expect(c.textContent).toBe('Preview unavailable — no page was recorded for this finding')
    expect(api.getFilePage).not.toHaveBeenCalled()
  })

  it('a render that fails is "Preview unavailable", named, not a blank', async () => {
    const c = await mount(createElement(PagePreview, { scanId: 's', file: 'a.pdf', page: 3 }))
    expect(c.textContent).toBe('Preview unavailable — no image of page 3 could be produced')
    expect(c.querySelector('img')).toBeNull()
  })

  it('a server that drew another page is not captioned as the page asked for', async () => {
    api.getFilePage = vi.fn(async () => ({ blob: png(), renderedPage: 2 }))
    const c = await mount(createElement(PagePreview, { scanId: 's', file: 'a.pdf', page: 5 }))
    expect(api.getFilePage).toHaveBeenCalledWith('s', 'a.pdf', 5, { detail: true })
    expect(c.textContent).toMatch(/Preview unavailable — page 5 is not in the document the preview service holds \(it drew page 2\)/)
    expect(c.querySelector('img')).toBeNull()
  })

  it('an unconfirmed page (the clamping route did not say) is captioned "not confirmed"', async () => {
    api.getFilePage = vi.fn(async () => png())   // a bare Blob: no X-ACP-Rendered-Page
    const c = await mount(createElement(PagePreview, { scanId: 's', file: 'a.pdf', page: 5 }))
    expect(c.querySelector('figcaption').textContent).toBe('Page 5 · not confirmed')
    expect(c.querySelector('img').getAttribute('alt')).toMatch(/requested for page 5; the page shown is not confirmed/)
  })

  it('a confirmed page is captioned plainly', async () => {
    api.getFilePage = vi.fn(async () => ({ blob: png(), renderedPage: 5 }))
    const c = await mount(createElement(PagePreview, { scanId: 's', file: 'a.pdf', page: 5 }))
    expect(c.querySelector('figcaption').textContent).toBe('Page 5')
    expect(c.querySelector('img').getAttribute('alt')).toBe('a.pdf — page 5')
  })

  it('with a digest it uses the strict exact-bytes route and shows the server\'s reason', async () => {
    api.getExactArtifactPage = vi.fn(async () => ({ ok: false, status: 404, detail: "slide 9 is beyond this document's 4 slides" }))
    const c = await mount(createElement(PagePreview, { scanId: 's', file: 'deck.pptx', page: 9, unit: 'slide', sha256: 'e'.repeat(64) }))
    expect(api.getExactArtifactPage).toHaveBeenCalledWith('s', 'deck.pptx', 'e'.repeat(64), 9)
    expect(api.getFilePage).not.toHaveBeenCalled()
    expect(c.textContent).toBe("Preview unavailable — slide 9 is beyond this document's 4 slides")
  })
})

// ── Thumbnail (L6/L8) ─────────────────────────────────────────────────────────

describe('Thumbnail', () => {
  it('orientation mode (no page prop): the first page, labelled as a document preview', async () => {
    const c = await mount(createElement(Thumbnail, { scanId: 's', file: 'a.pdf' }))
    expect(api.getFileThumbnail).toHaveBeenCalled()
    expect(c.querySelector('img').getAttribute('alt')).toBe('First page of a.pdf (document preview — not the location of a finding)')
  })

  it('page null: "Preview unavailable — no page was recorded", no request, no page 1', async () => {
    const c = await mount(createElement(Thumbnail, { scanId: 's', file: 'a.pdf', page: null }))
    expect(c.textContent).toBe('Preview unavailable — no page was recorded for this finding')
    expect(api.getFileThumbnail).not.toHaveBeenCalled()
    expect(api.getFilePage).not.toHaveBeenCalled()
  })

  it('a failed render is a named placeholder, not nothing', async () => {
    api.getFileThumbnail = vi.fn(async () => null)
    const c = await mount(createElement(Thumbnail, { scanId: 's', file: 'a.docx', page: 1 }))
    expect(c.textContent).toBe('Preview unavailable — this document could not be rendered')
  })

  it('page N from the clamping route is not claimed as page N unless confirmed', async () => {
    api.getFilePage = vi.fn(async () => png())
    let c = await mount(createElement(Thumbnail, { scanId: 's', file: 'a.pdf', page: 7 }))
    expect(c.querySelector('img').getAttribute('alt')).toBe('Preview of a.pdf requested for page 7; the page shown is not confirmed')
    expect(c.textContent).toContain('Page 7 · not confirmed')
    await unmountAll()
    api.getFilePage = vi.fn(async () => ({ blob: png(), renderedPage: 7 }))
    c = await mount(createElement(Thumbnail, { scanId: 's', file: 'a.pdf', page: 7 }))
    expect(c.querySelector('img').getAttribute('alt')).toBe('Page 7 of a.pdf')
    expect(c.textContent).not.toContain('not confirmed')
    await unmountAll()
    api.getFilePage = vi.fn(async () => ({ blob: png(), renderedPage: 3 }))
    c = await mount(createElement(Thumbnail, { scanId: 's', file: 'a.pdf', page: 7 }))
    expect(c.querySelector('img')).toBeNull()
    expect(c.textContent).toMatch(/Preview unavailable — page 7 is not in the document the preview service holds \(it drew page 3\)/)
  })
})

// ── EvidenceCard (L6/L7) ──────────────────────────────────────────────────────

describe('EvidenceCard preview and source link follow the pager', () => {
  const deck = (over = {}) => ({
    id: 7, scan_id: 's1', file: 'deck.pptx', rule_id: '1.1.1', rule_name: 'Non-text Content',
    status: 'pending', finding_count: 2, page: null,
    proposals: [
      { locator: 'ppt/slides/slide3.xml#rId2', proposed_value: 'A chart' },
      { locator: 'ppt/slides/slide7.xml#rId4', proposed_value: 'A photo' },
    ],
    ...over,
  })
  const mountCard = (item) => mount(createElement(EvidenceCard, { item, onAct: vi.fn().mockResolvedValue(undefined) }))

  it('asks for the source link at the page of the image in view, and re-asks when paging', async () => {
    api.getFileGeometry = vi.fn(async (_s, _f, loc) => ({ page: loc.includes('slide3') ? 3 : 7, x: 0.1, y: 0.1, w: 0.2, h: 0.2 }))
    api.getSourceLink = vi.fn(async (_s, _f, page) => ({ url: `https://sp.example/deck.pptx?web=1&slide=${page}`, label: 'Open in SharePoint' }))
    const c = await mountCard(deck())
    expect(api.getSourceLink).toHaveBeenLastCalledWith('s1', 'deck.pptx', 3)
    expect(c.querySelector('.evcard-source-link').getAttribute('href')).toMatch(/slide=3$/)
    await act(async () => { c.querySelector('button[aria-label="Next flagged image"]').click() })
    await flush()
    expect(api.getSourceLink).toHaveBeenLastCalledWith('s1', 'deck.pptx', 7)
    expect(c.querySelector('.evcard-source-link').getAttribute('href')).toMatch(/slide=7$/)
    expect(api.getSourceLink.mock.calls.every((call) => call[2] !== 1)).toBe(true)
  })

  it('no recorded or measured page: the link carries no page and the hero says so', async () => {
    const c = await mountCard(deck({ proposals: [{ locator: null, proposed_value: 'x' }], finding_count: 1 }))
    expect(api.getSourceLink).toHaveBeenCalledWith('s1', 'deck.pptx', null)
    expect(c.querySelector('.evcard-hero').textContent).toContain('Preview unavailable — no page was recorded for this finding')
    expect(api.getFileThumbnail).not.toHaveBeenCalled()
  })

  it('a recorded page is used for a single-image finding', async () => {
    api.getFilePage = vi.fn(async () => ({ blob: png(), renderedPage: 4 }))
    const c = await mountCard(deck({ page: 4, proposals: [{ locator: null, proposed_value: 'x' }], finding_count: 1 }))
    expect(api.getSourceLink).toHaveBeenCalledWith('s1', 'deck.pptx', 4)
    expect(api.getFilePage).toHaveBeenCalledWith('s1', 'deck.pptx', 4, { detail: true })
    expect(c.querySelector('.evcard-hero img').getAttribute('alt')).toBe('Page 4 of deck.pptx')
  })
})

// ── FileDrawer (L4/L5/L9) ─────────────────────────────────────────────────────

const SRC = '5'.repeat(64)
const factsFor = (file, findings) => ({
  factsVersion: 1, factsDigest: 'd', identity: { scanId: 'scan-1', file, sourceSha256: SRC, correctedSha256: null, currentArtifact: { kind: 'source', sha256: SRC } },
  findings, savedChanges: [], savedChangesComplete: true, savedChangesTotal: 0,
})
const loc = (label, kind, extra = {}) => ({ label, kind, page: null, slide: null, sheet: null, cell: null, objectId: null, ...extra })
const mountDrawer = (file) => mount(createElement(FileDrawer, { file, scanId: 'scan-1', onClose: () => {} }))
const occurrences = (c) => [...c.querySelectorAll('.finding-occurrences li')]

describe('FileDrawer lists every occurrence where it is', () => {
  it('Excel: each cell of a grouped finding, in words — not collapsed, not a page', async () => {
    const doc = { file: 'budget.xlsx', type: 'xlsx', status: 'analysed', score: 60, engine: 'xlsx', issues: [
      { wcag: 'SC_1_3_1', rule_id: 'XLSX-HDR', severity: 'SERIOUS', detail: 'Merged header cell', location: 'xlsx:sheet:Budget:cell:B3' },
      { wcag: 'SC_1_3_1', rule_id: 'XLSX-HDR', severity: 'SERIOUS', detail: 'Merged header cell', location: 'xlsx:sheet:Budget:cell:D9' },
    ] }
    const c = await mountDrawer(doc)
    const lines = occurrences(c).map((li) => li.textContent)
    expect(lines).toEqual(['Sheet Budget · cell B3', 'Sheet Budget · cell D9'])
    expect(btnText(c, /Show (page|slide)/)).toBeUndefined()
  })

  it('Word: paragraphs in words, no page preview offered', async () => {
    const doc = { file: 'policy.docx', type: 'docx', status: 'analysed', score: 60, engine: 'docx', issues: [
      { wcag: 'SC_1_3_1', rule_id: 'DOCX-HEADING', severity: 'MODERATE', detail: 'Fake heading', location: 'docx:paragraph:3', page: 2 },
      { wcag: 'SC_1_3_1', rule_id: 'DOCX-HEADING', severity: 'MODERATE', detail: 'Fake heading', location: 'docx:paragraph:14' },
    ] }
    const c = await mountDrawer(doc)
    expect(occurrences(c).map((li) => li.textContent)).toEqual(['Paragraph 4', 'Paragraph 15'])
    expect(btnText(c, /Show page/)).toBeUndefined()
  })

  it('PDF: each occurrence links to ITS server record and previews its own page', async () => {
    const doc = { file: 'guide.pdf', type: 'pdf', status: 'analysed', score: 60, engine: 'pdf', issues: [
      { wcag: 'SC_1_1_1', rule_id: 'PDF-ALT-001', severity: 'SERIOUS', detail: 'Figure without alt', page: 2, location: 'pdf:fig:2:0' },
      { wcag: 'SC_1_1_1', rule_id: 'PDF-ALT-001', severity: 'SERIOUS', detail: 'Figure without alt', page: 9, location: 'pdf:fig:9:0' },
      { wcag: 'SC_3_1_1', rule_id: 'PDF-LANG', severity: 'MODERATE', detail: 'No language' },
    ] }
    api.getFileReportFacts = vi.fn(async () => factsFor('guide.pdf', [
      { id: 'srv-p2', ruleId: 'PDF-ALT-001', sc: '1.1.1', detail: 'Figure without alt', location: loc('Page 2 · figure 1', 'object', { page: 2, objectId: 'figure:2:0', raw: 'pdf:fig:2:0', element: 'pdf:fig:2:0' }), state: 'open' },
      { id: 'srv-p9', ruleId: 'PDF-ALT-001', sc: '1.1.1', detail: 'Figure without alt', location: loc('Page 9 · figure 1', 'object', { page: 9, objectId: 'figure:9:0', raw: 'pdf:fig:9:0', element: 'pdf:fig:9:0' }), state: 'open' },
    ]))
    api.getExactArtifactPage = vi.fn(async (_s, _f, _sha, page) => ({ ok: true, blob: png(), page, renderedPage: page }))
    const c = await mountDrawer(doc)
    const lines = occurrences(c)
    expect(lines.map((li) => li.querySelector('span').textContent)).toEqual(['Page 2 · figure 1', 'Page 9 · figure 1'])
    const links = lines.map((li) => parseEvidenceHref(li.querySelector('a.finding-evidence-link').getAttribute('href')))
    expect(links.map((l) => l.findingId)).toEqual(['srv-p2', 'srv-p9'])
    expect(links.every((l) => l.scanId === 'scan-1' && l.file === 'guide.pdf' && l.sha256 === SRC)).toBe(true)
    // "Show page N" per occurrence, through the exact-bytes route (L9: pinned)
    await act(async () => { btnText(c, /^Show page 9$/).click() })
    await flush()
    expect(api.getExactArtifactPage).toHaveBeenCalledWith('scan-1', 'guide.pdf', SRC, 9)
    expect(btnText(c, /^Hide page$/)).toBeTruthy()
    expect(c.querySelector('.finding-occurrences img').getAttribute('alt')).toBe('guide.pdf — page 9')
  })

  it('an issue row that matches no single server record gets its location, and no link', async () => {
    const doc = { file: 'guide.pdf', type: 'pdf', status: 'analysed', score: 60, engine: 'pdf', issues: [
      { wcag: 'SC_1_1_1', rule_id: 'PDF-ALT-001', severity: 'SERIOUS', detail: 'Figure without alt', page: 2 },
    ] }
    // two indistinguishable server records → the row cannot say which it is
    api.getFileReportFacts = vi.fn(async () => factsFor('guide.pdf', [
      { id: 'a', ruleId: 'PDF-ALT-001', detail: 'Figure without alt', location: loc('Page 2', 'page', { page: 2 }) },
      { id: 'b', ruleId: 'PDF-ALT-001', detail: 'Figure without alt', location: loc('Page 2', 'page', { page: 2 }) },
    ]))
    const c = await mountDrawer(doc)
    expect(occurrences(c)[0].textContent).toMatch(/^Page 2/)
    expect(c.querySelector('a.finding-evidence-link')).toBeNull()
  })

  it('PowerPoint: slides are previewable, and the route\'s 404 is said out loud', async () => {
    const doc = { file: 'deck.pptx', type: 'pptx', status: 'analysed', score: 60, engine: 'pptx', issues: [
      { wcag: 'SC_1_1_1', rule_id: 'PPTX-ALT', severity: 'SERIOUS', detail: 'Picture without alt', page: 4, location: 'pptx:slide:3:element:7' },
    ] }
    api.getFileReportFacts = vi.fn(async () => factsFor('deck.pptx', []))
    api.getExactArtifactPage = vi.fn(async () => ({ ok: false, status: 404, detail: 'no preview available for this file type' }))
    const c = await mountDrawer(doc)
    expect(occurrences(c)[0].textContent).toMatch(/^Slide 4 · shape 7/)
    await act(async () => { btnText(c, /^Show slide 4$/).click() })
    await flush()
    expect(api.getExactArtifactPage).toHaveBeenCalledWith('scan-1', 'deck.pptx', SRC, 4)
    expect(c.querySelector('.finding-occurrences').textContent).toContain('Preview unavailable — no preview available for this file type')
  })
})
