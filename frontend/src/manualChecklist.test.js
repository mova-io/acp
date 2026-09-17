import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { fixSteps, hasGuidance, appName } from './remediationGuide.js'
import { LEGACY_JSPDF_RENDERERS } from './pdfReport.js'
// The retired jsPDF renderer, still checked headlessly; the UI now renders on the server.
const { exportScanReport } = LEGACY_JSPDF_RENDERERS

const here = dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(join(here, f), 'utf8')

describe('remediationGuide — native-app fix steps per OS', () => {
  it('gives a criterion-specific Mac + Windows path for a known SC × format', () => {
    const s = fixSteps('1.1.1', 'xlsx')
    expect(s.where).toMatch(/alt text/i)
    expect(s.mac).toMatch(/Edit Alt Text/i)
    expect(s.win).toMatch(/Edit Alt Text/i)
    expect(hasGuidance('1.1.1')).toBe(true)
  })

  it('distinguishes Mac vs Windows where the menu genuinely differs (3.1.1 language)', () => {
    const s = fixSteps('3.1.1', 'docx')
    expect(s.mac).not.toBe(s.win)          // Tools → Language (Mac) vs Review → Language (Win)
    expect(s.mac).toMatch(/Language/i)
    expect(s.win).toMatch(/Review/i)
  })

  it('falls back to the app Accessibility Checker for an unknown criterion', () => {
    const office = fixSteps('9.9.9', 'docx')
    expect(office.mac).toMatch(/Check Accessibility/i)
    const pdf = fixSteps('9.9.9', 'pdf')
    expect(pdf.mac).toMatch(/Acrobat/i)
    expect(hasGuidance('9.9.9')).toBe(false)
  })

  it('uses no glyphs the PDF font cannot render (→ silently wipes the whole cell)', () => {
    // jsPDF's standard WinAnsi helvetica drops U+2192 and takes the rest of the string with it,
    // leaving a blank cell. Menu paths must use ASCII '>' instead. Guard the whole guide.
    expect(read('remediationGuide.js')).not.toMatch(/[←-⇿➔-➿]/) // arrows blocks
  })

  it('names the native app per format', () => {
    expect(appName('docx')).toBe('Word')
    expect(appName('xlsx')).toBe('Excel')
    expect(appName('pptx')).toBe('PowerPoint')
    expect(appName('pdf')).toBe('Acrobat Pro')
  })
})

// Headless render: capture the real PDF bytes jsPDF would save and assert on them.
async function renderScanReport(extra) {
  const { jsPDF } = await import('jspdf')
  const captured = []
  jsPDF.API.save = function () { captured.push(Buffer.from(this.output('arraybuffer'))); return this }
  await exportScanReport({
    org: 'Acme', targetLevel: 'AA', platformVersion: 'test',
    date: 'July 13, 2026', timestamp: 'July 13, 2026, 10:00 AM PDT',
    totalFiles: 3, conformantN: 1, conformantPct: 33, avgScore: 60,
    statusCounts: { certifiable: 1, issues: 2, uncertain: 0, unanalysable: 0 },
    criteriaAutomated: 6, inScopeCount: 30,
    topFailing: [], heatmap: { depts: [], rows: [] }, byDept: [], bySource: [],
    routing: { fixed: 1, humanRouted: 2, deferred: 0, buckets: [] },
    effort: { autoPct: 33 }, hitl: { total: 0 }, appendix: [],
    ...extra,
  })
  expect(captured.length).toBe(1)
  return captured[0].toString('latin1')
}

describe('Estate report — Manual remediation checklist section', () => {
  const checklist = [
    { sc: '1.1.1', name: 'Non-text Content', level: 'A', fmt: 'xlsx', app: 'Excel', docs: 2,
      where: 'Images, charts...', mac: 'Right-click the chart > Edit Alt Text', win: 'Right-click the chart > Edit Alt Text', specific: true },
    { sc: '3.1.1', name: 'Language of Page', level: 'A', fmt: 'docx', app: 'Word', docs: 1,
      where: 'The document language is not set.', mac: 'Tools > Language > Set As Default', win: 'Review > Language > Set Proofing Language', specific: true },
  ]

  it('renders the checklist with checkboxes and both OS columns', async () => {
    const raw = await renderScanReport({ manualChecklist: checklist, checklistTruncated: false, checklistTotal: 2 })
    expect(raw).toContain('MANUAL REMEDIATION')        // section heading (rendered uppercase)
    expect(raw).toContain('Fix on Mac')               // column header
    expect(raw).toContain('Fix on Windows')           // column header
    expect(raw).toContain('[  ]')                     // tickable checkbox
    expect(raw).toContain('Excel')                    // the app to open
    // Real per-OS step text renders (single-word tokens survive cell wrapping):
    expect(raw).toContain('Tools')                    // Mac 3.1.1: Tools → Language
    expect(raw).toContain('Proofing')                 // Windows 3.1.1: Set Proofing Language
  })

  it('omits the section entirely when nothing failed', async () => {
    const raw = await renderScanReport({ manualChecklist: [] })
    expect(raw).not.toContain('MANUAL REMEDIATION')
  })

  it('notes truncation when the estate exceeds the cap', async () => {
    const raw = await renderScanReport({ manualChecklist: checklist, checklistTruncated: true, checklistTotal: 200 })
    expect(raw).toContain('most actionable')
  })
})

describe('scanReport wires the checklist from real FAIL traces', () => {
  it('builds one row per failing SC × format and passes it to the PDF', () => {
    const s = read('scanReport.js')
    expect(s).toMatch(/import \{ fixSteps, hasGuidance, appName \} from '\.\/remediationGuide\.js'/)
    expect(s).toMatch(/String\(r\.outcome \|\| ''\)\.toUpperCase\(\) !== 'FAIL'/) // FAIL-only
    expect(s).toMatch(/`\$\{sc\}::\$\{fmt\}`/)                                    // per SC×format key
    expect(s).toMatch(/manualChecklist: manualChecklist\.slice\(0, CHECKLIST_CAP\)/)
  })
})
