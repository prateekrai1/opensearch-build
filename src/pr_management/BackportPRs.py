import os
import requests
import subprocess
import sys

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
BASE_URL = "https://api.github.com"

def fetch_backport_prs(owner, repo):
    """Fetch backport PR's with the `backport` label"""
    url = f"{BASE_URL}/search/issues"
    query = f"repo:{owner}/{repo} label:backport is:pr is:open"
    response = requests.get(url, headers=HEADERS, params={"q":query})
    response.raise_for_status()
    return response.json()["items"]

def fetch_pr_details(owner, repo, pr_number):
    """Fetch PR details to get source and target branches"""
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def resolve_changelog_conflict(repo_dir, pr_branch, target_branch):
    """Resolve conflicts in CHANGELOG.md"""
    subprocess.run(["git","checkout",pr_branch], cwd=repo_dir)
    subprocess.run(["git","fetch","origin", target_branch], cwd=repo_dir)
    subprocess.run(["git","rebase",f"origin/{target_branch}"], cwd=repo_dir)

    conflicted_files = subprocess.check_output(
        ["git","diff","--name-only","--diff-filter=U"], cwd=repo_dir
    ).decode().strip().split("\n")
    if "CHANGELOG.md" in conflicted_files:
        print("Conflict detected in CHANGELOG.md. Resolving....")
        changelog_file = f"{repo_dir}/CHANGELOG.md"
        with open(changelog_file, "r") as file:
            lines = file.readlines()
        resolved_changes = []
        inside_conflict = False
        for line in lines:
            if line.startswith("<<<<<<<"):
                inside_conflict = True
                continue
            elif line.startswith("======="):
                resolved_changes.append("\n## New Changes (from backport PR):\n")
                continue
            elif line.startswith(">>>>>>>"):
                inside_conflict = False
                continue
            if not inside_conflict:
                resolved_changes.append(line)
        with open(changelog_file, "w") as file:
            file.writelines(resolved_changes)
        subprocess.run(["git", "add", "CHANGELOG.md"], cwd=repo_dir)
        subprocess.run(["git", "commit", "-m", "Resolved CHANGELOG.md conflict"], cwd=repo_dir)
    subprocess.run(["git", "push","--force-with-lease"], cwd=repo_dir)

def cherry_pick_commits(repo_dir, source_branch, target_branch):
    """Cherry-pick commits from source branch to target branch."""
    subprocess.run(["git", "checkout", target_branch], cwd=repo_dir)
    subprocess.run(["git", "cherry-pick", f"{source_branch}"], cwd=repo_dir)
    subprocess.run(["git", "push"], cwd=repo_dir)


def main_backport(owner, repo, repo_dir):
    """Main function to handle backport PRs"""
    print("Backport PRs script starting...")
    backport_prs = fetch_backport_prs(owner, repo)
    for pr in backport_prs:
        pr_number = pr["number"]
        pr_details = fetch_pr_details(owner, repo, pr_number)
        pr_branch = pr_details["head"]["ref"]
        target_branch = pr_details["base"]["ref"]
        print(f"Handling Backport PR #{pr_number}: {pr_branch} -> {target_branch}")
        try:
            resolve_changelog_conflict(repo_dir, pr_branch, target_branch)
        except subprocess.CalledProcessError as e:
            print(f"Rebase failed for Backport PR #{pr_number}. Attempting cherry-pick...")
            cherry_pick_commits(repo_dir, pr_branch, target_branch)

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python BackportPRs.py <owner> <repo> <repo_directory>")
        sys.exit(1)
    owner = sys.argv[1]
    repo = sys.argv[2]
    repo_dir = sys.argv[3]
    main_backport(owner, repo, repo_dir)