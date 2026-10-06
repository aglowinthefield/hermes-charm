# Charm patch safety

Keep supported customizations in native plugins. Core bug fixes remain explicit artifacts
until upstream supplies the behavior or an extension point. Plugin loading never modifies core.

## Controls

- `ACTIVE` records the ordered set; `pins.json` binds each reviewed artifact to its SHA-256.
- Native boolean settings select desired artifacts. A toggle never changes source by itself.
- Check is read-only. Apply reverses disabled artifacts exactly in the sandbox, validates the
  entire desired set, then writes. Ambiguous state or a conflicting selection fails closed.
- `local_patches.py` copies touched files into an isolated Git directory and preflights all patches.
- Every patch must reverse against the final sandbox result. Bundle overlapping context.
- The installer refuses unsafe paths, symlinks, hardlinked targets, unsupported modes, binaries,
  renames, changed artifacts, missing pins and checkout drift.
- Application preserves bytes/modes before writing, checks target bytes for concurrent edits,
  leaves the Git index alone, and restores completed writes on a caught installation failure.
- Receipts bind backups to the checkout, HEAD, before/after fingerprints and absent files.
- Rollback refuses changed backups, edited targets, a different checkout or a different HEAD.
- The optional startup guard requires a working native command and Plugin Doctor, then applies
  the bundle. A refusal prevents service startup. It sends no external notifications.

SHA-256 pins detect changes; they are neither signatures nor proof of behavior.
The lock serializes this installer's writers, not arbitrary updates or other editors.
Do not run updates or checkout writers concurrently with application or rollback.
The operation is not a filesystem-wide transaction. A process kill can interrupt multi-file
installation; preserve backups and require a fresh preflight before starting services.

## Commands

```sh
hermes charm check
hermes charm disable cron-worker-bootstrap
hermes charm apply --check
hermes charm apply
hermes charm apply --rollback ~/.hermes/state/patch-backup-RECEIPT_ID
```

Receipts stay in the active home's `state/local-patches.status`. A refusal leaves the last
successful receipt untouched; read stderr or the service journal for the current failure.
Backups stay beside the receipt. The installed plugin checkout contains no runtime receipts.
The default bundle lives beside the engine inside Charm. Environment overrides named
`LOCAL_PATCHES_AGENT_DIR`, `LOCAL_PATCHES_DIR`, and `LOCAL_PATCHES_STATE` are supported for tests
and explicit external companion invocations; they are not settings for automatic plugin loading.

## Adoption

1. Inspect the upstream diff and its interactions with every active patch.
2. Preserve scoped source and regression-test hunks. Verify forward and reverse application.
3. Run behavior tests at the intended Hermes revision; compare failures with baseline.
4. Update artifact, application order and digest together in the development repository.
5. Run safety tests, native smoke tests and preflight against the actual installed checkout.
6. Push, wait for CI, then deliberately install the tested Charm commit.
7. Restart separately when workers are idle. No command here automatically restarts a backend.

A conflict does not prove a fix landed upstream. Never auto-retire patches or hide their application
inside plugin registration. Disable a deliberately retired customization before startup to avoid
reapplication. Rollback restores source, not desired settings. The raw Python engine does not
read profile settings; the native command, shell wrapper and Charm guard do.

## Compatibility limits

Native CI validates discovery and behavior against pinned Hermes and upstream main using isolated
fixture patches. The extracted full bundle fails preflight on an unmodified upstream checkout
at `f5a9361dcc66b0b972fd488b83129561509b3486`: the skill-pin artifact assumes local context in
`hermes_cli/kanban_db.py` and `tests/hermes_cli/test_kanban_core_functionality.py`. It must be
rebased and behavior-tested before fresh upstream deployment. The live startup guard checks
the actual maintained checkout after each update. Native compatibility is not full-bundle compatibility.

The adopted teardown patch retains a known baseline home-I/O guard test failure from the live
home checkout. See the original private maintenance history for the with/without comparison.
It is not a failure of Charm's isolated safety tests.
