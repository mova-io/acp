import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

// The approved board's core, actually on the screen.
//
// Ten components landed on main across #551/#558/#559/#560 and NOT ONE was mounted. That is the
// specific failure this file exists to make impossible to repeat, and it is invisible by
// construction: every one of these components self-guards and returns `null` when it has nothing
// measurable, so a component that is never mounted and a component mounted with the wrong props
// look identical on screen — blank — and the entire suite stays green either way.
//
// Source-level deliberately. Each component's BEHAVIOUR is covered by its own DOM tests
// (remediationWorkPanel, remediationApprovals, manualWork, remediationVerify, deliveryPolicy,
// closeout). What none of those can see is whether anything renders them, which is precisely what
// was wrong. This asserts the composition; they assert the components.

const here = dirname(fileURLToPath(import.meta.url))
const read = (f) => readFileSync(join(here, f), 'utf8')

// Comments are stripped before every negative and structural assertion. A comment naming a
// component is not a mount, and a comment explaining a removal necessarily quotes what it removed
// — testing the vocabulary instead of the claim has failed on correct code four times in this
// codebase already.
const code = (f) => read(f)
  .replace(/\{\/\*[\s\S]*?\*\/\}/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const PAGE_LEVEL = [
  ['RemediationWork', 'R2/R3 — the work partitioned, and the deterministic batch'],
  ['ManualWork', 'R6 — work ACP cannot do at all'],
  ['RemediationVerify', 'R9 — did the fixes hold'],
  ['DeliveryPanel', 'R11 — where the fixed files go'],
  ['CloseoutPanel', 'R12 — close the loop'],
]

describe('the board core is mounted, not merely shipped', () => {
  const rem = () => code('Remediate.jsx')

  for (const [name, what] of PAGE_LEVEL) {
    it(`mounts ${name} (${what})`, () => {
      const s = rem()
      expect(s, `${name} is imported`).toMatch(new RegExp(`import ${name} from '\\./${name}\\.jsx'`))
      expect(s, `${name} is RENDERED, not just imported`).toMatch(new RegExp(`<${name}[\\s/>]`))
    })
  }

  it('does NOT mount RemediationApprovals — there is one approval surface, not two', () => {
    // Removed 2026-09-01 by the Remediate redesign. It offered a second place to approve the same
    // drafts the review panel approves, so "which one is authoritative?" had no answer on the
    // screen; and it was fed UI-shaped rows (`ruleId`) while reading DB field names (`rule_id`,
    // `status`), so its criterion column rendered empty for every row in production. The panel file
    // is kept per the retired-feature policy and tracked by unmountedComponents.test.jsx.
    const s = rem()
    expect(s).not.toMatch(/import RemediationApprovals from/)
    expect(s).not.toMatch(/<RemediationApprovals[\s/>]/)
  })

  it('leaves the review panel as the only finding-level decision surface', () => {
    const s = rem()
    // The inbox is mounted and its decisions route through the same `act` the removed panel used,
    // so this removed a screen rather than a capability.
    expect(s).toMatch(/<RemediationInbox/)
    expect(s).toMatch(/if \(d\.state === 'accepted'\) return act\(f\.id, 'approved'/)
    // (…and pass `f`, the viewed row, as the version the decision is bound to.)
    expect(s).toMatch(/if \(d\.state === 'rejected'\) return act\(f\.id, 'rejected'[,)]/)
  })

  it('gives every lane-aware panel the capability tables', () => {
    // Without cap/assessment these components cannot tell `auto` from `assisted` from `human`, and
    // the lanes are the entire point of the partition. They would not crash — they would quietly
    // render a different, wrong division of the work.
    const s = rem()
    for (const name of ['RemediationWork', 'ManualWork', 'RemediationVerify']) {
      const m = s.match(new RegExp(`<${name}[^/]*?/>`, 's'))
      expect(m, `${name} mount found`).toBeTruthy()
      expect(m[0], `${name} receives cap`).toMatch(/cap=\{cap\}/)
      expect(m[0], `${name} receives assessment`).toMatch(/assessment=\{assessment\}/)
    }
  })

  it('requires the plan before starting from the work breakdown', () => {
    const s = rem()
    const m = s.match(/<RemediationWork[^/]*?\/>/s)
    expect(m, 'RemediationWork mount found').toBeTruthy()
    expect(m[0]).toMatch(/onOpenPlan=\{readOnly \? undefined : openRemediationPlan\}/)
    expect(m[0]).toMatch(/applying=\{remBusy\}/)
    // RemediationWork supplies filenames while the older page controls supply file records.
    // Both must enter the same endpoint without producing an undefined scope.
    expect(s).toMatch(/typeof f === 'string' \? f : f\?\.file/)
  })

  it('the review panel reads the same queue the header counts are derived from', () => {
    // Two surfaces disagreeing about what is waiting for a decision is the four-denominator defect
    // in a new place. One queue feeds the workspace AND the run header's counts.
    const s = rem()
    expect(s).toMatch(/queue=\{inboxQueue\}/)
    expect(s).toMatch(/const workflowCount = \(k\) => inboxQueue\.filter/)
  })

  it('a decision REPORTS whether it saved, so the queue cannot advance past a refused write', () => {
    // `act` used to be fire-and-forget: the server could refuse a decision and the reviewer was
    // still advanced to the next finding, with the only trace a banner rendered above the whole
    // inbox. It now returns the write's promise and re-throws on failure, and the review pane
    // awaits that before it moves anyone.
    const s = rem()
    expect(s).toMatch(/undoAct\(item, kind, err, settled\.outcome\)\s*\n\s*throw err/)
    expect(s).toMatch(/reconcileHitlPutFailure\(item\?\.id, wanted, err\)/)
    expect(s).toMatch(/return p\.then\(/)
    const inbox = code('RemediationInbox.jsx')
    expect(inbox).toMatch(/await onDecide\?\.\(f, decision\)/)
    expect(inbox).toMatch(/if \(!ok\) \{ setSelectedId\(f\.id\); return \}/)
  })

  it('App passes the capability tables down to Remediate', () => {
    // The chain is App → Remediate → the panels. A break anywhere in it renders every lane-aware
    // panel blank, silently.
    const s = code('App.jsx')
    const m = s.match(/<Remediate[\s\S]*?\/>/)
    expect(m).toBeTruthy()
    expect(m[0]).toMatch(/cap=\{cap\}/)
    expect(m[0]).toMatch(/assessment=\{assessment\}/)
  })
})

describe('what this commit deliberately did NOT do', () => {
  it('leaves the existing review queue in place', () => {
    // Mounting ten components and deleting the screen they replace are two changes. Doing both at
    // once makes a regression impossible to attribute, so the removal is its own commit. If this
    // case ever fails, check that the removal was intended before "fixing" it.
    const s = code('Remediate.jsx')
    expect(s).toMatch(/<RemediationInbox/)
    expect(s).toMatch(/id="rem-review"/)
  })
})

describe('the per-item four reach the detail pane, beside their subject', () => {
  // These four were the "still unmounted, and that is recorded" cases. They have now been wired,
  // so that test did its job and is replaced rather than deleted: the same four names are still
  // asserted, in the opposite direction.
  //
  // R7 and R10 answer a question about ONE finding or ONE document, so a page-level mount
  // would have no subject. They are injected into RemediationInbox's detail pane through a render
  // prop, which keeps the composition with Remediate rather than making the inbox — already the
  // page's hardest state — the place every future panel lands. (R4 — the "Preview one fix" stepper —
  // was dropped by Deva and confirmed by Jeremy; see issue #568.)
  const rem = () => code('Remediate.jsx')

  for (const name of ['DocumentAudit']) {
    it(`renders ${name} for the selected finding`, () => {
      const s = rem()
      expect(s).toMatch(new RegExp(`import ${name} from '\\./${name}\\.jsx'`))
      expect(s).toMatch(new RegExp(`<${name}[\\s/>]`))
    })
  }

  it('keeps the document pipeline out of the human decision pane', () => {
    const s = rem()
    expect(s).not.toMatch(/import RemediationDocProgress from/)
    expect(s).not.toMatch(/<RemediationDocProgress[\s/>]/)
    expect(code('RemediationDocProgress.jsx')).toMatch(/export default function RemediationDocProgress/)
  })

  it('mounts FixOutcomes (R8) at PAGE level, not per finding', () => {
    // "What did not work in this run" is a property of the run, not of whichever finding happens
    // to be selected — so it must not be inside the detail-pane render prop.
    const s = rem()
    expect(s).toMatch(/<FixOutcomes scanId=\{run\?\.id\}/)
    // Sliced between two anchors rather than matched on indentation — a reformat must not silently
    // turn this into a test that finds nothing and therefore asserts nothing.
    const from = s.indexOf('renderDetailExtra=')
    const to = s.indexOf('queue={inboxQueue}', from)
    expect(from, 'renderDetailExtra found').toBeGreaterThan(-1)
    expect(to, 'end of the detail-pane block found').toBeGreaterThan(from)
    expect(s.slice(from, to)).not.toMatch(/<FixOutcomes/)
  })

  it('guards the empty selection rather than rendering three empty frames', () => {
    expect(rem()).toMatch(/renderDetailExtra=\{\(sel\) => \(sel \?/)
  })

  it('the inbox takes the components from its parent instead of importing them', () => {
    // A render prop, so RemediationInbox gains no new imports. If it starts importing board
    // components directly, it becomes the default home for every future panel.
    const inbox = code('RemediationInbox.jsx')
    expect(inbox).toMatch(/renderDetailExtra = null/)
    expect(inbox).toMatch(/renderDetailExtra \? renderDetailExtra\(selected\) : null/)
    for (const name of ['RemediationDocProgress', 'DocumentAudit']) {
      expect(inbox, `${name} must not be imported by the inbox`).not.toMatch(
        new RegExp(`import ${name} from`))
    }
  })
})

describe('R1 (board 9) — the hero names which assessment this backlog is from', () => {
  const rem = () => code('Remediate.jsx')

  it('passes assessedAt to the run header, which omits rather than invents', () => {
    // Pre-formatted by the caller — the same "format once, pass a string" contract every other
    // stamp on these tabs follows — so this component formats no dates of its own. The redesign
    // moved the stamp from the deleted hero into the compact run header, and the omission guard
    // moved with it: RemediationRunHeader renders nothing at all for a null assessedAt.
    const s = rem()
    expect(s).toMatch(/assessedAt = null/)
    expect(s).toMatch(/<RemediationRunHeader[\s\S]{0,400}?assessedAt=\{assessedAt\}/)
    expect(code('RemediationRunHeader.jsx')).toMatch(/Assessment:/)
  })

  it('App passes the run\'s own assessed_at, formatted via fmtStamp — never the raw column', () => {
    const app = code('App.jsx')
    expect(app).toMatch(/<Remediate[\s\S]{0,700}?assessedAt=\{fmtStamp\(run\?\.assessed_at\)\}/)
  })
})

describe('R15 (board 10) — undo an applied fix, mounted only for an auto-applied row', () => {
  const rem = () => code('Remediate.jsx')

  it('imports UndoFix', () => {
    expect(rem()).toMatch(/import UndoFix from '\.\/UndoFix\.jsx'/)
  })

  it('mounts UndoFix in the detail pane, guarded on sel.autoApplied', () => {
    // A drafted-AI or manually-authored finding was never something ACP applied on its own — the
    // guard is what keeps this from offering an "undo" that has nothing to undo.
    const s = rem()
    expect(s).toMatch(/sel\.autoApplied && !sel\.inspectionOnly && \([\s\S]{0,200}?<UndoFix\b/)
    expect(s).toMatch(/<UndoFix[\s\S]{0,200}?ruleId=\{sel\.ruleId\}/)
    expect(s).toMatch(/<UndoFix[\s\S]{0,200}?onUndone=\{onRefresh\}/)
  })
})

it('keeps initial planning and retires repeated Live release controls in favor of saved run details', () => {
  const source = code('Remediate.jsx')
  expect(source.slice(source.indexOf('plan={<>'), source.indexOf('reviewCount={reviewCounts', source.indexOf('plan={<>')))).toContain('releaseOption={<RemediationReleasePlan')
  expect(source.slice(source.indexOf('live={<>'))).not.toMatch(/<RemediationAutoRelease(?! statusOnly)/)
  expect(source).not.toContain('<RemediationAutoRelease'); expect(source).toContain('useAutomaticReleaseStatus(runId, impactScope, setAutomaticReleaseState)')
  expect(source).toContain('<RemediationOpsPanel streamlined')
  expect(source).toContain('<AcceptedRemediationPlanSummary')
  expect(code('RemediationImpactCard.jsx')).toContain('{releaseOption}')
  expect(source).toContain('runServerRemediation(impactScope, policy, intent)')
  expect(source).toContain('authorizeAcceptedRelease(runId, scope, r, releaseIntent)')
})
