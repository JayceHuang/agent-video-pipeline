from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
RESOLVER = SKILL_ROOT / "scripts/resolve_profile.py"
VALIDATOR = SKILL_ROOT / "scripts/validate_profile.py"


class CentralizedConfigContractTests(unittest.TestCase):
    def make_workspace(self, base: Path) -> tuple[Path, Path, Path]:
        workspace = base / "workspace"
        project = workspace / "videos" / "episode-01"
        config_root = workspace / ".agent-video"
        project.mkdir(parents=True)
        for name in ("profiles", "projects", "assets", "resolved"):
            (config_root / name).mkdir(parents=True, exist_ok=True)
        (config_root / "profiles/demo.yaml").write_text(
            json.dumps({"profile_id": "demo"}), encoding="utf-8"
        )
        (config_root / "runtime.local.yaml").write_text(
            json.dumps({"pipeline_runtime": {"python": sys.executable}}), encoding="utf-8"
        )
        return workspace, project, config_root

    def run_resolver(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RESOLVER), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_missing_config_root_blocks_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            project.mkdir()
            result = self.run_resolver("--project", str(project), "--profile-id", "demo")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("mandatory .agent-video config root not found", result.stderr)

    def test_profile_outside_profiles_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, project, config_root = self.make_workspace(Path(temp))
            outsider = Path(temp) / "personal.yaml"
            outsider.write_text(json.dumps({"profile_id": "outside"}), encoding="utf-8")
            result = self.run_resolver(
                "--config-root",
                str(config_root),
                "--profile",
                str(outsider),
                "--project",
                str(project),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("workspace profile must be stored under", result.stderr)

    def test_centralized_config_resolves_and_records_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, project, config_root = self.make_workspace(Path(temp))
            result = self.run_resolver("--project", str(project), "--profile-id", "demo")
            self.assertEqual(result.returncode, 0, result.stderr)
            resolved_path = project / ".pipeline/resolved-profile.json"
            resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
            self.assertEqual(resolved["_meta"]["config_contract_version"], 1)
            self.assertEqual(Path(resolved["_meta"]["config_root"]), config_root.resolve())
            self.assertEqual(
                [source["role"] for source in resolved["_meta"]["sources"]],
                ["base", "profile", "runtime"],
            )

            validation = subprocess.run(
                [sys.executable, str(VALIDATOR), "--profile", str(resolved_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(validation.returncode, 0, validation.stdout + validation.stderr)

    def test_downstream_validation_fails_when_config_root_breaks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, project, config_root = self.make_workspace(Path(temp))
            result = self.run_resolver("--project", str(project), "--profile-id", "demo")
            self.assertEqual(result.returncode, 0, result.stderr)
            (config_root / "assets").rmdir()
            validation = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--profile",
                    str(project / ".pipeline/resolved-profile.json"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(validation.returncode, 0)
            self.assertIn("required config directory is missing", validation.stdout)

    def test_symlink_cannot_escape_profiles_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            _, project, config_root = self.make_workspace(Path(temp))
            outsider = Path(temp) / "outside.yaml"
            outsider.write_text(json.dumps({"profile_id": "outside"}), encoding="utf-8")
            escaped = config_root / "profiles/escaped.yaml"
            escaped.symlink_to(outsider)
            result = self.run_resolver(
                "--config-root",
                str(config_root),
                "--profile",
                str(escaped),
                "--project",
                str(project),
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("workspace profile must be stored under", result.stderr)


if __name__ == "__main__":
    unittest.main()
