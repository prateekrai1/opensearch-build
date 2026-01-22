import os
import sys
import subprocess
import logging
import requests
import shutil

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
    return subprocess.run(cmd, cwd=cwd, check=check, text=True, capture_output=True)


def git_config(repo_dir):
    configs = [
        ("user.name", "prateekrai1"),
        ("user.email", "prateekr651@gmail.com"),
        ("rerere.enabled", "true"),
        ("rebase.autoStash", "true"),
    ]
    for key, value in configs:
        subprocess.run(["git", "config", key, value], cwd=repo_dir, check=False)


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


def safe_cleanup_git_state(repo_dir):
    git_dir = os.path.join(repo_dir, ".git")
    for p in ["rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "MERGE_HEAD"]:
        path = os.path.join(git_dir, p)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)

    subprocess.run(["git", "cherry-pick", "--abort"], cwd=repo_dir, check=False)
    subprocess.run(["git", "reset", "--hard"], cwd=repo_dir, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_dir, check=False)


def last_commit_is_auto(repo_dir):
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return msg.startswith(AUTO_COMMIT_PREFIX)


def resolve_conflicts(repo_dir):
    files = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    ).stdout.splitlines()

    for f in files:
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
    safe_cleanup_git_state(repo_dir)

    prs = gh("GET", f"/repos/{owner}/{repo}/pulls?state=open&labels=backport")

    for pr in prs:
        pr_num = pr["number"]

        if has_label(pr, LOCK_LABEL) or has_label(pr, FAIL_LABEL):
            logging.info(f"Skipping PR #{pr_num}")
            continue

        add_label(owner, repo, pr_num, LOCK_LABEL)

        try:
            commits = gh("GET", f"/repos/{owner}/{repo}/pulls/{pr_num}/commits")

            run(["git", "checkout", target], cwd=repo_dir)
            run(["git", "pull"], cwd=repo_dir)

            branch = f"backport-pr-{pr_num}"
            run(["git", "checkout", "-b", branch], cwd=repo_dir)

            for c in commits:
                r = subprocess.run(["git", "cherry-pick", c["sha"]], cwd=repo_dir)
                if r.returncode:
                    if last_commit_is_auto(repo_dir):
                        raise RuntimeError("Conflict loop detected")

                    resolve_conflicts(repo_dir)

            run(
                ["git", "push", "--force-with-lease", "origin", branch],
                cwd=repo_dir,
            )

        except Exception as e:
            logging.error(str(e))
            add_label(owner, repo, pr_num, FAIL_LABEL)
        finally:
            remove_label(owner, repo, pr_num, LOCK_LABEL)


if __name__ == "__main__":
    main()
