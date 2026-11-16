#!/usr/bin/env python3
"""Create and validate a clean AstrBot plugin archive under the 16 MiB limit."""

import argparse
import json
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = [
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "README.zh-CN.md",
    "LICENSE",
    "NOTICE",
]
REQUIRED = {
    "main.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "codex_orchestrator/service.py",
}


def build_archive(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for rel in INCLUDE:
            bundle.write(ROOT / rel, rel)
        for source in sorted((ROOT / "codex_orchestrator").glob("*.py")):
            bundle.write(source, f"codex_orchestrator/{source.name}")
    if path.stat().st_size >= 16 * 1024 * 1024:
        path.unlink(missing_ok=True)
        raise SystemExit("plugin archive exceeds AstrBot's 16 MiB limit")
    with zipfile.ZipFile(path) as bundle:
        if not REQUIRED <= set(bundle.namelist()):
            raise SystemExit("plugin archive is missing required install files")
        if not isinstance(json.loads(bundle.read("_conf_schema.json")), dict):
            raise SystemExit("AstrBot configuration schema must be a mapping object")
    print(f"plugin archive OK: {path.stat().st_size} bytes ({path})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="write the installable ZIP here")
    args = parser.parse_args()
    if args.output:
        build_archive(args.output.resolve())
    else:
        with tempfile.TemporaryDirectory(prefix="astrbot-codex-package-") as temp:
            build_archive(Path(temp) / "astrbot_plugin_codex_orchestrator.zip")
