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

    def test_failure_code_contract_is_complete_and_stable(self):
        self.assertEqual(
            cover_module.COVER_FAILURE_CODES,
            {
                "cover_invalid_url", "cover_scheme_blocked",
                "cover_credentials_blocked", "cover_port_blocked",
                "cover_dns_failed", "cover_address_blocked",
                "cover_redirect_blocked", "cover_redirect_loop",
                "cover_redirect_limit", "cover_http_status",
                "cover_compression_blocked", "cover_too_large",
                "cover_truncated", "cover_type_mismatch", "cover_malformed",
                "cover_timeout", "cover_cancelled", "cover_path_blocked",
                "cover_busy", "cover_io_failed", "cover_commit_failed",
                "cover_commit_uncertain",
            },
        )

    def assert_no_sidecars(self):
        if not self.root.exists():
            return
        names = [path.name for path in self.root.rglob("*") if path.is_file()]
        self.assertFalse(any(name.endswith((".part", ".lock", ".rollback")) for name in names))

    def test_jpeg_png_webp_success_uses_pinned_target_identity_and_relative_receipt(self):
        for body, mime, extension in (
            (JPEG_A, "image/jpeg", "jpg"),
            (PNG, "image/png; charset=binary", "png"),
            (WEBP, "image/webp", "webp"),
        ):
            with self.subTest(mime=mime):
                root = self.root / extension
                transport = FakeTransport(FakePlan(body=body, mime=mime, chunks=[body[:7], body[7:]]))
                service = CoverLocalizationService(root, transport=transport, resolver=self.resolver)
                receipt = service.localize(
                    platform="douyin", work_id="work", url="https://img.example/a"
                )
                self.assertEqual(receipt.status, "downloaded")
                self.assertTrue(receipt.relative_path.endswith(f".{extension}"))
                self.assertFalse(Path(receipt.relative_path).is_absolute())
                self.assertEqual((root / receipt.relative_path).read_bytes(), body)
                target, call = transport.calls[0]
                self.assertEqual(target.approved_ips, (PUBLIC_V4, PUBLIC_V6))
                self.assertEqual(call["headers"]["Accept-Encoding"], "identity")
                self.assertEqual(call["headers"]["Connection"], "close")

    def test_initial_url_failure_codes_are_strict_and_transport_stays_zero(self):
        cases = {
            "http://cdn.example/a": "cover_scheme_blocked",
            "file:///etc/passwd": "cover_scheme_blocked",
            "data:image/png;base64,AA": "cover_scheme_blocked",
            "ftp://cdn.example/a": "cover_scheme_blocked",
            "https://u:p@cdn.example/a": "cover_credentials_blocked",
            "https://cdn.example:444/a": "cover_port_blocked",
            "https://cdn.example/a%zz": "cover_invalid_url",
            "https://cdn.example/a\\b": "cover_invalid_url",
            "https://cdn.example/a\nX: y": "cover_invalid_url",
            "https://[fe80::1%25en0]/a": "cover_invalid_url",
            "https:///missing": "cover_invalid_url",
        }
        for url, code in cases.items():
            with self.subTest(code=code):
                transport = FakeTransport(FakePlan())
                receipt = self.download(self.service(transport=transport), url=url)
                self.assertEqual(receipt.reason_code, code)
                self.assertEqual(transport.calls, [])

    def test_idna_is_normalized_once_and_default_port_is_443(self):
        resolver = FakeResolver({"xn--fsqu00a.xn--0zwm56d": (PUBLIC_V4,)})
        transport = FakeTransport(FakePlan())
        receipt = self.download(
            self.service(resolver=resolver, transport=transport),
            url="https://例子.测试/cover",
        )
        self.assertEqual(receipt.status, "downloaded")
        target, _call = transport.calls[0]
        self.assertEqual(target.host, "xn--fsqu00a.xn--0zwm56d")
        self.assertEqual(target.port, 443)
        self.assertEqual(resolver.calls, [(target.host, 443)])

    def test_every_non_global_ipv4_ipv6_mapped_and_mixed_answer_is_blocked(self):
        blocked = (
            "127.0.0.1", "10.0.0.1", "169.254.1.1", "0.0.0.0", "224.0.0.1",
            "192.0.2.1", "::1", "fc00::1", "fe80::1", "ff02::1", "::",
            "::ffff:127.0.0.1",
        )
        for address in blocked:
            with self.subTest(address=address):
                resolver = FakeResolver({"cdn.example": (PUBLIC_V4, address)})
                transport = FakeTransport(FakePlan())
                receipt = self.download(self.service(resolver=resolver, transport=transport))
                self.assertEqual(receipt.reason_code, "cover_address_blocked")
                self.assertEqual(transport.calls, [])

    def test_douyin_official_fake_ip_uses_only_explicit_loopback_proxy(self):
        resolver = FakeResolver(
            {"p3-pc-sign.douyinpic.com": ("198.18.0.20",)}
        )
        transport = FakeTransport(FakePlan())
        provider_calls = []

        def proxy_provider():
            provider_calls.append(True)
            return CoverProxyEndpoint("127.0.0.1", 10909)

        receipt = self.download(
            self.service(
                resolver=resolver,
                transport=transport,
                proxy_provider=proxy_provider,
            ),
            platform="douyin",
            work_id="aweme-1",
            url="https://p3-pc-sign.douyinpic.com/tos-cn-i/test",
        )

        self.assertEqual(receipt.status, "downloaded")
        self.assertEqual(provider_calls, [True])
        target, call = transport.calls[0]
        self.assertEqual(target.approved_ips, ())
        self.assertEqual(
            target.proxy, CoverProxyEndpoint("127.0.0.1", 10909)
        )
        self.assertNotIn(
            "Proxy-Authorization",
            call["headers"],
        )

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

    def test_two_first_downloads_can_safely_share_new_managed_directory(self):
        root = self.base / "parallel-covers"
        services = (
            CoverLocalizationService(
                root,
                transport=FakeTransport(FakePlan(body=JPEG_A)),
                resolver=self.resolver,
            ),
            CoverLocalizationService(
                root,
                transport=FakeTransport(FakePlan(body=JPEG_B)),
                resolver=self.resolver,
            ),
        )
        barrier = threading.Barrier(2)
        real_mkdir = cover_module.os.mkdir

        def racing_mkdir(path, mode=0o777, *, dir_fd=None):
            if path == "parallel-covers":
                barrier.wait(timeout=2.0)
            return real_mkdir(path, mode, dir_fd=dir_fd)

        def localize(index):
            return services[index].localize(
                platform="douyin",
                work_id=f"parallel-{index}",
                url="https://cdn.example/cover",
            )

        with patch.object(cover_module.os, "mkdir", side_effect=racing_mkdir):
            with ThreadPoolExecutor(max_workers=2) as executor:
                receipts = tuple(executor.map(localize, (0, 1)))

        self.assertEqual([receipt.status for receipt in receipts], ["downloaded"] * 2)
        files = [path for path in root.rglob("*") if path.is_file()]
        self.assertEqual(sum(path.suffix == ".jpg" for path in files), 2)
        self.assertFalse(
            any(path.name.endswith((".part", ".lock", ".rollback")) for path in files)
        )

    def test_fake_ip_proxy_path_rejects_unapproved_hosts_platforms_and_mixed_answers(self):
        cases = (
            (
                "douyin",
                "https://evil.example/cover",
                ("198.18.0.20",),
            ),
            (
                "xiaohongshu",
                "https://p3-pc-sign.douyinpic.com/cover",
                ("198.18.0.20",),
            ),
            (
                "douyin",
                "https://p3-pc-sign.douyinpic.com/cover",
                ("198.18.0.20", PUBLIC_V4),
            ),
        )
        for platform, url, answers in cases:
            with self.subTest(platform=platform, answers=answers):
                host = urlsplit(url).hostname
                transport = FakeTransport(FakePlan())
                provider_calls = []
                receipt = self.download(
                    self.service(
                        resolver=FakeResolver({host: answers}),
                        transport=transport,
                        proxy_provider=lambda: provider_calls.append(True),
                    ),
                    platform=platform,
                    url=url,
                )
                self.assertEqual(receipt.reason_code, "cover_address_blocked")
                self.assertEqual(transport.calls, [])
                if len(answers) == 1:
                    self.assertEqual(provider_calls, [])

    def test_proxy_bound_redirect_cannot_leave_approved_platform_cdn(self):
        resolver = FakeResolver(
            {
                "p3-pc-sign.douyinpic.com": ("198.18.0.20",),
                "cdn.example": (PUBLIC_V4,),
            }
        )
        transport = FakeTransport(
            FakePlan(
                status=302,
                mime=None,
                headers=[("Location", "https://cdn.example/final")],
            ),
            FakePlan(),
        )
        receipt = self.download(
            self.service(
                resolver=resolver,
                transport=transport,
                proxy_provider=lambda: CoverProxyEndpoint("127.0.0.1", 10909),
            ),
            platform="douyin",
            url="https://p3-pc-sign.douyinpic.com/start",
        )
        self.assertEqual(receipt.reason_code, "cover_redirect_blocked")
        self.assertEqual(len(transport.calls), 1)

    def test_transparent_fake_ip_redirect_cannot_leave_approved_platform_cdn(self):
        resolver = FakeResolver(
            {
                "p3-pc-sign.douyinpic.com": ("198.18.0.20",),
                "cdn.example": (PUBLIC_V4,),
            }
        )
        transport = FakeTransport(
            FakePlan(
                status=302,
                mime=None,
                peer_ip="198.18.0.20",
                headers=[("Location", "https://cdn.example/final")],
            ),
            FakePlan(),
        )
        receipt = self.download(
            self.service(
                resolver=resolver,
                transport=transport,
                proxy_provider=lambda: None,
            ),
            platform="douyin",
            url="https://p3-pc-sign.douyinpic.com/start",
        )
        self.assertEqual(receipt.reason_code, "cover_redirect_blocked")
        self.assertEqual(len(transport.calls), 1)

    def test_macos_proxy_parser_accepts_only_literal_loopback_http_connect_endpoint(self):
        valid = """
        <dictionary> {
          HTTPEnable : 1
          HTTPPort : 10908
          HTTPProxy : 127.0.0.1
          HTTPSEnable : 1
          HTTPSPort : 10909
          HTTPSProxy : 127.0.0.1
        }
        """
        self.assertEqual(
            cover_module._parse_macos_proxy(valid),
            CoverProxyEndpoint("127.0.0.1", 10909),
        )
        for invalid in (
            "HTTPSEnable : 1\nHTTPSPort : 443\nHTTPSProxy : 10.0.0.2",
            "HTTPSEnable : 1\nHTTPSPort : 443\nHTTPSProxy : localhost",
            "HTTPSEnable : 1\nHTTPSPort : 0\nHTTPSProxy : 127.0.0.1",
            "HTTPSEnable : 0\nHTTPSPort : 10909\nHTTPSProxy : 127.0.0.1",
        ):
            with self.subTest(invalid=invalid):
                self.assertIsNone(cover_module._parse_macos_proxy(invalid))

    def test_empty_invalid_and_failed_dns_return_dns_failed(self):
        for answer in ((), ("not-an-ip",), OSError("TOP_SECRET")):
            with self.subTest(answer=type(answer).__name__):
                resolver = FakeResolver({"cdn.example": answer})
                receipt = self.download(self.service(FakePlan(), resolver=resolver))
                self.assertEqual(receipt.reason_code, "cover_dns_failed")

    def test_peer_ip_must_be_global_and_belong_to_current_approved_set(self):
        cases = (
            ("127.0.0.1", "cover_address_blocked"),
            (PUBLIC_V4_ALT, "cover_address_blocked"),
        )
        for peer, code in cases:
            with self.subTest(peer=peer):
                plan = FakePlan(peer_ip=peer)
                receipt = self.download(self.service(plan))
                self.assertEqual(receipt.reason_code, code)
                self.assertFalse(self.root.exists() and any(self.root.rglob("*.part")))

    def test_safe_redirect_re_resolves_each_hop_and_drops_fragment(self):
        resolver = FakeResolver({"one.example": (PUBLIC_V4,), "two.example": (PUBLIC_V6,)})
        transport = FakeTransport(
            FakePlan(status=302, mime=None, headers=[("Location", "https://two.example/final?sig=x#f")]),
            FakePlan(body=PNG, mime="image/png"),
        )
        receipt = self.download(
            self.service(resolver=resolver, transport=transport), url="https://one.example/start"
        )
        self.assertEqual(receipt.status, "downloaded")
        self.assertEqual([call[0].host for call in transport.calls], ["one.example", "two.example"])
        self.assertNotIn("#", transport.calls[1][0].url)
        self.assertEqual(resolver.calls, [("one.example", 443), ("two.example", 443)])

    def test_redirect_downgrade_private_loop_limit_missing_and_duplicate_location_fail(self):
        cases = (
            (
                self.service(FakePlan(status=302, mime=None, headers=[("Location", "http://cdn.example/a")])),
                "cover_redirect_blocked",
            ),
            (
                self.service(
                    FakePlan(status=302, mime=None, headers=[("Location", "https://inside.example/a")]),
                    resolver=FakeResolver({"cdn.example": (PUBLIC_V4,), "inside.example": ("10.0.0.1",)}),
                ),
                "cover_address_blocked",
            ),
            (
                self.service(FakePlan(status=302, mime=None, headers=[("Location", "/cover?token=TOP_SECRET")])),
                "cover_redirect_loop",
            ),
            (
                self.service(FakePlan(status=302, mime=None, headers=[("Location", "/next")]), max_redirects=0),
                "cover_redirect_limit",
            ),
            (self.service(FakePlan(status=302, mime=None, headers=[])), "cover_redirect_blocked"),
            (
                self.service(FakePlan(status=302, mime=None, headers=[("Location", "/a"), ("Location", "/b")])),
                "cover_redirect_blocked",
            ),
        )
        for service, code in cases:
            with self.subTest(code=code):
                self.assertEqual(self.download(service).reason_code, code)

    def test_proxy_netrc_and_caller_credentials_have_no_transport_surface(self):
        observed = []

        def inspect(_target, kwargs):
            observed.append(kwargs)

        plan = FakePlan(on_request=inspect)
        with patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9", "NETRC": "/tmp/evil"},
        ):
            receipt = self.download(self.service(plan))
        self.assertEqual(receipt.status, "downloaded")
        self.assertEqual(set(observed[0]), {"connect_timeout", "read_timeout", "headers"})
        headers = observed[0]["headers"]
        forbidden = {"authorization", "cookie", "host", "forwarded", "range", "proxy-authorization"}
        self.assertFalse(forbidden & {key.casefold() for key in headers})

    def test_only_200_identity_and_single_headers_are_accepted(self):
        cases = (
            (FakePlan(status=204), "cover_http_status"),
            (FakePlan(status=206), "cover_http_status"),
            (FakePlan(status=500), "cover_http_status"),
            (FakePlan(headers=[("Content-Encoding", "gzip")]), "cover_compression_blocked"),
            (FakePlan(headers=[("Content-Encoding", "br"), ("Content-Encoding", "identity")]), "cover_compression_blocked"),
            (FakePlan(mime=None, headers=[("Content-Type", "image/jpeg"), ("Content-Type", "image/png")]), "cover_type_mismatch"),
        )
        for plan, code in cases:
            with self.subTest(code=code):
                self.assertEqual(self.download(self.service(plan)).reason_code, code)

    def test_content_length_is_strict_and_raw_stream_has_hard_limit(self):
        malformed = ("-1", "+1", "01", "1,2", " 12 ", "not-a-number")
        for value in malformed:
            with self.subTest(value=value):
                plan = FakePlan(headers=[("Content-Length", value)])
                self.assertEqual(
                    self.download(self.service(plan)).reason_code, "cover_truncated"
                )
        duplicate = FakePlan(headers=[("Content-Length", "1"), ("Content-Length", "1")])
        self.assertEqual(self.download(self.service(duplicate)).reason_code, "cover_truncated")
        too_large = FakePlan(headers=[("Content-Length", str(len(JPEG_A) + 1))])
        self.assertEqual(
            self.download(self.service(too_large, max_bytes=len(JPEG_A))).reason_code,
            "cover_too_large",
        )
        overflow = FakePlan(headers=[("Content-Length", "9" * 200)])
        self.assertEqual(
            self.download(self.service(overflow)).reason_code,
            "cover_too_large",
        )
        short = FakePlan(body=JPEG_A, headers=[("Content-Length", str(len(JPEG_A) + 1))])
        self.assertEqual(
            self.download(self.service(short, max_bytes=len(JPEG_A) + 10)).reason_code,
            "cover_truncated",
        )
        chunked = FakePlan(body=b"", mime="image/jpeg", chunks=[b"a" * 7, b"b" * 7])
        self.assertEqual(
            self.download(self.service(chunked, max_bytes=10)).reason_code,
            "cover_too_large",
        )

    def test_mime_magic_structure_truncation_and_polyglot_are_rejected(self):
        ihdr = PNG[8:33]
        duplicate_ihdr = PNG[:33] + ihdr + PNG[33:]
        vp8x_only_chunk = b"VP8X" + (10).to_bytes(4, "little") + (b"\x00" * 10)
        vp8x_only = (
            b"RIFF"
            + (4 + len(vp8x_only_chunk)).to_bytes(4, "little")
            + b"WEBP"
            + vp8x_only_chunk
        )
        cases = (
            (FakePlan(body=JPEG_A, mime="image/png"), "cover_type_mismatch"),
            (FakePlan(body=b"<html>", mime="image/jpeg"), "cover_type_mismatch"),
            (FakePlan(body=JPEG_A[:-2], mime="image/jpeg"), "cover_truncated"),
            (FakePlan(body=JPEG_A + b"<script>", mime="image/jpeg"), "cover_malformed"),
            (FakePlan(body=PNG[:-12], mime="image/png"), "cover_malformed"),
            (FakePlan(body=PNG + b"SECOND", mime="image/png"), "cover_malformed"),
            (FakePlan(body=duplicate_ihdr, mime="image/png"), "cover_malformed"),
            (FakePlan(body=WEBP[:-1], mime="image/webp"), "cover_truncated"),
            (FakePlan(body=WEBP + b"SECOND", mime="image/webp"), "cover_truncated"),
            (FakePlan(body=vp8x_only, mime="image/webp"), "cover_malformed"),
        )
        for plan, code in cases:
            with self.subTest(code=code, size=len(plan.body)):
                self.assertEqual(self.download(self.service(plan)).reason_code, code)
                self.assert_no_sidecars()

    def test_dns_connect_read_and_total_deadlines_and_stage_cancellation(self):
        transport = FakeTransport(FakePlan(request_error=TimeoutError("TOP_SECRET")))
        self.assertEqual(
            self.download(self.service(transport=transport)).reason_code,
            "cover_timeout",
        )
        read = FakePlan(read_error=socket.timeout("TOP_SECRET"))
        self.assertEqual(self.download(self.service(read)).reason_code, "cover_timeout")

        resolver_started = threading_event()
        release = threading_event()

        def slow_resolver(_host, _port):
            resolver_started.set()
            release.wait(0.2)
            return (PUBLIC_V4,)

        clock = FakeClock([0.0, 0.0, 31.0])
        receipt = self.download(self.service(FakePlan(), resolver=slow_resolver, clock=clock))
        release.set()
        self.assertEqual(receipt.reason_code, "cover_timeout")

        self.assertEqual(
            self.download(self.service(FakePlan()), is_cancelled=lambda: True).reason_code,
            "cover_cancelled",
        )
        state = {"calls": 0}

        def cancel_midway():
            state["calls"] += 1
            return state["calls"] >= 5

        receipt = self.download(
            self.service(FakePlan(chunks=[JPEG_A[:8], JPEG_A[8:]])),
            is_cancelled=cancel_midway,
        )
        self.assertEqual(receipt.reason_code, "cover_cancelled")
        self.assert_no_sidecars()

    def test_repeat_is_idempotent_and_same_key_replaces_across_types(self):
        first = self.download(self.service(FakePlan(body=JPEG_A)))
        target = self.root / first.relative_path
        inode = target.stat().st_ino
        second = self.download(self.service(FakePlan(body=JPEG_A)))
        self.assertEqual(second.sha256, first.sha256)
        self.assertEqual(target.stat().st_ino, inode)

        third = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(third.status, "downloaded")
        self.assertEqual(target.read_bytes(), JPEG_B)
        png = self.download(self.service(FakePlan(body=PNG, mime="image/png")))
        self.assertTrue(png.relative_path.endswith(".png"))
        self.assertFalse(target.exists())
        self.assertEqual((self.root / png.relative_path).read_bytes(), PNG)
        self.assert_no_sidecars()

    def test_root_and_ancestor_symlinks_are_rejected_before_transport(self):
        external = self.base / "external"
        external.mkdir()
        root_link = self.base / "root-link"
        root_link.symlink_to(external, target_is_directory=True)
        transport = FakeTransport(FakePlan())
        service = CoverLocalizationService(root_link, transport=transport, resolver=self.resolver)
        self.assertEqual(self.download(service).reason_code, "cover_path_blocked")
        self.assertEqual(transport.calls, [])

        parent_link = self.base / "parent-link"
        parent_link.symlink_to(external, target_is_directory=True)
        transport = FakeTransport(FakePlan())
        service = CoverLocalizationService(parent_link / "covers", transport=transport, resolver=self.resolver)
        self.assertEqual(self.download(service).reason_code, "cover_path_blocked")
        self.assertEqual(transport.calls, [])

    def test_lock_target_fifo_directory_symlink_and_hardlink_fail_closed(self):
        platform = self.root / "xiaohongshu"
        platform.mkdir(parents=True)
        digest = self.digest()
        lock = platform / f".{digest}.lock"
        outside = self.base / "outside"
        outside.write_bytes(JPEG_A)
        for kind in ("symlink", "fifo"):
            with self.subTest(lock=kind):
                if kind == "symlink":
                    lock.symlink_to(outside)
                else:
                    os.mkfifo(lock)
                receipt = self.download(self.service(FakePlan()))
                self.assertEqual(receipt.reason_code, "cover_path_blocked")
                self.assertTrue(lock.exists() or lock.is_symlink())
                lock.unlink()

        target = self.target()
        for kind in ("symlink", "directory", "fifo", "hardlink"):
            with self.subTest(target=kind):
                if kind == "symlink":
                    target.symlink_to(outside)
                elif kind == "directory":
                    target.mkdir()
                elif kind == "fifo":
                    os.mkfifo(target)
                else:
                    os.link(outside, target)
                receipt = self.download(self.service(FakePlan(body=JPEG_B)))
                self.assertEqual(receipt.reason_code, "cover_path_blocked")
                self.assertEqual(outside.read_bytes(), JPEG_A)
                if target.is_dir() and not target.is_symlink():
                    target.rmdir()
                else:
                    target.unlink()

    def test_parent_directory_replacement_and_late_target_swap_cannot_escape_dir_fd(self):
        platform = self.root / "xiaohongshu"
        external = self.base / "external"
        external.mkdir()

        def replace_parent():
            moved = self.base / "held-platform"
            platform.rename(moved)
            platform.symlink_to(external, target_is_directory=True)

        receipt = self.download(self.service(FakePlan(on_read=replace_parent)))
        self.assertEqual(receipt.reason_code, "cover_path_blocked")
        self.assertEqual(list(external.iterdir()), [])
        platform.unlink()
        (self.base / "held-platform").rename(platform)

        outside = external / "outside.jpg"
        outside.write_bytes(JPEG_A)
        target = self.target()

        def swap_target():
            target.symlink_to(outside)

        receipt = self.download(self.service(FakePlan(body=JPEG_B, on_read=swap_target)))
        self.assertEqual(receipt.reason_code, "cover_path_blocked")
        self.assertEqual(outside.read_bytes(), JPEG_A)
        self.assertTrue(target.is_symlink())

    def test_cross_process_lock_returns_busy_and_does_not_delete_owner_lock(self):
        platform = self.root / "xiaohongshu"
        platform.mkdir(parents=True)
        lock = platform / f".{self.digest()}.lock"
        lock.write_text("other process", encoding="utf-8")
        transport = FakeTransport(FakePlan())
        receipt = self.download(self.service(transport=transport))
        self.assertEqual(receipt.reason_code, "cover_busy")
        self.assertEqual(lock.read_text("utf-8"), "other process")
        self.assertEqual(transport.calls, [])

    def test_write_replace_fsync_and_post_validation_failures_preserve_old_bytes(self):
        first = self.download(self.service(FakePlan(body=JPEG_A)))
        target = self.root / first.relative_path
        original = target.read_bytes()

        with patch.object(cover_module, "_write_all", side_effect=OSError(errno.ENOSPC, "SECRET")):
            receipt = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(receipt.reason_code, "cover_io_failed")
        self.assertEqual(target.read_bytes(), original)

        real_replace = cover_module.os.replace
        with patch.object(cover_module.os, "replace", side_effect=OSError(errno.EIO, "SECRET")):
            receipt = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(receipt.reason_code, "cover_commit_failed")
        self.assertEqual(target.read_bytes(), original)

        calls = {"count": 0}
        real_fsync = cover_module.os.fsync

        def fail_first_directory(descriptor):
            mode = os.fstat(descriptor).st_mode
            if stat.S_ISDIR(mode):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError(errno.EIO, "SECRET")
            return real_fsync(descriptor)

        with patch.object(cover_module.os, "fsync", side_effect=fail_first_directory):
            receipt = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(receipt.reason_code, "cover_commit_failed")
        self.assertEqual(target.read_bytes(), original)

        real_validate = cover_module._validate_image
        state = {"calls": 0}

        def reject_post(payload):
            state["calls"] += 1
            if state["calls"] == 2:
                raise cover_module._CoverFailure("cover_malformed")
            return real_validate(payload)

        with patch.object(cover_module, "_validate_image", side_effect=reject_post):
            receipt = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(receipt.reason_code, "cover_commit_failed")
        self.assertEqual(target.read_bytes(), original)
        self.assert_no_sidecars()

    def test_first_restore_failure_uses_safe_fallback_and_preserves_old_target(self):
        first = self.download(self.service(FakePlan(body=JPEG_A)))
        self.assertEqual(first.status, "downloaded")
        target = self.root / first.relative_path
        old_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        real_replace = cover_module.os.replace
        calls = {"count": 0}

        def fail_restore(source, destination, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError(errno.EIO, "rollback failed")
            return real_replace(source, destination, **kwargs)

        real_fsync = cover_module.os.fsync
        fsync_calls = {"dirs": 0}

        def fail_commit_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                fsync_calls["dirs"] += 1
                if fsync_calls["dirs"] == 2:
                    raise OSError(errno.EIO, "commit failed")
            return real_fsync(descriptor)

        with patch.object(cover_module.os, "replace", side_effect=fail_restore):
            with patch.object(cover_module.os, "fsync", side_effect=fail_commit_fsync):
                receipt = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(receipt.reason_code, "cover_commit_uncertain")
        self.assertEqual(receipt.status, "failed")
        self.assertTrue(target.is_file())
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), old_hash)
        self.assertEqual(target.read_bytes(), JPEG_A)
        self.assertEqual(calls["count"], 2)
        self.assert_no_sidecars()

    def test_backup_write_and_fsync_failures_leave_old_target_and_no_sidecars(self):
        first = self.download(self.service(FakePlan(body=JPEG_A)))
        target = self.root / first.relative_path
        old_hash = hashlib.sha256(target.read_bytes()).hexdigest()

        real_write = cover_module._write_all
        write_calls = {"count": 0}

        def fail_backup_write(descriptor, payload):
            write_calls["count"] += 1
            if write_calls["count"] == 2:
                raise OSError(errno.ENOSPC, "backup write failed")
            return real_write(descriptor, payload)

        with patch.object(cover_module, "_write_all", side_effect=fail_backup_write):
            receipt = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(receipt.reason_code, "cover_commit_failed")
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), old_hash)
        self.assert_no_sidecars()

        real_fsync = cover_module.os.fsync
        file_fsync_calls = {"count": 0}

        def fail_backup_fsync(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                file_fsync_calls["count"] += 1
                if file_fsync_calls["count"] == 2:
                    raise OSError(errno.EIO, "backup fsync failed")
            return real_fsync(descriptor)

        with patch.object(cover_module.os, "fsync", side_effect=fail_backup_fsync):
            receipt = self.download(self.service(FakePlan(body=JPEG_B)))
        self.assertEqual(receipt.reason_code, "cover_commit_failed")
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), old_hash)
        self.assert_no_sidecars()

    def test_total_restore_failure_keeps_last_recoverable_old_copy(self):
        first = self.download(self.service(FakePlan(body=JPEG_A)))
        target = self.root / first.relative_path
        real_replace = cover_module.os.replace
        replace_calls = {"count": 0}

        def fail_primary_restore(source, destination, **kwargs):
            replace_calls["count"] += 1
            if replace_calls["count"] == 2:
                raise OSError(errno.EIO, "restore replace failed")
            return real_replace(source, destination, **kwargs)

        real_fsync = cover_module.os.fsync
        fsync_calls = {"dirs": 0}

        def fail_commit_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                fsync_calls["dirs"] += 1
                if fsync_calls["dirs"] == 2:
                    raise OSError(errno.EIO, "commit fsync failed")
            return real_fsync(descriptor)

        with patch.object(cover_module.os, "replace", side_effect=fail_primary_restore):
            with patch.object(cover_module.os, "fsync", side_effect=fail_commit_fsync):
                with patch.object(
                    cover_module.os,
                    "ftruncate",
                    side_effect=OSError(errno.EIO, "fallback rewrite failed"),
                ):
                    receipt = self.download(self.service(FakePlan(body=JPEG_B)))

        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.reason_code, "cover_commit_uncertain")
        self.assertTrue(target.is_file())
        old_hash = hashlib.sha256(JPEG_A).hexdigest()
        rollback = target.parent / f".{target.name}.{old_hash}.rollback"
        self.assertTrue(rollback.is_file())
        self.assertEqual(rollback.read_bytes(), JPEG_A)
        self.assertEqual(stat.S_IMODE(rollback.stat().st_mode), 0o600)
        self.assertEqual(rollback.stat().st_nlink, 1)
        self.assertEqual(
            list(target.parent.glob(f".{self.digest()}.*.rollback")),
            [rollback],
        )
        forbidden = tuple(target.parent.glob(f".{self.digest()}.*.part"))
        forbidden += tuple(target.parent.glob(f".{self.digest()}.*.restore"))
        forbidden += tuple(target.parent.glob(f".{self.digest()}.lock"))
        self.assertEqual(forbidden, ())

    def test_startup_recovery_handles_missing_different_and_already_old_target_offline(self):
        for target_payload in (None, JPEG_B, JPEG_A):
            with self.subTest(target=target_payload):
                root = self.base / f"recover-{target_payload is None}-{target_payload == JPEG_A}"
                target, rollback = self.write_rollback(
                    root=root, target_payload=target_payload
                )
                transport = FakeTransport()
                with patch.object(
                    socket, "getaddrinfo", side_effect=AssertionError("system DNS")
                ):
                    with patch.object(
                        socket, "create_connection", side_effect=AssertionError("network")
                    ):
                        service = CoverLocalizationService(
                            root,
                            transport=transport,
                            resolver=FakeResolver(),
                        )
                        receipt = service.localize(
                            platform="xiaohongshu",
                            work_id="note-001",
                            url="https://must-not-be-requested.example/secret",
                        )
                self.assertEqual(len(service.startup_recovery_receipts), 1)
                self.assertEqual(receipt.status, "recovered_previous")
                self.assertEqual(receipt.reason_code, "")
                self.assertEqual(receipt.sha256, hashlib.sha256(JPEG_A).hexdigest())
                self.assertEqual(target.read_bytes(), JPEG_A)
                self.assertFalse(rollback.exists())
                self.assertEqual(transport.calls, [])
                self.assertFalse(
                    any(
                        path.name.endswith((".rollback", ".restore", ".lock"))
                        for path in target.parent.iterdir()
                    )
                )

    def test_next_same_target_operation_recovers_without_transport(self):
        transport = FakeTransport()
        service = CoverLocalizationService(
            self.root, transport=transport, resolver=self.resolver
        )
        target, rollback = self.write_rollback(target_payload=JPEG_B)
        receipt = self.download(service)
        self.assertEqual(receipt.status, "recovered_previous")
        self.assertEqual(target.read_bytes(), JPEG_A)
        self.assertFalse(rollback.exists())
        self.assertEqual(transport.calls, [])

    def test_recovery_failure_is_idempotent_and_keeps_one_exact_rollback(self):
        target, rollback = self.write_rollback(target_payload=JPEG_B)
        real_fsync = cover_module.os.fsync
        calls = {"dirs": 0}

        def fail_after_rollback_delete(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                calls["dirs"] += 1
                if calls["dirs"] == 2:
                    raise OSError(errno.EIO, "recovery directory fsync failed")
            return real_fsync(descriptor)

        with patch.object(
            cover_module.os, "fsync", side_effect=fail_after_rollback_delete
        ):
            first = CoverLocalizationService(
                self.root, transport=FakeTransport(), resolver=self.resolver
            )
        self.assertEqual(
            first.startup_recovery_receipts[0].reason_code,
            "cover_commit_uncertain",
        )
        self.assertEqual(target.read_bytes(), JPEG_A)
        self.assertTrue(rollback.is_file())
        self.assertEqual(rollback.read_bytes(), JPEG_A)

        second = CoverLocalizationService(
            self.root, transport=FakeTransport(), resolver=self.resolver
        )
        self.assertEqual(second.startup_recovery_receipts[0].status, "recovered_previous")
        self.assertEqual(target.read_bytes(), JPEG_A)
        self.assertFalse(rollback.exists())
        self.assertEqual(
            list(target.parent.glob(f".{self.digest()}.*.rollback")), []
        )

    def test_repeated_atomic_recovery_failure_never_renames_or_duplicates_rollback(self):
        target, rollback = self.write_rollback(target_payload=JPEG_B)
        expected_name = rollback.name
        expected_hash = hashlib.sha256(rollback.read_bytes()).hexdigest()
        for _attempt in range(2):
            with patch.object(
                cover_module.os,
                "replace",
                side_effect=OSError(errno.EIO, "recovery replace failed"),
            ):
                service = CoverLocalizationService(
                    self.root, transport=FakeTransport(), resolver=self.resolver
                )
            self.assertEqual(
                service.startup_recovery_receipts[0].reason_code,
                "cover_commit_uncertain",
            )
            remaining = list(
                target.parent.glob(f".{self.digest()}.*.rollback")
            )
            self.assertEqual([path.name for path in remaining], [expected_name])
            self.assertEqual(
                hashlib.sha256(remaining[0].read_bytes()).hexdigest(),
                expected_hash,
            )
            self.assertEqual(target.read_bytes(), JPEG_B)
            self.assertFalse(
                any(
                    path.name.endswith((".restore", ".lock"))
                    for path in target.parent.iterdir()
                )
            )

    def test_unresolved_rollback_blocks_new_write_and_same_key_lock_is_respected(self):
        target, rollback = self.write_rollback(target_payload=JPEG_B, mode=0o644)
        transport = FakeTransport(FakePlan(body=JPEG_B))
        service = CoverLocalizationService(
            self.root, transport=transport, resolver=self.resolver
        )
        receipt = self.download(service)
        self.assertEqual(receipt.reason_code, "cover_commit_uncertain")
        self.assertEqual(receipt.relative_path, "")
        self.assertEqual(target.read_bytes(), JPEG_B)
        self.assertTrue(rollback.exists())
        self.assertEqual(transport.calls, [])

        rollback.chmod(0o600)
        lock = target.parent / f".{self.digest()}.lock"
        lock.write_bytes(b"other process")
        transport = FakeTransport(FakePlan(body=JPEG_B))
        service = CoverLocalizationService(
            self.root, transport=transport, resolver=self.resolver
        )
        receipt = self.download(service)
        self.assertEqual(receipt.reason_code, "cover_busy")
        self.assertTrue(rollback.exists())
        self.assertEqual(lock.read_bytes(), b"other process")
        self.assertEqual(transport.calls, [])

    def test_malformed_rollback_variants_fail_closed_without_cleanup(self):
        variants = (
            "wrong_mode",
            "wrong_hash",
            "symlink",
            "hardlink",
            "multiple",
            "unknown",
        )
        for variant in variants:
            with self.subTest(variant=variant):
                root = self.base / f"malformed-{variant}"
                target, rollback = self.write_rollback(
                    root=root, target_payload=JPEG_B
                )
                if variant == "wrong_mode":
                    rollback.chmod(0o644)
                elif variant == "wrong_hash":
                    rollback.write_bytes(JPEG_B)
                elif variant == "symlink":
                    outside = root / "outside-old.jpg"
                    outside.write_bytes(JPEG_A)
                    rollback.unlink()
                    rollback.symlink_to(outside)
                elif variant == "hardlink":
                    os.link(rollback, rollback.with_suffix(".peer"))
                elif variant == "multiple":
                    second = rollback.with_name(
                        f".{self.digest()}.png."
                        f"{hashlib.sha256(PNG).hexdigest()}.rollback"
                    )
                    second.write_bytes(PNG)
                    second.chmod(0o600)
                elif variant == "unknown":
                    rollback.rename(
                        rollback.with_name(f".{self.digest()}.unknown.rollback")
                    )
                before = {
                    path.name: (path.read_bytes(), stat.S_IMODE(path.lstat().st_mode))
                    for path in target.parent.iterdir()
                    if path.name.endswith(".rollback")
                }
                transport = FakeTransport(FakePlan(body=JPEG_A))
                service = CoverLocalizationService(
                    root, transport=transport, resolver=self.resolver
                )
                receipt = self.download(service)
                self.assertEqual(receipt.reason_code, "cover_commit_uncertain")
                after = {
                    path.name: (path.read_bytes(), stat.S_IMODE(path.lstat().st_mode))
                    for path in target.parent.iterdir()
                    if path.name.endswith(".rollback")
                }
                self.assertEqual(after, before)
                self.assertEqual(target.read_bytes(), JPEG_B)
                self.assertEqual(transport.calls, [])

    def test_errors_are_stable_and_never_expose_query_token_headers_or_absolute_path(self):
        secret = "TOP_SECRET_TOKEN"
        plan = FakePlan(request_error=OSError(f"url=?token={secret} Cookie: {secret}"))
        receipt = self.download(
            self.service(plan), url=f"https://cdn.example/a?token={secret}#fragment={secret}"
        )
        rendered = repr(receipt)
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.reason_code, "cover_io_failed")
        self.assertNotIn(secret, rendered)
        self.assertNotIn(str(self.root), rendered)
        self.assertEqual(receipt.relative_path, "")

    def test_fake_suite_never_uses_system_dns_socket_or_default_transport(self):
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("system DNS")):
            with patch.object(socket, "create_connection", side_effect=AssertionError("network")):
                receipt = self.download(self.service(FakePlan()))
        self.assertEqual(receipt.status, "downloaded")

    def test_part_and_lock_unlinks_each_have_following_directory_fsync(self):
        events = []
        real_unlink = cover_module.os.unlink
        real_fsync = cover_module.os.fsync

        def record_unlink(name, **kwargs):
            events.append(("unlink", str(name)))
            return real_unlink(name, **kwargs)

        def record_fsync(descriptor):
            kind = (
                "dir_fsync"
                if stat.S_ISDIR(os.fstat(descriptor).st_mode)
                else "file_fsync"
            )
            events.append((kind, ""))
            return real_fsync(descriptor)

        with patch.object(cover_module.os, "unlink", side_effect=record_unlink):
            with patch.object(cover_module.os, "fsync", side_effect=record_fsync):
                receipt = self.download(
                    self.service(FakePlan(body=b"not-an-image"))
                )
        self.assertEqual(receipt.reason_code, "cover_type_mismatch")
        relevant = [
            event
            for event in events
            if event[0] == "dir_fsync"
            or (
                event[0] == "unlink"
                and event[1].endswith((".part", ".lock"))
            )
        ]
        self.assertEqual(relevant[0][0], "unlink")
        self.assertTrue(relevant[0][1].endswith(".part"))
        self.assertEqual(relevant[1][0], "dir_fsync")
        self.assertEqual(relevant[2][0], "unlink")
        self.assertTrue(relevant[2][1].endswith(".lock"))
        self.assertEqual(relevant[3][0], "dir_fsync")
        self.assert_no_sidecars()

        events.clear()
        with patch.object(cover_module.os, "unlink", side_effect=record_unlink):
            with patch.object(cover_module.os, "fsync", side_effect=record_fsync):
                receipt = self.download(self.service(FakePlan()))
        self.assertEqual(receipt.status, "downloaded")
        lock_index = next(
            index
            for index, event in enumerate(events)
            if event[0] == "unlink" and event[1].endswith(".lock")
        )
        self.assertEqual(events[lock_index + 1][0], "dir_fsync")
        self.assert_no_sidecars()

    def test_part_and_lock_unlink_or_fsync_failures_override_success_or_primary_failure(self):
        cases = (
            ("part_unlink", b"not-an-image"),
            ("part_fsync", b"not-an-image"),
            ("lock_unlink", JPEG_A),
            ("lock_fsync", JPEG_A),
        )
        for case, body in cases:
            with self.subTest(case=case):
                root = self.base / case
                state = {"last_unlink": "", "failed": False}
                real_unlink = cover_module.os.unlink
                real_fsync = cover_module.os.fsync

                def fail_selected_unlink(name, **kwargs):
                    value = str(name)
                    state["last_unlink"] = value
                    if (
                        not state["failed"]
                        and case.endswith("unlink")
                        and value.endswith(
                            ".part" if case.startswith("part") else ".lock"
                        )
                    ):
                        state["failed"] = True
                        raise OSError(errno.EIO, "sidecar unlink failed")
                    return real_unlink(name, **kwargs)

                def fail_selected_fsync(descriptor):
                    is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
                    suffix = ".part" if case.startswith("part") else ".lock"
                    if (
                        not state["failed"]
                        and case.endswith("fsync")
                        and is_directory
                        and state["last_unlink"].endswith(suffix)
                    ):
                        state["failed"] = True
                        raise OSError(errno.EIO, "sidecar directory fsync failed")
                    return real_fsync(descriptor)

                service = CoverLocalizationService(
                    root,
                    transport=FakeTransport(FakePlan(body=body)),
                    resolver=self.resolver,
                )
                with patch.object(
                    cover_module.os, "unlink", side_effect=fail_selected_unlink
                ):
                    with patch.object(
                        cover_module.os, "fsync", side_effect=fail_selected_fsync
                    ):
                        receipt = self.download(service)
                self.assertTrue(state["failed"])
                self.assertEqual(receipt.status, "failed")
                self.assertEqual(receipt.reason_code, "cover_commit_uncertain")
                self.assertEqual(receipt.relative_path, "")

    def test_cleanup_directory_identity_drift_preserves_path_blocked_receipt(self):
        state = {"platform_fd": None, "drift": False}
        real_unlink = cover_module.os.unlink
        real_fstat = cover_module.os.fstat

        def mark_after_part_unlink(name, **kwargs):
            result = real_unlink(name, **kwargs)
            if str(name).endswith(".part"):
                state["platform_fd"] = kwargs["dir_fd"]
                state["drift"] = True
            return result

        def drift_platform_identity(descriptor):
            info = real_fstat(descriptor)
            if state["drift"] and descriptor == state["platform_fd"]:
                values = list(info)
                values[1] = int(info.st_ino) + 1
                return os.stat_result(values)
            return info

        with patch.object(
            socket, "getaddrinfo", side_effect=AssertionError("system DNS")
        ):
            with patch.object(
                socket, "create_connection", side_effect=AssertionError("network")
            ):
                with patch.object(
                    cover_module.os, "unlink", side_effect=mark_after_part_unlink
                ):
                    with patch.object(
                        cover_module.os, "fstat", side_effect=drift_platform_identity
                    ):
                        receipt = self.download(
                            self.service(FakePlan(body=b"not-an-image"))
                        )
        self.assertTrue(state["drift"])
        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.reason_code, "cover_path_blocked")
        self.assertEqual(receipt.relative_path, "")

    def test_cleanup_failure_never_deletes_last_disaster_rollback(self):
        first = self.download(self.service(FakePlan(body=JPEG_A)))
        target = self.root / first.relative_path
        real_replace = cover_module.os.replace
        replace_calls = {"count": 0}

        def fail_restore(source, destination, **kwargs):
            replace_calls["count"] += 1
            if replace_calls["count"] == 2:
                raise OSError(errno.EIO, "restore replace failed")
            return real_replace(source, destination, **kwargs)

        real_fsync = cover_module.os.fsync
        fsync_calls = {"dirs": 0}

        def fail_commit_fsync(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                fsync_calls["dirs"] += 1
                if fsync_calls["dirs"] == 2:
                    raise OSError(errno.EIO, "commit fsync failed")
            return real_fsync(descriptor)

        real_cleanup = cover_module._unlink_owned_and_sync

        def fail_lock_cleanup(managed, owned):
            if owned.name.endswith(".lock"):
                raise OSError(errno.EIO, "lock cleanup failed")
            return real_cleanup(managed, owned)

        with patch.object(cover_module.os, "replace", side_effect=fail_restore):
            with patch.object(cover_module.os, "fsync", side_effect=fail_commit_fsync):
                with patch.object(
                    cover_module.os,
                    "ftruncate",
                    side_effect=OSError(errno.EIO, "fallback rewrite failed"),
                ):
                    with patch.object(
                        cover_module,
                        "_unlink_owned_and_sync",
                        side_effect=fail_lock_cleanup,
                    ):
                        receipt = self.download(
                            self.service(FakePlan(body=JPEG_B))
                        )

        self.assertEqual(receipt.reason_code, "cover_commit_uncertain")
        old_hash = hashlib.sha256(JPEG_A).hexdigest()
        rollback = target.parent / f".{target.name}.{old_hash}.rollback"
        self.assertTrue(rollback.is_file())
        self.assertEqual(rollback.read_bytes(), JPEG_A)
        self.assertEqual(
            list(target.parent.glob(f".{self.digest()}.*.rollback")),
            [rollback],
        )

    def test_default_transport_connects_approved_ip_with_origin_sni_and_exposes_extra_bytes(self):
        wire = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(JPEG_A)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + JPEG_A
            + b"EXTRA"
        )

        class FakeRaw:
            def close(self):
                pass

        class FakeTls:
            def __init__(self, peer_ip=PUBLIC_V4):
                self.timeout = None
                self.sent = b""
                self.closed = False
                self.peer_ip = peer_ip

            def settimeout(self, value):
                self.timeout = value

            def getpeername(self):
                return (self.peer_ip, 443)

            def sendall(self, payload):
                self.sent += payload

            def makefile(self, _mode):
                return io.BytesIO(wire)

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.server_hostname = ""
                self.tls = FakeTls()

            def wrap_socket(self, raw, *, server_hostname):
                self.server_hostname = server_hostname
                return self.tls

        context = FakeContext()
        target = CoverTarget(
            "https://cdn.example/a",
            "cdn.example",
            443,
            "/a",
            (PUBLIC_V4,),
        )
        with patch.object(socket, "create_connection", return_value=FakeRaw()) as connect:
            response = PinnedHttpsTransport(context=context).request(
                target,
                connect_timeout=5.0,
                read_timeout=10.0,
                headers={"Accept-Encoding": "identity", "Connection": "close"},
            )
        connect.assert_called_once_with((PUBLIC_V4, 443), 5.0)
        self.assertEqual(context.server_hostname, "cdn.example")
        self.assertEqual(response.peer_ip, PUBLIC_V4)
        self.assertIn(b"Host: cdn.example\r\n", context.tls.sent)
        self.assertIn(b"Accept-Encoding: identity\r\n", context.tls.sent)
        self.assertEqual(response.read(len(JPEG_A) + 10), JPEG_A)
        self.assertEqual(response.read(10), b"E")
        response.close()

        rebound = FakeContext()
        rebound.tls = FakeTls(PUBLIC_V4_ALT)
        with patch.object(socket, "create_connection", return_value=FakeRaw()):
            with self.assertRaises(cover_module._CoverFailure) as caught:
                PinnedHttpsTransport(context=rebound).request(
                    target,
                    connect_timeout=5.0,
                    read_timeout=10.0,
                    headers={"Accept-Encoding": "identity", "Connection": "close"},
                )
        self.assertEqual(caught.exception.code, "cover_address_blocked")
        self.assertEqual(rebound.tls.sent, b"")

    def test_default_transport_binds_transparent_fake_ip_and_origin_sni(self):
        fake_ip = "198.18.0.20"
        wire = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(JPEG_A)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + JPEG_A
        )

        class FakeRaw:
            def close(self):
                pass

        class FakeTls:
            def __init__(self, peer_ip=fake_ip):
                self.peer_ip = peer_ip
                self.sent = b""

            def settimeout(self, _value):
                pass

            def getpeername(self):
                return (self.peer_ip, 443)

            def sendall(self, payload):
                self.sent += payload

            def makefile(self, _mode):
                return io.BytesIO(wire)

            def close(self):
                pass

        class FakeContext:
            def __init__(self, peer_ip=fake_ip):
                self.server_hostname = ""
                self.tls = FakeTls(peer_ip)

            def wrap_socket(self, _raw, *, server_hostname):
                self.server_hostname = server_hostname
                return self.tls

        target = CoverTarget(
            "https://p3-pc-sign.douyinpic.com/a",
            "p3-pc-sign.douyinpic.com",
            443,
            "/a",
            (fake_ip,),
            transparent_fake_ip=True,
        )
        context = FakeContext()
        with patch.object(socket, "create_connection", return_value=FakeRaw()) as connect:
            response = PinnedHttpsTransport(context=context).request(
                target,
                connect_timeout=5.0,
                read_timeout=10.0,
                headers={"Accept-Encoding": "identity", "Connection": "close"},
            )

        connect.assert_called_once_with((fake_ip, 443), 5.0)
        self.assertEqual(context.server_hostname, "p3-pc-sign.douyinpic.com")
        self.assertFalse(response.via_proxy)
        response.close()

        rebound = FakeContext("198.18.0.21")
        with patch.object(socket, "create_connection", return_value=FakeRaw()):
            with self.assertRaises(cover_module._CoverFailure) as caught:
                PinnedHttpsTransport(context=rebound).request(
                    target,
                    connect_timeout=5.0,
                    read_timeout=10.0,
                    headers={"Accept-Encoding": "identity", "Connection": "close"},
                )
        self.assertEqual(caught.exception.code, "cover_address_blocked")
        self.assertEqual(rebound.tls.sent, b"")

    def test_default_transport_uses_loopback_connect_tunnel_and_origin_sni(self):
        wire = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(JPEG_A)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + JPEG_A
        )

        class FakeRaw:
            def __init__(self):
                self.sent = b""
                self.timeout = None
                self.closed = False

            def getpeername(self):
                return ("127.0.0.1", 10909)

            def settimeout(self, value):
                self.timeout = value

            def sendall(self, payload):
                self.sent += payload

            def makefile(self, _mode):
                return io.BytesIO(
                    b"HTTP/1.1 200 Connection established\r\n"
                    b"Proxy-Agent: fixture\r\n\r\n"
                )

            def close(self):
                self.closed = True

        class FakeTls:
            def __init__(self):
                self.timeout = None
                self.sent = b""
                self.closed = False

            def settimeout(self, value):
                self.timeout = value

            def getpeername(self):
                return ("127.0.0.1", 10909)

            def sendall(self, payload):
                self.sent += payload

            def makefile(self, _mode):
                return io.BytesIO(wire)

            def close(self):
                self.closed = True

        class FakeContext:
            def __init__(self):
                self.server_hostname = ""
                self.tls = FakeTls()

            def wrap_socket(self, raw, *, server_hostname):
                self.server_hostname = server_hostname
                return self.tls

        raw = FakeRaw()
        context = FakeContext()
        target = CoverTarget(
            "https://p3-pc-sign.douyinpic.com/a",
            "p3-pc-sign.douyinpic.com",
            443,
            "/a",
            (),
            CoverProxyEndpoint("127.0.0.1", 10909),
        )
        with patch.object(socket, "create_connection", return_value=raw) as connect:
            response = PinnedHttpsTransport(context=context).request(
                target,
                connect_timeout=5.0,
                read_timeout=10.0,
                headers={"Accept-Encoding": "identity", "Connection": "close"},
            )

        connect.assert_called_once_with(("127.0.0.1", 10909), 5.0)
        self.assertIn(
            b"CONNECT p3-pc-sign.douyinpic.com:443 HTTP/1.1\r\n",
            raw.sent,
        )
        self.assertNotIn(b"Proxy-Authorization", raw.sent)
        self.assertEqual(
            context.server_hostname, "p3-pc-sign.douyinpic.com"
        )
        self.assertTrue(response.via_proxy)
        self.assertEqual(response.peer_ip, "127.0.0.1")
        self.assertEqual(response.read(len(JPEG_A)), JPEG_A)
        response.close()


def threading_event():
    import threading

    return threading.Event()


if __name__ == "__main__":
    unittest.main()
