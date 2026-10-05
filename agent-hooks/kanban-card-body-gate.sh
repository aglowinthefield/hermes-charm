#!/usr/bin/env python3
"""kanban_create body gate (pre_tool_call).

Rebuilt 2026-10-04 after the original script was deleted while live sessions
still had it registered, which failed closed on every kanban_create.

Checks, per the documented behavior of the original:
1. The card body must contain '## Goal' and '## Acceptance criteria' headings.
2. Every name in the card's skills pins must resolve for the assignee:
   shared ~/.hermes/skills plus ~/.hermes/profiles/<assignee>/skills, plus
   <dir>/.hermes/skills and <dir>/.agents/skills for each skills.trusted_project_dirs
   entry in config.yaml (the project skills Hermes's loader trusts).
   A 'plugin:skill' pin resolves against ~/.hermes/plugins/<plugin>/skills/<skill>.

Input: stdin JSON. Tool arguments are found under 'tool_input', 'args', or the
top level. Pass: exit 0, no output. Block: stdout {"decision":"block",...},
reason on stderr, exit 2. Anything that is not kanban_create exits 0.

HOOK_SKILLS_DIRS (colon-separated) overrides the skill search dirs, for tests.
"""
import json
import os
import sys

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))

REQUIRED_HEADINGS = ("## Goal", "## Acceptance criteria")


def find_args(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("tool_input", "args"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    if "body" in payload or "skills" in payload or "assignee" in payload:
        return payload
    return None


def skill_dirs(assignee):
    override = os.environ.get("HOOK_SKILLS_DIRS")
    if override:
        return [d for d in override.split(":") if d]
    dirs = [os.path.join(HERMES_HOME, "skills")]
    if assignee:
        dirs.append(os.path.join(HERMES_HOME, "profiles", assignee, "skills"))
    # Project skills, from the same trusted roots Hermes's loader accepts.
    for root in trusted_project_dirs():
        dirs += [os.path.join(root, ".hermes", "skills"), os.path.join(root, ".agents", "skills")]
    return dirs


def trusted_project_dirs():
    """skills.trusted_project_dirs from config.yaml.

    System python3 has no yaml module, so read the one block-style list this needs.
    """
    try:
        with open(os.path.join(HERMES_HOME, "config.yaml"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    found, section, in_list = [], None, False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            section, in_list = line.split(":", 1)[0].strip(), False
        elif section == "skills" and indent == 2 and not line.lstrip().startswith("-"):
            in_list = line.strip() == "trusted_project_dirs:"
        elif in_list and line.lstrip().startswith("- "):
            found.append(os.path.expanduser(line.lstrip()[2:].strip().strip("'\"")))
    return found


def resolvable(name, dirs):
    if ":" in name:
        plugin, _, skill = name.partition(":")
        candidate = os.path.join(HERMES_HOME, "plugins", plugin, "skills", skill, "SKILL.md")
        return os.path.isfile(candidate)
    for base in dirs:
        if not os.path.isdir(base):
            continue
        for root, _dirnames, filenames in os.walk(base):
            if "SKILL.md" in filenames and os.path.basename(root) == name:
                return True
    return False


def block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))
    print(reason, file=sys.stderr)
    sys.exit(2)


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        # Not parseable; not ours to judge.
        return
    args = find_args(payload)
    if args is None:
        return

    body = args.get("body") or ""
    missing = [h for h in REQUIRED_HEADINGS if h not in body]
    if missing:
        block(
            "kanban_create body is missing required heading(s): "
            + ", ".join(missing)
            + ". Use the standard card shape: ## Goal, ## Context, ## Acceptance criteria."
        )

    pins = args.get("skills") or []
    if not isinstance(pins, list):
        block("kanban_create skills must be a list of skill names.")
    assignee = args.get("assignee") or ""
    dirs = skill_dirs(assignee)
    unknown = [name for name in pins if not resolvable(str(name), dirs)]
    if unknown:
        block(
            "kanban_create skills pin(s) not resolvable for assignee "
            + (assignee or "<none>")
            + ": "
            + ", ".join(unknown)
            + ". Skills must exist under ~/.hermes/skills or the assignee's "
            + "profile skills dir. Remove the pin or install the skill first."
        )


if __name__ == "__main__":
    main()
