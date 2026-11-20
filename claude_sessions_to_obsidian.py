#!/usr/bin/env python3
"""Export Claude Code session transcripts to Markdown notes in an Obsidian vault.

Claude Code keeps one JSON-lines file per session under ~/.claude/projects/.
This script turns each of them into a note with YAML frontmatter, so past
sessions become searchable and linkable from your vault.

    python3 claude_sessions_to_obsidian.py --vault ~/Documents/MyVault

Safe by design: it only ever overwrites or deletes notes that it generated
itself, and it masks common credentials before writing (see --no-redact).

Standard library only. No network access.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

__version__ = "0.1.0"

log = logging.getLogger("claude2obsidian")

GENERATOR = "claude-sessions-to-obsidian"
MARKER = "generator: " + GENERATOR
VAULT_ENV = "OBSIDIAN_VAULT"
TOOL_TEXT_LIMIT = 600

# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

_SECRET = r"(?=[A-Za-z0-9+/_.=\-]*[0-9])(?=[A-Za-z0-9+/_.=\-]*[A-Za-z])[A-Za-z0-9+/_.=\-]{12,}"

# (kind, pattern, replacement). Order matters: specific shapes first.
REDACTIONS: Tuple[Tuple[str, "re.Pattern[str]", str], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE "
                               r"KEY-----|\Z)", re.S), "[REDACTED:private_key]"),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED:anthropic_key]"),
    ("stripe_key", re.compile(r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,}"),
     "[REDACTED:stripe_key]"),
    ("api_key", re.compile(r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
     "[REDACTED:api_key]"),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"),
     "[REDACTED:github_token]"),
    ("supabase_key", re.compile(r"(?:sbp|sb_secret)_[A-Za-z0-9_]{20,}"),
     "[REDACTED:supabase_key]"),
    ("aws_key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
     "[REDACTED:aws_key]"),
    ("slack_token", re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack_token]"),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "[REDACTED:google_api_key]"),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
     "[REDACTED:jwt]"),
    ("bearer_token", re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/\-]{20,}=*"),
     r"\1[REDACTED:bearer_token]"),
    ("assigned_secret", re.compile(
        r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)[\"']?\s*[:=]\s*"
        r"[\"']?)" + _SECRET), r"\1[REDACTED:assigned_secret]"),
)


def redact(text: str, counts: Optional[Counter] = None) -> str:
    """Mask common credential shapes. Best effort: pattern matching, not a guarantee."""
    for kind, pattern, replacement in REDACTIONS:
        text, n = pattern.subn(replacement, text)
        if n and counts is not None:
            counts[kind] += n
    return text


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

@dataclass
class Session:
    path: Path
    session_id: str
    cwd: str = ""
    started: Optional[datetime] = None
    summary: str = ""
    first_user: str = ""
    messages: List[Tuple[str, str]] = field(default_factory=list)   # (role, text)


_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def _clip(value, limit: int = TOOL_TEXT_LIMIT) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " ...[truncated]"


def _quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def extract(content, tools: str = "names") -> Tuple[str, bool]:
    """Readable text of a message, and whether it consists only of tool traffic."""
    if isinstance(content, str):
        return _REMINDER.sub("", content).strip(), False
    if not isinstance(content, list):
        return "", False
    parts: List[str] = []
    spoke = False
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            spoke = True
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = _REMINDER.sub("", block.get("text") or "").strip()
            if text:
                parts.append(text)
                spoke = True
        elif kind == "tool_use" and tools != "none":
            line = "> **Tool:** `%s`" % str(block.get("name", "?")).replace("`", "'")
            if tools == "full" and block.get("input") not in (None, {}, ""):
                line += "\n" + _quote(_clip(block.get("input")))
            parts.append(line)
        elif kind == "tool_result" and tools != "none":
            line = "> **Tool result**"
            body = _result_text(block.get("content")) if tools == "full" else ""
            if body.strip():
                line += "\n" + _quote(_clip(body))
            parts.append(line)
        # "thinking" and unknown block types are left out on purpose
    return "\n\n".join(parts), bool(parts) and not spoke


def _timestamp(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone() if parsed.tzinfo else parsed


def parse_session(path: Path, tools: str = "names") -> Session:
    session = Session(path=path, session_id=path.stem)
    found_id = False
    with path.open(encoding="utf-8", errors="replace") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.debug("%s: line %d is not valid JSON, skipped", path.name, number)
                continue
            if not isinstance(rec, dict):
                continue
            if not found_id and isinstance(rec.get("sessionId"), str):
                session.session_id, found_id = rec["sessionId"], True
            if not session.cwd and isinstance(rec.get("cwd"), str):
                session.cwd = rec["cwd"]
            if rec.get("type") == "summary" and isinstance(rec.get("summary"), str):
                session.summary = session.summary or rec["summary"].strip()
                continue
            if rec.get("isMeta") or rec.get("isSidechain"):
                continue
            message = rec.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role") or rec.get("type")
            if role not in ("user", "assistant"):
                continue
            if session.started is None:
                session.started = _timestamp(rec.get("timestamp"))
            text, only_tools = extract(message.get("content"), tools)
            if not text.strip():
                continue
            if only_tools:
                role = "tool"
            elif role == "user" and not session.first_user:
                session.first_user = text
            session.messages.append((role, text))
    if session.started is None:
        session.started = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return session


# ---------------------------------------------------------------------------
# note building
# ---------------------------------------------------------------------------

def make_title(source: str, fallback: str) -> str:
    """A readable one-line title from the first meaningful line of `source`."""
    for raw in (source or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("<", "/", "#", "```", "!", "[", ">")):
            continue
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"\s+", " ", line).strip(" .,:;-")
        if len(line) < 3:
            continue
        if len(line) > 60:
            line = line[:60].rsplit(" ", 1)[0] + "…"
        return line[0].upper() + line[1:]
    return fallback


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:120].rstrip(" .")


def project_name(session: Session, folder_name: str) -> str:
    if session.cwd:
        return Path(session.cwd).name or session.cwd
    return folder_name.strip("-") or folder_name


def tag_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"


def yaml_str(value: str) -> str:
    """A YAML double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


_ROLE_HEADING = {"user": "### User", "assistant": "### Claude", "tool": "### Tool"}


def build_note(session: Session, project: str, do_redact: bool,
               counts: Optional[Counter] = None) -> Tuple[str, str]:
    """(title, full note text)."""
    clean = (lambda t: redact(t, counts)) if do_redact else (lambda t: t)
    short = session.session_id[:8]
    title = clean(make_title(session.summary or session.first_user, "Session " + short))
    started = session.started.strftime("%Y-%m-%d %H:%M") if session.started else ""
    lines = [
        "---",
        MARKER,
        "session_id: " + yaml_str(session.session_id),
        "project: " + yaml_str(project),
        "cwd: " + yaml_str(session.cwd),
        "started: " + yaml_str(started),
        "messages: %d" % len(session.messages),
        "title: " + yaml_str(title),
        "tags: [claude-session, claude/%s]" % tag_slug(project),
        "---",
        "",
        "# " + title,
        "",
        "> %s · %s · %d messages · `%s`" % (started, project, len(session.messages), short),
        "",
    ]
    for role, text in session.messages:
        lines += [_ROLE_HEADING.get(role, "### " + role), "", clean(text), ""]
    return title, "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# vault handling
# ---------------------------------------------------------------------------

def read_marker(path: Path) -> Optional[str]:
    """session_id of a note generated by this tool, '' if generated without one, else None."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if not head.startswith("---"):
        return None
    end = head.find("\n---", 3)
    front = head[3:end] if end != -1 else head[3:]
    if not any(l.strip() == MARKER for l in front.splitlines()):
        return None
    for line in front.splitlines():
        if line.startswith("session_id:"):
            raw = line.split(":", 1)[1].strip()
            try:
                return str(json.loads(raw))
            except (json.JSONDecodeError, TypeError):
                return raw.strip("\"'")
    return ""


def index_generated(out_dir: Path) -> Dict[str, Path]:
    """session_id -> note path, for the notes in `out_dir` that this tool wrote."""
    found: Dict[str, Path] = {}
    if out_dir.is_dir():
        for path in sorted(out_dir.glob("*.md")):
            sid = read_marker(path)
            if sid:
                found[sid] = path
    return found


def free_path(out_dir: Path, base: str, own: Optional[Path], taken: set) -> Path:
    """A path for `base`.md that does not collide with a foreign file or another new note."""
    n = 1
    while True:
        candidate = out_dir / ("%s%s.md" % (base, "" if n == 1 else " (%d)" % n))
        key = candidate.name.lower()
        if key not in taken and (not candidate.exists() or candidate == own):
            taken.add(key)
            return candidate
        n += 1


def matches(names: Sequence[str], patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(n.lower(), p.lower()) for n in names for p in patterns)


@dataclass
class Stats:
    written: int = 0
    unchanged: int = 0
    pruned: int = 0
    skipped: int = 0
    failed: int = 0
    redactions: Counter = field(default_factory=Counter)


def export(projects_dir: Path, out_dir: Path, *, tools: str = "names", do_redact: bool = True,
           include: Sequence[str] = (), exclude: Sequence[str] = (),
           since: Optional[date] = None, dry_run: bool = False, prune: bool = False) -> Stats:
    stats = Stats()
    if not projects_dir.is_dir():
        raise FileNotFoundError("projects directory not found: %s" % projects_dir)
    existing = index_generated(out_dir)
    taken: set = set()
    seen_ids: set = set()
    complete = True          # False if any session could not be read: then never prune

    for proj_dir in sorted(p for p in projects_dir.iterdir() if p.is_dir()):
        for source in sorted(proj_dir.glob("*.jsonl")):
            try:
                session = parse_session(source, tools)
            except OSError as exc:
                log.warning("cannot read %s: %s", source.name, exc.strerror or exc)
                stats.failed += 1
                complete = False
                continue
            if not session.messages:
                stats.skipped += 1
                continue
            # filtered out below or not, the session still exists: its note is no orphan
            seen_ids.add(session.session_id)
            project = project_name(session, proj_dir.name)
            names = [project, proj_dir.name, session.cwd]
            if (include and not matches(names, include)) or (exclude and matches(names, exclude)):
                stats.skipped += 1
                continue
            if since and session.started and session.started.date() < since:
                stats.skipped += 1
                continue

            title, text = build_note(session, project, do_redact, stats.redactions)
            day = session.started.strftime("%Y-%m-%d") if session.started else "undated"
            base = sanitize_filename("%s %s" % (day, title.rstrip("…"))) or day
            base = "%s - %s" % (base, session.session_id[:8])
            own = existing.get(session.session_id)
            target = free_path(out_dir, base, own, taken)

            if own == target and target.read_text(encoding="utf-8", errors="replace") == text:
                stats.unchanged += 1
                continue
            stats.written += 1
            if dry_run:
                continue
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
            if own and own != target and own.exists():
                own.unlink()                           # same session, title changed

    if prune and not complete:
        log.warning("some sessions could not be read: skipping --prune this time")
    elif prune:
        for sid, path in existing.items():
            if sid not in seen_ids:
                stats.pruned += 1
                if not dry_run:
                    path.unlink()
    return stats


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="claude2obsidian",
        description="Export Claude Code sessions to Markdown notes in an Obsidian vault.")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("--vault", metavar="PATH",
                    help="Obsidian vault folder (or set %s)" % VAULT_ENV)
    ap.add_argument("--subdir", default="Claude Sessions",
                    help="folder inside the vault (default: %(default)s)")
    ap.add_argument("--projects-dir", metavar="PATH", default="~/.claude/projects",
                    help="where Claude Code keeps transcripts (default: %(default)s)")
    ap.add_argument("--include", action="append", default=[], metavar="GLOB",
                    help="only projects matching this pattern (repeatable)")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                    help="skip projects matching this pattern (repeatable)")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="only sessions started on or after")
    ap.add_argument("--tools", choices=("none", "names", "full"), default="names",
                    help="how much of tool calls to keep (default: %(default)s)")
    ap.add_argument("--no-redact", action="store_true",
                    help="do not mask credentials (not recommended for synced vaults)")
    ap.add_argument("--prune", action="store_true",
                    help="delete generated notes whose session no longer exists")
    ap.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, format="%(levelname)s: %(message)s", force=True,
                        level=logging.DEBUG if args.verbose else logging.WARNING)
    vault = args.vault or os.environ.get(VAULT_ENV)
    if not vault:
        ap.error("no vault given: use --vault PATH or set %s" % VAULT_ENV)
    vault_path = Path(vault).expanduser()
    if not vault_path.is_dir():
        print("error: vault folder not found: %s" % vault_path, file=sys.stderr)
        return 2
    since = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            ap.error("--since must be a date like 2026-01-31")
    try:
        stats = export(Path(args.projects_dir).expanduser(), vault_path / args.subdir,
                       tools=args.tools, do_redact=not args.no_redact, include=args.include,
                       exclude=args.exclude, since=since, dry_run=args.dry_run, prune=args.prune)
    except FileNotFoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    verb = "would write" if args.dry_run else "wrote"
    print("%s %d note(s), %d unchanged, %d pruned, %d skipped, %d unreadable -> %s"
          % (verb, stats.written, stats.unchanged, stats.pruned, stats.skipped, stats.failed,
             vault_path / args.subdir))
    if stats.redactions:
        print("redacted: " + ", ".join("%s x%d" % kv for kv in sorted(stats.redactions.items())))
    return 1 if stats.failed else 0


if __name__ == "__main__":
    sys.exit(main())
