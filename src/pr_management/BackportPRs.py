#!/usr/bin/env python3

import os
import sys
import subprocess
import logging
import requests

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
    for cmd in [["git", "cherry-pick", "--abort"], ["git", "merge", "--abort"], ["git", "am", "--abort"]]:
        subprocess.run(cmd, cwd=repo_dir, check=False)
    subprocess.run(["git", "reset", "--hard"], cwd=repo_dir, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_dir, check=False)

def ensure_on_branch(repo_dir, branch_name):
    result = subprocess.run(["git", "branch", "--show-current"], cwd=repo_dir, text=True, capture_output=True)
    current = result.stdout.strip()
    if not current or current != branch_name:
        logging.info(f"Checking out branch: {branch_name}")
        subprocess.run(["git", "checkout", branch_name], cwd=repo_dir, check=True)

def run(cmd, cwd=None, check=True):
    logging.info(f"Running command: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)

def git_config(repo_dir):
    configs = [
        ("user.name", "OpenSearch Bot"),
        ("user.email", "opensearch-bot@amazon.com"),
        ("rerere.enabled", "true"),
    ]
    for key, value in configs:
        run(["git", "config", key, value], cwd=repo_dir, check=False)

def get_backport_prs(owner, repo, label="backport"):
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls?state=open&labels={label}"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()

def get_single_pr(owner, repo, pr_num):
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_num}"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()

def get_pr_commits(owner, repo, pr_num):
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_num}/commits"
    resp = requests.get(url, headers=HEADERS)
    resp.raise_for_status()
    return [c["sha"] for c in resp.json()]

def checkout_branch(repo_dir, branch):
    run(["git", "checkout", branch], cwd=repo_dir, check=True)

def resolve_changelog(repo_dir, prefer_theirs=True):
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

def resolve_all_conflicts(repo_dir):
    result = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir)
    for fname in result.stdout.splitlines():
        if fname == "CHANGELOG.md": continue
        run(["git", "checkout", "--theirs", fname], cwd=repo_dir)
        run(["git", "add", fname], cwd=repo_dir)

def cherry_pick_commits(repo_dir, commits, target_branch, new_branch):
    run(["git", "fetch", "origin", target_branch], cwd=repo_dir)
    run(["git", "checkout", "-b", new_branch, target_branch], cwd=repo_dir, check=True)
    safe_cleanup_git_state(repo_dir)
    for sha in commits:
        result = run(["git", "cherry-pick", sha], cwd=repo_dir, check=False)
        if result.returncode:
            resolve_all_conflicts(repo_dir)
            resolve_changelog(repo_dir)
            files = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=repo_dir, check=False)
            if files.stdout.strip():
                logging.error("Unresolved conflicts remain, aborting.")
                run(["git", "cherry-pick", "--abort"], cwd=repo_dir, check=False)
                sys.exit(1)
            run(["git", "cherry-pick", "--continue"], cwd=repo_dir)
    ensure_on_branch(repo_dir, new_branch)

def push_branch(repo_dir, remote, branch, remote_branch):
    run(["git", "push", "--force-with-lease", remote, f"{branch}:{remote_branch}"], cwd=repo_dir)

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("owner")
    parser.add_argument("repo")
    parser.add_argument("repo_dir")
    parser.add_argument("--target", required=True)
    parser.add_argument("--label", default="backport")
    args = parser.parse_args()

    git_config(args.repo_dir)
    safe_cleanup_git_state(args.repo_dir)
    backport_prs = get_backport_prs(args.owner, args.repo, args.label)
    if not backport_prs:
        logging.info("No backport PRs found with label '%s'", args.label)
        return
    for pr in backport_prs:
        pr_num = pr["number"]
        pr_full = get_single_pr(args.owner, args.repo, pr_num)
        commits = get_pr_commits(args.owner, args.repo, pr_num)
        target_branch = args.target
        new_branch = f"backport-pr-{pr_num}-{target_branch}"
        logging.info(f"Processing Backport PR #{pr_num}")
        cherry_pick_commits(args.repo_dir, commits, target_branch, new_branch)
        push_branch(args.repo_dir, "origin", new_branch, new_branch)
        safe_cleanup_git_state(args.repo_dir)
        logging.info(f"✅ PR #{pr_num} backport and push complete.")

if __name__ == "__main__":
    main()
