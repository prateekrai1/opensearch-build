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


def last_commit_is_auto(repo_dir):
    msg = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return msg.startswith(AUTO_COMMIT_PREFIX)


def resolve_conflicts(repo_dir):
    subprocess.run(["git", "checkout", "--theirs", "."], cwd=repo_dir)
    subprocess.run(["git", "add", "."], cwd=repo_dir)
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
            repo_url = pr["head"]["repo"]["clone_url"]

            run(["git", "fetch", repo_url, branch], cwd=repo_dir)
            run(["git", "checkout", branch], cwd=repo_dir)

            r = subprocess.run(
                ["git", "rebase", f"origin/{target}"],
                cwd=repo_dir,
            )

            if r.returncode:
                if last_commit_is_auto(repo_dir):
                    raise RuntimeError("Rebase loop detected")

                resolve_conflicts(repo_dir)
                run(["git", "rebase", "--continue"], cwd=repo_dir)

            run(["git", "push", "--force-with-lease"], cwd=repo_dir)

        except Exception as e:
            logging.error(str(e))
            add_label(owner, repo, pr_num, FAIL_LABEL)
        finally:
            remove_label(owner, repo, pr_num, LOCK_LABEL)


if __name__ == "__main__":
    main()
