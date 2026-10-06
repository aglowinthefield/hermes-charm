# Explicit core patch bundle

`ACTIVE` records application order. `pins.json` records reviewed artifact SHA-256 hashes.
Only the explicit companion installer applies this bundle. Native plugin loading does not.

This inventory was extracted from the user's maintained Hermes home at installed Hermes
revision `f5a9361dcc66b0b972fd488b83129561509b3486`. It is revision-specific, not a claim
that these patches apply to every release. Recheck upstream status before retiring a fix.

| Artifact | Origin and purpose |
| --- | --- |
| `kanban-worktree-teardown` | [NousResearch/hermes-agent#123017](https://github.com/NousResearch/hermes-agent/pull/123017), rebased shared-workspace cleanup. |
| `dashboard-dispatch-caps` | Local port of [NousResearch/hermes-agent#120651](https://github.com/NousResearch/hermes-agent/pull/120651), dispatch limits. |
| `project-skills-worktree-trust` | [NousResearch/hermes-agent#116040](https://github.com/NousResearch/hermes-agent/pull/116040), with local trust validation corrections. |
| `multiplex-notice-profile-scope` | [NousResearch/hermes-agent#128952](https://github.com/NousResearch/hermes-agent/pull/128952), profile-scoped notices. |
| `kanban-merged-review-sync` | [NousResearch/hermes-agent#117500](https://github.com/NousResearch/hermes-agent/pull/117500), persisted evidence and merged-card reconciliation. |
| `idle-compact-watchdog-79357` | Hand-port of [NousResearch/hermes-agent#79456](https://github.com/NousResearch/hermes-agent/pull/79456), idle activity timestamps. |
| `kanban-skill-pin-tolerance` | Hand-port of [NousResearch/hermes-agent#89601](https://github.com/NousResearch/hermes-agent/pull/89601), stale skill pins. |
| `kanban-token-hygiene` | Stacked bundle absorbing attachment delivery, compact handoffs, block-loop escalation and local guidance. |
| `kanban-branch-name-punctuation` | [NousResearch/hermes-agent#130500](https://github.com/NousResearch/hermes-agent/pull/130500), trailing title punctuation. |

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

Use `hermes charm check` for a read-only current preflight. See [SAFETY.md](SAFETY.md)
for explicit application, rollback, failure handling and adoption requirements.
