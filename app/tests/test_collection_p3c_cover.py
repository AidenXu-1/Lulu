"""P3-C 封面安全下载内核：纯 Fake transport / resolver / clock。"""

from __future__ import annotations

import errno
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import zlib

from src.core.collect import cover as cover_module
from src.core.collect.cover import (
    CoverLocalizationService,
    CoverProxyEndpoint,
    CoverTarget,
    CoverTransportResponse,
    PinnedHttpsTransport,
)


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_ALT = "1.1.1.1"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        len(data).to_bytes(4, "big")
        + kind
        + data
        + (zlib.crc32(kind + data) & 0xFFFFFFFF).to_bytes(4, "big")
    )


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + png_chunk(b"IHDR", b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00")
    + png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
    + png_chunk(b"IEND", b"")
)
WEBP_PAYLOAD = b"\x00\x00\x00\x9d\x01\x2a\x01\x00\x01\x00"
WEBP_CHUNK = b"VP8 " + len(WEBP_PAYLOAD).to_bytes(4, "little") + WEBP_PAYLOAD
WEBP = b"RIFF" + (4 + len(WEBP_CHUNK)).to_bytes(4, "little") + b"WEBP" + WEBP_CHUNK
JPEG_A = (
    b"\xff\xd8"
    + b"\xff\xc0\x00\x0b" + b"\x08\x00\x01\x00\x01\x01\x01\x11\x00"
    + b"\xff\xda\x00\x08" + b"\x01\x01\x00\x00\x3f\x00"
    + b"abc\xff\x00def\xff\xd9"
)
JPEG_B = JPEG_A.replace(b"abc", b"xyz")


class FakePlan:
    def __init__(
        self,
        *,
        status: int = 200,
        body: bytes = JPEG_A,
        mime: str | None = "image/jpeg",
        headers: list[tuple[str, str]] | None = None,
        peer_ip: str | None = None,
        read_error: BaseException | None = None,
        request_error: BaseException | None = None,
        on_request=None,
        on_read=None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = list(headers or [])
        if mime is not None:
            self.headers.append(("Content-Type", mime))
        self.peer_ip = peer_ip
        self.read_error = read_error
        self.request_error = request_error
        self.on_request = on_request
        self.on_read = on_read
        self.chunks = list(chunks) if chunks is not None else None
        self.closed = False


class FakeTransport:
    def __init__(self, *plans: FakePlan) -> None:
        self.plans = list(plans)
        self.calls = []

    def request(self, target, **kwargs):
        if not self.plans:
            raise AssertionError("unexpected transport request")
        plan = self.plans.pop(0)
        self.calls.append((target, kwargs))
        if plan.on_request is not None:
            plan.on_request(target, kwargs)
        if plan.request_error is not None:
            raise plan.request_error
        chunks = plan.chunks if plan.chunks is not None else [plan.body]
        state = {"index": 0, "offset": 0, "called": False, "timeout": None}

        def read(size: int) -> bytes:
            if not state["called"]:
                state["called"] = True
                if plan.on_read is not None:
                    plan.on_read()
            if plan.read_error is not None:
                raise plan.read_error
            if state["index"] >= len(chunks):
                return b""
            chunk = chunks[state["index"]]
            remaining = chunk[state["offset"]:]
            output = remaining[:size]
            state["offset"] += len(output)
            if state["offset"] >= len(chunk):
                state["index"] += 1
                state["offset"] = 0
            return output

        def close():
            plan.closed = True

        return CoverTransportResponse(
            status_code=plan.status,
            headers=plan.headers,
            peer_ip=plan.peer_ip or (
                target.proxy.host if target.proxy is not None else target.approved_ips[0]
            ),
            reader=read,
            closer=close,
            timeout_setter=lambda value: state.update(timeout=value),
            via_proxy=target.proxy is not None,
        )


class FakeResolver:
    def __init__(self, mapping=None) -> None:
        self.mapping = dict(mapping or {})
        self.calls = []

    def __call__(self, host: str, port: int):
        self.calls.append((host, port))
        value = self.mapping.get(host, (PUBLIC_V4, PUBLIC_V6))
        if isinstance(value, BaseException):
            raise value
        return value


class FakeClock:
    def __init__(self, values: list[float]) -> None:
        self.values = list(values)
        self.current = self.values[0] if self.values else 0.0

    def __call__(self):
        if self.values:
            self.current = self.values.pop(0)
        return self.current


class CoverLocalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "covers"
        self.resolver = FakeResolver()

    def tearDown(self):
        self.temp.cleanup()

    def service(self, *plans, resolver=None, transport=None, **limits):
        return CoverLocalizationService(
            self.root,
            transport=transport or FakeTransport(*plans),
            resolver=resolver or self.resolver,
            **limits,
        )

    def download(self, service, **changes):
        values = {
            "platform": "xiaohongshu",
            "work_id": "note-001",
            "url": "https://cdn.example/cover?token=TOP_SECRET#fragment",
        }
        values.update(changes)
        return service.localize(**values)

    @staticmethod
    def digest(platform="xiaohongshu", work_id="note-001"):
        return hashlib.sha256(f"{platform}:{work_id}".encode()).hexdigest()[:32]

    def target(self, extension="jpg"):
        return self.root / "xiaohongshu" / f"{self.digest()}.{extension}"

    def write_rollback(
        self,
        *,
        root=None,
        payload=JPEG_A,
        target_payload=None,
        mode=0o600,
        name=None,
    ):
        managed_root = Path(root or self.root)
        platform = managed_root / "xiaohongshu"
        platform.mkdir(parents=True, exist_ok=True)
        target = platform / f"{self.digest()}.jpg"
        if target_payload is not None:
            target.write_bytes(target_payload)
        old_hash = hashlib.sha256(payload).hexdigest()
        rollback = platform / (
            name or f".{target.name}.{old_hash}.rollback"
        )
        rollback.write_bytes(payload)
        rollback.chmod(mode)
        return target, rollback


    def assert_no_sidecars(self):
        if not self.root.exists():
            return
        names = [path.name for path in self.root.rglob("*") if path.is_file()]
        self.assertFalse(any(name.endswith((".part", ".lock", ".rollback")) for name in names))


    def test_douyin_official_fake_ip_without_system_proxy_uses_verified_transparent_tls(self):
        fake_ip = "198.18.0.20"
        resolver = FakeResolver(
            {"p3-pc-sign.douyinpic.com": (fake_ip,)}
        )
        transport = FakeTransport(FakePlan(peer_ip=fake_ip))
        receipt = self.download(
            self.service(
                resolver=resolver,
                transport=transport,
                proxy_provider=lambda: None,
            ),
            platform="douyin",
            work_id="7663085744302476579",
            url="https://p3-pc-sign.douyinpic.com/tos-cn-i/test",
        )

        self.assertEqual(receipt.status, "downloaded")
        target, _call = transport.calls[0]
        self.assertEqual(target.approved_ips, (fake_ip,))
        self.assertIsNone(target.proxy)
        self.assertTrue(target.transparent_fake_ip)

    def test_supported_platform_official_fake_ip_uses_verified_transparent_tls(self):
        cases = (
            ("xiaohongshu", "https://sns-webpic-qc.xhscdn.com/a.webp"),
            ("bilibili", "https://i2.hdslb.com/bfs/archive/a.jpg"),
            ("youtube", "https://i.ytimg.com/vi/yt-1/maxresdefault.jpg"),
        )
        for platform, url in cases:
            with self.subTest(platform=platform):
                fake_ip = "198.18.0.20"
                host = urlsplit(url).hostname
                transport = FakeTransport(FakePlan(peer_ip=fake_ip))
                receipt = self.download(
                    self.service(
                        resolver=FakeResolver({host: (fake_ip,)}),
                        transport=transport,
                        proxy_provider=lambda: None,
                    ),
                    platform=platform,
                    work_id=f"{platform}-1",
                    url=url,
                )

                self.assertEqual(receipt.status, "downloaded")
                target, _call = transport.calls[0]
                self.assertEqual(target.approved_ips, (fake_ip,))
                self.assertTrue(target.transparent_fake_ip)


def threading_event():
    import threading

    return threading.Event()


if __name__ == "__main__":
    unittest.main()
