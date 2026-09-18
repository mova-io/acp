/**
 * Settings → Rule explanations: a READ-ONLY view of what each rule checks, per WCAG criterion and
 * file format, served by GET /rules/explanations (api/rule_explanations/schema.py).
 *
 * The fixture follows that schema's field vocabularies. What these tests pin, in the order a
 * mistake would mislead an administrator:
 *   - status is WORDS (never colour alone), and "not implemented" / "manual" are labelled outright;
 *   - a threshold says whether it is FIXED IN CODE or CONFIGURABLE (and by which setting), and
 *     whether the number is the WCAG requirement or an ACP heuristic;
 *   - nothing on the tab can write: no inputs beyond search and the two filters;
 *   - payload strings render as text, never markup;
 *   - provenance is visible, so a stale explanation is detectable.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'

const XSS = '<img src=x onerror=alert(1)>'

const FIXTURE = {
  schema_version: 1,
  provenance: {
    commit: '734fec29abcdef0123456789',
    rule_catalog_version: '2026.09.18',
    rubric: { name: 'WCAG 2.1 AA', version: '3', hash: 'e85fcf7e14f9040c' },
    sources: ['api/remediation_capability.py', 'api/rule_explanations/catalog.py'],
  },
  formats: ['html', 'docx', 'pptx', 'xlsx', 'pdf'],
  criteria: [
    {
      sc: '1.3.1', name: 'Info and Relationships', level: 'A',
      enabled_in_rubric: { disabled_rule_ids: [] },
      formats: {
        docx: {
          status: 'implemented', assessment_lane: 'auto', remediation_lane: 'auto',
          checks: ['Paragraphs styled to look like headings without a Heading style'],
          thresholds: [
            { label: 'Minimum font size for a pseudo-heading', value: '14pt', source: 'api/office_audit.py:PSEUDO_HEADING_MIN_PT', configurable: false, setting: null, standard: false },
          ],
          does_not_check: ['Whether the heading text itself is meaningful'],
          evidence: 'One finding per paragraph, located word:p:N',
          fix: 'Applies the matching Heading style automatically; verified by re-scan.',
          rules: [{ id: 'DOCX_PSEUDO_HEADING', source: 'api/office_audit.py', method: 'heuristic', fix: 'auto', engine: 'acp' }],
        },
        pdf: {
          status: 'partial', assessment_lane: 'review', remediation_lane: 'assisted',
          checks: ['The document has a tag tree'],
          thresholds: [], does_not_check: ['Reading order of tagged content'],
          evidence: 'One finding per document', fix: 'Detection only.',
          rules: [{ id: 'PDF-TAGS-001', source: 'api/remediate_pdf.py', method: 'deterministic', fix: 'none', engine: 'acp' }],
        },
        html: {
          status: 'not_implemented', assessment_lane: null, remediation_lane: null,
          checks: [], thresholds: [], does_not_check: [], evidence: '', fix: '', rules: [],
        },
        pptx: { status: 'not_applicable', checks: [], thresholds: [], does_not_check: [], rules: [] },
        xlsx: { status: 'not_applicable', checks: [], thresholds: [], does_not_check: [], rules: [] },
      },
    },
    {
      sc: '1.4.3', name: 'Contrast (Minimum)', level: 'AA',
      enabled_in_rubric: { disabled_rule_ids: ['XLSX-CONTRAST-001'] },
      formats: {
        xlsx: {
          status: 'implemented', assessment_lane: 'auto', remediation_lane: 'auto',
          checks: ['Text colour against its cell fill'],
          thresholds: [
            { label: 'Normal text contrast ratio', value: '4.5:1', source: 'api/contrast.py:AA_NORMAL', configurable: false, setting: null, standard: true },
            { label: 'Compliance score threshold', value: '90', source: 'api/rubric.py:DEFAULT_THRESHOLD', configurable: true, setting: 'rubric.compliant_threshold', standard: false },
          ],
          does_not_check: [XSS],
          evidence: 'One finding per cell', fix: 'Darkens the text colour.',
          rules: [{ id: 'XLSX-CONTRAST-001', source: 'api/xlsx_audit.py', method: 'deterministic', fix: 'auto', engine: 'office-analyser' }],
        },
        docx: {
          status: 'manual', assessment_lane: 'human', remediation_lane: 'human',
          checks: ['Contrast over images is left to a person'], thresholds: [], does_not_check: [],
          evidence: '', fix: '', rules: [{ id: 'DOCX-CONTRAST-MANUAL', source: 'api/manual_plan.py', method: 'manual', fix: 'review' }],
        },
      },
    },
    {
      sc: '2.4.2', name: 'Page Titled', level: 'A',
      formats: {
        html: {
          status: 'implemented', assessment_lane: 'auto', remediation_lane: 'assisted',
          checks: ['The document has a non-empty title element'], thresholds: [], does_not_check: [],
          evidence: 'One finding per document', fix: 'AI drafts a title; a person approves it.',
          rules: [{ id: 'HTML-TITLE-001', source: 'api/html_audit.py', method: 'ai', fix: 'assisted', engine: 'acp' }],
        },
      },
    },
  ],
  settings: [
    { key: 'rubric.compliant_threshold', label: 'Compliance threshold', value: 90, scope: 'platform', editable_by: 'Platform admin', where: 'Rubric (API)', affects: ['Scan score'] },
  ],
  counts: { criteria: 3, cells: 9, by_status: { implemented: 4, partial: 1, manual: 1, not_implemented: 1, not_applicable: 2 } },
}

const getRuleExplanations = vi.fn(() => Promise.resolve(FIXTURE))
vi.mock('./api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  getRuleExplanations: (...a) => getRuleExplanations(...a),
}))

afterEach(() => { unmountAll(); getRuleExplanations.mockReset(); getRuleExplanations.mockImplementation(() => Promise.resolve(FIXTURE)) })

const { default: Settings } = await import('./Settings.jsx')
const { default: RuleExplanations } = await import('./RuleExplanations.jsx')

const settle = async (ms = 20) => {
  await act(async () => { await new Promise((r) => setTimeout(r, ms)) })
  for (let k = 0; k < 3; k++) await act(async () => { await new Promise((r) => setTimeout(r, 0)) })
}
const mountPanel = async () => {
  const { container, root } = createTestRoot()
  await act(async () => { root.render(createElement(RuleExplanations, { me: { is_admin: false } })) })
  await settle()
  return container
}
const setValue = (el, v) => {
  const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v)
  el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }))
}
const headings = (c) => [...c.querySelectorAll('h4.rx-criterion-head')].map((h) => h.textContent)
const search = (c) => c.querySelector('input[type="search"]')
const selectByLabel = (c, label) => [...c.querySelectorAll('label')]
  .find((l) => l.textContent.trim().startsWith(label))?.querySelector('select')
const toggleFor = (c, sc) => [...c.querySelectorAll('button[aria-expanded]')].find((b) => b.textContent.includes(sc))
const expand = async (c, sc) => { await act(async () => { toggleFor(c, sc).click() }) }

describe('Settings → Rule explanations tab', () => {
  it('is a Settings tab that shows the panel when selected, reachable with arrow keys', async () => {
    const { container, root } = createTestRoot()
    await act(async () => { root.render(createElement(Settings, { onClose: () => {}, me: { is_admin: false } })) })
    await settle(300)
    const tabs = [...container.querySelectorAll('[role="tablist"] [role="tab"]')]
    const memory = tabs.find((t) => t.textContent.trim() === 'Review Memory')
    const rules = tabs.find((t) => t.textContent.trim() === 'Rule explanations')
    expect(rules, 'no Rule explanations tab').toBeTruthy()
    expect(rules.id).toBe('settings-tab-rules')
    expect(rules.getAttribute('aria-controls')).toBe('settings-panel')
    expect(rules.getAttribute('tabindex')).toBe('-1')

    // Keyboard: ArrowRight from Review Memory moves focus AND selection to the new tab.
    await act(async () => { memory.click() })
    memory.focus()
    await act(async () => { memory.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })) })
    await settle()
    expect(document.activeElement).toBe(rules)
    expect(rules.getAttribute('aria-selected')).toBe('true')
    expect(rules.getAttribute('tabindex')).toBe('0')
    const panel = container.querySelector('#settings-panel')
    expect(panel.getAttribute('aria-labelledby')).toBe('settings-tab-rules')
    // Settings panels title themselves with an h3; criteria nest below it as h4.
    expect(panel.querySelector('h3').textContent).toBe('Rule explanations')
    expect(headings(panel)).toHaveLength(3)
    expect(getRuleExplanations).toHaveBeenCalled()
  })
})

describe('the Rule explanations panel', () => {
  it('lists every criterion as a heading with a polite results count', async () => {
    const c = await mountPanel()
    expect(headings(c)).toEqual([
      '1.3.1 Info and Relationships Level A',
      '1.4.3 Contrast (Minimum) Level AA',
      '2.4.2 Page Titled Level A',
    ])
    const count = c.querySelector('#rx-count')
    expect(count.getAttribute('aria-live')).toBe('polite')
    expect(count.textContent).toBe('3 criteria')
    // The search box has an accessible name from its label.
    expect(search(c).closest('label').textContent).toMatch(/Search criteria/)
  })

  it('search filters by SC number, by rule id and by bullet text', async () => {
    const c = await mountPanel()
    await act(async () => { setValue(search(c), '1.4.3') })
    expect(headings(c)).toEqual(['1.4.3 Contrast (Minimum) Level AA'])
    expect(c.querySelector('#rx-count').textContent).toBe('Showing 1 of 3 criteria')

    await act(async () => { setValue(search(c), 'html-title-001') })
    expect(headings(c)).toEqual(['2.4.2 Page Titled Level A'])

    await act(async () => { setValue(search(c), 'reading order of tagged') })
    expect(headings(c)).toEqual(['1.3.1 Info and Relationships Level A'])
    // A text match opens the criterion so the matching bullet is visible, not hidden in a fold.
    expect(c.textContent).toContain('Reading order of tagged content')

    await act(async () => { setValue(search(c), 'zzz-no-such-thing') })
    expect(headings(c)).toEqual([])
    expect(c.textContent).toContain('No criteria match these filters.')
    expect(c.querySelector('#rx-count').textContent).toBe('Showing 0 of 3 criteria')
    const clear = [...c.querySelectorAll('button')].find((b) => b.textContent === 'Clear search and filters')
    await act(async () => { clear.click() })
    expect(headings(c)).toHaveLength(3)
  })

  it('format and status filters narrow the list', async () => {
    const c = await mountPanel()
    await act(async () => { setValue(selectByLabel(c, 'Format'), 'xlsx') })
    // 1.3.1's XLSX cell is not applicable — it still lists, as one "Not applicable to" line.
    expect(headings(c)).toEqual(['1.3.1 Info and Relationships Level A', '1.4.3 Contrast (Minimum) Level AA'])
    await act(async () => { setValue(selectByLabel(c, 'Format'), '') })
    await act(async () => { setValue(selectByLabel(c, 'Status'), 'manual') })
    expect(headings(c)).toEqual(['1.4.3 Contrast (Minimum) Level AA'])
  })

  it('states status in words, labels not-implemented and manual, and folds not-applicable', async () => {
    const c = await mountPanel()
    const first = c.querySelectorAll('.rx-criterion')[0]
    const summary = first.querySelector('.rx-summary').textContent
    expect(summary).toContain('DOCX: Implemented')
    expect(summary).toContain('PDF: Partial')
    expect(summary).toContain('HTML: Not implemented')
    expect(summary).toContain('Not applicable to: PPTX, XLSX')
    expect(c.querySelectorAll('.rx-criterion')[1].querySelector('.rx-summary').textContent)
      .toContain('DOCX: Manual review only')

    await expand(c, '1.3.1')
    const html = [...first.querySelectorAll('h5')].find((h) => h.textContent.startsWith('HTML'))
    expect(html.textContent).toBe('HTML Not implemented')
    expect(first.textContent).toContain('nothing checks it yet')
    await expand(c, '1.4.3')
    expect(c.textContent).toContain('ACP cannot judge this from the file; a person must.')
  })

  it('marks each threshold fixed-in-code or configurable, and WCAG requirement or ACP heuristic', async () => {
    const c = await mountPanel()
    await expand(c, '1.4.3')
    const items = [...c.querySelectorAll('.rx-criterion')[1].querySelectorAll('.rx-block li')]
      .map((li) => li.textContent)
    const ratio = items.find((t) => t.startsWith('Normal text contrast ratio'))
    expect(ratio).toContain('4.5:1')
    expect(ratio).toContain('WCAG requirement')
    expect(ratio).toContain('Fixed in code')
    const score = items.find((t) => t.startsWith('Compliance score threshold'))
    expect(score).toContain('Configurable: rubric.compliant_threshold')
    expect(score).toContain('ACP heuristic')
    await expand(c, '1.3.1')
    const pt = [...c.querySelectorAll('.rx-block li')].map((li) => li.textContent)
      .find((t) => t.startsWith('Minimum font size'))
    expect(pt).toContain('Fixed in code')
    expect(pt).toContain('ACP heuristic')
  })

  it('shows method, fix and implementation details (rule id, engine, source) in words', async () => {
    const c = await mountPanel()
    await expand(c, '2.4.2')
    const card = c.querySelectorAll('.rx-criterion')[2]
    expect(card.textContent).toContain('AI-assisted')
    const impl = card.querySelector('details.rx-impl')
    expect(impl.querySelector('summary').textContent).toContain('Implementation details')
    expect(impl.textContent).toContain('HTML-TITLE-001')
    expect(impl.textContent).toContain('Assisted fix: a person approves before it is written')
    expect([...impl.querySelectorAll('code')].map((x) => x.textContent)).toEqual(['HTML-TITLE-001', 'acp', 'api/html_audit.py'])
    // A rule the active rubric disables says so.
    await expand(c, '1.4.3')
    expect(c.querySelectorAll('.rx-criterion')[1].textContent).toContain('Disabled in the active rubric')
  })

  it('details are a native button disclosure: aria-expanded, aria-controls, in the Tab order', async () => {
    const c = await mountPanel()
    const btn = toggleFor(c, '1.3.1')
    // A native <button> is what makes Enter and Space activate it in every browser; jsdom does not
    // synthesise that key→click step, so the test pins the element contract and then activates it.
    expect(btn.tagName).toBe('BUTTON')
    expect(btn.getAttribute('type')).toBe('button')
    expect(btn.hasAttribute('tabindex')).toBe(false)
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    const region = c.querySelector(`#${btn.getAttribute('aria-controls')}`)
    expect(region).toBeTruthy()
    expect(region.hidden).toBe(true)
    btn.focus()
    expect(document.activeElement).toBe(btn)
    await act(async () => { btn.click() })
    expect(btn.getAttribute('aria-expanded')).toBe('true')
    expect(region.hidden).toBe(false)
    expect(region.textContent).toContain('Paragraphs styled to look like headings')
    await act(async () => { btn.click() })
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    expect(region.hidden).toBe(true)
    // The accessible name says WHICH criterion, not only "Show details".
    expect(btn.textContent).toBe('Show details for 1.3.1 Info and Relationships')
  })

  it('renders a markup-shaped payload string as literal text', async () => {
    const c = await mountPanel()
    await expand(c, '1.4.3')
    expect(c.textContent).toContain(XSS)
    expect(c.querySelector('img')).toBeNull()
    expect(c.innerHTML).not.toContain('<img')
  })

  it('offers no write controls: only search and the two filters', async () => {
    const c = await mountPanel()
    for (const sc of ['1.3.1', '1.4.3', '2.4.2']) await expand(c, sc)
    const fields = [...c.querySelectorAll('input, select, textarea')]
    expect(fields.map((f) => `${f.tagName}:${f.type}`)).toEqual(['INPUT:search', 'SELECT:select-one', 'SELECT:select-one'])
    expect(c.querySelectorAll('input[type="checkbox"], input[type="radio"], [contenteditable]')).toHaveLength(0)
    // Every button is a disclosure toggle; none saves anything.
    for (const b of c.querySelectorAll('button')) expect(b.hasAttribute('aria-expanded')).toBe(true)
    expect(c.textContent).toContain('Thresholds are shown read-only. Changing them is not available in this version')
  })

  it('lists configurable settings as read-only text', async () => {
    const c = await mountPanel()
    const section = c.querySelector('[aria-labelledby="rx-configurable-h"]')
    expect(section.querySelector('h4').textContent).toBe('Configurable today')
    expect(section.textContent).toContain('Compliance threshold')
    expect(section.textContent).toContain('rubric.compliant_threshold')
    expect(section.textContent).toContain('Changed by: Platform admin')
  })

  it('shows provenance so a stale explanation is detectable', async () => {
    const c = await mountPanel()
    const foot = c.querySelector('footer.rx-provenance').textContent
    expect(foot).toContain('Rule catalog version 2026.09.18')
    expect(foot).toContain('Rubric WCAG 2.1 AA v3')
    expect(foot).toContain('e85fcf7e14f9')
    expect(foot).toContain('Built from commit 734fec2')
    expect(foot).not.toContain('734fec29abcdef')
  })

  it('shows loading, then an error with a working Retry', async () => {
    let fail
    getRuleExplanations.mockImplementationOnce(() => new Promise((_, rej) => { fail = rej }))
    const { container: c, root } = createTestRoot()
    await act(async () => { root.render(createElement(RuleExplanations)) })
    expect(c.querySelector('[role="status"]').textContent).toBe('Loading rule explanations…')
    await act(async () => { fail(new Error('503 Service Unavailable')) })
    await settle()
    const alert = c.querySelector('[role="alert"]')
    expect(alert.textContent).toContain('Could not load rule explanations: 503 Service Unavailable')
    const retry = [...alert.querySelectorAll('button')].find((b) => b.textContent === 'Retry')
    await act(async () => { retry.click() })
    await settle()
    expect(c.querySelector('[role="alert"]')).toBeNull()
    expect(headings(c)).toHaveLength(3)
  })

  it('has no axe WCAG 2.1 A/AA violations with every criterion expanded', async () => {
    const { default: axe } = await import('axe-core')
    const c = await mountPanel()
    for (const sc of ['1.3.1', '1.4.3', '2.4.2']) await expand(c, sc)
    for (const d of c.querySelectorAll('details')) d.open = true
    const r = await axe.run(c, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } })
    // color-contrast cannot be computed in jsdom (see wcagAxeMatrix.test.jsx); the panel uses only
    // existing theme tokens for colour.
    const v = r.violations.filter((x) => x.id !== 'color-contrast')
    expect(v.map((x) => `${x.id}: ${x.nodes[0]?.html}`)).toEqual([])
  })

  it('says it is unavailable in the demo build instead of inventing explanations', async () => {
    const { getRuleExplanations: real } = await vi.importActual('./api.js')
    getRuleExplanations.mockImplementationOnce(real)
    const c = await mountPanel()
    await settle(300)
    expect(c.textContent).toContain('Rule explanations are not available in the demo build')
    expect(headings(c)).toHaveLength(0)
  })
})
