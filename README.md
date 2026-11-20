# claude-sessions-to-obsidian

Turn your [Claude Code](https://claude.com/claude-code) sessions into Markdown
notes in an [Obsidian](https://obsidian.md) vault — searchable, linkable, and
with pasted credentials masked on the way out.

```console
$ python3 claude_sessions_to_obsidian.py --vault ~/Documents/MyVault
wrote 214 note(s), 0 unchanged, 0 pruned, 9 skipped, 0 unreadable -> ~/Documents/MyVault/Claude Sessions
redacted: assigned_secret x12, github_token x2, jwt x5
```

Run it again whenever you like: only new or changed sessions are rewritten.

## Why

Claude Code keeps every session as a JSON-lines file under
`~/.claude/projects/`. Weeks later you remember *that* you solved something,
not *where*. As notes in your vault those sessions show up in search, in the
graph, and next to everything else you know about the project.

## Quick start

Python 3.9+, no dependencies. Download the single file and run it:

```bash
python3 claude_sessions_to_obsidian.py --vault /path/to/vault --dry-run   # look first
python3 claude_sessions_to_obsidian.py --vault /path/to/vault
```

Or install the `claude2obsidian` command:

```bash
pip install git+https://github.com/nicolavaligi/claude-sessions-to-obsidian
export OBSIDIAN_VAULT=/path/to/vault
claude2obsidian
```

## What a note looks like

`Claude Sessions/2026-03-05 Add a contact form to the homepage - 1a2b3c4d.md`

```markdown
---
generator: claude-sessions-to-obsidian
session_id: "1a2b3c4d-0000-0000-0000-000000000000"
project: "bakery-site"
cwd: "/work/bakery-site"
started: "2026-03-05 11:00"
messages: 4
title: "Add a contact form to the homepage"
tags: [claude-session, claude/bakery-site]
---

# Add a contact form to the homepage

> 2026-03-05 11:00 · bakery-site · 4 messages · `1a2b3c4d`

### User

Add a contact form to the homepage

### Claude

I'll look at the current template first.

> **Tool:** `Read`

### Tool

> **Tool result**

### Claude

Done — the form posts to `/contact`.
```

The title comes from the session summary when there is one, otherwise from the
first thing you typed. Every note is tagged `claude/<project>`, so one tag
gathers all the sessions of a project.

## Options

| Option | Default | |
|---|---|---|
| `--vault PATH` | `$OBSIDIAN_VAULT` | The vault folder. Required. |
| `--subdir NAME` | `Claude Sessions` | Folder inside the vault. |
| `--projects-dir PATH` | `~/.claude/projects` | Where the transcripts are. |
| `--include GLOB` / `--exclude GLOB` | — | Filter by project name, repeatable (`--exclude 'tmp*'`). |
| `--since YYYY-MM-DD` | — | Only sessions started on or after this date. |
| `--tools none\|names\|full` | `names` | Tool calls: omit them, one line each, or with truncated input and output. |
| `--no-redact` | off | Do not mask credentials. |
| `--prune` | off | Delete generated notes whose session no longer exists. |
| `--dry-run` | off | Report what would change, write nothing. |

Model "thinking" blocks, sub-agent chatter and injected system reminders are
always left out.

## It only touches its own notes

Every generated note carries `generator: claude-sessions-to-obsidian` in its
frontmatter. The tool overwrites or deletes a file **only if it has that
marker**:

- a note of yours that happens to have the same file name is never overwritten
  — the export goes to `… (2).md`;
- if you edit a generated note and want to keep your version, delete the
  `generator:` line: from then on it is yours and a fresh copy is written beside it;
- `--prune` removes orphaned *generated* notes and nothing else, and is skipped
  altogether in a run where some transcript could not be read.

## Credential redaction

Transcripts routinely contain secrets: a pasted `.env`, a token in a curl
command, a key in an error message. Vaults are often synced to a cloud. So
masking is **on by default**. Recognised shapes:

Anthropic and OpenAI-style keys, GitHub tokens, Supabase keys, AWS access key
ids, Slack tokens, Stripe keys and webhook secrets, Google API keys, JWTs,
`Bearer` tokens, PEM private key blocks, and `password=` / `secret=` /
`token=` / `api_key=` assignments whose value looks like a secret.

Each becomes `[REDACTED:<kind>]`; the run prints a count per kind and never the
values.

**This is best-effort pattern matching, not a guarantee.** A secret in an
unusual format, a password in a sentence, a customer's personal data: none of
that is caught. If a credential ended up in a session, rotate it.

## Privacy & data

- Everything happens on your machine. The script makes no network calls and
  collects nothing.
- It reads your transcripts and writes Markdown files; it does not modify or
  delete anything under `~/.claude`.
- The notes contain what you and the assistant wrote, including file contents
  and command output quoted in the conversation (with `--tools full`, more of
  it). That may include personal data about other people. Think about that
  before pointing it at a vault that is synced, shared or published.
- Logs and console output contain counts and file names of transcripts, never
  message text.

## Limits

- The transcript format is an internal detail of Claude Code, not a documented
  interface: a future version may change it and this script will need updating.
  Unknown record types are skipped rather than guessed at.
- Notes are regenerated from the transcripts; edits made to a generated note
  are overwritten when its session changes (see above for how to keep one).
- Images and attachments in a session are not exported.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Tests use invented transcripts only. Plain standard-library Python, one file:
it has to run anywhere Claude Code runs, without an install step.

Unofficial tool, not affiliated with Anthropic or Obsidian.

## License

MIT
