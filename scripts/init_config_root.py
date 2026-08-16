#!/usr/bin/env python3
"""Create the mandatory centralized .agent-video workspace configuration."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

from profile_config import CONFIG_ROOT_NAME, SKILL_ROOT, validate_config_root


ASSET_DIRS = ("voice", "avatar", "character", "logo", "music")
TEMPLATE_ROOT = SKILL_ROOT / "references/templates"


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_template(name: str, replacements: dict[str, str]) -> str:
    path = TEMPLATE_ROOT / name
    if not path.is_file():
        raise FileNotFoundError(f"bundled sanitized template is missing: {path}")
    rendered = path.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    unresolved = sorted(set(re.findall(r"__[A-Z0-9_]+__", rendered)))
    if unresolved:
        raise ValueError(f"unresolved template markers in {path}: {', '.join(unresolved)}")
    return rendered


def validate_identifier(value: str, label: str) -> str:
    cleaned = value.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or cleaned.startswith(".")
        or not re.fullmatch(r"[\w.-]+", cleaned, flags=re.UNICODE)
    ):
        raise ValueError(
            f"{label} must use letters, numbers, underscore, dot, or hyphen without path separators"
        )
    return cleaned


def write_if_missing(path: Path, content: str, created: list[Path], kept: list[Path]) -> None:
    if path.exists():
        kept.append(path)
        return
    path.write_text(content, encoding="utf-8", newline="\n")
    created.append(path)


def render_profile(profile_id: str) -> str:
    return render_template(
        "workspace.example.yaml",
        {"__PROFILE_ID__": yaml_string(profile_id)},
    )


def render_runtime(pipeline_python: Path, tts_python: Path | None, model_path: Path | None) -> str:
    tts_value = "null" if tts_python is None else yaml_string(str(tts_python))
    model_value = "null" if model_path is None else yaml_string(str(model_path))
    return render_template(
        "runtime.local.example.yaml",
        {
            "__PIPELINE_PYTHON__": yaml_string(str(pipeline_python)),
            "__TTS_PYTHON__": tts_value,
            "__MODEL_PATH__": model_value,
        },
    )


def render_project(project_id: str) -> str:
    return f"""# Optional one-project overrides for {project_id}.
# Keep only exceptions here; reusable workspace settings belong in profiles/.
{{}}
"""


def render_readme() -> str:
    return """# Centralized Agent Video Configuration

This directory is required by agent-video-pipeline.

- `profiles/`: reusable neutral workspace profiles and optional explicit overrides
- `runtime.local.yaml`: machine-local interpreters, models, and credentials references
- `projects/`: optional one-project overrides
- `assets/`: optional reusable voice, avatar, character, logo, and music assets
- `resolved/`: local resolution cache

Do not copy workspace profiles or runtime settings into individual video projects.
Project-local `.pipeline/resolved-profile.*` files are generated snapshots, not editable sources.
"""


def format_command(parts: list[str]) -> str:
    if os.name == "nt":
        import subprocess

        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--profile-id",
        default="workspace",
        help="neutral external profile id (default: workspace)",
    )
    parser.add_argument("--pipeline-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--tts-python", type=Path)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument("--project-id")
    args = parser.parse_args()

    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        parser.error(f"workspace must already exist and be a directory: {workspace}")
    profile_id = validate_identifier(args.profile_id, "profile-id")
    project_id = validate_identifier(args.project_id, "project-id") if args.project_id else None

    pipeline_python = args.pipeline_python.expanduser().resolve()
    if not pipeline_python.is_file():
        parser.error(f"pipeline interpreter does not exist: {pipeline_python}")
    tts_python = args.tts_python.expanduser().resolve() if args.tts_python else None
    if tts_python is not None and not tts_python.is_file():
        parser.error(f"TTS interpreter does not exist: {tts_python}")
    model_path = args.model_path.expanduser().resolve() if args.model_path else None
    if model_path is not None and not model_path.exists():
        parser.error(f"model path does not exist: {model_path}")

    config_root = workspace / CONFIG_ROOT_NAME
    created: list[Path] = []
    kept: list[Path] = []
    for directory in (
        config_root,
        config_root / "profiles",
        config_root / "projects",
        config_root / "assets",
        config_root / "resolved",
        *[config_root / "assets" / name for name in ASSET_DIRS],
    ):
        if directory.exists():
            if not directory.is_dir() or directory.is_symlink():
                parser.error(f"required directory path is unsafe or not a directory: {directory}")
            kept.append(directory)
        else:
            directory.mkdir()
            created.append(directory)

    write_if_missing(
        config_root / "profiles" / f"{profile_id}.yaml",
        render_profile(profile_id),
        created,
        kept,
    )
    write_if_missing(
        config_root / "runtime.local.yaml",
        render_runtime(pipeline_python, tts_python, model_path),
        created,
        kept,
    )
    if project_id is not None:
        write_if_missing(
            config_root / "projects" / f"{project_id}.yaml",
            render_project(project_id),
            created,
            kept,
        )
    write_if_missing(
        config_root / ".gitignore",
        "runtime.local.yaml\nresolved/\n",
        created,
        kept,
    )
    write_if_missing(config_root / "README.md", render_readme(), created, kept)

    errors = validate_config_root(config_root)
    if errors:
        raise ValueError("initialized config root failed validation: " + "; ".join(errors))

    resolver = Path(__file__).resolve().with_name("resolve_profile.py")
    next_command = format_command(
        [
            str(pipeline_python),
            str(resolver),
            "--config-root",
            str(config_root),
            "--profile-id",
            profile_id,
            "--project",
            "<project-directory>",
        ]
    )
    print(f"centralized config root ready: {config_root}")
    print(f"created: {len(created)}; kept unchanged: {len(kept)}")
    print("next: edit the generated profile/runtime, place reusable assets under assets/, then run:")
    print(next_command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
