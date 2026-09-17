#!/usr/bin/env node
// Regenerates tests/fixtures/report_render_js_models.json by running the REAL frontend model
// builders over the REAL server facts. Run it from anywhere (paths resolve from this file):
//
//     node tests/fixtures/build_report_render_js_models.mjs
//
// WHY THIS EXISTS. The server renderer's tests used to feed it a model written by hand in the
// test file. That proves the renderer can render the renderer's own idea of a model, and nothing
// at all about the models the product actually produces: a block kind the frontend emits and the
// server does not accept would 422 every download of that report, and a hand-written fixture
// would stay green through it. So the fixture is DERIVED at both ends —
//
//   * the INPUT is api/report_facts.py's own output, captured into
//     tests/fixtures/report_facts_file_sample.json and report_facts_scan_sample.json, not
//     invented here (an invented facts object agrees with whatever the renderer expects);
//   * the MODELS are what frontend/src/reportModel.js and frontend/src/scanReport.js return —
//     the actual modules, imported as ES modules, not a bundled copy.
//
// tests/test_report_render.py::test_the_fixture_matches_the_builders re-runs this script and
// fails when the checked-in JSON has drifted from what the builders now return.
//
// The builders are ESM and read `import.meta.env` (Vite). Rather than bundling, the loader hook
// below substitutes that one expression, so the modules under test are the source files
// themselves — no build step, and no second copy of the code that could go stale.
//
// `adapt()` maps a facts object to the argument the builders take TODAY. When the frontend grows
// its own facts adapter, delete adapt() and call that instead; the cases do not change.

import { registerHooks } from 'node:module'
import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

registerHooks({
  load(url, context, nextLoad) {
    const result = nextLoad(url, context)
    if (!url.endsWith('.js') || url.includes('/node_modules/') || result.source == null) return result
    const source = typeof result.source === 'string' ? result.source : Buffer.from(result.source).toString('utf8')
    if (source.includes('import.meta.env')) {
      result.source = source.replaceAll('import.meta.env', '({ BASE_URL: "/", MODE: "test", VITE_SIM: "false" })')
    }
    return result
  },
})

// FREEZE THE CLOCK. The builders fall back to `new Date().toISOString()` when a caller supplies
// no timestamp — remediationReportModel does exactly that for `identity.generatedAt` even though
// the model it is given carries a generation time — so two runs a millisecond apart produce
// different models and the drift check would be red forever, for no reason anybody could act on.
// Measured, not assumed: without this, six of the 27 cases differed between back-to-back runs.
const FROZEN = new Date('2026-09-17T16:42:54.927Z')
const RealDate = Date
globalThis.Date = class extends RealDate {
  constructor(...args) {
    super(...(args.length ? args : [FROZEN.getTime()]))
  }

  static now() { return FROZEN.getTime() }
}

const HERE = dirname(fileURLToPath(import.meta.url))
const SRC = resolve(HERE, '../../frontend/src')
const OUT = resolve(HERE, 'report_render_js_models.json')

const { buildFileReportModel, buildRemediationModel, remediationReportModel } = await import(`${SRC}/reportModel.js`)
const { buildScanReportModel } = await import(`${SRC}/scanReport.js`)

const MODES = ['summary', 'reviewer', 'full']
const read = (name) => JSON.parse(readFileSync(resolve(HERE, name), 'utf8'))
const clone = (x) => JSON.parse(JSON.stringify(x))

// ── Inputs: the server's own facts ────────────────────────────────────────────────────────────

const FILE_FACTS = read('report_facts_file_sample.json')
const SCAN_FACTS = read('report_facts_scan_sample.json')

const LONG_NAME = 'Shared drive/Regional policy library/2026 refresh/'
  + 'Patient-services-accessibility-and-plain-language-review-final-APPROVED-v14-with-annexes.pdf'

/** The same facts under a 150-character path — the name has to survive in full everywhere. */
function withLongName(facts) {
  const out = clone(facts)
  const old = out.identity.file
  const rename = (s) => (typeof s === 'string' ? s.split(old).join(LONG_NAME) : s)
  out.identity.file = LONG_NAME
  out.savedChanges = out.savedChanges.map((c) => ({ ...c, id: rename(c.id) }))
  out.reviews = Object.fromEntries(Object.entries(out.reviews).map(
    ([k, v]) => [rename(k), { ...v, change_id: rename(v.change_id) }]))
  return out
}

/**
 * Nothing recorded: never assessed, no checksums, no changes, no decisions loaded.
 * `findingsOpen` is null with an accountingReason exactly as the facts module emits it when
 * assessment.state != 'assessed'. Every count in the report must read "Not recorded"; a 0 here
 * would say somebody looked and found nothing.
 */
function withNothingRecorded(facts) {
  const out = clone(facts)
  out.identity.sourceChecksum = null
  out.identity.sourceChecksumKind = null
  out.identity.sourceSha256 = null
  out.identity.correctedSha256 = null
  out.identity.currentArtifact = { kind: 'unknown', sha256: null }
  out.identity.remediatedAt = null
  out.identity.platformVersion = null
  out.assessment = {
    ...out.assessment, state: 'not_assessed', assessedAt: null, score: null, engine: null,
    artifactAssessed: 'unknown', findingsComplete: false, findingsTotal: null,
    stateReason: 'no scan has opened this document',
  }
  out.findings = []
  out.savedChanges = []
  out.savedChangesTotal = null
  out.savedChangesComplete = false
  out.reviews = {}
  out.accounting = {
    ...out.accounting, findingsTotal: null, findingsOpen: null, findingsResolvedVerified: null,
    resolutionLedger: 'none', savedChangesVerified: 0, savedChangesUnverified: 0,
    accountingReason: 'the document has not been assessed, so no finding count can be reported',
    humanReviews: { pending: 0, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 },
  }
  return out
}

/**
 * The saved-change list could not be READ. This is NOT "no changes are outstanding" — the report
 * has to say the list is missing, or a reviewer sees an empty section and concludes there is
 * nothing to review.
 */
function withUnreadableChanges(facts) {
  const out = clone(facts)
  out.savedChanges = []
  out.savedChangesComplete = false
  out.savedChangesTotal = null
  out.savedChangesUnverifiedSource = 'unavailable'
  out.reviews = {}
  return out
}

/** The same shapes, many more of them: the multi-page reviewer/full case. */
function scaledUp(facts, findings = 12, changes = 30) {
  const out = clone(facts)
  const scs = ['1.1.1', '1.4.3', '2.4.4', '1.3.1', '3.1.1', '2.4.2']
  out.findings = Array.from({ length: findings }, (_, i) => {
    const seed = facts.findings[i % facts.findings.length]
    const sc = scs[i % scs.length]
    return {
      ...clone(seed),
      id: `${'0'.repeat(30)}${i}`.slice(-32),
      sc,
      ruleId: seed.ruleId,
      instanceKey: `docx:image:${i}`,
      detail: `${seed.detail} (instance ${i + 1})`,
      location: { ...(seed.location || {}), label: `docx:image:${i}`, page: (i % 9) + 1 },
    }
  })
  out.savedChanges = Array.from({ length: changes }, (_, i) => {
    const seed = facts.savedChanges[i % facts.savedChanges.length]
    const sc = scs[i % scs.length]
    return {
      ...clone(seed),
      id: `${out.identity.file}::${sc}::${i}`,
      sc,
      ruleId: sc,
      seq: i,
      // One in four carries a value at the store's clip length, which the report must describe as
      // clipped-at-record-time and NOT promise in the Full evidence report.
      before: i % 4 === 3 ? 'Original paragraph text recorded before the edit. '.repeat(40) : seed.before,
      after: `${seed.after} (${i + 1})`,
      valueClipped: i % 4 === 3,
      changeDigest: `${'a'.repeat(62)}${(i % 90) + 10}`,
    }
  })
  out.savedChangesTotal = changes
  out.accounting = {
    ...out.accounting,
    findingsTotal: findings,
    savedChangesVerified: out.savedChanges.filter((c) => c.verification === 'verified').length,
    savedChangesUnverified: out.savedChanges.filter((c) => c.verification === 'not_verified').length,
  }
  return out
}

// ── facts → the argument today's builders take ────────────────────────────────────────────────

function adapt(facts, mode) {
  const file = facts.identity.file
  const assessed = facts.assessment.state === 'assessed'
  const bySc = new Map()
  for (const finding of facts.findings) {
    if (!bySc.has(finding.sc)) bySc.set(finding.sc, [])
    bySc.get(finding.sc).push(finding)
  }
  const verifiedScs = new Set(facts.savedChanges.filter((c) => c.verification === 'verified').map((c) => c.sc))
  const rows = []
  if (assessed) {
    for (const [sc, list] of bySc) {
      rows.push({
        id: sc, criterion: sc, outcome: 'FAIL', count: list.length,
        fileIssues: list.map((x) => ({
          id: x.id, ruleId: x.ruleId, detail: x.detail, severity: x.severity,
          location: x.location, recommended_action: x.recommendedAction,
          page: x.location ? x.location.page : null,
        })),
      })
    }
    for (const c of facts.savedChanges) {
      if (bySc.has(c.sc) || rows.some((r) => r.criterion === c.sc)) continue
      rows.push({ id: c.sc, criterion: c.sc, outcome: 'FIXED', count: 0, fileIssues: [], verified: verifiedScs.has(c.sc) })
    }
    rows.push({ id: '1.2.1', criterion: '1.2.1', outcome: 'PASS', count: 0, fileIssues: [] })
    rows.push({ id: '2.4.6', criterion: '2.4.6', outcome: 'UNCHECKED', count: null, fileIssues: [] })
  }
  const changesReadable = facts.savedChangesUnverifiedSource !== 'unavailable' && assessed
  return {
    mode,
    file,
    scanId: facts.identity.scanId,
    targetLevel: facts.identity.targetLevel,
    platformVersion: facts.identity.platformVersion,
    engine: facts.assessment.engine,
    timestamp: facts.generatedAt,
    identity: {
      scanId: facts.identity.scanId, file,
      sourceSha256: facts.identity.sourceSha256, correctedSha256: facts.identity.correctedSha256,
      artifactVersion: null, generatedAt: facts.generatedAt,
      platformVersion: facts.identity.platformVersion, targetLevel: facts.identity.targetLevel,
    },
    artifact: {
      sourceSha256: facts.identity.sourceSha256, correctedSha256: facts.identity.correctedSha256,
      currentSha256: facts.identity.currentArtifact.sha256,
    },
    rows,
    diffs: changesReadable ? facts.savedChanges.map((c) => ({
      rule_id: c.ruleId, seq: c.seq == null ? 0 : c.seq, before: c.before, after: c.after,
      note: c.note, location: c.locator ? { label: c.locator, element: c.locator } : null,
      verification: c.verification, verificationDetail: c.verificationDetail,
    })) : null,
    diffsError: facts.savedChangesUnverifiedSource === 'unavailable'
      ? 'the saved-change list could not be read for this document' : null,
    diffsComplete: changesReadable ? facts.savedChangesComplete : false,
    diffsTotal: changesReadable ? facts.savedChangesTotal : null,
    diffValueCap: facts.limits.valueMaxChars,
    reviews: changesReadable ? facts.reviews : null,
    previous: facts.previous,
    hitl: null,
  }
}

/** Scan facts → buildScanReportModel's argument. */
function adaptScan(facts, mode) {
  const files = (facts.files || []).map((f) => ({
    file: f.file,
    status: f.assessment.state === 'assessed' ? 'done' : f.assessment.state === 'error' ? 'error' : 'not-assessed',
    score: f.score,
    compliant: f.findingsOpen === 0,
    remediated_at: (f.currentArtifact && f.currentArtifact.kind === 'corrected') ? '2026-09-17T09:06:00+00:00' : null,
    issues: Array.from({ length: f.findingsOpen ?? 0 }, (_, i) => ({
      criterion: ['1.1.1', '1.4.3', '2.4.4'][i % 3], severity: 'SERIOUS',
      detail: `Finding ${i + 1} recorded on ${f.file}.`,
    })),
  }))
  const t = facts.totals || {}
  return {
    mode, scanId: facts.identity.scanId, org: 'Northgate Health',
    targetLevel: facts.identity.targetLevel,
    files, totalFiles: facts.filesTotal ?? files.length, analysedFiles: t.assessed ?? null,
    conformantN: files.filter((f) => f.compliant).length,
    avgScore: t.assessed ? 72 : null,
    statusCounts: { 'not-assessed': t.notAssessed ?? 0, unanalysable: t.error ?? 0 },
    hitl: null,
    routing: { eligibleAuto: null },
    execution: {
      documentsWithSavedEdits: files.filter((f) => f.remediated_at).length,
      verifiedFixes: t.savedChangesVerified ?? null,
      verifiedDocuments: files.filter((f) => f.remediated_at).length,
      verifiedComplete: facts.complete !== false,
      publishedDocuments: null,
    },
    generatedAt: facts.generatedAt, platformVersion: facts.identity.platformVersion,
  }
}

function scanScaledUp(facts, count = 40) {
  const out = clone(facts)
  const seeds = facts.files
  out.files = Array.from({ length: count }, (_, i) => {
    const seed = clone(seeds[i % seeds.length])
    seed.file = `Policies/2026/${['Regional', 'National'][i % 2]}/document-${i + 1}.${['docx', 'pdf', 'xlsx'][i % 3]}`
    if (seed.assessment.state === 'assessed') seed.findingsOpen = (i % 4) + 1
    return seed
  })
  out.filesTotal = count
  out.totals = {
    ...out.totals, documents: count,
    assessed: out.files.filter((f) => f.assessment.state === 'assessed').length,
    error: out.files.filter((f) => f.assessment.state === 'error').length,
  }
  return out
}

// ── Cases ─────────────────────────────────────────────────────────────────────────────────────

const cases = {}

const fileCases = {
  base: FILE_FACTS,
  long: withLongName(FILE_FACTS),
  missing: withNothingRecorded(FILE_FACTS),
  unreadable: withUnreadableChanges(FILE_FACTS),
  large: scaledUp(FILE_FACTS),
}
for (const [name, facts] of Object.entries(fileCases)) {
  for (const mode of MODES) cases[`file-${name}-${mode}`] = buildFileReportModel(adapt(facts, mode))
}

const scanCases = {
  base: SCAN_FACTS,
  missing: (() => {
    const out = clone(SCAN_FACTS)
    out.files = out.files.map((f) => ({
      ...f, assessment: { state: 'not_assessed', stateReason: 'no scan has opened this document' },
      score: null, findingsOpen: null, findingsTotal: 0,
      currentArtifact: { kind: 'unknown', sha256: null },
    }))
    out.totals = { ...out.totals, assessed: 0, notAssessed: out.files.length, error: 0, findingsTotal: null }
    return out
  })(),
  large: scanScaledUp(SCAN_FACTS),
}
for (const [name, facts] of Object.entries(scanCases)) {
  for (const mode of MODES) cases[`scan-${name}-${mode}`] = buildScanReportModel(adaptScan(facts, mode))
}

function remediationCase(perFile) {
  const files = []
  const diffsByFile = {}
  const appliedFixes = []
  const reviewsByFile = {}
  const currentShaByFile = {}
  for (const [i, facts] of perFile.entries()) {
    const file = facts.identity.file
    files.push({ file, remediated_at: facts.identity.remediatedAt })
    diffsByFile[file] = facts.savedChanges.map((c) => ({
      rule_id: c.ruleId, seq: c.seq == null ? 0 : c.seq, before: c.before, after: c.after,
      note: c.note, verification: c.verification, verificationDetail: c.verificationDetail,
    }))
    reviewsByFile[file] = facts.reviews
    currentShaByFile[file] = facts.identity.currentArtifact.sha256
    appliedFixes.push({ file, rule_id: facts.savedChanges[0] ? facts.savedChanges[0].ruleId : '1.1.1',
      created_at: `2026-09-1${(i % 8) + 1}T08:00:00Z` })
  }
  const base = buildRemediationModel({
    files, diffsByFile, appliedFixes, level: 'AA', org: 'Northgate Health',
    scanId: SCAN_FACTS.identity.scanId, generatedAt: FILE_FACTS.generatedAt,
    diffsComplete: true, platformVersion: FILE_FACTS.identity.platformVersion,
    diffsTotal: Object.values(diffsByFile).reduce((n, d) => n + d.length, 0),
  })
  return { base, reviewsByFile, currentShaByFile }
}

const renamed = (n) => {
  const out = clone(FILE_FACTS)
  const old = out.identity.file
  const next = `Policies/2026/Handbook-${n}.docx`
  out.identity.file = next
  out.savedChanges = out.savedChanges.map((c) => ({ ...c, id: c.id.split(old).join(next) }))
  out.reviews = Object.fromEntries(Object.entries(out.reviews).map(([k, v]) => [k.split(old).join(next), v]))
  return out
}

const remediationCases = {
  base: remediationCase([FILE_FACTS, renamed(2)]),
  large: remediationCase(Array.from({ length: 6 }, (_, i) => scaledUp(renamed(i + 1), 4, 5))),
}
for (const [name, spec] of Object.entries(remediationCases)) {
  for (const mode of MODES) {
    cases[`remediation-${name}-${mode}`] = remediationReportModel(spec.base, {
      mode, reviewsByFile: spec.reviewsByFile, currentShaByFile: spec.currentShaByFile,
    })
  }
}

const sorted = {}
for (const key of Object.keys(cases).sort()) sorted[key] = cases[key]
writeFileSync(OUT, `${JSON.stringify(sorted, null, 1)}\n`)
process.stderr.write(`wrote ${Object.keys(sorted).length} models to ${OUT}\n`)
