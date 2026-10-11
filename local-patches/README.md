# Explicit core patch bundle

`ACTIVE` records application order. `pins.json` records reviewed artifact SHA-256 hashes.
Only the explicit companion installer applies this bundle. Native plugin loading does not.

This inventory applies in `ACTIVE` order to a pristine checkout of Hermes revision
`f925b01791fe052e7d55dc975750c0bd985aff91`. It is revision-specific, not a claim that these
patches apply to every release; `hermes charm drift` checks the next one. Recheck upstream status before retiring a fix.

| Artifact | Origin and purpose |
| --- | --- |
| `dashboard-dispatch-caps` | Local port of [NousResearch/hermes-agent#120651](https://github.com/NousResearch/hermes-agent/pull/120651), dispatch limits. |
| `project-skills-worktree-trust` | [NousResearch/hermes-agent#116040](https://github.com/NousResearch/hermes-agent/pull/116040), with local trust validation corrections. |
| `multiplex-notice-profile-scope` | [NousResearch/hermes-agent#128952](https://github.com/NousResearch/hermes-agent/pull/128952), profile-scoped notices. |
| `idle-compact-watchdog-79357` | Hand-port of [NousResearch/hermes-agent#79456](https://github.com/NousResearch/hermes-agent/pull/79456), idle activity timestamps. |
| `kanban-skill-pin-tolerance` | Hand-port of [NousResearch/hermes-agent#89601](https://github.com/NousResearch/hermes-agent/pull/89601), stale skill pins. |
| `kanban-token-hygiene` | Stacked bundle absorbing attachment delivery, compact handoffs, block-loop escalation and local guidance. |
| `kanban-branch-name-punctuation` | [NousResearch/hermes-agent#130500](https://github.com/NousResearch/hermes-agent/pull/130500), trailing title punctuation. |
| `kanban-rate-limit-backoff` | [NousResearch/hermes-agent#119994](https://github.com/NousResearch/hermes-agent/pull/119994), escalating rate-limit respawn backoff. |
| `kanban-superseded-check-runs` | [NousResearch/hermes-agent#122701](https://github.com/NousResearch/hermes-agent/pull/122701) (closed unmerged), newest run per app decides each required check. `kanban-merged-review-sync` is cut on top of it. |
| `kanban-merged-review-sync` | [NousResearch/hermes-agent#117500](https://github.com/NousResearch/hermes-agent/pull/117500), persisted evidence and merged-card reconciliation, including the new `kanban_pr_reconcile.py` module. Cut on top of `kanban-superseded-check-runs`. |
| `kanban-worktree-teardown` | [NousResearch/hermes-agent#123017](https://github.com/NousResearch/hermes-agent/pull/123017), rebased shared-workspace cleanup. |
| `kanban-review-complete-action` | Local dashboard affordance: the Complete action for cards in Review, so the human reviewer can finish an approved card without reassigning it. |

The token-hygiene bundle combines work from
[NousResearch/hermes-agent#120542](https://github.com/NousResearch/hermes-agent/pull/120542),
[NousResearch/hermes-agent#120543](https://github.com/NousResearch/hermes-agent/pull/120543),
[NousResearch/hermes-agent#116561](https://github.com/NousResearch/hermes-agent/pull/116561),
[NousResearch/hermes-agent#125333](https://github.com/NousResearch/hermes-agent/pull/125333), and
[NousResearch/hermes-agent#118790](https://github.com/NousResearch/hermes-agent/pull/118790).
Independent artifacts shared context and invalidated each other's reverse checks.

Provenance links identify sources; they do not assert a current merge state. Preserved
regression-test hunks remain in the artifacts. Historical behavior-test results and known
baseline failures remain in the private `hermes-home` maintenance history.

## Staying in step with upstream

Artifacts are cut against one Hermes revision, so every upstream move can break one. Find out
before `hermes update` does, not after:

- **Nightly CI** (`Patch bundle on upstream main`) rebases the bundle onto upstream `main` and runs
  every test file the bundle carries. Exit 2 means a patch needs a hand port; the job summary
  lists artifacts that merge cleanly but must be regenerated.
- **Before updating**, run `hermes charm drift --tests`. It fetches `origin/main`, walks the bundle
  in a scratch clone, and reports `applies`, `merged` (3-way clean) or `conflict` per patch. It
  never touches the checkout or the artifacts.
- **To update**, in this repository: `python3 scripts/patch_series.py rebase --ref origin/main
  --tests --write` regenerates merged artifacts and their pins, new files included. Commit, push,
  wait for CI, install the Charm commit, then `hermes update`; the startup guard applies the
  bundle to the new revision. On a conflict, the kept clone is the hand-port workspace: finish the
  port there, then regenerate that artifact as the `git diff` of the stack before and after it.
- Artifacts are generated from tree diffs, never hand-edited, so a patch cannot ship without a
  file it adds. Never park a patch by editing the installed `ACTIVE`; that hides the failure.

Use `hermes charm check` for a read-only current preflight. See [SAFETY.md](SAFETY.md)
for explicit application, rollback, failure handling and adoption requirements.
