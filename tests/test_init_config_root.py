from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]
INITIALIZER = SKILL_ROOT / "scripts/init_config_root.py"
RESOLVER = SKILL_ROOT / "scripts/resolve_profile.py"
TEMPLATE_ROOT = SKILL_ROOT / "references/templates"


class ConfigRootInitializerTests(unittest.TestCase):
    def run_initializer(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INITIALIZER), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_initializer_creates_complete_cross_platform_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            result = self.run_initializer(
                "--workspace",
                str(workspace),
                "--project-id",
                "episode-01",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            root = workspace / ".agent-video"
            for directory in (
                "profiles",
                "projects",
                "assets",
                "assets/voice",
                "assets/avatar",
                "assets/character",
                "assets/logo",
                "assets/music",
                "resolved",
            ):
                self.assertTrue((root / directory).is_dir(), directory)
            for file_name in (
                "profiles/workspace.yaml",
                "projects/episode-01.yaml",
                "runtime.local.yaml",
                ".gitignore",
                "README.md",
            ):
                self.assertTrue((root / file_name).is_file(), file_name)
            generated_profile = (root / "profiles/workspace.yaml").read_text(encoding="utf-8")
            self.assertIn("profile_id: \"workspace\"", generated_profile)
            self.assertIn("language: auto", generated_profile)
            self.assertNotIn("private-author", generated_profile.lower())

            project = workspace / "videos/episode-01"
            project.mkdir(parents=True)
            resolved = subprocess.run(
                [
                    sys.executable,
                    str(RESOLVER),
                    "--project",
                    str(project),
                    "--profile-id",
                    "workspace",
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(resolved.returncode, 0, resolved.stderr)

    def test_bundled_templates_are_sanitized_and_are_used(self) -> None:
        workspace_template = (TEMPLATE_ROOT / "workspace.example.yaml").read_text(
            encoding="utf-8"
        )
        runtime_template = (TEMPLATE_ROOT / "runtime.local.example.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("__PROFILE_ID__", workspace_template)
        self.assertIn("__PIPELINE_PYTHON__", runtime_template)
        for forbidden in ("/Users/", "C:\\Users\\", "private-author", "fixed-brand"):
            self.assertNotIn(forbidden, workspace_template)
            self.assertNotIn(forbidden, runtime_template)

        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            result = self.run_initializer("--workspace", str(workspace))
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = (workspace / ".agent-video/profiles/workspace.yaml").read_text(
                encoding="utf-8"
            )
            expected = workspace_template.replace("__PROFILE_ID__", '"workspace"')
            self.assertEqual(generated, expected)

    def test_repeated_initialization_preserves_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            first = self.run_initializer(
                "--workspace", str(workspace)
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            profile = workspace / ".agent-video/profiles/workspace.yaml"
            runtime = workspace / ".agent-video/runtime.local.yaml"
            profile.write_text("profile_id: customized\n", encoding="utf-8")
            runtime.write_text("pipeline_runtime:\n  python: customized\n", encoding="utf-8")

            second = self.run_initializer(
                "--workspace", str(workspace)
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(profile.read_text(encoding="utf-8"), "profile_id: customized\n")
            self.assertEqual(
                runtime.read_text(encoding="utf-8"),
                "pipeline_runtime:\n  python: customized\n",
            )

    def test_invalid_profile_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            workspace.mkdir()
            result = self.run_initializer(
                "--workspace", str(workspace), "--profile-id", "../escape"
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("profile-id must use", result.stderr)


if __name__ == "__main__":
    unittest.main()
