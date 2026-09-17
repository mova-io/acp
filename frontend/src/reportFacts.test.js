/**
 * The report is built from the SERVER's facts, and every preview says which version it is.
 *
 * These cover the four claims that the independent review found unsupported in the first pass:
 *   1. the report data comes from GET .../report-facts and carries its `factsDigest`, so the
 *      render route can refuse a report whose evidence has moved on;
 *   2. saved changes include the ones the AI applied and NOTHING re-checked, under the server's
 *      own ids (the client cannot construct the `u<16hex>` form);
 *   3. a preview is labelled "Original"/"Corrected copy" only when it was fetched BY DIGEST, and
 *      "version not verified" when it came from the ambiguous page route — never "after";
 *   4. the scan facts index is PAGINATED and is read to the end, or says exactly what is missing.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('./api.js', () => ({ SIM: false, fetchChangeReviews: async () => ({}), putChangeReview: async () => ({}) }))
const {
  buildFileReportData, collectPreviews, loadScanReportFacts, comparisonFromFacts,
  scanComparisonFromFacts, savedChangeToDiff, PREVIEW_UNVERIFIED_CAPTION,
} = await import('./fileReportData.js')

const SRC = 's'.repeat(64)
const COR = 'c'.repeat(64)
const png = new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' })

const file = {
  file: 'guide.pdf',
  score: 70,
  issues: [{ wcag: 'SC_1_1_1', rule_id: 'img-alt', severity: 'SERIOUS', detail: 'Image 1 has no alt', page: 2 }],
}
const rows = [{ id: '1.1.1', name: 'Non-text Content', outcome: 'FIXED', count: 1 }]

// Exactly the per-file shape stream D builds (checked field-for-field against its real sample
// payload, /tmp/acp-report-facts-sample.json, on 2026-09-17).
const FACTS = {
  factsVersion: 1,
  factsDigest: 'digest-abc123',
  generatedAt: '2026-09-17T12:00:00Z',
  identity: {
    scanId: 's1', file: 'guide.pdf', sourceChecksum: SRC, sourceChecksumKind: 'sha256',
    sourceSha256: SRC, correctedSha256: COR, currentArtifact: { kind: 'corrected', sha256: COR },
    remediatedAt: '2026-09-16T09:00:00Z', platformVersion: '2026.9.17.1', targetLevel: 'AA',
    scopeDigest: 'scope-1', scanScope: null, rubricHash: null,
  },
  assessment: { state: 'assessed', assessedAt: '2026-09-15T00:00:00Z', score: 70, engine: 'pdf', artifactAssessed: 'source', findingsComplete: true, findingsTotal: 1, stateReason: null },
  findings: [{ id: 'f-server-1', ledgerFindingId: null, ruleId: 'img-alt', sc: '1.1.1', detail: 'Image 1 has no alt', severity: 'SERIOUS', recommendedAction: 'Describe the barn', location: { label: 'Page 2', page: 2, slide: null, sheet: null, cell: null, element: null }, state: 'open', stateReason: null }],
  savedChanges: [
    { id: 'guide.pdf::1.1.1::0', ruleId: '1.1.1', sc: '1.1.1', seq: 0, locator: null, before: '', after: 'A red barn', note: 'vision', verification: 'verified', verificationDetail: 'cleared the re-scan', artifactSha256: COR, valueClipped: false, changeDigest: 'cd-verified', findingIds: ['f-server-1'], source: 'remediation_diff' },
    { id: 'guide.pdf::1.3.1::u0123456789abcdef', ruleId: '1.3.1', sc: '1.3.1', seq: null, locator: 'p#3', before: 'x', after: 'y', note: null, verification: 'not_verified', verificationDetail: 'applied to the saved copy; no re-scan has confirmed it', artifactSha256: COR, valueClipped: true, changeDigest: 'cd-unverified', findingIds: null, source: 'unverified_changes' },
  ],
  savedChangesComplete: true, savedChangesTotal: 2, savedChangesLimit: 500,
  reviews: {},
  accounting: { findingsTotal: 1, findingsOpen: 1, findingsResolvedVerified: null, resolutionLedger: 'none', savedChangesVerified: 1, savedChangesUnverified: 1, humanReviews: { pending: 2, accepted: 0, correctionRequested: 0, rejected: 0, unable: 0, stale: 0 } },
  previous: null,
  previousReason: 'No earlier assessment of this document is recorded.',
  limits: { valueMaxChars: 2000, savedChangesLimit: 500 },
}

const deps = (over = {}) => ({
  getConfig: async () => ({ version: '2026.9.17.1' }),
  getFileReportFacts: vi.fn(async () => FACTS),
  getFileRemediationDiffs: vi.fn(async () => []),
  getDecisions: async () => ({}),
  getFilePage: vi.fn(async () => png),
  getFileArtifactPage: vi.fn(async (_s, _f, sha) => (sha === SRC || sha === COR ? png : null)),
  getScan: async () => ({ run: {}, files: [] }),
  getScanDiff: async () => ({ no_baseline: true }),
  loadReviews: async () => ({ ok: true, sim: false, artifact: {}, reviews: {} }),
  ...over,
})

describe('the report is built from server facts', () => {
  it('fetches the facts and carries the factsDigest that binds the render', async () => {
    const dp = deps()
    const d = await buildFileReportData({ file, scanId: 's1', mode: 'reviewer', rows, targetLevel: 'AA', deps: dp })
    expect(dp.getFileReportFacts).toHaveBeenCalledWith('s1', 'guide.pdf')
    expect(d.facts).toBe(FACTS)
    expect(d.factsDigest).toBe('digest-abc123')
    expect(d.identity.factsDigest).toBe('digest-abc123')
    expect(d.identity.currentArtifact).toEqual({ kind: 'corrected', sha256: COR })
    expect(d.identity.sourceChecksumKind).toBe('sha256')
    // The store's own clip length, not a client constant.
    expect(d.diffValueCap).toBe(2000)
    // The verified-only route is not consulted at all when the facts answered.
    expect(dp.getFileRemediationDiffs).not.toHaveBeenCalled()
  })

  it('includes applied-but-unverified changes, under the SERVER id, marked not verified', async () => {
    const d = await buildFileReportData({ file, scanId: 's1', mode: 'full', rows, targetLevel: 'AA', deps: deps() })
    expect(d.diffs).toHaveLength(2)
    const unverified = d.diffs.find((x) => x.verification === 'not_verified')
    // `u<16hex>` cannot be derived client-side (its seq is null), so it must be carried verbatim.
    expect(unverified.id).toBe('guide.pdf::1.3.1::u0123456789abcdef')
    expect(unverified.verified).toBe(false)
    expect(unverified.valueClipped).toBe(true)
    expect(unverified.changeDigest).toBe('cd-unverified')
    expect(d.savedChanges).toHaveLength(2)
  })

  it('a WIRING mistake is named as one, not reported as a failed read', async () => {
    // "The evidence could not be read" sends the reader to check their connection and their
    // permissions. An undefined dependency is a bug here and nothing they do will change it.
    const missing = await buildFileReportData({
      file, scanId: 's1', mode: 'full', rows, targetLevel: 'AA',
      deps: deps({ getFileReportFacts: undefined }),
    })
    expect(missing.factsError).toMatch(/problem in this application/)
    expect(missing.factsError).not.toMatch(/could not be read/)
    const thrown = await buildFileReportData({
      file, scanId: 's1', mode: 'full', rows, targetLevel: 'AA',
      deps: deps({ getFileReportFacts: async () => { throw new TypeError('getFileReportFacts is not a function') } }),
    })
    expect(thrown.factsError).toMatch(/problem in this application/)
  })

  it('a facts read that fails is a stated gap, not a quietly client-built report', async () => {
    const d = await buildFileReportData({
      file, scanId: 's1', mode: 'full', rows, targetLevel: 'AA',
      deps: deps({ getFileReportFacts: async () => { throw new Error('503') } }),
    })
    expect(d.facts).toBeNull()
    expect(d.factsDigest).toBeNull()
    expect(d.identity.factsDigest).toBeNull()
    expect(d.factsError).toMatch(/could not be read/)
    expect(d.factsError).toMatch(/503/)
  })
})

describe('comparison against a real baseline', () => {
  it('passes the server baseline and scope through when both sides are server-sourced', () => {
    const withPrev = {
      ...FACTS,
      previous: { scanId: 's0', generatedAt: '2026-09-01T00:00:00Z', sha256: SRC, scopeDigest: 'scope-1', findings: [{ id: 'f-server-1', ruleId: 'img-alt', sc: '1.1.1', detail: 'Image 1 has no alt', location: { label: 'Page 2', page: 2 } }] },
      previousReason: null,
    }
    const c = comparisonFromFacts(withPrev, 'AA')
    expect(c.previous.scanId).toBe('s0')
    expect(c.previous.scope).toEqual({ scopeDigest: 'scope-1', targetLevel: 'AA' })
    expect(c.scope).toEqual({ scopeDigest: 'scope-1', targetLevel: 'AA' })
    expect(c.currentFindings.map((f) => f.id)).toEqual(['f-server-1'])
    expect(c.previousReason).toBeNull()
  })

  it('no baseline reads unknown with the server’s own reason', () => {
    const c = comparisonFromFacts(FACTS, 'AA')
    expect(c.previous).toBeNull()
    expect(c.previousReason).toBe('No earlier assessment of this document is recorded.')
  })

  it('at ESTATE level a baseline is refused unless the current findings share its id scheme', () => {
    // buildComparison matches by id. Server ids on one side and client content-hash ids on the
    // other means nothing matches: every earlier finding reads "resolved" and every current one
    // "introduced". Unknown-with-a-reason is the only honest answer.
    const scanFacts = { factsDigest: 'd', previous: { scanId: 's0', findings: [{ id: 'f-server-1' }] }, previousReason: null, files: [] }
    const c = scanComparisonFromFacts(scanFacts, 'AA')
    expect(c.previous).toBeNull()
    expect(c.previousReason).toMatch(/not available at estate level/)
    expect(c.previousReason).toMatch(/never subtracted/)
  })
})

describe('preview provenance', () => {
  const pdf = { file: 'guide.pdf', issues: [{ page: 2 }] }

  it('labels a preview original or corrected only when it was fetched BY DIGEST', async () => {
    const getFileArtifactPage = vi.fn(async (_s, _f, sha) => (sha === SRC || sha === COR ? png : null))
    const getFilePage = vi.fn(async () => png)
    const { previews, previewPairs, status } = await collectPreviews({
      scanId: 's1', file: pdf, diffs: [], facts: FACTS, getFilePage, getFileArtifactPage,
    })
    expect(getFileArtifactPage.mock.calls.map((c) => [c[2], c[3]])).toEqual(
      expect.arrayContaining([[SRC, 2], [COR, 2]]),
    )
    expect(previewPairs[2].original).toMatchObject({ provenance: 'original', sha256: SRC })
    expect(previewPairs[2].corrected).toMatchObject({ provenance: 'corrected', sha256: COR })
    expect(previews[2].caption).toBe(`Corrected copy, page 2 (sha ${COR.slice(0, 12)})`)
    expect(previewPairs[2].original.caption).toBe(`Original document, page 2 (sha ${SRC.slice(0, 12)})`)
    expect(status.original).toEqual([2])
    expect(status.corrected).toEqual([2])
    expect(status.unverified).toEqual([])
    // The ambiguous route is not consulted when exact bytes answered.
    expect(getFilePage).not.toHaveBeenCalled()
    // Nothing anywhere in this output says "after".
    const all = JSON.stringify({ previews, previewPairs, status })
    expect(all).not.toMatch(/after the edit/i)
    expect(all).not.toMatch(/"caption":"After/i)
  })

  it('a preview from the ambiguous page route says the version is not verified', async () => {
    const noDigests = { ...FACTS, identity: { ...FACTS.identity, sourceSha256: null, correctedSha256: null } }
    const getFileArtifactPage = vi.fn(async () => png)
    const { previews, status } = await collectPreviews({
      scanId: 's1', file: pdf, diffs: [], facts: noDigests,
      getFilePage: async () => png, getFileArtifactPage,
    })
    // With no recorded digest there is nothing to ask the exact-bytes route FOR.
    expect(getFileArtifactPage).not.toHaveBeenCalled()
    expect(previews[2].provenance).toBe('unverified')
    expect(previews[2].caption).toContain(PREVIEW_UNVERIFIED_CAPTION)
    expect(previews[2].note).toMatch(/not evidence of what changed/)
    expect(status.unverified).toEqual([2])
    expect(status.provenanceNote).toMatch(/could not be tied to a recorded document version/)
  })

  it('no preview at all is said out loud, never implied by an empty map', async () => {
    const { previews, status } = await collectPreviews({
      scanId: 's1', file: pdf, diffs: [], facts: FACTS,
      getFilePage: async () => null, getFileArtifactPage: async () => null,
    })
    expect(previews).toEqual({})
    expect(status.unavailable).toEqual([2])
    expect(status.reason).toMatch(/No preview could be produced for page 2/)
  })
})

describe('the paginated scan facts index', () => {
  const pageOf = (offset, limit, total) => ({
    factsVersion: 1, factsDigest: 'scan-digest', filesTotal: total, offset, limit,
    complete: offset + limit >= total,
    files: Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) => ({ file: `doc-${offset + i}.pdf`, score: 80, findingsOpen: 1, savedChangesVerified: 0, savedChangesUnverified: 0 })),
    previous: null, previousReason: 'none',
  })

  it('reads every page of a big scan, and reports progress as it goes', async () => {
    const getScanReportFacts = vi.fn(async (_s, { offset, limit }) => pageOf(offset, limit, 450))
    const seen = []
    const got = await loadScanReportFacts('s1', { getScanReportFacts, limit: 200, onProgress: (p) => seen.push(p.loaded) })
    expect(getScanReportFacts.mock.calls.map((c) => c[1].offset)).toEqual([0, 200, 400])
    expect(got.files).toHaveLength(450)
    expect(got.complete).toBe(true)
    expect(got.incompleteReason).toBeNull()
    expect(got.facts.factsDigest).toBe('scan-digest')
    expect(seen[seen.length - 1]).toBe(450)
  })

  it('names precisely what is missing when a page cannot be read — never a silent cap', async () => {
    const getScanReportFacts = vi.fn(async (_s, { offset, limit }) => {
      if (offset >= 200) throw new Error('504 gateway timeout')
      return pageOf(offset, limit, 450)
    })
    const got = await loadScanReportFacts('s1', { getScanReportFacts, limit: 200 })
    expect(got.files).toHaveLength(200)
    expect(got.complete).toBe(false)
    expect(got.incompleteReason).toMatch(/stopped after 200 of 450 documents/)
    expect(got.incompleteReason).toMatch(/504 gateway timeout/)
  })

  it('stops at the page budget and says so rather than looping', async () => {
    const getScanReportFacts = vi.fn(async (_s, { offset, limit }) => pageOf(offset, limit, 100000))
    const got = await loadScanReportFacts('s1', { getScanReportFacts, limit: 10, maxPages: 3 })
    expect(getScanReportFacts).toHaveBeenCalledTimes(3)
    expect(got.complete).toBe(false)
    expect(got.incompleteReason).toMatch(/stopped at 30 of 100000 documents after 3 pages/)
  })
})

describe('savedChangeToDiff', () => {
  it('keeps the server verification wording and never invents "verified"', () => {
    const d = savedChangeToDiff({ id: 'x', ruleId: '1.1.1', verification: 'not_verified', verificationDetail: 'no re-scan' }, 'a.pdf')
    expect(d.verified).toBe(false)
    expect(d.verificationDetail).toBe('no re-scan')
    const unknown = savedChangeToDiff({ id: 'y', ruleId: '1.1.1' }, 'a.pdf')
    expect(unknown.verified).toBeNull()
    expect(unknown.verification).toBe('unknown')
  })
})

// ── The traps in stream D's REAL payload ────────────────────────────────────────────────────
// Checked against its sample on 2026-09-17. Both of these are shapes my fixtures above do not
// have, and both are the ordinary case rather than the exotic one.
describe("the real payload's awkward cases", () => {
  // Drive records md5, SharePoint a quickXorHash. The exact-bytes preview route is addressed BY
  // SHA-256, so for those documents the ORIGINAL simply cannot be requested — and a reader who is
  // not told that will read "Corrected copy" next to no counterpart as "there was nothing before".
  const md5Facts = {
    ...FACTS,
    identity: {
      ...FACTS.identity,
      sourceChecksum: 'b1946ac92492d2347c6235b4d2611184', sourceChecksumKind: 'md5', sourceSha256: null,
    },
  }

  it('says WHY no preview can be shown as the original when the source checksum is not sha-256', async () => {
    const getFileArtifactPage = vi.fn(async (_s, _f, sha) => (sha === COR ? png : null))
    const { previews, previewPairs, status } = await collectPreviews({
      scanId: 's1', file: { file: 'guide.pdf', issues: [{ page: 2 }] }, diffs: [], facts: md5Facts,
      getFilePage: async () => png, getFileArtifactPage,
    })
    // Only the corrected sha is ever asked for; there is no sha-256 for the original to ask with.
    expect(getFileArtifactPage.mock.calls.map((c) => c[2])).toEqual([COR])
    expect(previewPairs[2].original).toBeNull()
    expect(previews[2].provenance).toBe('corrected')
    expect(status.provenanceNote).toMatch(/recorded as md5, not sha-256/)
    expect(status.provenanceNote).toMatch(/no preview is shown as the original/)
  })

  it('carries the md5 source checksum and its KIND, so nothing reads it as a sha-256', async () => {
    const d = await buildFileReportData({
      file, scanId: 's1', mode: 'reviewer', rows, targetLevel: 'AA',
      deps: deps({ getFileReportFacts: async () => md5Facts }),
    })
    expect(d.identity.sourceSha256).toBeNull()
    expect(d.identity.sourceChecksum).toBe('b1946ac92492d2347c6235b4d2611184')
    expect(d.identity.sourceChecksumKind).toBe('md5')
  })

  it("uses the server's re-evaluated decisions verbatim, including staleReason", async () => {
    const reviewed = {
      ...FACTS,
      reviews: {
        'guide.pdf::1.1.1::0': {
          change_id: 'guide.pdf::1.1.1::0', verdict: 'edited', verdictLabel: 'correction requested',
          edited_value: 'The north entrance', note: 'Say which entrance.', reviewer: 'owner@hosp.org',
          at: '2026-09-17T11:30:00+00:00', artifact_sha256: COR, change_digest: 'cd-verified',
          current_change_digest: 'cd-verified', verification: 'verified',
          stale: false, staleReason: 'bound to the current copy and the current change',
        },
      },
    }
    const d = await buildFileReportData({
      file, scanId: 's1', mode: 'reviewer', rows, targetLevel: 'AA',
      deps: deps({ getFileReportFacts: async () => reviewed }),
    })
    const r = d.reviews['guide.pdf::1.1.1::0']
    // The server evaluates staleness inside the transaction that reads the artifact; the client's
    // own reasoning must not overwrite it, and `edited` stays a correction REQUESTED.
    expect(r.stale).toBe(false)
    expect(r.staleReason).toBe('bound to the current copy and the current change')
    expect(r.verdict).toBe('edited')
    expect(d.reviewsError).toBeNull()
  })

  it('at estate level D reports the comparison per document, and that reason is passed through', () => {
    const c = scanComparisonFromFacts({
      factsDigest: 'd', identity: { scopeDigest: 'scope-1' }, files: [],
      previous: null, previousReason: 'scan-level comparison is reported per document',
    }, 'AA')
    expect(c.previous).toBeNull()
    expect(c.previousReason).toBe('scan-level comparison is reported per document')
    expect(c.scope).toEqual({ scopeDigest: 'scope-1', targetLevel: 'AA' })
  })
})

describe('the exact-bytes preview is checked against the bytes it returned', () => {
  const pdf = { file: 'guide.pdf', issues: [{ page: 2 }] }
  const pngFor = (sha) => ({ blob: png, sha256: sha })

  it('confirms the caption\u2019s digest against X-ACP-Artifact-Sha256', async () => {
    const { previews, previewPairs, status } = await collectPreviews({
      scanId: 's1', file: pdf, diffs: [], facts: FACTS,
      getFilePage: async () => png,
      getFileArtifactPage: async (_s, _f, sha) => pngFor(sha),
    })
    expect(previewPairs[2].original.shaConfirmed).toBe(true)
    expect(previews[2].shaConfirmed).toBe(true)
    expect(status.shaConfirmed).toBe(true)
    expect(status.shaMismatch).toEqual([])
  })

  it('REFUSES an image whose served digest is not the one asked for', async () => {
    // A picture labelled with a checksum it does not have is worse than no picture: the label is
    // the whole evidentiary value, and here it would be attached to some other document's page.
    const { previews, status } = await collectPreviews({
      scanId: 's1', file: pdf, diffs: [], facts: FACTS,
      getFilePage: async () => null,
      getFileArtifactPage: async () => pngFor('9'.repeat(64)),
    })
    expect(previews[2]).toBeUndefined()
    expect(status.shaMismatch.map((m) => m.served)).toEqual(['9'.repeat(64), '9'.repeat(64)])
    expect(status.provenanceNote).toMatch(/different document version than the one requested/)
    expect(status.provenanceNote).toMatch(/not shown at all/)
  })

  it('says so when the server did not name the version it rendered', async () => {
    // Cross-origin without Access-Control-Expose-Headers: the label then rests on the request
    // alone. Still usable, but the reader is told it was not confirmed.
    const { previews, status } = await collectPreviews({
      scanId: 's1', file: pdf, diffs: [], facts: FACTS,
      getFilePage: async () => null,
      getFileArtifactPage: async () => ({ blob: png, sha256: null }),
    })
    expect(previews[2].provenance).toBe('corrected')
    expect(previews[2].shaConfirmed).toBe(false)
    expect(status.shaConfirmed).toBe(false)
    expect(status.provenanceNote).toMatch(/did not name the version it rendered/)
  })
})

describe('counts that are NOT RECORDED stay that way', () => {
  it('carries a null findingsOpen with the reason, never a zero', async () => {
    const d = await buildFileReportData({
      file, scanId: 's1', mode: 'reviewer', rows, targetLevel: 'AA',
      deps: deps({
        getFileReportFacts: async () => ({
          ...FACTS,
          assessment: { ...FACTS.assessment, state: 'error', stateReason: 'the analyser did not produce a result' },
          accounting: {
            ...FACTS.accounting, findingsOpen: null,
            accountingReason: 'the document was not assessed, so the number of open findings is not known',
          },
        }),
      }),
    })
    expect(d.accounting.findingsOpen).toBeNull()
    expect(d.accounting.accountingReason).toMatch(/not known/)
    expect(d.assessment.state).toBe('error')
    expect(d.assessment.stateReason).toMatch(/did not produce a result/)
  })
})

describe("the changes awaiting review could not be read", () => {
  const unavailable = {
    ...FACTS,
    savedChanges: [FACTS.savedChanges[0]],          // only the VERIFIED one survived
    savedChangesComplete: false,
    savedChangesUnverifiedSource: 'unavailable',
  }

  it('reads as an incomplete list naming what is missing, not as a complete one', async () => {
    const d = await buildFileReportData({
      file, scanId: 's1', mode: 'reviewer', rows, targetLevel: 'AA',
      deps: deps({ getFileReportFacts: async () => unavailable }),
    })
    expect(d.diffsComplete).toBe(false)
    expect(d.diffsError).toMatch(/changes awaiting review could not be read/)
    expect(d.diffsError).toMatch(/none of them are shown here/)
    expect(d.diffsError).toMatch(/Do not treat this as a complete list/)
    // What DID arrive is still shown — the gap is stated, not used to blank the page.
    expect(d.diffs).toHaveLength(1)
  })
})
