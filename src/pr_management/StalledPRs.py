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

LOCK_LABEL = "automation-in-progress"
FAIL_LABEL = "automation-conflict"
AUTO_COMMIT_PREFIX = "[automation] resolve conflicts"


def run(cmd, cwd=None, check=True):
    logging.info(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, cwd=cwd, check=check)


def git_config(repo_dir):
    subprocess.run(["git", "config", "user.name", "prateekrai1"], cwd=repo_dir)
    subprocess.run(["git", "config", "user.email", "prateekr651@gmail.com"], cwd=repo_dir)


def gh(method, url, **kwargs):
    r = requests.request(method, f"{BASE_URL}{url}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json() if r.text else None


def has_label(pr, label):
    return any(l["name"] == label for l in pr["labels"])


def add_label(owner, repo, pr, label):
    gh("POST", f"/repos/{owner}/{repo}/issues/{pr}/labels", json={"labels": [label]})


def remove_label(owner, repo, pr, label):
    gh("DELETE", f"/repos/{owner}/{repo}/issues/{pr}/labels/{label}")


def resolve_changelog_conflicts(path="CHANGELOG.md"):
    resolved = []
    left, right = [], []
    in_conflict = False
    side = None

    with open(path, "r") as f:
        for line in f:
            if line.startswith("<<<<<<<"):
                in_conflict = True
                left, right = [], []
                side = "left"
                continue
            if line.startswith("=======") and in_conflict:
                side = "right"
                continue
            if line.startswith(">>>>>>>") and in_conflict:
                resolved.extend(left)
                resolved.extend(right)
                in_conflict = False
                side = None
                continue

            if in_conflict:
                (left if side == "left" else right).append(line)
            else:
                resolved.append(line)

    with open(path, "w") as f:
        f.writelines(resolved)


def resolve_conflicts(repo_dir):
    files = subprocess.check_output(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_dir,
        text=True,
    ).splitlines()

    for f in files:
        if f == "CHANGELOG.md":
            resolve_changelog_conflicts(os.path.join(repo_dir, f))
        else:
            subprocess.run(["git", "checkout", "--theirs", f], cwd=repo_dir)

        subprocess.run(["git", "add", f], cwd=repo_dir)

    subprocess.run(
        ["git", "commit", "-m", AUTO_COMMIT_PREFIX],
        cwd=repo_dir,
        check=True,
    )


def main():
    owner, repo, repo_dir, target = sys.argv[1:5]

    git_config(repo_dir)

    prs = gh("GET", f"/repos/{owner}/{repo}/pulls?state=open&labels=stalled")

    for pr in prs:
        pr_num = pr["number"]

        if has_label(pr, "backport"):
            continue

        if has_label(pr, LOCK_LABEL) or has_label(pr, FAIL_LABEL):
            continue

        add_label(owner, repo, pr_num, LOCK_LABEL)

        try:
            branch = pr["head"]["ref"]
            run(["git", "fetch", "origin", branch], repo_dir)
            run(["git", "checkout", branch], repo_dir)

            r = subprocess.run(
                ["git", "rebase", f"origin/{target}"],
                cwd=repo_dir,
            )

            if r.returncode:
                resolve_conflicts(repo_dir)
                run(["git", "rebase", "--continue"], repo_dir)

            run(["git", "push", "--force-with-lease"], repo_dir)

        except Exception as e:
            logging.error(str(e))
            add_label(owner, repo, pr_num, FAIL_LABEL)
        finally:
            remove_label(owner, repo, pr_num, LOCK_LABEL)


if __name__ == "__main__":
    main()
