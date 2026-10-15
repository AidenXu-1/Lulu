from __future__ import annotations

import base64
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import plistlib
import runpy
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from src.core.updater.archive import extract_zip, preflight_zip, validate_staged_app
from src.core.updater.cleanup import cleanup_after_install, cleanup_stale_on_startup
from src.core.updater.install import InstallTransaction, write_ready
from src.core.updater.manifest import decode_signature, parse_manifest, verify_signature
from src.core.updater.models import UpdateError, UpdateSnapshot
from src.core.updater.network import Response, SecureHttpClient, validate_url
from src.core.updater.service import UpdateService
from src.core.updater.state import StateStore
from src.core.updater.secure_io import atomic_json
from src.version import (
    __build__,
    __channel__,
    __version__,
    channel_for_profile,
    identity,
    runtime_channel,
)


def _manifest(zip_sha="a" * 64, zip_size=12):
    version = "0.2.4"; build = "20260712.1"
    return {
        "schema": 1, "app": "Lulu", "channel": "trusted-test", "latest_version": version,
        "latest_build": build, "min_supported_version": "0.2.2", "key_id": "lulu-update-rsa3072-2026-07",
        "mandatory": False, "published_at": "2026-07-11T19:25:00+08:00", "notes_md": "- 本地隔离更新",
        "assets": {
            "macos_arm64": {"kind": "zip", "filename": f"Lulu-{version}-{build}-arm64.zip", "url": f"https://github.com/AidenXu-1/lulu-releases/releases/download/v{version}/Lulu-{version}-{build}-arm64.zip", "sha256": zip_sha, "size": zip_size, "tree_manifest_sha256": "c" * 64},
            "macos_arm64_dmg": {"kind": "dmg", "filename": f"Lulu-{version}-{build}-arm64.dmg", "url": f"https://github.com/AidenXu-1/lulu-releases/releases/download/v{version}/Lulu-{version}-{build}-arm64.dmg", "sha256": "b" * 64, "size": 13},
        },
    }


def _raw_manifest(**kwargs):
    value = _manifest(**kwargs)
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def _zip_info(name, mode, directory=False):
    info = zipfile.ZipInfo(name + ("/" if directory and not name.endswith("/") else ""))
    info.create_system = 3
    info.external_attr = ((stat.S_IFDIR if directory else stat.S_IFREG) | mode) << 16
    if directory: info.external_attr |= 0x10
    return info


def _symlink_info(name, mode=0o755):
    info=zipfile.ZipInfo(name); info.create_system=3; info.external_attr=(stat.S_IFLNK|mode)<<16; return info


def _fixture_zip(path: Path, *, version="0.2.4", build="20260712.1", mutate=None):
    plist = plistlib.dumps({"CFBundleName": "Lulu", "CFBundleExecutable": "Lulu", "CFBundleShortVersionString": version, "CFBundleVersion": build})
    entries = [
        (_zip_info("Lulu.app", 0o755, True), b""),
        (_zip_info("Lulu.app/Contents", 0o755, True), b""),
        (_zip_info("Lulu.app/Contents/MacOS", 0o755, True), b""),
        (_zip_info("Lulu.app/Contents/MacOS/Lulu", 0o755), b"fixture-arm64"),
        (_zip_info("Lulu.app/Contents/Info.plist", 0o644), plist),
    ]
    if mutate: entries = mutate(entries)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for info, data in entries: zf.writestr(info, data)


class FakeClient:
    def __init__(self, manifest, signature): self.payloads=[manifest,signature]
    def open(self, url, **_kwargs): return Response(url,200,{"content-length":str(len(self.payloads[0]))},BytesIO(self.payloads.pop(0)))
    def read_limited(self,response,**_kwargs): return response.stream.read()


class DownloadClient:
    def __init__(self,responses): self.responses=list(responses); self.headers=[]
    def open(self,url,*,headers,**_kwargs): self.headers.append(dict(headers)); status,response_headers,data=self.responses.pop(0); return Response(url,status,response_headers,BytesIO(data))


def _http_wire(body, *, declared_size=None, chunked=False, complete=True, status=200, headers=None):
    size = len(body) if declared_size is None else declared_size
    fields = dict(headers or {})
    if chunked:
        fields['Transfer-Encoding'] = 'chunked'
        body = f'{size:x}\r\n'.encode() + body + (b'\r\n0\r\n\r\n' if complete else b'')
    else:
        fields['Content-Length'] = str(size)
    return f'HTTP/1.1 {status} OK\r\n'.encode() + b''.join(f'{key}: {value}\r\n'.encode() for key,value in fields.items()) + b'\r\n' + body


class HttpFixtureOpener:
    """Exercise the real HTTP parser without sockets, DNS or TLS access."""
    def __init__(self, wires):
        self.wires=list(wires); self.requests=[]; self.responses=[]

    def open(self, request, **_kwargs):
        self.requests.append(request)
        wire=self.wires.pop(0)
        socket=type('Socket',(),{'makefile':lambda *_args,**_kwargs:BytesIO(wire)})()
        response=http.client.HTTPResponse(socket);response.begin()
        self.responses.append(response)
        return response


class AutoUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory(); root=Path(cls.keys.name); cls.private=root/"private.pem"; cls.public=root/"public.pem"
        subprocess.run(["/usr/bin/openssl","genpkey","-algorithm","RSA","-pkeyopt","rsa_keygen_bits:3072","-out",str(cls.private)],check=True,capture_output=True)
        subprocess.run(["/usr/bin/openssl","pkey","-in",str(cls.private),"-pubout","-out",str(cls.public)],check=True,capture_output=True)
        cls.public_pem=cls.public.read_text()
        cls.private_next=root/"private-next.pem"; cls.public_next=root/"public-next.pem"
        subprocess.run(["/usr/bin/openssl","genpkey","-algorithm","RSA","-pkeyopt","rsa_keygen_bits:3072","-out",str(cls.private_next)],check=True,capture_output=True)
        subprocess.run(["/usr/bin/openssl","pkey","-in",str(cls.private_next),"-pubout","-out",str(cls.public_next)],check=True,capture_output=True)
        cls.public_next_pem=cls.public_next.read_text()

    @classmethod
    def tearDownClass(cls): cls.keys.cleanup()

    def sign(self, data, private=None):
        root=Path(self.keys.name); source=root/"manifest.json"; sig=root/"sig.bin"; source.write_bytes(data)
        subprocess.run(["/usr/bin/openssl","dgst","-sha256","-sign",str(private or self.private),"-out",str(sig),str(source)],check=True)
        return base64.b64encode(sig.read_bytes())+b"\n"


    def test_manifest_reader_rejects_truncated_content_length_and_chunked_bodies(self):
        url='https://github.com/AidenXu-1/Lulu/releases/latest/download/version.json'
        for chunked in (False,True):
            for complete in (False,True):
                with self.subTest(chunked=chunked,complete=complete):
                    opener=HttpFixtureOpener([_http_wire(b'ABCDEF' if complete else b'ABC',declared_size=6,chunked=chunked,complete=complete)])
                    client=SecureHttpClient(opener=opener)
                    response=client.open(url,headers={},connect_timeout=1,total_timeout=2)
                    if complete:
                        self.assertEqual(client.read_limited(response,limit=6,total_timeout=2,chunk_size=2),b'ABCDEF')
                    else:
                        with self.assertRaises(UpdateError) as caught:
                            client.read_limited(response,limit=6,total_timeout=2,chunk_size=2)
                        self.assertEqual(caught.exception.code,'network')
                    self.assertTrue(response.stream.closed)

    def test_manifest_reader_normalizes_reset_and_incomplete_read(self):
        class BrokenStream(BytesIO):
            def read(self,_size): raise error
        for error in (ConnectionResetError('fixture'),http.client.IncompleteRead(b'ABC',3)):
            with self.subTest(error=type(error).__name__):
                stream=BrokenStream()
                with self.assertRaises(UpdateError) as caught:
                    SecureHttpClient().read_limited(Response('',200,{},stream),limit=10,total_timeout=2)
                self.assertEqual(caught.exception.code,'network');self.assertTrue(stream.closed)

    def test_manifest_reader_preserves_size_limits_and_supported_framing(self):
        url='https://github.com/AidenXu-1/Lulu/releases/latest/download/version.json'
        cases=(
            ('close_delimited',b'HTTP/1.1 200 OK\r\n\r\nABCDEF',None),
            ('chunked_ignores_length',_http_wire(b'ABCDEF',chunked=True,headers={'Content-Length':'1'}),None),
            ('declared_too_large',_http_wire(b'ABC',declared_size=7),'too_large'),
            ('chunked_too_large',_http_wire(b'ABCDEFG',chunked=True),'too_large'),
            ('close_delimited_too_large',b'HTTP/1.1 200 OK\r\n\r\nABCDEFG','too_large'),
            ('invalid_length',b'HTTP/1.1 200 OK\r\nContent-Length: unknown\r\n\r\nABCDEF','http'),
        )
        for name,wire,error in cases:
            with self.subTest(name=name):
                client=SecureHttpClient(opener=HttpFixtureOpener([wire]))
                response=client.open(url,headers={},connect_timeout=1,total_timeout=2)
                if error:
                    with self.assertRaises(UpdateError) as caught:
                        client.read_limited(response,limit=6,total_timeout=2,chunk_size=2)
                    self.assertEqual(caught.exception.code,error)
                else:
                    self.assertEqual(client.read_limited(response,limit=6,total_timeout=2,chunk_size=2),b'ABCDEF')
                self.assertTrue(response.stream.closed)

    @patch('src.core.updater.service.__version__', '0.2.3')
    @patch('src.core.updater.service.runtime_channel', return_value='trusted-test')
    def test_manifest_and_signature_interruption_reopen_then_resume_verified_download(self,_channel):
        import hashlib
        payload=b'ABCDEF';raw=_raw_manifest(zip_sha=hashlib.sha256(payload).hexdigest(),zip_size=6)
        sig=self.sign(raw);keys={'lulu-update-rsa3072-2026-07':self.public_pem}
        for target in ('manifest','signature'):
            for chunked in (False,True):
                with self.subTest(target=target,chunked=chunked),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp)/'updates'
                    initial=HttpFixtureOpener([_http_wire(raw),_http_wire(sig),_http_wire(payload[:3],declared_size=6,headers={'ETag':'E1'})])
                    service=UpdateService(root=root,client=SecureHttpClient(opener=initial),public_keys=keys)
                    self.assertEqual(service.check(manual=True).ui_state,'update_available')
                    with self.assertRaises(UpdateError): service.download()
                    part=root/'downloads'/(service.manifest.zip_asset.filename+'.part')
                    meta=part.with_name(part.name+'.meta.json')
                    expected_meta=meta.read_bytes()
                    for _ in range(2):
                        body=raw if target=='manifest' else sig
                        interrupted=_http_wire(body[:len(body)//2],declared_size=len(body),chunked=chunked,complete=False)
                        wires=[interrupted,_http_wire(sig)] if target=='manifest' else [_http_wire(raw),interrupted]
                        opener=HttpFixtureOpener(wires)
                        service=UpdateService(root=root,client=SecureHttpClient(opener=opener),public_keys=keys)
                        self.assertEqual(part.read_bytes(),payload[:3])
                        with self.assertRaises(UpdateError) as caught: service.download()
                        self.assertEqual(caught.exception.code,'recheck')
                        self.assertEqual(service.snapshot.ui_state,'download_paused')
                        self.assertEqual(service.snapshot.error_code,'network')
                        self.assertFalse(service.snapshot.verified_manifest);self.assertIsNone(service.manifest)
                        self.assertEqual((service.snapshot.downloaded,service.snapshot.total),(3,6))
                        self.assertEqual(meta.read_bytes(),expected_meta)
                        self.assertEqual(len(opener.requests),1 if target=='manifest' else 2)
                        self.assertTrue(all(response.closed for response in opener.responses))
                    recovered=HttpFixtureOpener([_http_wire(raw),_http_wire(sig),_http_wire(payload[3:],status=206,headers={'ETag':'E1','Content-Range':'bytes 3-5/6'})])
                    service=UpdateService(root=root,client=SecureHttpClient(opener=recovered),public_keys=keys)
                    self.assertEqual(part.read_bytes(),payload[:3])
                    self.assertEqual(service.download().read_bytes(),payload)
                    self.assertEqual(recovered.requests[-1].get_header('Range'),'bytes=3-')
                    self.assertTrue(service.snapshot.verified_manifest)
                    self.assertFalse(part.exists());self.assertFalse(meta.exists())
                    self.assertTrue(all(response.closed for response in recovered.responses))

    @patch('src.core.updater.service.__version__', '0.2.3')
    @patch('src.core.updater.service.runtime_channel', return_value='trusted-test')
    def test_complete_invalid_manifest_or_signature_still_blocks_resumed_download(self,_channel):
        raw=_raw_manifest(zip_size=6);sig=self.sign(raw)
        keys={'lulu-update-rsa3072-2026-07':self.public_pem}
        for target in ('manifest','signature'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'updates'
                service=UpdateService(root=root,client=FakeClient(raw,sig),public_keys=keys)
                self.assertEqual(service.check(manual=True).ui_state,'update_available')
                service.client=DownloadClient([(200,{'etag':'E1','content-length':'6'},b'ABC')])
                with self.assertRaises(UpdateError):service.download()
                body=b'{"broken":' if target=='manifest' else raw
                signature=self.sign(body) if target=='manifest' else base64.b64encode(b'invalid-signature')+b'\n'
                opener=HttpFixtureOpener([_http_wire(body),_http_wire(signature)])
                service=UpdateService(root=root,client=SecureHttpClient(opener=opener),public_keys=keys)
                with self.assertRaises(UpdateError) as caught: service.download()
                self.assertEqual(caught.exception.code,'recheck')
                self.assertEqual(service.snapshot.error_code,target)
                self.assertNotEqual(service.snapshot.ui_state,'download_paused')
                self.assertFalse(service.snapshot.verified_manifest);self.assertIsNone(service.manifest)
                self.assertEqual(len(opener.requests),2)
                self.assertFalse(list((root/'downloads').glob('*.zip')))


    def test_download_timeout_and_read_failure_keep_only_bound_resumable_parts(self):
        payload = b"ABCDEF"
        digest = __import__("hashlib").sha256(payload).hexdigest()
        manifest = parse_manifest(
            _raw_manifest(zip_sha=digest, zip_size=len(payload)),
            local_channel="trusted-test",
        )

        class Stream:
            def __init__(self, chunks):
                self.chunks = list(chunks)
                self.closed = False

            def read(self, _size):
                value = self.chunks.pop(0) if self.chunks else b""
                if isinstance(value, BaseException):
                    raise value
                return value

            def close(self):
                self.closed = True

        class Client:
            def __init__(self, stream):
                self.stream = stream

            def open(self, *_args, **_kwargs):
                return type(
                    "DownloadResponse",
                    (),
                    {
                        "status": 200,
                        "headers": {
                            "content-length": str(len(payload)),
                            "etag": '"fixture"',
                        },
                        "stream": self.stream,
                    },
                )()

        for name, chunks, clock, expected in (
            ("timeout", [b"ABC"], [0.0, 10**9], UpdateError),
            ("read", [b"ABC", OSError("fixture read failure")], None, UpdateError),
            ("invalid", [b"ABC", ValueError("fixture invalid stream")], None, ValueError),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                stream = Stream(chunks)
                service = UpdateService(
                    root=Path(tmp) / "updates",
                    client=Client(stream),
                    public_keys={},
                )
                service.manifest = manifest
                service.snapshot.verified_manifest = True
                context = (
                    patch(
                        "src.core.updater.service.time.monotonic",
                        side_effect=clock,
                    )
                    if clock is not None
                    else patch("src.core.updater.service.time.monotonic", wraps=__import__("time").monotonic)
                )
                with context, self.assertRaises(expected):
                    service.download()
                downloads = service.store.root / "downloads"
                filename = manifest.zip_asset.filename
                if name == 'read':
                    self.assertEqual((downloads / f"{filename}.part").read_bytes(), b"ABC")
                    meta = json.loads((downloads / f"{filename}.part.meta.json").read_text())
                    self.assertEqual(meta['downloaded_bytes'], 3)
                    self.assertEqual(meta['sha256'], digest)
                    self.assertEqual(meta['etag'], '\"fixture\"')
                    self.assertEqual(service.snapshot.ui_state, 'download_paused')
                else:
                    self.assertFalse((downloads / f"{filename}.part").exists())
                    self.assertFalse((downloads / f"{filename}.part.meta.json").exists())
                self.assertFalse((downloads / filename).exists())
                self.assertTrue(stream.closed)

    def test_network_interruption_preserves_progress_and_reopen_resumes_verified_asset(self):
        import hashlib
        payload = b"ABCDEF"
        manifest = parse_manifest(_raw_manifest(zip_sha=hashlib.sha256(payload).hexdigest(), zip_size=len(payload)), local_channel="trusted-test")
        class Stream:
            def __init__(self, error): self.reads = 0; self.error = error; self.closed = False
            def read(self, _size):
                self.reads += 1
                if self.reads == 1: return payload[:3]
                raise self.error
            def close(self): self.closed = True
        for failure in (TimeoutError("offline"), ConnectionResetError("offline")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                stream = Stream(failure)
                client = type('Client', (), {'open': lambda *_args, **_kwargs: Response(manifest.zip_asset.url, 200, {'etag':'E1', 'content-length':'6'}, stream)})()
                service = UpdateService(root=Path(tmp)/'updates', client=client, public_keys={})
                service.manifest = manifest; service.snapshot.verified_manifest = True
                with self.assertRaises(UpdateError): service.download()
                downloads = service.store.root/'downloads'; part = downloads/(manifest.zip_asset.filename+'.part')
                self.assertTrue(stream.closed)
                self.assertEqual(part.read_bytes(), payload[:3])
                self.assertEqual(service.snapshot.ui_state, 'download_paused')
                self.assertEqual(service.snapshot.downloaded, 3)
                resume = DownloadClient([(206, {'etag':'E1','content-range':'bytes 3-5/6'}, payload[3:])])
                reopened = UpdateService(root=service.store.root, client=resume, public_keys={})
                self.assertEqual(part.read_bytes(), payload[:3])
                def recheck(*, manual):
                    self.assertTrue(manual)
                    reopened.manifest = manifest; reopened.snapshot.verified_manifest = True
                    reopened.snapshot.ui_state = 'update_available'
                    return reopened.snapshot
                with patch.object(reopened, 'check', side_effect=recheck) as checked:
                    self.assertEqual(reopened.download().read_bytes(), payload)
                checked.assert_called_once_with(manual=True)
                self.assertEqual(resume.headers[0]['Range'], 'bytes=3-')
                self.assertFalse(part.exists())

    def test_interrupted_cache_is_rejected_when_asset_changes_or_bytes_are_tampered(self):
        import hashlib
        payload=b'ABCDEF'; digest=hashlib.sha256(payload).hexdigest()
        for changed in ('asset','bytes','etag'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                manifest=parse_manifest(_raw_manifest(zip_sha=digest,zip_size=len(payload)),local_channel='trusted-test')
                responses = [(206,{'etag':'E1','content-range':'bytes 3-5/6'},payload[3:])]
                if changed == 'asset': responses = [(200,{'etag':'E1','content-length':'6'},payload)]
                if changed == 'etag': responses = [(206,{'etag':'E2','content-range':'bytes 3-5/6'},payload[3:]),(200,{'etag':'E2','content-length':'6'},payload)]
                client=DownloadClient(responses);service=UpdateService(root=Path(tmp)/'updates',client=client,public_keys={})
                service.manifest=manifest;service.snapshot.verified_manifest=True
                downloads=service.store.root/'downloads';part=downloads/(manifest.zip_asset.filename+'.part');meta=downloads/(manifest.zip_asset.filename+'.part.meta.json')
                part.write_bytes(b'BAD' if changed=='bytes' else payload[:3]);part.chmod(0o600)
                atomic_json(meta, {'url':manifest.zip_asset.url,'version':manifest.latest_version,'build':manifest.latest_build,'sha256':'0'*64 if changed=='asset' else digest,'expected_size':6,'filename':manifest.zip_asset.filename,'etag':'E1','downloaded_bytes':3,'txn':'a'*64})
                if changed == 'bytes':
                    with self.assertRaises(UpdateError) as caught: service.download()
                    self.assertEqual(caught.exception.code,'sha256')
                    self.assertFalse((downloads/manifest.zip_asset.filename).exists())
                else:
                    self.assertEqual(service.download().read_bytes(),payload)
                    self.assertNotIn('Range',client.headers[-1])
                self.assertFalse(part.exists());self.assertFalse(meta.exists())

    def test_resume_connection_failure_keeps_existing_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest=parse_manifest(_raw_manifest(zip_size=6),local_channel='trusted-test')
            client=type('Client',(),{'open':lambda *_args,**_kwargs:(_ for _ in ()).throw(UpdateError('network','offline'))})()
            service=UpdateService(root=Path(tmp)/'updates',client=client,public_keys={});service.manifest=manifest;service.snapshot.verified_manifest=True
            downloads=service.store.root/'downloads';part=downloads/(manifest.zip_asset.filename+'.part');meta=downloads/(manifest.zip_asset.filename+'.part.meta.json')
            part.write_bytes(b'ABC');part.chmod(0o600)
            atomic_json(meta,{'url':manifest.zip_asset.url,'version':manifest.latest_version,'build':manifest.latest_build,'sha256':manifest.zip_asset.sha256,'expected_size':6,'filename':manifest.zip_asset.filename,'etag':'E1','downloaded_bytes':3,'txn':'a'*64})
            for _ in range(2):
                with self.assertRaises(UpdateError): service.download()
                self.assertEqual(part.read_bytes(),b'ABC');self.assertEqual(json.loads(meta.read_text())['downloaded_bytes'],3)
                self.assertEqual(service.snapshot.ui_state,'download_paused')

    def test_incomplete_download_without_etag_does_not_promise_resume(self):
        import hashlib
        payload=b'ABCDEF';manifest=parse_manifest(_raw_manifest(zip_sha=hashlib.sha256(payload).hexdigest(),zip_size=6),local_channel='trusted-test')
        with tempfile.TemporaryDirectory() as tmp:
            service=UpdateService(root=Path(tmp)/'updates',client=DownloadClient([(200,{'content-length':'6'},payload[:3])]),public_keys={})
            service.manifest=manifest;service.snapshot.verified_manifest=True
            with self.assertRaises(UpdateError): service.download()
            self.assertEqual(service.snapshot.ui_state,'download_error')
            self.assertEqual(service.snapshot.downloaded,0)
            self.assertEqual(list((service.store.root/'downloads').iterdir()),[])

    def test_total_download_timeout_preserves_closed_bound_progress(self):
        class SmallReads(BytesIO):
            def read(self, size): return super().read(min(size,3))
        with tempfile.TemporaryDirectory() as tmp:
            stream=SmallReads(b'ABCDEF')
            client=type('Client',(),{'open':lambda *_args,**_kwargs:Response('',200,{'etag':'E1','content-length':'6'},stream)})()
            service=UpdateService(root=Path(tmp)/'updates',client=client,public_keys={})
            service.manifest=parse_manifest(_raw_manifest(zip_size=6),local_channel='trusted-test');service.snapshot.verified_manifest=True
            with patch('src.core.updater.service.time.monotonic',side_effect=[0.,0.,10**9]),self.assertRaises(UpdateError) as caught:
                service.download()
            self.assertEqual(caught.exception.code,'timeout');self.assertTrue(stream.closed)
            self.assertEqual(service.snapshot.ui_state,'download_paused')
            self.assertEqual(service.snapshot.downloaded,3)
            part=service.store.root/'downloads'/(service.manifest.zip_asset.filename+'.part')
            self.assertEqual(part.read_bytes(),b'ABC')

    @patch('src.core.updater.service.__version__', '0.2.3')
    @patch('src.core.updater.service.runtime_channel', return_value='trusted-test')
    def test_paused_download_survives_offline_rechecks_and_repeated_reopens(self, _channel):
        import hashlib
        payload=b'ABCDEF';raw=_raw_manifest(zip_sha=hashlib.sha256(payload).hexdigest(),zip_size=6)
        keys={'lulu-update-rsa3072-2026-07':self.public_pem};sig=self.sign(raw)
        for error in ('network','timeout','http'):
            with self.subTest(error=error),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'updates'
                service=UpdateService(root=root,client=FakeClient(raw,sig),public_keys=keys)
                self.assertEqual(service.check(manual=True).ui_state,'update_available')
                service.client=DownloadClient([(200,{'etag':'E1','content-length':'6'},payload[:3])])
                with self.assertRaises(UpdateError): service.download()
                part=root/'downloads'/(service.manifest.zip_asset.filename+'.part')
                offline=type('Client',(),{'open':lambda *_args,**_kwargs:(_ for _ in ()).throw(UpdateError(error,'offline'))})()
                for _ in range(2):
                    service=UpdateService(root=root,client=offline,public_keys=keys)
                    with self.assertRaises(UpdateError) as caught: service.download()
                    self.assertEqual(caught.exception.code,'recheck')
                    self.assertEqual(service.snapshot.ui_state,'download_paused')
                    self.assertFalse(service.snapshot.verified_manifest)
                    self.assertEqual(part.read_bytes(),payload[:3])
                # Closing while rechecking must preserve the same pending work.
                service._save(ui_state='checking_manual')
                for _ in range(2):
                    service=UpdateService(root=root,client=offline,public_keys=keys)
                    self.assertEqual(service.snapshot.ui_state,'download_paused')
                    self.assertEqual(part.read_bytes(),payload[:3])
                service=UpdateService(root=root,client=FakeClient(raw,sig),public_keys=keys)
                self.assertEqual(service.check(manual=True).ui_state,'download_paused')
                # Quitting after a successful recheck must also retain the partial.
                service=UpdateService(root=root,client=FakeClient(raw,sig),public_keys=keys)
                self.assertEqual(part.read_bytes(),payload[:3])
                self.assertEqual(service.check(manual=True).ui_state,'download_paused')
                client=DownloadClient([(206,{'etag':'E1','content-range':'bytes 3-5/6'},payload[3:])])
                service.client=client
                self.assertEqual(service.download().read_bytes(),payload)
                self.assertEqual(client.headers[0]['Range'],'bytes=3-')

    def test_paused_download_never_bypasses_failed_manifest_verification(self):
        raw=_raw_manifest(zip_size=6)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'updates'
            service=UpdateService(root=root,client=DownloadClient([(200,{'etag':'E1','content-length':'6'},b'ABC')]),public_keys={})
            service.manifest=parse_manifest(raw,local_channel='trusted-test');service.snapshot.verified_manifest=True
            with self.assertRaises(UpdateError): service.download()
            service=UpdateService(root=root,client=FakeClient(raw,base64.b64encode(b'bad')+b'\n'),public_keys={'lulu-update-rsa3072-2026-07':self.public_pem})
            with self.assertRaises(UpdateError) as caught: service.download()
            self.assertEqual(caught.exception.code,'recheck')
            self.assertEqual(service.snapshot.ui_state,'manifest_verify_error')
            self.assertFalse(service.snapshot.verified_manifest)
            self.assertIsNone(service.manifest)
            self.assertFalse(list((root/'downloads').glob('*.zip')))

    def test_cancel_without_bound_progress_requires_redownload(self):
        class SmallReads(BytesIO):
            def read(self,size): return super().read(min(size,3))
        for before_read in (False,True):
            with self.subTest(before_read=before_read),tempfile.TemporaryDirectory() as tmp:
                stream=SmallReads(b'ABCDEF');headers={'content-length':'6'}
                if before_read: headers['etag']='E1'
                client=type('Client',(),{'open':lambda *_args,**_kwargs:Response('',200,headers,stream)})()
                service=UpdateService(root=Path(tmp)/'updates',client=client,public_keys={})
                service.manifest=parse_manifest(_raw_manifest(zip_size=6),local_channel='trusted-test');service.snapshot.verified_manifest=True
                with self.assertRaises(UpdateError) as caught:
                    service.download(cancelled=lambda:before_read or stream.tell()>0)
                self.assertEqual(caught.exception.code,'cancelled')
                self.assertEqual(service.snapshot.ui_state,'download_error')
                self.assertEqual(service.snapshot.downloaded,0)
                self.assertEqual(list((service.store.root/'downloads').iterdir()),[])

    def test_connection_failure_before_first_byte_is_download_error(self):
        client = type('Client', (), {'open': lambda *_args, **_kwargs: (_ for _ in ()).throw(UpdateError('network', 'offline'))})()
        with tempfile.TemporaryDirectory() as tmp:
            service=UpdateService(root=Path(tmp)/'updates', client=client, public_keys={})
            service.manifest=parse_manifest(_raw_manifest(),local_channel='trusted-test');service.snapshot.verified_manifest=True
            with self.assertRaises(UpdateError): service.download()
            self.assertEqual(service.snapshot.ui_state, 'download_error')
            self.assertEqual(list((service.store.root/'downloads').iterdir()), [])


    def _apps(self, root):
        parent=root/"Applications"; parent.mkdir(mode=0o700); target=parent/"Lulu.app"; staged=root/"updates"/"staged"/("a"*64)/"Lulu.app"
        def make(app, marker):
            executable=app/"Contents/MacOS/Lulu"; executable.parent.mkdir(parents=True); executable.write_bytes(b"fixture-arm64"); executable.chmod(0o755)
            (app/"Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleName":"Lulu","CFBundleExecutable":"Lulu","CFBundleShortVersionString":"0.2.4","CFBundleVersion":"20260712.1"}))
            (app/marker).write_text(marker)
        make(target,"old"); make(staged,"new"); return parent,target,staged


    def test_success_cleanup_removes_update_artifacts_but_never_follows_links(self):
        from src.core.updater.cache_cleanup import cleanup_successful_update
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "updates"
            txn = "a" * 64
            downloads = root / "downloads"
            staged = root / "staged" / txn / "Lulu.app"
            downloads.mkdir(parents=True, mode=0o700)
            staged.mkdir(parents=True, mode=0o700)
            (downloads / "archive.zip").write_bytes(b"verified")
            (downloads / "nested").mkdir()
            (downloads / "nested" / "meta.json").write_text("{}")
            (staged / "payload").write_bytes(b"app")
            outside = Path(tmp) / "business-data"
            outside.mkdir()
            marker = outside / "keep.txt"
            marker.write_text("keep")

            receipt = cleanup_successful_update(root, {"txn": txn})

            self.assertEqual(list(downloads.iterdir()), [])
            self.assertFalse((root / "staged" / txn).exists())
            self.assertEqual(marker.read_text(), "keep")
            self.assertEqual(receipt["txn"], txn)
            self.assertTrue(list((root / "logs").glob("cleanup-*.json")))

            victim = outside / "victim.txt"
            victim.write_text("unchanged")
            (downloads / "link").symlink_to(victim)
            with self.assertRaises(UpdateError):
                cleanup_successful_update(root, {"txn": txn})
            self.assertEqual(victim.read_text(), "unchanged")


if __name__ == "__main__": unittest.main()
