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

    def release(self, title: str, body: str):
        print('Fetching last commit...')
        commit = self.repo.get_commits()[0]
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

    def release(self, title: str, body: str):
        print("Creating release...")
        self.project.releases.create({'ref': 'main', 'name': title, 'tag_name': title, 'description': body})


class Version:
    def __init__(self):
        # what package backend do we use? load pyproject.toml...
        backend = toml.load("pyproject.toml")['build-system']['build-backend']
        if backend == "hatchling.build":
            self.backend = "uv"
        elif backend == "poetry.core.masonry.api":
            self.backend = "poetry"
        else:
            raise RuntimeError("Could not determine backend from pyproject.toml.")
        print(f'Build backend:   {self.backend}')

        # check, whether it is installed
        try:
            shell(f'{self.backend} -V')
        except subprocess.CalledProcessError:
            raise RuntimeError(f'No {self.backend} found.')

    def version(self):
        return shell('poetry version').split()[1].strip()

    def command(self, version: str | None) -> str:
        if version is None:
            version = "patch"

        if self.backend == "uv":
            return f"uv version --bump {version}"
        elif self.backend == "poetry":
            return f"poetry version {version}"
        else:
            raise RuntimeError(f"Invalid backend {self.backend}.")

    def bump(self, version: str | None):
        shell(self.command(version))


def main():
    # set up parser
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--version', type=str, help='Version to release.')
    parser.add_argument('-t', '--token', type=str, help='GitHub access token.')
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
    m = re.search(r'github\.com[:/](.*)\.git$', repo_remote)
    if m is not None:
        repo_name = m.group(1)
        hoster = GitHub()
    m = re.search(r'git@gitlab\.gwdg\.de[:/](.*)\.git$', repo_remote)
    if m is not None:
        repo_name = m.group(1)
        hoster = GitLab()

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
    cur_branch = shell('git rev-parse --abbrev-ref HEAD')
    if cur_branch != 'develop':
        print('Current branch is not develop.')
        return 1

    # print plan
    print()
    print('Will perform the following tasks:')
    print(f'1. Set new version using "{version.command(args.version)}"')
    print(f'2. Commit and pull change.')
    print(f'3. Create PR develop -> {main_branch}')
    print(f'4. Merge PR')
    print(f'5. Create tag and release with new version')

    # continue
    if input('Continue [y/N]') not in 'yY':
        return 0

    # set new version
    print()
    print('Setting new version...')
    version.bump(args.version)
    print(f'New version: {version.version()}')

    # commit it
    shell(f'git commit -m "v{version.version()}" pyproject.toml')
    shell(f'git push')

    # shortcuts
    title = f'v{version.version()}'
    body = f'version {version.version()}'

    # create PR
    print('Creating PR...')
    hoster.create_pull(title=title, body=body, src='develop', dest=main_branch)

    # merge PR
    print('Merging PR...')
    hoster.merge(title=title, body=body)

    # get last commit and release
    hoster.release(title=title, body=body)
    print('Done.')


def shell(cmd, check=True):
    result = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, check=check)
    return result.stdout.decode('utf-8').strip()


if __name__ == '__main__':
    code = main()
    sys.exit(code)
