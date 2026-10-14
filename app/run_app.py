"""打包用入口:以「导入 src 包」的方式启动,使 src 内的相对导入成立。

(直接把 src/main.py 当脚本跑会让其中的相对导入失效,故用这个 shim。)

自检模式:设环境变量 LULU_SELFTEST=<音频或链接> 时,跳过界面,
headless 跑一遍主链路并打印逐字稿后退出——用于验证打包后的 App 内
torch/funasr/ffmpeg/解析器是否都能正常工作。
"""

import multiprocessing
import os
import sys


def _selftest(source: str) -> int:
    from src.core.pipeline import transcribe_source

    def prog(msg, frac):
        print(f"[selftest] {msg}", file=sys.stderr, flush=True)

    save_dir = os.environ.get("LULU_SAVE_DIR")
    t = transcribe_source(
        source,
        engine_name=os.environ.get("LULU_ENGINE") or None,
        keep_media=bool(save_dir),
        media_dir=save_dir,
        progress=prog,
    )
    print("=== SELFTEST TRANSCRIPT ===")
    print(t.text)
    print(f"=== chars={len(t.text)} engine={t.engine} saved={t.saved_media} ===")
    return 0


def _selfcheck() -> int:
    """验证打包后 requests / keyring 可用、且 keychain 能真实存取(冻结环境)。"""
    import requests  # noqa: F401

    from src.core import secrets

    secrets.set_key("__frozencheck__", "abc123")
    got = secrets.get_key("__frozencheck__")
    secrets.delete_key("__frozencheck__")
    backend = "?"
    try:
        import keyring

        backend = keyring.get_keyring().__class__.__name__
    except Exception as exc:  # noqa: BLE001
        backend = f"导入失败:{exc}"
    try:
        from dashscope.audio.asr import Recognition  # noqa: F401

        dash = "OK"
    except Exception as exc:  # noqa: BLE001
        dash = f"导入失败:{exc}"
    print(f"SELFCHECK requests=OK keyring_backend={backend} dashscope={dash} roundtrip={got}")
    return 0


def main() -> int:
    if os.environ.get("LULU_SELFCHECK"):
        return _selfcheck()
    st = os.environ.get("LULU_SELFTEST")
    if st:
        return _selftest(st)
    from src.main import main as gui_main

    return gui_main()


if __name__ == "__main__":
    # 打包(PyInstaller)后,torch/funasr 用 spawn 起子进程会重新执行本入口。
    # freeze_support() 必须是 __main__ 里的第一句:它在子进程里拦截并接管,
    # 不再往下走到 main(),否则每个子进程都会重启整个 App(GUI 下不停弹新窗口)。
    multiprocessing.freeze_support()
    raise SystemExit(main())
