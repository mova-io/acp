/**
 * The remediation report's mode menu, on the real Remediate page.
 *
 * Remediate used to offer one unlabelled "Remediation report (PDF)" button — no Summary, no
 * Reviewer packet, no Full evidence — while the file drawer and the scan report both had all
 * three. The evidence behind it is covered in remediationReportModes.test.jsx.
 *
 * api.js is deliberately NOT mocked here: mounting the whole page pulls in most of it, and the
 * assertion is about page composition (same approach as remediatePageRender.test.jsx).
 */
import { describe, it, expect } from 'vitest'
import { renderToString } from 'react-dom/server'
import { createElement } from 'react'
import Remediate from './Remediate.jsx'

describe('the remediation report menu', () => {
  it('offers Summary, Reviewer packet and Full evidence instead of one unlabelled button', () => {
    history.replaceState({}, '', '/?tab=remediate')
    const container = document.createElement('div')
    container.innerHTML = renderToString(createElement(Remediate, { run: { id: 's1', status: 'completed' }, files: [] }))
    const menu = [...container.querySelectorAll('.reportmode')].find((d) => /Remediation report/.test(d.textContent))
    expect(menu, 'the Remediation report menu is not on the page').toBeTruthy()
    expect([...menu.querySelectorAll('.reportmode-row')].map((r) => r.getAttribute('aria-label')))
      .toEqual(['Summary', 'Reviewer packet', 'Full evidence'])
    // The old single-shot button is gone.
    expect(container.textContent).not.toMatch(/Remediation report \(PDF\)/)
  })
})
