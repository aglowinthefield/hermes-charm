#!/usr/bin/env python3
"""Weekly claim-audit shadow summary (no_agent cron job).

Counts 'claim-audit' comments per board over the last 7 days and all time,
so the operator can decide whether the kanban_task_blocked claim-audit hook
(~/.hermes/agent-hooks/kanban-blocked-claim-audit.sh, shadow mode since
2026-10-04) should be promoted to kick bad blocks back to ready, stay shadow,
or be retired.
"""
import glob
import os
import sqlite3
import time

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
BOARDS = os.environ.get("KANBAN_BOARDS_DIR") or os.path.join(HERMES_HOME, "kanban", "boards")
WEEK_S = 7 * 86400

now = int(time.time())
cutoff = now - WEEK_S

lines = []
total_week = 0
for db_path in sorted(glob.glob(os.path.join(BOARDS, "*", "kanban.db"))):
    board = os.path.basename(os.path.dirname(db_path))
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        week = db.execute(
            "select count(*) from task_comments where author='claim-audit' and created_at>=?",
            (cutoff,),
        ).fetchone()[0]
        total = db.execute(
            "select count(*) from task_comments where author='claim-audit'"
        ).fetchone()[0]
        recent = db.execute(
            "select task_id from task_comments where author='claim-audit' and created_at>=? "
            "order by created_at desc limit 5",
            (cutoff,),
        ).fetchall()
        db.close()
    except sqlite3.Error:
        continue
    if week or total:
        ids = ", ".join(r[0] for r in recent)
        lines.append(f"{board}: {week} flag(s) this week, {total} all time" + (f" ({ids})" if ids else ""))
        total_week += week

if not lines:
    print("claim-audit shadow: no flags ever recorded. The hook has caught nothing; "
          "decide whether to keep it shadow, promote it, or retire it.")
else:
    print(f"claim-audit shadow weekly check: {total_week} flag(s) in the last 7 days.")
    print("\n".join(lines))
    print("Review the flagged cards' block reasons: true fabrications argue for promoting "
          "the hook to kick bad blocks back to ready; false positives argue for tuning "
          "the command regexes in kanban-blocked-claim-audit.sh.")
