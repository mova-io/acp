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

  it('inline variant renders a labeled group without its own disclosure', async () => {
    const c = await mount({ inline: true, label: 'Scan report', formats: [{ key: 'pdf', label: 'PDF', run: vi.fn() }] })
    expect(c.querySelector('details')).toBeNull()
    expect(c.querySelector('[role="group"][aria-label="Scan report"]')).toBeTruthy()
    expect(c.querySelectorAll('button')).toHaveLength(3)
  })
})
