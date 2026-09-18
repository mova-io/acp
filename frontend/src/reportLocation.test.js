// Contract 1: the browser's location parser against the SAME table the server's is pinned to.
//
// tests/fixtures/report_location_cases.json is read here and by tests/test_report_location.py.
// A report built on the server and one built here from the same finding must name the same place
// — neither may fall back to page 1, and neither may print `docx:paragraph:14` at a reader.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseLocation, locationOf, factsFindings } from './reportEvidence.js'

const here = dirname(fileURLToPath(import.meta.url))
const { cases } = JSON.parse(readFileSync(join(here, '..', '..', 'tests', 'fixtures', 'report_location_cases.json'), 'utf8'))
const KEYS = ['label', 'kind', 'page', 'slide', 'sheet', 'cell', 'objectId', 'raw']
const pick = (loc) => (loc ? Object.fromEntries(KEYS.map((k) => [k, loc[k]])) : null)

describe('the shared location table', () => {
  it('has the cases the server test reads', () => {
    expect(cases.length).toBeGreaterThan(20)
  })
  it.each(cases.map((c) => [c.name, c]))('%s', (_name, c) => {
    expect(pick(parseLocation(c.location, c.page, c.fmt))).toEqual(c.expect)
    // locationOf on the raw issue row gives the same object (plus href)
    expect(pick(locationOf({ location: c.location, page: c.page }, { fmt: c.fmt }))).toEqual(c.expect)
  })
})

describe('server-shaped locations are used verbatim', () => {
  it('keeps the contract-1 object the facts carry, label and all', () => {
    const server = { label: 'Slide 2 · shape 5', kind: 'object', page: 2, slide: 2, sheet: null, cell: null, objectId: 'shape:5', element: 'pptx:slide:1:element:5', raw: 'pptx:slide:1:element:5' }
    const [f] = factsFindings({ identity: { file: 'deck.pptx' }, findings: [{ id: 'x', ruleId: 'r', sc: '1.1.1', location: server }] })
    expect(pick(f.location)).toEqual(pick(server))
    expect(f.page).toBe(2)
    expect(f.slide).toBe(2)
  })
  it('re-parses a legacy nested location that predates contract 1 rather than printing the machine string', () => {
    const legacy = { label: 'docx:paragraph:3', page: 1, slide: null, sheet: null, cell: null, element: 'docx:paragraph:3' }
    const loc = locationOf({ location: legacy }, { fmt: 'docx' })
    expect(loc.label).toBe('Paragraph 4')
    expect(loc.page).toBeNull()
  })
})
