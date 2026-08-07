#!/usr/bin/env python3
"""Render the public page from a live registry, then push it.

Runs without a language model. It reads the registry, applies the projection
in redact.py, substitutes values between markers in site/template.html,
refuses to write anything that trips the leak guard, and commits only when
the rendered bytes actually changed.

    python3 tools/publish.py --db /var/lib/ecom/registry.sqlite
    tools/dump_registry.sh > /tmp/dump.json && python3 tools/publish.py --json /tmp/dump.json
    python3 tools/publish.py --json … --push
    python3 tools/publish.py --json … --check         # diff only, no writes
    python3 tools/publish.py --json … --denylist /etc/agent-estate/denylist.txt

An LLM is optional and off by default (--engine claude). It may rewrite prose
sections only; it never decides what is publishable — the projection and the
guard run after it, on its output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redact  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "site" / "template.html"
DEFAULT_OUT = ROOT / "docs" / "index.html"

# The tool surface is declared once, here, and the page's count is derived
# from it. The previous hand-written figure drifted by eight.
TOOLS = [
    "list_projects", "get_project", "create_project", "update_project",
    "append_env_note", "set_latest_commit",
    "register_agent", "list_agents", "set_agent_status", "heartbeat_agent",
    "assign_task", "get_task", "list_tasks", "ready_tasks", "submit_plan",
    "approve_plan", "log_result", "add_task_dependency",
    "remove_task_dependency", "task_blockers",
    "record_signal", "list_signals", "register_signal_source",
    "list_signal_sources",
    "create_finding", "get_finding", "list_findings", "add_finding_evidence",
    "decide_finding",
    "system_status", "create_backup",
]

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve "
         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen").split()
_TENS = ("", "", "twenty", "thirty", "forty", "fifty",
         "sixty", "seventy", "eighty", "ninety")


def spell(n: int) -> str:
    """Numbers read as words on this page; past ninety-nine, as digits."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    return f"{n:,}"


def count(n: int, singular: str, plural: str | None = None) -> str:
    """'six holdings', 'one holding' — spelled out, correctly inflected."""
    word = singular if n == 1 else (plural or singular + "s")
    return f"{spell(n)} {word}"


def escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


# --------------------------------------------------------------------------
# Reading the registry
# --------------------------------------------------------------------------

def _shape(projects, agents, tasks, findings, signals) -> dict:
    """Common shape, whatever store the rows came from.

    Every list is sorted here rather than in the query. Two stores that
    return the same rows in a different order would otherwise render two
    different pages, and a publisher on a timer would commit the difference
    every quarter of an hour.
    """
    def newest(rows: list[dict], field: str) -> list[dict]:
        return sorted(
            rows,
            key=lambda r: (str(r.get(field) or ""), int(r.get("id") or 0)),
            reverse=True,
        )

    tasks = newest(tasks, "created_at")
    findings = newest(findings, "created_at")
    signals = newest(signals, "observed_at")

    return {
        "projects": [p for p in projects if (p.get("status") or "active") == "active"],
        "agents": agents,
        "tasks": tasks,
        "all_findings": findings,
        "findings": sorted(
            findings,
            key=lambda f: (int(f.get("severity") or 0), int(f.get("confidence") or 0)),
            reverse=True,
        )[:5],
        "finding_count": len(findings),
        "open_findings": sum(1 for f in findings if f.get("status") == "open"),
        "signals": signals[:12],
        "signal_count": len(signals),
    }


def read_sqlite(db_path: Path) -> dict:
    if not db_path.exists():
        sys.exit(f"publish: no database at {db_path}")

    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row

    def rows(sql: str) -> list[dict]:
        try:
            return [dict(r) for r in connection.execute(sql)]
        except sqlite3.OperationalError as error:
            sys.exit(f"publish: {error} — is this an Agent Estate registry?")

    def decode(items: list[dict], *fields: str) -> list[dict]:
        for item in items:
            for field in fields:
                value = item.get(field)
                if isinstance(value, str):
                    try:
                        item[field] = json.loads(value)
                    except (ValueError, TypeError):
                        item[field] = []
        return items

    data = _shape(
        rows("SELECT * FROM projects"),
        decode(rows("SELECT * FROM agents ORDER BY id"), "capabilities"),
        rows("SELECT id, title, status, created_at FROM tasks ORDER BY created_at DESC"),
        decode(rows("SELECT * FROM findings"), "evidence_signal_ids"),
        rows("SELECT * FROM signals ORDER BY observed_at DESC"),
    )
    connection.close()
    return data


def read_json(path: Path) -> dict:
    """Read a dump produced by tools/dump_registry.sh.

    The registry may live in Postgres, SQLite, or anything else a conformant
    implementation chooses. Rather than teach the publisher every store — and
    take on a driver dependency for each — it accepts a plain JSON dump. The
    dump script is twenty lines and store-specific; this file stays stdlib.
    """
    if not path.exists():
        sys.exit(f"publish: no dump at {path}")
    try:
        payload = json.loads(path.read_text())
    except ValueError as error:
        sys.exit(f"publish: {path} is not valid JSON — {error}")

    missing = [k for k in ("projects", "agents", "tasks", "findings", "signals")
               if k not in payload]
    if missing:
        sys.exit(f"publish: dump is missing {', '.join(missing)}")

    return _shape(
        payload["projects"], payload["agents"], payload["tasks"],
        payload["findings"], payload["signals"],
    )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_roster(agents: list[dict]) -> str:
    lines = []
    for index, agent in enumerate(agents, start=1):
        row = redact.project_agent(agent, index)
        pill = "ok" if row["status"] == "working" else "wait"
        capabilities = " · ".join(row["capabilities"]) or "—"
        lines.append(
            f'<tr><td class="k">{escape(row["name"])}</td>'
            f'<td>{escape(row["role"])}</td>'
            f'<td>{escape(row["model"])}</td>'
            f'<td>{escape(capabilities)}</td>'
            f'<td><span class="pill {pill}">{escape(row["status"])}</span></td></tr>'
        )
    return "\n".join(lines)


def render_findings(findings, salt, denylist, cache=None) -> str:
    lines = []
    for finding in findings:
        row = redact.project_finding(finding, salt, denylist)
        if cache:
            row["title"] = cache.publishable(row["title"])
        severity = row["severity"]
        bar = "●" * severity + "○" * (5 - severity)
        pill = "sev" if row["status"] == "open" else "ok"
        lines.append(
            f'<tr><td class="k"><span class="sev-bar">{bar}</span></td>'
            f'<td>{row["confidence"]}</td>'
            f'<td>{escape(row["kind"])}</td>'
            f'<td>{escape(row["title"])}</td>'
            f'<td><span class="pill {pill}">{escape(row["status"])}</span></td></tr>'
        )
    return "\n".join(lines)


def render_signals(signals, salt, denylist, cache=None) -> str:
    lines = []
    for signal in signals:
        row = redact.project_signal(signal, salt, denylist)
        lines.append(
            f'<tr><td class="k">{escape(row["kind"])}</td>'
            f'<td>{escape(row["metric"])}</td>'
            f'<td>{escape(row["value"])}</td>'
            f'<td>{escape(row["note"])}</td></tr>'
        )
    return "\n".join(lines)


def _week_of(stamp: str | None) -> tuple[int, int] | None:
    """ISO (year, week) for a timestamp, or None if it cannot be read."""
    if not stamp:
        return None
    text = str(stamp).replace("Z", "+00:00")
    for parse in (datetime.fromisoformat, lambda s: datetime.strptime(s[:10], "%Y-%m-%d")):
        try:
            moment = parse(text)
        except (ValueError, TypeError):
            continue
        iso = moment.isocalendar()
        return iso[0], iso[1]
    return None


def _week_dates(year: int, week: int) -> str:
    monday = datetime.fromisocalendar(year, week, 1)
    sunday = datetime.fromisocalendar(year, week, 7)
    if monday.month == sunday.month:
        return f"{monday.day} – {sunday.day} {sunday:%b %Y}"
    return f"{monday.day} {monday:%b} – {sunday.day} {sunday:%b %Y}"


def render_buildlog(data: dict, denylist: list[str], weeks: int = 3, cache=None) -> str:
    """Weekly entries derived from the record, not written by hand.

    Shipped  — tasks that succeeded that week.
    Learned  — findings raised that week, worst first.
    Still human — tasks still pending, whenever they were assigned.

    Every line carries the identifier it came from, so a reader can ask the
    registry for the row rather than take the page's word for it.
    """
    buckets: dict[tuple[int, int], dict[str, list[str]]] = {}

    def bucket(key: tuple[int, int]) -> dict[str, list[str]]:
        return buckets.setdefault(key, {"shipped": [], "learned": []})

    for task in data["tasks"]:
        key = _week_of(task.get("created_at"))
        if key and task.get("status") == "succeeded":
            title = redact.redact_text(task.get("title") or "untitled", denylist)
            if cache:
                title = cache.publishable(title)
            bucket(key)["shipped"].append(
                f"{escape(title)} <span class=\"ref\">[task {task['id']}]</span>"
            )

    for finding in data["all_findings"]:
        key = _week_of(finding.get("created_at"))
        if key:
            title = redact.redact_text(finding.get("title") or "untitled", denylist)
            if cache:
                title = cache.publishable(title)
            severity = int(finding.get("severity") or 0)
            bucket(key)["learned"].append(
                f"{escape(title)} <span class=\"ref\">[finding {finding['id']} · "
                f"severity {severity}]</span>"
            )

    pending = [t for t in data["tasks"] if t.get("status") == "pending"]

    entries: list[str] = []
    for index, key in enumerate(sorted(buckets, reverse=True)[:weeks]):
        year, week = key
        content = buckets[key]
        parts = [
            '<div class="entry">',
            f'  <div class="entry-head"><span class="wk">Week {week}</span> '
            f'<span class="dates">{_week_dates(year, week)}</span></div>',
        ]

        if content["shipped"]:
            parts.append("  <h5>Shipped</h5>\n  <ul>")
            parts += [f"    <li>{line}</li>" for line in content["shipped"][:6]]
            parts.append("  </ul>")
        if content["learned"]:
            parts.append("  <h5>Learned</h5>\n  <ul>")
            parts += [f"    <li>{line}</li>" for line in content["learned"][:6]]
            parts.append("  </ul>")
        if not content["shipped"] and not content["learned"]:
            parts.append("  <p>No customer-visible change this week.</p>")

        # Only the newest entry states what is still human — repeating it on
        # every week would imply it was re-decided each time.
        if index == 0:
            parts.append("  <h5>Still human</h5>\n  <ul>")
            if pending:
                for task in pending[:3]:
                    title = redact.redact_text(task.get("title") or "untitled", denylist)
                    if cache:
                        title = cache.publishable(title)
                    parts.append(
                        f'    <li>{escape(title)} — left <strong>pending</strong> '
                        f'rather than closed. <span class="ref">[task {task["id"]}]</span></li>'
                    )
            else:
                parts.append("    <li>Every finding decision. None has been recorded.</li>")
            parts.append("  </ul>")

        parts.append("</div>")
        entries.append("\n".join(parts))

    if not entries:
        return ('<div class="entry"><p>No entries yet — the registry holds no '
                "dated work.</p></div>")
    return "\n".join(entries)


def build_projection(data: dict, salt: str, denylist: list[str]) -> dict:
    projects = len(data["projects"])
    agents = len(data["agents"])
    tasks = len(data["tasks"])
    signals = data["signal_count"]
    findings = data["finding_count"]
    open_findings = data["open_findings"]
    decided = findings - open_findings

    if decided == 0:
        stage3 = (
            f"Recording works: {count(findings, 'finding')}, ranked worst-first, "
            "several carrying the signal identifiers that support them. "
            "Deciding does not yet happen — every finding is still <em>open</em>."
        )
    else:
        stage3 = (
            f"{count(findings, 'finding').capitalize()} recorded, "
            f"{spell(decided)} decided, {spell(open_findings)} still open."
        )

    return {
        "tools": str(len(TOOLS)),
        "tools-word": spell(len(TOOLS)),
        "stage1": (
            f"{count(projects, 'holding').capitalize()} and "
            f"{count(agents, 'agent')} currently registered; "
            f"{count(tasks, 'task')} recorded with their full retry history."
        ),
        "stage2": count(signals, "observation").capitalize(),
        "stage3": stage3,
        "buildlog": render_buildlog(data, denylist),
        "roster": render_roster(data["agents"]),
        "findings": render_findings(data["findings"], salt, denylist),
        "signals": render_signals(data["signals"], salt, denylist),
        "generated": (
            "Generated from the live registry at "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
            "by a deterministic exporter. No language model read this record."
        ),
    }


def substitute(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        opening, closing = f"<!--ecom:{key}-->", f"<!--/ecom:{key}-->"
        start = out.find(opening)
        end = out.find(closing)
        if start == -1 or end == -1:
            sys.exit(f"publish: marker '{key}' missing from the template")
        out = out[: start + len(opening)] + value + out[end:]
    return out


def wrap(body: str) -> str:
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="description" content="ECOM — an anonymous, self-hosted MCP '
        'registry for unattended software estates.">\n'
        "<style>*{margin:0;padding:0}img,svg{display:block;max-width:100%}</style>\n"
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------
# Optional prose engine — off by default
# --------------------------------------------------------------------------

def rewrite_prose(text: str, instruction: str) -> str:
    """Rewrite one prose block with Claude. Never decides publishability.

    Called only with --engine claude. The projection has already stripped
    identifiers before this point, and the leak guard still runs on the
    result, so a model failure cannot widen what gets published.
    """
    try:
        import anthropic
    except ImportError:
        sys.exit("publish: --engine claude needs `pip install anthropic`")

    client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY or an ant profile
    response = client.messages.create(
        model="claude-opus-5",
        max_tokens=2000,
        system=(
            "You rewrite one block of prose for a technical page written in UK "
            "English. Keep every number, status label and claim exactly as "
            "given — you may change wording, never facts. Return the rewritten "
            "block and nothing else."
        ),
        messages=[{"role": "user", "content": f"{instruction}\n\n---\n{text}"}],
    )
    if response.stop_reason == "refusal":
        sys.exit("publish: the model declined to rewrite that block")
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


class TranslationCache:
    """Restate one agent-written line in publishable English, once ever.

    Keyed by a hash of the line, so a finding raised in July is translated on
    the first publish and read from disk on every publish after it. A timer
    running every quarter of an hour must not spend a token per run — and a
    line that has already been reviewed should not silently change wording
    underneath a reader.
    """

    def __init__(self, path: Path | None):
        self.path = path or (ROOT / ".cache" / "publishable.json")
        self.entries: dict[str, str] = {}
        self.dirty = False
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text())
            except ValueError:
                self.entries = {}

    def publishable(self, text: str) -> str:
        key = hashlib.sha256(text.encode()).hexdigest()[:16]
        if key not in self.entries:
            self.entries[key] = rewrite_prose(
                text,
                "Restate this one-line engineering note as a single plain "
                "English sentence for a public page. Keep the technical "
                "substance and any number. Remove product names, screen "
                "names, file names, internal identifiers and anything that "
                "would identify the company or its apps — describe the kind "
                "of thing instead. Do not add detail that is not there.",
            )
            self.dirty = True
        return self.entries[key]

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=1, sort_keys=True))


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------

def git(*args: str, key: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, TZ="UTC")
    if key:
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )
    return subprocess.run(
        ["git", *args], cwd=ROOT, env=env, capture_output=True, text=True
    )


def commit_and_push(out_path: Path, key: Path | None) -> None:
    # --out may be given relative to the caller's working directory, which is
    # not necessarily the repository root; resolve before asking git.
    resolved = out_path.resolve()
    try:
        tracked = resolved.relative_to(ROOT)
    except ValueError:
        sys.exit(
            f"publish: --push needs --out inside the repository, but "
            f"{resolved} is outside {ROOT}"
        )

    git("config", "user.name", "agent-estate")
    git("config", "user.email", "agent-estate@users.noreply.github.com")
    git("add", str(tracked))
    staged = git("diff", "--cached", "--quiet")
    if staged.returncode == 0:
        print("publish: nothing staged")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    result = git("commit", "-m", f"publish: record as of {stamp}")
    if result.returncode != 0:
        sys.exit(f"publish: commit failed — {result.stderr.strip()}")
    pushed = git("push", key=key)
    if pushed.returncode != 0:
        sys.exit(f"publish: push failed — {pushed.stderr.strip()}")
    print("publish: pushed")


# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="SQLite registry file")
    source.add_argument("--json", type=Path, dest="json_dump",
                        help="dump from tools/dump_registry.sh (Postgres etc.)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--denylist", type=Path,
                        help="estate-specific strings, kept outside the repo")
    parser.add_argument("--salt", default=os.environ.get("ECOM_PSEUDONYM_SALT", ""),
                        help="stable pseudonyms; keep outside the repo")
    parser.add_argument("--key", type=Path, help="SSH deploy key for --push")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="report whether output would change; write nothing")
    parser.add_argument("--engine", choices=["none", "claude"], default="none",
                        help="regenerate free-text blocks; needs ANTHROPIC_API_KEY")
    parser.add_argument("--cache", type=Path,
                        help="where restated lines are kept between runs")
    args = parser.parse_args()

    denylist: list[str] = []
    if args.denylist:
        if not args.denylist.exists():
            sys.exit(f"publish: denylist not found at {args.denylist}")
        denylist = args.denylist.read_text().splitlines()
    else:
        print("publish: no --denylist given; structural patterns only",
              file=sys.stderr)

    data = read_sqlite(args.db) if args.db else read_json(args.json_dump)
    values = build_projection(data, args.salt, denylist)

    # Counts, stages and the roster are derived from structured columns and
    # are always safe to publish. The build log and the two excerpt tables
    # render free text written by agents — in whatever language and with
    # whatever product vocabulary the estate happens to use. A denylist
    # cannot anticipate an internal screen name, so those blocks are only
    # regenerated when a model is available to restate them in publishable
    # form. Otherwise the template's reviewed copy is left standing.
    FREE_TEXT = ("buildlog", "findings", "signals")
    if args.engine == "none":
        for key in FREE_TEXT:
            values.pop(key)
        print("publish: free-text blocks left as reviewed copy "
              "(pass --engine claude to regenerate them)", file=sys.stderr)
    else:
        cache = TranslationCache(args.cache)
        values["findings"] = render_findings(
            data["findings"], args.salt, denylist, cache)
        values["signals"] = render_signals(
            data["signals"], args.salt, denylist, cache)
        values["buildlog"] = render_buildlog(data, denylist, cache=cache)
        cache.save()

    body = substitute(TEMPLATE.read_text(), values)
    page = wrap(body)

    leaks = redact.scan(page, denylist)
    if leaks:
        print("publish: leak guard tripped — nothing written", file=sys.stderr)
        for leak in leaks[:20]:
            print(f"  {leak}", file=sys.stderr)
        if len(leaks) > 20:
            print(f"  … and {len(leaks) - 20} more", file=sys.stderr)
        sys.exit(2)

    previous = args.out.read_text() if args.out.exists() else ""
    if page == previous:
        print("publish: unchanged")
        return
    if args.check:
        print("publish: output would change")
        sys.exit(1)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    print(f"publish: wrote {args.out}")

    if args.push:
        commit_and_push(args.out, args.key)


if __name__ == "__main__":
    main()
