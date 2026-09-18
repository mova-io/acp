/**
 * ReportModeMenu at the DOM level (jsdom). The browser preview server serves the SHARED checkout
 * (CLAUDE.md), so this is where the worktree's menu is actually exercised.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { createElement } from 'react'
import { act } from 'react-dom/test-utils'
import { createTestRoot, unmountAll } from './testRoots.js'
import ReportModeMenu from './ReportModeMenu.jsx'

afterEach(unmountAll)

const flush = async () => { await act(async () => { for (let i = 0; i < 5; i++) await Promise.resolve() }) }

async function mount(props) {
  const { container, root } = createTestRoot()
  await act(async () => { root.render(createElement(ReportModeMenu, props)) })
  return container
}
const btn = (c, name) => c.querySelector(`button[aria-label="${name}"]`)

describe('ReportModeMenu', () => {
  it('offers Summary, Reviewer packet and Full evidence in every supplied format', async () => {
    const c = await mount({ label: 'Document report', formats: [
      { key: 'pdf', label: 'PDF', run: vi.fn() }, { key: 'html', label: 'HTML', run: vi.fn() }] })
    const names = [...c.querySelectorAll('button')].map((b) => b.getAttribute('aria-label'))
    expect(names).toEqual([
      'Summary — PDF', 'Summary — HTML',
      'Reviewer packet — PDF', 'Reviewer packet — HTML',
      'Full evidence — PDF', 'Full evidence — HTML',
    ])
    expect(c.querySelector('summary').textContent).toBe('Document report')
  })

  it.each([['Summary', 'summary'], ['Reviewer packet', 'reviewer'], ['Full evidence', 'full']])(
    '%s calls the chosen format with mode %s', async (label, mode) => {
      const pdf = vi.fn(async () => ({ ok: true }))
      const html = vi.fn(async () => undefined)
      const c = await mount({ formats: [{ key: 'pdf', label: 'PDF', run: pdf }, { key: 'html', label: 'HTML', run: html }] })
      await act(async () => { btn(c, `${label} — HTML`).click() })
      await flush()
      expect(html).toHaveBeenCalledWith(mode)
      expect(pdf).not.toHaveBeenCalled()
      await act(async () => { btn(c, `${label} — PDF`).click() })
      await flush()
      expect(pdf).toHaveBeenCalledWith(mode)
      expect(c.querySelector('[role="alert"]')).toBeNull()
      expect(c.querySelector('[role="status"]').textContent).toMatch(new RegExp(`${label} \\(PDF\\) generated`))
    })

  it('a thrown export shows a visible error, not only a console message', async () => {
    const c = await mount({ formats: [{ key: 'pdf', label: 'PDF', run: async () => { throw new Error('render service down') } }] })
    await act(async () => { btn(c, 'Full evidence — PDF').click() })
    await flush()
    const alert = c.querySelector('[role="alert"]')
    expect(alert).toBeTruthy()
    expect(alert.textContent).toMatch(/Full evidence \(PDF\) was not generated: render service down/)
  })

  it('a refusal returned as a value ({ok:false}) is shown too', async () => {
    const c = await mount({ formats: [{ key: 'pdf', label: 'PDF', run: async () => ({ ok: false, message: 'Report too large (413)' }) }] })
    await act(async () => { btn(c, 'Summary — PDF').click() })
    await flush()
    expect(c.querySelector('[role="alert"]').textContent).toMatch(/Report too large \(413\)/)
  })

  it('shows progress and blocks a second export while one is generating', async () => {
    let release
    const run = vi.fn(() => new Promise((r) => { release = r }))
    const c = await mount({ formats: [{ key: 'pdf', label: 'PDF', run }] })
    await act(async () => { btn(c, 'Reviewer packet — PDF').click() })
    expect(c.querySelector('[role="status"]').textContent).toMatch(/Generating Reviewer packet \(PDF\)/)
    expect(btn(c, 'Summary — PDF').disabled).toBe(true)
    await act(async () => { release({ ok: true }) })
    await flush()
    expect(btn(c, 'Summary — PDF').disabled).toBe(false)
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('each mode states its own purpose, not a length', async () => {
    const c = await mount({ formats: [{ key: 'pdf', label: 'PDF', run: vi.fn() }], modeNotes: { full: 'Large scan: use per-file packets.' } })
    const rows = [...c.querySelectorAll('.reportmode-row')].map((r) => r.textContent)
    expect(rows[0]).toMatch(/For a decision: one page/)
    expect(rows[1]).toMatch(/For the person confirming the work: each change to confirm and each remaining action/)
    expect(rows[2]).toMatch(/For audit: every recorded finding, change and decision, with no caps/)
    expect(rows[2]).toMatch(/Large scan: use per-file packets\./)
    expect(rows[0]).not.toMatch(/Large scan/)
  })

  it('a cancellable format gets a signal, live progress text and a Cancel button', async () => {
    let seen
    const run = vi.fn((mode, ctx) => new Promise((resolve) => {
      seen = ctx
      ctx.onProgress({ n: 3 })
      ctx.signal.addEventListener('abort', () => resolve({ ok: false, cancelled: true, message: '3 of 9 done; 6 not.' }))
    }))
    const c = await mount({ formats: [{ key: 'zip', label: 'ZIP', cancellable: true, progressText: (p) => `${p.n} of 9 done`, run }] })
    await act(async () => { btn(c, 'Full evidence — ZIP').click() })
    await flush()
    expect(run).toHaveBeenCalledWith('full', expect.objectContaining({ signal: expect.any(AbortSignal) }))
    expect(c.querySelector('[role="status"]').textContent).toBe('3 of 9 done')
    const cancel = c.querySelector('button.reportmode-cancel')
    await act(async () => { cancel.click() })
    await flush()
    expect(seen.signal.aborted).toBe(true)
    expect(c.querySelector('[role="alert"]').textContent).toBe('Full evidence (ZIP) was cancelled. 3 of 9 done; 6 not.')
    expect(c.querySelector('button.reportmode-cancel')).toBeNull()
  })

  it('an incomplete export is neither "generated" nor "not generated", and its extra downloads are offered', async () => {
    const run = async () => ({ ok: false, incomplete: true, message: 'Exported 7 of 8.', downloads: [{ label: 'Master index (CSV)', filename: 'i.csv', blob: new Blob(['x']) }] })
    const c = await mount({ formats: [{ key: 'zip', label: 'ZIP', cancellable: true, run }] })
    await act(async () => { btn(c, 'Summary — ZIP').click() })
    await flush()
    expect(c.querySelector('[role="alert"]').textContent).toBe('Summary (ZIP) was downloaded but is INCOMPLETE. Exported 7 of 8.')
    expect(c.querySelector('[role="status"]').textContent).toBe('')
    expect([...c.querySelectorAll('.reportmode-downloads button')].map((b) => b.textContent)).toEqual(['Download Master index (CSV)'])
  })

  it('a format can be limited to some modes', async () => {
    const c = await mount({ formats: [{ key: 'pdf', label: 'PDF', run: vi.fn() }, { key: 'zip', label: 'ZIP', modes: ['full'], run: vi.fn() }] })
    expect(btn(c, 'Full evidence — ZIP')).toBeTruthy()
    expect(btn(c, 'Summary — ZIP')).toBeNull()
  })

  it('inline variant renders a labeled group without its own disclosure', async () => {
    const c = await mount({ inline: true, label: 'Scan report', formats: [{ key: 'pdf', label: 'PDF', run: vi.fn() }] })
    expect(c.querySelector('details')).toBeNull()
    expect(c.querySelector('[role="group"][aria-label="Scan report"]')).toBeTruthy()
    expect(c.querySelectorAll('button')).toHaveLength(3)
  })
})
