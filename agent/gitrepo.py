"""Read-only view of the repo at one ref, via the local git binary. The fast path plans against this snapshot.

Reads only; nothing here writes to the working tree, the index, or any remote. `fetch` updates a private ref
(refs/sentinel/main by default) and never touches the checked-out branch.
"""
import io
import subprocess
import tarfile
from collections import namedtuple
from pathlib import Path

Commit = namedtuple("Commit", "sha ts author subject")


class GitRepo:
    def __init__(self, path, ref="refs/sentinel/main"):
        self.path, self.ref = Path(path), ref

    def _git(self, *args, check=True):
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.decode('utf-8', 'replace').strip()}")
        return r

    def fetch(self, url, branch="main"):
        self._git("fetch", "-q", url, f"+refs/heads/{branch}:{self.ref}")

    def head(self):
        return self._git("rev-parse", self.ref).stdout.decode().strip()

    def show(self, rev, path):
        r = self._git("show", f"{rev}:{path}", check=False)
        return r.stdout.decode("utf-8") if r.returncode == 0 else None

    def read(self, path):
        return self.show(self.ref, path)

    def ls(self, prefix):
        """File paths under `prefix` at the ref."""
        out = self._git("ls-tree", "-r", "--name-only", self.ref, "--", prefix, check=False).stdout.decode("utf-8")
        return [l for l in out.splitlines() if l]

    def log(self, prefix, limit=10):
        """prefix: one pathspec or a list of them."""
        specs = [prefix] if isinstance(prefix, str) else list(prefix)
        out = self._git("log", self.ref, "-n", str(limit), "--format=%H%x1f%ct%x1f%an%x1f%s", "--", *specs).stdout.decode("utf-8")
        res = []
        for line in out.splitlines():
            sha, ts, author, subject = line.split("\x1f", 3)
            res.append(Commit(sha, float(ts), author, subject))
        return res

    def diff(self, sha, prefix, max_bytes=6000):
        """Unified diff of one commit (vs its first parent) limited to `prefix`, truncated to max_bytes."""
        specs = [prefix] if isinstance(prefix, str) else list(prefix)
        out = self._git("show", "--format=", "--no-color", "-U2", sha, "--", *specs).stdout.decode("utf-8", "replace")
        return out if len(out) <= max_bytes else out[:max_bytes] + "\n[... diff truncated]"

    def changed(self, sha):
        """[(status, path)] for one commit vs its first parent. Merge commits yield [] (the playbooks decline those)."""
        out = self._git("diff-tree", "--no-commit-id", "--name-status", "-r", sha).stdout.decode("utf-8")
        return [tuple(l.split("\t", 1)) for l in out.splitlines() if "\t" in l]

    def extract(self, paths, dest):
        """Write the given paths as they are at the ref into `dest` (used for the validation build)."""
        data = self._git("archive", "--format=tar", self.ref, *paths).stdout
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(dest, filter="data")
