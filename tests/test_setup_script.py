"""End-to-end tests for setup.sh.

The wizard is run for real, with canned answers on stdin and stub `docker`
and `curl` executables first on PATH, so no network and no daemon are ever
touched. Assertions are on the .env it writes and on its exit status.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

STUB_DOCKER = r"""#!/usr/bin/env bash
# Stub docker: enough of the CLI for setup.sh, driven by env vars.
case "$1 $2" in
  "info ")            exit 0 ;;
  "--version ")       echo "Docker version 27.0.0-stub"; exit 0 ;;
  "compose version")  exit 0 ;;
  "compose up")       echo "compose up $*" >> "$STUB_CALLS"; exit 0 ;;
  "compose ps")       [ "${STUB_PS_EXIT:-0}" = 0 ] && echo "stubcontainer123"; exit "${STUB_PS_EXIT:-0}" ;;
  "compose logs")     printf '%s\n' "$STUB_LOGS"; exit "${STUB_LOGS_EXIT:-0}" ;;
  "inspect --format") echo "$STUB_HEALTH"; exit 0 ;;
esac
echo "stub docker: unhandled args: $*" >&2
exit 99
"""

STUB_CURL = r"""#!/usr/bin/env bash
# Stub curl: every endpoint is reachable and answers 200/207.
out=""; code="200"; want_code=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    -w) want_code="$2"; shift ;;
    -X) [ "$2" = "PROPFIND" ] && code="207"; shift ;;
  esac
  shift
done
[ -n "$out" ] && : > "$out"
# setup.sh passes -w "%{http_code}" whenever it wants the status code
[ -n "$want_code" ] && printf '%s' "$code"
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _path_hiding(tool: str, mirror_dir: Path) -> str:
    """Real PATH with `tool` made unresolvable. A bare stub-only PATH hides
    everything (cat, id, the /usr/bin/env a stub's shebang needs), which isn't
    what a machine actually missing one tool looks like. Instead, mirror (via
    symlinks) just the PATH directories that contain `tool`, omitting it there,
    and leave every other directory as-is."""
    parts = []
    for i, d in enumerate(os.environ["PATH"].split(os.pathsep)):
        src = Path(d)
        if src.is_dir() and (src / tool).exists():
            mirror = mirror_dir / f"dir{i}"
            mirror.mkdir(parents=True, exist_ok=True)
            for entry in src.iterdir():
                if entry.name != tool:
                    try:
                        (mirror / entry.name).symlink_to(entry)
                    except OSError:
                        pass
            parts.append(str(mirror))
        else:
            parts.append(str(src))
    return os.pathsep.join(parts)


def run_setup(
    tmp_path: Path,
    answers: list[str],
    *,
    health="running healthy",
    logs="",
    env_overrides: dict | None = None,
) -> subprocess.CompletedProcess:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    shutil.copy(REPO / "setup.sh", work / "setup.sh")
    shutil.copy(REPO / "docker-compose.yml", work / "docker-compose.yml")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    _write_exec(stubs / "docker", STUB_DOCKER)
    _write_exec(stubs / "curl", STUB_CURL)
    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "STUB_HEALTH": health,
        "STUB_LOGS": logs,
        "STUB_CALLS": str(tmp_path / "docker-calls.log"),
        # Keep the health wait fast; setup.sh reads these overrides.
        "SETUP_HEALTH_POLL_INTERVAL": "0",
        "SETUP_HEALTH_TIMEOUT": "2",
    }
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", "./setup.sh"],
        cwd=work,
        input="\n".join(answers) + "\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def read_env(tmp_path: Path) -> dict[str, str]:
    values = {}
    for line in (tmp_path / "work" / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


# Answers in prompt order. Keep these in one place so a reordered prompt
# breaks every test the same way.
def answers(
    *,
    storage="A",
    folder="meeting-notes",
    email="n",
    smtp_host="",
    smtp_port="",
    smtp_from="notes@example.com",
    smtp_auth="n",
    overrides="n",
) -> list[str]:
    a = [
        "y",  # bot user created?
        "https://cloud.example.com",  # Nextcloud URL
        "ai-notetaker",  # bot username
        "s3cret",  # bot password
        "n",  # Yunohost SSO?
        "AIza-test-key",  # Gemini key
        storage,  # A) Nextcloud  B) local
    ]
    if storage == "A":
        a.append(folder)
    a.append(email)
    if email == "y":
        a += [smtp_host, smtp_port, smtp_from, smtp_auth]
        if smtp_auth == "y":
            a += ["mailer", "mailpass"]
        a.append(overrides)
    return a


def test_local_storage_writes_the_container_path_not_a_host_path(tmp_path):
    """LOCAL_NOTES_DIR is read inside the container; ./notes there is /app/notes,
    which is not mounted and vanishes on restart. Only /data/notes persists."""
    result = run_setup(tmp_path, answers(storage="B"))
    assert result.returncode == 0, result.stdout + result.stderr
    env = read_env(tmp_path)
    assert env["NOTES_STORAGE"] == "local"
    assert env["LOCAL_NOTES_DIR"] == "/data/notes"
    assert "./notes" not in (tmp_path / "work" / ".env").read_text()


@pytest.mark.parametrize("typed", ["localhost", "127.0.0.1"])
def test_smtp_localhost_is_rewritten_for_the_container(tmp_path, typed):
    result = run_setup(tmp_path, answers(email="y", smtp_host=typed, smtp_port="25"))
    assert result.returncode == 0, result.stdout + result.stderr
    assert read_env(tmp_path)["SMTP_HOST"] == "host.docker.internal"
    assert "host.docker.internal" in result.stdout


def test_smtp_defaults_point_at_the_docker_host(tmp_path):
    result = run_setup(tmp_path, answers(email="y"))  # accept every default
    assert result.returncode == 0, result.stdout + result.stderr
    env = read_env(tmp_path)
    assert env["SMTP_HOST"] == "host.docker.internal"
    assert env["SMTP_PORT"] == "587"


ENV_KEY_RE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]+)=")


def env_example_keys() -> set[str]:
    keys = set()
    for line in (REPO / ".env.example").read_text().splitlines():
        m = ENV_KEY_RE.match(line.strip())
        if m:
            keys.add(m.group(1))
    return keys


def test_generated_env_carries_every_documented_setting(tmp_path):
    """Whatever .env.example documents, the wizard writes (possibly commented),
    so a user editing .env later sees every knob in one place."""
    result = run_setup(tmp_path, answers(email="y", smtp_auth="y"))
    assert result.returncode == 0, result.stdout + result.stderr
    text = (tmp_path / "work" / ".env").read_text()
    written = {
        m.group(1)
        for m in (ENV_KEY_RE.match(l.strip()) for l in text.splitlines())
        if m
    }
    missing = env_example_keys() - written
    assert not missing, f"wizard does not write: {sorted(missing)}"


def test_env_file_is_private(tmp_path):
    result = run_setup(tmp_path, answers())
    assert result.returncode == 0, result.stdout + result.stderr
    mode = stat.S_IMODE((tmp_path / "work" / ".env").stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_rerun_backs_up_the_existing_env(tmp_path):
    (tmp_path / "work").mkdir()
    old = "NEXTCLOUD_URL=https://old.example.com\nGEMINI_API_KEY=old-key\n"
    (tmp_path / "work" / ".env").write_text(old)
    result = run_setup(tmp_path, answers())
    assert result.returncode == 0, result.stdout + result.stderr
    backups = sorted((tmp_path / "work").glob(".env.bak.*"))
    assert len(backups) == 1, backups
    assert backups[0].read_text() == old
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert read_env(tmp_path)["NEXTCLOUD_URL"] == "https://cloud.example.com"
    assert backups[0].name in result.stdout


def test_missing_curl_is_reported_up_front(tmp_path):
    """Every connectivity test in the wizard uses curl; without it the first
    failure would be a confusing 'Could not connect' at Step 2."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _write_exec(stubs / "docker", STUB_DOCKER)
    # Stub docker first on PATH, then the real PATH with curl hidden — a
    # machine genuinely missing curl still has cat/id/env for everything else.
    no_curl = _path_hiding("curl", tmp_path / "no-curl-path")
    env = {
        "PATH": f"{stubs}{os.pathsep}{no_curl}",
        "HOME": str(tmp_path),
        "STUB_HEALTH": "",
        "STUB_LOGS": "",
        "STUB_CALLS": str(tmp_path / "c.log"),
    }
    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(REPO / "setup.sh", work / "setup.sh")
    result = subprocess.run(
        [shutil.which("bash"), "./setup.sh"],
        cwd=work,
        input="y\n",
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert result.returncode != 0
    assert "curl" in result.stdout


def test_empty_web_password_is_left_unset_so_the_app_falls_back(tmp_path):
    """config.py uses the web password verbatim when the variable exists, so
    an empty NEXTCLOUD_WEB_PASSWORD= line would break every browser login."""
    result = run_setup(tmp_path, answers())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "NEXTCLOUD_WEB_PASSWORD" not in read_env(tmp_path)
    assert "# NEXTCLOUD_WEB_PASSWORD=" in (tmp_path / "work" / ".env").read_text()


PREFLIGHT_FAIL_LOG = (
    "2026-09-14 18:03:20 notetaker.preflight INFO PREFLIGHT ok: state-dir\n"
    "2026-09-14 18:03:21 notetaker.preflight ERROR PREFLIGHT FAILED: smtp: [Errno 111] Connection refused\n"
    "2026-09-14 18:03:21 notetaker.main ERROR Refusing to start with 1 failed preflight check(s). Set SKIP_PREFLIGHT=1 to override.\n"
)


def test_unhealthy_container_is_reported_not_celebrated(tmp_path):
    """The app refuses to start when a dependency check fails and the
    container crash-loops. The wizard must say so and show the reason."""
    result = run_setup(
        tmp_path, answers(), health="restarting unhealthy", logs=PREFLIGHT_FAIL_LOG
    )
    assert result.returncode == 1
    assert "PREFLIGHT FAILED: smtp" in result.stdout
    assert "Notetaker is running" not in result.stdout


def test_healthy_container_is_reported_running(tmp_path):
    result = run_setup(tmp_path, answers(), health="running healthy")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Notetaker is running" in result.stdout


def test_health_wait_times_out_loudly(tmp_path):
    """A container that never becomes healthy (stuck 'starting') is a failure too."""
    result = run_setup(
        tmp_path, answers(), health="running starting", logs="still starting"
    )
    assert result.returncode == 1
    assert "did not become healthy" in result.stdout


def test_compose_ps_failure_is_reported_not_a_raw_bash_error(tmp_path):
    """`CONTAINER_ID=$($COMPOSE_CMD ps -q notetaker | head -n1)` is not safe
    under `set -euo pipefail`: if `ps` exits non-zero while `head` still exits
    0, pipefail fails the pipeline and set -e aborts the script right there,
    before the friendly -z check ever runs. Repro: `set -euo pipefail;
    f() { return 3; }; x=$(f | head -n1); echo reached` never prints
    "reached". The wizard must show its own error, not die with a raw one."""
    result = run_setup(tmp_path, answers(), env_overrides={"STUB_PS_EXIT": "3"})
    assert result.returncode == 1
    assert "The notetaker container was not created" in result.stdout


def test_compose_logs_failure_still_shows_the_fix_hint(tmp_path):
    """Same pipefail class of bug inside show_startup_log: if `compose logs`
    exits non-zero, `set -e` must not abort the function before its trailing
    `info "Fix the setting..."` hint prints."""
    result = run_setup(
        tmp_path,
        answers(),
        health="restarting unhealthy",
        logs=PREFLIGHT_FAIL_LOG,
        env_overrides={"STUB_LOGS_EXIT": "1"},
    )
    assert result.returncode == 1
    assert "Fix the setting it names in .env" in result.stdout


def test_setup_script_parses_and_lints():
    subprocess.run(["bash", "-n", str(REPO / "setup.sh")], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(
            ["shellcheck", "-S", "warning", str(REPO / "setup.sh")], check=True
        )
    else:
        pytest.skip("shellcheck not installed")


def _code_env_names() -> set[str]:
    names = set()
    for py in (REPO / "src" / "notetaker").glob("*.py"):
        names |= set(
            re.findall(
                r'os\.(?:getenv|environ\.get|environ)\s*[\(\[]\s*"([A-Z][A-Z0-9_]+)"',
                py.read_text(),
            )
        )
    return names


def test_env_example_and_readme_document_every_setting_the_code_reads():
    code = _code_env_names()
    assert code, "no env reads found - regex broken?"
    readme = set(
        re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", (REPO / "README.md").read_text(), re.M)
    )
    assert not code - env_example_keys(), (
        f".env.example missing: {sorted(code - env_example_keys())}"
    )
    assert not code - readme, f"README table missing: {sorted(code - readme)}"
    assert not env_example_keys() - code, (
        f".env.example documents unknown settings: {sorted(env_example_keys() - code)}"
    )


def test_env_backups_are_gitignored():
    """A wizard re-run writes .env.bak.<stamp> holding the previous secrets.
    .gitignore matched only .env, so `git add -A` on this public repo would
    have staged them."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".env.bak.20260101-000000"],
        cwd=REPO,
        check=False,
    )
    assert result.returncode == 0, ".env.bak.* is not ignored"
