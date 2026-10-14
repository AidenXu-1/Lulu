#!/usr/bin/env python3
"""Generate the DMG install guide from validated release identity."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat


_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_BUILD = re.compile(r"^[0-9]{8}\.[0-9]+$")


def render_install_instructions(*, version: str, build: str, dmg_name: str) -> str:
    if not _VERSION.fullmatch(version) or not _BUILD.fullmatch(build):
        raise ValueError("安装说明版本身份无效")
    expected_dmg = f"Lulu-{version}-{build}-arm64.dmg"
    if dmg_name != expected_dmg:
        raise ValueError("安装说明 DMG 名称与版本身份不一致")
    return f"""Lulu {version} 安装说明（build {build} · Apple Silicon）

这是提供给已知可信用户的对外候选包。当前安装包使用本地签名，尚未经过 Apple 开发者签名与公证，因此 macOS 首次打开时仍会显示开发者或安全拦截提示；这是当前分发边界，不代表安装包损坏，也不是免警告正式版。

包信息：Lulu {version} · build {build} · 作者归属：兆基 · 2026
DMG 文件：{dmg_name}

系统要求
1. 使用 Apple Silicon（M1 或后续芯片）的 Mac。
2. macOS 15 或更高版本。
3. 从 Lulu 官方 GitHub 发布页下载 DMG 即可，无需另外下载校验文件。

安装与首次打开
1. 打开 DMG，把 Lulu 拖到“应用程序”。
2. 在“应用程序”中找到 Lulu。
3. 首次启动时按住 Control 点击 Lulu，选择“打开”，再确认一次“打开”。
4. 如果仍被拦截，打开“系统设置”→“隐私与安全性”，找到 Lulu 的提示并点击“仍要打开”。

更新已有 Lulu
4.1.5 及以前的用户，请先完成一次覆盖安装：
1. 从 https://github.com/AidenXu-1/Lulu/releases/latest 重新下载最新版 DMG；不继续安装以前下载好或更新窗口缓存的旧包。
2. 等待任务结束并保存文稿，退出 Lulu。
3. 把新版 Lulu 拖入“应用程序”，选择“替换”，再从“应用程序”打开。
4. 如出现 macOS 授权窗口，请本人处理；确认原有文稿、模型和任务正常显示。
无需先卸载，不删除 Lulu 的应用支持目录、模型目录或钥匙串记录。覆盖只替换程序，资料继续保存在原位置。
完成这次过渡后，后续版本可在 App 内检查更新，下载后确认重启。

隐私说明
- 默认转录在本机完成，不上传音频或逐字稿，也没有遥测。
- 粘贴视频链接时会联网下载公开媒体。
- 只有你主动选择云端能力时，内容才会发送到你选择的云服务。

重要边界
- 当前包尚未经过 Apple 开发者签名与公证，需要用户按上述步骤人工放行。
- 当前包不是免 Gatekeeper 警告的正式签名版本。
"""


def write_exclusive(path: Path, text: str) -> None:
    try:
        parent_mode = path.parent.lstat().st_mode
    except OSError as exc:
        raise OSError("安装说明输出父目录不存在") from exc
    if not stat.S_ISDIR(parent_mode) or path.parent.is_symlink():
        raise OSError("安装说明输出父目录必须是普通目录")
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        data = text.encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build", required=True)
    parser.add_argument("--dmg-name", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.is_symlink():
        raise SystemExit("安装说明输出不能是符号链接")
    try:
        text = render_install_instructions(
            version=args.version,
            build=args.build,
            dmg_name=args.dmg_name,
        )
        write_exclusive(output, text)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
