# ACP web UI accessibility — evaluation protocol and evidence checklist

Status: protocol only. **No manual or assistive-technology result in this document has been
recorded yet.** Every result cell reads *Not yet tested* until a named tester fills it with a real
run. Do not fill a cell from markup, from a code review, or from an automated scan alone.

Local jsdom/vitest tests cited below establish DOM facts (roles, attributes, key handling, colour
values in source). They are not screen-reader output, not rendered measurements, and not a
conformance result. Fixes described as implemented exist on a local branch only; none is recorded
here as deployed or verified.

**Still outstanding before any UI conformance claim** (none has a recorded result):

- screen-reader runs with exact AT, browser and OS versions (section 5);
- an automated re-scan of the deployed build that contains the banner fix, plus a manual contrast
  check in both palettes (sections 6.8, 8 step 5; findings F1, F2, F5);
- 320 CSS px reflow / 400 % zoom (6.5), 200 % browser zoom and text-only zoom (6.6), text spacing
  (6.7), content on hover or focus (6.9), per-message status announcements (6.10);
- the six WCAG 2.2 A/AA additions — 2.4.11, 2.5.7, 2.5.8 target size, 3.2.6, 3.3.7, 3.3.8
  authentication (section 7).

Product: Movate AccessOps (internal name ACP), **web application user interface**.

## 1. Scope — what this document is about, and what it is not

This protocol covers **the accessibility of ACP's own web UI**: the screens, dialogs and workflows
a person uses to operate the product.

It is **not** about the quality or conformance of the customer documents ACP assesses and
remediates. Those have their own evidence machinery, and nothing here changes it:

| Subject | Where it lives | Relationship to this document |
|---|---|---|
| ACP's own UI (this document) | this protocol; the Conformance tab's ACR workspace ([PRD](prd-acr-workspace.md), [ADR 0047](adr/0047-acr-workspace-data-model.md)); guided plans in `config/acr-manual-test-plans.json` | The runs this protocol asks for are the manual evidence the ACR workspace records |
| ACP's own UI, older hand-written summary | [`conformance-report.md`](conformance-report.md) Part 1 | Superseded as evidence by runs recorded under this protocol (see the note added there) |
| Customer documents | per-scan certification report ([guide](certification-report-for-auditors.md)), capability matrix | Out of scope. A document finding is never evidence for a UI claim (`tests/test_acr_no_regression.py::test_the_acr_tables_do_not_join_to_scan_data`) |

A conformance claim about the UI is scoped, per the WCAG conformance requirements, to **full
pages**, **complete processes**, and **every state a page can be in** — including content that is
hidden until revealed. A claim made from "the primary workflow views" in one browser does not meet
that scope.

## 2. Tool limitations — what automation can and cannot tell you

- **Automated checks cannot establish conformance.** axe-core reports what its rules can see.
  `config/acr-manual-test-plans.json` (`_meta`) records that axe-core 4.12.1 has rules for
  **23 of the 55** WCAG 2.2 A/AA criteria and **none** for the other 32, and every automated row the
  ACR workspace ingests is declared `Coverage.PARTIAL` (ADR 0031), so an automated pass never
  becomes "Supports" by itself.
- **Accessibility Insights for Web's Assessment is organised around WCAG 2.1 AA.** A completed
  Assessment is not evidence for the six Level A/AA criteria WCAG 2.2 added (2.4.11, 2.5.7, 2.5.8,
  3.2.6, 3.3.7, 3.3.8). Each needs its own test (section 7). Check the release notes of the
  version you used before relying on any automated 2.2 coverage, and record the version.
- **This repo's own automated UI audits run a different palette from the default.**
  `frontend/e2e/wcag-a11y.spec.js` (`runAxe`, lines 76–86) and `frontend/src/wcagAxeMatrix.test.jsx`
  set `document.documentElement.dataset.wcag = 'on'` before running axe. That attribute is the
  **High-contrast palette** preference in the header Accessibility menu, which is **off by
  default** (`App.jsx`, `wcagMode` initialised from `localStorage`, `false` when unset). A green run
  of those suites is evidence about the high-contrast palette, not the default one. The same spec
  runs axe with tags `wcag2a`, `wcag2aa`, `wcag21aa` only — axe rules tagged `wcag22aa` (for
  example target size) are not executed. `wcagAxeMatrix.test.jsx` also disables `color-contrast`,
  because jsdom cannot compute rendered colours. Neither suite is evidence that every view or state
  was scanned; neither renders the update banner (`VersionToast`) at all, which is where the
  reported 1.4.3 failure is.
- **jsdom (vitest) has no layout.** Reflow, zoom, text spacing, target size, focus obscuring and
  contrast of rendered pixels cannot be established there.
- **The shared preview server serves the shared checkout, not a worktree** (see `CLAUDE.md`).
  Browser evidence must come from a build whose commit is recorded (section 4).
- **A screen reader is required for 4.1.2 and 4.1.3.** An `aria-live` attribute in markup shows
  intent, not that anything is announced.

## 3. Coverage — pages, states and complete workflows

Test every row. A row is complete only when each state listed has a recorded result.

### 3.1 Views (header navigation, `TABS` in `frontend/src/App.jsx`)

| View | States to cover |
|---|---|
| Sign-in (`SignIn.jsx`) | initial; busy; each error message; signed-out notice |
| Overview | empty; populated |
| Sources (integrations) | no connection; connecting; connected; reconnect/expired; error |
| Discover | no run; running (live progress); completed; failed/unreadable files; scope wizard (`ScanScopeWizard`) |
| Assess | no eligible files; setup; running; results; finding detail; empty filters |
| Remediate | plan choices; running (live progress); Review workspace; approve/reject; bulk selection; comments; caption editor; exceptions |
| Release | plan; confirmation; in-progress; completed; failed with recovery actions; history; reports |
| Monitor | empty; populated |
| Live Operations | no capacity; active work; drawer open |
| Scan Analytics | no scans; populated; comparison |
| Knowledge Graph | populated; detail panel |
| Conformance (ACR workspace) | no reports; report overview; criteria matrix; criterion detail; manual plan run; publish; revision history |
| Settings / admin | each settings panel; roles and people access; capacity schedule editor; disposition rules |

### 3.2 Global and revealed content (test on every view where it can appear)

Header Accessibility menu and account menu (`<details>` disclosures); HITL notification bell;
chat widget; update banner (`VersionToast`); drawers (queue, file, review, graph findings, Live
Operations); dialogs (`ConfirmDialog`, scan review, cross-stage conflict, workspace roles);
tooltips (`InfoTip`, `Term`, `RemediationOptionHelp` — shown on hover and focus); success and
error toasts; loading placeholders; empty states; error boundary; process-health and
release-recovery banners; disabled/locked navigation items; the dev/`?a11y` self-check panel
(test it if it is reachable in the evaluated build, otherwise record it as not present).

### 3.3 Complete processes (end to end, one sitting, same AT session)

| # | Process | Start → end |
|---|---|---|
| P1 | Authentication | Signed-out landing → Microsoft (Entra) or Google sign-in → first signed-in view → sign out |
| P2 | Connect a source | Sources → add connection → choose scope → connected |
| P3 | Discover | Start scan → watch progress → review inventory |
| P4 | Assess | Choose scope → run → read results → open a finding |
| P5 | Remediate and review | Choose plan → run → review a proposal → approve / reject / comment |
| P6 | Release / publish | Select files → confirm → watch delivery → handle a failure → read the report |
| P7 | Conformance report | Create report → record evidence → complete a manual plan → publish |
| P8 | Administration | Change a setting → change a role → save → confirm the change |

## 4. Evidence record — required fields for every test

One record per test (criterion × view/process × AT/browser combination).

| Field | Notes |
|---|---|
| Record ID | stable, e.g. `UI-2.4.11-P6-NVDA-CHR-01` |
| Date and time of test | when it actually happened |
| Build | `version` and `commit` from `GET https://<host>/healthz`. `commit: null` = image predates stamping; a `-dirty` suffix = not a committed tree; record either as-is |
| Environment | host/URL (no secrets or customer names), data fixture used |
| URL / view / state | including the revealed state under test |
| Browser + exact version | |
| Operating system + version | |
| Assistive technology + exact version | or "none" |
| Palette | default or High-contrast (header Accessibility menu) |
| Zoom / viewport / text settings | e.g. 1280×1024 at 400 %; text-only zoom 200 % |
| Tester | name and role |
| Tool + version | e.g. Accessibility Insights for Web x.y.z (axe-core a.b.c) |
| Steps | numbered, reproducible |
| Expected | the pass condition from this document |
| Actual | what happened, verbatim announcements where relevant |
| Evidence reference | screenshot / recording / AT speech log path |
| Result | `Pass` · `Fail` · `Not applicable (reason)` · `Not yet tested` |
| Linked finding | ID in section 10 when `Fail` |

Record runs in the Conformance tab's manual test plans where a plan exists, so the evidence carries
the same metadata the publish gate requires (PRD §*Phase 3*).

## 5. Assistive technology and browser matrix

Versions are left blank on purpose — fill with what was actually installed on the day.

| # | Screen reader (version) | Browser (version) | OS (version) | Processes covered | Result |
|---|---|---|---|---|---|
| AT1 | NVDA (____) | Chrome (____) | Windows (____) | P1–P8 | Not yet tested |
| AT2 | NVDA (____) | Firefox (____) | Windows (____) | P1–P8 | Not yet tested |
| AT3 | JAWS (____) | Chrome (____) | Windows (____) | P1–P8 | Not yet tested |
| AT4 | JAWS (____) | Edge (____) | Windows (____) | P1–P8 | Not yet tested |
| AT5 | VoiceOver (macOS ____) | Safari (____) | macOS (____) | P1–P8 | Not yet tested |
| KB | none — keyboard only | Chrome, Firefox, Safari (____) | as above | P1–P8 | Not yet tested |
| MAG | Windows Magnifier or ZoomText (____) / browser zoom | Chrome (____) | Windows (____) | P1–P8 | Not yet tested |
| VC | Voice Control (macOS) or Dragon (____) | Safari / Chrome (____) | as above | P1, P5, P6 | Not yet tested |

## 6. General criteria procedures

### 6.1 Keyboard — 2.1.1, 2.1.2, 2.4.3, 2.4.7, 1.4.11 (focus indicator)

For each process P1–P8, mouse unplugged:

1. Tab / Shift+Tab through every control, including revealed ones. **Pass:** every function
   reachable and operable; no trap; order preserves meaning.
2. Open each dialog/drawer/menu. **Pass:** focus moves into it; Escape or a close control exits;
   focus returns to the opener (or to a logical place if the opener is gone).
3. Operate composite widgets with their expected keys (tabs: arrows; menus; the resizable
   separator in the Review workspace: arrows).
4. Every focused control shows a visible indicator. **Pass (2.4.7):** visible in default and
   High-contrast palettes. **Pass (1.4.11):** the indicator pixels contrast ≥ 3:1 against adjacent
   colours — measure with a colour picker on a screenshot, not from CSS.
5. Live updates (progress, counters) arrive while a control has focus. **Pass:** focus does not
   move.

### 6.2 Navigation semantics — 1.3.1, 4.1.2

Use the role that matches the behaviour, and verify what the screen reader announces:

| Pattern | Correct semantics | What does not prove it |
|---|---|---|
| Real tabs (switch panels in place) | `role="tablist"`/`tab`/`tabpanel`; selection via `aria-selected`; arrow-key movement; `aria-controls` to a panel that exists | `aria-expanded` — that attribute belongs to disclosures, not tabs |
| Site/app navigation (links or buttons that change view) | `nav` landmark with links/buttons; current item via `aria-current` (`page` or `step`) | `role="tab"` on navigation items |
| Disclosures (menus, expanders, tooltip toggles) | button with `aria-expanded` | `aria-selected` |

**What the code does (DOM facts from jsdom tests, not screen-reader output).** The header workflow
navigation is a real tab widget, not a list of links: `<nav aria-label="Compliance workflow">`
wraps a `role="tablist"` of `<button role="tab">` items, each `aria-controls="workflow-panel"`,
with one `role="tabpanel"` inside `<main id="main-content">` (`App.jsx`). Selection is
`aria-selected` (plus a redundant `aria-current="step"`); no tab uses `aria-expanded`. Only the
selected tab is in the Tab order; Left/Right wrap, Home/End jump, disabled tabs are skipped, and
arrow movement also activates (`workflowTabs.js`). Focus stays on the tab after a view change.
Pinned by `frontend/src/workflowNavigationSemantics.test.jsx`; in-page tab sets by
`subTabSemantics.test.jsx` and `liveOpsFlowTabsSemantics.test.jsx`.

Test: with each screen reader, move to the header navigation and to one in-page tab set. Record
the role, name, state and position announced, and whether arrow keys or Tab move between items.

### 6.3 2.4.5 Multiple Ways

A **skip link is a bypass mechanism (2.4.1)**, not a way to locate a page, and does not count.
Workflow navigation is **one** way. List the genuine ways for each view, e.g. header navigation,
links from Overview/stage cards, notification bell deep links, search/filter where it locates a
view, browser history/URLs if views are addressable.

**Pass:** at least two genuine ways reach each view, **or** the view is a step in a process and the
report states that exception explicitly for that view. Record which applies per view.

### 6.4 3.3.4 Error prevention for data-changing actions

For each of: publish/release to a source, approve/reject proposals (single and bulk), auto-apply
AI enablement, delete/remove a connection or rule, overwrite source files, change roles, publish a
conformance report. **Pass:** the action is reversible, **or** input is checked with a chance to
correct, **or** the user reviews and confirms before it is final. Record which mechanism and where.

### 6.5 1.4.10 Reflow

Browser window 1280 CSS px wide at 400 % zoom (equivalent to 320 CSS px), and a 320 px viewport
for vertical scrolling content. **Pass:** no loss of content or function and no horizontal
scrolling for text content. Data tables, the waterfall/graph views and similar two-dimensional
content may scroll in two dimensions **inside their own container**; record each such exception by
name. Existing layout fixtures (`frontend/e2e/app-styling.fixture.mjs`,
[app styling audit](app-styling-audit.md)) emulate the width; they do not replace a native
browser-zoom check, and that audit says so.

### 6.6 1.4.4 Resize text

Two checks, both required: browser zoom 200 %, and **text-only zoom 200 %** (Firefox *Zoom text
only*; Safari/Chrome minimum font size where applicable). **Pass:** no clipped, truncated or
overlapping text and no lost function, in every state from section 3.

### 6.7 1.4.12 Text spacing

Apply line-height 1.5, paragraph spacing 2× font size, letter spacing 0.12×, word spacing 0.16×
(a text-spacing bookmarklet or stylesheet). **Pass:** no content or function lost, including
tooltips, tiles, drawers, buttons with fixed heights and truncated filenames.

### 6.8 1.4.3 / 1.4.11 Contrast

Measure rendered colours (colour picker on screenshots, or the tool's contrast checker) for every
text and UI-component state in both palettes: toasts and banners, disabled/locked items, badges,
chart marks, focus indicators, placeholder text. **Pass:** text ≥ 4.5:1 (≥ 3:1 large text);
component boundaries/states and required graphics ≥ 3:1.

### 6.9 1.4.13 Content on hover or focus

ACP has hover/focus content (`InfoTip`, `Term`, `RemediationOptionHelp` render `role="tooltip"` on
hover and focus). **Pass:** dismissible without moving pointer/focus (Escape), hoverable (pointer
can move onto the tooltip), persistent until dismissed or no longer relevant.

### 6.10 4.1.3 Status messages — per message, not per region

One live region working proves that one region works. Inventory every status message and verify
each with every screen reader in section 5. The source contains live-region or status markup in
well over a hundred components, so build the inventory from the running UI, not from a grep.

| Kind | Examples to find | Pass condition |
|---|---|---|
| Toasts / banners | update banner, auto-approve confirmation, save confirmations | announced once, without focus moving |
| Progress | Discover, Assess, Remediate, Release live progress; queue drawer | material changes announced (start, phase change, completion, failure); not every counter tick |
| Result counts | filter/search result counts, eligibility counts | new count announced after the change |
| Errors not caused by form submission focus | save failures, token/session expiry, network loss, release failures | announced (assertive where urgent) |
| Completion | scan/assessment/remediation/release finished | announced even if the user is on another view, or clearly not claimed |
| Chat widget replies | assistant responses | announced |

Record per message: trigger, expected announcement, actual announcement (verbatim), focus
location before and after.

## 7. WCAG 2.2 additions (A/AA) — separate evidence required

### 7.1 2.4.11 Focus Not Obscured (Minimum) — AA

Author-created overlays to check: the fixed update banner (`VersionToast.jsx`,
`position: fixed; top: 0; z-index: 9999`), sticky table headers, sticky page header, drawers,
chat widget, toasts, release-recovery banner.

1. With each overlay showing, Tab through the page behind it (and through long tables with sticky
   headers, scrolling by keyboard).
2. **Pass:** no focused component is **entirely** hidden by author content. A user-dismissible
   overlay that can be closed without moving focus is acceptable only if that is how the report
   describes it.
3. Repeat at 400 % zoom, where sticky elements take a larger share of the viewport.

### 7.2 2.5.7 Dragging Movements — AA

Known drag interactions in the source to check: the Review workspace's resizable separators
(`RemediationInbox.jsx`, `Divider`: pointer drag plus arrow keys), range sliders
(`RemediationImpactCard.jsx`, `type="range"`), the activity-history scroll control
(`RemediationOpsPanel.jsx`, `<input type="range" class="remops-history-scrollbar">`, 16 px wide), and pan/zoom in graph views (React Flow
in Live Operations and the waterfall graph).

1. For each, try to achieve the same result **with single pointer clicks/taps only, no drag**.
2. **Pass:** a single-pointer alternative exists (e.g. click on a slider track, step buttons,
   preset sizes, zoom buttons), or dragging is essential. **A keyboard alternative does not satisfy
   2.5.7** — it satisfies 2.1.1.

### 7.3 2.5.8 Target Size (Minimum) — AA

1. For each pointer target, measure the rendered bounding box in DevTools (Computed → box model, or
   `getBoundingClientRect()`), at 100 % zoom.
2. **Pass:** ≥ 24 × 24 CSS px, **or** a 24 px diameter circle centred on the target's bounding box
   does not intersect another target or another target's circle (spacing exception), **or** an
   equivalent control meets the size, **or** it is inline in text, user-agent controlled, or the
   presentation is essential.
3. Priority targets: icon-only buttons (dismiss ✕ buttons, tooltip "i" triggers, `Term` info
   buttons whose SVG is 13 px — measure the button box, not the icon), the 7 px-wide resizable separators, the 16 px-wide activity-history range
control, table row actions, chips, pagination,
   checkboxes in bulk selection, chart legends that act as controls.

### 7.4 3.2.6 Consistent Help — A

1. Inventory help mechanisms: human contact details, contact mechanisms, self-help pages, and
   automated contact (the chat widget, if it is offered as help).
2. On every view where each appears, note its position relative to other page content.
3. **Pass:** each mechanism that appears on multiple pages appears in the same relative order
   (unless the user changed it). If no help mechanism exists, record Not Applicable with that
   reason — 3.2.6 does not require help to exist.

### 7.5 3.3.7 Redundant Entry — A

1. Walk P2, P5, P6, P7 and P8. Note every field a user types into.
2. **Pass:** information already entered or provided in the same process is auto-populated or
   selectable, unless re-entry is essential, required for security, or the earlier value is no
   longer valid. Check Back/Previous in wizards (scan scope wizard, ACR metadata form) and error
   recovery (does a failed save clear the form?).

### 7.6 3.3.8 Accessible Authentication (Minimum) — AA

ACP's sign-in (`SignIn.jsx`) hands off to identity providers: **Sign in with Microsoft** (Entra via
MSAL) and a Google sign-in button (Google Identity Services). The password, MFA and consent
screens are the provider's. A build whose `/config` resolves `auth: 'demo'` shows demo personas
instead; that is not a production authentication path — record which mode the build under test
used.

1. Walk P1 with a password manager and with paste. **Pass:** ACP's own screens contain no cognitive
   function test; every credential step can be completed by paste or autofill, or by an
   alternative method.
2. Record the provider flow observed (tenant configuration determines it) and whether paste and
   autofill worked on each provider screen.
3. **Scope note:** provider pages are third-party content. Report them separately — what ACP
   controls, what the tenant's configuration controls, and what the provider controls — and do not
   claim "Supports" for provider pages without having tested the configured flow.

## 8. How to record results

1. Create or open the WCAG 2.2 edition report in the Conformance tab for the build under test.
2. For each criterion, run the matching plan from `config/acr-manual-test-plans.json` and fill the
   section-4 fields.
3. Attach automated runs as automated evidence (they stay partial).
4. Decide a status only when every applicable process and state in section 3 has a result.
   Where evidence is missing, the honest state is **needs verification** (the workspace's internal
   `needs_review` / `not_evaluated`), not "Supports" and not "Does Not Support".
5. Re-test on the deployed build after every fix; a fix merged is not a fix verified until
   `/healthz` shows a commit that contains it (`git merge-base --is-ancestor <fix> <live>`).

## 9. Review of the September 16, 2026 self-assessment

The vendor ACR (VPAT 2.5Rev WCAG edition) for Movate AccessOps dated September 16, 2026 reports a
self-assessment performed September 15–16, 2026 with Accessibility Insights for Web 2.49.0
(axe-core 4.11.3) on Chrome 153, covering "primary workflow views (discover, assess, remediate,
analytics)". It reports one defect, under 1.4.3.

**Recommendation.** Where the table below says evidence is still required, report the criterion as
**needs verification** in the working record and do not publish a "Supports" for it until the
evidence exists. This review does not assert that any of these criteria fail, and does not assert
that they pass.

| Criterion | Claim in the report | Why it is not sufficient evidence | Evidence still required |
|---|---|---|---|
| Evaluation Methods | The full Assessment "covers the WCAG 2.1 AA and 2.2 AA success criteria"; "primary workflow views" in Google Chrome | The Assessment is WCAG 2.1 AA-based; 2.2 additions need separate tests. Only primary views in one browser; no AT, AT version, OS, tester or build/commit recorded; states and complete processes not enumerated | Sections 3–5 of this protocol: full view/state list, P1–P8, AT matrix with versions, `/healthz` commit |
| 1.4.3 Contrast (Minimum) | Partially Supports — one instance, the update notification | The defect is real (below), but "all other evaluated text meets 4.5:1" covers only the evaluated views, and the default vs High-contrast palette is not recorded | Section 6.8 in both palettes across all section-3 states. Update banner: retest on the deployed fix (section 10, F1, F2, F5) |
| 2.1.1 Keyboard | Supports — "tab stop analysis and manual keyboard testing" | Tab-stop analysis shows reachability, not operation; primary views only; drawers, dialogs, graph views, resizable separators and admin screens not listed | Section 6.1 for P1–P8 |
| 2.4.3 Focus Order | Supports — focus moves into revealed content and returns | No list of which dialogs/drawers were tested | Section 6.1 step 2 for every item in section 3.2 |
| 2.4.5 Multiple Ways | Supports — "workflow tab navigation and a skip link" | A skip link is a bypass mechanism (2.4.1), not a way to locate a page; tab navigation is one way | Section 6.3: two genuine ways per view, or the process exception stated per view |
| 2.4.11 Focus Not Obscured (Min) | Supports | 2.2 criterion not covered by the Assessment; no procedure recorded; fixed top banner and sticky headers exist | Section 7.1 |
| 2.5.7 Dragging Movements | Not Applicable — "no functions require dragging" | Drag interactions exist in the source (resizable separators, sliders, graph pan) | Section 7.2; N/A only if each has been shown to have a single-pointer alternative or not to be present in the evaluated build |
| 2.5.8 Target Size (Min) | Supports | 2.2 criterion; no measurements recorded; small icon-only targets exist in the source | Section 7.3 with measurements |
| 3.2.6 Consistent Help | Supports — "help access is consistently located" | No inventory of help mechanisms or of the views compared | Section 7.4 |
| 3.3.7 Redundant Entry | Supports | 2.2 criterion; no processes listed | Section 7.5 |
| 3.3.8 Accessible Authentication (Min) | Supports — "evaluated with no failures" | Authentication is delegated to Microsoft/Google; provider flow, tenant configuration and paste/password-manager behaviour not recorded | Section 7.6, with the third-party scope stated |
| 3.3.4 Error Prevention | Supports — "data submissions can be reviewed and corrected" | Does not name the data-changing actions checked (publish/release, approve, delete, overwrite source files) | Section 6.4 per action |
| 1.4.10 Reflow | Supports — "verified in guided review" | No width/zoom recorded; data tables and graphs need their 2D-scroll exceptions named | Section 6.5 |
| 1.4.4 Resize Text | Supports — "up to 200%", zoom not disabled | Browser zoom and text-only zoom are different tests; not stated which | Section 6.6, both methods |
| 1.4.12 Text Spacing | Supports — "no failing elements" | Views and states not stated | Section 6.7 across section 3 |
| 4.1.2 Name, Role, Value | Supports — the custom tab interface "implements the ARIA Tabs pattern with correct roles, states (aria-expanded)" | `aria-expanded` is the disclosure state; tabs convey selection with `aria-selected`, and whether the header items are tabs or navigation at all is a separate question (section 6.2). No screen reader output recorded | Section 6.2 with each AT in section 5; see the navigation facts below |
| 4.1.3 Status Messages | Supports — verified that the update notification uses `aria-live="polite"` | One region's markup does not show that any message is announced, nor that the many other status messages are | Section 6.10, per message, per AT |

**Navigation semantics (DOM facts).** These come from jsdom tests on the evaluation branch, not
from assistive technology:

- "Correct roles": true for the header workflow navigation (tablist / tab / tabpanel, section 6.2).
- "States (aria-expanded)": false. Selection is `aria-selected`; no tab carries `aria-expanded`.
- "Keyboard interaction": true for the header navigation (one Tab stop, arrows, Home/End). It was
  **not** true for four in-page tab sets — Settings, the source drawer, Live Operations flow view
  and the waterfall drawer (every tab a Tab stop, no arrow keys, no tab panel, or `aria-controls`
  naming panels that were not rendered). These are fixed on the evaluation branch and pinned by
  `subTabSemantics.test.jsx` and `liveOpsFlowTabsSemantics.test.jsx`; not yet deployed or verified
  with AT. The Review workspace tabs (`RemediationInbox.jsx`) show the same gaps from source
  reading and were not changed.
- Reproduced in `workflowNavigationSemantics.test.jsx`, then fixed on the evaluation branch (not yet
  deployed or verified with AT): when a user was on a view their role no longer permits, no header
  tab was in the Tab order and the panel's `aria-labelledby` named a tab that was not rendered
  (`App.jsx`). Now the first rendered, unlocked tab takes the Tab stop without being selected,
  `view` does not change and the Access restricted screen stays; the panel is named by
  `aria-label` ("Access restricted: <view>", or "Access pending"). If every rendered tab is locked
  or none is rendered, no tab claims the stop.
- 2.4.5: the skip link (`href="#main-content"`, target focusable) is a 2.4.1 mechanism. The code
  shows no second way (search, site map, addressable URLs) to the same views; section 6.3 decides
  whether that is a failure or the process exception.

**Criteria the report lists as Supports without a recorded method** (1.1.1–3.3.2 and the other AA
rows) are outside this review's specific list, but the same scope gap applies: primary views, one
browser, no AT recorded. Re-establish them under sections 3–5 before the next refresh.

## 10. Known findings

Results computed from source are marked as such; they are not rendered measurements and must be
confirmed in a deployed build.

| ID | Criterion | Where | Finding | Evidence | Status |
|---|---|---|---|---|---|
| F1 | 1.4.3 | Update banner, "A new version of ACP is available." (`VersionToast.jsx`) | White text on `#16a34a` at 14 px / weight 500 | The September 16, 2026 self-assessment reported insufficient contrast here (it gives no ratio). Our own calculation from source at `73743c10` with the WCAG relative-luminance formula: ≈ 3.3:1, below 4.5:1 | **Locally implemented and tested:** on branch `worktree-a11y-feedback-versiontoast` the banner is `#15803d` (white text computes to ≈ 5.0:1), and `versionToast.test.jsx` computes the ratio from the component's inline styles in jsdom (not from rendered pixels) and fails on the old colour. **Pending:** merge, deployment and validation on the deployed build, both an automated re-scan and a manual check (section 8, step 5). Due **2026-09-25** |
| F2 | 1.4.3 / 1.4.11 | Same banner, dismiss "✕" button | `rgba(255,255,255,.8)` glyph on `#16a34a` | Computed from source at `73743c10`: ≈ 2.6:1 — below 3:1. Not reported by the self-assessment; not yet confirmed rendered | Same branch as F1: glyph is now solid white on `#15803d` (≈ 5.0:1, computed). Needs verification on the deployed build |
| F5 | 1.4.11 / 2.4.7 | Same banner, focus indicator on its two buttons | Shared `:focus-visible` outline (`#7a5c8e` by default, `#005fcc` in High-contrast) drawn on the green banner | Computed from source at `73743c10`: ≈ 1.7:1 and ≈ 1.8:1 against the banner — below 3:1. Not reported by the self-assessment | Same branch as F1: `version-toast.css` gives the banner's controls a white 2 px outline (≈ 5.0:1 on `#15803d`, computed). Needs a visual check in both palettes on the deployed build |
| F3 | 2.5.7, 2.5.8 (candidates) | Review workspace resizable separators (`RemediationInbox.jsx` `Divider`) | Pointer-drag resize with arrow-key alternative; 7 px wide | Source reading only. Whether it renders in the live Review view, and whether any single-pointer alternative exists elsewhere, not yet checked | Needs verification (sections 7.2, 7.3) |
| F4 | Evidence gap | Automated UI audits (`wcag-a11y.spec.js`, `wcagAxeMatrix.test.jsx`) | Run with the High-contrast palette forced on; default palette not audited by them; `wcag22aa` axe rules not run | Source reading | Open — evidence gap, not a conformance finding |

| F6 | 1.4.10, 2.5.8 | Same banner at narrow widths | The absolutely positioned "✕" sat over the centred message and "Reload now" below ≈ 440 CSS px (16 px side padding, no wrap); the "✕" target was ≈ 22 px tall | Reasoned from source CSS (jsdom has no layout) | Same branch as F1: 52 px side padding reserved for the "✕", the row wraps, "✕" is 28×28 and "Reload now" at least 24 px tall; pinned by `versionToast.test.jsx`. Needs a browser check at 320 CSS px, ≈ 440 px and 200 % text. Wrapping makes the fixed banner taller on narrow screens, so include it in the 2.4.11 check (section 7.1) |

The Reload button on the same banner (`#15803d` on `#fff`, 13 px / 650) computes to ≈ 5.0:1; listed
here only so that nobody re-measures it as part of F1.
