import { expect, it } from 'vitest'
import app from './App.jsx?raw'
import remediate from './Remediate.jsx?raw'

// Remediate explains which unresolved FINDINGS remain only if it can see the server's finding
// ledger for this run (domain_reconciliation.unresolved_findings on the remediate stage snapshot).
// Without the prop the explanation treats findings as unknown — honest, but the "Unresolved
// findings 1" tile then has nothing on the review screen that names what that one finding is.
it('App hands the canonical remediate stage snapshot to Remediate', () => {
  const mount = app.slice(app.indexOf('<Remediate run={run}'))
  expect(mount.slice(0, mount.indexOf('/>'))).toContain('remediationStage={remediationStage}')
  expect(app).toMatch(/const remediationStage = canonicalWorkflowStages\(canonicalRun\.lineage\)\.find\(stage => stage\.stage === 'remediate'\)/)
})

it('Remediate reads findings only from a stage snapshot of the same run', () => {
  expect(remediate).toContain('remediationStage.scan_id === runId')
  expect(remediate).toContain('findingInputsFrom(')
})
