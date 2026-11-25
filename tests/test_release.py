import shutil
import subprocess
import sys

import pytest

import do_python_release as dpr


@pytest.mark.parametrize("current, spec, expected", [
    # pre-release bump on a stable version also bumps the patch level
    ("2.28.3", "dev", "uv version --bump patch --bump dev"),
    ("2.28.3", "alpha", "uv version --bump patch --bump alpha"),
    ("2.28.3", "rc", "uv version --bump patch --bump rc"),
    # continuing a pre-release track stays as is
    ("2.29.0.dev1", "dev", "uv version --bump dev"),
    ("2.29.0a1", "beta", "uv version --bump beta"),
    # patch on a pre-release releases that version instead of skipping it
    ("2.29.0.dev1", "patch", "uv version --bump stable"),
    ("2.29.0rc1", "patch", "uv version --bump stable"),
    # plain bumps
    ("2.28.3", "patch", "uv version --bump patch"),
    ("2.28.3", "minor", "uv version --bump minor"),
    ("2.29.0.dev1", "minor", "uv version --bump minor"),
    # explicit bump lists are passed through
    ("2.28.3", "minor,dev", "uv version --bump minor --bump dev"),
    ("2.28.3", "minor, dev", "uv version --bump minor --bump dev"),
    # explicit versions are passed positionally
    ("2.28.3", "2.29.0.dev1", "uv version 2.29.0.dev1"),
])
def test_uv_command(current, spec, expected):
    assert dpr.uv_command(current, spec) == expected


def test_uv_command_invalid_list():
    with pytest.raises(dpr.ReleaseError, match="Invalid bump list"):
        dpr.uv_command("2.28.3", "minor,foo")


@pytest.mark.parametrize("spec", ["patch", "preminor", "prerelease", "2.29.0a0"])
def test_poetry_command(spec):
    assert dpr.poetry_command(spec) == f"poetry version {spec}"


@pytest.mark.parametrize("spec", ["dev", "alpha", "rc", "stable", "minor,dev"])
def test_poetry_command_invalid(spec):
    with pytest.raises(dpr.ReleaseError):
        dpr.poetry_command(spec)


@pytest.mark.parametrize("current, new, expected", [
    ("2.29.0.dev1", "2.29.0", True),
    ("2.29.0rc1", "2.29.0", True),
    ("2.29.0.dev1", "2.29.0.dev2", False),
    ("2.28.3", "2.28.4", False),
    ("2.28.3", "2.28.4.dev1", False),
    ("garbage", "2.28.4", False),
])
def test_leaves_prerelease(current, new, expected):
    assert dpr.leaves_prerelease(current, new) == expected


def _uv_project(path, version):
    (path / "pyproject.toml").write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    (path / "uv.lock").write_text("")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
@pytest.mark.parametrize("current, spec, expected", [
    ("2.28.3", "dev", "2.28.4.dev1"),
    ("2.28.3", "minor,dev", "2.29.0.dev1"),
    ("2.29.0.dev1", None, "2.29.0.dev2"),
    ("2.29.0.dev1", "patch", "2.29.0"),
    ("2.28.3", None, "2.28.4"),
])
def test_preview_uv(tmp_path, monkeypatch, current, spec, expected):
    _uv_project(tmp_path, current)
    monkeypatch.chdir(tmp_path)
    v = dpr.Version()
    assert v.preview(spec) == expected
    # a dry run must not change anything
    assert v.version() == current


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_preview_uv_fails_before_changes(tmp_path, monkeypatch):
    _uv_project(tmp_path, "2.29.0rc1")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(dpr.ReleaseError, match="would fail"):
        dpr.Version().preview("dev")


def _run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["do-python-release", *args])
    return dpr.main()


def test_main_outside_git_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert _run_main(monkeypatch) == 1
    assert f"Not a git repository: {tmp_path}" in capsys.readouterr().err


def test_main_without_origin(tmp_path, monkeypatch, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    assert _run_main(monkeypatch) == 1
    assert "No 'origin' remote in" in capsys.readouterr().err


def test_main_reports_failed_command(monkeypatch, capsys):
    def fail():
        raise subprocess.CalledProcessError(2, "uv version --bump dev")

    monkeypatch.setattr(dpr, "run", fail)
    assert dpr.main() == 1
    err = capsys.readouterr().err
    assert "Command failed (exit 2): uv version --bump dev" in err
    assert "Traceback" not in err


class FakeRepo:
    def __init__(self):
        self.calls = []

    def create_git_tag_and_release(self, **kwargs):
        self.calls.append(kwargs)


@pytest.mark.parametrize("prerelease, latest", [(True, "false"), (False, "true")])
def test_github_release_tags_given_sha(prerelease, latest):
    gh = dpr.GitHub()
    gh.repo = FakeRepo()
    gh.release(title="v1.0.0", body="version 1.0.0", sha="abc123", prerelease=prerelease)
    call = gh.repo.calls[0]
    assert call["object"] == "abc123"
    assert call["type"] == "commit"
    assert call["prerelease"] is prerelease
    assert call["make_latest"] == latest


class FakePR:
    def __init__(self, merged, sha="merge123", message=""):
        self.status = type("Status", (), {"merged": merged, "sha": sha, "message": message})()

    def merge(self, **kwargs):
        return self.status


def test_github_merge_returns_sha():
    gh = dpr.GitHub()
    gh.pr = FakePR(merged=True)
    assert gh.merge(title="t", body="b") == "merge123"


def test_github_merge_fails():
    gh = dpr.GitHub()
    gh.pr = FakePR(merged=False, message="conflict")
    with pytest.raises(dpr.ReleaseError, match="conflict"):
        gh.merge(title="t", body="b")


class FakeMR:
    iid = 1

    def __init__(self, result):
        self.result = result

    def merge(self):
        return self.result


class FakeMRs:
    def __init__(self, later):
        self.later = later

    def get(self, iid):
        return type("MR", (), {"merge_status": "can_be_merged", "attributes": self.later})()


@pytest.mark.parametrize("result, later, expected", [
    ({"merge_commit_sha": "m1", "sha": "h1"}, {}, "m1"),
    # merge commit filled in only on a later fetch
    ({"merge_commit_sha": None, "state": "opened", "sha": "h1"}, {"merge_commit_sha": "m2"}, "m2"),
    # fast-forward merge: no merge commit, head sha is the new target head
    ({"merge_commit_sha": None, "state": "merged", "sha": "h1"}, {}, "h1"),
])
def test_gitlab_merge_returns_sha(monkeypatch, result, later, expected):
    monkeypatch.setattr(dpr.time, "sleep", lambda s: None)
    gl = dpr.GitLab()
    gl.mr = FakeMR(result)
    gl.project = type("Project", (), {"mergerequests": FakeMRs(later)})()
    assert gl.merge(title="t", body="b") == expected


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_pushed_commit(tmp_path, monkeypatch):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    _git("init", "-q", "--bare", str(origin), cwd=tmp_path)
    _git("init", "-q", "-b", "develop", str(work), cwd=tmp_path)
    _git("remote", "add", "origin", str(origin), cwd=work)
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    _git(*env, "commit", "-q", "--allow-empty", "-m", "one", cwd=work)
    _git("push", "-q", "origin", "develop", cwd=work)
    monkeypatch.chdir(work)
    assert dpr.pushed_commit("develop") == _git("rev-parse", "HEAD", cwd=work)

    # a local commit that was not pushed must not be released
    _git(*env, "commit", "-q", "--allow-empty", "-m", "two", cwd=work)
    with pytest.raises(dpr.ReleaseError, match="not at the pushed commit"):
        dpr.pushed_commit("develop")
