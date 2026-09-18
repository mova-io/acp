// C5 — "open this specific review item". Anything may ask (the remaining-findings explainer, the
// Live card's queue drawer); Remediate is the one listener. It switches the workspace to review,
// and RemediationInbox selects, scrolls to and focuses the row.
//
// The request is also remembered briefly, because the asker can be on screen while Remediate is
// not mounted yet (the stage card is App-level; Remediate mounts only on its own tab). Without the
// memory, a click that also navigates would dispatch into nothing and the button would be dead.
export const OPEN_REVIEW_ITEM_EVENT = 'acp:open-review-item'
const PENDING_MS = 30000
let pending = null

export function requestOpenReviewItem({ itemId, scanId = null, tab = null } = {}) {
  if (itemId == null || itemId === '') return false
  const detail = { itemId: String(itemId), scanId: scanId || null, tab: tab || null }
  pending = { detail, at: Date.now() }
  window.dispatchEvent(new CustomEvent(OPEN_REVIEW_ITEM_EVENT, { detail }))
  return true
}

// Called by the listener once it has acted, and on mount to pick up a request made before it
// existed. Returns the detail only while fresh, and only once.
export function takePendingReviewItem(scanId = null) {
  const current = pending
  if (!current || Date.now() - current.at > PENDING_MS) { pending = null; return null }
  if (scanId && current.detail.scanId && current.detail.scanId !== scanId) return null
  pending = null
  return current.detail
}
