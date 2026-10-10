# Archive Report: agent-qa-foundation-vnc

**Change**: agent-qa-foundation-vnc
**Archived**: 2026-10-10
**Store mode**: hybrid (Engram + openspec filesystem)
**Branch / HEAD at close**: feat/vnc-screen at 135513b (verified candidate d0896bb)
**Archive verdict**: intentional-with-warnings: none. Closed with accepted non-blocking warnings only (see Warnings).

## Final State at Close

Final-state facts rank above intermediate snapshots (per the sdd-archive Final-State Authority). The launch prompt is the authority for post-verify facts below.

- **Requirements**: 22/22 compliant; **scenarios**: 54/54 compliant. Source: `verify-report` (#1179) at evidence sha256:c98f6993791e6366d5313396fe983a6bc3ad09dcb3cf22f76b6eb175acb29ad4, run on d0896bb. Carried forward by the launch prompt.
- **Tests**: 181 passed, 0 failed (`uv run pytest -q`, exit 0). Also passes with `-W error::SyntaxWarning`. Launch prompt states 181 still pass on 135513b, which renamed test fixtures only. Not re-run during archive.
- **Tasks**: 23/23 complete in `tasks.md` (1.1-1.10, 2.1-2.6, 3.1-3.7). No unchecked implementation tasks; no stale-checkbox reconciliation was needed.
- **Production code**: no diff between d0896bb and 135513b in `agent/` or `dev_tools/` (launch prompt).
- **Post-verify changes**:
  - Commit 135513b renamed the password-named test constant to `AUTH_FIXTURE="mock-auth-fixture"` in tests (GitGuardian-driven).
  - Upstream rewrote fixtures in 27d6728, replacing password-like placeholders in `feat/vnc-client`. PR #3 was rebased onto that.
- **Delivery** (launch prompt, not independently re-verified by archive): three stacked PRs, all MERGEABLE/CLEAN with GitGuardian passing:
  - #1 feat/foundation -> main (c4fed14)
  - #2 feat/vnc-client -> feat/foundation (27d6728)
  - #3 feat/vnc-screen -> feat/vnc-client (0e02d9d, 373468c, 0ee64b0, 135513b)
- **Real-desktop smoke** (task 3.7, historical): `record_screens` saved a correct 1280x800 SIGECOM capture. The app showed "Hoy: 14/10/2026" against a system date of 10/10/2026. That is a property of the target app under test, not of this change.

## Observation Traceability (Engram, project `agent-qa`)

| Artifact | Topic key | Observation ID | Role at close |
|---|---|---|---|
| Proposal | `sdd/agent-qa-foundation-vnc/proposal` | #1171 | Scope and intent. Current |
| Spec (7 delta specs) | `sdd/agent-qa-foundation-vnc/spec` | #1172 | Merged to main specs (see below) |
| Design | `sdd/agent-qa-foundation-vnc/design` | #1173 | Architecture as built. Current |
| Tasks | `sdd/agent-qa-foundation-vnc/tasks` | #1174 | Completion source, rank 2 |
| Verify report | `sdd/agent-qa-foundation-vnc/verify-report` | #1179 | Snapshot at d0896bb. Historical |
| Review transaction/ledger/receipt/gate-context | `sdd/agent-qa-foundation-vnc/review/*` | none found | Not applicable: review mode disabled |

Apply-progress was not retrieved by this archive run. The launch prompt's final-state facts cover the post-apply state.

## Specs Synced

No main specs existed before this archive. The 7 delta specs are full specs, so each was copied verbatim to `openspec/specs/<capability>/spec.md`. No destructive merge occurred, so no pre-merge warning was required by `openspec/config.yaml` `rules.archive`.

| Capability | Action | Requirements |
|---|---|---|
| runtime-config | Created | 2 |
| core-contracts | Created | 4 |
| secret-safe-logging | Created | 2 |
| health-endpoint | Created | 1 |
| vnc-client | Created | 5 |
| vnc-screen | Created | 5 |
| vnc-test-harness | Created | 3 |

Total: 22 requirements in the main specs, matching the verify-report 22/22. Scenario total is 54, matching 54/54.

## Archive Contents

Archived folder: `openspec/changes/archive/2026-10-10-agent-qa-foundation-vnc/`

- `proposal.md`
- `design.md`
- `tasks.md`
- `verify-report.md` (previously untracked; now inside the archive)
- `specs/` (7 delta specs)
- `archive-report.md` (this file)

## Gates

- **Native review receipt gate**: review mode is globally OFF, so `reviewGate.delivery: disabled/unmanaged` applies. No review topics exist in Engram. Nothing manufactured.
- **Task completion gate**: passed. 23/23 tasks checked.
- **CRITICAL findings**: none. The archive would have been blocked otherwise.
- **Action context**: repo-local. All writes stayed within `openspec/`.

## Warnings and Open Items (carried forward, all non-blocking)

- **W4**: `MockVncServer(version="3.8")` takes a str rather than bytes. Accepted.
- **W5**: PR #3 (PR2b) exceeds the 400/800-line budget. Accepted in the launch prompt.
- **W8**: Historical TDD evidence rows lack SAFETY NET/TRIANGULATE columns. Accepted.
- **Suggestions (deferred)**: S1 lifespan in `agent/main.py`; S2 `VncScreen` as an async context manager; S3 pytest-cov/ruff/mypy; S4 public `is_connected` to replace private-attribute asserts.
- **Open assumptions for future changes**: `TaskStatus` values `pendiente/en_curso/terminada/fallida/cancelada` (resolved in task 1.5, but `design.md` still shows the open-question checkbox unchecked; the design file is archived as written). Minimal fields on Observation/Action/Commit/Procedure/Decision/Classification.
- **Stale snapshot claims, not current**:
  - `tasks` #1174 header says "28 tasks" and "~1600 lines, Decision needed before apply: Yes, Chain strategy pending". The persisted `tasks.md` lists 23 tasks, all checked. The forecast reflects planning time. Delivery actually ran as three stacked PRs (launch prompt).
  - Task 3.7 says "150 passed". That was the count at its time. The final count is 181.
  - `verify-report` says "task 3.7 smoke evidence is historical" and "real desktop not contacted". The verify run did not touch the desktop. The smoke was done earlier during apply.

## Contradictions Recorded (not resolved silently)

- **Test count after 135513b**: launch prompt says 181 pass on 135513b. Not re-run by archive, which has no shell access. Recorded as launch-prompt-sourced.

## Verification of This Archive

- [x] Main specs written: 7 of 7 under `openspec/specs/`
- [x] Archive folder created with all artifacts including `verify-report.md`
- [ ] **Active change folder NOT removed.** The archive executor had no delete or move capability (no shell; Write and Edit only). `openspec/changes/agent-qa-foundation-vnc/` still exists as a copy. The orchestrator or user must remove it, e.g. `mv` or `rm -r`, after confirming the archive copy.
- [x] No commit, stage or push performed (per instruction; the orchestrator commits).

## Traceability Notes

Files and folders touched: `openspec/specs/**` (new), `openspec/changes/archive/2026-10-10-agent-qa-foundation-vnc/**` (new). `openspec/changes/agent-qa-foundation-vnc/` is left in place pending removal (see above). No code, test or config file was modified.
