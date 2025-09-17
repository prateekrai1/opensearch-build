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
            
            # Collect HEAD changes (current branch)
            while i < len(lines) and not lines[i].startswith('======='):
                head_changes.append(lines[i])
                i += 1
            
            # Skip the separator
            i += 1
            incoming_changes = []
            
            # Collect incoming changes (from main/target branch)
            while i < len(lines) and not lines[i].startswith('>>>>>>> '):
                incoming_changes.append(lines[i])
                i += 1
            
            # Skip the end marker
            i += 1
            
            # For stalled PRs, we want to keep PR changes on top of main branch changes
            # So we put incoming (main) changes first, then PR changes
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

def check_for_conflicts(repo_dir):
    """Check if there are any unresolved conflicts."""
    result = subprocess.run(["git", "diff", "--name-only", "--diff-filter=U"], 
                          cwd=repo_dir, capture_output=True, text=True)
    if result.returncode == 0:
        conflicted_files = result.stdout.strip().split('\n') if result.stdout.strip() else []
        return [f for f in conflicted_files if f]
    return []

def handle_stalled_pr(owner, repo, repo_dir, pr_details):
    """Handle a single stalled PR with improved rebasing."""
    pr_number = pr_details["number"]
    pr_branch = pr_details["head"]["ref"]
    target_branch = pr_details["base"]["ref"]
    fork_owner = pr_details["head"]["repo"]["owner"]["login"]
    
    print(f"Processing Stalled PR #{pr_number}: {pr_branch} -> {target_branch}")
    print(f"Fork owner: {fork_owner}, Repo owner: {owner}")
    
    # Setup git config
    setup_git_config(repo_dir)
    
    # Clean working directory
    safe_git_operation(["git", "reset", "--hard"], repo_dir, ignore_errors=True)
    safe_git_operation(["git", "clean", "-fd"], repo_dir, ignore_errors=True)
    
    # Add the fork as a remote if it's from a fork
    fork_remote = None
    if fork_owner != owner:
        fork_remote = f"fork_{fork_owner}"
        safe_git_operation(["git", "remote", "remove", fork_remote], repo_dir, ignore_errors=True)
        if not safe_git_operation(["git", "remote", "add", fork_remote, 
                                 f"https://github.com/{fork_owner}/{repo}.git"], repo_dir):
            print(f"Failed to add fork remote for {fork_owner}")
            return False
        
        if not safe_git_operation(["git", "fetch", fork_remote], repo_dir):
            print(f"Failed to fetch from fork remote")
            return False
        
        full_pr_branch = f"{fork_remote}/{pr_branch}"
    else:
        safe_git_operation(["git", "fetch", "origin"], repo_dir)
        full_pr_branch = f"origin/{pr_branch}"
    
    # Create a local branch for the PR
    local_branch = f"local_stalled_{pr_number}"
    safe_git_operation(["git", "branch", "-D", local_branch], repo_dir, ignore_errors=True)
    
    if not safe_git_operation(["git", "checkout", "-b", local_branch, full_pr_branch], repo_dir):
        print(f"Failed to checkout PR branch {full_pr_branch}")
        return False
    
    # Fetch latest target branch
    if not safe_git_operation(["git", "fetch", "origin", target_branch], repo_dir):
        print(f"Failed to fetch target branch {target_branch}")
        return False
    
    print(f"Starting rebase of {local_branch} onto origin/{target_branch}")
    
    # Attempt rebase
    result = subprocess.run(["git", "rebase", f"origin/{target_branch}"], 
                          cwd=repo_dir, capture_output=True, text=True)
    
    if result.returncode != 0:
        print("Conflicts detected during rebase")
        
        # Handle conflicts in a loop until rebase is complete
        max_attempts = 10  # Prevent infinite loops
        attempts = 0
        
        while attempts < max_attempts:
            attempts += 1
            print(f"Conflict resolution attempt {attempts}")
            
            # Check for conflicted files
            conflicted_files = check_for_conflicts(repo_dir)
            print(f"Conflicted files: {conflicted_files}")
            
            if not conflicted_files:
                # No more conflicts, try to continue
                continue_result = subprocess.run(["git", "rebase", "--continue"], 
                                               cwd=repo_dir, capture_output=True, text=True)
                if continue_result.returncode == 0:
                    print("Rebase completed successfully")
                    break
                elif "No changes" in continue_result.stdout or "nothing to commit" in continue_result.stdout:
                    # Skip this commit
                    print("Skipping empty commit")
                    safe_git_operation(["git", "rebase", "--skip"], repo_dir)
                    continue
                else:
                    print(f"Failed to continue rebase: {continue_result.stderr}")
                    safe_git_operation(["git", "rebase", "--abort"], repo_dir)
                    return False
            
            # Resolve conflicts
            conflict_resolved = False
            for file_path in conflicted_files:
                full_file_path = os.path.join(repo_dir, file_path)
                
                if file_path == "CHANGELOG.md":
                    print(f"Resolving CHANGELOG.md conflict")
                    resolve_changelog_conflict_advanced(full_file_path)
                    safe_git_operation(["git", "add", "CHANGELOG.md"], repo_dir)
                    conflict_resolved = True
                else:
                    # For other files, try to automatically resolve by taking the PR version
                    print(f"Auto-resolving conflict in {file_path} (taking PR version)")
                    safe_git_operation(["git", "checkout", "--theirs", file_path], repo_dir)
                    safe_git_operation(["git", "add", file_path], repo_dir)
                    conflict_resolved = True
            
            if not conflict_resolved:
                print("Could not resolve conflicts automatically")
                safe_git_operation(["git", "rebase", "--abort"], repo_dir)
                return False
        
        if attempts >= max_attempts:
            print("Max conflict resolution attempts reached")
            safe_git_operation(["git", "rebase", "--abort"], repo_dir)
            return False
    else:
        print("Rebase completed without conflicts")
    
    # Push the rebased branch
    success = False
    if fork_owner != owner and fork_remote:
        # Push to the fork
        print(f"Pushing to fork: {fork_owner}/{repo}")
        fork_remote_url = f"https://{GITHUB_TOKEN}@github.com/{fork_owner}/{repo}.git"
        
        if safe_git_operation(["git", "remote", "set-url", fork_remote, fork_remote_url], repo_dir):
            if safe_git_operation(["git", "push", "--force-with-lease", fork_remote, 
                                 f"{local_branch}:{pr_branch}"], repo_dir):
                print(f"Successfully pushed to fork")
                success = True
            else:
                # Try regular force push if force-with-lease fails
                if safe_git_operation(["git", "push", "--force", fork_remote, 
                                     f"{local_branch}:{pr_branch}"], repo_dir):
                    print(f"Successfully force pushed to fork")
                    success = True
    else:
        # Push to origin (same repo)
        print(f"Pushing to origin: {owner}/{repo}")
        remote_url = f"https://{GITHUB_TOKEN}@github.com/{owner}/{repo}.git"
        
        if safe_git_operation(["git", "remote", "set-url", "origin", remote_url], repo_dir):
            if safe_git_operation(["git", "push", "--force-with-lease", "origin", 
                                 f"{local_branch}:{pr_branch}"], repo_dir):
                print(f"Successfully pushed to origin")
                success = True
            else:
                # Try regular force push if force-with-lease fails
                if safe_git_operation(["git", "push", "--force", "origin", 
                                     f"{local_branch}:{pr_branch}"], repo_dir):
                    print(f"Successfully force pushed to origin")
                    success = True
    
    # Cleanup
    safe_git_operation(["git", "checkout", target_branch], repo_dir, ignore_errors=True)
    safe_git_operation(["git", "branch", "-D", local_branch], repo_dir, ignore_errors=True)
    
    if success:
        print(f"Successfully rebased Stalled PR #{pr_number}")
        return True
    else:
        print(f"Failed to push rebased changes for Stalled PR #{pr_number}")
        return False

def main_stalled(owner, repo, repo_dir):
    """Main function to handle stalled PRs"""
    print("Stalled PRs script starting...")
    
    if not GITHUB_TOKEN:
        print("Error: GITHUB_TOKEN environment variable is not set")
        sys.exit(1)
    
    try:
        stalled_prs = fetch_stalled_prs(owner, repo)
        print(f"Found {len(stalled_prs)} stalled PRs")
        
        if not stalled_prs:
            print("No stalled PRs found")
            return
        
        success_count = 0
        for pr in stalled_prs:
            pr_number = pr["number"]
            print(f"\n--- Processing PR #{pr_number} ---")
            
            try:
                pr_details = fetch_pr_details(owner, repo, pr_number)
                
                # Skip closed or merged PRs
                if pr_details["state"] != "open":
                    print(f"Skipping PR #{pr_number} - not open (state: {pr_details['state']})")
                    continue
                
                if handle_stalled_pr(owner, repo, repo_dir, pr_details):
                    success_count += 1
                    print(f"✓ Successfully processed Stalled PR #{pr_number}")
                else:
                    print(f"✗ Failed to process Stalled PR #{pr_number}")
                    
            except Exception as e:
                print(f"✗ Error processing PR #{pr_number}: {e}")
        
        print(f"\n--- Summary ---")
        print(f"Successfully processed {success_count}/{len(stalled_prs)} stalled PRs")
        
    except Exception as e:
        print(f"Error in stalled PR processing: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python StalledPRs.py <owner> <repo> <repo_directory>")
        print("Example: python StalledPRs.py opensearch-project OpenSearch /path/to/repo")
        sys.exit(1)
    
    owner = sys.argv[1]
    repo = sys.argv[2]
    repo_dir = sys.argv[3]
    
    if not os.path.exists(repo_dir):
        print(f"Error: Repository directory '{repo_dir}' does not exist")
        sys.exit(1)
    
    main_stalled(owner, repo, repo_dir)