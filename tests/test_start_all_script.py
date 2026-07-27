import os
import subprocess
import textwrap
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _prepare_fake_bin(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "docker",
        """
        #!/usr/bin/env bash
        set -euo pipefail

        printf '%s\\n' "$*" >> "${DOCKER_CALL_LOG:?}"

        if [[ "$1" == "compose" && "${2:-}" == "version" ]]; then
          exit 0
        fi

        if [[ "$1" == "info" ]]; then
          exit 0
        fi

        if [[ "$1" == "ps" && "${2:-}" == "-aq" ]]; then
          if [[ -n "${FAKE_CONTAINER_ID:-existing-neo4j}" ]]; then
            printf '%s\\n' "${FAKE_CONTAINER_ID:-existing-neo4j}"
          fi
          exit 0
        fi

        if [[ "$1" == "inspect" ]]; then
          printf '%s\\n' "${FAKE_CONTAINER_RUNNING:-true}"
          exit 0
        fi

        if [[ "$1" == "start" ]]; then
          exit 0
        fi

        if [[ "$1" == "compose" && "$*" == *" up -d neo4j" ]]; then
          printf '%s\\n' "compose up should not be called when named container exists" >&2
          exit 42
        fi

        printf 'unexpected docker args: %s\\n' "$*" >&2
        exit 99
        """,
    )

    for command_name in ("lsof", "curl"):
        _write_executable(
            fake_bin / command_name,
            """
            #!/usr/bin/env bash
            exit 0
            """,
        )

    return fake_bin


def _run_start_all(tmp_path: Path, *, container_running: str) -> subprocess.CompletedProcess[str]:
    fake_bin = _prepare_fake_bin(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "BACKEND_PORT": "18000",
            "DOCKER_CALL_LOG": str(tmp_path / "docker_calls.log"),
            "FAKE_CONTAINER_RUNNING": container_running,
            "FRONTEND_PORT": "15173",
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
            "START_NEO4J": "true",
        }
    )
    return subprocess.run(
        ["bash", str(ROOT_DIR / "start_all.sh")],
        cwd=ROOT_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_start_all_reuses_running_neo4j_container(tmp_path: Path) -> None:
    result = _run_start_all(tmp_path, container_running="true")

    assert result.returncode == 0, result.stderr
    assert "Neo4j container stock-graphiti-neo4j already exists and is running. Reusing it." in result.stdout
    assert "compose --profile graphiti" not in (tmp_path / "docker_calls.log").read_text(encoding="utf-8")


def test_start_all_starts_existing_stopped_neo4j_container(tmp_path: Path) -> None:
    result = _run_start_all(tmp_path, container_running="false")

    assert result.returncode == 0, result.stderr
    assert "Neo4j container stock-graphiti-neo4j already exists but is not running. Starting it ..." in result.stdout
    assert "start existing-neo4j" in (tmp_path / "docker_calls.log").read_text(encoding="utf-8")
