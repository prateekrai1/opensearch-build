#!/usr/bin/env python3

import os
import sys
import subprocess
import logging
import requests
from typing import List, Optional

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

BASE_URL = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}" if GITHUB_TOKEN else "",
}

def safe_cleanup_git_state(repo_dir):
    import shutil
    git_dir = os.path.join(repo_dir, ".git")
    for pattern in ["rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "MERGE_HEAD", "AM_HEAD"]:
        path = os.path.join(git_dir, pattern)
        if os.path.exists(path):
            logging.warning(f"Cleaning up stale git state: {path}")
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
    for cmd in [
        ["git", "rebase", "--abort"],
        ["git", "cherry-pick", "--abort"],
        ["git", "merge", "--abort"],
        ["git", "am", "--abort"]
    ]:
        subprocess.run(cmd, cwd=repo_dir, check=False)
    subprocess.run(["git", "reset", "--hard"], cwd=repo_dir, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_dir, check=False)

def ensure_on_branch(repo_dir, branch_name):
    # Ensures repo is on a branch before push
    result = subprocess.run(["git", "branch", "--show-current"], cwd=repo_dir, text=True, capture_output=True)
    current = result.stdout.strip()
    if not current or current != branch_name:
        logging.info(f"Checking out branch: {branch_name}")
        subprocess.run(["git", "checkout", branch_name], cwd=repo_dir, check=True)

def run(cmd: List[str], cwd: Optional[str] = None, check=True) -> subprocess.CompletedProcess:
    logging.info(f"Running command: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)

def git_config(repo_dir: str):
    configs = [
        ("user.name", "OpenSearch Bot"),
        ("user.email", "opensearch-bot@amazon.com"),
        ("rerere.enabled", "true"),
    ]
    for key, value in configs:
        run(["git", "config", key, value], cwd=repo_dir, check=False)

def get_pr(owner: str, repo: str, pr_num: int):
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_num}"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()

def checkout_branches(repo_dir: str, remote_url: str, remote_ref: str, branch: str):
    # Remove 'head' remote if already exists
    run(["git", "remote", "remove", "head"], cwd=repo_dir, check=False)
    run(["git", "remote", "add", "head", remote_url], cwd=repo_dir, check=False)
    run(["git", "fetch", "head", f"{remote_ref}:{branch}"], cwd=repo_dir)
    run(["git", "checkout", branch], cwd=repo_dir)

def resolve_changelog(repo_dir: str, prefer_theirs=True):
    path = os.path.join(repo_dir, "CHANGELOG.md")
    if not os.path.isfile(path):
        return
    with open(path) as f:
        content = f.read()
    if "<<<<<<< " not in content:
        return
    lines = content.splitlines()
    new_lines = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("<<<<<<< "):
            ours, theirs = [], []
            i += 1
            while i < len(lines) and not lines[i].startswith("======="):
                ours.append(lines[i]); i += 1
            i += 1
            while i < len(lines) and not lines[i].startswith(">>>>>>> "):
                theirs.append(lines[i]); i += 1
            i += 1
            if prefer_theirs:
                new_lines.extend(theirs)
                new_lines.extend(ours)
            else:
                new_lines.extend(ours)
                new_lines.extend(theirs)
        else:
            new_lines.append(lines[i])
            i += 1
    with open(path, "w") as f:
        f.write("\n".join(new_lines) + ("\n" if content[-1:] == "\n" else ""))
    run(["git", "add", "CHANGELOG.md"], cwd=repo_dir)

def resolve_all_conflicts(repo_dir: str):
    result = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir)
    for fname in result.stdout.splitlines():
        if fname == "CHANGELOG.md": continue
        run(["git", "checkout", "--theirs", fname], cwd=repo_dir)
        run(["git", "add", fname], cwd=repo_dir)

def rebase_and_resolve(repo_dir: str, pr_branch: str, target_branch: str):
    run(["git", "checkout", target_branch], cwd=repo_dir)
    run(["git", "pull", "--ff-only"], cwd=repo_dir)
    run(["git", "checkout", pr_branch], cwd=repo_dir)
    safe_cleanup_git_state(repo_dir)
    result = run(["git", "rebase", target_branch], cwd=repo_dir, check=False)
    if result.returncode:
        resolve_all_conflicts(repo_dir)
        resolve_changelog(repo_dir)
        files = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir, check=False)
        if files.stdout.strip():
            logging.error("Unresolved conflicts remain, aborting.")
            run(["git", "rebase", "--abort"], cwd=repo_dir, check=False)
            sys.exit(1)
        run(["git", "rebase", "--continue"], cwd=repo_dir)
    # Ensure we are on the branch (not detached HEAD) before push!
    ensure_on_branch(repo_dir, pr_branch)

def push_branch(repo_dir: str, remote: str, branch: str, remote_branch: str):
    run(["git", "push", "--force-with-lease", remote, f"{branch}:{remote_branch}"], cwd=repo_dir)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("owner")
    parser.add_argument("repo")
    parser.add_argument("repo_dir")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--target", default="main")
    args = parser.parse_args()
    git_config(args.repo_dir)
    safe_cleanup_git_state(args.repo_dir)
    pr = get_pr(args.owner, args.repo, args.pr)
    pr_head_repo_clone = pr["head"]["repo"]["clone_url"]
    pr_head_ref = pr["head"]["ref"]
    base_ref = pr["base"]["ref"]
    branch_name = f"pr-{args.pr}-{pr_head_ref}"
    logging.info(f"Processing PR #{args.pr}")
    checkout_branches(args.repo_dir, pr_head_repo_clone, pr_head_ref, branch_name)
    run(["git", "fetch", "origin", args.target], cwd=args.repo_dir)
    rebase_and_resolve(args.repo_dir, branch_name, args.target)
    push_branch(args.repo_dir, "head", branch_name, pr_head_ref)
    safe_cleanup_git_state(args.repo_dir)
    logging.info("✅ Rebase and push complete.")

if __name__ == "__main__":
    main()
