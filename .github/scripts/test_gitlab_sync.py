"""Tests for gitlab_sync.py: python3 -m unittest discover -s .github/scripts"""

import datetime as dt
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import gitlab_sync as gs

GH_WEB = "https://github.com/2SCloud/demo"
GL_WEB = "https://gitlab.com/2scloud/demo"
T0 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def quiet(fn, *args):
    with redirect_stdout(io.StringIO()) as out:
        result = fn(*args)
    return result, out.getvalue()


class TextTest(unittest.TestCase):
    def test_render_round_trip(self):
        body = "Hi @alice, mail a@b.com\n```\n@code\n```\nsee `@x` and (@bob)"
        copy = gs.render(body, ("carol", "https://github.com/carol"), f"{GH_WEB}/issues/3#issuecomment-9", gs.GITHUB)
        self.assertTrue(copy.endswith(f"Created by [@{gs.WORD_JOINER}carol](https://github.com/carol) on GitHub "
                                      f"({GH_WEB}/issues/3#issuecomment-9)"))
        self.assertIn(f"Hi @{gs.WORD_JOINER}alice", copy)
        self.assertIn(f"(@{gs.WORD_JOINER}bob)", copy)
        self.assertIn("a@b.com", copy)
        self.assertIn("\n@code\n", copy)
        self.assertIn("`@x`", copy)
        self.assertEqual(gs.split_footer(copy)[1:], (gs.GITHUB, f"{GH_WEB}/issues/3#issuecomment-9"))
        self.assertEqual(gs.unrender(copy), body)
        self.assertEqual(gs.split_footer("no footer\nhere"), ("no footer\nhere", None, None))

    def test_empty_body_and_loose(self):
        copy = gs.render("", ("x", "https://gitlab.com/x"), f"{GL_WEB}/-/issues/1", gs.GITLAB)
        self.assertEqual(gs.unrender(copy), "")
        self.assertEqual(gs.loose("Hi `@alice`  there"), gs.loose("Hi @alice there"))


# ---- code -------------------------------------------------------------------------


class CodeSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, GIT_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.gh = os.path.join(self.tmp.name, "gh.git")
        self.gl = os.path.join(self.tmp.name, "gl.git")
        self.work = os.path.join(self.tmp.name, "work")
        for path in (self.gh, self.gl):
            self.git("init", "-q", "--bare", path)
        self.git("config", "receive.advertisePushOptions", "true", cwd=self.gl)
        self.git("init", "-q", self.work)
        self.commit("initial")
        for remote in (self.gh, self.gl):
            self.git("push", "-q", remote, "HEAD:refs/heads/main", "HEAD:refs/heads/develop", cwd=self.work)
        self.state = {}

    def git(self, *args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, msg, parent=None):
        if parent:
            self.git("checkout", "-q", "--detach", parent, cwd=self.work)
        self.git("commit", "-q", "--allow-empty", "-m", msg, cwd=self.work)
        return self.git("rev-parse", "HEAD", cwd=self.work)

    def ref(self, remote, ref):
        out = self.git("for-each-ref", "--format=%(objectname)", ref, cwd=remote)
        return out or None

    def run_sync(self):
        sync = gs.Sync(dry_run=False)
        sync.git_auth = {gs.GITHUB: None, gs.GITLAB: None}
        ctx = gs.RepoContext("demo", {"clone_url": self.gh, "full_name": "2SCloud/demo"},
                             {"http_url_to_repo": self.gl, "id": 1}, None, self.state, None, None,
                             known_problems=set(self.state.get("problems", [])))
        with mock.patch.object(sync, "fork_branches", return_value=({gs.GITHUB: {}, gs.GITLAB: {}},
                                                                     {gs.GITHUB: [], gs.GITLAB: []})):
            _, out = quiet(sync.sync_code, ctx)
        self.state["problems"] = ctx.problems
        return sync, out

    def test_commits_copied_one_by_one(self):
        self.run_sync()
        main = self.ref(self.gh, "refs/heads/main")
        for i in range(6):
            main = self.commit(f"github {i}", main)
        self.git("push", "-q", self.gh, f"{main}:refs/heads/main", cwd=self.work)
        self.run_sync()
        self.assertEqual(self.ref(self.gl, "refs/heads/main"), main)
        self.assertEqual(self.git("rev-list", "--count", "main", cwd=self.gl), "7")

        for i in range(3):
            main = self.commit(f"gitlab {i}", main)
        self.git("push", "-q", self.gl, f"{main}:refs/heads/main", cwd=self.work)
        self.run_sync()
        self.assertEqual(self.ref(self.gh, "refs/heads/main"), main)
        self.assertEqual(self.git("rev-list", "--count", "main", cwd=self.gh), "10")

    def test_new_and_deleted_refs_both_ways(self):
        self.run_sync()
        feature = self.commit("feature", self.ref(self.gl, "refs/heads/main"))
        self.git("push", "-q", self.gl, f"{feature}:refs/heads/feat/x", f"{feature}:refs/tags/v1", cwd=self.work)
        self.run_sync()
        self.assertEqual(self.ref(self.gh, "refs/heads/feat/x"), feature)
        self.assertEqual(self.ref(self.gh, "refs/tags/v1"), feature)

        self.git("push", "-q", self.gh, ":refs/heads/feat/x", cwd=self.work)
        self.run_sync()
        self.assertIsNone(self.ref(self.gl, "refs/heads/feat/x"))
        self.git("push", "-q", self.gl, ":refs/heads/develop", cwd=self.work)
        self.run_sync()
        self.assertIsNone(self.ref(self.gh, "refs/heads/develop"))

    def test_force_push_on_one_side_is_copied(self):
        self.run_sync()
        base = self.ref(self.gh, "refs/heads/main")
        ahead = self.commit("ahead", base)
        for remote in (self.gh, self.gl):
            self.git("push", "-q", remote, f"{ahead}:refs/heads/feat", cwd=self.work)
        self.run_sync()
        rewritten = self.commit("rewritten", base)
        self.git("push", "-q", "-f", self.gh, f"{rewritten}:refs/heads/feat", cwd=self.work)
        self.run_sync()
        self.assertEqual(self.ref(self.gl, "refs/heads/feat"), rewritten)

    def test_divergence_is_reported_once_and_left_alone(self):
        self.run_sync()
        base = self.ref(self.gh, "refs/heads/main")
        on_gh, on_gl = self.commit("on github", base), self.commit("on gitlab", base)
        self.git("push", "-q", self.gh, f"{on_gh}:refs/heads/main", cwd=self.work)
        self.git("push", "-q", self.gl, f"{on_gl}:refs/heads/main", cwd=self.work)
        sync, out = self.run_sync()
        self.assertEqual(sync.new_problems, 1)
        self.assertIn("main has diverged", out)
        self.assertEqual((self.ref(self.gh, "refs/heads/main"), self.ref(self.gl, "refs/heads/main")), (on_gh, on_gl))
        sync, out = self.run_sync()
        self.assertEqual(sync.new_problems, 0)
        self.assertIn("already reported", out)

        merge = self.git("commit-tree", f"{on_gh}^{{tree}}", "-p", on_gh, "-p", on_gl, "-m", "merge", cwd=self.work)
        self.git("push", "-q", self.gh, f"{merge}:refs/heads/main", cwd=self.work)
        sync, _ = self.run_sync()
        self.assertEqual(self.ref(self.gl, "refs/heads/main"), merge)
        self.assertEqual(self.state["problems"], [])

    def test_first_sync_drops_gitlab_branches_already_merged_on_github(self):
        main = self.ref(self.gh, "refs/heads/main")
        unique = self.commit("only on gitlab", main)
        self.git("push", "-q", self.gl, f"{main}:refs/heads/merged-and-deleted", f"{unique}:refs/heads/wip",
                 cwd=self.work)
        self.run_sync()
        self.assertIsNone(self.ref(self.gl, "refs/heads/merged-and-deleted"))
        self.assertEqual(self.ref(self.gh, "refs/heads/wip"), unique)


# ---- issues -----------------------------------------------------------------------


class FakeSite:
    """In-memory GitHub or GitLab repository with the GitHubRepo/GitLabRepo API."""

    def __init__(self, side, clock):
        self.side, self.clock = side, clock
        self.web = GH_WEB if side == gs.GITHUB else GL_WEB
        self.links = gs.Links(GH_WEB, GL_WEB)
        self.data, self.notes, self.next_id = {}, {}, 1000
        self.cutoff = None

    def url(self, kind, number):
        if self.side == gs.GITHUB:
            return f"{self.web}/{'issues' if kind == 'issue' else 'pull'}/{number}"
        return f"{self.web}/-/{'issues' if kind == 'issue' else 'merge_requests'}/{number}"

    def add(self, kind, number, title, body="", state="open", author="someone", created=None, **extra):
        created = created or self.clock.tick()
        item = gs.Item(self.side, kind, number, self.url(kind, number), title, body, [], state,
                       (author, f"https://example.com/{author}"), created, created, **extra)
        item.imported = bool(self.cutoff and created < self.cutoff)
        self.data[item.key] = item
        self.notes[item.key] = []
        return item

    def comment(self, item, body, author="someone", created=None):
        self.next_id += 1
        anchor = "issuecomment-" if self.side == gs.GITHUB else "note_"
        c = gs.make_comment(self.side, self.next_id, f"{item.url}#{anchor}{self.next_id}", body,
                            (author, f"https://example.com/{author}"), gs.iso(created or self.clock.tick()))
        self.notes[item.key].append(c)
        self.data[item.key].updated = self.clock.tick()
        return c

    # API used by IssueSync
    def changed_since(self, since):
        return True

    def items(self):
        out = []
        for item in self.data.values():
            copy = gs.Item(**vars(item))
            copy.link = self.links.item(item.body, gs.OTHER[self.side])
            out.append(copy)
        return out

    def comments(self, item):
        return [gs.make_comment(self.side, c.id, c.url, c.body, c.author, gs.iso(c.created), c.header)
                for c in self.notes[item.key]]

    def create_item(self, kind, title, body, labels, draft=False, source="", target="", number=None):
        number = number or max([n for _, n in self.data] + [100]) + 1
        item = self.add(kind, number, title, body, author="sync-bot", draft=draft, source=source, target=target)
        item.labels = sorted(labels)
        return gs.Item(**vars(item))

    def update_item(self, item, changes):
        stored = self.data[item.key]
        for name, value in changes.items():
            setattr(stored, name, value)
        stored.updated = self.clock.tick()

    def refresh(self, item):
        return gs.Item(**vars(self.data[item.key]))

    def add_comment(self, item, body):
        self.comment(self.data[item.key], body, author="sync-bot")

    def edit_comment(self, item, comment, body):
        next(c for c in self.notes[item.key] if c.id == comment.id).body = body

    def delete_comment(self, item, comment):
        self.notes[item.key] = [c for c in self.notes[item.key] if c.id != comment.id]


class Clock:
    def __init__(self):
        self.now = T0

    def tick(self):
        self.now += dt.timedelta(minutes=1)
        return self.now


class IssueSyncTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gh, self.gl = FakeSite(gs.GITHUB, self.clock), FakeSite(gs.GITLAB, self.clock)
        self.state = {}
        self.cutoff = None
        branches = {f"refs/heads/{b}": "x" for b in ("main", "feat")}
        self.refs = {gs.GITHUB: dict(branches), gs.GITLAB: dict(branches)}

    def run_sync(self):
        sync = gs.Sync(dry_run=False)
        sync.anonymous_gitlab = False
        ctx = gs.RepoContext("demo", {}, {}, self.cutoff, self.state, self.gh, self.gl, refs=self.refs)
        _, out = quiet(gs.IssueSync(sync, ctx).run, "all")
        self.assertEqual(sync.new_problems, 0, out)
        return out

    def only(self, site, kind="issue"):
        items = [i for i in site.data.values() if i.kind == kind]
        self.assertEqual(len(items), 1)
        return items[0]

    def test_issue_born_on_github_is_copied_and_edits_flow_back(self):
        src = self.gh.add("issue", 7, "Bug", "Broken for @alice", author="onihilist")
        self.run_sync()
        copy = self.only(self.gl)
        self.assertEqual((copy.number, copy.title), (7, "Bug"))
        self.assertTrue(copy.body.endswith(f"on GitHub ({src.url})"))
        self.assertIn(f"@{gs.WORD_JOINER}alice", copy.body)

        self.gl.update_item(copy, {"title": "Bug in cache", "body": gs.render("Broken for @alice, again",
                                                                              src.author, src.url, gs.GITHUB)})
        self.gh.update_item(src, {"labels": ["bug"]})
        self.run_sync()
        self.assertEqual(src.title, "Bug in cache")
        self.assertEqual(src.body, "Broken for @alice, again")
        self.assertEqual(copy.labels, ["bug"])

        self.gl.update_item(copy, {"state": "closed"})
        self.run_sync()
        self.assertEqual(src.state, "closed")
        before = (dict(vars(src)), dict(vars(copy)))
        self.run_sync()
        self.assertEqual((vars(src), vars(copy)), before)  # nothing left to copy

    def test_merge_request_born_on_gitlab_becomes_a_pull_request(self):
        mr = self.gl.add("pr", 3, "Add cache", "Body", source="feat", target="main", draft=True)
        self.run_sync()
        pr = self.only(self.gh, "pr")
        self.assertEqual((pr.title, pr.source, pr.target, pr.draft), ("Add cache", "feat", "main", True))
        self.assertTrue(pr.body.endswith(f"on GitLab ({mr.url})"))
        self.gh.update_item(pr, {"draft": False})
        self.run_sync()
        self.assertFalse(mr.draft)

    def test_open_request_waits_for_its_branch(self):
        self.gl.add("pr", 3, "Add cache", source="missing", target="main")
        sync = gs.Sync(dry_run=False)
        sync.anonymous_gitlab = False
        ctx = gs.RepoContext("demo", {}, {}, None, self.state, self.gh, self.gl, refs=self.refs)
        _, out = quiet(gs.IssueSync(sync, ctx).run, "all")
        self.assertIn("branch missing missing on GitHub", out)
        self.assertEqual(self.state["retry"], ["gitlab-p3"])
        self.assertEqual(self.gh.data, {})

    def test_comments_both_ways(self):
        src = self.gh.add("issue", 1, "Q")
        self.run_sync()
        copy = self.only(self.gl)
        gh_comment = self.gh.comment(src, "From GitHub", author="alice")
        gl_comment = self.gl.comment(copy, "From GitLab", author="bob")
        self.run_sync()
        self.assertEqual([gs.unrender(c.body) for c in self.gl.notes[copy.key]], ["From GitLab", "From GitHub"])
        self.assertEqual([gs.unrender(c.body) for c in self.gh.notes[src.key]], ["From GitHub", "From GitLab"])
        self.assertIn(f"on GitLab ({gl_comment.url})", self.gh.notes[src.key][1].body)

        gh_comment.body = "From GitHub, edited"
        self.gl.delete_comment(copy, gl_comment)
        self.run_sync()
        self.assertEqual([gs.unrender(c.body) for c in self.gl.notes[copy.key]], ["From GitHub, edited"])
        self.assertEqual([gs.unrender(c.body) for c in self.gh.notes[src.key]], ["From GitHub, edited"])
        counts = (len(self.gh.notes[src.key]), len(self.gl.notes[copy.key]))
        self.run_sync()
        self.assertEqual((len(self.gh.notes[src.key]), len(self.gl.notes[copy.key])), counts)

    def test_imported_items_keep_their_text_and_old_comments(self):
        self.cutoff = self.gl.cutoff = T0 + dt.timedelta(minutes=10)
        old = T0 + dt.timedelta(minutes=1)
        src = self.gh.add("issue", 4, "Old", "Ping @alice", created=old)
        imported = self.gl.add("issue", 4, "Old", "Ping `@alice`", created=old)
        self.gh.comment(src, "old comment", created=old)
        self.gl.comment(imported, "old comment", created=old)
        self.clock.now = self.cutoff
        self.gh.comment(src, "new comment")
        self.run_sync()
        self.assertEqual(imported.body, "Ping `@alice`")  # not rewritten
        self.assertEqual([gs.unrender(c.body) for c in self.gl.notes[imported.key]], ["old comment", "new comment"])
        self.assertEqual(len(self.gh.notes[src.key]), 2)

    def test_merged_on_one_side_closes_the_other(self):
        mr = self.gl.add("pr", 5, "Fix", source="feat", target="main")
        self.run_sync()
        pr = self.only(self.gh, "pr")
        self.gl.update_item(mr, {"state": "merged"})
        self.run_sync()
        self.assertEqual(pr.state, "closed")
        self.assertIn(f"Merged on GitLab: {mr.url}", self.gh.notes[pr.key][-1].body)
        self.assertEqual(len(self.gl.notes[mr.key]), 0)  # the notice is not copied back
        self.run_sync()
        self.assertEqual(len(self.gl.notes[mr.key]), 0)

    def test_both_sides_edit_the_same_field_latest_wins(self):
        src = self.gh.add("issue", 2, "Title")
        self.run_sync()
        copy = self.only(self.gl)
        self.gh.update_item(src, {"title": "From GitHub"})
        self.gl.update_item(copy, {"title": "From GitLab"})
        self.run_sync()
        self.assertEqual((src.title, copy.title), ("From GitLab", "From GitLab"))


if __name__ == "__main__":
    unittest.main()
