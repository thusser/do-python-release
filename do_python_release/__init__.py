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

    def merge(self, title: str, body: str):
        self.pr.merge(commit_title=title, commit_message=body)

    def release(self, title: str, body: str, branch: str = "main"):
        print('Fetching last commit...')
        commit = self.repo.get_commits(sha=branch)[0]
        print(f'Commit is {commit.sha}.')

        # tag & release
        print('Create tag and release...')
        self.repo.create_git_tag_and_release(tag=title, tag_message=body, release_name=title, release_message=body,
                                             object=commit.sha, type='commit')


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

    def merge(self, title: str, body: str):
        while self.project.mergerequests.get(self.mr.iid).merge_status != "can_be_merged":
            time.sleep(1)
        self.mr.merge()

    def release(self, title: str, body: str, branch: str = "main"):
        print("Creating release...")
        self.project.releases.create({'ref': branch, 'name': title, 'tag_name': title, 'description': body})


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
            raise RuntimeError("Could not determine backend.")
        print(f'Build backend:   {self.backend}')

        # check, whether it is installed
        try:
            shell(f'{self.backend} -V')
        except subprocess.CalledProcessError:
            raise RuntimeError(f'No {self.backend} found.')

    def version(self):
        return shell(f'{self.backend} version').split()[1].strip()

    def default_bump_type(self) -> str:
        # if we're currently on a pre-release, default to continuing that
        # pre-release track instead of jumping straight to a full release
        try:
            v = PackagingVersion(self.version())
        except InvalidVersion:
            return "patch"

        if not v.is_prerelease:
            return "patch"

        if self.backend == "poetry":
            return "prerelease"

        # uv has no generic "prerelease" bump type; continue the same segment
        if v.pre is not None:
            return {"a": "alpha", "b": "beta", "rc": "rc"}[v.pre[0]]
        if v.dev is not None:
            return "dev"
        return "patch"

    def command(self, version: str | None) -> str:
        if version is None:
            version = self.default_bump_type()

        if self.backend == "uv":
            return f"uv version --bump {version}"
        elif self.backend == "poetry":
            return f"poetry version {version}"
        else:
            raise RuntimeError(f"Invalid backend {self.backend}.")

    def bump(self, version: str | None):
        shell(self.command(version))


PRERELEASE_BUMP_TYPES = {'premajor', 'preminor', 'prepatch', 'prerelease', 'alpha', 'beta', 'rc', 'dev'}


def bump_leaves_prerelease(current_version: str, requested: str | None) -> bool:
    # whether bumping from current_version with the given bump type/version
    # would move from a pre-release to a full release
    try:
        if not PackagingVersion(current_version).is_prerelease:
            return False
    except InvalidVersion:
        return False

    bump_type = requested or "patch"
    if bump_type in PRERELEASE_BUMP_TYPES:
        return False
    try:
        return not PackagingVersion(bump_type).is_prerelease
    except InvalidVersion:
        # a bump keyword (major/minor/patch/...) drops any pre-release suffix
        return True


def porcelain_paths(line):
    # `git status --porcelain` v1 lines look like "XY path"; rename/copy
    # entries look like "XY old -> new", in which case both paths matter.
    path = line[3:]
    if ' -> ' in path:
        return path.split(' -> ')
    return [path]


def main():
    # set up parser
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--version', type=str, help='Version to release.')
    parser.add_argument('-t', '--token', type=str, help='GitHub access token.')
    parser.add_argument('-y', '--yes', action="store_true", help='Auto-accept all questions.')
    parser.add_argument('--include-dirty', action="store_true", help='Include other uncommitted changes in the release commit (needed to force this non-interactively with -y).')
    parser.add_argument('--no-merge', action="store_true", help='No merge, just bump version and release on current branch.')
    args = parser.parse_args()

    # repo owner
    repo_owner = shell('git config --get user.name')

    # get repo name
    repo_remote = shell('git config --get remote.origin.url')
    if not repo_remote:
        print("Not a git repository.")
        sys.exit(1)
    print(repo_remote)

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

    # current version
    print(f'Current version: {version.version()}')

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
    print(f'1. Set new version using "{version.command(args.version)}"')
    print(f'2. Commit and pull change.')
    if not args.no_merge:
        print(f'3. Create PR develop -> {main_branch}')
        print(f'4. Merge PR')
        print(f'5. Create tag and release with new version')
    else:
        print(f'3. Create tag and release with new version')

    # warn if this bump would turn a pre-release into a full release
    if bump_leaves_prerelease(version.version(), args.version or version.default_bump_type()):
        print()
        print(f'Warning: current version {version.version()} is a pre-release, '
              f'and this bump will publish a full release.')
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

    # set new version
    print()
    print('Setting new version...')
    version.bump(args.version)
    print(f'New version: {version.version()}')

    # commit it
    shell(f'git add pyproject.toml {version.lock_file} {" ".join(dirty_files)}')
    shell(f'git commit -m "v{version.version()}"')
    shell(f'git push')

    # shortcuts
    title = f'v{version.version()}'
    body = f'version {version.version()}'

    if args.no_merge:
        # get last commit and release
        hoster.release(title=title, body=body, branch=cur_branch)
    else:
        # create PR
        print('Creating PR...')
        hoster.create_pull(title=title, body=body, src='develop', dest=main_branch)

        # merge PR
        print('Merging PR...')
        hoster.merge(title=title, body=body)

        # get last commit and release
        hoster.release(title=title, body=body, branch=main_branch)

    print('Done.')


def shell(cmd, check=True):
    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, check=check)
    return result.stdout.decode('utf-8').rstrip()


if __name__ == '__main__':
    code = main()
    sys.exit(code)
