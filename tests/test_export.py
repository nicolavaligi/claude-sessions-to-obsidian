"""Tests for claude_sessions_to_obsidian. All transcripts and secrets here are invented."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import claude_sessions_to_obsidian as c2o  # noqa: E402


def rec(role, content, ts="2026-03-05T10:00:00Z", sid="11111111-aaaa-bbbb-cccc-000000000001",
        cwd="/work/bakery-site", **extra):
    data = {"type": role, "sessionId": sid, "cwd": cwd, "timestamp": ts,
            "message": {"role": role, "content": content}}
    data.update(extra)
    return data


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.projects = root / "projects"
        self.vault = root / "vault"
        self.out = self.vault / "Claude Sessions"
        self.projects.mkdir()
        self.vault.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def session(self, records, project="-work-bakery-site", name="s1"):
        folder = self.projects / project
        folder.mkdir(exist_ok=True)
        path = folder / (name + ".jsonl")
        path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in records)
                        + "\n", encoding="utf-8")
        return path

    def simple(self, text="Add a contact form to the homepage", **kw):
        return self.session([rec("user", text), rec("assistant", "Done.")], **kw)

    def export(self, **kw):
        return c2o.export(self.projects, self.out, **kw)

    def notes(self):
        return sorted(p.name for p in self.out.glob("*.md")) if self.out.is_dir() else []

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = c2o.main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()


class Parsing(Base):
    def test_string_and_block_content(self):
        path = self.session([
            rec("user", "Fix the footer"),
            rec("assistant", [
                {"type": "thinking", "thinking": "private reasoning"},
                {"type": "text", "text": "Looking at it."},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "footer.html"}},
            ]),
            rec("user", [{"type": "tool_result", "content": "<footer>old</footer>"}]),
            rec("assistant", [{"type": "text", "text": "Fixed."}]),
        ])
        s = c2o.parse_session(path)
        self.assertEqual([r for r, _ in s.messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(s.messages[1][1], "Looking at it.\n\n> **Tool:** `Read`")
        self.assertEqual(s.messages[2][1], "> **Tool result**")
        self.assertNotIn("private reasoning", json.dumps(s.messages))
        self.assertEqual((s.session_id, s.cwd, s.first_user),
                         ("11111111-aaaa-bbbb-cccc-000000000001", "/work/bakery-site",
                          "Fix the footer"))

    def test_tools_modes(self):
        records = [rec("user", "go"),
                   rec("assistant", [{"type": "tool_use", "name": "Bash",
                                      "input": {"command": "ls -la"}}]),
                   rec("user", [{"type": "tool_result",
                                 "content": [{"type": "text", "text": "a.txt\nb.txt"}]}])]
        path = self.session(records)
        self.assertEqual(len(c2o.parse_session(path, "none").messages), 1)
        full = c2o.parse_session(path, "full").messages
        self.assertIn('> {"command": "ls -la"}', full[1][1])
        self.assertIn("> a.txt\n> b.txt", full[2][1])

    def test_long_tool_output_is_truncated(self):
        path = self.session([rec("user", "go"),
                             rec("user", [{"type": "tool_result", "content": "x" * 5000}])])
        text = c2o.parse_session(path, "full").messages[1][1]
        self.assertLess(len(text), 800)
        self.assertIn("[truncated]", text)

    def test_noise_is_skipped(self):
        path = self.session([
            "this line is not json {",
            "",
            "[1, 2, 3]",
            {"type": "summary", "summary": "Footer redesign"},
            rec("user", "internal caveat", isMeta=True),
            rec("assistant", "subagent chatter", isSidechain=True),
            {"type": "system", "content": "hook output"},
            rec("user", "<system-reminder>injected</system-reminder>Real question"),
            rec("assistant", [{"type": "text", "text": ""}]),
        ])
        s = c2o.parse_session(path)
        self.assertEqual(s.messages, [("user", "Real question")])
        self.assertEqual(s.summary, "Footer redesign")

    def test_missing_timestamp_falls_back_to_file_time(self):
        path = self.session([{"type": "user", "message": {"role": "user", "content": "hi"}}])
        s = c2o.parse_session(path)
        self.assertIsNotNone(s.started)
        self.assertEqual(s.session_id, "s1")


class Titles(unittest.TestCase):
    def test_make_title(self):
        t = c2o.make_title
        self.assertEqual(t("fix the bug", "F"), "Fix the bug")
        self.assertEqual(t("<tag>\n/clear\n\n  see https://example.com/x   please. ", "F"),
                         "See please")
        self.assertEqual(t("", "F"), "F")
        self.assertEqual(t("ok", "F"), "F")
        long = t("word " * 40, "F")
        self.assertTrue(long.endswith("…") and len(long) <= 61)

    def test_sanitize_filename(self):
        self.assertEqual(c2o.sanitize_filename('a/b\\c:d*e?f"g<h>i|j#k^l[m]n'),
                         "a-b-c-d-e-f-g-h-i-j-k-l-m-n")
        self.assertEqual(c2o.sanitize_filename("  dots...  "), "dots")
        self.assertLessEqual(len(c2o.sanitize_filename("x" * 500)), 120)

    def test_yaml_and_tags(self):
        self.assertEqual(c2o.yaml_str('Fix: the "main" bug'), '"Fix: the \\"main\\" bug"')
        self.assertEqual(c2o.tag_slug("Bakery Site 2.0"), "bakery-site-2-0")
        self.assertEqual(c2o.tag_slug("///"), "unknown")


class Redaction(unittest.TestCase):
    CASES = {
        "anthropic_key": "sk-ant-api03-" + "A1b2" * 8,
        "api_key": "sk-proj-" + "Zx9" * 10,
        "github_token": "ghp_" + "a1B2" * 9,
        "supabase_key": "sbp_" + "0f" * 20,
        "aws_key": "AKIA" + "ABCD1234EFGH5678",
        "slack_token": "xoxb-1234567890-abcdefghij",
        "stripe_key": "sk_live_" + "9z" * 12,
        "google_api_key": "AIza" + "B" * 35,
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcDEF123_-xyz",
    }

    def test_each_kind(self):
        for kind, secret in self.CASES.items():
            counts = Counter()
            out = c2o.redact("before %s after" % secret, counts)
            self.assertEqual(out, "before [REDACTED:%s] after" % kind, kind)
            self.assertEqual(counts, Counter({kind: 1}), kind)

    def test_github_pat_and_webhook_secret(self):
        self.assertEqual(c2o.redact("github_pat_" + "a1_" * 12), "[REDACTED:github_token]")
        self.assertEqual(c2o.redact("whsec_" + "a1" * 10), "[REDACTED:stripe_key]")

    def test_bearer_and_assignments_keep_the_label(self):
        self.assertEqual(c2o.redact("Authorization: Bearer abcdef0123456789abcdef0123"),
                         "Authorization: Bearer [REDACTED:bearer_token]")
        self.assertEqual(c2o.redact('PASSWORD="hunter2hunter2"'),
                         'PASSWORD="[REDACTED:assigned_secret]"')
        self.assertEqual(c2o.redact("api_key: 0123456789abcdef"),
                         "api_key: [REDACTED:assigned_secret]")

    def test_private_key_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
        self.assertEqual(c2o.redact("key:\n%s\ndone" % pem),
                         "key:\n[REDACTED:private_key]\ndone")

    def test_ordinary_code_is_left_alone(self):
        for text in ("token = get_token()", "password = os.environ.get('PW')",
                     "the secret: patience", "sk-learn is a library", "task-ant-colony",
                     "api_key = settings.API_KEY", "max_tokens: 4096"):
            self.assertEqual(c2o.redact(text), text, text)


class Export(Base):
    def test_note_content(self):
        self.session([rec("user", 'Fix: the "contact" form'), rec("assistant", "Done.")])
        stats = self.export()
        self.assertEqual((stats.written, stats.unchanged), (1, 0))
        name, = self.notes()
        self.assertEqual(name, "2026-03-05 Fix- the -contact- form - 11111111.md")
        text = (self.out / name).read_text(encoding="utf-8")
        head = text.split("---")[1]
        self.assertIn("generator: claude-sessions-to-obsidian\n", head)
        self.assertIn('title: "Fix: the \\"contact\\" form"\n', head)
        self.assertIn('project: "bakery-site"\n', head)
        self.assertIn('cwd: "/work/bakery-site"\n', head)
        self.assertIn("messages: 2\n", head)
        self.assertIn("tags: [claude-session, claude/bakery-site]\n", head)
        self.assertIn('# Fix: the "contact" form\n', text)
        self.assertIn("### User\n\nFix: the \"contact\" form\n\n### Claude\n\nDone.\n", text)

    def test_summary_becomes_title_and_project_falls_back_to_folder(self):
        records = [{"type": "summary", "summary": "Newsletter signup"},
                   {"type": "user", "timestamp": "2026-03-05T10:00:00Z",
                    "message": {"role": "user", "content": "hello there"}}]
        self.session(records, project="-srv-app")
        self.export()
        text = (self.out / self.notes()[0]).read_text(encoding="utf-8")
        self.assertIn('title: "Newsletter signup"', text)
        self.assertIn('project: "srv-app"', text)

    def test_second_run_rewrites_nothing(self):
        self.simple()
        self.export()
        note = self.out / self.notes()[0]
        before = note.stat().st_mtime_ns
        stats = self.export()
        self.assertEqual((stats.written, stats.unchanged), (0, 1))
        self.assertEqual(note.stat().st_mtime_ns, before)
        self.assertEqual([p.name for p in self.out.iterdir()], [note.name])

    def test_changed_session_is_rewritten_in_place(self):
        path = self.simple()
        self.export()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec("user", "One more thing")) + "\n")
        self.assertEqual(self.export().written, 1)
        self.assertEqual(len(self.notes()), 1)
        self.assertIn("One more thing", (self.out / self.notes()[0]).read_text(encoding="utf-8"))

    def test_title_change_renames_instead_of_duplicating(self):
        path = self.simple()
        self.export()
        old, = self.notes()
        path.write_text(json.dumps({"type": "summary", "summary": "Contact form work"}) + "\n"
                        + path.read_text(encoding="utf-8"), encoding="utf-8")
        self.export()
        new, = self.notes()
        self.assertNotEqual(old, new)
        self.assertIn("Contact form work", new)

    def test_never_overwrites_a_note_it_did_not_write(self):
        self.simple()
        self.export()
        name, = self.notes()
        (self.out / name).write_text("my own notes\n", encoding="utf-8")
        stats = self.export()
        self.assertEqual(stats.written, 1)
        self.assertEqual((self.out / name).read_text(encoding="utf-8"), "my own notes\n")
        self.assertEqual(len(self.notes()), 2)
        self.assertTrue(any(n.endswith("(2).md") for n in self.notes()))

    def test_prune_removes_only_generated_orphans(self):
        gone = self.simple(name="gone")
        self.session([rec("user", "Keep this one", sid="22222222-0000-0000-0000-000000000002"),
                      rec("assistant", "ok")], name="kept")
        self.export()
        self.assertEqual(len(self.notes()), 2)
        (self.out / "Handwritten.md").write_text("---\ntitle: mine\n---\nhello\n")
        (self.out / "Plain.md").write_text("mentions generator: claude-sessions-to-obsidian\n")
        gone.unlink()
        self.assertEqual(self.export().pruned, 0)             # not without --prune
        self.assertEqual(len(self.notes()), 4)
        stats = self.export(prune=True)
        self.assertEqual(stats.pruned, 1)
        names = self.notes()
        self.assertEqual(len(names), 3)
        self.assertIn("Handwritten.md", names)
        self.assertIn("Plain.md", names)
        self.assertTrue(any("Keep this one" in n for n in names))

    def test_prune_keeps_notes_of_sessions_filtered_out_this_run(self):
        self.simple()
        self.export()
        self.assertEqual(self.export(prune=True, exclude=["bakery*"]).pruned, 0)
        self.assertEqual(self.export(prune=True, since=date(2030, 1, 1)).pruned, 0)
        self.assertEqual(len(self.notes()), 1)

    def test_dry_run_writes_nothing(self):
        self.simple()
        stats = self.export(dry_run=True)
        self.assertEqual(stats.written, 1)
        self.assertFalse(self.out.exists())

    def test_since_and_filters(self):
        self.simple()
        self.session([rec("user", "Parse the invoices", ts="2026-06-01T08:00:00Z",
                          sid="33333333-0000-0000-0000-000000000003", cwd="/work/invoice-parser"),
                      rec("assistant", "ok")], project="-work-invoice-parser")
        self.assertEqual(self.export(dry_run=True, since=date(2026, 5, 1)).written, 1)
        self.assertEqual(self.export(dry_run=True, include=["invoice*"]).written, 1)
        self.assertEqual(self.export(dry_run=True, exclude=["invoice*", "bakery*"]).written, 0)
        self.assertEqual(self.export(dry_run=True).written, 2)

    def test_redaction_on_by_default(self):
        secret = "ghp_" + "a1B2" * 9
        self.simple("Use this token %s to push" % secret)
        stats = self.export()
        text = (self.out / self.notes()[0]).read_text(encoding="utf-8")
        self.assertNotIn(secret, text + self.notes()[0])
        self.assertIn("[REDACTED:github_token]", text)
        self.assertGreaterEqual(stats.redactions["github_token"], 1)

    def test_no_redact(self):
        secret = "ghp_" + "a1B2" * 9
        self.simple("Use this token %s to push" % secret)
        self.export(do_redact=False)
        self.assertIn(secret, (self.out / self.notes()[0]).read_text(encoding="utf-8"))

    def test_empty_sessions_and_stray_files_are_ignored(self):
        self.session([{"type": "summary", "summary": "nothing said"}], name="empty")
        (self.projects / "loose.jsonl").write_text("{}\n")
        stats = self.export()
        self.assertEqual((stats.written, stats.skipped), (0, 1))


class CommandLine(Base):
    def test_end_to_end_reports_counts_not_content(self):
        secret = "sk-ant-api03-" + "A1b2" * 8
        self.simple("Deploy with key %s now" % secret)
        code, out, err = self.cli("--vault", str(self.vault), "--projects-dir", str(self.projects))
        self.assertEqual(code, 0)
        self.assertIn("wrote 1 note(s), 0 unchanged", out)
        self.assertIn("redacted: anthropic_key x", out)
        self.assertNotIn(secret, out + err)
        self.assertNotIn("Deploy with key", out + err)
        self.assertEqual(len(self.notes()), 1)

    def test_vault_from_environment_and_custom_subdir(self):
        self.simple()
        with mock.patch.dict(os.environ, {c2o.VAULT_ENV: str(self.vault)}):
            code, out, _ = self.cli("--projects-dir", str(self.projects), "--subdir", "AI/Log",
                                    "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would write 1 note(s)", out)
        self.assertFalse((self.vault / "AI").exists())

    def test_errors(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(c2o.VAULT_ENV, None)
            self.assertEqual(self.cli("--projects-dir", str(self.projects))[0], 2)
        self.assertEqual(self.cli("--vault", str(self.vault / "missing"))[0], 2)
        self.assertEqual(self.cli("--vault", str(self.vault), "--projects-dir",
                                  str(self.projects / "missing"))[0], 2)
        self.assertEqual(self.cli("--vault", str(self.vault), "--since", "yesterday")[0], 2)


if __name__ == "__main__":
    unittest.main()
