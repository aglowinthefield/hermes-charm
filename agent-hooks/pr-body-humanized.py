#!/usr/bin/env python3
"""Pre-tool hook: a PR description must pass the unslop check before it reaches GitHub.

The hook reads the body the command would send and blocks on the mechanical
AI tells from the unslop skill (~/.agents/skills/unslop/SKILL.md, installed with
`npx skills add cursor/plugins --skill unslop -g`): em dashes, curly quotes,
emoji, stock AI vocabulary, filler, chatbot phrases, "not just X, but Y", and
first person, which in a body posted under the operator's account reads as the
operator claiming work an agent did. A clean body passes silently. The block
reason lists each hit by line, so the agent fixes the text rather than
asserting it ran a pass: the old HERMES_HUMANIZED=1 marker was a claim the hook
could not check, and it no longer bypasses anything.

A body the hook cannot read (--fill, --template, a shell variable, piped stdin)
is blocked with a request to pass the text through --body-file instead.

Wired as:
  - Hermes pre_tool_call (matcher: terminal) in config.yaml and each profile
  - Claude Code PreToolUse (matcher: Bash) in ~/.claude/settings.json
  - Codex PreToolUse (matcher: ^Bash$) in ~/.codex/hooks.json
All three send JSON with tool_input.command on stdin and treat exit 2 as a
block: Hermes reads the reason from stdout JSON, Claude Code and Codex from
stderr, so the reason goes to both.

What counts as setting a PR body, wherever `gh` is the executable of a
command segment (after VAR=val prefixes, `env`/`command`/`exec`/`sudo`/...
wrappers, an absolute path, or inside `sh -c`, `eval`, `$(...)`, backticks):
  - gh pr create/edit with --body/-b, --body-file/-F, a heredoc or here-string
  - gh pr create with --fill* or --template/-T (body from commits/template)
  - gh api POST/PATCH to .../pulls or .../pulls/N with a body field or --input
  - gh api graphql calling createPullRequest or updatePullRequest
Text inside a quoted argument (`echo "gh pr create --body x"`) is not a command.
"""

import json
import os
import re
import shlex
import sys

UNSLOP = "~/.agents/skills/unslop/SKILL.md"
SEPARATOR = set(";&|()\n")
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
# wrapper -> its options that consume the next token
WRAPPERS = {
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "time": {"-f", "-o"},
    "env": {"-u", "-C", "--chdir", "--unset", "-S", "--split-string"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
}
PULLS_PATH = re.compile(r"(^|/)pulls(/\d+)?/?$")
HEREDOC = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
CAT_FILE = re.compile(r"^\$\(\s*cat\s+(['\"]?)([^'\"$()<>|;&]+)\1\s*\)$")

# --- the unslop rules a script can check ------------------------------------
# Each entry: (pattern, what it is, the fix). Word lists stay narrow on purpose:
# a false positive stalls an unattended run, so ambiguous technical words
# (surface, vector, primitive, harness, robust, landscape) are left to the skill.
WORDS = (
    "additionally|crucial|delve|delves|delving|enduring|enhance|enhances|enhancing|"
    "fostering|foster|garner|interplay|intricate|intricacies|pivotal|showcase|"
    "showcases|showcasing|tapestry|testament|underscore|underscores|vibrant|"
    "seamless|seamlessly|leverage|leverages|leveraging|utilize|utilizes|utilizing|"
    "facilitate|facilitates|substrate|nexus|locus|paradigm|flywheel"
)
RULES = [
    (re.compile("—"), "em dash", "use a period or a comma (unslop 13)"),
    (re.compile(r"\s–\s"), "spaced en dash", "use a period or a comma (unslop 13)"),
    (re.compile("[“”‘’]"), "curly quote", "use straight quotes (unslop 19)"),
    (
        re.compile("[\U0001f300-\U0001faff☀-➿⭐✅❌]"),
        "emoji",
        "remove it (unslop 18)",
    ),
    (
        re.compile(rf"\b({WORDS})\b", re.I),
        "AI vocabulary",
        "say it with a plain word (unslop 7, 26, 31)",
    ),
    (re.compile(r"\bnorth star\b", re.I), "AI vocabulary", "name the actual goal (unslop 26)"),
    (
        re.compile(
            r"\b(in order to|due to the fact that|it is important to note|it's important to note|"
            r"it is worth noting|it's worth noting|at this point in time|in the event that)\b",
            re.I,
        ),
        "filler phrase",
        "cut it or use the short form (unslop 23)",
    ),
    (
        re.compile(r"\b(serves as|stands as|boasts)\b", re.I),
        "fancy 'is'",
        "say 'is' or 'has' (unslop 8)",
    ),
    (
        re.compile(
            r"(i hope this helps|let me know if|feel free to|great question|certainly!|of course!)",
            re.I,
        ),
        "chatbot phrase",
        "remove it (unslop 20)",
    ),
    (
        re.compile(r"\b(not just|not only|not merely)\b[^.\n]{0,80}\bbut\b", re.I),
        "'not just X, but Y'",
        "state Y directly (unslop 9)",
    ),
    (
        re.compile(r"(?<![\w/.-])(I|I'm|I've|I'd|I'll|[Mm]y)(?![\w/.-])"),
        "first person",
        "the body is posted under the operator's account; name the actor or drop the pronoun",
    ),
]
MASKS = [
    re.compile(r"<!--.*?-->", re.S),  # template comments
    re.compile(r"(^|\n)(`{3,}|~{3,})[^\n]*\n.*?\n\2[^\n]*", re.S),  # fenced code
    re.compile(r"`[^`\n]+`"),  # inline code
    re.compile(r"https?://\S+"),  # URLs
]


def lint(body: str) -> list[str]:
    """Return one message per rule hit, with its line number."""
    for mask in MASKS:  # blank masked spans but keep line numbering
        body = mask.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), body)
    hits = []
    for n, line in enumerate(body.split("\n"), 1):
        quoted = line.lstrip().startswith(">")
        for pattern, what, fix in RULES:
            if quoted and what == "first person":
                continue  # quoting someone else's words
            for m in pattern.finditer(line):
                hits.append(f'line {n}: {what} "{m.group(0).strip()}" -> {fix}')
    return hits


# --- finding the body in the command ------------------------------------------


class Unreadable(Exception):
    """The command sets a PR body the hook cannot see."""


def heredoc_bodies(cmd: str) -> list[str]:
    bodies, pending, current = [], [], []
    for line in cmd.split("\n"):
        if pending:
            strip_tabs, word = pending[0]
            if (line.lstrip("\t") if strip_tabs else line) == word:
                bodies.append("\n".join(current))
                current = []
                pending.pop(0)
            else:
                current.append(line)
            continue
        pending = [(m.group(1) == "-", m.group(3)) for m in HEREDOC.finditer(line)]
    return bodies


def strip_heredoc_bodies(cmd: str) -> str:
    """Drop heredoc body lines; their text is data, not commands, and a stray
    apostrophe in them would break tokenizing. The `<<` operator stays."""
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
    return list(lex)


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
    """Inner commands of $(...) inside a token that shlex kept whole, e.g. "$(gh ...)"."""
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
    """Skip VAR=val prefixes and wrapper commands; return the argv."""
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
            i += 1  # the duration
    return seg[i:]


class Context:
    def __init__(self, cmd: str, cwd: str):
        self.heredocs = heredoc_bodies(cmd)
        self.cwd = cwd

    def read_file(self, path: str) -> str:
        path = os.path.join(self.cwd, os.path.expanduser(path))
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError as e:
            raise Unreadable(f"could not read {path}: {e.strerror}") from e

    def stdin_body(self) -> str:
        if not self.heredocs:
            raise Unreadable("the body comes from piped stdin")
        return "\n".join(self.heredocs)  # several heredocs: check them all

    def literal(self, value: str) -> str:
        if value.startswith("$(cat <<") or value.startswith("$(cat<<"):
            return self.stdin_body()
        m = CAT_FILE.match(value.strip())
        if m:
            return self.read_file(m.group(2))
        if "$" in value:
            raise Unreadable("the body comes from a shell variable or substitution")
        return value


def flag_value(args: list[str], j: int, names: tuple[str, ...]):
    """If args[j] is one of `names` (as `--x v`, `--x=v` or `-xv`), return its value."""
    a = args[j]
    for name in names:
        if a == name:
            return args[j + 1] if j + 1 < len(args) else ""
        if name.startswith("--") and a.startswith(name + "="):
            return a[len(name) + 1 :]
        if not name.startswith("--") and a.startswith(name) and len(a) > len(name):
            return a[len(name) :].lstrip("=")
    return None


def pr_bodies(args: list[str], ctx: Context):
    """Yield each PR body the gh invocation would send. Raise Unreadable when
    it sets one the hook cannot see. Yield nothing when it sets no body."""
    if len(args) >= 2 and args[0] == "pr" and args[1] in ("create", "edit"):
        rest = args[2:]
        for j in range(len(rest)):
            v = flag_value(rest, j, ("--body", "-b"))
            if v is not None:
                yield ctx.literal(v)
            v = flag_value(rest, j, ("--body-file", "-F"))
            if v is not None:
                yield ctx.stdin_body() if v == "-" else ctx.read_file(v)
        flags = {a.split("=", 1)[0] for a in rest}
        if args[1] == "create" and flags & {
            "--fill", "--fill-first", "--fill-verbose", "-f", "--template", "-T"
        }:
            raise Unreadable("--fill/--template builds the body from commits or a template")
        return
    if args[:1] != ["api"]:
        return
    rest = args[1:]
    if "graphql" in rest:
        queries = [a for a in rest if re.search(r"\b(create|update)PullRequest\b", a)]
        for q in queries:
            m = re.search(r'\bbody\s*:\s*"((?:[^"\\]|\\.)*)"', q)
            if m:
                yield json.loads(f'"{m.group(1)}"')
            elif "body" in q:
                raise Unreadable("the GraphQL body is not a literal string")
        return
    paths = [a.split("?", 1)[0] for a in rest if not a.startswith("-") and "/" in a]
    if not any(PULLS_PATH.search(p) for p in paths):
        return
    for j in range(len(rest)):
        v = flag_value(rest, j, ("--input",))
        if v is not None:
            raw = ctx.stdin_body() if v == "-" else ctx.read_file(v)
            try:
                body = json.loads(raw).get("body")
            except (ValueError, AttributeError) as e:
                raise Unreadable("--input is not a JSON object") from e
            if body:
                yield body
        v = flag_value(rest, j, ("-f", "-F", "--field", "--raw-field"))
        if v is not None and v.startswith("body="):
            value = v[len("body=") :]
            reads_file = rest[j].startswith(("-F", "--field"))  # -f/--raw-field keep "@" literal
            if reads_file and value.startswith("@"):
                yield ctx.stdin_body() if value == "@-" else ctx.read_file(value[1:])
            else:
                yield ctx.literal(value)


def check(cmd: str, ctx: Context, depth: int = 0) -> list[str]:
    """Problems with every PR body the command sets (empty list: allow)."""
    if depth > 4:
        return []
    problems: list[str] = []
    for seg in segments(tokenize(cmd)):
        argv = unwrap(seg)
        for tok in seg:
            for inner in substitutions(tok):
                problems += check(inner, ctx, depth + 1)
        if not argv:
            continue
        exe, args = os.path.basename(argv[0]), argv[1:]
        if exe == "cd" and args:
            ctx.cwd = os.path.join(ctx.cwd, os.path.expanduser(args[0]))
        elif exe in SHELLS:
            for j, a in enumerate(args[:-1]):
                if re.fullmatch(r"-[a-z]*c[a-z]*", a):
                    problems += check(args[j + 1], ctx, depth + 1)
                    break
        elif exe == "eval":
            problems += check(" ".join(args), ctx, depth + 1)
        elif exe == "gh":
            try:
                for body in pr_bodies(args, ctx):
                    problems += lint(body)
            except Unreadable as e:
                problems.append(f"UNREADABLE: {e}")
    return problems


def reason(problems: list[str]) -> str:
    unreadable = [p[len("UNREADABLE: ") :] for p in problems if p.startswith("UNREADABLE: ")]
    hits = [p for p in problems if not p.startswith("UNREADABLE: ")]
    parts = []
    if unreadable:
        parts.append(
            "This hook checks every PR description against the unslop rules, and it cannot see "
            f"this one ({'; '.join(unreadable)}). Write the body to a file and pass "
            "`--body-file <path>` (or a literal `--body '...'`), then retry."
        )
    if hits:
        parts.append(
            f"The PR description breaks the unslop rules ({UNSLOP}). Fix each of these, then "
            "retry the same command:\n  " + "\n  ".join(hits)
        )
    parts.append(
        f"Read {UNSLOP} for the rules a script cannot check (plain words, active voice, name the "
        "mechanism or number). For a PR body: keep the repo's PR template and its headings, "
        "bullets are fine, and say what was verified and how, separately from what is inferred."
    )
    return "\n\n".join(parts)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        cmd = payload.get("tool_input", {}).get("command") or ""
    except (ValueError, AttributeError):
        return 0
    if isinstance(cmd, list):  # some hosts send argv
        cmd = shlex.join(cmd)
    if "gh" not in cmd:
        return 0
    ctx = Context(cmd, payload.get("cwd") or os.getcwd())
    try:
        problems = check(cmd, ctx)
    except ValueError:  # unbalanced quotes: decide on the raw text, fail closed
        if not re.search(r"\bgh\b.*\b(pr\s+(create|edit)|api)\b.*\bbody", cmd):
            return 0
        problems = ["UNREADABLE: the command could not be parsed"]
    if not problems:
        return 0
    text = reason(problems)
    print(json.dumps({"decision": "block", "reason": text}))
    print(text, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
