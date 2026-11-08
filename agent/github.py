"""Minimal GitHub REST client for the fixer identity (Phase 4). Standard library only.

The fixer's only power is: create a branch, commit to it, open a pull request, read PR state, delete its own
branch. There is deliberately no merge call and nothing that writes to `main`: autonomy level L1 means a human
approves. `preflight()` refuses to run unless GitHub itself enforces that (main is protected, token is not admin).
"""
import json
import urllib.error
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, status, message):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def _urllib_transport(method, url, headers, body):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"message": raw.decode("utf-8", "replace")[:300]}
    except urllib.error.URLError as e:
        raise GitHubError(0, f"network error: {e.reason}")


class GitHub:
    def __init__(self, repo, token, transport=None):
        """repo: 'owner/name'. transport(method, url, headers, body_bytes) -> (status, json); injectable for tests."""
        if "/" not in repo:
            raise ValueError("repo must be owner/name")
        self.repo, self.token = repo, token
        self._t = transport or _urllib_transport

    def _req(self, method, path, body=None, ok=(200, 201, 204)):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "Authorization": f"Bearer {self.token}", "User-Agent": "edgeloop-sentinel-fixer",
                   "Content-Type": "application/json"}
        status, data = self._t(method, f"{API}{path}", headers, None if body is None else json.dumps(body).encode())
        if status not in ok:
            raise GitHubError(status, (data or {}).get("message", "") if isinstance(data, dict) else str(data))
        return data

    # --- reads ---------------------------------------------------------------
    def whoami(self):
        return self._req("GET", "/user")["login"]

    def repo_info(self):
        return self._req("GET", f"/repos/{self.repo}")

    def branch(self, name):
        return self._req("GET", f"/repos/{self.repo}/branches/{name}")

    def pull(self, number):
        return self._req("GET", f"/repos/{self.repo}/pulls/{number}")

    # --- the only writes the fixer has ---------------------------------------------
    def open_fix_pr(self, *, base_branch, base_sha, branch, files, message, title, body):
        """One commit on a NEW branch (parent = base_sha), then a PR into base_branch. Returns (number, url).
        files: {path: new_text}. Refuses to touch the base branch or reuse an existing one."""
        if branch == base_branch or not branch.startswith("sentinel/"):
            raise ValueError(f"refusing to write to branch {branch!r}")
        if not files:
            raise ValueError("no files to commit")
        r = self.repo
        tree_sha = self._req("GET", f"/repos/{r}/git/commits/{base_sha}")["tree"]["sha"]
        tree = self._req("POST", f"/repos/{r}/git/trees", {"base_tree": tree_sha, "tree": [
            {"path": p, "mode": "100644", "type": "blob", "content": c} for p, c in sorted(files.items())]})
        commit = self._req("POST", f"/repos/{r}/git/commits", {"message": message, "tree": tree["sha"], "parents": [base_sha]})
        self._req("POST", f"/repos/{r}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})   # new ref only
        pr = self._req("POST", f"/repos/{r}/pulls", {"title": title, "head": branch, "base": base_branch, "body": body})
        return pr["number"], pr["html_url"]

    def delete_own_branch(self, branch):
        if not branch.startswith("sentinel/"):
            raise ValueError(f"refusing to delete branch {branch!r}")
        self._req("DELETE", f"/repos/{self.repo}/git/refs/heads/{branch}", ok=(204, 422))


def preflight(gh, base_branch="main", allow_admin=False):
    """Hard rails before the fast path may open PRs. Returns a list of problems (empty = ok)."""
    problems = []
    try:
        login = gh.whoami()
    except GitHubError as e:
        return [f"token rejected by GitHub ({e})"]
    try:
        info = gh.repo_info()
    except GitHubError as e:
        return [f"token cannot read the repository ({e})"]
    perms = info.get("permissions") or {}
    if not perms.get("push"):
        problems.append(f"{login} has no write access to {gh.repo}; the PAT needs Contents + Pull requests write")
    if perms.get("admin") and not allow_admin:
        problems.append(f"{login} is an ADMIN of {gh.repo}: admins can bypass branch protection. Use a separate bot "
                        "account as a (write) collaborator, or pass --allow-admin-token knowingly")
    try:
        if not gh.branch(base_branch).get("protected"):
            problems.append(f"branch '{base_branch}' is not protected: enable branch protection (require a pull request) "
                            "first, otherwise L1 is only a convention")
    except GitHubError as e:
        problems.append(f"cannot read branch '{base_branch}' ({e})")
    return problems
