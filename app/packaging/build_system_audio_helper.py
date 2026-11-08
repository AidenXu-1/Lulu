"""Compile the existing native recorder at build time, never on an end user's Mac."""
from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import platform
import subprocess


def build(app_root: Path, output: Path) -> Path:
    tree = ast.parse((app_root / "src/core/recording/macos_system_audio.py").read_text())
    source = next(ast.literal_eval(node.value) for node in tree.body
                  if isinstance(node, ast.Assign) and any(
                      isinstance(target, ast.Name) and target.id == "_SWIFT_HELPER" for target in node.targets))
    output.mkdir(parents=True, exist_ok=True)
    swift = output / "SystemAudioCapture.swift"
    binary = output / "SystemAudioCapture"
    swift.write_text(source)
    env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(output / "module-cache"))
    subprocess.run([
        "/usr/bin/xcrun", "swiftc", "-parse-as-library", "-target",
        f"{platform.machine()}-apple-macosx15.0", str(swift),
        "-framework", "ScreenCaptureKit", "-framework", "AVFoundation",
        "-framework", "CoreMedia", "-o", str(binary),
    ], env=env, check=True, timeout=90)
    return binary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.output.resolve()))
