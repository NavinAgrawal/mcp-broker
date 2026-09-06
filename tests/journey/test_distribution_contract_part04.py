from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from tests.support.makefiles import read_make_variable_defaults


pytestmark = pytest.mark.journey
ROOT = Path(__file__).resolve().parents[2]


def test_local_mcp_registry_publish_reuses_gh_token_without_device_login(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "publisher-capture.txt"
    commands = {
        "gh": """#!/bin/sh
if [ "$1 $2" = "auth token" ]; then printf 'fake-github-token\\n'; exit 0; fi
exit 2
""",
        "mcp-publisher": """#!/bin/sh
env_state=missing
if [ "${MCP_GITHUB_TOKEN:-}" = "fake-github-token" ]; then env_state=present; fi
printf 'env=%s arg=%s\\n' "$env_state" "$*" >> "$PUBLISH_CAPTURE"
""",
        "python": """#!/bin/sh
printf 'publish\\n'
""",
    }
    for name, body in commands.items():
        executable = bin_dir / name
        executable.write_text(body, encoding="utf-8")
        executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["PUBLISH_CAPTURE"] = str(capture)

    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "_publish-everywhere-mcp-registry",
            f"PYTHON_BIN={bin_dir / 'python'}",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8") == (
        "env=present arg=login github\n"
        "env=present arg=publish\n"
    )
    assert "fake-github-token" not in result.stdout + result.stderr


@pytest.mark.parametrize("token_output", ["", "fail"])
def test_local_mcp_registry_publish_stops_when_gh_token_is_unavailable(
    tmp_path: Path,
    token_output: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "publisher-capture.txt"
    gh = bin_dir / "gh"
    gh.write_text(
        """#!/bin/sh
if [ "${TOKEN_OUTPUT:-}" = "fail" ]; then exit 1; fi
printf '%s' "${TOKEN_OUTPUT:-}"
""",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    publisher = bin_dir / "mcp-publisher"
    publisher.write_text(
        "#!/bin/sh\nprintf 'called\\n' >> \"$PUBLISH_CAPTURE\"\n",
        encoding="utf-8",
    )
    publisher.chmod(0o755)
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\nprintf 'publish\\n'\n", encoding="utf-8")
    python.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["PUBLISH_CAPTURE"] = str(capture)
    env["TOKEN_OUTPUT"] = token_output

    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "_publish-everywhere-mcp-registry",
            f"PYTHON_BIN={python}",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not capture.exists()


def test_docker_publish_uses_managed_attestation_builder(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "docker-capture.txt"
    builder_ready = tmp_path / "builder-ready"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CAPTURE"
if [ "$1 $2" = "buildx inspect" ] && [ ! -f "$BUILDER_READY" ]; then exit 1; fi
if [ "$1 $2" = "buildx create" ]; then : > "$BUILDER_READY"; fi
if [ "$1 $2" = "buildx inspect" ]; then printf 'Driver: %s\\n' "$EXPECTED_DRIVER"; fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    make_vars = read_make_variable_defaults(ROOT)
    builder = make_vars["DOCKER_RELEASE_BUILDER"]
    driver = make_vars["DOCKER_RELEASE_BUILDER_DRIVER"]
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["DOCKER_CAPTURE"] = str(capture)
    env["BUILDER_READY"] = str(builder_ready)
    env["EXPECTED_DRIVER"] = driver

    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "docker-buildx",
            "DOCKER_PUSH=1",
            "DOCKER_PLATFORMS=linux/arm64",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = capture.read_text(encoding="utf-8").splitlines()
    assert calls[0] == f"buildx inspect {builder}"
    assert calls[1] == (
        f"buildx create --name {builder} --driver {driver} --bootstrap"
    )
    assert calls[2] == f"buildx inspect {builder}"
    assert calls[3] == f"buildx inspect --bootstrap {builder}"
    assert calls[4].startswith(
        f"buildx build --builder {builder} --platform linux/arm64"
    )
    assert "--sbom=true" in calls[4]
    assert "--provenance=true" in calls[4]
    assert "--push" in calls[4]


def test_docker_publish_rejects_existing_builder_with_wrong_driver(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "docker-capture.txt"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$DOCKER_CAPTURE"
if [ "$1 $2" = "buildx inspect" ]; then printf 'Driver: docker\\n'; fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["DOCKER_CAPTURE"] = str(capture)

    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "docker-buildx",
            "DOCKER_PUSH=1",
            "DOCKER_PLATFORMS=linux/arm64",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "builder driver mismatch" in result.stderr
    assert not any("buildx build" in call for call in capture.read_text().splitlines())
