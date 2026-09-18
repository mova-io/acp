import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { createElement, act } from 'react'
import { jsPDF } from 'jspdf'
import Overview from './Overview.jsx'
import { createTestRoot, unmountAll } from './testRoots.js'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
let downloads, errors, save
beforeEach(() => {
  downloads = []
  save = jsPDF.API.save
  jsPDF.API.save = function (filename) { downloads.push({ filename, bytes: this.output('arraybuffer') }); return this }
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')))
  errors = vi.spyOn(console, 'error').mockImplementation(() => {})
})
afterEach(() => { unmountAll(); jsPDF.API.save = save; vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function clickReport(certifiable = 1, assessed = true) {
  const { root, container } = createTestRoot()
  const files = [{ file: 'report.docx', type: 'DOCX', score: assessed ? 80 : null, status: assessed ? 'done' : 'discovered', issues: assessed ? [{ wcag: 'SC_1_3_1', severity: 'SERIOUS' }] : [] }]
  await act(async () => { root.render(createElement(Overview, { run: { id: 'export-test', files: 2, certifiable, avg_score: assessed ? 80 : null, status: 'done', scope: {} }, files })) })
  const button = [...container.querySelectorAll('button')].find(button => button.textContent === 'Quarterly governance report')
  await act(async () => { button.click() })
  await settle(button)
  return { container, button }
}

// Wait for the export to FINISH, rather than for a fixed 100ms. The click starts a dynamic import
// and a full jsPDF render, and 100ms is a guess about how long that takes on an idle machine —
// under a loaded full-suite run it is not enough, and the failure reads as "no PDF was produced"
// rather than as "the test did not wait". The button re-enables when the handler settles, so that
// is the condition to wait on.
async function settle(button, tries = 200) {
  for (let i = 0; i < tries; i++) {
    if (!button.disabled) return
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)) })
  }
}

describe('Quarterly governance report download from Overview', () => {
  it.each([[2, true], [1, true], [0, true], [null, false]])('creates a real PDF for certifiable=%s assessed=%s', async (certifiable, assessed) => {
    await clickReport(certifiable, assessed)
    expect(errors.mock.calls).toEqual([])
    expect(downloads).toHaveLength(1)
    expect(downloads[0].filename).toBe('mova-quarterly-governance-report.pdf')
    expect(new TextDecoder().decode(downloads[0].bytes).startsWith('%PDF-')).toBe(true)
  })
  it('says on its own first page that this browser-drawn PDF is not tagged', async () => {
    const { container } = await clickReport()
    expect(downloads).toHaveLength(1)
    const text = new TextDecoder('latin1').decode(downloads[0].bytes)
    expect(text).toContain('About this PDF: it is drawn in your browser and is not tagged')
    // …and the menu says so before anyone downloads it
    expect(container.querySelector('#rep-gov-hint').textContent).toMatch(/not tagged for assistive technology/)
  })
  it('shows an export error and allows a successful retry', async () => {
    const successfulSave = jsPDF.API.save
    jsPDF.API.save = function () { throw new Error('download failed') }
    const { container, button } = await clickReport()
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('could not be generated')
    expect(button.disabled).toBe(false)
    expect(downloads).toHaveLength(0)
    expect(errors.mock.calls.some(([message]) => message === 'PDF export failed')).toBe(true)
    jsPDF.API.save = successfulSave
    await act(async () => { button.click() })
    await settle(button)
    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(downloads).toHaveLength(1)
    expect(button.disabled).toBe(false)
  })

})
