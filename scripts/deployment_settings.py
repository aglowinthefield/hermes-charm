#!/usr/bin/env python3
"""Read nonsecret integration settings from HERMES_DEPLOYMENT_CONFIG.

Python callers use get(section, key). The command accepts section and key and
prints strings unchanged, or JSON for structured values. No local fallback is
provided. Credentials remain in the environment or an external secret manager.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


class ConfigurationError(ValueError):
    """Deployment settings are absent or invalid."""


_INTEGER_KEYS = {"daily_card_cap", "net_fails_before_alert", "log_excerpt_cap",
                 "send_timeout", "max_sends"}
_LIST_KEYS = {"watched_workflows", "deploy_workflows", "hold_labels"}
_MAP_KEYS = {"team_to_board", "contract_by_board"}
_SECTIONS = {"linear-intake", "ci-watchdog", "review-notifications", "pr-conflict-sweep"}


def get(section: str, key: str) -> Any:
    """Return a required setting, refusing absent files, keys and invalid types."""
    filename = os.environ.get("HERMES_DEPLOYMENT_CONFIG", "").strip()
    if not filename:
        raise ConfigurationError("HERMES_DEPLOYMENT_CONFIG must name a deployment JSON file")
    path = Path(filename).expanduser()
    try:
        config = json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError(f"Cannot read deployment config {path}: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigurationError(f"Deployment config {path} must be a root object")
    if section not in _SECTIONS:
        raise ConfigurationError(f"Unknown deployment section {section!r}")
    values = config.get(section)
    if not isinstance(values, dict) or key not in values:
        raise ConfigurationError(f"Deployment config {path} requires {section}.{key}")
    value = values[key]
    if key in _INTEGER_KEYS:
        valid = type(value) is int and value > 0
    elif key in _LIST_KEYS:
        valid = isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)
    elif key in _MAP_KEYS:
        valid = isinstance(value, dict) and all(
            isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
            for k, v in value.items())
    else:
        valid = isinstance(value, str) and bool(value.strip())
    if not valid:
        raise ConfigurationError(f"Deployment config {path} has invalid {section}.{key}")
    if key == 'home' and isinstance(value, str):
        return str(Path(value).expanduser().resolve())
    return value


def hermes_environment(home: Path) -> dict[str, str]:
    """Bind child CLI settings and the deliberately shared Kanban root together."""
    return {**os.environ, 'HERMES_HOME': str(home), 'HERMES_KANBAN_HOME': str(home)}


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: deployment_settings.py SECTION KEY", file=sys.stderr)
        return 2
    try:
        value = get(sys.argv[1], sys.argv[2])
    except ConfigurationError as exc:
        print(f"deployment settings error: {exc}", file=sys.stderr)
        return 1
    print(value if isinstance(value, str) else json.dumps(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
