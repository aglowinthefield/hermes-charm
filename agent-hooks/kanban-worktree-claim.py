#!/usr/bin/env python3
"""Pre-tool hook: a session may not write into a kanban card's worktree while a worker can own it.

A card assigned to a dispatchable profile can get a worker at any time: the dispatcher claims
it as soon as it is ready, and a dashboard move from triage or blocked is enough to make it
ready. On 2026-10-07 a chat session repaired SIL-440 (t_fc438142) by hand in that card's
worktree while the card sat in triage. Someone moved it to todo, the dispatcher spawned scribe
into the same worktree, and two writers pushed to one branch, one force-pushing over the other.

This hook blocks history-writing git and `gh pr` mutations, plus write_file/patch edits, that
land inside the worktree of a card that:
  - is on a board under the kanban root and not done or archived,
  - has a `.worktrees/` workspace path (cards whose workspace is the repo root claim nothing),
  - is assigned to a profile in `kanban.dispatch_profiles`,
  - and whose worker is not this process or one of its ancestors. The worker is the run's
    spawned pid (`tasks.worker_pid`) or the interpreter the profile owner bound to the run
    (`worker_bound` events for `tasks.current_run_id`): in owner-routed dispatch the agent runs
    in that bound interpreter, a sibling of the spawned client, not one of its descendants.
The card's own worker passes. To work the card by hand, reassign it to a profile the
dispatcher cannot claim (`default`) with --reclaim first; the block reason prints the command.
Reads (git status/log/diff, gh pr view/checks) always pass.

Targets come from `cd`, `git -C`, the terminal `workdir` argument and the session cwd, through
VAR=val prefixes, wrapper commands, `sh -c`, `eval` and `$(...)`. A shell writing files by other
means (sed -i, a formatter) is not covered. An unparseable command that names a claimed worktree
and a write verb fails closed.

Wired as a Hermes pre_tool_call hook (matcher: terminal|write_file|patch) in config.yaml and
each profile. Hermes reads the reason from stdout JSON; it is echoed to stderr for other hosts.
"""

import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys

SEPARATOR = set(";&|()\n")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
WRAPPERS = {
    "command": set(), "builtin": set(), "exec": {"-a"}, "nohup": set(), "time": {"-f", "-o"},
    "env": {"-u", "-C", "--chdir", "--unset", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}, "doas": {"-u", "-C"},
    "nice": {"-n"}, "timeout": {"-s", "-k", "--signal", "--kill-after"},
}
GIT_WRITES = {
    "add", "am", "apply", "checkout", "cherry-pick", "clean", "commit", "merge", "mv", "pull",
    "push", "rebase", "reset", "restore", "revert", "rm", "stash", "switch", "tag", "worktree",
}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"}
GH_PR_WRITES = {"create", "edit", "ready", "merge", "close", "reopen", "checkout"}
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
WRITE_HINT = re.compile(
    r"\bgit\b.*\b(" + "|".join(sorted(GIT_WRITES)) + r")\b|\bgh\s+pr\s+(" + "|".join(sorted(GH_PR_WRITES)) + r")\b"
)
V4A_PATH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)
FINISHED = ("done", "archived")
FALLBACK_NON_DISPATCHABLE = {"", "default", "tom", "none"}


class Unparseable(Exception):
    pass


# --- which directories does the tool call write into? -------------------------------------


def strip_heredoc_bodies(cmd: str) -> str:
    out, pending = [], []
    for line in cmd.split("\n"):
        if pending:
            strip_tabs, word = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == word:
                pending.pop(0)
            continue
        out.append(line)
        pending = [(m.group(1) == "-", m.group(3)) for m in HEREDOC.finditer(line)]
    return "\n".join(out)


def tokenize(cmd: str) -> list[str]:
    cmd = strip_heredoc_bodies(cmd).replace("\\\n", " ").replace("`", " ; ")
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError as e:
        raise Unparseable(str(e)) from e


def segments(tokens: list[str]):
    seg: list[str] = []
    for tok in tokens:
        if tok and set(tok) <= SEPARATOR:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(tok)
    if seg:
        yield seg


def substitutions(tok: str):
    start = tok.find("$(")
    while start != -1:
        depth, i = 0, start + 1
        for i in range(start + 1, len(tok)):
            depth += tok[i] == "("
            depth -= tok[i] == ")"
            if depth == 0:
                break
        yield tok[start + 2 : i]
        start = tok.find("$(", i)


def unwrap(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg):
        tok = seg[i]
        if ASSIGN.match(tok):
            i += 1
            continue
        name = os.path.basename(tok)
        if name not in WRAPPERS:
            break
        takes_arg = WRAPPERS[name]
        i += 1
        while i < len(seg) and seg[i].startswith("-"):
            i += 2 if seg[i] in takes_arg else 1
        if name == "timeout" and i < len(seg):
            i += 1
    return seg[i:]


def resolve(base: str, path: str) -> str:
    if sys.platform == "win32":  # Git Bash spells C:\x as /c/x
        path = re.sub(r"^/([A-Za-z])(?=/|$)", r"\1:", path)
    return os.path.normpath(os.path.join(base, os.path.expanduser(path)))


def comparable(path: str) -> str:
    """Resolved, case-folded where the filesystem is, and with `/` separators on every OS."""
    return os.path.normcase(os.path.realpath(path)).replace("\\", "/")


def git_target(args: list[str], cwd: str) -> str | None:
    """Directory a git invocation writes in, or None for a read."""
    target, i = cwd, 0
    while i < len(args) and args[i].startswith("-"):
        opt = args[i]
        if opt == "-C" and i + 1 < len(args):
            target = resolve(target, args[i + 1])
        elif opt.startswith("--work-tree="):
            target = resolve(target, opt.split("=", 1)[1])
        i += 2 if opt in GIT_OPTS_WITH_VALUE else 1
    return target if i < len(args) and args[i] in GIT_WRITES else None


def command_targets(cmd: str, cwd: str, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    targets: list[str] = []
    for seg in segments(tokenize(cmd)):
        for tok in seg:
            for inner in substitutions(tok):
                targets += command_targets(inner, cwd, depth + 1)
        argv = unwrap(seg)
        if not argv:
            continue
        exe, args = os.path.basename(argv[0]), argv[1:]
        if exe == "cd" and args:
            cwd = resolve(cwd, args[0])
        elif exe in SHELLS:
            for j, a in enumerate(args[:-1]):
                if re.fullmatch(r"-[a-z]*c[a-z]*", a):
                    targets += command_targets(args[j + 1], cwd, depth + 1)
                    break
        elif exe == "eval":
            targets += command_targets(" ".join(args), cwd, depth + 1)
        elif exe == "git":
            target = git_target(args, cwd)
            if target:
                targets.append(target)
        elif exe == "gh" and len(args) >= 2 and args[0] == "pr" and args[1] in GH_PR_WRITES:
            targets.append(cwd)
    return targets


def tool_targets(tool: str, tool_input: dict, cwd: str) -> tuple[list[str], bool]:
    """(directories or files written, whether the command failed to parse)."""
    if tool in ("write_file", "patch"):
        paths = [tool_input.get("path")] if tool_input.get("path") else []
        paths += V4A_PATH.findall(tool_input.get("patch") or "")
        return [resolve(cwd, p.strip()) for p in paths if isinstance(p, str)], False
    cmd = tool_input.get("command") or ""
    if isinstance(cmd, list):
        cmd = shlex.join(cmd)
    if not isinstance(cmd, str) or not cmd.strip():
        return [], False
    cwd = resolve(cwd, tool_input["workdir"]) if tool_input.get("workdir") else cwd
    try:
        return command_targets(cmd, cwd), False
    except Unparseable:
        return [cwd], bool(WRITE_HINT.search(cmd))


# --- which cards claim those directories? ---------------------------------------------------


def boards_dir() -> Path:
    if os.environ.get("KANBAN_BOARDS_DIR"):
        return Path(os.environ["KANBAN_BOARDS_DIR"])
    if os.environ.get("HERMES_KANBAN_HOME"):
        return Path(os.environ["HERMES_KANBAN_HOME"]).expanduser() / "kanban" / "boards"
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if home.parent.name == "profiles":  # boards are shared across profiles, at the root
        home = home.parent.parent
    return home / "kanban" / "boards"


def bound_workers(conn: sqlite3.Connection, task_ids: list[str]) -> dict[str, set[int]]:
    """task id -> pids the owner bound to the card's current run (`worker_bound` events).

    Only the current run counts, so a pid from an ended attempt never admits a later process
    that reuses it. Boards without run tracking have no such events and give an empty map."""
    if not task_ids:
        return {}
    marks = ",".join("?" * len(task_ids))
    try:
        rows = conn.execute(
            "SELECT e.task_id, e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
            f"WHERE e.kind = 'worker_bound' AND e.run_id = t.current_run_id AND t.id IN ({marks})",
            task_ids,
        ).fetchall()
    except sqlite3.Error:
        return {}
    bound: dict[str, set[int]] = {}
    for task_id, payload in rows:
        try:
            pid = json.loads(payload or "{}").get("pid")
        except (ValueError, AttributeError):
            continue
        if isinstance(pid, int) and pid > 0:
            bound.setdefault(task_id, set()).add(pid)
    return bound


def open_cards() -> list[dict]:
    cards = []
    for db in sorted(boards_dir().glob("*/kanban.db")):
        if db.parent.name.startswith(("_", "-")):
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT id, assignee, status, workspace_path, worker_pid FROM tasks "
                    "WHERE workspace_kind = 'worktree' "
                    "AND REPLACE(workspace_path, '\\', '/') LIKE '%/.worktrees/%' "
                    "AND status NOT IN (?, ?)", FINISHED,
                ).fetchall()
                bound = bound_workers(conn, [row[0] for row in rows])
            finally:
                conn.close()
        except sqlite3.Error:
            continue  # one unreadable board must not hide claims on the others
        for task_id, assignee, status, path, pid in rows:
            pids = bound.get(task_id, set()) | ({pid} if pid else set())
            cards.append({"board": db.parent.name, "id": task_id, "assignee": assignee or "",
                          "status": status, "path": os.path.normpath(path), "pids": pids})
    return cards


def dispatch_profiles() -> set[str] | None:
    try:
        out = subprocess.run(["hermes", "config", "get", "kanban.dispatch_profiles"],
                             capture_output=True, text=True, timeout=8, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = {m.group(1) for m in re.finditer(r"^\s*-\s*([\w.-]+)\s*$", out, re.M)}
    return names or None


def windows_parents() -> dict[int, int]:
    """pid -> parent pid for every process, from a Toolhelp snapshot (no /proc or ps)."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_void_p), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("ppid", wintypes.DWORD), ("priority", ctypes.c_long), ("flags", wintypes.DWORD),
                    ("exe", ctypes.c_char * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    entry, parents = Entry(size=ctypes.sizeof(Entry)), {}
    try:
        more = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.pid] = entry.ppid
            more = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return parents


def ancestors() -> set[int]:
    pids, pid = set(), os.getpid()
    if sys.platform == "win32":
        parents = windows_parents()
        while pid and pid not in pids:
            pids.add(pid)
            pid = parents.get(pid, 0)
        return pids
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            try:
                pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                                         text=True, timeout=3).stdout.strip() or 0)
            except (OSError, ValueError, subprocess.SubprocessError):
                break
    return pids


def inside(target: str, root: str) -> bool:
    return target == root or target.startswith(root.rstrip("/") + "/")


def blocking_claims(targets: list[str]) -> list[dict]:
    targets = [comparable(t) for t in targets]
    if not any("/.worktrees/" in t + "/" for t in targets):
        return []
    hits = [c for c in open_cards()
            if any(inside(t, comparable(c["path"])) for t in targets)]
    if not hits:
        return []
    profiles = dispatch_profiles()
    mine = ancestors()
    claims = []
    for card in hits:
        dispatchable = (card["assignee"] in profiles if profiles is not None
                        else card["assignee"] not in FALLBACK_NON_DISPATCHABLE)
        if dispatchable and not card["pids"] & mine:
            claims.append(card)
    return claims


def reason(claims: list[dict]) -> str:
    lines = []
    for c in claims:
        board = f"hermes kanban --board {c['board']}"
        lines.append(
            f"- {c['id']} on board `{c['board']}` ({c['status']}, assigned {c['assignee']}) owns "
            f"{c['path']}.\n"
            f"  Check it: `{board} show {c['id']}`\n"
            f"  Take it over: `{board} reassign {c['id']} default --reclaim --reason \"taken over by hand\"`\n"
            f"  Hand it back when done: `{board} reassign {c['id']} {c['assignee']}`"
        )
    return (
        "This writes into a kanban card's worktree that a dispatched worker owns or can be given at "
        "any time (a dashboard move is enough). Two writers on one branch end in a force-push over "
        "someone's work. Reassigning to `default` takes the card out of the dispatcher's reach; "
        "--reclaim stops a worker that is running now, so check the card first. Reads are not "
        "affected.\n" + "\n".join(lines)
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        tool = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        cwd = payload.get("cwd") or os.getcwd()
        if not isinstance(tool_input, dict):
            return 0
    except (ValueError, AttributeError):
        return 0
    targets, unparsed_write = tool_targets(tool, tool_input, cwd)
    if unparsed_write:  # cannot tell which commands write: judge every worktree the text names
        cmd = str(tool_input.get("command") or "")
        targets += [resolve(cwd, p) for p in re.findall(
            r"((?:[A-Za-z]:)?[/\\][^\s'\";&|()]*?[/\\]\.worktrees[/\\][^\s'\";&|()/\\]+)", cmd)]
    elif not targets:
        return 0
    claims = blocking_claims(targets)
    if not claims:
        return 0
    text = reason(claims)
    print(json.dumps({"decision": "block", "reason": text}))
    print(text, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
