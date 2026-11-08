"""Playbook: revert the most recent change to the failing app's manifests.

Why it is safe to automate: the fix is "put back what was there before", so its content is already known-good
history, nothing is invented. It only fires when ALL of these hold:
  - the latest commit touching apps/<app>/ landed shortly before the incident opened (the blamed change);
  - that commit only *modified* files under apps/<app>/base or apps/<app>/overlays (no adds/deletes, no other
    directories such as infra/, no app source code);
  - it is not itself a revert or a sentinel fix (never revert a revert: that is how fix loops start);
  - none of those files changed again afterwards (so restoring the parent's content is exactly an undo).
"""
NAME = "revert-recent-change"
SKIP_SUBJECT_PREFIXES = ("revert", "sentinel(")


def plan(incident, repo, cfg):
    from . import Plan, app_prefix
    prefix = app_prefix(incident, repo)
    if not prefix:
        return None, "app not found under apps/ (or unsafe name)"
    app, opened = incident["app"], float(incident["opened_at"])
    cand = next((c for c in repo.log(prefix, limit=10) if c.ts <= opened + cfg.slack_s), None)
    if cand is None:
        return None, f"no commit has ever touched {prefix}"
    if cand.ts < opened - cfg.lookback_s:
        return None, f"latest change to {prefix} is {int(opened - cand.ts)}s older than the incident (window {int(cfg.lookback_s)}s)"
    if cand.subject.lower().startswith(SKIP_SUBJECT_PREFIXES):
        return None, f"latest change {cand.sha[:7]} is itself a revert/fix ({cand.subject!r}); not reverting a revert"
    changed = repo.changed(cand.sha)
    if not changed:
        return None, f"{cand.sha[:7]} has no single-parent diff (merge commit?)"
    allowed = (f"{prefix}base/", f"{prefix}overlays/")
    bad = [f"{st} {p}" for st, p in changed if st != "M" or not p.startswith(allowed)]
    if bad:
        return None, f"{cand.sha[:7]} touches things this playbook will not revert: {', '.join(bad)}"
    files = {}
    for _, path in changed:
        if repo.read(path) != repo.show(cand.sha, path):
            return None, f"{path} changed again after {cand.sha[:7]}; a plain revert is not an exact undo"
        before = repo.show(f"{cand.sha}^", path)
        if before is None:
            return None, f"{path} did not exist before {cand.sha[:7]}"
        files[path] = before
    return Plan(
        playbook=NAME, app=app, files=files,
        title=f"sentinel({app}): revert {cand.sha[:7]} \"{cand.subject[:60]}\"",
        rationale=(f"The incident opened {int(opened - cand.ts)}s after commit `{cand.sha[:7]}` by {cand.author} changed "
                   f"`{prefix}`. This restores the files to their content before that commit."),
        changes=[f"restore {p} to its content before {cand.sha[:7]}" for p in sorted(files)]), ""
