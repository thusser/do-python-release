#!/usr/bin/python3

import argparse
import re
import subprocess
import sys
import time
import github
import gitlab
import os
import toml
from packaging.version import InvalidVersion, Version as PackagingVersion


class GitHub:
    name = "GitHub"

    def __init__(self):
        self.gh = None
        self.repo = None
        self.pr = None

    def connect(self, token: str):
        self.gh = github.Github(token)

    def use_repo(self, name: str):
        self.repo = self.gh.get_repo(name)

    def get_branches(self):
        return self.repo.get_branches()

    def create_pull(self, src: str, dest: str, title: str, body: str):
        self.pr = self.repo.create_pull(title=title, body=body, head=src, base=dest)

    def merge(self, title: str, body: str) -> str:
        """Merge the PR and return the SHA of the merge commit."""
        status = self.pr.merge(commit_title=title, commit_message=body)
        if not status.merged:
            raise ReleaseError(f'Merging the PR failed: {status.message}')
        return status.sha

    def release(self, title: str, body: str, sha: str, prerelease: bool = False):
        # tag the given commit, never the branch head: right after a push or merge,
        # the API can still return the previous head
        print(f'Create tag and release on {sha}...')
        self.repo.create_git_tag_and_release(tag=title, tag_message=body, release_name=title, release_message=body,
                                             object=sha, type='commit', prerelease=prerelease,
                                             make_latest='false' if prerelease else 'true')


class GitLab:
    name = "GitLab"

    def __init__(self):
        self.gl = None
        self.project = None
        self.mr = None

    def connect(self, token: str):
        self.gl = gitlab.Gitlab(url='https://gitlab.gwdg.de', private_token=token)
        self.gl.auth()

    def use_repo(self, name: str):
        self.project = self.gl.projects.get(name)

    def get_branches(self):
        return self.project.branches.list()

    def create_pull(self, src: str, dest: str, title: str, body: str):
        self.mr = self.project.mergerequests.create({'source_branch': src, 'target_branch': dest, 'title': title})

    def merge(self, title: str, body: str) -> str:
        """Merge the MR and return the SHA of the commit now at the head of the target branch."""
        while self.project.mergerequests.get(self.mr.iid).merge_status != "can_be_merged":
            time.sleep(1)
        result = self.mr.merge()

        # merge_commit_sha may only be filled in a moment later; with fast-forward
        # merges there is no merge commit, then the MR's head sha is the new target head
        for _ in range(30):
            sha = result.get('merge_commit_sha') or result.get('squash_commit_sha')
            if sha:
                return sha
            if result.get('state') == 'merged' and result.get('sha'):
                return result['sha']
            time.sleep(1)
            result = self.project.mergerequests.get(self.mr.iid).attributes
        raise ReleaseError('Could not determine the merge commit of the MR.')

    def release(self, title: str, body: str, sha: str, prerelease: bool = False):
        # tag the given commit, never the branch head (see GitHub.release);
        # GitLab releases have no pre-release flag
        print(f'Creating release on {sha}...')
        self.project.releases.create({'ref': sha, 'name': title, 'tag_name': title, 'description': body})


class ReleaseError(Exception):
    """An expected failure, printed as a plain error message without a traceback."""


# bump keywords each backend understands for `uv version --bump X` / `poetry version X`
UV_BUMP_TYPES = {'major', 'minor', 'patch', 'stable', 'alpha', 'beta', 'rc', 'post', 'dev'}
UV_PRERELEASE_BUMP_TYPES = {'alpha', 'beta', 'rc', 'dev'}
POETRY_BUMP_TYPES = {'major', 'minor', 'patch', 'premajor', 'preminor', 'prepatch', 'prerelease'}


def parse_version(version: str) -> PackagingVersion | None:
    try:
        return PackagingVersion(version)
    except InvalidVersion:
        return None


def is_prerelease(version: str) -> bool:
    v = parse_version(version)
    return v is not None and v.is_prerelease


def leaves_prerelease(current_version: str, new_version: str) -> bool:
    # whether going from current_version to new_version turns a pre-release into a full release
    return is_prerelease(current_version) and parse_version(new_version) is not None \
        and not is_prerelease(new_version)


def uv_command(current_version: str, spec: str) -> str:
    """Build the `uv version` command for a bump spec.

    spec is an explicit version, a bump keyword, or a comma-separated list of bump keywords
    (e.g. "minor,dev" -> `uv version --bump minor --bump dev`).
    """
    bumps = [b.strip() for b in spec.split(',')]
    if not all(b in UV_BUMP_TYPES for b in bumps):
        if len(bumps) > 1:
            raise ReleaseError(f'Invalid bump list "{spec}", allowed are: {", ".join(sorted(UV_BUMP_TYPES))}.')
        # an explicit version string must be passed positionally
        return f'uv version {spec}'

    if len(bumps) == 1:
        current_is_pre = is_prerelease(current_version)
        if bumps[0] in UV_PRERELEASE_BUMP_TYPES and not current_is_pre:
            # uv refuses e.g. 2.28.3 -> 2.28.3.dev1 ("didn't increase the version"),
            # so also bump the patch level: 2.28.3 -> 2.28.4.dev1
            bumps = ['patch', bumps[0]]
        elif bumps[0] == 'patch' and current_is_pre:
            # uv turns 2.29.0.dev1 into 2.29.1 on a patch bump, skipping 2.29.0;
            # "stable" releases 2.29.0, which is also what poetry does for "patch"
            bumps = ['stable']

    return 'uv version ' + ' '.join(f'--bump {b}' for b in bumps)


def poetry_command(spec: str) -> str:
    if ',' in spec:
        raise ReleaseError(f'Poetry takes a single bump type or version, not "{spec}".')
    if spec in UV_BUMP_TYPES - POETRY_BUMP_TYPES:
        raise ReleaseError(f'Poetry has no "{spec}" bump, use one of '
                           f'{", ".join(sorted(POETRY_BUMP_TYPES))} or an explicit version.')
    return f'poetry version {spec}'


class Version:
    def __init__(self):
        # what package backend do we use? load pyproject.toml...
        if os.path.exists('uv.lock'):
            self.backend = "uv"
            self.lock_file = "uv.lock"
        elif os.path.exists('poetry.lock'):
            self.backend = "poetry"
            self.lock_file = "poetry.lock"
        else:
            raise ReleaseError("Could not determine backend (no uv.lock or poetry.lock found).")
        print(f'Build backend:   {self.backend}')

        # check, whether it is installed
        try:
            shell(f'{self.backend} -V')
        except subprocess.CalledProcessError:
            raise ReleaseError(f'No {self.backend} found.')

    def version(self):
        return shell(f'{self.backend} version').split()[1].strip()

    def default_bump_type(self) -> str:
        # if we're currently on a pre-release, default to continuing that
        # pre-release track instead of jumping straight to a full release
        v = parse_version(self.version())
        if v is None or not v.is_prerelease:
            return "patch"

        if self.backend == "poetry":
            return "prerelease"

        # uv has no generic "prerelease" bump type; continue the same segment
        if v.dev is not None:
            return "dev"
        if v.pre is not None:
            return {"a": "alpha", "b": "beta", "rc": "rc"}[v.pre[0]]
        return "patch"

    def command(self, version: str | None) -> str:
        if version is None:
            version = self.default_bump_type()

        if self.backend == "uv":
            return uv_command(self.version(), version)
        elif self.backend == "poetry":
            return poetry_command(version)
        else:
            raise ReleaseError(f"Invalid backend {self.backend}.")

    def preview(self, version: str | None) -> str:
        """Run the bump as a dry run and return the resulting version, without changing anything.

        Both uv ("name 1.0.0 => 1.0.1") and poetry ("Bumping version from 1.0.0 to 1.0.1")
        print the new version as the last word.
        """
        cmd = f'{self.command(version)} --dry-run'
        result = subprocess.run(cmd, shell=True, capture_output=True, check=False)
        out = result.stdout.decode('utf-8').strip()
        if result.returncode != 0 or not out:
            err = result.stderr.decode('utf-8').strip() or out
            raise ReleaseError(f'Version bump "{cmd}" would fail:\n{err}')
        return out.split()[-1]

    def bump(self, version: str | None):
        shell(self.command(version))


def porcelain_paths(line):
    # `git status --porcelain` v1 lines look like "XY path"; rename/copy
    # entries look like "XY old -> new", in which case both paths matter.
    path = line[3:]
    if ' -> ' in path:
        return path.split(' -> ')
    return [path]


def sync_with_remote() -> bool:
    """Synchronize the current branch with its remote upstream.

    A plain ``git push`` is rejected as non-fast-forward whenever the local
    branch is behind its remote counterpart, which aborts the release and
    leaves the version-bump commit behind unpushed. Pull with rebase so any
    local commits are replayed on top of the remote ones and the following
    push can fast-forward.

    Only acts when the current branch actually has an upstream and no tracked
    files are modified or staged; otherwise nothing is touched and True is
    returned, so the push behaves exactly as before. Returns False only when
    the sync could not be completed automatically (e.g. a rebase conflict),
    in which case the branch is left in its original state.
    """
    # current branch without a remote upstream? nothing to sync against
    if not shell('git rev-parse --abbrev-ref @{u} 2>/dev/null', check=False):
        return True
    # a rebase refuses to run with modified or staged files; never stash or
    # touch the user's uncommitted changes, so skip the sync in that case
    if any(line and not line.startswith('??')
           for line in shell('git status --porcelain').splitlines()):
        return True
    print('Syncing with remote...')
    try:
        shell('git pull --rebase')
    except subprocess.CalledProcessError:
        shell('git rebase --abort', check=False)
        print('Could not sync with the remote automatically (rebase failed).')
        print('Resolve manually, e.g. with "git pull --rebase", then re-run this tool.')
        return False
    return True


def push_with_retry() -> None:
    """Push the current branch, re-syncing once if the push is rejected.

    Even after an initial sync, the remote can advance between that sync and
    the push, in which case the push is rejected as non-fast-forward. Sync
    again and retry once; if the retry fails, the original error propagates.
    """
    try:
        shell('git push')
    except subprocess.CalledProcessError:
        print('Push rejected (branch may be behind), syncing with the remote and retrying...')
        if sync_with_remote():
            shell('git push')
        else:
            raise


def pushed_commit(branch: str) -> str:
    """Return the local HEAD, after checking that origin's branch points to it as well."""
    sha = shell('git rev-parse HEAD')
    remote = shell(f'git ls-remote origin refs/heads/{branch}').split()
    if not remote or remote[0] != sha:
        raise ReleaseError(f"origin/{branch} is at {remote[0] if remote else 'nothing'}, "
                           f"not at the pushed commit {sha}.")
    return sha


# set once the release commit exists, so a later failure can say the release stopped partway
PROGRESS = {'committed': False}


def main():
    try:
        return run()
    except ReleaseError as e:
        print(f'Error: {e}', file=sys.stderr)
    except subprocess.CalledProcessError as e:
        # the command's own stderr was already shown live, so just say what failed
        print(f'Command failed (exit {e.returncode}): {e.cmd}', file=sys.stderr)
    if PROGRESS['committed']:
        print('The release stopped partway: the version commit was already created. '
              'Check the git log, the remote and the releases before re-running.', file=sys.stderr)
    return 1


def run():
    # set up parser
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--version', type=str, help='Version to release.')
    parser.add_argument('-t', '--token', type=str, help='GitHub access token.')
    parser.add_argument('-y', '--yes', action="store_true", help='Auto-accept all questions.')
    parser.add_argument('--include-dirty', action="store_true", help='Include other uncommitted changes in the release commit (needed to force this non-interactively with -y).')
    parser.add_argument('--no-merge', action="store_true", help='No merge, just bump version and release on current branch.')
    args = parser.parse_args()

    # inside a git repo with an origin remote?
    repo_root = shell('git rev-parse --show-toplevel 2>/dev/null', check=False)
    if not repo_root:
        raise ReleaseError(f'Not a git repository: {os.getcwd()}')
    repo_remote = shell('git config --get remote.origin.url', check=False)
    if not repo_remote:
        raise ReleaseError(f"No 'origin' remote in {repo_root}")
    print(repo_remote)

    # repo owner
    repo_owner = shell('git config --get user.name', check=False)

    # github or gitlab?
    hoster = None
    repo_name = None
    m = re.search(r'github\.com[:/](.*?)(\.git)?$', repo_remote)
    if m is not None:
        repo_name = m.group(1)
        hoster = GitHub()
    m = re.search(r'git@gitlab\.gwdg\.de[:/](.*?)(\.git)?$', repo_remote)
    if m is not None:
        repo_name = m.group(1)
        hoster = GitLab()
    if hoster is None:
        print('Unsupported remote (not GitHub or GitLab).')
        return 1
    if repo_name.startswith("/"):
        repo_name = repo_name[1:]

    # access token
    token = args.token
    if token is None and hoster.name == "GitHub":
        token = os.getenv('GITHUB_ACCESS_TOKEN')
    if token is None and hoster.name == "GitLab":
        token = os.getenv('GITLAB_ACCESS_TOKEN')

    # check access token
    if token is None:
        print(f'No {hoster.name} access token found.')
        return 1

    # print it
    print(f'Repository:      {repo_name}')
    print(f'User:            {repo_owner}')

    # pyproject.toml?
    if not os.path.exists('pyproject.toml'):
        print('No pyproject.toml found.')
        return 1

    # Poetry?
    version = Version()

    # current version, and check the bump with a dry run before anything is touched
    current_version = version.version()
    print(f'Current version: {current_version}')
    new_version = version.preview(args.version)
    print(f'New version:     {new_version}')

    # never merge a pre-release into main/master
    if is_prerelease(new_version) and not args.no_merge:
        print(f'{new_version} is a pre-release, releasing from the current branch without merging '
              f'(as with --no-merge).')
        args.no_merge = True

    # connect to hoster
    print()
    print(f'Connecting to {hoster.name}...')
    hoster.connect(token)

    # get repo
    print('Fetching repository...')
    hoster.use_repo(repo_name)
    cur_branch = shell('git rev-parse --abbrev-ref HEAD')
    if not args.no_merge:
        print('Fetching repository...')
        hoster.use_repo(repo_name)
        branches = hoster.get_branches()
        branch_names = [b.name for b in branches]
        if 'develop' not in branch_names:
            print('No develop branch found.')
            return 1
        main_branch = 'main'
        if main_branch not in branch_names:
            main_branch = 'master'
            if main_branch not in branch_names:
                print('No main/master branch found.')
                return 1

        # currently in develop?
        if cur_branch != 'develop':
            print('Current branch is not develop.')
            return 1

    # print plan
    print()
    print('Will perform the following tasks:')
    print(f'1. Set new version {current_version} -> {new_version} using "{version.command(args.version)}"')
    print(f'2. Sync with remote, commit and push change.')
    if not args.no_merge:
        print(f'3. Create PR develop -> {main_branch}')
        print(f'4. Merge PR')
        print(f'5. Create tag and release with new version')
        print(f'6. Merge {main_branch} back into develop (local merge + push)')
    else:
        print(f'3. Create tag and release with new version')

    # warn if this bump would turn a pre-release into a full release
    if leaves_prerelease(current_version, new_version):
        print()
        print(f'Warning: current version {current_version} is a pre-release, '
              f'and this bump will publish the full release {new_version}.')
        if not args.yes and input('Continue [y/N]') not in 'yY':
            return 0

    # continue
    if not args.yes:
        if input('Continue [y/N]') not in 'yY':
            return 0

    # detect any other uncommitted changes, so we don't sweep them into the release commit
    dirty_files = [
        path
        for line in shell('git status --porcelain').splitlines()
        if line and not line.startswith('??')
        for path in porcelain_paths(line)
        if path not in ('pyproject.toml', version.lock_file)
    ]
    if dirty_files:
        print()
        print('Other uncommitted changes found:')
        for f in dirty_files:
            print(f'  {f}')
        if not args.include_dirty and (args.yes or input('Include them in the release commit? [y/N]') not in 'yY'):
            dirty_files = []

    # make sure we are not committing on top of a stale branch: the push
    # below would otherwise be rejected as non-fast-forward
    if not sync_with_remote():
        return 1

    # set new version
    print()
    print('Setting new version...')
    version.bump(args.version)
    print(f'New version: {version.version()}')

    # commit it
    shell(f'git add pyproject.toml {version.lock_file} {" ".join(dirty_files)}')
    shell(f'git commit -m "v{version.version()}"')
    PROGRESS['committed'] = True
    push_with_retry()

    # shortcuts
    title = f'v{version.version()}'
    body = f'version {version.version()}'

    prerelease = is_prerelease(version.version())

    if args.no_merge:
        # release exactly the commit we just pushed
        sha = pushed_commit(cur_branch)
        hoster.release(title=title, body=body, sha=sha, prerelease=prerelease)
    else:
        # create PR
        print('Creating PR...')
        hoster.create_pull(title=title, body=body, src='develop', dest=main_branch)

        # merge PR
        print('Merging PR...')
        sha = hoster.merge(title=title, body=body)

        # release exactly the merge commit
        hoster.release(title=title, body=body, sha=sha, prerelease=prerelease)

        # merge the release commit back into develop directly, so it doesn't drift
        # from main/master after release. Not a PR: develop's required status
        # checks (CI) can't pass on a PR merged the instant it's opened, so the
        # API-based create_pull()+merge() used above for develop->main silently
        # never completes here. This content already passed CI once as part of
        # the develop->main release PR, so a local merge+push is both simpler
        # and actually works.
        print(f'Merging {main_branch} back into develop...')
        shell(f'git fetch origin {main_branch}')
        try:
            shell(f'git merge origin/{main_branch} --no-edit')
        except subprocess.CalledProcessError:
            shell('git merge --abort', check=False)
            print(f'Could not merge {main_branch} back into develop automatically (conflict) -- resolve manually.')
        else:
            push_with_retry()

    print('Done.')


def shell(cmd, check=True):
    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, check=check)
    return result.stdout.decode('utf-8').rstrip()


if __name__ == '__main__':
    code = main()
    sys.exit(code)
