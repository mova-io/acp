// Contract 1: the browser's location parser against the SAME table the server's is pinned to.
//
// tests/fixtures/report_location_cases.json is read here and by tests/test_report_location.py.
// A report built on the server and one built here from the same finding must name the same place
// — neither may fall back to page 1, and neither may print `docx:paragraph:14` at a reader.
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { parseLocation, locationOf, factsFindings, buildChangeCards, LEGACY_NOTE_QUALIFIER, withLocationSource } from './reportEvidence.js'

const here = dirname(fileURLToPath(import.meta.url))
const table = JSON.parse(readFileSync(join(here, '..', '..', 'tests', 'fixtures', 'report_location_cases.json'), 'utf8'))
const { cases } = table
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

describe('R1: where a saved change location came from', () => {
  it('uses the same legacy-note qualifier text as the server (shared fixture)', () => {
    expect(LEGACY_NOTE_QUALIFIER).toBe(table.legacyNoteQualifier)
  })
  it('keeps the server location source on a facts change card and does not qualify twice', () => {
    const server = { label: `image 1${table.legacyNoteQualifier}`, kind: null, page: null, slide: null, sheet: null, cell: null, objectId: null, element: 'image 1', raw: 'image 1', source: 'legacy_note' }
    const [card] = buildChangeCards({ file: 'a.docx', diffs: [{ id: 'a.docx::1.4.5::0', ruleId: '1.4.5', after: 'A cow', locator: 'image 1', locationSource: 'legacy_note', location: server }] })
    expect(card.location.label).toBe(server.label)
    expect(card.location.source).toBe('legacy_note')
    expect(card.locationSource).toBe('legacy_note')
  })
  it('qualifies a raw store row the browser labels itself, and drops any page it might imply', () => {
    const [card] = buildChangeCards({ file: 'p.pdf', diffs: [{ rule_id: '1.1.1', seq: 0, before: '', after: 'Chart', locator: 'pdf:fig:2:0', page: null, location_source: 'legacy_note' }] })
    expect(card.location.label).toBe(`Page 2 · figure 1${table.legacyNoteQualifier}`)
    expect(card.location.page).toBeNull()
    expect(card.location.source).toBe('legacy_note')
  })
  it('a recorded Office locator never names a page, and an unknown row has no location', () => {
    const [rec, none] = buildChangeCards({ file: 'a.docx', diffs: [
      { rule_id: '1.3.1', seq: 0, before: 'b', after: 'a', locator: 'word:p:4', page: 3, location_source: 'recorded' },
      { rule_id: '2.4.2', seq: 0, before: '', after: 'Title', locator: null, page: null, location_source: null },
    ] })
    expect(rec.location.label).toBe('Paragraph 4')
    expect(rec.location.page).toBeNull()
    expect(rec.locationSource).toBe('recorded')
    expect(none.location).toBeNull()
    expect(none.locationSource).toBeNull()
  })
  it('withLocationSource leaves recorded and absent locations alone', () => {
    const loc = parseLocation('pdf:fig:2:0', 2, 'pdf')
    expect(withLocationSource(loc, 'recorded')).toBe(loc)
    expect(withLocationSource(null, 'legacy_note')).toBeNull()
  })
})

describe('rf-C: a machine string is never glued to a page label', () => {
  it.each(['/Document/Sect[2]/Figure', 'rId5', 'a_b', 'x:y:z', '#frag', '{guid}'])('%s', (raw) => {
    const loc = parseLocation(raw, 4, 'pdf')
    expect(loc.label).toBe('Page 4')
    expect(loc.raw).toBe(raw)
  })
})
