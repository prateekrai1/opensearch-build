import os
import requests
import subprocess


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
BASE_URL = "https://api.github.com"

def fetch_pr_details(owner, repo, pr_number):
    """Fetch PR details to get source and target branches"""
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def fetch_stalled_prs(owner, repo):
    """Fetch stalled PRs with the `stalled` label"""
    url = f"{BASE_URL}/search/issues"
    query = f"repo:{owner}/{repo} label:stalled is:pr is:open"
    response = requests.get(url, headers=HEADERS, params={"q": query})
    response.raise_for_status()
    return response.json()["items"]

def resolve_changelog_conflict(file_path):
    """Resolve Git conflict markers in CHANGELOG.md by merging both sides."""
    with open(file_path, 'r') as f:
        lines = f.readlines()

    merged_lines = []
    i = 0
    while i < len(lines):
        if lines[i].startswith('<<<<<<<'):
            i += 1
            local_changes = []
            while i < len(lines) and not lines[i].startswith('======='):
                local_changes.append(lines[i])
                i += 1
            i += 1
            incoming_changes = []
            while i < len(lines) and not lines[i].startswith('>>>>>>>'):
                incoming_changes.append(lines[i])
                i += 1
            i += 1
            merged_lines.extend(incoming_changes + local_changes)
        else:
            merged_lines.append(lines[i])
            i += 1

    with open(file_path, 'w') as f:
        f.writelines(merged_lines)

def rebase_pr(repo_dir, pr_branch, target_branch):
    """Rebase a stalled PR onto the target branch."""
    subprocess.run(["git", "checkout", pr_branch], cwd=repo_dir, check=True)
    subprocess.run(["git", "fetch", "origin", target_branch], cwd=repo_dir, check=True)
    result = subprocess.run(["git", "rebase", f"origin/{target_branch}"], cwd=repo_dir)

    if result.returncode != 0:
        conflict_path = os.path.join(repo_dir, "CHANGELOG.md")
        if os.path.exists(conflict_path):
            print("Conflict detected in CHANGELOG.md. Resolving...")
            resolve_changelog_conflict(conflict_path)
            subprocess.run(["git", "add", "CHANGELOG.md"], cwd=repo_dir, check=True)
            subprocess.run(["git", "rebase", "--continue"], cwd=repo_dir, check=True)

    subprocess.run(["git", "push", "--force-with-lease"], cwd=repo_dir, check=True)

def main_stalled(owner, repo, repo_dir):
    """Main function to handle stalled PRs"""
    print("Stalled PRs script starting...")
    stalled_prs = fetch_stalled_prs(owner,repo)
    for pr in stalled_prs:
        pr_number = pr["number"]
        pr_details = fetch_pr_details(owner, repo, pr_number)
        pr_branch = pr_details["head"]["ref"]
        target_branch = pr_details["base"]["ref"]
        print(f"Handling Stalled PR #{pr_number}: {pr_branch} -> {target_branch}")
        rebase_pr(repo_dir, pr_branch, target_branch)


