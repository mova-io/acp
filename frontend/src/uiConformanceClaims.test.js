// The self-check "Conformance report (PDF)" (exportConformanceReport) and docs/conformance-report.md
// describe ACP's OWN UI. Both once asserted full WCAG 2.1 AA conformance from axe + code review, with
// 1.4.13 marked Not Applicable (InfoTip/Term tooltips open on hover and focus) and 4.1.3 marked
// Supports from markup alone (axe has no 4.1.3 rule; no screen-reader testing had been done). The
// 2026-09-16 self-assessment also found a 1.4.3 failure on the update banner. These tests keep the
// two statements honest and in agreement until real evidence (docs/ui-accessibility-evaluation.md)
// supports a stronger claim — at which point update both, and this test, together.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const pdf = readFileSync(join(here, 'pdfReport.js'), 'utf8')
const md = readFileSync(join(here, '..', '..', 'docs', 'conformance-report.md'), 'utf8')
const protocol = readFileSync(join(here, '..', '..', 'docs', 'ui-accessibility-evaluation.md'), 'utf8')

const acrRow = (sc) => pdf.match(new RegExp(`\\['${sc.replace(/\./g, '\\.')}', '[^']*', 'AA?', '([^']*)'`))?.[1]
const mdRow = (sc) => md.split('\n').find((l) => l.startsWith(`| ${sc} `))?.split('|')[3]?.trim()

describe('UI conformance claims', () => {
  it('neither statement asserts the UI conforms to WCAG 2.1 AA', () => {
    expect(pdf).not.toMatch(/platform UI conforms to WCAG/)
    expect(pdf).not.toMatch(/zero Level A\/AA violations/)
    expect(pdf).toContain('screen-reader announcement evidence remains pending')
    expect(md).not.toMatch(/\*\*conforms to WCAG 2\.1 Level AA\*\*/)
    expect(md).not.toMatch(/\*\*Status:\*\* Conformant/)
  })

  // The PDF once said "axe-core across every view (zero Level A/AA violations)" and both said
  // "axe-core, all views". Neither was evidenced: the repo's axe suites cover selected views with the
  // High-contrast palette forced on and never render the update banner, and the September 2026
  // self-assessment reported a 1.4.3 failure there.
  it('neither statement claims a zero-violation or every-view automated result', () => {
    for (const src of [pdf, md]) {
      expect(src).not.toMatch(/\b(zero|no|0)\s+(WCAG\s+)?(Level\s+)?A\s*\/\s*AA\s+violations/i)
      expect(src).not.toMatch(/zero (axe|automated|accessibility)?\s*violations/i)
      expect(src).not.toMatch(/axe-core[^.\n]{0,20}\b(all|every) views?\b/i)
      expect(src).not.toMatch(/\b(across|on|in) (all|every) views?\b/i)
      expect(src).toMatch(/do not establish conformance/)
      expect(src).toMatch(/September 2026 self-assessment reported a 1\.4\.3/)
      expect(src).toContain('Rows without recorded manual or assistive-technology evidence should be read as needing verification.')
    }
    expect(pdf).toMatch(/Screen-reader testing has not been performed/)
    expect(protocol).not.toMatch(/\b(zero|no|0)\s+(WCAG\s+)?(Level\s+)?A\s*\/\s*AA\s+violations/i)
    expect(protocol).not.toMatch(/zero (axe|automated|accessibility)?\s*violations/i)
  })

  // Part 2 counts which criteria have a detect/remediate capability. That is not conformance of
  // any document the platform outputs, and must never be worded as if it were.
  it('document capability coverage is not presented as conformance', () => {
    for (const src of [pdf, md]) {
      expect(src).not.toMatch(/conformance reached/i)
      expect(src).not.toMatch(/legally-required criterion .* is covered/i)
      expect(src).toMatch(/capability coverage, not conformance/i)
      expect(src).toMatch(/does not mean any individual output document conforms/)
    }
  })

  it('the 1.4.3 row does not attribute our computed ratio to the self-assessment', () => {
    const pdfRow = pdf.split('\n').find((l) => l.includes("['1.4.3'"))
    expect(pdfRow).not.toMatch(/\d:1/)
    expect(mdRow('1.4.3') && md.split('\n').find((l) => l.startsWith('| 1.4.3 '))).toMatch(/Our own calculation/)
  })

  it.each([
    ['1.4.3', 'Partially Supports'],
    ['1.4.11', 'Partially Supports'],   // banner dismiss glyph and focus ring, computed from source
    ['1.4.13', 'Needs verification'],
    ['4.1.2', 'Partially Supports'],    // tab-pattern gaps reproduced in jsdom; Review tabs unchanged
    ['4.1.3', 'Needs verification'],
  ])('%s is reported as %s in the PDF and the doc', (sc, level) => {
    expect(acrRow(sc)).toBe(level)
    expect(mdRow(sc)?.replace(/\*/g, '')).toBe(level)
  })
})
