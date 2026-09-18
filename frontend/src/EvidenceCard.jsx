import CropReviewContext from './CropReviewContext.jsx'
import { isPdfStructuralRow, pdfStructuralSummary } from './pdfStructuralProposal.js'
import { useState, useEffect, useMemo, useRef } from 'react'
import { aiProvenance, getCopilotGuidance, getFileGeometry, getFileRemediationDiffs, getScanAiCalls, getSourceLink, suggestFix, validateAlt } from './api.js'
import Thumbnail from './Thumbnail.jsx'
import BeforeAfterEvidence from './BeforeAfterEvidence.jsx'
import RiskChip from './RiskChip.jsx'
import { applyOutcomeCopy, authoringScaffold, buildEvidenceCard, DESCRIBED_NOT_REPLACED, describedImageType, evidenceOf, evidenceSignals, firstProposed, groupPages, guidanceSentence, houseStyleOf, imagesOfTextException, isReviewedCropEvidence, isValueFix, leadWithIsolatedImage, pageOf, primaryActionLabel, proposalsOf, reviewIntent, reviewTelemetry, thumbAlt, thumbSize, trustStates, validationChecklist, verificationLadder, whyHumanReview, whyRecommendation, whySafeToApprove } from './reviewCard.js'
import ProposalThumb, { isSafeThumb } from './ProposalThumb.jsx'
import ProposalEditors, { seedValues } from './ProposalEditors.jsx'
import CaptionEditor from './CaptionEditor.jsx'
import { speakAsScreenReader, srSupported } from './srPreview.js'
import { runAutoDraft, resetAutoDraftBreaker } from './autoDraft.js'
import { escalationPath, escalationFromDraft } from './escalationPath.js'
import { houseStyleFromDraft } from './houseStyle.js'
import { loadAiModels } from './aiModel.js'
import HowToConfirm from './HowToConfirm.jsx'
import TracePanel from './TracePanel.jsx'

// Evidence Card (PRD v2) — a PR-style review of ONE accessibility issue. The human APPROVES
// ACP's recommendation; ACP applies it. Assembles only shipped primitives (confidence basis,
// remediationTrack, remediation_diff, thumbnail) and records review telemetry (edited flag +
// review time) so the workspace can report reviewer time saved and calibrate confidence.
//
// onAct(id, status, note, approvedValue, telemetry) — the parent owns the write, so its
// optimistic update and the queue-drain event stay wired. traceUrl is optional.
// Trust-state pill styling (ADR 0019) — a verifiable state, never a confidence colour-by-score.
const _TRUST_BG = { ok: '#E1F5EE', warn: 'var(--warn-bg)', todo: 'rgba(0,0,0,.05)' }
const _TRUST_FG = { ok: '#0F6E56', warn: 'var(--warn-fg)', todo: '#6b6b6b' }
const trustPill = (tone) => ({ padding: '2px 9px', borderRadius: 6, fontSize: 12, whiteSpace: 'nowrap',
  background: _TRUST_BG[tone] || _TRUST_BG.todo, color: _TRUST_FG[tone] || _TRUST_FG.todo })
const trustIcon = (tone) => (tone === 'ok' ? '✓' : tone === 'warn' ? '◐' : '○')

// ADR 0021 §E — "house style applied", the chip that makes review memory's influence on a draft
// visible instead of a hidden hand. Collapsed it names how many of the org's rules shaped this
// draft's prompt; expanded it shows each rule's exact guidance, its scope, and — for a rule the
// org accepted from the derivation job — the real count from `hitl_events` that justified it.
//
// It is a `<details>` for the same reason the OCR aid below it is: the reviewer's job is the
// value in the box, and a wall of house rules on every card would bury it. One line says
// influence happened; the disclosure answers "which rules, and why".
//
// Renders nothing when no memory applied. The absence IS the message — the backend attaches
// these rules only when it actually injected them, so no chip means the prompt was byte-for-byte
// the pre-memory one, not that nobody looked.
function HouseStyleChip({ houseStyle }) {
  if (!houseStyle) return null
  return (
    <details className="evcard-house-style">
      <summary className="muted" style={{ fontSize: 12, cursor: 'pointer' }}>
        📐 {houseStyle.label} — what your org asked the model for
      </summary>
      <ul style={{ listStyle: 'none', margin: '4px 0 0', padding: 0 }}>
        {houseStyle.rules.map((r, i) => (
          <li key={r.id || i} className="evcard-house-style-rule" data-kind={r.kind}
              style={{ margin: '0 0 6px' }}>
            <span className="muted" style={{ fontSize: 11 }}>
              {r.kindLabel}
              {' · '}
              {r.ruleId ? `WCAG ${r.ruleId}` : 'all criteria'}
              {' · '}
              {r.format ? r.format.toUpperCase() : 'all formats'}
            </span>
            <div style={{ fontSize: 12, wordBreak: 'break-word' }}>{r.guidance}</div>
            {/* The evidence is quoted, never characterised — the count the row carries and no
                adjective, percentage or confidence this card invented (ADR 0016). */}
            {r.evidence && (
              <div className="muted evcard-house-style-evidence" style={{ fontSize: 11 }}>
                {r.evidence}
              </div>
            )}
            {r.evidenceMissing && (
              <div className="muted evcard-house-style-evidence-missing" style={{ fontSize: 11 }}>
                This rule recorded evidence that could not be read — judge the guidance on its
                own terms rather than as measured.
              </div>
            )}
          </li>
        ))}
      </ul>
    </details>
  )
}

export default function EvidenceCard({ item, onAct, onResolved, traceUrl = null, actualZone = null }) {
  const [diffs, setDiffs] = useState([])
  // In-app trace: when we know the scan + file we render the trace INSIDE AccessOps
  // (TracePanel) instead of linking out to Langfuse, whose page needs a login. Falls back
  // to the outbound traceUrl only when those aren't available.
  const [tracePanel, setTracePanel] = useState(false)
  // One editor per proposal: the row carries one proposal per image, and a single text box
  // could never describe ten different pictures. Seeded from each image's own draft, so
  // approving without touching anything means "the drafts I was shown are correct" — the
  // server applies exactly these, per locator.
  const proposalList = proposalsOf(item)
  // Prefill from the server-side AI proposal when there is one — that is what turns a 30s
  // "write the alt text" into a 5s "confirm this". Falls back to a previously-approved value.
  const [value, setValue] = useState(firstProposed(item) ?? item?.approved_value ?? '')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  // A decision the server refused. Rendered as an alert — a silent failure here is a
  // reviewer believing they signed something off that was never recorded.
  const [actError, setActError] = useState(null)
  const [showExplain, setShowExplain] = useState(false)   // "✨ Explain this finding" toggle
  const [askReject, setAskReject] = useState(false)        // reject → "why?" reason chips (feedback intelligence)
  const [showAudit, setShowAudit] = useState(false)       // "🔎 AI audit trail" toggle (#129)
  const [aiCalls, setAiCalls] = useState(null)            // null = unloaded, [] = loaded-empty
  const [drafting, setDrafting] = useState(false)
  const [draftMsg, setDraftMsg] = useState(null)   // { kind: 'ai' | 'template' | 'error', text }
  // The escalation path read straight off a /ai/suggest draft response (#378) — the preferred source
  // over re-deriving it from the ai_calls ledger. Set by the draft functions when a response carries
  // the numbered `escalation` field (i.e. a cloud escalation actually happened); null otherwise, and
  // the render then falls back to the ledger-derived path for drafts that predate this field.
  const [draftEscalation, setDraftEscalation] = useState(null)
  // /ai/status's non-admin cloud-provider signal (#378: cloud_enabled / cloud_provider / cloud_zone),
  // fetched lazily once a draft has actually failed so the empty-state message tells the truth about
  // whether a governed cloud fallback exists — instead of guessing from the AI-provenance zone.
  // null = not yet loaded (module-cached via loadAiModels, so one fetch serves the whole inbox).
  const [cloudStatus, setCloudStatus] = useState(null)
  // Deterministic verification aid: the text OCR actually read from the drafted image, so the
  // reviewer checks the description against what the image SAYS instead of squinting at a thumb.
  const [ocrAid, setOcrAid] = useState(null)
  // ADR 0021 §E — the org house-style rules that shaped this draft's PROMPT, read off the draft
  // response that used them (houseStyleFromDraft). Null unless review memory actually applied,
  // which is the common case: ACP_REVIEW_MEMORY defaults off, and with it off the backend sends
  // nothing because the prompt was the pre-memory one. No chip therefore means no influence —
  // never "we didn't check". Deliberately NOT cleared by a draft that carries no memory: see
  // setHouseStyle's call sites in draftAll, where one image's rules must not be blanked by the
  // next image in the same batch.
  const [draftHouseStyle, setHouseStyle] = useState(null)
  const [copilotResult, setCopilotResult] = useState(null)
  const [copilotLoading, setCopilotLoading] = useState(false)
  // Which deferred image the reviewer is looking at. The vision model describes ONE image, so
  // a row carrying nineteen must say which — defaulting to the first silently captions the
  // wrong picture and the reviewer approves alt text for an image they never saw.
  const evidence = evidenceOf(item)
  // The per-image editors are driven by proposals when the AI drafted any. When it drafted none
  // — a DEFERRED 1.1.1 row that carries only `evidence` thumbnails — the same editors are driven
  // by the evidence instead, so a deck's nineteen undescribed images each get a box rather than a
  // picker that could only ever record one value. Evidence has no draft, hence proposed_value ''.
  const usingEvidence = proposalList.length === 0 && evidence.length > 0
  const instances = proposalList.length
    ? proposalList
    : evidence.map((e) => ({ locator: e.locator, thumb: e.thumb, proposed_value: '',
                             approved_value: e.approved_value }))
  const [values, setValues] = useState(() => seedValues(instances))
  const setValueAt = (i, v) => setValues((prev) => prev.map((x, j) => (j === i ? v : x)))
  // Unlike `instances`, this also captures calls made on demand after the card mounted. Keep the
  // identifier beside the value it produced; the two positional arrays travel together.
  const [instanceCallIds, setInstanceCallIds] = useState(
    () => instances.map((instance) => instance?.model_call_id || null))
  const setCallIdAt = (i, callId) => setInstanceCallIds(
    (prev) => prev.map((value, j) => (j === i ? callId || null : value)))
  // Approve-similar (#132): copy row i's description to every instance that is the SAME image
  // (byte-identical thumbnail) — a logo reused across slides gets described once.
  const applyToSimilar = (i) => {
    const t = instances[i]?.thumb
    if (!t) return
    setValues((prev) => prev.map((x, j) => (instances[j]?.thumb === t ? prev[i] : x)))
  }
  // Cross-check the CURRENT value of image i against a fresh independent description (#123). Returns
  // the verdict so ProposalEditors can show it inline. Best-effort → null on any failure.
  const crossCheck = (i) => {
    if (!item?.scan_id || !item?.file || !item?.rule_id) return Promise.resolve(null)
    return validateAlt(item.scan_id, item.file, item.rule_id, instances[i]?.locator, values[i])
      .then((v) => (v && v.verdict ? v : null))
      .catch(() => null)
  }
  const [draftingIdx, setDraftingIdx] = useState(null)
  // Which flagged image the HERO is showing (#122 multi-image pager). A 1.1.1 row can carry many
  // undescribed images across several slides; the pager steps the large preview + its bounding box
  // through each one, so a reviewer verifies every finding in place, not just the first.
  const [heroIdx, setHeroIdx] = useState(0)
  const [sourceLink, setSourceLink] = useState(null)   // {url, label} or null
  // Page heatmap (#121 / vision §17): which pages/slides the flagged objects live on, from
  // MEASURED geometry only (the same bbox lookup the hero overlay uses — never guessed). A
  // multi-image finding renders a clickable page strip; a page the geometry can't attribute
  // simply doesn't appear. instances is rebuilt every render, so the fetch keys on the stable
  // locator signature instead.
  const [pageMap, setPageMap] = useState({})       // instance idx -> page number
  const locatorSig = instances.map((x) => x?.locator || '').join('|')
  useEffect(() => {
    setPageMap({})
    if (!item?.scan_id || !item?.file || instances.length < 2) return
    let live = true
    instances.forEach((inst, i) => {
      if (!inst?.locator) return
      getFileGeometry(item.scan_id, item.file, inst.locator)
        .then((b) => { if (live && b && b.page) setPageMap((m) => ({ ...m, [i]: b.page })) })
    })
    return () => { live = false }
  }, [item?.scan_id, item?.file, locatorSig]) // eslint-disable-line react-hooks/exhaustive-deps
  const pageStrip = useMemo(() => groupPages(pageMap), [pageMap])
  // The page the HERO is on, following the pager: the measured page of the current image, else
  // the item's recorded page when there is only one image. With several images and no measured
  // page for this one, it is unknown — null, never the first image's page and never 1.
  const heroPage = pageMap[heroIdx] ?? (instances.length <= 1 ? pageOf(item) : null)
  // "Open in SharePoint" for the page the reviewer is LOOKING at. It used to be fetched once with
  // `item.page || 1`, so paging to image 3 on slide 7 still opened slide 1. Unknown page → the
  // link opens the document without a slide anchor (api.getSourceLink(…, null)).
  useEffect(() => {
    setSourceLink(null)
    if (!item?.scan_id || !item?.file) return
    let live = true
    getSourceLink(item.scan_id, item.file, heroPage)
      .then((d) => { if (live && d?.url) setSourceLink(d) })
    return () => { live = false }
  }, [item?.scan_id, item?.file, heroPage])
  const shownAt = useRef(Date.now())               // reviewer-time metric starts when the card mounts
  // The value the AI actually proposed — reviewTelemetry diffs the human's final value against
  // this to derive the `edited` calibration signal, so it must be the proposal, not the draft.
  const aiDraft = useRef(firstProposed(item) ?? item?.approved_value ?? null)
  // Server-precomputed vision proposals already carry their exact call ID. A multi-instance
  // card can contain several independent calls, so only bind the card-level decision when the
  // decision has exactly one generated value; otherwise attribution would be false precision.
  const modelCallId = useRef(instances.length === 1 ? instances[0]?.model_call_id || null : null)
  // Preserve each generated value's producer. Position i follows the same instances ordering as
  // approvedValues, so a collapsed multi-image card never attributes every decision to whichever
  // vision call happened to be first (or drops all attribution because there was more than one).
  const modelCallIds = instanceCallIds
  // Auto-draft plumbing: the card element (for the viewport observer), a once-guard so the auto
  // draft fires at most once, and whether the card has been scrolled into view yet.
  const rootRef = useRef(null)
  const autoDraftedRef = useRef(false)
  const [seen, setSeen] = useState(false)

  useEffect(() => {
    let live = true
    if (item?.scan_id && item?.file) {
      getFileRemediationDiffs(item.scan_id, item.file).then((r) => { if (live) setDiffs(r || []) }).catch(() => {})
    }
    return () => { live = false }
  }, [item?.scan_id, item?.file])

  // Draft on demand. Most items reach the inbox with a server-side proposal already attached,
  // but an image whose alt text the vision model could not produce arrives with nothing — and
  // the reviewer had no way to ask for one. This is that ask.
  const draftWithAi = async () => {
    if (!item?.scan_id || !item?.file || !item?.rule_id) return
    // Re-arm the missing-model breaker. Reaching here is either a reviewer's own ↻ Try again —
    // usually the click right after `ollama pull`, and the only thing that can prove the model is
    // back — or the automatic path, where the breaker was already closed and this is a no-op.
    resetAutoDraftBreaker()
    setDrafting(true); setDraftMsg(null)
    try {
      // This box is only shown for a single value-fix finding with no proposals and no evidence
      // (e.g. one link-text finding), so there is no per-image locator to pass — a deferred image
      // row uses draftInstance instead. evidence[0] is undefined here, which is correct.
      const r = await suggestFix(item.scan_id, item.file, item.rule_id, evidence[0]?.locator)
      const s = (r?.suggestion || '').trim()
      if (!s) { setDraftMsg({ kind: 'error', text: 'The model returned nothing — write the value yourself.' }); return }
      setValue(s)
      modelCallId.current = r?.ai_call_id || null
      setOcrAid(r.ocr_text || null)
      // Read the escalation path off the response itself (#378) — null when this draft stayed local.
      setDraftEscalation(escalationFromDraft(r))
      // Likewise the house style that shaped it (ADR 0021 §E). Set unconditionally: this is the
      // single-box path, so a re-draft REPLACES the card's state — if memory no longer applies
      // (an admin retired the rule, or the flag went off), the chip must go with it rather than
      // keep asserting an influence this draft did not have.
      setHouseStyle(houseStyleFromDraft(r))
      if (r.is_template) {
        // A fill-in-the-blank template, NOT a description of this image. It is deliberately
        // NOT recorded as aiDraft: approving it verbatim must count as human-authored, and
        // reviewTelemetry must not log a template as an accepted AI value. Say WHY it is a
        // template — "no vision model described this image" read as "llava looked and failed"
        // even when no vision model was ever consulted.
        setDraftMsg({ kind: 'template', text: r.reason || 'Template only — no vision model was available to look at this image. Rewrite it before approving.' })
      } else {
        aiDraft.current = s   // a genuine AI value: `edited` now means the human changed it
        setDraftMsg(r.deterministic
          // Machine-detected, not model-authored (e.g. 3.1.2 language ID via langdetect):
          // say so — "AI draft · llama3.2" would misattribute a deterministic detection.
          ? { kind: 'ai', text: `Detected deterministically${r.model ? ` · ${r.model}` : ''} — approve, or override if the detection is wrong.` }
          : { kind: 'ai', text: `AI draft${r.model ? ` · ${r.model}` : ''} — edit if it misses the meaning.` })
      }
    } catch (e) {
      setDraftMsg({ kind: 'error', text: e?.message || 'AI draft unavailable — write the value yourself.' })
      // The message is on the card either way; rethrowing a MISSING-MODEL failure is how the
      // shared gate learns of it, so the other cards in the inbox stop asking. Every other
      // failure stays swallowed — it is this card's problem and the next card may well succeed.
      if (e?.aiModelNotPulled) throw e
    } finally {
      setDrafting(false)
    }
  }

  // Draft one deferred image, by its own row. Unlike draftWithAi (single box), this writes the
  // result into instance i's value — the model saw image i, so its description belongs to image i.
  // "Draft all with AI" — fill every still-empty image editor with an AI description in one
  // click, so a 14-image card becomes review-and-approve instead of author-14-by-hand. Runs
  // sequentially (one vision call at a time) so the local model isn't overwhelmed; skips
  // images the reviewer already filled. Most cards arrive PRE-drafted from the scan; this is
  // the on-demand path for when vision was unavailable then and is reachable now.
  const [draftingAll, setDraftingAll] = useState(false)
  const draftAll = async () => {
    if (draftingAll || draftingIdx != null) return
    resetAutoDraftBreaker()   // deliberate click — see draftWithAi
    setDraftingAll(true); setDraftMsg(null)
    let n = 0
    let missing = null
    for (let i = 0; i < instances.length; i++) {
      if ((values[i] || '').trim()) continue        // don't overwrite a value already there
      try {
        const r = await suggestFix(item.scan_id, item.file, item.rule_id, instances[i]?.locator)
        const s = (r?.suggestion || '').trim()
        if (s && !r.is_template) {
          setValueAt(i, s)
          setCallIdAt(i, r?.ai_call_id)
          n += 1
        }
        const esc = escalationFromDraft(r)   // surface the path from whichever image escalated (#378)
        if (esc) setDraftEscalation(esc)
        // House style is card-level (it keys on org + rule + format, all identical across this
        // card's images), so any image's response answers for the batch. Only set when present,
        // so an image that failed or returned a template cannot blank a chip a sibling earned.
        const hs = houseStyleFromDraft(r)
        if (hs) setHouseStyle(hs)
      } catch (e) {
        // One image failing must not stop the batch — but a model that is not pulled fails every
        // image identically, so asking 18 more times tells the reviewer nothing new and delays the
        // answer. Stop, and hand back the server's own line (it names the model and the pull).
        if (e?.aiModelNotPulled) { missing = e; break }
      }
    }
    setDraftingAll(false)
    if (missing) {
      setDraftMsg({ kind: 'error', text: missing.message })
      throw missing        // tell the shared gate, so the rest of the inbox stops asking
    }
    setDraftMsg({ kind: n ? 'ai' : 'template', text: n
      ? `Drafted ${n} image${n === 1 ? '' : 's'} with AI — review and edit before approving.`
      : 'No AI drafts available — describe the images yourself.' })
  }

  const draftInstance = async (i, style = '') => {
    if (draftingIdx != null || !item?.scan_id || !item?.file || !item?.rule_id) return
    resetAutoDraftBreaker()   // deliberate click — see draftWithAi
    setDraftingIdx(i); setDraftMsg(null)
    try {
      const r = await suggestFix(item.scan_id, item.file, item.rule_id, instances[i]?.locator, style || null)
      const s = (r?.suggestion || '').trim()
      if (!s) { setDraftMsg({ kind: 'error', text: `Image ${i + 1}: the model returned nothing — write it yourself.` }); return }
      setValueAt(i, s)
      setCallIdAt(i, r?.is_template ? null : r?.ai_call_id)
      if (instances.length === 1) modelCallId.current = r?.ai_call_id || null
      setOcrAid(r.ocr_text || null)
      // A per-image escalation reads off this image's own response (#378); keep any earlier one shown
      // if this image stayed local, so the card doesn't blank a path a sibling image established.
      const esc = escalationFromDraft(r)
      if (esc) setDraftEscalation(esc)
      const hs = houseStyleFromDraft(r)   // card-level; don't blank a sibling image's chip
      if (hs) setHouseStyle(hs)
      const styleWord = style === 'shorter' ? ' (shorter)' : style === 'detailed' ? ' (more detail)'
        : style === 'regenerate' ? ' (regenerated)' : style === 'numbers' ? ' (numbers stated)'
        : style === 'no_colour' ? ' (colour-free)' : style === 'professional' ? ' (professional tone)'
        : style === 'plain' ? ' (plain language)' : ''
      setDraftMsg(r.is_template
        ? { kind: 'template', text: r.reason || 'Template only — no vision model was available. Rewrite it before approving.' }
        : r.deterministic
          ? { kind: 'ai', text: `Detected deterministically${r.model ? ` · ${r.model}` : ''} — approve, or override if the detection is wrong.` }
          : { kind: 'ai', text: `Image ${i + 1} drafted${styleWord}${r.model ? ` · ${r.model}` : ''} — edit if it misses the meaning.` })
    } catch (e) {
      setDraftMsg({ kind: 'error', text: e?.message || 'AI draft unavailable — write the value yourself.' })
    } finally {
      setDraftingIdx(null)
    }
  }

  const askCopilot = async () => {
    setCopilotLoading(true)
    setCopilotResult(null)
    try {
      const r = await getCopilotGuidance(item?.scan_id, item?.file, item?.rule_id, heroLocator)
      setCopilotResult(r)
    } catch (e) {
      setCopilotResult({ error: e?.message || 'Could not reach the cloud model — try again.' })
    } finally {
      setCopilotLoading(false)
    }
  }

  const card = buildEvidenceCard(item, diffs)
  // Everything about the "current" image follows the pager (#122) so the hero box, the object thumb,
  // and the kind chip all describe the SAME image — paging to image 3 must not leave image 1's thumb
  // beside the text. All fall back to the card's first instance for a single-image finding.
  const heroInst = instances[heroIdx] || null
  const heroLocator = heroInst?.locator || card.locator
  const heroThumb = heroInst?.thumb ?? card.thumb
  const heroCrop = (card.sc === '1.4.5' || card.sc === '1.4.9') && isReviewedCropEvidence(heroInst?.visible_crop) ? heroInst.visible_crop : null
  // The kind of image, read from the model's own description (#130) — a routing hint for what a
  // good alt text looks like here (a chart needs a trend, an icon two words). null → no chip.
  const imgKind = describedImageType(heroInst ? { proposals: [heroInst] } : item)
  // Lead with the ISOLATED embedded image as the hero, not a whole-page render, when the format's
  // page render would obscure the object under review (xlsx = the entire sheet). Only when we
  // actually hold the image bytes; otherwise fall through to the page-render hero as before.
  const heroIsImage = leadWithIsolatedImage(card) && isSafeThumb(heroThumb)
  // Reviewer-trust primitives (all derived from real fields — never a fabricated score):
  //   ladder  — how far the pipeline got before handing off (detected → validated → your approval)
  //   signals — the concrete evidence behind the finding (detection basis, reasoning, subjective flag)
  const ladder = verificationLadder(card)
  const signals = evidenceSignals(card)
  const whyReview = whyHumanReview(card)
  // Review Intent (Review queue) — the one plain-language sentence at the top: why you're here
  // and what to do, before any audit detail. The primary button's label follows the same workflow.
  const intent = reviewIntent(item, card.sc)
  const primaryAction = primaryActionLabel(item)
  // AI provenance (ADR 0019 Phase 0): which model produced this + where the bytes were processed.
  const aiProv = aiProvenance()
  // W6 — the zone shown is the ACTUAL one this file's AI ran in (from the per-call ledger, passed
  // down by Remediate) when we know it, falling back to the configured provider zone only when we
  // don't. This ordering is the whole point: a GPU→CPU fallback makes the config say "cloud" while
  // the call really ran "local", and the reviewer must see what actually happened, not the intent.
  const provZone = actualZone || aiProv?.zone || null
  const aiValueShown = !!(card.proposal || card.recommendation)

  // The AI audit trail (#129) — the real per-call provenance ledger, lazily fetched on first open
  // and narrowed to this file. Every row is a logged model call (surface/model/zone/latency), so the
  // reviewer (and an auditor) can see exactly what ran, where, and how long — no fabrication.
  const toggleAudit = () => {
    setShowAudit((s) => !s)
    if (aiCalls === null && item?.scan_id) {
      getScanAiCalls(item.scan_id)
        .then((r) => setAiCalls(Array.isArray(r) ? r : []))
        .catch(() => setAiCalls([]))
    }
  }
  const auditRows = (aiCalls || []).filter((c) => !c.file || c.file === item?.file)
  // Auto-escalation numbered path (P1 / #378) — a local vision attempt that couldn't ground, followed
  // by an escalation to a governed cloud provider, rendered as the transparent path on the surface so
  // the failed local attempt doesn't read as a dead end. PREFERRED source is the real field the draft
  // response now carries (escalationFromDraft); the ledger-derived path is the fallback for drafts that
  // predate #378 (e.g. a card pre-drafted at scan time, where no live /ai/suggest call was made). Null
  // in the common all-local case, from either source.
  const escalation = draftEscalation || escalationPath(auditRows)
  // ADR 0021 §E — same two-source shape as `escalation` directly above, and for the same reason.
  // A live /ai/suggest draft carries its own house style (draftHouseStyle); a card PRE-DRAFTED at
  // scan time never made that call, and reads it off the proposals the scan stamped instead. The
  // live draft wins when both exist: it is the value on screen, so its rules are the ones that
  // shaped what the reviewer is about to approve.
  const houseStyle = draftHouseStyle
    || houseStyleFromDraft({ house_style: houseStyleOf(item) })
  // The ledger is otherwise fetched lazily (only when the reviewer opens the audit trail). To show
  // the escalation path WITHOUT a click — the whole point of item 2 — pull it once for a file whose
  // AI actually ran in the cloud (provZone 'cloud' is the escalation signal). This stays bounded to
  // escalated/cloud files rather than fetching the whole-scan ledger for every card in the inbox
  // (Remediate deliberately fetches it once for the scan, not per card); an all-local card makes no
  // request here and simply shows nothing.
  useEffect(() => {
    if (aiCalls !== null || !aiValueShown || provZone !== 'cloud' || !item?.scan_id) return
    let live = true
    getScanAiCalls(item.scan_id)
      .then((r) => { if (live) setAiCalls(Array.isArray(r) ? r : []) })
      .catch(() => { if (live) setAiCalls([]) })
    return () => { live = false }
  }, [aiValueShown, provZone, item?.scan_id]) // eslint-disable-line react-hooks/exhaustive-deps

  // Empty-state honesty (P1 / #378) — a draft that failed with NO escalation is the manual-authoring
  // state. WHY it is manual now comes from /ai/status's non-admin cloud signal (cloud_enabled), not
  // from the AI-provenance zone the card previously used as a proxy: the proxy could not distinguish
  // "no cloud provider configured" from "cloud configured but this file happened to run local", and it
  // never had a first-class "is a governed cloud fallback enabled?" answer. #378 exposes exactly that,
  // key-free, so a reviewer sees the honest reason. Fetched lazily (module-cached via loadAiModels →
  // one /ai/status call for the whole inbox) and only once a draft has actually failed.
  const draftFailed = draftMsg?.kind === 'error' || draftMsg?.kind === 'template'
  useEffect(() => {
    // Only fetch when there is actually a manual empty-state to explain: a failed draft, no escalation
    // shown instead, and the cloud status not already loaded.
    if (!draftFailed || escalation || cloudStatus !== null) return
    let live = true
    ;(async () => {
      try {
        const s = await loadAiModels()
        if (live) setCloudStatus(s || {})
      } catch { if (live) setCloudStatus({}) }
    })()
    return () => { live = false }
  }, [draftFailed, escalation, cloudStatus])
  // Fetch cloud status eagerly for 1.1.1 evidence-only cards so the "Help me" copilot
  // button can appear without needing a draft failure first. loadAiModels is module-cached
  // (one /ai/status call for the whole inbox), so this adds no extra network cost.
  useEffect(() => {
    if (cloudStatus !== null || card.sc !== '1.1.1' || !usingEvidence) return
    let live = true
    ;(async () => {
      try {
        const s = await loadAiModels()
        if (live) setCloudStatus(s || {})
      } catch { if (live) setCloudStatus({}) }
    })()
    return () => { live = false }
  }, [card.sc, usingEvidence]) // eslint-disable-line react-hooks/exhaustive-deps
  const cloudEnabled = !!cloudStatus?.cloud_enabled
  const cloudProvider = (typeof cloudStatus?.cloud_provider === 'string' && cloudStatus.cloud_provider.trim())
    ? cloudStatus.cloud_provider.trim() : null
  // Show the hint only once we KNOW the cloud state (status loaded), so the message is never wrong in
  // the gap before the fetch resolves. No escalation + a failed draft is the trigger.
  const showManualHint = draftFailed && !escalation && cloudStatus !== null
  // Reuse the app's existing custom-event navigation (acp:session-expired / acp:scan-unavailable are
  // the same pattern) rather than hardcoding a route — App listens for acp:open-settings and opens
  // the Settings modal (which contains the AI Providers panel), gated on the settings permission.
  const openProviderSettings = () =>
    window.dispatchEvent(new CustomEvent('acp:open-settings', { detail: { section: 'ai-providers' } }))
  const manualCloudHint = showManualHint ? (
    <div className="evcard-manual-empty" role="note"
         style={{ display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap', fontSize: 12.5,
                  background: 'var(--surface-1, #f6f5f2)', border: '1px solid var(--line)', borderRadius: 8,
                  padding: '8px 11px', margin: '6px 0 0' }}>
      <span aria-hidden="true">✍️</span>
      <span style={{ flex: 1, minWidth: 0 }}>
        {cloudEnabled ? (
          <>
            {/* A governed cloud provider IS enabled but this image still needs a human — the local
                model couldn't describe it and the cloud fallback couldn't ground one either. Name the
                provider honestly rather than implying none exists. */}
            <b>Why you’re writing this by hand:</b> the local vision model couldn’t describe this image,
            and {cloudProvider ? <>your governed cloud provider (<b>{cloudProvider}</b>)</> : 'your governed cloud provider'} couldn’t
            ground a description for it either — so it needs a human. Author the value below.{' '}
            <button type="button" className="evcard-linkbtn" onClick={openProviderSettings}>
              Settings → AI Providers
            </button>
          </>
        ) : (
          <>
            <b>Why you’re writing this by hand:</b> the local vision model couldn’t describe this image,
            and no governed cloud provider is enabled to fall back to. Author the value below — or enable a
            cloud provider so ACP can draft descriptions the local model can’t ground.{' '}
            <button type="button" className="evcard-linkbtn" onClick={openProviderSettings}>
              Settings → AI Providers
            </button>
          </>
        )}
      </span>
    </div>
  ) : null
  // Verifiable trust states (ADR 0019 §3a) — grounding + validation, the evidence-based replacement
  // for a confidence label. No number, no opaque level; the review-requirement axis is whyReview.
  const trust = trustStates(card)
  // "Why this is safe to approve" (Review queue P0) — the affirmative, plain-language summary of
  // what has ALREADY been checked, so the reviewer builds confidence up front instead of digging
  // through the audit trail. Null when the honest answer is "still needs judgement" — then only the
  // "Why human review?" callout stands.
  const whySafe = whySafeToApprove(card, { trust })
  // Deterministic, keyless plain-English explanation (the "✨ Explain this finding" answer).
  // "Why this recommendation?" (#9) — the structured reasoning chain. The real OCR snippet, when
  // the pipeline read one, is embedded in the rationale/evidence as OCR: "…"; pull it out so the
  // panel can show what the model actually read, exactly like a reviewer would want.
  const ocrText = (() => {
    const src = `${card.rationale || ''} ${(evidence[0] && evidence[0].evidence) || ''}`
    const m = /OCR:\s*[“"']?([^”"'’)]+)/i.exec(src)
    return m ? m[1].trim() : null
  })()
  const why = whyRecommendation(card, { trust, kind: imgKind, ocrText })
  // Deterministic-validation receipt (vision #12) — the machine-verified proof for an APPLIED fix,
  // shown separately from the AI generation. Null until something is applied + re-scan-cleared.
  const valChecklist = validationChecklist(card)
  // Cluster the evidence by group (Detection / Reasoning / Document state) in a stable order — the
  // way a reviewer scans it — rather than one flat list.
  const signalGroups = signals.reduce((m, s) => { (m[s.group] = m[s.group] || []).push(s); return m }, {})
  const GROUP_ORDER = ['Detection', 'Document state', 'Reasoning']
  // An editor appears for every value-fix criterion, draft or not — a reviewer must be able to
  // author alt text the AI could not draft. Keying off `aiDraft != null` (as this once did)
  // silently hid the box exactly when the human was most needed.
  // An item carrying a proposal always takes a value, even if its SC isn't a classic VALUE_FIX
  // (e.g. a 1.3.3 sensory rewrite) — otherwise the reviewer sees a proposal they cannot accept.
  // An EXPLAIN-ONLY row (a PDF structure/heading map, a page reading order) is confirmed, not
  // authored: its value is the re-authoring instruction and the compliance evidence, and nothing
  // is ever written into the file. An editable box would promise a write-back that no applier
  // performs — the same lie the server-side flag stops the certify gate believing (api/store.py
  // _row_approved_values). So the map renders read-only and the card asks for a confirmation.
  const explainOnly = proposalList.length > 0 && proposalList.every((p) => p.explain_only)
  // A COMPANION row is the case explain-only was wrongly used for, and it is nearly its opposite
  // here. Its value is a FILE delivered beside the document — a caption track for a video, a
  // transcript for a recording — so nothing is written into the source, exactly as for a
  // structure map. But the reviewer AUTHORS it: the draft is machine transcription, the card
  // itself says to check the names and numbers, and the corrected text is the artefact that
  // ships. Rendered under `explainOnly` (which is how captions first shipped, in #1177) the box
  // is read-only and `approvedValues` is suppressed, so a reviewer is told to fix a transcript
  // they cannot edit and whose edits would be discarded on approval.
  const companionRow = proposalList.length > 0 && proposalList.every((p) => p.companion_file)
  // THE MEDIA THE CAPTIONS BELONG TO. `/content` serves the scanned file's original bytes and now
  // names a playable MIME type for media, so a caption reviewer can hear what they are correcting
  // — the one thing checking a machine transcript actually requires.
  //
  // The proposal's `locator` is the media filename (propose_captions sets it to the file's own
  // name), which is also how every other route addresses a scanned file. Falling back to
  // `item.file` covers a row whose locator was lost; a missing source is handled INSIDE the
  // editor, which says why rather than rendering a broken player.
  const companionMediaName = companionRow
    ? (proposalList[0]?.locator || item?.file || '') : ''
  // Audio-only files get an <audio> element: a <video> tag on an .mp3 renders a black rectangle
  // where a reviewer expects a transport, which reads as a file that failed to load.
  const companionMediaKind = /\.(mp3|m4a|wav|aac|flac|ogg|oga|opus|wma)$/i.test(companionMediaName)
    ? 'audio' : 'video'
  // A DECORATIVE row is the same shape of mistake in the other direction. Its draft reads "Mark
  // as decorative — no alt text needed": an instruction to the reviewer, not a description of the
  // image. Rendered as an editable alt box — which is what happens today — the card invites
  // someone to write a description for an image they are about to declare needs none, and then
  // discards whatever they typed. #43 made that safe for the DOCUMENT (the value is routed to the
  // marker writer and ignored), so nothing corrupt is written any more; what is left is a field
  // that lies about what approving it does. The decision is a yes/no about the picture, so show
  // the picture and ask for the yes.
  const decorativeRow = proposalList.length > 0
    && proposalList.every((p) => p.kind === 'decorative')
  // `companionRow` is deliberately NOT excluded here: editing is the whole point of one.
  const structuralRow = isPdfStructuralRow(item)
  const structuralReady = !structuralRow || proposalList.every(pdfStructuralSummary)
  const editable = !explainOnly && !decorativeRow && !structuralRow
    && card.track.track !== 'auto' && (isValueFix(card.sc) || !!card.proposal || companionRow)
  // The primary button reads by workflow (primaryAction, above) — "Approve AI fix" / "Confirm
  // fix" / "Approve description". The honest "writes into the document vs records sign-off"
  // distinction stays in the "What you need to do" prose below, not on the button.
  // Any row carrying proposals gets the per-instance editor. It is the only surface that shows
  // WHAT is changing — the image, or the passage of text — beside the value being written, and
  // a reviewer cannot honestly approve a description of something they were never shown.
  // Per-instance editors render for a row with proposals (drafted images) OR a deferred row with
  // evidence (undrafted images) — either way `instances` has the entries and `values` is
  // positionally aligned with them.
  const multi = instances.length > 0
  // Only a row with many findings and NO per-image instances at all still can't be expressed in
  // one box — keep warning rather than implying it can. (Now rare: deferred rows carry evidence.)
  const manyInstances = !multi && card.findingCount > 1 && isValueFix(card.sc)

  // Auto-draft the preview when the card scrolls into view (Principle: the reviewer confirms a
  // suggestion, they never click "Draft with AI" and wait). The inbox lists every finding at once,
  // so we observe visibility and draft only what the reviewer actually reaches — off-screen cards
  // stay dormant. jsdom/SSR has no IntersectionObserver: treat as immediately seen (tests + no-JS).
  useEffect(() => {
    const el = rootRef.current
    if (!el || seen) return
    if (typeof IntersectionObserver !== 'function') { setSeen(true); return }
    const obs = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setSeen(true); obs.disconnect() }
    }, { rootMargin: '150px' })
    obs.observe(el)
    return () => obs.disconnect()
  }, [seen])

  // Auto-generate the preview (Review queue P0 — replaces the "Draft with AI" button): a value-fix
  // that reached the inbox with no draft is the fallback case where vision was unavailable at scan
  // time. Draft it automatically, at most once, only after it is in view, and through the shared
  // gate so opening a 40-finding inbox never stampedes the model. A reviewer's own text is never
  // overwritten (guarded on an empty value) and pre-drafted cards are skipped (aiDraft/values set).
  useEffect(() => {
    if (!seen || autoDraftedRef.current) return
    if (!editable || !item?.scan_id || !item?.file || !item?.rule_id) return
    // A card the breaker SHORT-CIRCUITED never ran its draft function, so nothing set a message —
    // and an empty card with no explanation is the very thing this is meant to end. Render the
    // reason here (the server's own line, naming the model and the pull) with no request made.
    // Every other failure is already on the card from the draft function's own catch.
    const onFail = (e) => { if (e?.aiModelNotPulled) setDraftMsg({ kind: 'error', text: e.message }) }
    if (multi) {
      if (usingEvidence && instances.length && values.some((v) => !(v || '').trim())) {
        autoDraftedRef.current = true
        runAutoDraft(draftAll).catch(onFail)
      }
    } else if (!aiDraft.current && !(value || '').trim()) {
      autoDraftedRef.current = true
      runAutoDraft(draftWithAi).catch(onFail)
    }
  }, [seen]) // eslint-disable-line react-hooks/exhaustive-deps

  const decide = async (status, rejectReason = null, resolution = null) => {
    if (busy) return
    setBusy(true)
    setActError(null)
    setAskReject(false)
    // The headline value (audit log, telemetry) is the first image's text when the row carries
    // proposals — the same one the collapsed card shows.
    const headline = multi ? (values[0] || '') : value
    const t = reviewTelemetry({
      editable, status, value: headline, aiDraft: aiDraft.current,
      elapsedMs: Date.now() - shownAt.current,
    })
    // What actually gets written into the document, one entry per image, positionally aligned
    // with `instances` (proposals or deferred evidence). Only on approval: rejecting approves no
    // content. A WCAG-exception resolution (decorative / essential logo) writes NO value — the
    // finding is resolved by human judgment, not by authoring alt text — so suppress the values.
    // An explain-only row is the same shape of thing: confirming a derived map authors no content
    // for the document, so it sends no values either. The server ignores them for such a row in any
    // case (store._row_approved_values / _row_is_explain_only) — this keeps the record honest at the
    // source rather than relying on the far end to discard a value we should never have claimed.
    // A decorative confirmation authors no text either — store.approved_decorative_locators
    // routes it to the marker writer, which ignores the value — so it sends none. The draft is an
    // instruction to the reviewer; recording it as their approved TEXT misdescribes what they
    // signed, and it is the string that used to reach the document before #43.
    //
    // A COMPANION row IS sent, and that is the one exception to the pattern above. Its value is
    // not content for the document — store._row_approved_values skips it, so the certify gate is
    // untouched — but it IS the deliverable, and store.approve_proposal_values is the only thing
    // that records the reviewer's text at all. Suppressing it here (which is what happened while
    // captions rode the explain-only branch) discards the correction at the moment of approval,
    // silently, because the machine's draft and the corrected file are both valid WebVTT.
    // ADR 0055's resolution is the ONE that carries text, and every rule below has to know it.
    // "A resolution stands in for the authored value" is true of decorative, essential logo and
    // out-of-scope — each closes a finding by judgement having authored nothing. It is false of
    // described_not_replaced: the reviewer kept the image AND wrote its description, the backend
    // records that description as 1.1.1 alt text the document owes, and routes/hitl.py refuses the
    // decision outright without one. Suppressing the values here would send the reviewer's work
    // nowhere and turn a correct decision into a 422.
    const describedRow = resolution === DESCRIBED_NOT_REPLACED
    const approvedValues = (status === 'approved' && (!resolution || describedRow)
                            && !explainOnly && !decorativeRow && instances.length)
      ? (multi ? values : [value || ''])
      : null
    // Same exception, same reason: a described row DID author a value, so the audit line must
    // carry it rather than reading as a judgement with nothing behind it.
    const finalValue = ((resolution && !describedRow) || explainOnly || decorativeRow)
      ? null : t.finalValue
    const noteOut = note || (resolution === 'decorative' ? 'Marked decorative — no description needed'
      : resolution === 'essential_exception' ? 'Marked essential logo/brand — exempt'
      : describedRow ? 'Kept the image of text and described it — described as alt text, not replaced'
      : null)
    try {
      await onAct(card.id, status, noteOut, finalValue,
                  { edited: t.edited, reviewMs: t.reviewMs, aiValue: t.aiValue, approvedValues,
                    rejectReason, resolution, modelCallId: modelCallId.current,
                    modelCallIds: modelCallIds.some(Boolean) ? modelCallIds : null })
      onResolved && onResolved(card.id, status)
    } catch (e) {
      // HitlBell rolls the optimistic list back and rethrows. Without this catch the rejection
      // was unhandled: the reviewer saw the card sit there with no error, having signed off on
      // nothing. An unrecorded approval must never look like a recorded one.
      setActError(`Not saved: ${e?.message || e}. Nothing was recorded — try again.`)
    } finally {
      setBusy(false)
    }
  }

  // The accessible name carries the document, not just the criterion. It used to read
  // "Review —" (card.wcag falls back to an em-dash when the row has no SC), so a screen-reader
  // user was told nothing about what they were being asked to approve.
  return (
    <section ref={rootRef} className="evcard"
             aria-label={['Review', card.sc ? card.wcag : null, card.fileName || null].filter(Boolean).join(' — ')}
             style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 14, marginBottom: 12, background: 'var(--card, #fff)' }}>
      <header className="evcard-hd" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <span className="fmtchip">{card.fmt}</span>
        {/* WHICH DOCUMENT. First thing after the format chip, because a reviewer approving a
            change to a file needs to know the file before anything else about the finding. */}
        {card.fileName && (
          <span className="evcard-file" title={card.file}>
            <b className="evcard-filename">{card.fileName}</b>
            {card.fileDir && <span className="evcard-filedir muted"> · {card.fileDir}</span>}
          </span>
        )}
        <b className="evcard-wcag">{card.wcag}</b>
        <span className="muted">{card.name}</span>
        {/* Where to look. Rendered only when the analysers attributed a page — the reviewer
            gets no location rather than a wrong one. */}
        {card.location && (
          <span className="evcard-loc muted" title={`This criterion fails on ${card.location.toLowerCase()}`}>
            📍 {card.location}
          </span>
        )}
        {/* Risk tier + estimated effort (#6) — triage signal: how much scrutiny this needs and
            roughly how long, from the review type + severity + grounding. */}
        <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 8, alignItems: 'center' }}>
          <RiskChip item={item} />
          <span className={`conf conf-${card.track.badge.tone}`}>{card.track.badge.label}</span>
        </span>
      </header>

      {/* Review Intent (Review queue) — the first thing the reviewer reads: one plain-language
          sentence, task-first, no jargon. The audit lifecycle/trust states follow below. */}
      {intent && <p className="evcard-intent">{intent}</p>}

      {/* "Why this is safe to approve" (Review queue P0) — positive, up-front evidence framing so
          the reviewer sees what has already been checked without opening the audit trail. Rendered
          ONLY when whySafeToApprove found genuinely affirmative, real signals; a finding that still
          needs human judgement shows nothing here (the "Why human review?" callout carries it). */}
      {whySafe && (
        <div className="evcard-safe">
          <b>✓ Why this is safe to approve</b>
          <ul>
            {whySafe.points.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        </div>
      )}

      {/* Verification ladder — the honest lifecycle of this finding. Each step reflects a real
          pipeline state; a value-fix reads "validates on approval" (not a green pass) because the
          write + re-scan happen when you approve, so the card never claims a fix the doc still lacks. */}
      <div className="evcard-ladder" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', margin: '0 0 12px' }}>
        {ladder.map((s, i) => (
          <span key={i} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
            {i > 0 && <span className="muted" aria-hidden="true">→</span>}
            {/* A not-yet-reached step is dimmed by COLOUR, not opacity. `opacity: 0.55` blended
                --ink (#2b2330, 14:1 on white) down to #8a868d — 3.57:1, under the 4.5:1 that
                11px text requires. --muted is 5.68:1 and reads as the same de-emphasis.
                This is still live, meaningful text — not an inactive control — so 1.4.3's
                disabled-component exemption does not apply to it. */}
            <span className={s.state === 'done' ? 'conf conf-high' : s.state === 'current' ? 'conf conf-medium' : s.state === 'failed' ? 'conf conf-low' : 'conf'}
                  style={s.state === 'todo' ? { color: 'var(--muted)' } : undefined}>
              {s.state === 'done' ? '✓ ' : s.state === 'current' ? '● ' : s.state === 'failed' ? '✗ ' : ''}{s.label}
            </span>
          </span>
        ))}
      </div>

      {/* Post-write outcome — the approved value was written to a working copy, the re-scan refused
          to credit it, and the copy was discarded. Until this line existed the reviewer approved,
          saw nothing change, and had no way to learn why: apply.unverified was only ever logged. */}
      {(() => {
        const o = applyOutcomeCopy(card)
        return o && (
          <p className="evcard-apply-outcome" role="status" style={{ margin: '0 0 12px' }}>
            <b>{o.headline}</b> {o.body}
          </p>
        )
      })()}

      {/* Before/after evidence — ONE pattern for every finding type (HITL vision, roadmap #2): the
          contrast swatch, the heading-outline correction, the language tag. Self-hides when a
          finding type has no visual before/after (the prose + editor carry it). */}
      <BeforeAfterEvidence card={card} />


      {/* Large page preview (ADR 0018) — the visual "where": the finding's page rendered big, the
          hero of the card (Principle 2). When the backend can't rasterize, or no page is recorded
          for the image in view, Thumbnail says "Preview unavailable" and why — it never falls back
          to page 1: PDF always; Office (pptx/docx/xlsx) once LibreOffice is in the image. The
          bounding-box overlay pinpointing the object is the next slice (needs per-shape geometry). */}
      {card.scanId && card.file && (
        <div className="evcard-hero" style={{ margin: '0 0 12px' }}>
          {instances.length > 1 && (
            <div className="evcard-hero-pager">
              <button type="button" aria-label="Previous flagged image"
                      onClick={() => setHeroIdx((i) => (i - 1 + instances.length) % instances.length)}>‹</button>
              <span>Image {heroIdx + 1} of {instances.length}</span>
              <button type="button" aria-label="Next flagged image"
                      onClick={() => setHeroIdx((i) => (i + 1) % instances.length)}>›</button>
            </div>
          )}
          {/* Document heatmap (vision §17): the pages that carry findings, with counts — click to
              jump the hero straight to that page's first finding. Only rendered when geometry
              attributed 2+ pages (a single-page finding needs no map). */}
          {pageStrip.length > 1 && (
            <div className="evcard-pagestrip" aria-label="Pages with findings">
              <span className="muted">On pages:</span>
              {pageStrip.map(([p, idxs]) => (
                <button key={p} type="button"
                        className={`evcard-pagechip${pageMap[heroIdx] === p ? ' evcard-pagechip-on' : ''}`}
                        title={`${idxs.length} finding${idxs.length > 1 ? 's' : ''} on page/slide ${p} — click to jump`}
                        onClick={() => setHeroIdx(idxs[0])}>
                  {p}<span className="evcard-pagechip-n">{idxs.length}</span>
                </button>
              ))}
            </div>
          )}
          {heroIsImage ? (
            // xlsx (ADR 0018): the page render is the whole sheet, so lead with the flagged image
            // itself — the actual r:embed bytes — shown large. A caption says it's isolated from the
            // sheet so the reviewer knows THIS is the image under review, not a crop of a screenshot.
            <figure className="evcard-hero-img">
              <ProposalThumb thumb={heroThumb} alt={thumbAlt(card.thumbKind, card.file)} size={360} />
              <figcaption className="muted">The flagged image, shown on its own (isolated from the sheet)</figcaption>
            </figure>
          ) : (
            <Thumbnail scanId={card.scanId} file={card.file} page={heroPage} locator={heroLocator} maxHeight={360}
                       kindLabel={imgKind?.label?.toLowerCase() || null} />
          )}
          {sourceLink?.url && (
            <div style={{ marginTop: 6, textAlign: 'right' }}>
              <a href={sourceLink.url} target="_blank" rel="noopener noreferrer"
                 className="evcard-source-link"
                 style={{ fontSize: 12, color: 'var(--link-color, #1a56db)', textDecoration: 'none' }}>
                ↗ {sourceLink.label || 'Open source'}
              </a>
            </div>
          )}
        </div>
      )}

      <div className="evcard-body" style={{ display: 'flex', gap: 12, alignItems: 'flex-start' }}>
        {/* The specific object under review — the offending image beside the text. Follows the pager
            (#122) so it's the SAME image the hero boxes above. The whole-page context is the hero
            preview; this is the "what". Suppressed when the hero IS this image already (xlsx isolated
            image) — no point showing the same picture twice, big then small. */}
        {heroThumb && !heroIsImage && !heroCrop && (
          <ProposalThumb thumb={heroThumb} alt={thumbAlt(card.thumbKind, card.file)}
                         size={thumbSize(card.thumbKind, 96)} className="evcard-thumb" />
        )}
        <div className="evcard-main" style={{ flex: 1, minWidth: 0 }}>
          <CropReviewContext evidence={heroCrop} thumb={heroThumb} locator={heroLocator}
                             draft={heroInst?.proposed_value} sourceUrl={sourceLink?.url} />
          <p className="evcard-problem">{guidanceSentence(card) || card.problem}</p>
          {/* Image-kind routing hint (#130) — the model's own noun for what this is, with a hint on
              what a good description looks like for that kind. Honest: derived from the description,
              shown only when a kind is recognised. */}
          {imgKind && (
            <div className="evcard-imgkind" title={`Read from the AI description — ${imgKind.hint}`}>
              <span className="evcard-imgkind-tag">{imgKind.icon} {imgKind.label}</span>
              <span className="muted">{imgKind.hint}</span>
            </div>
          )}

          {/* "Why this recommendation?" (#9) — the structured reasoning chain, one click, not buried:
              problem → what ACP detected (OCR text / grounding / image kind) → suggested value →
              the WCAG requirement that makes it necessary. Every line is a real finding/catalog
              field; no model call, no fabricated number. Every reviewer eventually asks "why did the
              AI think this?" — this answers it in place. */}
          {why && (
            <div className="evcard-explain" style={{ margin: '2px 0 8px' }}>
              <button type="button" className="evcard-explain-btn"
                      aria-expanded={showExplain}
                      onClick={() => setShowExplain((v) => !v)}>
                {showExplain ? '× Hide reasoning' : '✨ Why this recommendation?'}
              </button>
              {showExplain && (
                <div style={{ fontSize: 12.5, lineHeight: 1.5, margin: '7px 0 0', padding: '10px 12px',
                     background: 'var(--surface-1, #f6f5f2)', border: '1px solid var(--line)', borderRadius: 8 }}>
                  {why.problem && <div style={{ fontWeight: 600, marginBottom: 6 }}>{why.problem}</div>}
                  {why.detected.map((d, i) => (
                    <div key={i} style={{ display: 'flex', gap: 6, margin: '2px 0' }}>
                      <span className="muted" style={{ minWidth: 130, flex: '0 0 auto' }}>{d.label}:</span>
                      <span>{d.value}</span>
                    </div>
                  ))}
                  {why.suggested && (
                    <div style={{ display: 'flex', gap: 6, margin: '2px 0' }}>
                      <span className="muted" style={{ minWidth: 130, flex: '0 0 auto' }}>Suggested:</span>
                      <span style={{ fontStyle: 'italic' }}>“{why.suggested}”</span>
                    </div>
                  )}
                  {why.because && (
                    <div style={{ marginTop: 7, paddingTop: 7, borderTop: '1px dashed var(--line)', color: 'var(--fg, #1c1620)' }}>
                      {why.because}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Deferred images (evidence, no AI draft) now render through ProposalEditors below —
              one editable row per image, each with its own "Draft with AI". The old thumbnail
              picker fed one shared textarea, so it could record only a single value; it is gone. */}

          {editable && companionRow ? (
            /* BEFORE the `multi` branch, and that ordering is the bug this fixes. A companion row
               carries exactly one proposal, so `instances.length` is 1 and `multi` is TRUE — the
               per-image editor won the chain and rendered a plain box holding a whole WebVTT
               document. The card looked right and the reviewer got the surface this slice exists
               to replace, which is why the wiring has its own test rather than only the
               component. */
            <div className="evcard-rec">
              <span className="evcard-rec-head">
                <span className="muted" style={{ fontSize: 12 }}>
                  Machine transcription — play the media and correct any line that is wrong
                </span>
              </span>
              {/* The editor fetches the media through api.js itself. The card used to hand it a
                  hand-built `/scans/.../content` string, which carried no bearer and no BASE — so
                  it could only ever have worked against a same-origin, signed-out API. */}
              <CaptionEditor value={value} onChange={setValue} scanId={item?.scan_id}
                             mediaKind={companionMediaKind} filename={companionMediaName} />
            </div>
          ) : editable && multi ? (
            <>
              {/* Drafts generate automatically once the card is in view (auto-draft effect above),
                  so a 14-image card becomes review-and-approve without a click. While the batch runs
                  we show a passive status; if it left images blank (vision unavailable), a low-key
                  "Draft remaining" retry stands in for the removed button — recovery, not a primary
                  action. */}
              {draftingAll ? (
                <span className="evcard-drafting" role="status" style={{ marginBottom: 8 }}>
                  ✨ Drafting all {instances.length} images…
                </span>
              ) : (autoDraftedRef.current && instances.length >= 2 && draftingIdx == null
                    && values.some((v) => !(v || '').trim()) && (
                <button type="button" className="evcard-draftall-btn"
                        title="Some images have no description yet — ask the vision model to draft the rest"
                        onClick={draftAll} style={{ marginBottom: 8 }}>
                  ↻ Draft remaining {values.filter((v) => !(v || '').trim()).length} image{values.filter((v) => !(v || '').trim()).length === 1 ? '' : 's'}
                </button>
              ))}
              <ProposalEditors proposals={instances} values={values} sc={card.sc}
                               onChange={setValueAt} file={card.file}
                               onDraft={usingEvidence ? draftInstance : undefined}
                               draftingIdx={usingEvidence ? draftingIdx : null}
                               onApplyToSimilar={applyToSimilar} onCrossCheck={crossCheck} />
              {usingEvidence && draftMsg && (
                <span className={`evcard-draft-msg evcard-draft-${draftMsg.kind}`} role="status">
                  {draftMsg.text}
                </span>
              )}
              {usingEvidence && !draftingAll && draftingIdx == null && manualCloudHint}
              {usingEvidence && cloudEnabled && card.sc === '1.1.1' && (
                <div className="evcard-copilot" style={{ margin: '8px 0 0' }}>
                  <button type="button" className="evcard-linkbtn"
                          disabled={copilotLoading}
                          onClick={askCopilot}
                          style={{ fontSize: 12.5 }}>
                    {copilotLoading ? '✨ Asking the cloud model…' : '✨ Help me understand this image'}
                  </button>
                  {copilotResult && !copilotResult.error && (
                    <div role="note"
                         style={{ marginTop: 6, fontSize: 12.5, fontStyle: 'italic',
                                  background: 'var(--surface-2, #eff6ff)',
                                  border: '1px solid var(--line)', borderRadius: 8,
                                  padding: '7px 11px' }}>
                      <span style={{ display: 'block', fontStyle: 'normal', fontWeight: 600,
                                     fontSize: 11, color: 'var(--muted)', marginBottom: 3 }}>
                        AI guidance — not a draft{copilotResult.provider ? ` · ${copilotResult.provider}` : ''}
                      </span>
                      {copilotResult.guidance}
                    </div>
                  )}
                  {copilotResult?.error && (
                    <span className="evcard-draft-msg evcard-draft-error" role="alert"
                          style={{ marginTop: 4 }}>
                      {copilotResult.error}
                    </span>
                  )}
                </div>
              )}
              {usingEvidence && ocrAid && (
                <details className="evcard-ocr-aid">
                  <summary className="muted" style={{ fontSize: 12, cursor: 'pointer' }}>
                    🔍 Verify — text OCR-read from this image
                  </summary>
                  <p className="muted" style={{ fontSize: 12, margin: '4px 0 0' }}>“{ocrAid}”</p>
                </details>
              )}
              {/* NOT gated on `usingEvidence`, unlike the OCR aid above it. That guard was
                  correct when the chip could only come from a live draft — drafting in this
                  branch only happens for deferred-evidence rows. It is wrong now: `usingEvidence`
                  is false exactly when the card HAS proposals, which is the scan-time pre-drafted
                  case this chip was extended to cover, so the guard suppressed the one situation
                  it most needed to show. HouseStyleChip renders null on a null value, so it is
                  self-gating and needs no condition here. */}
              <HouseStyleChip houseStyle={houseStyle} />
            </>
          ) : structuralRow ? (
            <section className="evcard-rec-static" aria-label="Proposed PDF tag changes"><b>Proposed PDF tag changes</b><p>ACP writes these source-anchored changes into the saved copy after approval. Document text is preserved; verification remains separate.</p>{proposalList.map((p, i) => <div key={i}><p>{pdfStructuralSummary(p) || 'Structural proposal unavailable — refresh suggestions'}</p><details><summary>Technical plan</summary><pre className="machine-value">{p.proposed_value}</pre></details></div>)}</section>
          ) : editable ? (
            <label className="evcard-rec">
              <span className="evcard-rec-head">
                {/* The draft generates automatically (auto-draft effect above) — no button. The label
                    reflects the live state: drafting → recommendation → or "author it yourself" when
                    no model value could be produced. */}
                <span className="muted" style={{ fontSize: 12 }}>
                  {aiDraft.current ? 'AI recommendation (edit before approving if needed)'
                    : drafting ? '✨ Drafting a suggestion…'
                    : 'No AI draft — type the value a screen reader should announce'}
                </span>
              </span>
              <textarea className="evcard-rec-input" rows={2} value={value}
                        placeholder={aiDraft.current ? '' : 'Type the value a screen reader should announce…'}
                        onChange={(e) => setValue(e.target.value)} />
              {/* Suggested outline — turns the empty box into a guided task when authoring from
                  scratch. Disappears the moment the reviewer starts typing. */}
              {!aiDraft.current && !(value || '').trim() && authoringScaffold(card.sc) && (
                <div className="evcard-scaffold">
                  <b>Suggested outline</b>
                  <ul>{authoringScaffold(card.sc).map((h, i) => <li key={i}>{h}</li>)}</ul>
                </div>
              )}
              {srSupported() && (value || '').trim() && (
                <button type="button" className="evcard-refine-btn" style={{ marginTop: 4 }}
                        title="Hear this read aloud the way a screen reader announces it"
                        onClick={() => speakAsScreenReader(card.sc, value)}>🔊 Hear it</button>
              )}
              {draftMsg && (
                <span className={`evcard-draft-msg evcard-draft-${draftMsg.kind}`} role="status">
                  {draftMsg.text}
                  {/* Auto-draft failed or returned a template — a low-key retry replaces the removed
                      "Draft with AI" button so a transient vision outage is recoverable. */}
                  {(draftMsg.kind === 'error' || draftMsg.kind === 'template') && !drafting && (
                    <button type="button" className="evcard-linkbtn" style={{ marginLeft: 6 }}
                            onClick={draftWithAi}>↻ Try again</button>
                  )}
                </span>
              )}
              {!drafting && manualCloudHint}
              {ocrAid && (
                <details className="evcard-ocr-aid">
                  <summary className="muted" style={{ fontSize: 12, cursor: 'pointer' }}>
                    🔍 Verify — text OCR-read from this image
                  </summary>
                  <p className="muted" style={{ fontSize: 12, margin: '4px 0 0' }}>“{ocrAid}”</p>
                </details>
              )}
              <HouseStyleChip houseStyle={houseStyle} />
            </label>
          ) : explainOnly ? (
            /* Confirm-the-map, not write-this-back. The derived map is shown verbatim and
               read-only: approving records that a human agrees this IS the document's structure,
               which is both the instruction for re-authoring it and the evidence of what the
               structure should be. Nothing here is written into the PDF. */
            <div className="evcard-rec-static evcard-explain-only">
              <span className="muted" style={{ fontSize: 12 }}>
                Confirm this map — it is recorded as the required structure and used to re-author
                the file. Nothing is written into the document.
              </span>
              {instances.map((p, i) => (
                <pre key={i} className="evcard-explain-map">{p.proposed_value}</pre>
              ))}
            </div>
          ) : decorativeRow ? (
            /* A yes/no about the picture, so lead with the picture. No text box: the only text in
               play is the card's instruction to the reviewer, and approving discards it. Approval
               marks the image decorative in the document (the OOXML marker apply_alt writes),
               which is what tells a screen reader to skip it. */
            <div className="evcard-rec-static evcard-decorative">
              <span className="muted" style={{ fontSize: 12 }}>
                Approving marks {instances.length === 1 ? 'this image' : 'these images'} decorative,
                so assistive technology skips {instances.length === 1 ? 'it' : 'them'}. No
                description is written. Only confirm if the image conveys nothing a reader needs.
              </span>
              {instances.map((p, i) => (
                <div key={i} className="evcard-decorative-row">
                  <ProposalThumb thumb={p.thumb} alt="" />
                  <span className="muted" style={{ fontSize: 12 }}>{p.rationale}</span>
                </div>
              ))}
            </div>
          ) : card.recommendation ? (
            <p className="evcard-rec-static"><b>AI recommendation:</b> {card.recommendation}</p>
          ) : null}

          {/* THE ARTEFACT'S WAY OUT. A companion row's value is a FILE that ships beside the
              media, and until #1177's follow-up nothing could hand it back: the WebVTT lived in
              a JSON column no route read, so a reviewer could approve the finding and still not
              obtain the thing they approved.

              A plain <a href>, not a fetch-and-blob: the endpoint sets Content-Disposition, so
              the browser saves it under the right name with no script, which also means it works
              for a keyboard user and a screen reader without any of the focus handling a
              scripted download needs. Shown for a companion row whether or not it is approved —
              a reviewer checking a transcript against the audio needs the file in a player
              BEFORE deciding, and making them approve it to find out whether they should is
              backwards. */}
          {companionRow && item?.id && (
            <p className="evcard-companion-download" style={{ fontSize: 12.5, margin: '6px 0 0' }}>
              <a href={`/hitl/queue/${encodeURIComponent(item.id)}/companion`}
                 download={proposalList[0]?.companion_file || undefined}>
                Download {proposalList[0]?.companion_file || 'the caption file'}
              </a>
              <span className="muted"> — ships beside the media; the file itself is not changed.</span>
            </p>
          )}

          {/* W6 — provenance ON THE SURFACE, not behind the audit disclosure. A reviewer approving an
              AI value must see where it ran WITHOUT expanding anything, because the whole risk is a
              silent GPU→CPU fallback: the weaker local model looks identical on the card. Shown only
              where AI produced a value and we actually know the zone — never a fabricated badge. */}
          {aiValueShown && provZone && (
            <div className="evcard-prov-surface" style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', margin: '2px 0 8px' }}>
              <span style={{ padding: '2px 9px', borderRadius: 6, fontSize: 11.5, fontWeight: 600, whiteSpace: 'nowrap',
                   background: provZone === 'local' ? '#E1F5EE' : 'var(--warn-bg)',
                   color: provZone === 'local' ? '#0F6E56' : 'var(--warn-fg)' }}>
                {provZone === 'local' ? '🟢 Local AI' : '🟡 Cloud AI'}
              </span>
              <span className="muted" style={{ fontSize: 12 }}>
                {provZone === 'local'
                  ? 'ran on your infrastructure — no document left your network'
                  : 'processed off your network'}{actualZone ? '' : ' (configured)'}
              </span>
            </div>
          )}

          {/* Auto-escalation numbered path (P1) — when the ledger shows the local model attempted this
              image and couldn't ground a description, the escalation to a governed cloud provider is
              shown as a transparent path, ON THE CARD, so the failed local attempt reads as step one
              of a working handoff rather than a dead end. Every step is a real ai_calls row — the
              provider names itself, nothing is fabricated (see escalationPath.js). */}
          {escalation && (
            <div className="evcard-escalation" role="note"
                 style={{ display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap', fontSize: 12,
                          background: '#E1F5EE', border: '1px solid var(--line)', borderRadius: 8,
                          padding: '7px 11px', margin: '2px 0 8px' }}>
              <span aria-hidden="true" style={{ color: '#0F6E56' }}>↗</span>
              <span>
                <b>AI escalation path:</b>{' '}
                <span className="evcard-escalation-steps">
                  ✓ local attempted → no grounded description → escalated to {escalation.provider}
                  {escalation.cloudModel ? ` (${escalation.cloudModel})` : ''} → {escalation.cloudOk ? 'grounded' : 'cloud unavailable'}
                </span>
              </span>
            </div>
          )}

          {/* Details ▾ (Review queue progressive disclosure) — the audit jargon a reviewer only
              needs when they want to dig: the trust-state enums, model/provenance/zone + audit
              trail, the clustered detection evidence, and the honest "why a human is here". The
              primary flow above answers what to do; this answers how the AI got there. Collapsed by
              default so the card leads with the decision, not the machinery. */}
          {(trust.grounding || trust.validation || ((card.proposal || card.recommendation) && aiProv) || signals.length > 0 || whyReview) && (
            <details className="evcard-details">
              <summary>🔎 Detection, provenance &amp; audit — how the AI reached this</summary>
              <div className="evcard-details-body">

          {/* Trust basis (ADR 0019 §3a) — verifiable states, NOT a confidence score. Grounding = what
              the value is anchored in (OCR / document text / a visual guess); Validation = whether an
              objective check has actually passed. The review-requirement axis is the "Why human
              review?" callout below. No number, no opaque level. */}
          {(trust.grounding || trust.validation) && (
            <div className="evcard-trust" style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center', margin: '4px 0 8px' }}>
              {trust.grounding && (
                <span style={trustPill(trust.grounding.tone)} title="Grounding — what the value is anchored in">{trustIcon(trust.grounding.tone)} {trust.grounding.label}</span>
              )}
              <span style={trustPill(trust.validation.tone)} title="Validation — whether an objective check has passed">{trustIcon(trust.validation.tone)} {trust.validation.label}</span>
              {trust.review && (
                <span style={trustPill(trust.review.tone)} title="Review requirement — why a human is (or isn’t) needed">{trustIcon(trust.review.tone)} {trust.review.label}</span>
              )}
              {card.proposal && card.proposal.list.length > 1 && (
                <span className="muted" style={{ fontSize: 12 }}>· {card.proposal.list.length} instances on this criterion</span>
              )}
            </div>
          )}

          {/* AI provenance (ADR 0019 Phase 0) — don't hide the model. Names the model that produced
              this value (from the proposal's own source, or the active model) and, from the real
              backend config, WHERE the bytes were processed: 🟢 local = on your own infrastructure,
              nothing left your network. Shown only where AI actually generated a value. */}
          {(card.proposal || card.recommendation) && aiProv && (
            <div className="evcard-provenance" style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', fontSize: 12, margin: '2px 0 8px' }}>
              <span className="muted">🤖 {card.proposalSource || `${aiProv.provider} · ${card.sc === '1.1.1' ? aiProv.vision_model : aiProv.model}`}</span>
              <span style={{ padding: '2px 8px', borderRadius: 6, fontSize: 11.5, whiteSpace: 'nowrap',
                   background: provZone === 'local' ? '#E1F5EE' : 'var(--warn-bg)',
                   color: provZone === 'local' ? '#0F6E56' : 'var(--warn-fg)' }}>
                {provZone === 'local' ? '🟢 Local only' : '🟡 Cloud'}
              </span>
              <span className="muted">{provZone === 'local'
                ? 'processed on your infrastructure — no document left your network'
                : `sent to ${aiProv.host}`}</span>
              {item?.scan_id && (
                <button type="button" className="evcard-audit-btn" aria-pressed={showAudit}
                        onClick={toggleAudit}>
                  {showAudit ? 'Hide audit trail' : '🔎 AI audit trail'}
                </button>
              )}
            </div>
          )}

          {/* The AI audit trail (#129) — the real ledger of model calls for THIS file: what model
              ran, on which surface, in which privacy zone, how long it took, and whether it
              succeeded. Every row is a persisted ai_calls record (ADR 0019 Phase 0b) — nothing here
              is fabricated; an empty ledger says so honestly rather than inventing activity. */}
          {showAudit && (
            <div className="evcard-audit" style={{ margin: '2px 0 10px' }}>
              {aiCalls === null ? (
                <div className="muted" style={{ fontSize: 12 }}>Loading audit trail…</div>
              ) : auditRows.length === 0 ? (
                <div className="muted" style={{ fontSize: 12 }}>
                  No AI calls recorded for this file — it was handled deterministically (no model saw it).
                </div>
              ) : (
                <table className="evcard-audit-table">
                  <thead>
                    <tr><th>Surface</th><th>Model</th><th>Zone</th><th>Latency</th><th>Result</th></tr>
                  </thead>
                  <tbody>
                    {auditRows.map((c, i) => (
                      <tr key={c.id || i}>
                        <td>{c.surface || '—'}</td>
                        <td>{c.provider ? `${c.provider} · ${c.model}` : (c.model || '—')}</td>
                        <td><span className={`audit-zone audit-zone-${c.zone === 'local' ? 'local' : 'cloud'}`}>
                          {c.zone === 'local' ? '🟢 local' : '🟡 cloud'}</span></td>
                        <td>{Number.isFinite(c.latency_ms) ? `${(c.latency_ms / 1000).toFixed(1)}s` : '—'}</td>
                        <td>{c.ok === false ? '✗ failed' : '✓ ok'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {/* The evidence behind the finding — concrete, checkable signals the pipeline produced,
              CLUSTERED (Detection / Document state / Reasoning) the way a reviewer scans them, never
              an invented score. An empty group is skipped. */}
          {signals.length > 0 && (
            <div className="evcard-evidence" style={{ margin: '2px 0 8px' }}>
              {GROUP_ORDER.filter((g) => signalGroups[g]).map((g) => (
                <div key={g} style={{ margin: '0 0 6px' }}>
                  <div className="muted" style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.04em', margin: '0 0 2px' }}>{g}</div>
                  {signalGroups[g].map((s, i) => (
                    <div key={i} style={{ display: 'flex', gap: 7, alignItems: 'flex-start', fontSize: 12, margin: '2px 0' }}>
                      <span aria-hidden="true" style={{ color: s.tone === 'warn' ? '#BA7517' : '#0F6E56', flexShrink: 0 }}>{s.tone === 'warn' ? '⚠' : '✓'}</span>
                      <span className="muted">{s.text}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}

          {/* "Why am I reviewing this?" — the honest reason a human is in the loop for this finding,
              so the reviewer understands the ask before approving. Null → nothing shown. */}
          {whyReview && (
            <div className="evcard-whyreview" style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 12.5,
                 background: 'var(--warn-bg)', border: '1px solid var(--line)', borderRadius: 8, padding: '8px 11px', margin: '0 0 8px' }}>
              <span aria-hidden="true" style={{ color: 'var(--warn-fg)' }}>❓</span>
              <span><b>Why human review?</b> {whyReview}</span>
            </div>
          )}

              </div>
            </details>
          )}

          {card.diffs.length > 0 && (
            <div className="evcard-ba">
              {card.diffs.slice(0, 1).map((d, i) => (
                <div key={i}>
                  <div className="diffbox before"><span className="difftag">before</span>{d.before}</div>
                  <div className="diffbox after"><span className="difftag">after</span>{d.after}</div>
                </div>
              ))}
            </div>
          )}

          {/* Deterministic validation (vision #12/#33) — the machine-verified receipt for an applied
              fix, kept DISTINCT from the AI-written value above. Every ✓ is a real fact (a
              remediation_diff / validated proposal proves write + re-open + re-scan + clear), never
              a bare "Done". Absent until something is actually applied + verified. */}
          {valChecklist && (
            <div className="evcard-valcheck" style={{ margin: '2px 0 8px', padding: '9px 11px',
                 background: '#E1F5EE', border: '1px solid var(--line)', borderRadius: 8 }}>
              <div style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.04em', color: '#0F6E56', margin: '0 0 4px' }}>Deterministic validation</div>
              {valChecklist.map((v, i) => (
                <div key={i} style={{ display: 'flex', gap: 7, alignItems: 'flex-start', fontSize: 12.5, margin: '2px 0', color: '#0F6E56' }}>
                  <span aria-hidden="true">✓</span><span>{v.label}</span>
                </div>
              ))}
            </div>
          )}

          {/* What approval actually does. A judgement sign-off resolves the criterion; a
              value-fix approval only records evidence — ACP has no write-back yet, so the
              criterion keeps failing until the file is fixed and re-scanned. */}
          {card.certifiesOnApprove ? (
            <p className="evcard-impact muted" style={{ fontSize: 12 }}>
              Compliance: <span className="conf conf-low">{card.impact.before}</span> → <span className="conf conf-high">{card.impact.after}</span> after approval
            </p>
          ) : (
            <div className="evcard-todo">
              <b>What you need to do</b>
              {manyInstances ? (
                <p>
                  These <b>{card.findingCount} images</b> each need their own description — one sentence
                  cannot describe them all. Open the file, write alt text on each image, then
                  <b> ✋ I’ll fix it</b> to re-scan and confirm.
                </p>
              ) : (
                <p>
                  Write the text a screen reader should announce, then approve it. ACP records
                  your value as compliance evidence.
                </p>
              )}
              <p className="muted">
                Approving records your sign-off — it does <b>not</b> write the value into the document,
                so <span className="conf conf-low">{card.sc}</span> keeps failing until the file is
                fixed and re-scanned.
              </p>
            </div>
          )}

          {/* How the reviewer confirms the fix in the native app — the "verify it yourself in
              under 10s" half of the trust model. Renders per-criterion Word/Excel/PowerPoint/
              Acrobat steps (Mac/Win), or only the universal Accessibility-Checker line when no
              crisp native step exists; nothing at all when neither applies (never a wrong path). */}
          <HowToConfirm sc={card.sc} file={card.file} />

          {/* Certification preview — the auditor-facing entry this review will produce, assembled
              from what's already on the card (no new endpoint). The reviewer + timestamp are
              stamped on approval, so it says "on approval" rather than pre-filling a fake signer. */}
          {(card.thumb || card.recommendation || value) && (
            <details className="evcard-cert" style={{ margin: '0 0 8px' }}>
              <summary style={{ fontSize: 13, cursor: 'pointer', color: 'var(--muted, #666)' }}>
                📄 What the certification report will record
              </summary>
              <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start', marginTop: 8, padding: '9px 11px',
                   border: '1px solid var(--line)', borderRadius: 8, background: 'var(--surface-1, #f6f5f2)' }}>
                {card.thumb && <ProposalThumb thumb={card.thumb} alt="" size={40} />}
                <div style={{ fontSize: 12, minWidth: 0 }}>
                  <div><b>{card.wcag}</b> · {card.file}{card.location ? ` · ${card.location}` : ''}</div>
                  {(value || (multi && values[0]) || card.recommendation) && (
                    <div className="muted" style={{ margin: '3px 0', wordBreak: 'break-word' }}>Value: “{value || (multi && values[0]) || card.recommendation}”</div>
                  )}
                  <div><span className="conf conf-high">Verified</span> <span className="muted">on approval · signed off by you</span></div>
                </div>
              </div>
            </details>
          )}

          <input className="rc-note" placeholder="Reviewer note (optional)" value={note}
                 onChange={(e) => setNote(e.target.value)} />

          {/* Beside the buttons that failed, not in a corner. role=alert so a screen-reader
              user is told too — this card is the accessibility product's own review surface. */}
          {actError && (
            <p role="alert" className="evcard-act-error"
               style={{ margin: '0 0 8px', padding: '9px 11px', borderRadius: 8, fontSize: 13,
                        background: '#FDECEC', color: '#8A1F1F', border: '1px solid #E9A8A8' }}>
              {actError}
            </p>
          )}

          {/* WCAG-exception resolution (HITL-six close-out) — the two findings a model must NOT
              decide alone. Instead of authoring alt text, the reviewer applies the exception the
              standard allows: a decorative image needs no description (1.1.1), and an essential
              logo/brand mark is exempt from the images-of-text rule (1.4.5/1.4.9). One tap resolves
              the finding by human judgment; the reason is recorded in the audit trail, and no
              DESCRIPTION is written, so nothing is misrepresented as a fix. Decorative on an Office
              file additionally writes the OOXML decorative MARKING (empty alt + the marker the
              analysers honour), so the decision lives in the document and a later scan does not
              re-raise the same finding — see api/apply_alt.py. */}
          {card.sc === '1.1.1' && (
            <div className="evcard-exception">
              <span className="muted">Is this image purely decorative? Then it needs no description.</span>
              <button type="button" className="ghost small" disabled={busy}
                      title="Resolve as decorative — records that this image needs no text alternative (WCAG 1.1.1). No description is written; the image is marked decorative instead."
                      onClick={() => decide('approved', null, 'decorative')}>🚫 Decorative — no alt needed</button>
            </div>
          )}
          {/* Images-of-text exception (1.4.5/1.4.9), routed by detected image kind (#130): a logo or
              an unidentified image gets the "essential logotype — exempt" disposition; a DATA image
              (a chart, diagram, screenshot…) instead gets honest remediation guidance and NO logo
              question — a chart is not a logo, and its text should become real text, not be waved
              through as essential. */}
          {(() => {
            const exc = imagesOfTextException(card.sc, imgKind, heroCrop)
            if (!exc) return null
            return (
              <div className="evcard-exception">
                {/* The note and the action are no longer alternatives. A DATA image gets both:
                    the guidance still leads (real text is the better fix whenever it is possible)
                    and "keep it — describe it" follows as the way out when it is not. Before
                    ADR 0055 this branch rendered guidance and nothing to click. */}
                {exc.note && <span className="muted evcard-exception-note">{exc.note}</span>}
                {exc.prompt && <span className="muted">{exc.prompt}</span>}
                {exc.action && (() => {
                  // A described decision CARRIES the reviewer's text, so the button waits for it —
                  // and waits for text they actually WROTE, not the box as it came.
                  //
                  // The editor is seeded from `proposed_value` (ProposalEditors.seedValues), and on
                  // a 1.4.5 card that draft is the OCR TRANSCRIPT: the words baked into the picture.
                  // A transcript is not a description of the image. Accepting the unedited box would
                  // write "Q3 revenue rose 12%" where "a slide titled Q3 revenue, reading …" belongs,
                  // and would do it on the one path whose whole premise is that the picture stays.
                  // The backend refuses to fall back to a draft for exactly this reason
                  // (store.queue_described_image_alt); this is the same rule where the reviewer can
                  // see it, as a disabled control with a reason rather than a click that 422s.
                  const wantsText = !!exc.action.needsText
                  const seeded = seedValues(instances)
                  const authored = (v, i) => {
                    const t = String(v || '').trim()
                    return !!t && t !== String(seeded[i] || '').trim()
                  }
                  const hasOwnWords = multi
                    ? values.some(authored)
                    : authored(value, 0)
                  const blocked = wantsText && !hasOwnWords
                  return (
                    <button type="button" className="ghost small" disabled={busy || blocked}
                            title={blocked ? exc.action.needsTextHint : exc.action.title}
                            onClick={() => decide('approved', null, exc.action.resolution)}>
                      {exc.action.label}
                    </button>
                  )
                })()}
              </div>
            )
          })()}

          <div className="evcard-actions">
            <button className="qbtn approve" disabled={busy || !structuralReady} onClick={() => decide('approved')}>✓ {primaryAction}</button>
            <button className="qbtn self" disabled={busy}
                    title="Take ownership — fix it yourself, then re-scan to confirm"
                    onClick={() => decide('skipped')}>✋ I’ll fix it</button>
            <button className="qbtn reject" disabled={busy} aria-expanded={askReject}
                    onClick={() => setAskReject((v) => !v)}>✕ Reject{askReject ? ' —' : '…'}</button>
            {/* Out of scope — the criterion does not apply to this document. Resolves the finding
                (status stays approved, no value written) and takes it OUT of the coverage denominator,
                persisted as an out_of_scope resolution — distinct from the decorative/logo exceptions,
                which resolve an in-scope finding. */}
            <button className="qbtn" disabled={busy}
                    title="Not applicable — this criterion does not apply to the document. Resolves the finding without writing any text and removes it from the coverage denominator."
                    onClick={() => decide('approved', null, 'out_of_scope')}>⊘ Not applicable</button>
            {item?.scan_id && item?.file
              ? <button type="button" className="rc-trace" onClick={() => setTracePanel(true)}
                        title="View this document’s trace inside AccessOps (no Langfuse login)">📊 View trace</button>
              : traceUrl && <a className="rc-trace" href={traceUrl} target="_blank" rel="noopener noreferrer">📊 View trace</a>}
            {tracePanel && <TracePanel scanId={item.scan_id} file={item.file} onClose={() => setTracePanel(false)} />}
          </div>
          {/* Feedback intelligence: one more click captures WHY. Each rejection becomes training
              signal for "which rules/doc types are weakest" — real reviewer behaviour, not
              intuition. Chips submit immediately; no extra confirm. */}
          {askReject && (
            <div className="evcard-rejectwhy" role="group" aria-label="Why are you rejecting this?"
                 style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center', marginTop: 8 }}>
              <span className="muted" style={{ fontSize: 12 }}>Why?</span>
              {[['incorrect_object', 'Incorrect object'], ['too_vague', 'Too vague'],
                ['hallucinated', 'Hallucinated'], ['missed_text', 'Missed important text'],
                ['org_preference', 'Organization preference'], ['other', 'Other']].map(([k, label]) => (
                <button key={k} type="button" className="ghost small" disabled={busy}
                        onClick={() => decide('rejected', k)}>{label}</button>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
