#!/usr/bin/env python3
import os
import json
import subprocess
import sys
import shlex
from typing import List, Tuple, Optional

BASE_URL = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
HEADERS = {
    "Accept": "application/vnd.github+json",
    **({"Authorization": f"Bearer {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}),
}

def run(cmd: List[str], cwd: Optional[str] = None, check: bool = True) -> Tuple[int, str, str]:
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, output=out, stderr=err)
    return p.returncode, out, err

def setup_git_config(repo_dir: str) -> None:
    run(["git", "config", "user.name", "OpenSearch Bot"], cwd=repo_dir, check=False)
    run(["git", "config", "user.email", "opensearch-bot@amazon.com"], cwd=repo_dir, check=False)
    run(["git", "config", "rerere.enabled", "true"], cwd=repo_dir, check=False)

def abort_in_progress_ops(repo_dir: str) -> None:
    for cmd in [
        ["git", "rebase", "--abort"],
        ["git", "cherry-pick", "--abort"],
        ["git", "am", "--abort"],
        ["git", "merge", "--abort"],
    ]:
        run(cmd, cwd=repo_dir, check=False)

def ensure_clean_repo(repo_dir: str) -> None:
    abort_in_progress_ops(repo_dir)
    run(["git", "reset", "--hard"], cwd=repo_dir, check=False)
    run(["git", "clean", "-fd"], cwd=repo_dir, check=False)

def fetch_pr(owner: str, repo: str, pr_number: int) -> dict:
    import requests
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def list_commits_for_pr(owner: str, repo: str, pr_number: int) -> List[str]:
    import requests
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    commits = r.json()
    return [c["sha"] for c in commits]

def resolve_changelog_conflict(repo_dir: str, path: str = "CHANGELOG.md", prefer_pr_on_top: bool = True) -> bool:
    full = os.path.join(repo_dir, path)
    if not os.path.exists(full):
        return False
    with open(full, "r", encoding="utf-8") as f:
        content = f.read()
    if "<<<<<<< " not in content:
        return False
    lines = content.splitlines()
    out = []
    i = 0
    changed = False
    while i < len(lines):
        line = lines[i]
        if line.startswith("<<<<<<< "):
            changed = True
            ours = []
            theirs = []
            i += 1
            while i < len(lines) and not lines[i].startswith("======="):
                ours.append(lines[i]); i += 1
            i += 1
            while i < len(lines) and not lines[i].startswith(">>>>>>> "):
                theirs.append(lines[i]); i += 1
            i += 1
            if prefer_pr_on_top:
                out.extend(theirs)
                out.extend(ours)
            else:
                out.extend(ours)
                out.extend(theirs)
        else:
            out.append(line)
            i += 1
    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + ("\n" if content.endswith("\n") else ""))
    run(["git", "add", path], cwd=repo_dir, check=False)
    return changed

def cherry_pick_with_conflict_handling(repo_dir: str, shas: List[str], prefer_pr_on_top_changelog: bool = True, take_pr_side_for_others: bool = True) -> None:
    for sha in shas:
        code, out, err = run(["git", "cherry-pick", "-x", sha], cwd=repo_dir, check=False)
        if code == 0:
            continue
        # Resolve non-changelog files by side selection
        _, out2, _ = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir, check=False)
        for f in [f for f in out2.splitlines() if f.strip() and os.path.basename(f) != "CHANGELOG.md"]:
            run(["git", "checkout", "--theirs" if take_pr_side_for_others else "--ours", f], cwd=repo_dir, check=False)
            run(["git", "add", f], cwd=repo_dir, check=False)
        # Resolve changelog
        resolve_changelog_conflict(repo_dir, "CHANGELOG.md", prefer_pr_on_top=prefer_pr_on_top_changelog)
        # Continue or skip
        code2, outc, errc = run(["git", "cherry-pick", "--continue"], cwd=repo_dir, check=False)
        if code2 != 0:
            text = (outc or "") + "\n" + (errc or "")
            if "The previous cherry-pick is now empty" in text or "nothing to commit" in text:
                run(["git", "cherry-pick", "--skip"], cwd=repo_dir, check=False)
            else:
                raise subprocess.CalledProcessError(code2 or 1, ["git", "cherry-pick", "--continue"], output=outc, stderr=errc)

def backport_pr(owner: str, repo: str, repo_dir: str, pr_number: int, target_branch: str) -> None:
    setup_git_config(repo_dir)
    ensure_clean_repo(repo_dir)
    run(["git", "fetch", "--all", "--prune"], cwd=repo_dir, check=False)
    run(["git", "checkout", target_branch], cwd=repo_dir)
    run(["git", "pull", "--ff-only"], cwd=repo_dir, check=False)

    pr = fetch_pr(owner, repo, pr_number)
    shas = list_commits_for_pr(owner, repo, pr_number)

    new_branch = f"backport-{pr_number}-to-{target_branch}"
    run(["git", "checkout", "-b", new_branch], cwd=repo_dir, check=False)

    cherry_pick_with_conflict_handling(repo_dir, shas, prefer_pr_on_top_changelog=True, take_pr_side_for_others=True)

    run(["git", "push", "-u", "origin", new_branch], cwd=repo_dir, check=False)
    abort_in_progress_ops(repo_dir)
    print(f"✅ Backport branch pushed: {new_branch}")

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Backport a PR to a target branch with robust conflict handling.")
    ap.add_argument("owner")
    ap.add_argument("repo")
    ap.add_argument("repo_dir")
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--target", required=True, help="Target backport branch, e.g., 2.x")
    args = ap.parse_args()
    backport_pr(args.owner, args.repo, args.repo_dir, args.pr, args.target)

if __name__ == "__main__":
    main()
