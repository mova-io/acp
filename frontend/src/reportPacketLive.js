// The LIVE wiring of the per-file packet export: api.js transport, the document drawer's own
// coverage-row derivation, the real model builder and the server renderer. Overview's Reports menu
// and the Assess RuleBreakdown menu both call runScanPacketExport; everything it does is in
// reportPacketExport.js, which tests drive with recorded server responses.
import JSZip from 'jszip'
import {
  getScanReportFacts, getFileReportFacts, getScan, getRules, getRubric, getCapability, getConfig,
  getDecisions, getFileRemediationState, listDispositions, getFileRemediationDiffs,
  getFileArtifactPage, getFilePage, getScanDiff,
} from './api.js'
import { computeCoverageRows } from './FileDrawer.jsx'
import { buildFileReportData, loadScanReportFacts } from './fileReportData.js'
import { buildFileReportModel } from './reportModel.js'
import { renderReportBlob, downloadBlob } from './reportRenderClient.js'
import { isDispositionable, normalizeDisposition } from './disposition.js'
import { statusOf } from './docStatus.js'
import { SCOPE_SCS } from './activeScope.js'
import { scOf } from './BeforeAfter.jsx'
import { CAPABILITY_FALLBACK } from './capability.js'
import * as evidenceLink from './evidenceLink.js'
import { exportScanPackets, makeFileDataBuilder } from './reportPacketExport.js'

const scOfWcag = (v) => ((v || '').replace(/^SC_/, '').replace(/_/g, '.').match(/^\d+\.\d+\.\d+/) || [''])[0]

/**
 * "Open in ACP" for one document of the index: an ABSOLUTE link on this app's own origin, or text.
 * A per-document link needs a document-level evidence route (evidenceLink.fileEvidenceHref); a
 * finding-level link would pick one finding to stand for the document, which is the "first match"
 * guess the evidence viewer refuses to make — so without that route the index says so instead.
 */
export function appLinkFor(scanId, file, { origin = typeof window !== 'undefined' ? window.location?.origin : null, links = evidenceLink } = {}) {
  const trusted = typeof links.trustedAppOrigin === 'function' ? links.trustedAppOrigin(origin) : null
  if (!trusted) return { href: null, note: 'not linked: this export was made without a trusted ACP web address' }
  if (typeof links.fileEvidenceHref !== 'function') return { href: null, note: `open scan ${scanId} in ACP and select this document (no document link could be built)` }
  const rel = links.fileEvidenceHref({ scanId, file })
  const href = rel ? links.absoluteAppHref(rel, { origin }) : null
  return href ? { href, note: null } : { href: null, note: 'not linked: no document link could be built for this name' }
}

export async function runScanPacketExport({ scanId, mode, files = [], signal = null, onProgress = null, aiEnabled = true, origin } = {}) {
  // Exactly the transport calls FileDrawer makes for its report — named, not the whole module.
  const api = {
    getScanReportFacts, getFileReportFacts, getScan, getRules, getRubric, getCapability, getConfig,
    getDecisions, getFileRemediationState, listDispositions, getFileRemediationDiffs,
    getFileArtifactPage, getFilePage, getScanDiff,
  }
  const buildFileData = makeFileDataBuilder({
    scanId, api, computeCoverageRows, buildFileReportData, isDispositionable, normalizeDisposition,
    scOf, inScope: (i) => SCOPE_SCS.has(scOfWcag(i.wcag)), capabilityFallback: CAPABILITY_FALLBACK, aiEnabled,
  })
  return exportScanPackets({
    scanId, mode, files, signal, onProgress,
    deps: {
      loadIndex: ({ onProgress: p }) => loadScanReportFacts(scanId, { getScanReportFacts: api.getScanReportFacts, onProgress: p }),
      // The ONE final fresh read (contract 7): same transport, offset 0, limit 1, no digest.
      getScanReportFacts: api.getScanReportFacts,
      loadScanFiles: async () => (await api.getScan(scanId))?.files || [],
      buildFileData,
      buildModel: buildFileReportModel,
      renderBlob: renderReportBlob,
      download: downloadBlob,
      JSZip,
      appLink: (file) => appLinkFor(scanId, file, origin !== undefined ? { origin } : {}),
      statusOf,
    },
  })
}
