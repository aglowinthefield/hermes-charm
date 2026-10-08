<p align="center"><img src="assets/charm.png" width="260" alt="A spiral of green leaves and pink flowers"></p>
<h1 align="center">Charm</h1>
<p align="center">Customizations you carry between Hermes versions.</p>

Charm is a private, independently installable native Hermes plugin. It bundles its
maintenance implementation, tests and a separate SHA-256-pinned core patch set.
It does not depend on scripts copied into your Hermes home.

<!-- charm:catalog:start -->
## Customizations

Upstream status checked at `2026-10-05T02:52:11Z`. This is a snapshot, not a live query.

Each row is one independent toggle. All ship enabled by default to preserve the existing active set.
A merge is a reason to review retirement, not permission to silently remove a patch.

### Homebrew policy, no planned upstream submission

| Toggle | What it changes | Upstream / maintenance |
| --- | --- | --- |
| `kanban-review-complete-action` | Expose the existing Complete action for cards in Review so a human reviewer can finish an approved card without reassigning it. | Local dashboard affordance; the backend already permits review-to-done completion. |

### Temporary fixes awaiting upstream merges

| Toggle | What it changes | Upstream / maintenance |
| --- | --- | --- |
| `kanban-worktree-teardown` | Clean completed, clean, fully pushed worktrees without waiting on children using independent workspaces; retain shared/scratch workspaces while needed. | [NousResearch/hermes-agent#123017](https://github.com/NousResearch/hermes-agent/pull/123017) (open). Rebased backport; retain until upstream behavior and regression tests are verified. |
| `dashboard-dispatch-caps` | Respect configured total/per-profile in-progress caps, failure limits and default assignee when nudging dashboard dispatch. | [NousResearch/hermes-agent#120651](https://github.com/NousResearch/hermes-agent/pull/120651) (open). Partial port. It does not implement the shared resolver or the upstream proposal's configured max_spawn default. |
| `project-skills-worktree-trust` | Let registered linked worktrees inherit canonical-repository skill trust after Git metadata validation. | [NousResearch/hermes-agent#116040](https://github.com/NousResearch/hermes-agent/pull/116040) (open). Local validation changes differ from the current proposal; review additional upstream trust corrections before retirement. |
| `multiplex-notice-profile-scope` | Use the owning profile's preferences, language, cron store and delivery adapters for shutdown and interrupted-cron notices. | [NousResearch/hermes-agent#128952](https://github.com/NousResearch/hermes-agent/pull/128952) (open). Local port does not include every module extraction or later correction in the current proposal. |
| `kanban-merged-review-sync` | Reuse persisted PR evidence, prefer the latest execution of each required GitHub check, and reconcile unclaimed merged reviews without holding board locks across GitHub reads. | [NousResearch/hermes-agent#117500](https://github.com/NousResearch/hermes-agent/pull/117500) (open). Recheck handoff identity before completion; never bypass acceptance gates. |
| `idle-compact-watchdog-79357` | Preserve pre-watchdog activity time so the idle-compaction check sees the actual idle gap. | [NousResearch/hermes-agent#79456](https://github.com/NousResearch/hermes-agent/pull/79456) (open). Hand-port adapted to the extracted compaction module. |
| `kanban-skill-pin-tolerance` | Drop unavailable assignee skill pins at creation and worker launch, recording creation-time drops and warning at launch. | [NousResearch/hermes-agent#89601](https://github.com/NousResearch/hermes-agent/pull/89601) (open). One owner for this behavior: duplicated skill-filter hunks were removed from the token-hygiene artifact. |
| `kanban-branch-name-punctuation` | Strip boundary periods/hyphens, including after truncation, while retaining internal version-number periods. | [NousResearch/hermes-agent#130500](https://github.com/NousResearch/hermes-agent/pull/130500) (open). Backport; retain until the upstream behavior is verified. |
| `kanban-rate-limit-backoff` | Hold a card 5m, 15m, 45m, then 2h after consecutive rate-limited runs instead of respawning every 5 minutes forever; relay-pool health gate and board-wide circuit stay inert for non-pool providers. | [NousResearch/hermes-agent#119994](https://github.com/NousResearch/hermes-agent/pull/119994) (open). Whole-PR adoption. 2026-10-02 storm: 1,329 rate-limited respawns across 18 cards in 13 hours. |
| `kanban-superseded-check-runs` | Judge each required check by its newest run per app. A cancelled run from a superseded workflow suite on the same head no longer rejects completion of a green or merged PR. | [NousResearch/hermes-agent#122701](https://github.com/NousResearch/hermes-agent/pull/122701) (closed without merge). Whole-PR adoption; fixes issue NousResearch/hermes-agent#122688. The author closed the PR unmerged after a bot review confirmed the fix. Overlaps the parked kanban-merged-review-sync hunk in collect_acceptance; drop that hunk when re-adapting it. |

### Mixed upstream fixes and homebrew policy

| Toggle | What it changes | Upstream / maintenance |
| --- | --- | --- |
| `kanban-token-hygiene` | Deliver parent attachments and archived-parent handoffs; reduce duplicated output/history; add compact/minimal views, hard-blocker escalation and local read/card-budget guidance. | [NousResearch/hermes-agent#120542](https://github.com/NousResearch/hermes-agent/pull/120542) (open); [NousResearch/hermes-agent#120543](https://github.com/NousResearch/hermes-agent/pull/120543) (open); [NousResearch/hermes-agent#116561](https://github.com/NousResearch/hermes-agent/pull/116561) (open); [NousResearch/hermes-agent#125333](https://github.com/NousResearch/hermes-agent/pull/125333) (open); [NousResearch/hermes-agent#118790](https://github.com/NousResearch/hermes-agent/pull/118790) (open). Atomic toggle for overlapping changes. Local prompt/card-budget policy has no planned upstream submission; the linked fixes await merges. Default/full output is also trimmed. |

### Upstream successor merged, retirement needs verification

| Toggle | What it changes | Upstream / maintenance |
| --- | --- | --- |

The token-hygiene row is one atomic group of overlapping handoff/prompt changes.
Its upstream portions and local policy cannot be switched separately yet.
Skill-pin tolerance has its own toggle; it is no longer duplicated inside token hygiene.

<!-- charm:catalog:end -->

## Install

Use an exact, tested commit SHA. The private repository requires SSH access.

```sh
hermes plugins install git@github.com:aglowinthefield/hermes-charm.git --ref FULL_COMMIT_SHA --no-enable --yes-deps
hermes plugins doctor charm --ci
hermes plugins enable charm
hermes charm check
```

Native installation records the repository and commit in Hermes's provenance tracking.
A pinned install stays pinned. Upgrade deliberately by reinstalling at a newer tested
SHA with `--force`; do not edit the installed checkout or invent a self-updater.

## What ships

| Part | Responsibility |
| --- | --- |
| `plugin.yaml`, `__init__.py`, `charm.py` | Native catalog, profile settings and explicit check/apply commands. |
| `customizations.json` | Descriptions, defaults, maintenance intent and verified upstream status. |
| `local_patches.py` | Preflight the complete ordered patch set, preserve backups and install checked bytes. |
| `local-patches/` | Explicit core patches, application order, SHA-256 pins and provenance. |
| `scripts/charm-prestart.sh` | Require a loaded Charm command and successful Plugin Doctor before applying the bundle. |
| `tests/`, `scripts/runtime_smoke.py` | Real-Git safety tests and native CLI behavior in isolated homes. |

Plugin loading never applies patches, dispatches work, changes settings or contacts a
provider. Patch application is an explicit companion operation, not a plugin hook.

## Customization controls

Use the toggles in Charm's settings form in the Desktop Plugins tab, or the native CLI:

```sh
hermes charm list
hermes charm disable kanban-worktree-teardown
hermes charm check
hermes charm apply
hermes charm enable kanban-worktree-teardown
```

Enable/disable changes only the desired profile setting. Check reports pending additions
and removals without touching core. Explicit apply installs enabled artifacts and safely
reverses disabled ones in a sandbox first, retaining backups, unrelated edits and the index.
Changes affect running backends only after a separate, safe restart. No command restarts them.

Settings live at `plugins.entries.charm.settings.<toggle-id>` and are real booleans.
For example, `hermes config set plugins.entries.charm.settings.kanban-worktree-teardown false`.
An unknown toggle, non-boolean setting, ambiguous patch state or unsafe combination refuses
the operation. Merely deleting an ACTIVE entry would not remove its already-applied code.

Settings are profile-local, but core patches change a checkout shared by every process using it.
Use one authoritative profile's selection for a shared checkout, not conflicting selections.
The old home-owned local-maintenance guard does not read Charm's toggles. Adopt the optional
Charm guard separately before relying on these settings across restarts.

## Check, apply and roll back

The native command checks the checkout without changing it:

```sh
hermes charm check
```

Apply only while updates and other checkout writers are idle:

```sh
hermes charm apply
```

The installer stores receipts and backups under the active Hermes home's `state/`.
It refuses changed artifacts, unsafe paths, conflicting patches and unsafe rollback.
It preserves unrelated edits and the Git index. See `local-patches/SAFETY.md`.

```sh
hermes charm apply --rollback ~/.hermes/state/patch-backup-RECEIPT_ID
```

Disable a deliberately retired customization before restarting, or the guard will reapply it.
A conflict alone does not prove that upstream has implemented the fix.

`scripts/local-patches-ensure.sh` delegates to native `hermes charm apply`, so it reads the same
profile settings. It accepts `--hermes /absolute/path/to/hermes` before operation flags.
Direct `python3 local_patches.py` is a low-level, settings-free engine for tests and recovery;
it does not read native settings and enables all artifacts unless passed `--disable NAME`.

## Startup protection

The optional Linux systemd template is `systemd/charm.conf`. It calls the companion
guard with the managed Hermes launcher. Installing the plugin does not enable this
service integration or restart services. Review paths and install the drop-in separately.

The guard refuses startup when Charm is absent, disabled, cannot register, or its
patch set cannot preflight. It does not silently start a partially customized Hermes.
Patch operations are not a filesystem-wide transaction; retained backups support
recovery after an interrupted multi-file write.

## Compatibility

CI tests Python behavior and native plugin discovery against a pinned Hermes revision
and upstream `main`, on pushes and daily. Native integration tests exercise read-only
checks, persisted toggles, explicit application/removal and disabled-plugin refusal in clean homes.
These CI lanes test native compatibility using isolated fixture patches.

The extracted full bundle currently preflights on the maintained local checkout,
but fails on an unmodified checkout at the same Hermes SHA. The skill-pin artifact
depends on local context in `hermes_cli/kanban_db.py` and its regression-test file.
Do not apply this bundle to a fresh upstream install until it has been rebased and
its behavior tests rerun. The installer refuses that mismatch rather than forcing it.

A green native plugin test does not prove every core patch applies to every future
Hermes revision. Local preflight remains mandatory after each update. Keep a known-good
plugin SHA and review failures before updating pins or retiring patches.

```sh
python3 -m unittest discover -s tests -v
hermes plugins doctor . --ci
python3 scripts/runtime_smoke.py /absolute/path/to/hermes
python3 scripts/sync_catalog.py --check
```

The companion patch installer supports Linux and macOS, requires Git, and uses `fcntl`.
The service template is Linux-specific. Windows is not supported by this installer.

## Scope

Charm owns the core bundle and reusable workflow implementations. Home configuration
selects their entry points; merely installing Charm does not register or run them.
No automatic backend restarts, network telemetry or unreviewed plugin updates occur.

| Implementation | Entry point | Configuration owner |
| --- | --- | --- |
| Review routing and CI handoff gate | `agent-hooks/kanban-review-routing.sh` | Home supplies reviewer, operator, investigation profile, and board paths |
| PR-body policy | `agent-hooks/pr-body-humanized.py` | Explicit harness hook registration |
| Repository dependency bootstrap | `agent-hooks/kanban-worktree-deps.sh` | Repository supplies its own `scripts/worktree-deps.sh` |
| Card-body gate | `agent-hooks/kanban-card-body-gate.sh` | Optional registration; migration does not enable it |
| Block-claim auditing and weekly report | `agent-hooks/kanban-blocked-claim-audit.sh`, `scripts/claim-audit-weekly.py` | Shadow-only hook and home schedule |
| Parked-review and give-up notifications | `agent-hooks/kanban-review-parked.sh`, `scripts/kanban-giveup-watch.py` | Home board, destination, identities, and schedule |
| CI watcher and Linear intake | `scripts/ci-watchdog.py`, `scripts/linear-intake.py` | Required deployment JSON; existing state controls live/shadow behavior |
| Worktree maintenance | `scripts/kanban-worktree-sweep.py` | Home schedule and active runtime paths |
| Conflicting-PR rework routing | `scripts/pr-conflict-sweep.py` | Required deployment JSON; reopens only cards parked on the human review assignee |

The eleven native `charm enable/disable` settings above control core patch selection.
Shell policies and scheduled integrations are separately selected by home hook and
cron registrations. They are not silently enabled by those core settings or by plugin
registration. Blocking policies keep their fail-closed shell adapter rather than
becoming native callbacks whose exceptions are logged and skipped.

Integration scripts require `HERMES_DEPLOYMENT_CONFIG` to name the home-owned JSON
file. Sections are `linear-intake`, `ci-watchdog`, `review-notifications`, and
`pr-conflict-sweep`.
`scripts/deployment_settings.py` validates required settings and types. Missing
configuration fails visibly. Credential values remain in the secret manager or
runtime environment; JSON contains references only.

The extraction preserves workflow policy. Bootstrap still runs after worker spawn,
claim auditing remains shadow-only, and the retired Linear notification hook stays
absent. No computer-use guard was copied because the active guard blocked its own
source text; the existing home guard remains in place.

`customizations.json` is the catalog's source. After reviewing updated metadata, run
`python3 scripts/sync_catalog.py` to regenerate this table and the native settings schema.
Update the upstream snapshot deliberately; plugin loading and catalog reads make no network calls.

## Artwork and upstream code

The README illustration came from the user-supplied image below. Its white background
was removed and the result saved as a transparent PNG. The original creator and image
license are unknown; this repository does not grant rights to that artwork.

https://i.pinimg.com/1200x/93/02/e8/9302e8b78051429bae50c90ed2ce9858.jpg

Core patch hunks derive from Nous Research's MIT-licensed Hermes Agent. See
`THIRD_PARTY.md`, `LICENSE.hermes-agent` and the patch provenance notes.
