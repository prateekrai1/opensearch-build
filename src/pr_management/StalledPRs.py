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
    """Run a subprocess and return (code, out, err)."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    if check and p.returncode != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd, output=out, stderr=err)
    return p.returncode, out, err

def log(cmd: List[str]) -> None:
    print(f"$ {' '.join(shlex.quote(c) for c in cmd)}")

def setup_git_config(repo_dir: str) -> None:
    run(["git", "config", "user.name", "OpenSearch Bot"], cwd=repo_dir, check=False)
    run(["git", "config", "user.email", "opensearch-bot@amazon.com"], cwd=repo_dir, check=False)
    run(["git", "config", "rerere.enabled", "true"], cwd=repo_dir, check=False)

def abort_in_progress_ops(repo_dir: str) -> None:
    """Best-effort abort of any in-progress git ops that could poison a new rebase."""
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

def fetch_pr_details(owner: str, repo: str, pr_number: int) -> dict:
    import requests
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def checkout_pr(repo_dir: str, pr_head_repo_clone_url: str, pr_head_ref: str, local_branch: str) -> None:
    # Ensure remotes are present/fresh
    run(["git", "remote", "-v"], cwd=repo_dir, check=False)
    # Create or update a dedicated 'head' remote pointing at the PR source repo
    code, out, _ = run(["git", "remote"], cwd=repo_dir, check=False)
    remotes = out.split()
    if "head" not in remotes:
        run(["git", "remote", "add", "head", pr_head_repo_clone_url], cwd=repo_dir, check=False)
    else:
        run(["git", "remote", "set-url", "head", pr_head_repo_clone_url], cwd=repo_dir, check=False)
    # Fetch the PR head ref into a local working branch
    run(["git", "fetch", "head", f"{pr_head_ref}:{local_branch}"], cwd=repo_dir)
    run(["git", "checkout", local_branch], cwd=repo_dir)

def resolve_changelog_conflict(repo_dir: str, path: str = "CHANGELOG.md", prefer_pr_on_top: bool = True) -> bool:
    """
    Resolve a typical conflict block in CHANGELOG.md by concatenating both sides
    in a deterministic order. Return True if conflict was found and resolved.
    prefer_pr_on_top=True keeps PR (theirs during rebase) entries before main.
    """
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
            # collect ours
            while i < len(lines) and not lines[i].startswith("======="):
                ours.append(lines[i]); i += 1
            i += 1  # skip =======
            # collect theirs
            while i < len(lines) and not lines[i].startswith(">>>>>>> "):
                theirs.append(lines[i]); i += 1
            i += 1  # skip >>>>>>>
            if prefer_pr_on_top:
                out.extend(theirs)  # PR side first during rebase
                out.extend(ours)
            else:
                out.extend(ours)
                out.extend(theirs)
        else:
            out.append(line)
            i += 1
    with open(full, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + ("\n" if content.endswith("\n") else ""))
    run(["git", "add", path], cwd=repo_dir)
    return changed

def add_all_conflicted_files(repo_dir: str, take_pr_side: bool = True, exclude_changelog: bool = True) -> List[str]:
    """
    Auto-resolve conflicts by taking the PR side (theirs during rebase) or target side (ours).
    Returns list of files resolved.
    """
    _, out, _ = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir, check=False)
    files = [f for f in out.splitlines() if f.strip()]
    resolved = []
    for fpath in files:
        if exclude_changelog and os.path.basename(fpath) == "CHANGELOG.md":
            continue
        if take_pr_side:
            run(["git", "checkout", "--theirs", fpath], cwd=repo_dir, check=False)  # PR side during rebase
        else:
            run(["git", "checkout", "--ours", fpath], cwd=repo_dir, check=False)   # target branch side
        run(["git", "add", fpath], cwd=repo_dir, check=False)
        resolved.append(fpath)
    return resolved

def continue_or_skip_rebase(repo_dir: str) -> None:
    """Try to continue rebase; if git reports nothing to commit, skip."""
    code, out, err = run(["git", "rebase", "--continue"], cwd=repo_dir, check=False)
    if code == 0:
        return
    text = (out or "") + "\n" + (err or "")
    if "No changes" in text or "nothing to commit" in text or "The previous cherry-pick is now empty" in text:
        run(["git", "rebase", "--skip"], cwd=repo_dir, check=False)
    else:
        raise subprocess.CalledProcessError(code or 1, ["git", "rebase", "--continue"], output=out, stderr=err)

def rebase_pr_onto_target(repo_dir: str, pr_branch: str, target_branch: str, prefer_pr_on_top_changelog: bool = True, take_pr_side_for_others: bool = True) -> None:
    # Make sure target is up to date
    run(["git", "fetch", "--all", "--prune"], cwd=repo_dir, check=False)
    run(["git", "checkout", target_branch], cwd=repo_dir)
    run(["git", "pull", "--ff-only"], cwd=repo_dir, check=False)

    # Switch to PR branch and rebase
    run(["git", "checkout", pr_branch], cwd=repo_dir)
    abort_in_progress_ops(repo_dir)
    code, out, err = run(["git", "rebase", target_branch], cwd=repo_dir, check=False)
    if code == 0:
        return  # clean rebase

    # Conflict path
    add_all_conflicted_files(repo_dir, take_pr_side=take_pr_side_for_others, exclude_changelog=True)
    resolve_changelog_conflict(repo_dir, "CHANGELOG.md", prefer_pr_on_top=prefer_pr_on_top_changelog)
    continue_or_skip_rebase(repo_dir)

    while True:
        _, conflicts, _ = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir, check=False)
        if not conflicts.strip():
            break
        add_all_conflicted_files(repo_dir, take_pr_side=take_pr_side_for_others, exclude_changelog=True)
        resolve_changelog_conflict(repo_dir, "CHANGELOG.md", prefer_pr_on_top=prefer_pr_on_top_changelog)
        continue_or_skip_rebase(repo_dir)

def force_push(repo_dir: str, remote: str, src_local_branch: str, dest_remote_branch: str) -> None:
    # Push local branch back to the PR head repo/branch (e.g., remote='head', branch='test-stalled')
    run(["git", "push", "--force-with-lease", remote, f"{src_local_branch}:{dest_remote_branch}"], cwd=repo_dir)

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Rebase PR branch onto target branch and resolve common conflicts.")
    ap.add_argument("owner", help="GitHub org/owner of the base repository (e.g., opensearch-project)")
    ap.add_argument("repo", help="GitHub repository name (e.g., OpenSearch)")
    ap.add_argument("repo_dir", help="Local path to a checkout of the base repository")
    ap.add_argument("--pr", type=int, required=True, help="Pull request number to operate on")
    ap.add_argument("--target", default="main", help="Target branch to rebase onto (default: main)")
    ap.add_argument("--prefer-pr-on-top-changelog", action="store_true", help="Keep PR CHANGELOG entries above main entries during conflict resolution")
    ap.add_argument("--take-pr-side", action="store_true", help="When non-CHANGELOG conflicts occur during rebase, prefer the PR side")
    args = ap.parse_args()

    owner, repo, repo_dir = args.owner, args.repo, args.repo_dir
    pr_number = args.pr
    target_branch = args.target
    prefer_pr = args.prefer_pr_on_top_changelog
    take_pr_side = args.take_pr_side

    if not os.path.isdir(repo_dir):
        print(f"Repository directory not found: {repo_dir}")
        sys.exit(2)

    setup_git_config(repo_dir)
    ensure_clean_repo(repo_dir)

    # Pull PR details
    pr = fetch_pr_details(owner, repo, pr_number)
    head = pr["head"]
    base = pr["base"]
    pr_head_repo_clone = head["repo"]["clone_url"]
    pr_head_ref = head["ref"]                  # actual PR source branch name
    pr_branch = f"pr-{pr_number}-{pr_head_ref}"  # local working branch

    print(f"Handling PR #{pr_number}: {pr_head_ref} -> {base['ref']}")
    checkout_pr(repo_dir, pr_head_repo_clone, pr_head_ref, pr_branch)

    # Ensure we have the up-to-date target
    run(["git", "fetch", "origin", target_branch], cwd=repo_dir)
    run(["git", "checkout", pr_branch], cwd=repo_dir)

    # Perform the rebase with robust conflict handling
    rebase_pr_onto_target(repo_dir, pr_branch, target_branch, prefer_pr_on_top_changelog=prefer_pr, take_pr_side_for_others=take_pr_side)

    # Push back to the PR's head repo/branch
    force_push(repo_dir, remote="head", src_local_branch=pr_branch, dest_remote_branch=pr_head_ref)

    # Cleanup safety
    abort_in_progress_ops(repo_dir)
    print("✅ Rebase completed and force-pushed.")

if __name__ == "__main__":
    main()
