#!/usr/bin/env python3
"""Two-way sync between the 2SCloud GitHub organisation and the 2scloud GitLab group.

Every GitHub repository has a GitLab project of the same name, created when
missing (GitLab cannot host names that start with a dot, such as `.github`).
Each run copies what changed on either side since the previous run:

* Code: branches and tags, commit for commit: both sides hold the same
  commits, with the same hashes. A ref changed on one side only is copied to
  the other, deletions and force pushes included. When both sides changed, a
  fast-forward is copied, and a real divergence is reported and left alone
  until someone merges one side into the other.
* Issues, and pull requests <-> merge requests: creation, title, description,
  labels, open/closed state, draft flag, target branch, and comments. When a
  field changed on both sides between two runs, the latest change wins. A
  comment is edited and deleted where it was written; its copy follows.
* Releases: GitHub -> GitLab only. The release pipeline lives on GitHub, and a
  tag pushed on GitLab reaches GitHub and triggers it there.

Copies are written with the tokens' accounts and end with the line
"Created by @user on GitHub|GitLab (<url>)", which links a copy to its
original. Items the GitLab GitHub importer created (dated before the project's
import) are linked by number instead, and the comments it copied are not
copied again. @mentions in copies get an invisible word joiner after the @,
so nobody with the same name on the other site gets pinged.

The state (last synced refs and issue fields, time windows, problems already
reported) lives in state.json on the `gitlab-sync-state` branch of the
repository that runs the workflow, or in a local file outside GitHub Actions.

Environment:
  GITLAB_TOKEN    GitLab personal access token (scopes: api, write_repository)
  GH_SYNC_TOKEN   GitHub personal access token (scopes: repo, workflow)
  GITHUB_TOKEN    read-only fallback for dry runs
  STATE_REPO, STATE_TOKEN   GitHub repository and token for the state branch
  GITHUB_ORG (2SCloud), GITLAB_GROUP (2scloud), GITLAB_URL (https://gitlab.com)
  REPOS, SINCE, DRY_RUN     defaults for --repo, --since and --dry-run
"""

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

UTC = dt.timezone.utc
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

GITHUB, GITLAB = "github", "gitlab"
SITE = {GITHUB: "GitHub", GITLAB: "GitLab"}
OTHER = {GITHUB: GITLAB, GITLAB: GITHUB}

STATE_BRANCH = "gitlab-sync-state"
STATE_FILE = "state.json"
# Overlap between two runs, so that nothing edited during a run is missed.
WINDOW_MARGIN = dt.timedelta(minutes=10)
# How long to wait for a site to mark a pull/merge request merged after a push.
MERGE_DETECTION_WAIT = 30

# Branches that give fork pull/merge requests a source branch on the other
# side. They are managed apart from the two-way ref sync and never copied back.
GH_FORK_BRANCH = "refs/heads/github-pr/"  # on GitLab, for GitHub fork PRs
GL_FORK_BRANCH = "refs/heads/gitlab-mr/"  # on GitHub, for GitLab fork MRs

WORD_JOINER = "⁠"
FOOTER = "Created by [@" + WORD_JOINER + "{login}]({profile}) on {site} ({url})"
FOOTER_RE = re.compile(r"^Created by .* on (GitHub|GitLab) \((https://[^\s()]+)\)$")
# Ends the comments the sync writes itself (merge notices); never copied.
NOTICE_MARK = "<!-- gitlab-sync -->"
MENTION_RE = re.compile(r"(?<![\w@/.`\[-])@(?=[A-Za-z0-9])")
IMPORTER_MENTION_RE = re.compile(r"`(@[\w.-]+)`")
DRAFT_RE = re.compile(r"^\s*(?:\[draft\]|\(draft\)|draft:|draft\s+-)\s*", re.I)
REVIEW_TITLES = {
    "APPROVED": "✅ Review: approved",
    "CHANGES_REQUESTED": "❌ Review: changes requested",
    "COMMENTED": "💬 Review",
    "DISMISSED": "Review (dismissed)",
}

# ---- logging ---------------------------------------------------------------

SUMMARY = []


def log(msg):
    print(msg, flush=True)


def warn(msg):
    SUMMARY.append(f"⚠️ {msg}")
    print(f"::warning::{msg}" if IN_ACTIONS else f"WARNING: {msg}", flush=True)


def error(msg):
    SUMMARY.append(f"❌ {msg}")
    print(f"::error::{msg}" if IN_ACTIONS else f"ERROR: {msg}", flush=True)


class LogGroup:
    def __init__(self, title):
        self.title = title

    def __enter__(self):
        log(f"::group::{self.title}" if IN_ACTIONS else f"== {self.title}")

    def __exit__(self, *exc):
        if IN_ACTIONS:
            log("::endgroup::")


# ---- text ------------------------------------------------------------------


def parse_time(value):
    if not value:
        return None
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(text):
    return (text or "").replace("\r\n", "\n").strip()


def digest(text):
    return hashlib.sha1(norm(text).encode()).hexdigest()[:16]


def quote(value):
    return urllib.parse.quote(str(value), safe="")


def short_ref(ref):
    return ref.removeprefix("refs/heads/").removeprefix("refs/")


def short_sha(sha):
    return sha[:8] if sha else "none"


def neutralize(text):
    """Put a word joiner after each @mention outside code, so it pings nobody."""
    out, in_fence = [], False
    for line in text.split("\n"):
        fence_line = bool(re.match(r"\s*(```|~~~)", line))
        if fence_line:
            in_fence = not in_fence
        if in_fence or fence_line:
            out.append(line)
            continue
        parts = re.split(r"(`+[^`]*`+)", line)
        out.append("".join(p if i % 2 else MENTION_RE.sub("@" + WORD_JOINER, p) for i, p in enumerate(parts)))
    return "\n".join(out)


def split_footer(text):
    """(text without footer, site of the original, URL of the original)."""
    lines = norm(text).split("\n")
    m = FOOTER_RE.match(lines[-1].strip())
    if not m:
        return norm(text), None, None
    rest = lines[:-1]
    while rest and not rest[-1].strip():
        rest.pop()
    if rest and rest[-1].strip() == "---":
        rest.pop()
    return "\n".join(rest).strip(), m.group(1).lower(), m.group(2)


def render(text, author, url, side):
    """A copy of `text`, written on `side` at `url` by `author`."""
    footer = FOOTER.format(login=author[0], profile=author[1], site=SITE[side], url=url)
    text = neutralize(norm(text))
    return f"{text}\n\n---\n{footer}" if text else footer


def unrender(text):
    """The original text of a copy."""
    return split_footer(text)[0].replace("@" + WORD_JOINER, "@")


def loose(text):
    """Text without footer, mention escaping or spacing differences."""
    text = IMPORTER_MENTION_RE.sub(r"\1", unrender(text))
    return re.sub(r"\s+", " ", text).strip()


def notice(text):
    return f"{text}\n\n{NOTICE_MARK}"


def line_header(path, line, hunk=None):
    header = f"**`{path}`**" + (f" · line {line}" if line else "")
    hunk = "\n".join((hunk or "").splitlines()[-4:])
    if hunk:
        fence = "~~~~" if "```" in hunk else "```"
        header += f"\n\n{fence}diff\n{hunk}\n{fence}"
    return header


def github_author(user):
    return (user["login"], user["html_url"]) if user else ("ghost", "https://github.com/ghost")


def gitlab_author(user):
    return (user["username"], user["web_url"]) if user else ("ghost", "https://gitlab.com/ghost")


# ---- HTTP and git -------------------------------------------------------------


class SyncError(Exception):
    pass


class ApiError(SyncError):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


class Api:
    """JSON API client. In a dry run, writes are logged and return None."""

    def __init__(self, base, headers, dry_run=False):
        self.base = base.rstrip("/")
        self.headers = {"User-Agent": "2scloud-gitlab-sync", **headers}
        self.dry_run = dry_run
        self.calls = 0

    def request(self, method, path, params=None, body=None, allow_404=False, raw=False):
        url = path if path.startswith("https://") else self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        shown = url.removeprefix(self.base)
        if self.dry_run and method != "GET":
            data = json.dumps(body, ensure_ascii=False)[:160] if body else ""
            log(f"      [dry run] {method} {shown} {data}")
            return None, {}
        headers = dict(self.headers)
        if raw:
            headers["Accept"] = "application/vnd.github.raw+json"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(6):
            self.calls += 1
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    content = resp.read()
                    if raw:
                        return content.decode(), resp.headers
                    return (json.loads(content) if content else None), resp.headers
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                if e.code == 404 and allow_404:
                    return None, e.headers
                wait = None
                if e.code == 429 or (e.code == 403 and (e.headers.get("x-ratelimit-remaining") == "0"
                                                        or e.headers.get("retry-after"))):
                    wait = retry_delay(e.headers, attempt)
                elif e.code >= 500 and method == "GET":
                    wait = 2 ** attempt
                if wait is not None and attempt < 5 and wait <= 900:
                    log(f"      HTTP {e.code} on {method} {shown}, retrying in {wait:.0f}s")
                    time.sleep(wait)
                    continue
                raise ApiError(e.code, f"{method} {shown}: HTTP {e.code}: {text[:400]}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if method == "GET" and attempt < 5:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(0, f"{method} {shown}: {e}") from None
        raise AssertionError("unreachable")

    def get(self, path, allow_404=False, **params):
        return self.request("GET", path, params or None, allow_404=allow_404)[0]

    def post(self, path, body):
        return self.request("POST", path, body=body)[0]

    def put(self, path, body):
        return self.request("PUT", path, body=body)[0]

    def patch(self, path, body):
        return self.request("PATCH", path, body=body)[0]

    def delete(self, path):
        return self.request("DELETE", path)[0]

    def paginate(self, path, **params):
        params.setdefault("per_page", 100)
        url = self.base + path + "?" + urllib.parse.urlencode(params)
        while url:
            items, headers = self.request("GET", url)
            yield from items
            url = next_link(headers.get("Link"))


def retry_delay(headers, attempt):
    if headers.get("retry-after"):
        return float(headers["retry-after"]) + 1
    reset = headers.get("x-ratelimit-reset") or headers.get("ratelimit-reset")
    if reset:
        return max(1.0, float(reset) - time.time() + 1)
    return 5.0 * 2 ** attempt


def next_link(link_header):
    for part in (link_header or "").split(","):
        m = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


def github_headers(token):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def git_auth(url_prefix, user, token):
    """Git config entry that sends the token in a header, never in a URL or a log."""
    if not token:
        return None
    basic = base64.b64encode(f"{user}:{token}".encode()).decode()
    return (f"http.{url_prefix}.extraHeader", f"Authorization: Basic {basic}")


def git(*args, cwd=None, auth=None, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if auth:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=auth[0], GIT_CONFIG_VALUE_0=auth[1])
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and result.returncode:
        raise SyncError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[-1500:]}")
    return result


def ls_remote(url, auth=None):
    refs = {}
    for line in git("ls-remote", url, auth=auth).stdout.splitlines():
        sha, ref = line.split("\t", 1)
        refs[ref] = sha
    return refs


def mirrored_refs(refs):
    return {ref: sha for ref, sha in refs.items()
            if ref.startswith(("refs/heads/", "refs/tags/")) and not ref.endswith("^{}")}


def is_ancestor(repo, ancestor, descendant):
    result = git("merge-base", "--is-ancestor", ancestor, descendant, cwd=repo, check=False)
    if result.returncode > 1:
        raise SyncError(f"git merge-base failed: {result.stderr.strip()}")
    return result.returncode == 0


def is_fork(pr, repo):
    head_repo = pr["head"].get("repo")
    return not head_repo or head_repo["full_name"] != repo["full_name"]


# ---- issues and pull requests, as seen on each site ----------------------------


@dataclass
class Item:
    """An issue, or a pull/merge request (kind "pr"), on one site."""
    side: str
    kind: str
    number: int  # GitHub number or GitLab iid
    url: str
    title: str  # without any draft prefix
    body: str
    labels: list
    state: str  # open, closed or merged
    author: tuple  # (login, profile URL)
    created: dt.datetime
    updated: dt.datetime
    draft: bool = False
    source: str = ""
    target: str = ""
    fork: bool = False
    node_id: str = ""
    link: tuple = None  # (side, kind, number) of the original, for a copy
    imported: bool = False  # created by the GitLab importer

    @property
    def key(self):
        return (self.kind, self.number)

    @property
    def name(self):
        sign = "!" if self.side == GITLAB and self.kind == "pr" else "#"
        return f"{SITE[self.side]} {sign}{self.number}"


@dataclass
class Comment:
    side: str
    id: int
    url: str
    body: str
    author: tuple
    created: dt.datetime
    header: str = ""  # context shown above the text in a copy (file and line, review)
    link: str = None  # URL of the original, for a copy
    notice: bool = False

    def text(self):
        body = norm(self.body)
        if not self.header:
            return body
        return f"{self.header}\n\n{body}" if body else self.header


def make_comment(side, cid, url, body, author, created, header=""):
    _, site, link = split_footer(body)
    return Comment(side, cid, url, body or "", author, parse_time(created), header,
                   link if site == OTHER[side] else None, NOTICE_MARK in (body or ""))


class Links:
    """Reads which item a copy was made from, out of its footer."""

    def __init__(self, gh_web, gl_web):
        self.patterns = {
            GITHUB: re.compile(re.escape(gh_web) + r"/(issues|pull)/(\d+)", re.I),
            GITLAB: re.compile(re.escape(gl_web) + r"/-/(issues|merge_requests)/(\d+)", re.I),
        }

    def item(self, body, original_side):
        _, site, url = split_footer(body)
        m = self.patterns[original_side].fullmatch(url) if site == original_side else None
        if not m:
            return None
        return (original_side, "issue" if m.group(1) == "issues" else "pr", int(m.group(2)))


class GitHubRepo:
    side = GITHUB

    def __init__(self, api, repo, links):
        self.api, self.repo, self.links = api, repo, links
        self.path = f"/repos/{repo['full_name']}"

    def changed_since(self, since):
        return since is None or bool(self.api.get(f"{self.path}/issues", state="all", since=iso(since), per_page=1))

    def items(self):
        items = [self._issue(raw) for raw in self.api.paginate(f"{self.path}/issues", state="all")
                 if "pull_request" not in raw]
        return items + [self._pr(raw) for raw in self.api.paginate(f"{self.path}/pulls", state="all")]

    def _item(self, raw, kind, state):
        return Item(GITHUB, kind, raw["number"], raw["html_url"], raw["title"].strip(), raw.get("body") or "",
                    sorted(label["name"] for label in raw.get("labels") or []), state,
                    github_author(raw.get("user")), parse_time(raw["created_at"]), parse_time(raw["updated_at"]),
                    link=self.links.item(raw.get("body"), GITLAB))

    def _issue(self, raw):
        return self._item(raw, "issue", raw["state"])

    def _pr(self, raw):
        item = self._item(raw, "pr", "merged" if raw.get("merged_at") else raw["state"])
        item.draft = bool(raw.get("draft"))
        item.source, item.target = raw["head"]["ref"], raw["base"]["ref"]
        item.fork = is_fork(raw, self.repo)
        item.node_id = raw.get("node_id", "")
        return item

    def refresh(self, item):
        return self._pr(self.api.get(f"{self.path}/pulls/{item.number}"))

    def comments(self, item):
        out = [make_comment(GITHUB, c["id"], c["html_url"], c.get("body"), github_author(c.get("user")),
                            c["created_at"])
               for c in self.api.paginate(f"{self.path}/issues/{item.number}/comments")]
        if item.kind != "pr":
            return out
        for c in self.api.paginate(f"{self.path}/pulls/{item.number}/comments"):
            header = line_header(c.get("path"), c.get("line") or c.get("original_line"), c.get("diff_hunk"))
            out.append(make_comment(GITHUB, c["id"], c["html_url"], c.get("body"), github_author(c.get("user")),
                                    c["created_at"], header))
        for r in self.api.paginate(f"{self.path}/pulls/{item.number}/reviews"):
            body = norm(r.get("body"))
            if r["state"] == "PENDING" or not r.get("submitted_at"):
                continue
            if not body and r["state"] not in ("APPROVED", "CHANGES_REQUESTED"):
                continue  # only holds line comments, copied on their own
            header = f"**{REVIEW_TITLES.get(r['state'], 'Review')}**"
            out.append(make_comment(GITHUB, r["id"], r["html_url"], body, github_author(r.get("user")),
                                    r["submitted_at"], header))
        return out

    def create_item(self, kind, title, body, labels, draft=False, source="", target="", number=None):
        if kind == "issue":
            raw = self.api.post(f"{self.path}/issues", {"title": title, "body": body, "labels": labels})
            return raw and self._issue(raw)
        raw = self.api.post(f"{self.path}/pulls",
                            {"title": title, "body": body, "head": source, "base": target, "draft": draft})
        if raw is None:
            return None
        if labels:
            self.api.patch(f"{self.path}/issues/{raw['number']}", {"labels": labels})
        item = self._pr(raw)
        item.labels = sorted(labels)
        return item

    def update_item(self, item, changes):
        fields = {}
        if "title" in changes:
            fields["title"] = changes["title"]
        if "body" in changes:
            fields["body"] = changes["body"]
        if "state" in changes:
            fields["state"] = "open" if changes["state"] == "open" else "closed"
        if item.kind == "issue":
            if "labels" in changes:
                fields["labels"] = changes["labels"]
            if fields:
                self.api.patch(f"{self.path}/issues/{item.number}", fields)
            return
        if "target" in changes:
            fields["base"] = changes["target"]
        if fields:
            self.api.patch(f"{self.path}/pulls/{item.number}", fields)
        if "labels" in changes:
            self.api.patch(f"{self.path}/issues/{item.number}", {"labels": changes["labels"]})
        if "draft" in changes:
            mutation = "convertPullRequestToDraft" if changes["draft"] else "markPullRequestReadyForReview"
            result = self.api.post("https://api.github.com/graphql", {
                "query": f"mutation($id: ID!) {{ {mutation}(input: {{pullRequestId: $id}}) {{ clientMutationId }} }}",
                "variables": {"id": item.node_id}})
            if result and result.get("errors"):
                raise ApiError(0, f"{mutation}: {result['errors']}")

    def add_comment(self, item, body):
        self.api.post(f"{self.path}/issues/{item.number}/comments", {"body": body})

    def edit_comment(self, item, comment, body):
        self.api.patch(f"{self.path}/issues/comments/{comment.id}", {"body": body})

    def delete_comment(self, item, comment):
        self.api.delete(f"{self.path}/issues/comments/{comment.id}")


class GitLabRepo:
    side = GITLAB

    def __init__(self, api, project, links, cutoff):
        self.api, self.project, self.links, self.cutoff = api, project, links, cutoff
        self.path = f"/projects/{project['id']}"

    def changed_since(self, since):
        if since is None:
            return True
        return any(self.api.get(f"{self.path}/{kind}", updated_after=iso(since), per_page=1)
                   for kind in ("issues", "merge_requests"))

    def items(self):
        items = [self._issue(raw) for raw in self.api.paginate(f"{self.path}/issues")]
        return items + [self._mr(raw) for raw in self.api.paginate(f"{self.path}/merge_requests")]

    def _item(self, raw, kind, state, title):
        created = parse_time(raw["created_at"])
        return Item(GITLAB, kind, raw["iid"], raw["web_url"], title.strip(), raw.get("description") or "",
                    sorted(raw.get("labels") or []), state, gitlab_author(raw.get("author")), created,
                    parse_time(raw["updated_at"]), link=self.links.item(raw.get("description"), GITHUB),
                    imported=bool(self.cutoff and created < self.cutoff))

    def _issue(self, raw):
        return self._item(raw, "issue", "closed" if raw["state"] == "closed" else "open", raw["title"])

    def _mr(self, raw):
        state = {"merged": "merged", "closed": "closed"}.get(raw["state"], "open")
        item = self._item(raw, "pr", state, DRAFT_RE.sub("", raw["title"], count=1))
        item.draft = bool(raw.get("draft") or raw.get("work_in_progress"))
        item.source, item.target = raw["source_branch"], raw["target_branch"]
        item.fork = raw.get("source_project_id") not in (None, raw.get("project_id"))
        return item

    def _kind_path(self, item):
        return f"{self.path}/{'issues' if item.kind == 'issue' else 'merge_requests'}/{item.number}"

    def refresh(self, item):
        return self._mr(self.api.get(self._kind_path(item)))

    def comments(self, item):
        out = []
        for n in self.api.paginate(f"{self._kind_path(item)}/notes", sort="asc", order_by="created_at"):
            if n.get("system"):
                continue
            header = ""
            position = n.get("position") if n.get("type") == "DiffNote" else None
            if position:
                header = line_header(position.get("new_path") or position.get("old_path"),
                                     position.get("new_line") or position.get("old_line"))
            out.append(make_comment(GITLAB, n["id"], f"{item.url}#note_{n['id']}", n.get("body"),
                                    gitlab_author(n.get("author")), n["created_at"], header))
        return out

    def create_item(self, kind, title, body, labels, draft=False, source="", target="", number=None):
        if kind == "issue":
            data = {"title": title, "description": body, "labels": ",".join(labels)}
            if number:
                data["iid"] = number  # keep the GitHub number when GitLab allows it
            try:
                raw = self.api.post(f"{self.path}/issues", data)
            except ApiError as e:
                if not number or e.status not in (400, 409, 422):
                    raise
                del data["iid"]
                raw = self.api.post(f"{self.path}/issues", data)
            return raw and self._issue(raw)
        data = {"source_branch": source, "target_branch": target, "title": ("Draft: " if draft else "") + title,
                "description": body, "labels": ",".join(labels), "remove_source_branch": False}
        try:
            raw = self.api.post(f"{self.path}/merge_requests", data)
        except ApiError as e:
            # That branch already has an open merge request: it becomes the copy.
            m = re.search(r"!(\d+)", str(e))
            if e.status != 409 or not m:
                raise
            raw = self.api.put(f"{self.path}/merge_requests/{m.group(1)}",
                               {k: data[k] for k in ("title", "description", "labels")})
        return raw and self._mr(raw)

    def update_item(self, item, changes):
        data = {}
        if "title" in changes or "draft" in changes:
            title = changes.get("title", item.title)
            data["title"] = ("Draft: " if item.kind == "pr" and changes.get("draft", item.draft) else "") + title
        if "body" in changes:
            data["description"] = changes["body"]
        if "labels" in changes:
            data["labels"] = ",".join(changes["labels"])
        if "target" in changes:
            data["target_branch"] = changes["target"]
        if "state" in changes:
            data["state_event"] = "reopen" if changes["state"] == "open" else "close"
        if data:
            self.api.put(self._kind_path(item), data)

    def add_comment(self, item, body):
        self.api.post(f"{self._kind_path(item)}/notes", {"body": body})

    def edit_comment(self, item, comment, body):
        self.api.put(f"{self._kind_path(item)}/notes/{comment.id}", {"body": body})

    def delete_comment(self, item, comment):
        self.api.delete(f"{self._kind_path(item)}/notes/{comment.id}")


def wait_merged(site, item):
    """A site marks a request merged a few seconds after its commits are pushed."""
    deadline = time.time() + MERGE_DETECTION_WAIT
    while True:
        item = site.refresh(item)
        if item.state == "merged" or time.time() >= deadline:
            return item
        time.sleep(5)


# ---- per-repository sync ------------------------------------------------------


@dataclass
class RepoContext:
    name: str
    repo: dict
    project: dict
    cutoff: dt.datetime  # import date: GitLab already had what GitHub had then
    state: dict
    gh: GitHubRepo
    gl: GitLabRepo
    created: bool = False
    refs: dict = None  # side -> {ref: sha}, kept up to date by the pushes
    pushed: dict = field(default_factory=dict)  # side -> whether we pushed to it
    known_problems: set = field(default_factory=set)
    problems: list = field(default_factory=list)

    def site(self, side):
        return self.gh if side == GITHUB else self.gl

    def has_branch(self, side, branch):
        return f"refs/heads/{branch}" in self.refs[side]


class Sync:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.org = os.environ.get("GITHUB_ORG", "2SCloud")
        self.group = os.environ.get("GITLAB_GROUP", "2scloud")
        self.gitlab_url = os.environ.get("GITLAB_URL", "https://gitlab.com").rstrip("/")
        gh_token = os.environ.get("GH_SYNC_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        gl_token = os.environ.get("GITLAB_TOKEN", "")
        self.gh_api = Api("https://api.github.com", github_headers(gh_token), dry_run)
        self.gl_api = Api(f"{self.gitlab_url}/api/v4", {"PRIVATE-TOKEN": gl_token} if gl_token else {}, dry_run)
        self.anonymous_gitlab = not gl_token
        self.git_auth = {GITHUB: git_auth("https://github.com/", "x-access-token", gh_token),
                         GITLAB: git_auth(f"{self.gitlab_url}/", "oauth2", gl_token)}
        self.new_problems = 0

    def report(self, ctx, key, msg):
        """A problem that fails the run the first time it shows up, then only warns."""
        ctx.problems.append(key)
        if key in ctx.known_problems:
            warn(f"{msg} (already reported)")
        else:
            error(msg)
            self.new_problems += 1

    def github_repos(self, wanted):
        repos = sorted(self.gh_api.paginate(f"/orgs/{self.org}/repos", type="all"), key=lambda r: r["name"].lower())
        if wanted:
            missing = sorted(set(wanted) - {r["name"].lower() for r in repos})
            if missing:
                raise SyncError(f"not in the {self.org} GitHub org: {', '.join(missing)}")
            repos = [r for r in repos if r["name"].lower() in wanted]
        return repos

    def gitlab_project(self, repo):
        path = f"{self.group}/{repo['name']}"
        project = self.gl_api.get(f"/projects/{quote(path)}", allow_404=True)
        if project:
            return project, False
        if self.dry_run:
            log(f"  [dry run] would create the GitLab project {path}")
            return None, True
        group = self.gl_api.get(f"/groups/{quote(self.group)}")
        project = self.gl_api.post("/projects", {
            "name": repo["name"], "path": repo["name"], "namespace_id": group["id"],
            "visibility": "private" if repo["private"] else "public",
            "description": repo.get("description") or "",
        })
        log(f"  created the GitLab project {project['web_url']}")
        return project, True

    def sync_repo(self, repo, rstate, parts, since):
        project, created = self.gitlab_project(repo)
        if project is None:
            return
        # Without a token GitLab hides the import status: assume an import.
        imported = not created and project.get("import_status", "unknown") != "none"
        cutoff = parse_time(project["created_at"]) if imported else None
        links = Links(repo["html_url"], project["web_url"])
        ctx = RepoContext(repo["name"], repo, project, cutoff, rstate,
                          GitHubRepo(self.gh_api, repo, links), GitLabRepo(self.gl_api, project, links, cutoff),
                          created=created, known_problems=set(rstate.get("problems", [])))
        try:
            if "code" in parts:
                self.sync_code(ctx)
        except SyncError as e:
            self.report(ctx, f"code:{digest(str(e))}", f"{ctx.name} code: {e}")
        if ctx.refs is None:  # needed to know which branches exist
            ctx.refs = {GITHUB: mirrored_refs(ls_remote(repo["clone_url"], self.git_auth[GITHUB])),
                        GITLAB: mirrored_refs(ls_remote(project["http_url_to_repo"], self.git_auth[GITLAB]))}
        if "issues" in parts:
            try:
                IssueSync(self, ctx).run(since)
            except SyncError as e:
                self.report(ctx, f"issues:{digest(str(e))}", f"{ctx.name} issues: {e}")
        if "releases" in parts:
            try:
                self.sync_releases(ctx)
            except SyncError as e:
                self.report(ctx, f"releases:{digest(str(e))}", f"{ctx.name} releases: {e}")
        rstate["problems"] = sorted(set(ctx.problems))

    # ---- code -----------------------------------------------------------------

    def sync_code(self, ctx):
        urls = {GITHUB: ctx.repo["clone_url"], GITLAB: ctx.project["http_url_to_repo"]}
        remote = {side: ls_remote(urls[side], self.git_auth[side]) for side in (GITHUB, GITLAB)}
        ctx.refs = {side: mirrored_refs(remote[side]) for side in remote}
        refs = self.synced_refs(ctx)
        first_run = "refs" not in ctx.state
        base = ctx.state.get("refs", {})
        plan = {GITHUB: {}, GITLAB: {}}  # side -> {ref: (new sha or None, expected sha or None, why)}
        undecided, orphans = [], []
        for ref in sorted(set(refs[GITHUB]) | set(refs[GITLAB]) | set(base)):
            gh, gl, last = refs[GITHUB].get(ref), refs[GITLAB].get(ref), base.get(ref)
            if gh == gl:
                continue
            if gh is None or gl is None:
                has, sha = (GITHUB, gh) if gh else (GITLAB, gl)
                other = OTHER[has]
                if last is None and first_run and has == GITLAB:
                    orphans.append(ref)  # decided once the commits are fetched
                elif last is None:
                    plan[other][ref] = (sha, None, f"new on {SITE[has]}")
                elif sha == last:
                    plan[has][ref] = (None, sha, f"deleted on {SITE[other]}")
                else:
                    plan[other][ref] = (sha, None, f"deleted on {SITE[other]} but changed on {SITE[has]}: restored")
            elif last == gh:
                plan[GITHUB][ref] = (gl, gh, "changed on GitLab")
            elif last == gl:
                plan[GITLAB][ref] = (gh, gl, "changed on GitHub")
            else:
                undecided.append(ref)
        fork_plan, fork_specs = self.fork_branches(ctx, remote)
        for side in plan:
            plan[side].update(fork_plan[side])

        if not any(plan.values()) and not undecided and not orphans:
            log("  code: in sync")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                git("init", "-q", "--bare", tmp)
                for side, prefix in ((GITHUB, "gh"), (GITLAB, "gl")):
                    git("fetch", "-q", "--no-tags", urls[side], f"+refs/heads/*:refs/{prefix}/heads/*",
                        f"+refs/tags/*:refs/{prefix}/tags/*", *fork_specs[side], cwd=tmp, auth=self.git_auth[side])
                for ref in undecided:
                    gh, gl = refs[GITHUB][ref], refs[GITLAB][ref]
                    if is_ancestor(tmp, gl, gh):
                        plan[GITLAB][ref] = (gh, gl, "fast-forward from GitHub")
                    elif is_ancestor(tmp, gh, gl):
                        plan[GITHUB][ref] = (gl, gh, "fast-forward from GitLab")
                    else:
                        self.report(ctx, f"diverged:{ref}:{gh}:{gl}",
                                    f"{ctx.name}: {short_ref(ref)} has diverged (GitHub {gh[:8]}, GitLab {gl[:8]}). "
                                    "Merge one into the other, on either side, and the next sync copies the result.")
                for ref in orphans:
                    # First sync: a ref only GitLab has, whose commits GitHub
                    # already has, was most likely deleted on GitHub.
                    sha = refs[GITLAB][ref]
                    contained = git("for-each-ref", "--count=1", f"--contains={sha}^{{commit}}", "refs/gh/",
                                    cwd=tmp, check=False).stdout.strip()
                    if contained:
                        plan[GITLAB][ref] = (None, sha, "gone from GitHub (first sync)")
                    else:
                        plan[GITHUB][ref] = (sha, None, "new on GitLab")
                for side in (GITLAB, GITHUB):
                    self.push(ctx, tmp, side, urls[side], plan[side])
        if ctx.created and ctx.pushed.get(GITLAB) and not self.dry_run:
            self.gl_api.put(f"/projects/{ctx.project['id']}", {"default_branch": ctx.repo["default_branch"]})
        if not self.dry_run:
            ctx.state["refs"] = self.new_base(ctx, base)

    @staticmethod
    def synced_refs(ctx):
        """Refs under two-way sync: everything but the fork branches."""
        return {GITHUB: {r: s for r, s in ctx.refs[GITHUB].items() if not r.startswith(GL_FORK_BRANCH)},
                GITLAB: {r: s for r, s in ctx.refs[GITLAB].items() if not r.startswith(GH_FORK_BRANCH)}}

    def new_base(self, ctx, old_base):
        """Last synced value of each ref: refs equal on both sides, else unchanged."""
        refs = self.synced_refs(ctx)
        base = {}
        for ref in set(refs[GITHUB]) | set(refs[GITLAB]) | set(old_base):
            gh, gl = refs[GITHUB].get(ref), refs[GITLAB].get(ref)
            if gh is not None and gh == gl:
                base[ref] = gh
            elif (gh is not None or gl is not None) and ref in old_base:
                base[ref] = old_base[ref]
        return base

    def fork_branches(self, ctx, remote):
        """Pushes that keep a branch on the other side for each open fork request."""
        plan, specs = {GITHUB: {}, GITLAB: {}}, {GITHUB: [], GITLAB: []}
        wanted = {GITHUB: {}, GITLAB: {}}
        for pr in self.gh_api.paginate(f"/repos/{ctx.repo['full_name']}/pulls", state="open"):
            head = f"refs/pull/{pr['number']}/head"
            if is_fork(pr, ctx.repo) and head in remote[GITHUB]:
                wanted[GITLAB][f"{GH_FORK_BRANCH}{pr['number']}"] = remote[GITHUB][head]
                specs[GITHUB].append(f"+{head}:refs/gh/pull/{pr['number']}")
        for mr in self.gl_api.paginate(f"/projects/{ctx.project['id']}/merge_requests", state="opened"):
            head = f"refs/merge-requests/{mr['iid']}/head"
            if mr.get("source_project_id") != ctx.project["id"] and head in remote[GITLAB]:
                wanted[GITHUB][f"{GL_FORK_BRANCH}{mr['iid']}"] = remote[GITLAB][head]
                specs[GITLAB].append(f"+{head}:refs/gl/mr/{mr['iid']}")
        for side, prefix in ((GITLAB, GH_FORK_BRANCH), (GITHUB, GL_FORK_BRANCH)):
            have = {r: s for r, s in ctx.refs[side].items() if r.startswith(prefix)}
            for ref, sha in wanted[side].items():
                if have.get(ref) != sha:
                    plan[side][ref] = (sha, have.get(ref), "branch of a fork request")
            for ref, sha in have.items():
                if ref not in wanted[side]:
                    plan[side][ref] = (None, sha, "fork request closed")
        return plan, specs

    def push(self, ctx, repo_dir, side, url, updates):
        """Push `updates`; each ref only moves if it still has the expected value."""
        if not updates:
            return
        args, specs = ["push", "--porcelain"], []
        for ref, (sha, expected, why) in sorted(updates.items()):
            args.append(f"--force-with-lease={ref}:{expected or ''}")
            specs.append(f"{sha}:{ref}" if sha else f":{ref}")
            log(f"  code: {SITE[side]} {short_ref(ref)} {short_sha(expected)} -> {short_sha(sha)} ({why})")
        if self.dry_run:
            return
        if side == GITLAB:
            args += ["-o", "ci.skip"]
        result = git(*args, url, *specs, cwd=repo_dir, auth=self.git_auth[side], check=False)
        done, rejected = set(), False
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or ":" not in parts[1]:
                continue
            ref = parts[1].split(":", 1)[1]
            if ref not in updates:
                continue
            if parts[0] == "!":
                rejected = True
                self.report(ctx, f"push:{side}:{ref}:{updates[ref][0]}",
                            f"{ctx.name}: {SITE[side]} refused {short_ref(ref)}: {parts[2]} "
                            f"{result.stderr.strip()[-300:]}")
            else:
                done.add(ref)
        if result.returncode and not done and not rejected:
            raise SyncError(f"push to {SITE[side]} failed: {result.stderr.strip()[-1500:]}")
        for ref in done:
            sha = updates[ref][0]
            if sha:
                ctx.refs[side][ref] = sha
            else:
                ctx.refs[side].pop(ref, None)
        if done:
            ctx.pushed[side] = True

    # ---- releases (GitHub -> GitLab) -------------------------------------

    def sync_releases(self, ctx):
        releases = [r for r in self.gh_api.get(f"/repos/{ctx.repo['full_name']}/releases", per_page=100)
                    if not r["draft"]]
        if not releases:
            return
        pid = ctx.project["id"]
        existing = {r["tag_name"]: r for r in self.gl_api.paginate(f"/projects/{pid}/releases")}
        failed = False
        for r in reversed(releases):  # oldest first
            try:
                self.sync_release(ctx, pid, r, existing.get(r["tag_name"]))
            except ApiError as e:
                # Releases are compared in full on every run, so this is retried.
                warn(f"{ctx.name} release {r['tag_name']}: {e}")
                failed = True
        if not failed:
            log(f"  releases: {len(releases)} checked")

    def sync_release(self, ctx, pid, r, current):
        tag = r["tag_name"]
        name = r["name"] or tag
        description = render(r["body"], github_author(r.get("author")), r["html_url"], GITHUB)
        links = [{"name": a["name"], "url": a["browser_download_url"], "link_type": "other"}
                 for a in r.get("assets", [])]
        base = f"/projects/{pid}/releases"
        if current is None:
            self.gl_api.post(base, {"tag_name": tag, "name": name, "description": description,
                                    "released_at": r["published_at"] or r["created_at"], "assets": {"links": links}})
            log(f"  releases: {tag} created")
            return
        if split_footer(current.get("description"))[1] is None and ctx.cutoff \
                and parse_time(r["created_at"]) < ctx.cutoff:
            return  # copied by the importer: left as it is
        changes = {}
        if current["name"] != name:
            changes["name"] = name
        if norm(current.get("description")) != norm(description):
            changes["description"] = description
        if changes:
            self.gl_api.put(f"{base}/{quote(tag)}", changes)
            log(f"  releases: {tag} updated ({', '.join(sorted(changes))})")
        have = {link["url"] for link in current.get("assets", {}).get("links", [])}
        for link in links:
            if link["url"] not in have:
                self.gl_api.post(f"{base}/{quote(tag)}/assets/links", link)
                log(f"  releases: {tag}: link to {link['name']} added")


# ---- issues and pull requests ---------------------------------------------------


@dataclass
class Pair:
    gh: Item = None
    gl: Item = None
    origin: str = GITHUB  # site the item was created on

    @property
    def kind(self):
        return (self.gh or self.gl).kind

    @property
    def id(self):
        prefix = "i" if self.kind == "issue" else "p"
        return f"{prefix}{self.gh.number}" if self.gh else f"gitlab-{prefix}{self.gl.number}"

    @property
    def name(self):
        return " / ".join(item.name for item in (self.gh, self.gl) if item)

    @property
    def imported(self):
        return bool(self.gl and self.gl.imported)

    def changed_since(self, since):
        return any(item and (since[item.side] is None or item.updated >= since[item.side])
                   for item in (self.gh, self.gl))


def pair_items(gh_items, gl_items):
    """Match each item with its copy or original on the other site."""
    gl_map = {i.key: i for i in gl_items}
    gl_for_gh = {}
    for i in gl_items:  # importer links first, footers override them
        if i.imported and not i.link:
            gl_for_gh.setdefault(i.key, i)
    for i in gl_items:
        if i.link and i.link[1] == i.kind:
            gl_for_gh[i.link[1:]] = i
    pairs, used = [], set()
    for i in gh_items:
        if i.link:  # copy of a GitLab item
            gl = gl_map.get(i.link[1:]) if i.link[1] == i.kind else None
            if gl is None:
                continue  # its original is gone
            pairs.append(Pair(i, gl, GITLAB))
        else:
            gl = gl_for_gh.get(i.key)
            pairs.append(Pair(i, gl, GITHUB))
        if gl:
            used.add(gl.key)
    for i in gl_items:
        if i.key not in used and not i.link and not i.imported:
            pairs.append(Pair(None, i, GITLAB))
    return pairs


class IssueSync:
    def __init__(self, sync, ctx):
        self.sync, self.ctx = sync, ctx
        self.bases = ctx.state.setdefault("items", {})

    def run(self, since_arg):
        ctx = self.ctx
        start = dt.datetime.now(UTC)
        windows = ctx.state.get("since", {})
        since = {}
        for side in (GITHUB, GITLAB):
            if since_arg == "all":
                since[side] = None
            elif since_arg == "import":
                since[side] = ctx.cutoff
            elif isinstance(since_arg, dt.datetime):
                since[side] = since_arg
            else:
                since[side] = parse_time(windows.get(side)) or ctx.cutoff
        retry = set(ctx.state.get("retry", []))
        if not retry and not ctx.gh.changed_since(since[GITHUB]) and not ctx.gl.changed_since(since[GITLAB]):
            log("  issues/PRs: no change")
        else:
            pairs = pair_items(ctx.gh.items(), ctx.gl.items())
            todo = [p for p in pairs if p.id in retry or p.changed_since(since)]
            log(f"  issues/PRs: {len(todo)} changed")
            failed = []
            for p in todo:
                try:
                    self.sync_pair(p)
                except SyncError as e:
                    failed.append(p.id)
                    self.sync.report(ctx, f"item:{p.id}:{digest(str(e))}", f"{ctx.name} {p.name}: {e}")
            ctx.state["retry"] = failed
        if not self.sync.dry_run:
            ctx.state["since"] = {side: iso(start - WINDOW_MARGIN) for side in (GITHUB, GITLAB)}

    def sync_pair(self, p):
        if p.gh is None or p.gl is None:
            p = self.create_copy(p)
            if p is None:
                return
        else:
            self.merge(p)
        self.sync_comments(p)

    def create_copy(self, p):
        src = p.gh or p.gl
        side = OTHER[src.side]
        site = self.ctx.site(side)
        source = target = ""
        if src.kind == "pr":
            source = src.source
            if src.fork:
                source = f"github-pr/{src.number}" if src.side == GITHUB else f"gitlab-mr/{src.number}"
            target = src.target
            missing = [b for b in (source, target) if not self.ctx.has_branch(side, b)]
            if missing:
                msg = f"branch {', '.join(missing)} missing on {SITE[side]}"
                if src.state == "open":
                    raise SyncError(msg)
                log(f"    {src.name} ({src.state}): not copied, {msg}")
                return None
        body = render(src.body, src.author, src.url, src.side)
        try:
            new = site.create_item(src.kind, src.title, body, src.labels, src.draft, source, target,
                                   number=src.number if side == GITLAB and src.kind == "issue" else None)
        except ApiError as e:
            if src.state == "open" or e.status != 422:
                raise
            log(f"    {src.name} ({src.state}): not copied, {e}")
            return None
        log(f"    {src.name}: copied to {new.name if new else SITE[side]}")
        if new is None:  # dry run
            return None
        if src.state != "open":
            site.update_item(new, {"state": "closed"})
            new.state = "closed"
            if src.state == "merged":
                site.add_comment(new, notice(f"Merged on {SITE[src.side]}: {src.url}"))
        p = Pair(src, new, GITHUB) if src.side == GITHUB else Pair(new, src, GITLAB)
        self.bases[p.id] = {"title": src.title, "labels": src.labels, "state": src.state, "draft": src.draft,
                            "target": src.target, "ob": digest(src.body), "cb": digest(body)}
        return p

    def merge(self, p):
        """Copy the fields changed on one side to the other (latest wins if both)."""
        gh, gl = p.gh, p.gl
        base = self.bases.get(p.id)
        gh_newer = gh.updated >= gl.updated
        changes = {GITHUB: {}, GITLAB: {}}
        notices = {}

        def pick(name, gh_value, gl_value):
            if gh_value == gl_value:
                return gh_value
            if base is None or name not in base:
                winner = p.origin
            elif gl_value == base[name]:
                winner = GITHUB
            elif gh_value == base[name]:
                winner = GITLAB
            else:
                winner = GITHUB if gh_newer else GITLAB
            value = gh_value if winner == GITHUB else gl_value
            changes[OTHER[winner]][name] = value
            return value

        title = pick("title", gh.title, gl.title)
        labels = pick("labels", gh.labels, gl.labels)
        draft, target = gh.draft, gh.target
        if p.kind == "pr":
            draft = pick("draft", gh.draft, gl.draft)
            if "merged" not in (gh.state, gl.state):
                target = pick("target", gh.target, gl.target)
        state = self.merge_state(p, pick, changes, notices)
        origin_body, copy_body = self.merge_body(p, base, changes)
        for side, fields in changes.items():
            if fields:
                item = gh if side == GITHUB else gl
                self.ctx.site(side).update_item(item, fields)
                log(f"    {item.name}: {', '.join(sorted(fields))} updated from {SITE[OTHER[side]]}")
        for side, text in notices.items():
            self.ctx.site(side).add_comment(gh if side == GITHUB else gl, notice(text))
        self.bases[p.id] = {"title": title, "labels": labels, "state": state, "draft": draft, "target": target,
                            "ob": digest(origin_body), "cb": digest(copy_body)}

    def merge_state(self, p, pick, changes, notices):
        gh, gl = p.gh, p.gl
        if gh.state == gl.state:
            return gh.state
        if p.kind == "pr" and "merged" in (gh.state, gl.state):
            merged, other = (gh, gl) if gh.state == "merged" else (gl, gh)
            if other.state != "open":
                return "merged"
            if self.ctx.pushed.get(other.side) and not self.sync.dry_run:
                if wait_merged(self.ctx.site(other.side), other).state == "merged":
                    return "merged"  # the push merged it there too
            changes[other.side]["state"] = "closed"
            notices[other.side] = f"Merged on {SITE[merged.side]}: {merged.url}"
            return "merged"
        return pick("state", gh.state, gl.state)

    def merge_body(self, p, base, changes):
        """Origin text and copy text after the sync."""
        o, c = (p.gh, p.gl) if p.origin == GITHUB else (p.gl, p.gh)
        o_body, c_body = norm(o.body), norm(c.body)
        wanted = norm(render(o_body, o.author, o.url, o.side))
        if c_body == wanted:
            return o_body, c_body
        if base is None:
            if loose(c_body) == loose(o_body):
                return o_body, c_body  # same text, formatted by the GitLab importer
            changes[c.side]["body"] = wanted
            return o_body, wanted
        o_changed = digest(o_body) != base.get("ob")
        c_changed = digest(c_body) != base.get("cb")
        if not o_changed and not c_changed:
            return o_body, c_body
        if c_changed and (not o_changed or c.updated > o.updated):
            new_origin = unrender(c_body)
            if new_origin != o_body:
                changes[o.side]["body"] = o_body = new_origin
            wanted = norm(render(o_body, o.author, o.url, o.side))
            if wanted != c_body:  # e.g. the footer was removed
                changes[c.side]["body"] = c_body = wanted
            return o_body, c_body
        changes[c.side]["body"] = wanted
        return o_body, wanted

    def sync_comments(self, p):
        if self.sync.anonymous_gitlab:
            log(f"    {p.name}: comments not compared (GitLab hides them without a token)")
            return
        cutoff = self.ctx.cutoff if p.imported else None
        gh_comments, gl_comments = self.ctx.gh.comments(p.gh), self.ctx.gl.comments(p.gl)
        self.copy_comments(gh_comments, p.gh, gl_comments, p.gl, cutoff)
        self.copy_comments(gl_comments, p.gl, gh_comments, p.gh, cutoff)

    def copy_comments(self, src_comments, src_item, dst_comments, dst_item, cutoff):
        site = self.ctx.site(dst_item.side)
        copies = {c.link: c for c in dst_comments if c.link and c.link.startswith(src_item.url + "#")}
        created = edited = deleted = 0
        for c in sorted(src_comments, key=lambda c: c.created):
            if c.link or c.notice or (cutoff and c.created < cutoff):
                continue  # a copy, a sync notice, or already copied by the importer
            wanted = render(c.text(), c.author, c.url, c.side)
            copy = copies.pop(c.url, None)
            if copy is None:
                site.add_comment(dst_item, wanted)
                created += 1
            elif norm(copy.body) != norm(wanted):
                site.edit_comment(dst_item, copy, wanted)
                edited += 1
        for copy in copies.values():  # original deleted
            site.delete_comment(dst_item, copy)
            deleted += 1
        if created or edited or deleted:
            log(f"    {dst_item.name}: comments from {SITE[src_item.side]} "
                f"+{created} ~{edited} -{deleted}")


# ---- state --------------------------------------------------------------------


class FileStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def save(self, state):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path + ".tmp", "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
        os.replace(self.path + ".tmp", self.path)


class BranchStore:
    """state.json alone on a branch, replaced by a new parentless commit on save."""

    def __init__(self, token, repo):
        self.api = Api("https://api.github.com", github_headers(token))
        self.repo = repo

    def load(self):
        text = self.api.request("GET", f"/repos/{self.repo}/contents/{STATE_FILE}", {"ref": STATE_BRANCH},
                                allow_404=True, raw=True)[0]
        return json.loads(text) if text else {}

    def save(self, state):
        git_api = f"/repos/{self.repo}/git"
        text = json.dumps(state, indent=1, sort_keys=True) + "\n"
        blob = self.api.post(f"{git_api}/blobs", {"content": text, "encoding": "utf-8"})
        tree = self.api.post(f"{git_api}/trees",
                             {"tree": [{"path": STATE_FILE, "mode": "100644", "type": "blob", "sha": blob["sha"]}]})
        commit = self.api.post(f"{git_api}/commits",
                               {"message": "Update the GitLab sync state", "tree": tree["sha"], "parents": []})
        if self.api.get(f"{git_api}/ref/heads/{STATE_BRANCH}", allow_404=True):
            self.api.patch(f"{git_api}/refs/heads/{STATE_BRANCH}", {"sha": commit["sha"], "force": True})
        else:
            self.api.post(f"{git_api}/refs", {"ref": f"refs/heads/{STATE_BRANCH}", "sha": commit["sha"]})


# ---- main ---------------------------------------------------------------------


def parse_since(value):
    value = (value or "").strip()
    if value in ("", "import", "all"):
        return value or None
    return parse_time(value)


def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and SUMMARY:
        with open(path, "a") as f:
            f.write("### GitLab sync\n\n" + "\n".join(f"- {line}" for line in SUMMARY) + "\n")


def main():
    env_repos = os.environ.get("REPOS", "").replace(",", " ").split()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", action="append", help="GitHub repository name, repeatable (default: all)")
    parser.add_argument("--since", default=os.environ.get("SINCE", ""),
                        help='issues/PRs updated since: "import", "all" or an ISO 8601 date '
                             "(default: since the previous run)")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN") == "true",
                        help="read everything, write nothing (state included)")
    parser.add_argument("--only", action="append", choices=["code", "issues", "releases"],
                        help="sync only these parts (repeatable)")
    parser.add_argument("--state-file", default=os.path.expanduser("~/.cache/2scloud-gitlab-sync/state.json"),
                        help="state file, used when STATE_REPO is not set")
    args = parser.parse_args()

    if not args.dry_run:
        missing = [name for name in ("GITLAB_TOKEN", "GH_SYNC_TOKEN") if not os.environ.get(name)]
        if missing:
            error(f"missing: {', '.join(missing)}")
            return 2
    sync = Sync(args.dry_run)
    if os.environ.get("STATE_REPO"):
        store = BranchStore(os.environ.get("STATE_TOKEN") or os.environ.get("GITHUB_TOKEN", ""),
                            os.environ["STATE_REPO"])
    else:
        store = FileStore(args.state_file)
    wanted = {name.lower() for name in (args.repo or env_repos)}
    parts = set(args.only or ["code", "issues", "releases"])
    try:
        since = parse_since(args.since)
        state = store.load()
        repos = sync.github_repos(wanted)
    except (SyncError, ValueError) as e:
        error(str(e))
        return 1
    saved = json.dumps(state, sort_keys=True)
    log(f"{sync.org} <-> {sync.group}: {len(repos)} repo(s)" + (" (dry run)" if args.dry_run else ""))

    failures = 0
    for repo in repos:
        name = repo["name"]
        if name.startswith("."):
            log(f"== {repo['full_name']}: skipped, GitLab project names cannot start with a dot")
            continue
        rstate = state.setdefault("repos", {}).setdefault(name, {})
        with LogGroup(f"{repo['full_name']} <-> {sync.group}/{name}"):
            try:
                sync.sync_repo(repo, rstate, parts, since)
            except SyncError as e:
                error(f"{repo['full_name']}: {e}")
                failures += 1
            except Exception:  # a bug: report it and go on with the next repo
                error(f"{repo['full_name']}: unexpected error\n{traceback.format_exc()}")
                failures += 1
        text = json.dumps(state, sort_keys=True)
        if not args.dry_run and text != saved:
            try:
                store.save(state)
                saved = text
            except SyncError as e:
                error(f"state not saved: {e}")
                failures += 1
    log(f"API calls: GitHub {sync.gh_api.calls}, GitLab {sync.gl_api.calls}")
    write_summary()
    return 1 if failures or sync.new_problems else 0


if __name__ == "__main__":
    sys.exit(main())
