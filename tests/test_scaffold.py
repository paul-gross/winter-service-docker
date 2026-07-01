"""Tests for the docker_orchestrator scaffolder (docker_orchestrator.scaffold)."""

from __future__ import annotations

from pathlib import Path

import pytest

from docker_orchestrator.scaffold import main, scaffold


class TestScaffoldFunction:
    """Unit tests for the ``scaffold()`` function."""

    def test_writes_environment_compose_yaml(self, tmp_path: Path) -> None:
        written = scaffold(tmp_path)
        names = {p.name for p in written}
        assert "environment-compose.yaml" in names
        assert (tmp_path / "environment-compose.yaml").exists()

    def test_writes_workspace_compose_yaml(self, tmp_path: Path) -> None:
        written = scaffold(tmp_path)
        names = {p.name for p in written}
        assert "workspace-compose.yaml" in names
        assert (tmp_path / "workspace-compose.yaml").exists()

    def test_writes_config_toml(self, tmp_path: Path) -> None:
        written = scaffold(tmp_path)
        names = {p.name for p in written}
        assert "config.toml" in names
        assert (tmp_path / "config.toml").exists()

    def test_environment_compose_yaml_uses_wsd_port_substitution(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        content = (tmp_path / "environment-compose.yaml").read_text()
        assert "${WSD_PORT_" in content, "environment-compose.yaml must use ${WSD_PORT_*} placeholders"

    def test_workspace_compose_yaml_has_named_volume(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        content = (tmp_path / "workspace-compose.yaml").read_text()
        assert "volumes:" in content, "workspace-compose.yaml must declare a named volume section"

    def test_config_toml_has_project_prefix(self, tmp_path: Path) -> None:
        """project_prefix is documented as a commented-out optional override — it is
        not set by default (issue #5: the prefix defaults to WINTER_SERVICE_PREFIX)."""
        scaffold(tmp_path)
        content = (tmp_path / "config.toml").read_text()
        assert "project_prefix" in content
        assert '# project_prefix = "myapp"' in content

    def test_config_toml_has_environment_compose_file(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        content = (tmp_path / "config.toml").read_text()
        assert "environment_compose_file" in content

    def test_config_toml_has_workspace_compose_file(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        content = (tmp_path / "config.toml").read_text()
        assert "workspace_compose_file" in content

    def test_config_toml_has_service_entries(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        content = (tmp_path / "config.toml").read_text()
        assert "[[service]]" in content

    def test_creates_dest_dir_if_absent(self, tmp_path: Path) -> None:
        dest = tmp_path / "newdir" / "subdir"
        assert not dest.exists()
        scaffold(dest)
        assert dest.is_dir()
        assert (dest / "environment-compose.yaml").exists()
        assert (dest / "workspace-compose.yaml").exists()

    def test_refuses_to_clobber_without_force(self, tmp_path: Path) -> None:
        scaffold(tmp_path)
        with pytest.raises(FileExistsError, match="--force"):
            scaffold(tmp_path)

    def test_refuses_lists_conflicting_files(self, tmp_path: Path) -> None:
        (tmp_path / "environment-compose.yaml").write_text("existing", encoding="utf-8")
        with pytest.raises(FileExistsError) as exc_info:
            scaffold(tmp_path)
        assert "environment-compose.yaml" in str(exc_info.value)

    def test_force_overwrites_existing_files(self, tmp_path: Path) -> None:
        (tmp_path / "environment-compose.yaml").write_text("old content", encoding="utf-8")
        scaffold(tmp_path, force=True)
        content = (tmp_path / "environment-compose.yaml").read_text()
        assert "old content" not in content
        assert "${WSD_PORT_" in content

    def test_returns_list_of_paths(self, tmp_path: Path) -> None:
        written = scaffold(tmp_path)
        assert isinstance(written, list)
        assert len(written) == 3
        assert all(isinstance(p, Path) for p in written)

    def test_no_partial_write_on_collision(self, tmp_path: Path) -> None:
        """If any file exists, nothing should be written (pre-flight check)."""
        # Only config.toml exists.
        (tmp_path / "config.toml").write_text("existing", encoding="utf-8")
        with pytest.raises(FileExistsError):
            scaffold(tmp_path)
        # environment-compose.yaml should NOT have been written.
        assert not (tmp_path / "environment-compose.yaml").exists()


class TestScaffoldCLI:
    """Unit tests for the ``main()`` CLI entrypoint."""

    def test_cli_writes_files(self, tmp_path: Path) -> None:
        dest = tmp_path / "output"
        rc = main([str(dest)])
        assert rc == 0
        assert (dest / "environment-compose.yaml").exists()
        assert (dest / "workspace-compose.yaml").exists()
        assert (dest / "config.toml").exists()

    def test_cli_exits_1_on_collision(self, tmp_path: Path) -> None:
        dest = tmp_path / "output"
        main([str(dest)])
        rc = main([str(dest)])
        assert rc == 1

    def test_cli_force_flag_overwrites(self, tmp_path: Path) -> None:
        dest = tmp_path / "output"
        main([str(dest)])
        rc = main([str(dest), "--force"])
        assert rc == 0

    def test_cli_returns_0_on_success(self, tmp_path: Path) -> None:
        dest = tmp_path / "fresh"
        rc = main([str(dest)])
        assert rc == 0
