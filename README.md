<p align="center"><img src="assets/charm.png" width="260" alt="A spiral of green leaves and pink flowers"></p>
<h1 align="center">Charm</h1>
<p align="center">Customizations you carry between Hermes versions.</p>

Charm is a private, independently installable native Hermes plugin. It bundles its
maintenance implementation, tests and a separate SHA-256-pinned core patch set.
It does not depend on scripts copied into your Hermes home.

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
| `plugin.yaml`, `__init__.py`, `charm.py` | Register the read-only `hermes charm check` command through the native API. |
| `local_patches.py` | Preflight the complete ordered patch set, preserve backups and install checked bytes. |
| `local-patches/` | Explicit core patches, application order, SHA-256 pins and provenance. |
| `scripts/charm-prestart.sh` | Require a loaded Charm command and successful Plugin Doctor before applying the bundle. |
| `tests/`, `scripts/runtime_smoke.py` | Real-Git safety tests and native CLI behavior in isolated homes. |

Plugin loading never applies patches, dispatches work, changes settings or contacts a
provider. Patch application is an explicit companion operation, not a plugin hook.

## Check, apply and roll back

The native command checks the checkout without changing it:

```sh
hermes charm check
```

Apply only while updates and other checkout writers are idle:

```sh
sh ~/.hermes/plugins/charm/scripts/local-patches-ensure.sh
```

The installer stores receipts and backups under the active Hermes home's `state/`.
It refuses changed artifacts, unsafe paths, conflicting patches and unsafe rollback.
It preserves unrelated edits and the Git index. See `local-patches/SAFETY.md`.

```sh
sh ~/.hermes/plugins/charm/scripts/local-patches-ensure.sh --rollback ~/.hermes/state/patch-backup-RECEIPT_ID
```

Remove a retired patch from `ACTIVE` before restarting, or the guard will reapply it.
A conflict alone does not prove that upstream has implemented the fix.

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
checks, explicit startup application and disabled-plugin refusal in clean homes.
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
```

The companion patch installer supports Linux and macOS, requires Git, and uses `fcntl`.
The service template is Linux-specific. Windows is not supported by this installer.

## Scope

The first release extracts the existing maintenance code and eleven active patches.
Other workflow hooks and session customizations have not been migrated into Charm.
No automatic backend restarts, network telemetry or unreviewed plugin updates occur.

## Artwork and upstream code

The README illustration came from the user-supplied image below. Its white background
was removed and the result saved as a transparent PNG. The original creator and image
license are unknown; this repository does not grant rights to that artwork.

https://i.pinimg.com/1200x/93/02/e8/9302e8b78051429bae50c90ed2ce9858.jpg

Core patch hunks derive from Nous Research's MIT-licensed Hermes Agent. See
`THIRD_PARTY.md`, `LICENSE.hermes-agent` and the patch provenance notes.
