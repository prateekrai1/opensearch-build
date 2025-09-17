import os
import requests
import subprocess
import sys

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
BASE_URL = "https://api.github.com"

def setup_git_config(repo_dir):
    """Setup git configuration for the repository."""
    subprocess.run(["git", "config", "user.name", "OpenSearch Bot"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "opensearch-bot@amazon.com"], cwd=repo_dir, check=True)

def fetch_backport_prs(owner, repo):
    """Fetch backport PR's with the `backport` label"""
    url = f"{BASE_URL}/search/issues"
    query = f"repo:{owner}/{repo} label:backport is:pr is:open"
    response = requests.get(url, headers=HEADERS, params={"q": query})
    response.raise_for_status()
    return response.json()["items"]

def fetch_pr_details(owner, repo, pr_number):
    """Fetch PR details to get source and target branches"""
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def get_pr_commits(owner, repo, pr_number):
    """Get all commits in a PR"""
    url = f"{BASE_URL}/repos/{owner}/{repo}/pulls/{pr_number}/commits"
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    return response.json()

def resolve_changelog_conflict_advanced(file_path):
    """Advanced changelog conflict resolution that preserves chronological order."""
    if not os.path.exists(file_path):
        print(f"Warning: {file_path} does not exist")
        return
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Check if there are conflict markers
    if not ('<<<<<<< ' in content and '=======' in content and '>>>>>>> ' in content):
        print("No conflict markers found in CHANGELOG.md")
        return
    
    print("Resolving CHANGELOG.md conflicts...")
    
    # Split content by conflict blocks
    lines = content.split('\n')
    resolved_lines = []
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        if line.startswith('<<<<<<< '):
            # Start of conflict block
            i += 1
            head_changes = []
            
            # Collect HEAD changes
            while i < len(lines) and not lines[i].startswith('======='):
                head_changes.append(lines[i])
                i += 1
            
            # Skip the separator
            i += 1
            incoming_changes = []
            
            # Collect incoming changes
            while i < len(lines) and not lines[i].startswith('>>>>>>> '):
                incoming_changes.append(lines[i])
                i += 1
            
            # Skip the end marker
            i += 1
            
            # Merge changes - put newer changes first (incoming changes are usually newer)
            resolved_lines.extend(incoming_changes)
            resolved_lines.extend(head_changes)
            
        else:
            resolved_lines.append(line)
            i += 1
    
    # Write resolved content
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(resolved_lines))
    
    print("CHANGELOG.md conflict resolved successfully")

def safe_git_operation(command, repo_dir, ignore_errors=False):
    """Execute git command with error handling."""
    try:
        result = subprocess.run(command, cwd=repo_dir, check=not ignore_errors, 
                              capture_output=True, text=True)
        if result.returncode != 0 and not ignore_errors:
            print(f"Git command failed: {' '.join(command)}")
            print(f"Error: {result.stderr}")
            return False
        return True
    except subprocess.CalledProcessError as e:
        print(f"Git operation failed: {e}")
        return False

def handle_backport_pr(owner, repo, repo_dir, pr_details):
    """Handle a single backport PR with improved conflict resolution."""
    pr_number = pr_details["number"]
    pr_branch = pr_details["head"]["ref"]
    target_branch = pr_details["base"]["ref"]
    fork_owner = pr_details["head"]["repo"]["owner"]["login"]
    
    print(f"Processing Backport PR #{pr_number}: {pr_branch} -> {target_branch}")
    
    # Setup git config
    setup_git_config(repo_dir)
    
    # Clean working directory
    safe_git_operation(["git", "reset", "--hard"], repo_dir)
    safe_git_operation(["git", "clean", "-fd"], repo_dir)
    
    # Add the fork as a remote if it's from a fork
    if fork_owner != owner:
        fork_remote = f"fork_{fork_owner}"
        safe_git_operation(["git", "remote", "remove", fork_remote], repo_dir, ignore_errors=True)
        safe_git_operation(["git", "remote", "add", fork_remote, 
                          f"https://github.com/{fork_owner}/{repo}.git"], repo_dir)
        safe_git_operation(["git", "fetch", fork_remote], repo_dir)
        full_pr_branch = f"{fork_remote}/{pr_branch}"
    else:
        full_pr_branch = f"origin/{pr_branch}"
    
    # Fetch latest changes
    safe_git_operation(["git", "fetch", "origin"], repo_dir)
    
    # Checkout the target branch and update it
    if not safe_git_operation(["git", "checkout", target_branch], repo_dir):
        return False
    
    safe_git_operation(["git", "pull", "origin", target_branch], repo_dir)
    
    # Get commits from the PR
    commits = get_pr_commits(owner, repo, pr_number)
    commit_shas = [commit["sha"] for commit in commits]
    
    print(f"Found {len(commit_shas)} commits to cherry-pick")
    
    # Cherry-pick each commit individually
    success = True
    for i, commit_sha in enumerate(commit_shas):
        print(f"Cherry-picking commit {i+1}/{len(commit_shas)}: {commit_sha[:8]}")
        
        result = subprocess.run(["git", "cherry-pick", commit_sha], 
                              cwd=repo_dir, capture_output=True, text=True)
        
        if result.returncode != 0:
            print("Conflict detected during cherry-pick")
            
            # Check for conflicts in CHANGELOG.md
            changelog_path = os.path.join(repo_dir, "CHANGELOG.md")
            if os.path.exists(changelog_path):
                # Check if CHANGELOG.md has conflicts
                status_result = subprocess.run(["git", "status", "--porcelain"], 
                                             cwd=repo_dir, capture_output=True, text=True)
                if "CHANGELOG.md" in status_result.stdout:
                    resolve_changelog_conflict_advanced(changelog_path)
                    safe_git_operation(["git", "add", "CHANGELOG.md"], repo_dir)
            
            # Add any other resolved files
            safe_git_operation(["git", "add", "."], repo_dir)
            
            # Try to continue the cherry-pick
            result = subprocess.run(["git", "cherry-pick", "--continue"], 
                                  cwd=repo_dir, capture_output=True, text=True)
            
            if result.returncode != 0:
                print(f"Failed to resolve conflicts for commit {commit_sha[:8]}")
                safe_git_operation(["git", "cherry-pick", "--abort"], repo_dir)
                success = False
                break
    
    if success:
        # Push the changes
        remote_url = f"https://{GITHUB_TOKEN}@github.com/{owner}/{repo}.git"
        safe_git_operation(["git", "remote", "set-url", "origin", remote_url], repo_dir)
        
        if safe_git_operation(["git", "push", "origin", target_branch], repo_dir):
            print(f"Successfully processed Backport PR #{pr_number}")
            return True
        else:
            print(f"Failed to push changes for Backport PR #{pr_number}")
    
    return False

def main_backport(owner, repo, repo_dir):
    """Main function to handle backport PRs"""
    print("Backport PRs script starting...")
    
    if not GITHUB_TOKEN:
        print("Error: GITHUB_TOKEN environment variable is not set")
        sys.exit(1)
    
    try:
        backport_prs = fetch_backport_prs(owner, repo)
        print(f"Found {len(backport_prs)} backport PRs")
        
        if not backport_prs:
            print("No backport PRs found")
            return
        
        success_count = 0
        for pr in backport_prs:
            pr_number = pr["number"]
            print(f"\n--- Processing PR #{pr_number} ---")
            
            try:
                pr_details = fetch_pr_details(owner, repo, pr_number)
                
                if handle_backport_pr(owner, repo, repo_dir, pr_details):
                    success_count += 1
                    print(f"✓ Successfully processed Backport PR #{pr_number}")
                else:
                    print(f"✗ Failed to process Backport PR #{pr_number}")
                    
            except Exception as e:
                print(f"✗ Error processing PR #{pr_number}: {e}")
        
        print(f"\n--- Summary ---")
        print(f"Successfully processed {success_count}/{len(backport_prs)} backport PRs")
        
    except Exception as e:
        print(f"Error in backport processing: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python BackportPRs.py <owner> <repo> <repo_directory>")
        print("Example: python BackportPRs.py opensearch-project OpenSearch /path/to/repo")
        sys.exit(1)
    
    owner = sys.argv[1]
    repo = sys.argv[2]
    repo_dir = sys.argv[3]
    
    if not os.path.exists(repo_dir):
        print(f"Error: Repository directory '{repo_dir}' does not exist")
        sys.exit(1)
    
    main_backport(owner, repo, repo_dir)