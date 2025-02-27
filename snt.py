import argparse
import json
import os
import subprocess
import sys
import time
from git import Repo, GitCommandError

CONFIG_FILE = "config.json"
REPOS_DIR = "repos"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config():
    """Loads configuration from file or creates a new dictionary."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        return {"repos": {}}


def save_config(config):
    """Saves configuration to file."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)


def get_repo_config(config, repo_id):
    """Returns the repository configuration by its identifier."""
    return config["repos"].get(str(repo_id))


def update_repo_config(config, repo_id, url=None, token=None):
    """Updates or creates an entry for the repository."""
    repo_key = str(repo_id)
    if repo_key not in config["repos"]:
        config["repos"][repo_key] = {}
    if url:
        config["repos"][repo_key]["url"] = url
    if token:
        config["repos"][repo_key]["token"] = token
    save_config(config)


def get_authenticated_url(url, token):
    """
    Returns a cloning URL with token substitution if specified.
    Example: https://github.com/user/repo.git -> https://<token>@github.com/user/repo.git
    """
    if token:
        if url.startswith("https://"):
            return url.replace("https://", f"https://{token}@", 1)
        elif url.startswith("http://"):
            return url.replace("http://", f"http://{token}@", 1)
    return url


def clone_or_update_repo(repo_config, local_repo_dir, debug=False):
    url = repo_config.get("url")
    token = repo_config.get("token")
    auth_url = get_authenticated_url(url, token)
    
    if not os.path.exists(os.path.join(local_repo_dir, ".git")):
        if debug:
            print(f"[DEBUG] Cloning repository from {url} to {local_repo_dir}")
        try:
            Repo.clone_from(auth_url, local_repo_dir)
        except GitCommandError as e:
            print(f"Error cloning repository: {e}")
            return False
    else:
        if debug:
            print(f"[DEBUG] Repository already cloned in {local_repo_dir}. Checking for updates...")
        repo = Repo(local_repo_dir)
        origin = repo.remotes.origin
        try:
            origin.fetch()
        except GitCommandError as e:
            print(f"Error fetching updates: {e}")
            return False

        branch_name = repo.active_branch.name
        local_commit = repo.head.commit.hexsha
        try:
            remote_commit = origin.refs[branch_name].commit.hexsha
        except (IndexError, AttributeError):
            if debug:
                print(f"[DEBUG] Remote branch {branch_name} not found.")
            return False

        if debug:
            print(f"[DEBUG] Local commit: {local_commit}")
            print(f"[DEBUG] Remote commit: {remote_commit}")

        if local_commit != remote_commit:
            if debug:
                print("[DEBUG] Updates found. Executing pull.")
            try:
                repo.git.reset('--hard')
                repo.git.clean('-fd')
                origin.pull()
                return True
            except GitCommandError as e:
                print(f"Error updating repository: {e}")
                return False
        else:
            if debug:
                print("[DEBUG] No updates found.")
            return False

    return True


def run_start_file(local_path, start_file, debug=False):
    """
    Runs the specified start file in the repository directory.
    Returns the process started using subprocess.Popen.
    """
    start_file_path = os.path.join(local_path, start_file)
    if not os.path.exists(start_file_path):
        print(f"File {start_file} not found in repository directory.")
        sys.exit(1)
    if debug:
        print(f"[DEBUG] Running file: {start_file_path}")
    process = subprocess.Popen([sys.executable, start_file_path])
    return process


def main():
    parser = argparse.ArgumentParser(
        description="Command-line utility for downloading and managing GitHub repositories."
    )
    parser.add_argument("--token", type=str, help="GitHub token for repository access.")
    subparsers = parser.add_subparsers(dest="command", help="Utility commands.")

    start_parser = subparsers.add_parser("start", help="Start repository")
    start_parser.add_argument("--repo", required=True, type=int, help="Repository number from configuration.")
    start_parser.add_argument("--repo-url", type=str, help="Repository URL (for creating a new entry).")
    start_parser.add_argument("--start-file", required=True, type=str, help="The file to be executed.")
    start_parser.add_argument("--check-time", required=True, type=int, help="Interval for checking updates (in seconds).")
    start_parser.add_argument("--debug", action="store_true", help="Enable debug mode.")

    args = parser.parse_args()

    if args.command == "start":
        config = load_config()
        repo_config = get_repo_config(config, args.repo)
        if repo_config is None:
            if args.repo_url:
                if args.debug:
                    print(f"[DEBUG] Repository with number {args.repo} not found. Creating a new entry.")
                update_repo_config(config, args.repo, url=args.repo_url, token=args.token)
                repo_config = get_repo_config(config, args.repo)
            else:
                print(f"Configuration for repository number {args.repo} not found. Specify --repo-url to create an entry.")
                sys.exit(1)
        else:
            if args.token:
                update_repo_config(config, args.repo, token=args.token)
                repo_config = get_repo_config(config, args.repo)

        local_repo_dir = os.path.join(BASE_DIR, "repo")

        if args.debug:
            print("[DEBUG] Checking and downloading repository...")
        clone_or_update_repo(repo_config, local_repo_dir, debug=args.debug)
        if args.debug:
            print("[DEBUG] Running start file.")
        process = run_start_file(local_repo_dir, args.start_file, debug=args.debug)

        try:
            while True:
                time.sleep(args.check_time)
                if args.debug:
                    print("[DEBUG] Checking for updates...")
                updated = clone_or_update_repo(repo_config, local_repo_dir, debug=args.debug)
                if updated:
                    if args.debug:
                        print("[DEBUG] Updates detected. Restarting process.")
                    process.terminate()
                    process.wait()
                    process = run_start_file(local_repo_dir, args.start_file, debug=args.debug)
        except KeyboardInterrupt:
            if args.debug:
                print("\n[DEBUG] Terminating utility.")
            process.terminate()
            process.wait()
            sys.exit(0)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
