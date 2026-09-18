import { useEffect, useRef, useState } from 'react'
import { reconcileReviewTargets } from './api.js'
import { reconciliationSummary } from './reviewTargetReconciliationCopy.js'

// C8 — the explicit action that settles an ALREADY-completed run. Newly applied changes are
// reconciled by the backend as they are written; a run that finished before that existed is only
// corrected when someone asks, and this is where they ask. It reads the recorded check of the saved
// copy on the server: no AI call, no document write, no change to anyone's decision.
//
// The result is reported in the server's own terms, mapped to plain language. A failed request
// claims nothing — not "nothing changed", because a lost response cannot say that.
// `available` false hides the button but keeps the last result on screen: a successful re-check
// usually closes the very items that made the action appear, and its answer must not vanish with them.
export default function ReconcileReviewTargets({ scanId, onReconciled, available = true }) {
  const [state, setState] = useState(null)   // null | { busy } | { message } | { error }
  const scanRef = useRef(scanId)
  scanRef.current = scanId
  useEffect(() => { setState(null) }, [scanId])
  const run = async () => {
    const asked = scanId
    setState({ busy: true })
    try {
      const result = await reconcileReviewTargets(asked)
      if (scanRef.current !== asked) return
      setState({ message: reconciliationSummary(result) })
      onReconciled?.(result)
    } catch (error) {
      if (scanRef.current === asked) setState({ error: `The re-check did not complete${error?.message ? ` (${error.message})` : ''}. Its outcome is not known here; the items below are shown as last loaded.` })
    }
  }
  if (!available && !state?.message && !state?.error) return null
  return <section className="reconcile-review-targets" aria-label="Re-check remaining items">
    {available && <>
      <button type="button" className="ghost small" disabled={!scanId || state?.busy} onClick={run}>
        {state?.busy ? 'Re-checking…' : 'Re-check remaining items against the saved copy'}
      </button>
      <p className="muted" style={{ margin: '4px 0 0', fontSize: 12.5 }}>Uses the recorded check of the saved copy. No AI calls and no change to the document.</p>
    </>}
    {state?.message && <p role="status" className="reconcile-review-targets__result">{state.message}</p>}
    {state?.error && <p role="alert" className="error">{state.error}</p>}
  </section>
}
