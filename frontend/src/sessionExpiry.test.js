import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { SESSION_EXPIRED } from './api.js'

const here = dirname(fileURLToPath(import.meta.url))
const raw = (f) => readFileSync(join(here, f), 'utf8')
// Executable lines only: a comment describing the deleted bug is not the bug.
const code = (f) => raw(f).split('\n')
  .filter((l) => { const t = l.trim(); return !t.startsWith('//') && !t.startsWith('*') && !t.startsWith('/*') })
  .join('\n')

// Two failures made a broken session indistinguishable from a working one:
//   1. a 401 bounced the user to the sign-in screen with no explanation, and
//   2. a review decision that never reached the server still left the queue and bumped the
//      "approved" counter, because every write ended in `.catch(() => {})`.
// The second is an audit-integrity bug: an approval nobody recorded.

describe('an expired session explains itself', () => {
  it('the message says what happened and what to do', () => {
    expect(SESSION_EXPIRED).toMatch(/signed out/i)
    expect(SESSION_EXPIRED).toMatch(/sign in again/i)
    expect(SESSION_EXPIRED).not.toMatch(/401|token|Bearer/i)   // no implementation leakage
  })

  it('the 401 handler carries the reason on the event', () => {
    const api = code('api.js')
    expect(api).toMatch(/acp:session-expired[\s\S]{0,80}detail:\s*\{\s*reason/)
  })

  it('App forwards the reason to SignIn instead of dropping it', () => {
    const app = code('App.jsx')
    expect(app).toMatch(/setSignedOutReason\(e\?\.detail\?\.reason/)
    expect(app).toMatch(/<SignIn onSignedIn=\{signIn\} notice=\{signedOutReason\}/)
  })

  it('a successful sign-in clears the notice, so it cannot outlive the problem', () => {
    expect(code('App.jsx')).toMatch(/setSignedOutReason\(null\)/)
  })

  it('both sign-in cards render it — GIS and demo', () => {
    const signin = code('SignIn.jsx')
    expect((signin.match(/signin-notice/g) || []).length).toBe(2)
    expect(signin).toMatch(/role="status"/)
  })
})

describe('a review decision that fails is not shown as saved', () => {
  const rem = code('Remediate.jsx')

  it('no HITL write silently swallows its failure', () => {
    // The exact shape of the bug: updateHitlItem(...).catch(() => {})
    expect(rem).not.toMatch(/updateHitlItem\([^)]*\)\.catch\(\(\)\s*=>\s*\{\s*\}\)/)
    expect(rem).not.toMatch(/\bp\.catch\(\(\)\s*=>\s*\{\s*\}\)/)
  })

  it('every decision path reconciles an uncertain response before rolling back', () => {
    // A capacity 503 explicitly reports that the write outcome is unknown. Both paths must
    // inspect the durable row before undoing local state, while settleActFailure retains the
    // rollback + rethrow behavior for a decision proven not to have landed.
    expect(rem).toMatch(/updateHitlItem\(item\.id, 'skipped'[\s\S]{0,180}\.catch\([\s\S]{0,120}settleActFailure\(item, 'deferred', \{ status: 'skipped' \}, e\)/)
    expect(rem).toMatch(/\(e\) => settleActFailure\(item, kind, \{[\s\S]{0,300}status: apiStatus/)
    expect(rem).toMatch(/undoAct\(item, kind, err, settled\.outcome\)\s*\n\s*throw err/)
  })

  it('undoAct restores the card, undoes the count, and explains', () => {
    expect(rem).toMatch(/const undoAct = /)
    expect(rem).toMatch(/setQueue\(\(q\) => \(q\.some/)          // card returns to the queue
    expect(rem).toMatch(/Math\.max\(0, \(a\[kind\] \|\| 0\) - 1\)/) // counter rolls back, never negative
    expect(rem).toMatch(/setActError\(/)
    expect(rem).toMatch(/was NOT saved/)
  })

  it('a cosmetic refresh failure can never roll back a saved decision', () => {
    // .then().catch() would catch onRefresh's rejection too. Two-arg then keeps them apart.
    expect(rem).toMatch(/p\.then\(\s*\n/)
    expect(rem).not.toMatch(/p\.then\([^)]*\)\.catch\(\(e\) => undoAct/)
  })

  it('the rollback is announced to assistive tech, not just coloured red', () => {
    expect(rem).toMatch(/role="alert"/)
    expect(rem).toMatch(/\{actError\}/)
  })
})


describe('the bell / ReviewCenter path also surfaces a refused decision', () => {
  const bell = code('HitlBell.jsx')
  const card = code('EvidenceCard.jsx')

  it('HitlBell rolls its optimistic list back and rethrows', () => {
    // Every failure rolls back and rethrows; a viewed-version 409 is rethrown as its normalised
    // conflict (the server's sentence) — see viewedApprovalBell.test.jsx.
    // (Every failure is also stated by the bell itself — see viewedApprovalBell.test.jsx.)
    expect(bell).toMatch(/\.catch\(\(e\) => \{\s*if \(prev\) setItems\(prev\)[\s\S]{0,900}throw conflict \|\| e\s*\}\)/)
  })

  it('EvidenceCard catches that rethrow instead of leaving it unhandled', () => {
    // Without this, HitlBell's `throw e` became an unhandled rejection: the list quietly
    // rolled back and the reviewer was told nothing.
    expect(card).toMatch(/catch \(e\) \{[\s\S]{0,200}setActError\(/)
    expect(card).toMatch(/Nothing was recorded/)
  })

  it('and announces it, next to the buttons that failed', () => {
    expect(card).toMatch(/role="alert"/)
    expect(card).toMatch(/\{actError\}/)
  })

  it('decide() clears a stale error before each new attempt', () => {
    expect(card).toMatch(/setBusy\(true\)\s*\n\s*setActError\(null\)/)
  })
})
