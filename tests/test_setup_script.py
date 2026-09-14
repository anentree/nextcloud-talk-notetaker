"""End-to-end tests for setup.sh.

The wizard is run for real, with canned answers on stdin and stub `docker`
and `curl` executables first on PATH, so no network and no daemon are ever
touched. Assertions are on the .env it writes and on its exit status.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

STUB_DOCKER = r'''#!/usr/bin/env bash
# Stub docker: enough of the CLI for setup.sh, driven by env vars.
case "$1 $2" in
  "info ")            exit 0 ;;
  "--version ")       echo "Docker version 27.0.0-stub"; exit 0 ;;
  "compose version")  exit 0 ;;
  "compose up")       echo "compose up $*" >> "$STUB_CALLS"; exit 0 ;;
  "compose ps")       echo "stubcontainer123"; exit 0 ;;
  "compose logs")     printf '%s\n' "$STUB_LOGS"; exit 0 ;;
  "inspect --format") echo "$STUB_HEALTH"; exit 0 ;;
esac
echo "stub docker: unhandled args: $*" >&2
exit 99
'''

STUB_CURL = r'''#!/usr/bin/env bash
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
'''


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run_setup(tmp_path: Path, answers: list[str], *, health="running healthy",
              logs="", env_overrides: dict | None = None) -> subprocess.CompletedProcess:
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
        ["bash", "./setup.sh"], cwd=work, input="\n".join(answers) + "\n",
        text=True, capture_output=True, env=env, timeout=30,
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
def answers(*, storage="A", folder="meeting-notes", email="n",
            smtp_host="", smtp_port="", smtp_from="notes@example.com",
            smtp_auth="n", overrides="n") -> list[str]:
    a = [
        "y",                          # bot user created?
        "https://cloud.example.com",  # Nextcloud URL
        "ai-notetaker",               # bot username
        "s3cret",                     # bot password
        "n",                          # Yunohost SSO?
        "AIza-test-key",              # Gemini key
        storage,                      # A) Nextcloud  B) local
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
