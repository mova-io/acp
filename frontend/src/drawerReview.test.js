import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(join(here, f), 'utf8')

describe('review in place — the FileDrawer mounts the real EvidenceCard per finding', () => {
  const src = read('FileDrawer.jsx')

  it('fetches review items scoped to this scan + file — pending, plus approved ones the apply refused — and stays in sync with the bell', () => {
    // No status filter on the fetch: an approved-but-refused row (apply_outcome) must reach the
    // drawer, and reviewableInPlace (reviewCard.js) is the single definition of what is shown.
    expect(src).toMatch(/listHitlQueue\(scanId\)/)
    expect(src).not.toMatch(/listHitlQueue\(scanId, 'pending'\)/)
    // …plus an approval the writer is holding (approval_recheck_required), which needs a fresh approval.
    expect(src).toMatch(/\.filter\(\(r\) => r\.file === file\.file && \(reviewableInPlace\(r\) \|\| needsReapproval\(r\)\)\)/)
    expect(src).toMatch(/import \{[^}]*\breviewableInPlace\b[^}]*\} from '\.\/reviewCard\.js'/)
    expect(src).toMatch(/addEventListener\('acp:hitl-changed', load\)/)
  })

  it('a finding with a pending item gets the SAME EvidenceCard the inbox uses — no fork', () => {
    expect(src).toMatch(/import EvidenceCard from '\.\/EvidenceCard\.jsx'/)
    expect(src).toMatch(/<EvidenceCard item=\{hi\} onAct=\{drawerAct\}/)
  })

  it('acting on the card removes the item locally and notifies the bell to reconcile', () => {
    expect(src).toMatch(/updateHitlItem\(itemId, status, note, approvedValue, \{[\s\S]{0,160}expectedVersion:/)
    expect(src).toMatch(/setHitlItems\(\(cur\) => cur\.filter\(\(h\) => h\.id !== itemId\)\)/)
    expect(src).toMatch(/dispatchEvent\(new Event\('acp:hitl-changed'\)\)/)
  })

  it('matches queue rows to findings across both rule-id forms (SC_1_1_1 and 1.1.1)', () => {
    expect(src).toMatch(/scOfWcag\(h\.rule_id\) \|\| h\.rule_id/)
  })
})
