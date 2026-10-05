# Charm contributor rules

Charm is a standalone native Hermes plugin with an explicit companion patch installer.

- Register only through documented `ctx` APIs. Do not import Hermes internals or monkey-patch core.
- Plugin import, registration and `hermes charm check` must never modify the Hermes checkout.
- Apply core patches only through the explicit companion script or opt-in startup guard.
- Keep user state and receipts under the active Hermes home, not the installed Git tree.
- Preserve pins, patch order, Git index, unrelated edits and safe rollback behavior.
- Never silently retire a conflicting patch. Fail with a reason and retain the last success receipt.
- Do not include credentials, local config, sessions, or another user's profile data.
- New behavior needs a failing test first. Run `python3 -m unittest discover -s tests -v`.
- Run `python3 scripts/runtime_smoke.py /absolute/path/to/hermes` for native integration.
- Run `hermes plugins doctor . --ci` and `git diff --check` before committing.
- CI tests native plugin behavior against pinned Hermes and upstream main. The core overlay is
  revision-specific; CI does not imply those patches apply to every upstream revision.
