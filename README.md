# do-python-release

A small CLI tool that automates releasing a Python package on GitHub or GitLab: it bumps the
project version, commits and pushes the change, merges `develop` into `main`/`master` (optional),
and creates a tag/release with the new version.

## What it does

1. Bumps the version in `pyproject.toml` using `uv version --bump` or `poetry version`
   (whichever backend it detects via `uv.lock` / `poetry.lock`).
2. Commits the change (`vX.Y.Z`) and pushes it.
3. Unless `--no-merge` is passed:
   - Creates a pull/merge request from `develop` into `main` (or `master`).
   - Merges it.
4. Creates a tag and a release named `vX.Y.Z` on GitHub or GitLab.

The hoster (GitHub or GitLab) is detected automatically from the `origin` remote URL. GitLab is
only supported for `gitlab.gwdg.de`.

## Requirements

- Python >= 3.11
- [uv](https://github.com/astral-sh/uv) or [Poetry](https://python-poetry.org/), depending on
  which one your project uses (detected via `uv.lock` or `poetry.lock`)
- A git repository with an `origin` remote pointing to GitHub or GitLab
- A `pyproject.toml` in the current directory
- An access token for your hoster (see below)

## Installation

```bash
pipx install git+https://github.com/thusser/do-python-release
```

or with uv:

```bash
uv tool install git+https://github.com/thusser/do-python-release
```

Since this installs directly from the `main` branch, neither tool will pick up new commits on
its own. To update to the latest version, reinstall with `--force`:

```bash
pipx install --force git+https://github.com/thusser/do-python-release
# or
uv tool install --force git+https://github.com/thusser/do-python-release
```

## Usage

Run from the root of your project (where `pyproject.toml` lives):

```bash
do-python-release
```

You'll be shown a plan of what will happen and asked to confirm before anything is changed.

### Access tokens

Provide a token either via `--token` or an environment variable:

- GitHub: `GITHUB_ACCESS_TOKEN`
- GitLab: `GITLAB_ACCESS_TOKEN`

### Options

| Flag              | Description                                                              |
|-------------------|---------------------------------------------------------------------------|
| `-v`, `--version` | Version bump to apply (e.g. `patch`, `minor`, `major`). Defaults to `patch`. |
| `-t`, `--token`   | Access token for GitHub/GitLab. Falls back to the environment variables above. |
| `-y`, `--yes`     | Auto-accept the confirmation prompt.                                    |
| `--no-merge`      | Skip the `develop` → `main` PR/merge step; bump, commit, and release directly on the current branch. |

### Examples

Standard release (requires a `develop` branch and merges into `main`/`master`):

```bash
do-python-release -v minor
```

Release from the current branch without merging:

```bash
do-python-release --no-merge -y
```

## Notes

- Without `--no-merge`, the tool requires a `develop` branch and a `main` or `master` branch to
  exist on the remote, and expects to be run from `develop`.
- The release title and body are both derived from the new version, e.g. `v1.2.3`.

## License

MIT — see [LICENSE](LICENSE).