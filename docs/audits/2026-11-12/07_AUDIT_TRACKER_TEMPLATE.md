# Audit artifact templates

Use stable IDs and link files; extend these formats when evidence needs it.

## Finding

### MA-001: concrete failure title

- Type: confirmed bug / architectural risk / evidence gap / missing capability / hypothesis.
- Severity / confidence: P0–P3 / high–medium–low.
- Affected modes: demo / one agency / shared multiple agencies / isolated deployments.
- Snapshot: commit + working-tree differences or archive hashes.
- Contract and source: expected invariant and dated decision/spec/code basis.
- Reachability and preconditions:
- Trigger / interleaving:
- Actual vs expected outcome:
- Source chain: file:line -> caller / transaction / side effect -> file:line.
- Impact and bounds:
- Reproduction: synthetic setup, command/test and exact observed result.
- Validation: reproduced / static proof / not run; environment, exit code, skips.
- Existing coverage and gap:
- Mitigations checked / falsification result:
- Minimal fix / regression test / compatibility risks:
- Related known issue IDs:

## Coverage CSV

```csv
path,subsystem,status,depth,evidence,related_tests,limitations
app/web.py,ingress,unread,,,,
```

Use unread/reviewed/traced/tested consistently. Group test files only with an
explicit file list; do not silently classify all tests as reviewed after running pytest.

## Test evidence

| Finding/contract | Snapshot | Isolated environment | Command | Result/skips | Evidence file | Limit |
|---|---|---|---|---|---|---|

## Readiness

| Capability | Operating mode | Evidence | Status | Blocker/unknown | Minimal acceptance check |
|---|---|---|---|---|---|

## Checkpoint

- Snapshot and working tree:
- Phase / completed areas:
- Confirmed findings / open hypotheses:
- Actual tests and limitations:
- Unread/unvalidated scope:
- Next exact action:
- Questions requiring owner evidence:
- Artifact paths:
