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

    def test_version_truth(self):
        self.assertRegex(__version__, r'^\d+\.\d+\.\d+$')
        self.assertRegex(__build__, r'^\d{8}\.\d+$')
        root=Path(__file__).resolve().parents[1]
        self.assertEqual(subprocess.run([sys.executable,str(root/"packaging/version_value.py"),"version"],capture_output=True,text=True,check=True).stdout.strip(),__version__)
        self.assertEqual(subprocess.run([sys.executable,str(root/"packaging/version_value.py"),"profile-channel","internal_test"],capture_output=True,text=True,check=True).stdout.strip(),"internal-test")
        self.assertEqual(subprocess.run([sys.executable,str(root/"packaging/version_value.py"),"profile-channel","external_candidate"],capture_output=True,text=True,check=True).stdout.strip(),"production")
        self.assertEqual(subprocess.run([sys.executable,str(root/"packaging/version_value.py"),"profile-channel","trusted_test"],capture_output=True,text=True,check=True).stdout.strip(),"trusted-test")

    def test_profile_channels_are_sealed_and_bidirectionally_isolated(self):
        from src.core.license._service_core import app_version

        self.assertEqual(channel_for_profile("internal_test"), "internal-test")
        self.assertEqual(channel_for_profile("external_candidate"), "production")
        self.assertEqual(channel_for_profile("trusted_test"), "trusted-test")
        with self.assertRaises(RuntimeError):
            channel_for_profile("production")

        identities = {
            "internal_test": {
                "app_name": "Lulu Internal Test",
                "bundle_id": "com.lulu.transcribe.internal-test",
                "artifact_prefix": "Lulu-Internal-Test",
                "upload_target": "none",
                "entry": "run_internal.py",
            },
            "external_candidate": {
                "app_name": "Lulu",
                "bundle_id": "com.lulu.transcribe",
                "artifact_prefix": "Lulu",
                "upload_target": "github-release-candidate",
                "entry": "run_external.py",
            },
            "trusted_test": {
                "app_name": "Lulu Trusted Test",
                "bundle_id": "com.lulu.transcribe.trusted-test",
                "artifact_prefix": "Lulu-Trusted-Test",
                "upload_target": "none",
                "entry": "run_trusted_test.py",
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            contents = Path(temp) / "Lulu.app/Contents"
            executable = contents / "MacOS/Lulu"
            resources = contents / "Resources"
            executable.parent.mkdir(parents=True)
            resources.mkdir()
            executable.write_bytes(b"fixture")
            profile_path = resources / "build_profile.json"
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(executable)
            ):
                for profile, expected in (
                    ("internal_test", "internal-test"),
                    ("external_candidate", "production"),
                    ("trusted_test", "trusted-test"),
                ):
                    payload = {"schema": 1, "profile": profile, **identities[profile]}
                    profile_path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(runtime_channel(), expected)
                    self.assertEqual(identity()["channel"], expected)
                    self.assertTrue(app_version().endswith(f".{expected}"))

                polluted = {"schema": 1, "profile": "external_candidate", **identities["external_candidate"]}
                polluted["bundle_id"] = identities["trusted_test"]["bundle_id"]
                profile_path.write_text(json.dumps(polluted), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    runtime_channel()

                polluted = {"schema": 1, "profile": "trusted_test", **identities["trusted_test"], "channel": "production"}
                profile_path.write_text(json.dumps(polluted), encoding="utf-8")
                with self.assertRaises(RuntimeError):
                    runtime_channel()

                profile_path.unlink()
                outside = Path(temp) / "outside.json"
                outside.write_text(json.dumps({"schema": 1, "profile": "external_candidate", **identities["external_candidate"]}), encoding="utf-8")
                profile_path.symlink_to(outside)
                with self.assertRaises(RuntimeError):
                    runtime_channel()

    @patch('src.core.updater.service.__version__', '0.3.1')
    @patch('src.core.updater.service.__build__', '20260815.3')
    def test_production_updater_accepts_only_production_signed_manifest(self):
        for channel, expected_ui in (
            ("production", "update_available"),
            ("trusted-test", "check_error"),
        ):
            payload = _manifest() | {
                "channel": channel,
                "latest_version": "0.3.2",
                "latest_build": "20260815.1",
            }
            for key, kind, extension in (
                ("macos_arm64", "zip", "zip"),
                ("macos_arm64_dmg", "dmg", "dmg"),
            ):
                name = f"Lulu-0.3.2-20260815.1-arm64.{extension}"
                payload["assets"][key]["kind"] = kind
                payload["assets"][key]["filename"] = name
                payload["assets"][key]["url"] = f"https://github.com/AidenXu-1/lulu-releases/releases/download/v0.3.2/{name}"
            raw = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as temp, patch(
                "src.core.updater.service.runtime_channel",
                return_value="production",
            ):
                service = UpdateService(
                    root=Path(temp),
                    client=FakeClient(raw, self.sign(raw)),
                    public_keys={"lulu-update-rsa3072-2026-07": self.public_pem},
                )
                snapshot = service.check(manual=True)
                self.assertEqual(snapshot.ui_state, expected_ui)
                if channel != "production":
                    self.assertFalse(snapshot.verified_manifest)
                    self.assertEqual(snapshot.error_code, "manifest")

    def test_update_release_channel_is_profile_derived(self):
        root = Path(__file__).resolve().parents[1]
        tool = runpy.run_path(str(root / "packaging/make_update_release.py"))
        channel = tool["update_release_channel"]
        self.assertEqual(
            channel({"profile": "external_candidate", "upload_target": "github-release-candidate"}),
            "production",
        )
        with self.assertRaises(SystemExit):
            channel({"profile": "trusted_test", "upload_target": "none"})
        with self.assertRaises(SystemExit):
            channel({"profile": "external_candidate", "upload_target": "github-release-candidate", "channel": "trusted-test"})

    def test_four_public_assets_still_verify_download_and_stage(self):
        import shutil
        from src.core.updater.archive import app_file_manifest

        root = Path(__file__).resolve().parents[1]
        tool = runpy.run_path(str(root / 'packaging/make_update_release.py'))
        main = tool['main']
        real_run = subprocess.run

        def run(args, **kwargs):
            if args[0] == '/usr/bin/codesign':
                return subprocess.CompletedProcess(args, 0)
            return real_run(args, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            app = temp / 'Lulu.app'
            (app / 'Contents/MacOS').mkdir(parents=True)
            exe = app / 'Contents/MacOS/Lulu'
            exe.write_bytes(b'isolated-package-fixture')
            exe.chmod(0o755)
            (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({
                'CFBundleName': 'Lulu', 'CFBundleExecutable': 'Lulu',
                'CFBundleShortVersionString': __version__, 'CFBundleVersion': __build__,
            }))
            dmg = temp / 'fixture.dmg'
            dmg.write_bytes(b'isolated-dmg-fixture')
            notes = temp / 'notes.md'
            notes.write_text('本版更新说明', encoding='utf-8')
            out = temp / 'assets'
            self.private.chmod(0o600)
            argv = ['make_update_release.py', '--app', str(app), '--dmg', str(dmg),
                    '--output', str(out), '--private-key', str(self.private),
                    '--notes', str(notes), '--repository', 'AidenXu-1/Lulu']
            with patch.dict(main.__globals__, {
                'PROFILE_GATE': {'read_app_profile': lambda _: {
                    'profile': 'external_candidate', 'upload_target': 'github-release-candidate'}},
                'prepare_update_app': lambda source, target: shutil.copytree(source, target),
            }), patch.object(sys, 'argv', argv), patch.object(subprocess, 'run', side_effect=run):
                main()
            stem = f'Lulu-{__version__}-{__build__}-arm64'
            self.assertEqual({p.name for p in out.iterdir()}, {
                stem + '.dmg', stem + '.zip', 'version.json', 'version.json.sig'})
            raw = (out / 'version.json').read_bytes()
            sig = (out / 'version.json.sig').read_bytes()
            with patch('src.core.updater.service.runtime_channel', return_value='production'):
                service = UpdateService(root=temp / 'updates', client=FakeClient(raw, sig),
                    public_keys={'lulu-update-rsa3072-2026-07': self.public_pem})
                snapshot = service.check(manual=True)
            self.assertTrue(snapshot.verified_manifest)
            self.assertEqual(snapshot.notes, '本版更新说明')
            payload = (out / (stem + '.zip')).read_bytes()
            service.client = DownloadClient([(200, {'content-length': str(len(payload))}, payload)])
            downloaded = service.download()
            staged = service.stage(downloaded, verify_system=False)
            self.assertEqual(app_file_manifest(staged), app_file_manifest(app))
            evidence = temp / '.assets-release-evidence'
            self.assertEqual((evidence / 'release-notes.md').read_text(), '本版更新说明')
            self.assertTrue((evidence / 'tree-manifest.json').is_file())
            self.assertTrue((evidence / (stem + '.zip.sha256')).is_file())

    def test_install_instructions_are_exact_generated_and_exclusive(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "packaging/make_install_instructions.py"
        tool = runpy.run_path(str(script))
        from src.version import __version__, __build__
        dmg_name = f"Lulu-{__version__}-{__build__}-arm64.dmg"
        rendered = tool["render_install_instructions"](
            version=__version__,
            build=__build__,
            dmg_name=dmg_name,
        )
        self.assertEqual(
            rendered,
            (root / "packaging/安装说明.txt").read_text(encoding="utf-8"),
        )
        for required in (
            dmg_name,
            "无需另外下载校验文件",
            "App 内检查更新",
            "macOS 15",
            "Apple 开发者签名与公证",
            "Control",
            "隐私与安全性",
            "不是免警告正式版",
        ):
            self.assertIn(required, rendered)
        self.assertNotIn("0.2.2-rc-p1", rendered)
        with self.assertRaises(ValueError):
            tool["render_install_instructions"](
                version="0.2.15",
                build="20260809.2",
                dmg_name="Lulu-0.2.15-arm64.dmg",
            )
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "安装说明.txt"
            argv = [
                sys.executable,
                str(script),
                "--output",
                str(output),
                "--version",
                __version__,
                "--build",
                __build__,
                "--dmg-name",
                dmg_name,
            ]
            subprocess.run(argv, check=True, capture_output=True, text=True)
            first = output.read_bytes()
            second = subprocess.run(argv, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(output.read_bytes(), first)

    def test_release_tool_missing_private_key_fails_closed(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            temp=Path(tmp); app=temp/"Lulu.app"; app.mkdir(); dmg=temp/"x.dmg"; dmg.write_bytes(b"x"); notes=temp/"notes.md"; notes.write_text("fixture")
            result=subprocess.run([sys.executable,str(root/"packaging/make_update_release.py"),"--app",str(app),"--dmg",str(dmg),"--output",str(temp/"out"),"--private-key","","--notes",str(notes),"--repository","AidenXu-1/Lulu"],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0); self.assertIn("项目外显式路径",result.stderr+result.stdout)

    def test_signature_and_strict_manifest(self):
        raw=_raw_manifest(); sig=self.sign(raw)
        self.assertEqual(verify_signature(raw,sig,{"lulu-update-rsa3072-2026-07":self.public_pem}),"lulu-update-rsa3072-2026-07")
        parsed=parse_manifest(raw,local_channel="trusted-test"); self.assertEqual(parsed.latest_version,"0.2.4")
        with self.assertRaises(UpdateError): verify_signature(raw+b" ",sig,{"lulu-update-rsa3072-2026-07":self.public_pem})
        value=_manifest(); value["unknown"]=1
        with self.assertRaises(UpdateError): parse_manifest(json.dumps(value).encode(),local_channel="trusted-test")
        unknown=_manifest(); unknown["key_id"]="unknown-key"; unknown_raw=(json.dumps(unknown,separators=(",",":"))+"\n").encode()
        self.assertEqual(verify_signature(unknown_raw,self.sign(unknown_raw),{"lulu-update-rsa3072-2026-07":self.public_pem}),"lulu-update-rsa3072-2026-07")
        self.assertEqual(parse_manifest(unknown_raw,local_channel="trusted-test",verified_key_id="lulu-update-rsa3072-2026-07").key_id,"lulu-update-rsa3072-2026-07")
        rotated=_manifest(); rotated["key_id"]="lulu-update-rsa3072-next"; rotated_raw=(json.dumps(rotated,separators=(",",":"))+"\n").encode(); keys={"lulu-update-rsa3072-2026-07":self.public_pem,"lulu-update-rsa3072-next":self.public_next_pem}
        self.assertEqual(verify_signature(rotated_raw,self.sign(rotated_raw,self.private_next),keys),"lulu-update-rsa3072-next")
        self.assertEqual(verify_signature(rotated_raw,self.sign(rotated_raw,self.private),keys),"lulu-update-rsa3072-2026-07")
        with self.assertRaises(UpdateError): parse_manifest(b" "*131073,local_channel="trusted-test")

    def test_signature_encoding_rejections(self):
        for value in (b"a\nb\n",b"@@@",b"YQ==junk",b"a"*16385):
            with self.subTest(value=value[:8]),self.assertRaises(UpdateError): decode_signature(value)

    def test_url_rejections(self):
        for repository in ("AidenXu-1/Lulu", "AidenXu-1/lulu-releases"):
            valid=f"https://github.com/{repository}/releases/latest/download/version.json"
            self.assertEqual(validate_url(valid,initial=True).hostname,"github.com")
        for url in ("http://github.com/x","https://user@github.com/x","https://github.com:444/x","https://evil.example/x","https://github.com/AidenXu-1/lulu-releases/releases/download/v0.2.4/version.json","https://github.com/AidenXu-1/lulu/releases/latest/download/version.json","https://github.com/other/Lulu/releases/latest/download/version.json"):
            with self.subTest(url=url),self.assertRaises(UpdateError): validate_url(url,initial=True)

    def test_latest_manifest_allows_only_exact_tagged_manifest_redirects(self):
        for repository in ("AidenXu-1/Lulu", "AidenXu-1/lulu-releases"):
            base=f"https://github.com/{repository}/releases/download/v0.2.4/"
            self.assertEqual(validate_url(base+"version.json").path.rsplit("/",1)[-1],"version.json")
            self.assertEqual(validate_url(base+"version.json.sig").path.rsplit("/",1)[-1],"version.json.sig")
            for url in (base+"version.json.bak",base+"notes.md",base+"version.json?download=1",base.replace("v0.2.4","v0.2" )+"version.json"):
                with self.subTest(url=url),self.assertRaises(UpdateError): validate_url(url)

    def test_manifest_accepts_only_canonical_and_legacy_release_assets(self):
        for repository in ("AidenXu-1/Lulu", "AidenXu-1/lulu-releases"):
            value = _manifest()
            for key in ("macos_arm64", "macos_arm64_dmg"):
                value["assets"][key]["url"] = value["assets"][key]["url"].replace(
                    "AidenXu-1/lulu-releases", repository
                )
            parsed = parse_manifest(
                (json.dumps(value, separators=(",", ":")) + "\n").encode(),
                local_channel="trusted-test",
            )
            self.assertIn(repository, parsed.zip_asset.url)
        value = _manifest()
        value["assets"]["macos_arm64"]["url"] = value["assets"]["macos_arm64"]["url"].replace(
            "AidenXu-1/lulu-releases", "AidenXu-1/Lulu-malicious"
        )
        with self.assertRaises(UpdateError):
            parse_manifest(
                (json.dumps(value, separators=(",", ":")) + "\n").encode(),
                local_channel="trusted-test",
            )

    def test_redirect_hop_matrix(self):
        class Headers(dict):
            def items(self): return super().items()
        class Raw:
            def __init__(self,status,location=None): self.status=status; self.headers=Headers({} if not location else {"Location":location})
            def close(self): pass
        class Opener:
            def __init__(self,items): self.items=list(items)
            def open(self,_request,timeout):
                item=self.items.pop(0)
                if isinstance(item,Exception): raise item
                return item
        initial="https://github.com/AidenXu-1/lulu-releases/releases/latest/download/version.json"
        allowed="https://objects.githubusercontent.com/object"
        self.assertEqual(SecureHttpClient(opener=Opener([Raw(302,allowed),Raw(200)])).open(initial,headers={},connect_timeout=1,total_timeout=2).url,allowed)
        tagged="https://github.com/AidenXu-1/lulu-releases/releases/download/v0.2.4/version.json"
        self.assertEqual(SecureHttpClient(opener=Opener([Raw(302,tagged),Raw(200)])).open(initial,headers={},connect_timeout=1,total_timeout=2).url,tagged)
        for redirect in ("http://objects.githubusercontent.com/x","https://evil.example/x","https://user@github.com/x","https://github.com:444/x","https://github.com/other/repo/releases/download/v1.0.0/x.zip"):
            with self.subTest(redirect=redirect),self.assertRaises(UpdateError): SecureHttpClient(opener=Opener([Raw(302,redirect)])).open(initial,headers={},connect_timeout=1,total_timeout=2)
        with self.assertRaises(UpdateError): SecureHttpClient(opener=Opener([Raw(302,allowed)]*4)).open(initial,headers={},connect_timeout=1,total_timeout=2)

    def test_network_size_and_timeout_fail_closed(self):
        response=Response("https://objects.githubusercontent.com/x",200,{},BytesIO(b"12345"))
        with self.assertRaises(UpdateError): SecureHttpClient().read_limited(response,limit=4,total_timeout=2,chunk_size=2)
        class TimeoutStream:
            def read(self,_size): raise TimeoutError("fixture")
            def close(self): pass
        with self.assertRaises(UpdateError) as caught:
            SecureHttpClient().read_limited(Response("https://objects.githubusercontent.com/x",200,{},TimeoutStream()),limit=10,total_timeout=2)
        self.assertEqual(caught.exception.code,"timeout")

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

    def test_service_does_not_expose_unverified_fields(self):
        raw=_raw_manifest(); bad=base64.b64encode(b"bad")+b"\n"
        with tempfile.TemporaryDirectory() as tmp:
            service=UpdateService(root=Path(tmp),client=FakeClient(raw,bad),public_keys={"lulu-update-rsa3072-2026-07":self.public_pem})
            snap=service.check(manual=True)
            self.assertEqual(snap.ui_state,"manifest_verify_error"); self.assertFalse(snap.verified_manifest); self.assertEqual((snap.latest_version,snap.notes,snap.asset_size),("","",0))

    @patch('src.core.updater.service.__version__', '0.3.1')
    @patch('src.core.updater.service.__build__', '20260815.3')
    @patch('src.core.updater.service.runtime_channel', return_value='trusted-test')
    def test_service_signed_state_variants(self, _channel):
        for local_manifest, expected in ((_manifest()|{"latest_version":"0.3.2","latest_build":"20260815.1"},"update_available"),(_manifest(),"up_to_date"),(_manifest()|{"min_supported_version":"9.0.0"},"manual_required")):
            version=local_manifest["latest_version"]; build=local_manifest["latest_build"]
            for key,kind,ext in (("macos_arm64","zip","zip"),("macos_arm64_dmg","dmg","dmg")):
                local_manifest["assets"][key]["filename"]=f"Lulu-{version}-{build}-arm64.{ext}"; local_manifest["assets"][key]["url"]=f"https://github.com/AidenXu-1/lulu-releases/releases/download/v{version}/Lulu-{version}-{build}-arm64.{ext}"
            raw=(json.dumps(local_manifest,separators=(",",":"))+"\n").encode()
            with tempfile.TemporaryDirectory() as tmp:
                service=UpdateService(root=Path(tmp),client=FakeClient(raw,self.sign(raw)),public_keys={"lulu-update-rsa3072-2026-07":self.public_pem})
                self.assertEqual(service.check(manual=True).ui_state,expected)

    def test_restart_drops_stale_download_promise_without_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "updates"
            store = StateStore(root)
            retained = store.root / "downloads" / "verified-existing.zip"
            retained.write_bytes(b"keep-existing-package")
            store.save(
                UpdateSnapshot(
                    state="available",
                    ui_state="update_available",
                    latest_version="0.2.5",
                    latest_build="20260728.3",
                    update_available=True,
                    verified_manifest=True,
                )
            )

            service = UpdateService(root=root, public_keys={})

            self.assertIsNone(service.manifest)
            self.assertEqual(service.snapshot.ui_state, "check_error")
            self.assertEqual(
                service.snapshot.error_code,
                "manifest_recheck_required",
            )
            self.assertFalse(service.snapshot.update_available)
            self.assertFalse(service.snapshot.verified_manifest)
            self.assertFalse(retained.exists())
            cleanup_log = store.root / "logs" / "cleanup.jsonl"
            self.assertIn('"category":"startup"', cleanup_log.read_text())
            reloaded = StateStore(root).load()
            self.assertEqual(reloaded.ui_state, "check_error")
            self.assertFalse(reloaded.verified_manifest)

    def test_startup_cleanup_preserves_resumable_states_and_active_lock(self):
        for ui_state in (
            "downloading",
            "download_paused",
            "verifying",
            "preparing",
            "ready",
            "ready_blocked",
            "installing",
            "waiting_ready",
        ):
            with self.subTest(ui_state=ui_state), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "updates"
                store = StateStore(root)
                download = store.root / "downloads" / "retained.part"
                staged = store.root / "staged" / "retained" / "Lulu.app"
                download.write_bytes(b"resume")
                staged.mkdir(parents=True)
                store.save(UpdateSnapshot(state="downloading", ui_state=ui_state, latest_version="99.0.0", latest_build="20990101.1"))
                UpdateService(root=root, public_keys={})
                self.assertEqual(download.read_bytes(), b"resume")
                self.assertTrue(staged.is_dir())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "updates"
            store = StateStore(root)
            retained = store.root / "downloads" / "locked.zip"
            retained.write_bytes(b"locked")
            lock = store.root / "transactions" / "update.lock"
            lock.write_text("a" * 64)
            os.chmod(lock, 0o600)
            UpdateService(root=root, public_keys={})
            self.assertEqual(retained.read_bytes(), b"locked")

    def test_startup_cleanup_refuses_symlink_and_special_file_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "updates"
            store = StateStore(root)
            outside = Path(tmp) / "outside.txt"
            outside.write_text("keep")
            ordinary = store.root / "downloads" / "ordinary.zip"
            ordinary.write_bytes(b"delete")
            link = store.root / "downloads" / "outside-link"
            link.symlink_to(outside)
            fifo = store.root / "downloads" / "blocked.fifo"
            os.mkfifo(fifo)

            result = cleanup_stale_on_startup(store.root, UpdateSnapshot())

            self.assertEqual(result["result"], "partial")
            self.assertFalse(ordinary.exists())
            self.assertTrue(link.is_symlink())
            self.assertTrue(fifo.exists())
            self.assertEqual(outside.read_text(), "keep")
            reasons = {item["reason"] for item in result["blocked"]}
            self.assertEqual(reasons, {"symlink", "special_file"})

    def test_download_resume_and_etag_drift_restart(self):
        data=b"0123456789abcdef"; digest=__import__("hashlib").sha256(data).hexdigest(); raw=_raw_manifest(zip_sha=digest,zip_size=len(data)); manifest=parse_manifest(raw,local_channel="trusted-test")
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"updates"; client=DownloadClient([(206,{"etag":"E1","content-range":f"bytes 4-{len(data)-1}/{len(data)}"},data[4:])]); service=UpdateService(root=root,client=client,public_keys={}); service.manifest=manifest; service.snapshot.verified_manifest=True
            downloads=service.store.root/"downloads"; part=downloads/(manifest.zip_asset.filename+".part"); meta=downloads/(manifest.zip_asset.filename+".part.meta.json"); part.write_bytes(data[:4]); os.chmod(part,0o600)
            atomic_json(meta,{"url":manifest.zip_asset.url,"version":manifest.latest_version,"build":manifest.latest_build,"sha256":digest,"etag":"E1","downloaded_bytes":4,"expected_size":len(data),"filename":manifest.zip_asset.filename,"txn":"a"*64})
            self.assertEqual(service.download().read_bytes(),data); self.assertEqual(client.headers[0]["Range"],"bytes=4-")
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/"updates"; client=DownloadClient([(200,{"etag":"E2"},b"ignored"),(200,{"etag":"E2","content-length":str(len(data))},data)]); service=UpdateService(root=root,client=client,public_keys={}); service.manifest=manifest; service.snapshot.verified_manifest=True
            downloads=service.store.root/"downloads"; part=downloads/(manifest.zip_asset.filename+".part"); meta=downloads/(manifest.zip_asset.filename+".part.meta.json"); part.write_bytes(data[:4]); os.chmod(part,0o600); atomic_json(meta,{"url":manifest.zip_asset.url,"version":manifest.latest_version,"build":manifest.latest_build,"sha256":digest,"etag":"E1","downloaded_bytes":4,"expected_size":len(data),"filename":manifest.zip_asset.filename,"txn":"a"*64})
            self.assertEqual(service.download().read_bytes(),data); self.assertNotIn("Range",client.headers[1])

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

    def test_valid_zip_extract_and_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); archive=root/"valid.zip"; _fixture_zip(archive); staged=root/"staged"/"txn"
            app=extract_zip(archive,staged); digest=validate_staged_app(app,version="0.2.4",build="20260712.1",verify_system=False)
            self.assertRegex(digest,r"^[0-9a-f]{64}$"); self.assertEqual(stat.S_IMODE((app/"Contents/MacOS/Lulu").stat().st_mode),0o755)

    def test_bounded_internal_symlink_closure_extracts_and_preserves_targets(self):
        def links(entries):
            for name in ("Lulu.app/Contents/Frameworks","Lulu.app/Contents/Frameworks/Foo.framework","Lulu.app/Contents/Frameworks/Foo.framework/Versions","Lulu.app/Contents/Frameworks/Foo.framework/Versions/A","Lulu.app/Contents/Resources"):
                entries.append((_zip_info(name,0o755,True),b""))
            entries.extend([(_zip_info("Lulu.app/Contents/Frameworks/Foo.framework/Versions/A/Foo",0o755),b"framework"),(_symlink_info("Lulu.app/Contents/Frameworks/Foo.framework/Versions/Current"),b"A"),(_symlink_info("Lulu.app/Contents/Frameworks/Foo.framework/Foo"),b"Versions/Current/Foo"),(_symlink_info("Lulu.app/Contents/Resources/Foo"),b"../Frameworks/Foo.framework/Foo")])
            return entries
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); archive=root/"links.zip"; _fixture_zip(archive,mutate=links); self.assertTrue(preflight_zip(archive)); app=extract_zip(archive,root/"staged")
            self.assertTrue((app/"Contents/Resources/Foo").is_symlink()); self.assertEqual(os.readlink(app/"Contents/Resources/Foo"),"../Frameworks/Foo.framework/Foo"); self.assertEqual(os.readlink(app/"Contents/Frameworks/Foo.framework/Versions/Current"),"A"); self.assertEqual((app/"Contents/Resources/Foo").read_bytes(),b"framework")

    def test_symlink_attack_matrix_and_zip_junk_fail_closed(self):
        def simple_link(target):
            return lambda entries:entries+[(_zip_info("Lulu.app/Contents/Resources",0o755,True),b""),(_symlink_info("Lulu.app/Contents/Resources/link"),target)]
        attacks=[b"/tmp/evil",b"../../../outside",b"missing",b"link",b"a\x00b",b"a\nb",b"..\\evil",b"C:/evil",b"~/evil",b"\xff"]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for index,target in enumerate(attacks):
                path=root/f"link-{index}.zip"; _fixture_zip(path,mutate=simple_link(target))
                with self.subTest(target=target),self.assertRaises(UpdateError): preflight_zip(path)
            def cycle(entries): return entries+[(_zip_info("Lulu.app/Contents/Resources",0o755,True),b""),(_symlink_info("Lulu.app/Contents/Resources/a"),b"b"),(_symlink_info("Lulu.app/Contents/Resources/b"),b"a")]
            path=root/"cycle.zip"; _fixture_zip(path,mutate=cycle)
            with self.assertRaises(UpdateError): preflight_zip(path)
            def deep(entries):
                entries.append((_zip_info("Lulu.app/Contents/Links",0o755,True),b""))
                for index in range(9): entries.append((_symlink_info(f"Lulu.app/Contents/Links/l{index}"),(f"l{index+1}" if index<8 else "../MacOS/Lulu").encode()))
                return entries
            path=root/"deep.zip"; _fixture_zip(path,mutate=deep)
            with self.assertRaises(UpdateError): preflight_zip(path)
            def linked_parent(entries): return entries+[(_zip_info("Lulu.app/Contents/Resources",0o755,True),b""),(_symlink_info("Lulu.app/Contents/Resources/parent"),b"../MacOS"),(_zip_info("Lulu.app/Contents/Resources/parent/child",0o644),b"x")]
            path=root/"linked-parent.zip"; _fixture_zip(path,mutate=linked_parent)
            with self.assertRaises(UpdateError): preflight_zip(path)
            for index,name in enumerate(("__MACOSX/x","Lulu.app/._Info.plist","Lulu.app/.DS_Store","Other.app/x","Lulu.app/Contents/com.apple.FinderInfo")):
                path=root/f"junk-{index}.zip"; _fixture_zip(path,mutate=lambda entries,n=name:entries+[(_zip_info(n,0o644),b"x")])
                with self.subTest(name=name),self.assertRaises(UpdateError): preflight_zip(path)

    def test_symlink_target_rewrite_during_creation_fails_and_cleans_staged(self):
        mutate=lambda entries:entries+[(_zip_info("Lulu.app/Contents/Resources",0o755,True),b""),(_symlink_info("Lulu.app/Contents/Resources/LuluExec"),b"../MacOS/Lulu")]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); archive=root/"rewrite.zip"; staged=root/"staged"; _fixture_zip(archive,mutate=mutate); real=os.symlink
            def rewrite(_target,name,*,dir_fd): return real("../Info.plist",name,dir_fd=dir_fd)
            with patch("src.core.updater.archive.os.symlink",side_effect=rewrite),self.assertRaises(UpdateError): extract_zip(archive,staged)
            self.assertFalse(staged.exists())

    def test_zip_attack_matrix(self):
        def replace_name(name,mode=0o644,kind=stat.S_IFREG):
            def mutate(entries):
                info=zipfile.ZipInfo(name); info.create_system=3; info.external_attr=(kind|mode)<<16; return entries+[(info,b"x")]
            return mutate
        canonical_collision=lambda entries:entries+[(_zip_info("Lulu.app/Cafe\u0301",0o644),b"a"),(_zip_info("Lulu.app/Caf\u00e9",0o644),b"b")]
        attacks=[replace_name("/absolute"),replace_name("Lulu.app/../evil"),replace_name("Lulu.app\\evil"),replace_name("lulu.app/CONTENTS/info.PLIST"),canonical_collision,replace_name("Lulu.app/link",0o777,stat.S_IFLNK),replace_name("Lulu.app/world",0o666),replace_name("Lulu.app/socket",0o644,stat.S_IFSOCK)]
        with tempfile.TemporaryDirectory() as tmp:
            for i,attack in enumerate(attacks):
                path=Path(tmp)/f"bad-{i}.zip"; _fixture_zip(path,mutate=attack)
                with self.subTest(i=i),self.assertRaises(UpdateError): preflight_zip(path)

    def test_zip_limits_duplicates_and_missing_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def additions(entries):
                return entries+[(entries[-1][0],entries[-1][1])]
            duplicate=root/"duplicate.zip"; _fixture_zip(duplicate,mutate=additions)
            deep=root/"deep.zip"; _fixture_zip(deep,mutate=lambda entries:entries+[(_zip_info("Lulu.app/"+"/".join(["x"]*33),0o644),b"x")])
            long_path=root/"long.zip"; _fixture_zip(long_path,mutate=lambda entries:entries+[(_zip_info("Lulu.app/"+("x"*510),0o644),b"x")])
            missing=root/"missing.zip"; _fixture_zip(missing,mutate=lambda entries:[item for item in entries if item[0].filename!="Lulu.app/Contents/MacOS/Lulu"])
            for path in (duplicate,deep,long_path,missing):
                with self.subTest(path=path.name),self.assertRaises(UpdateError): preflight_zip(path)
            bomb=root/"bomb.zip"; _fixture_zip(bomb,mutate=lambda entries:entries+[(_zip_info("Lulu.app/bomb",0o644),b"0"*2048)])
            with patch("src.core.updater.archive.MAX_SINGLE_UNCOMPRESSED_BYTES",1024),self.assertRaises(UpdateError): preflight_zip(bomb)

    def test_staged_parent_symlink_and_app_identity_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); archive=root/"valid.zip"; _fixture_zip(archive)
            outside=root/"outside"; outside.mkdir(); link=root/"link"; link.symlink_to(outside,target_is_directory=True)
            with self.assertRaises(UpdateError): extract_zip(archive,link/"txn")
            staged=root/"staged"/"txn"; app=extract_zip(archive,staged)
            for version,build in (("9.9.9","20260712.1"),("0.2.4","99999999.1")):
                with self.subTest(version=version,build=build),self.assertRaises(UpdateError): validate_staged_app(app,version=version,build=build,verify_system=False)
            fake=type("Result",(),{"returncode":1,"stdout":b"","stderr":b""})()
            with patch("src.core.updater.archive.subprocess.run",return_value=fake),self.assertRaises(UpdateError): validate_staged_app(app,version="0.2.4",build="20260712.1",verify_system=True)

    def test_zip_mode_and_special_file_matrix(self):
        def add(mode,kind,name="Lulu.app/extra"):
            def mutate(entries):
                info=zipfile.ZipInfo(name); info.create_system=3; info.external_attr=(kind|mode)<<16; return entries+[(info,b"x")]
            return mutate
        attacks=[add(0o4755,stat.S_IFREG),add(0o2755,stat.S_IFREG),add(0o1755,stat.S_IFREG),add(0o644,stat.S_IFIFO),add(0o644,stat.S_IFCHR),add(0o755,stat.S_IFREG,"Lulu.app/not-an-exec")]
        with tempfile.TemporaryDirectory() as tmp:
            for i,attack in enumerate(attacks):
                path=Path(tmp)/f"mode-{i}.zip"; _fixture_zip(path,mutate=attack)
                with self.subTest(i=i),self.assertRaises(UpdateError): preflight_zip(path)

    def test_state_symlink_and_unknown_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp)/"updates"); store.path.write_text('{"unknown":1}') ; os.chmod(store.path,0o600)
            with self.assertRaises(UpdateError): store.load()
            store.path.unlink(); store.path.write_text('{"downloaded":"not-an-int"}'); os.chmod(store.path,0o600)
            with self.assertRaises(UpdateError): store.load()
            store.path.unlink(); target=Path(tmp)/"outside"; target.write_text("x"); store.path.symlink_to(target)
            with self.assertRaises(UpdateError): store.load()

    def _apps(self, root):
        parent=root/"Applications"; parent.mkdir(mode=0o700); target=parent/"Lulu.app"; staged=root/"updates"/"staged"/("a"*64)/"Lulu.app"
        def make(app, marker):
            executable=app/"Contents/MacOS/Lulu"; executable.parent.mkdir(parents=True); executable.write_bytes(b"fixture-arm64"); executable.chmod(0o755)
            (app/"Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleName":"Lulu","CFBundleExecutable":"Lulu","CFBundleShortVersionString":"0.2.4","CFBundleVersion":"20260712.1"}))
            (app/marker).write_text(marker)
        make(target,"old"); make(staged,"new"); return parent,target,staged

    def test_transaction_success_ready_and_data_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); updates=root/"updates"; business=root/"support"/"models"; business.mkdir(parents=True); marker=business/"keep"; marker.write_text("same")
            tx=InstallTransaction(updates,allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            plan_path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            def launch(new_target,p):
                write_ready(Path(p["ready_path"]),p,pid=2,pid_start_time="n",executable_path=str(new_target/"Contents/MacOS/Lulu"))
            def wait(path,p,_timeout): return tx.validate_ready(path,p,pid_validator=lambda pid,start,exe:(pid,start)==(2,"n"))
            result=tx.execute_local(plan_path,launch=launch,wait_ready=wait,old_pid_validator=lambda *_:True)
            self.assertEqual(result,"installed"); self.assertTrue((target/"new").exists()); self.assertEqual(marker.read_text(),"same"); self.assertFalse(plan_path.parent.exists())

    def test_transaction_success_cleanup_runs_before_lock_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); updates=root/"updates"
            download=updates/"downloads"/"package.zip"; download.parent.mkdir(parents=True); download.write_bytes(b"package")
            tx=InstallTransaction(updates,allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            plan_path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            observed={"lock":False}
            def launch(new_target,p):
                write_ready(Path(p["ready_path"]),p,pid=2,pid_start_time="n",executable_path=str(new_target/"Contents/MacOS/Lulu"))
            def wait(path,p,_timeout): return tx.validate_ready(path,p,pid_validator=lambda pid,start,exe:(pid,start)==(2,"n"))
            def cleanup(current):
                observed["lock"]=tx.lock.exists()
                cleanup_after_install(updates,current["txn"])
            result=tx.execute_local(plan_path,launch=launch,wait_ready=wait,old_pid_validator=lambda *_:True,on_installed=cleanup)
            self.assertEqual(result,"installed")
            self.assertTrue(observed["lock"])
            self.assertFalse(download.exists())
            self.assertFalse((updates/"staged"/plan["txn"]).exists())
            self.assertFalse(tx.lock.exists())

    def test_cleanup_callback_failure_does_not_reverse_installed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); updates=root/"updates"
            tx=InstallTransaction(updates,allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            plan_path,_=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            def launch(new_target,p): write_ready(Path(p["ready_path"]),p,pid=2,pid_start_time="n",executable_path=str(new_target/"Contents/MacOS/Lulu"))
            def wait(path,p,_timeout): return tx.validate_ready(path,p,pid_validator=lambda pid,start,exe:(pid,start)==(2,"n"))
            result=tx.execute_local(plan_path,launch=launch,wait_ready=wait,old_pid_validator=lambda *_:True,on_installed=lambda _plan: (_ for _ in ()).throw(OSError("cleanup failed")))
            self.assertEqual(result,"installed")
            self.assertTrue((target/"new").exists())

    def test_transaction_copy_preserves_symlink_target_and_tree_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); resources=staged/"Contents/Resources"; resources.mkdir(); (resources/"LuluExec").symlink_to("../MacOS/Lulu")
            tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); archive=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]); sha=archive.app_file_manifest(staged)[1]
            path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            expected=(2,"n",str(Path(plan["target_app"])/"Contents/MacOS/Lulu"))
            def launch(new_target,current): write_ready(Path(current["ready_path"]),current,pid=2,pid_start_time="n",executable_path=expected[2]); return expected
            result=tx.execute_local(path,launch=launch,wait_ready=lambda ready,current,_:tx.validate_ready(ready,current,pid_validator=lambda *_:True,expected_child=expected),old_pid_validator=lambda *_:True)
            self.assertEqual(result,"installed"); self.assertTrue((target/"Contents/Resources/LuluExec").is_symlink()); self.assertEqual(os.readlink(target/"Contents/Resources/LuluExec"),"../MacOS/Lulu"); self.assertEqual(archive.app_file_manifest(target)[1],plan["tree_manifest_sha256"])

    def test_launch_exception_rolls_back_without_manual_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,_=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            result=tx.execute_local(path,launch=lambda *_:(_ for _ in ()).throw(OSError("fixture")),wait_ready=lambda *_:False,old_pid_validator=lambda *_:True)
            self.assertEqual(result,"rolled_back"); self.assertTrue((target/"old").exists()); self.assertFalse(tx.lock.exists())

    def test_failed_launch_rolls_back_original_symlink_tree_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root)
            old_resources=target/"Contents/Resources"; old_resources.mkdir(); (old_resources/"LuluExec").symlink_to("../MacOS/Lulu")
            staged_resources=staged/"Contents/Resources"; staged_resources.mkdir(); (staged_resources/"NewExec").symlink_to("../MacOS/Lulu")
            archive=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]); old_sha=archive.app_file_manifest(target)[1]; staged_sha=archive.app_file_manifest(staged)[1]
            tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False)
            path,_=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=staged_sha)
            result=tx.execute_local(path,launch=lambda *_:None,wait_ready=lambda *_:False,old_pid_validator=lambda *_:True)
            self.assertEqual(result,"rolled_back"); self.assertEqual(archive.app_file_manifest(target)[1],old_sha)
            self.assertTrue((target/"Contents/Resources/LuluExec").is_symlink()); self.assertEqual(os.readlink(target/"Contents/Resources/LuluExec"),"../MacOS/Lulu")
            self.assertFalse((target/"Contents/Resources/NewExec").exists())

    def test_running_child_preserves_both_bundles_instead_of_rolling_back(self):
        from src.core.updater.install import RollbackBlocked
        from src.core.updater.archive import app_file_manifest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _parent, target, staged = self._apps(root)
            tx = InstallTransaction(root/"updates", allowed_targets={target}, verify_system=False)
            path, plan = tx.create_plan(
                target=target, staged_app=staged, version="0.2.4", build="20260712.1",
                old_pid=1, old_pid_start_time="s", old_executable_path="/old",
                staged_sha=app_file_manifest(staged)[1],
            )
            def blocked(*_):
                raise RollbackBlocked("fixture child still alive")
            try:
                with self.assertRaises(RollbackBlocked):
                    tx.execute_local(path, launch=lambda *_: None, wait_ready=blocked,
                                     old_pid_validator=lambda *_: True)
                self.assertTrue((target/"new").is_file())
                self.assertTrue((Path(plan["backup_app"])/"old").is_file())
                self.assertTrue(path.is_file())
            finally:
                tx.release()

    @patch('src.core.updater.helper_entry.PreSwapHandover')
    def test_helper_final_exception_uses_recovery_matrix(self, _handover):
        from src.core.updater import helper_entry
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"updates"/"transactions"/("a"*64)/"install_plan.json"; path.parent.mkdir(parents=True); path.write_text("fixture")
            plan={"old_pid":1,"old_pid_start_time":"old","old_executable_path":"/old","target_app":str(Path(tmp)/"Applications/Lulu.app"),"staged_app":tmp,"target_parent":tmp}
            transaction=__import__("unittest.mock",fromlist=["Mock"]).Mock(); transaction.load_plan.return_value=plan; transaction.execute_local.side_effect=OSError("fixture"); transaction.recover.return_value="rolled_back"
            store=__import__("unittest.mock",fromlist=["Mock"]).Mock(); store.load.return_value=UpdateSnapshot()
            with patch.object(helper_entry,"InstallTransaction",return_value=transaction),patch.object(helper_entry,"StateStore",return_value=store),patch.object(helper_entry,"process_identity",return_value=None),patch.object(helper_entry.subprocess,"Popen") as popen:
                self.assertEqual(helper_entry.run_helper(str(path)),5)
            transaction.recover.assert_called_once_with(path.resolve()); popen.assert_called_once(); store.save.assert_called_once()

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

    @patch('src.core.updater.helper_entry.PreSwapHandover')
    def test_helper_cleanup_is_best_effort_after_confirmed_install(self, _handover):
        from src.core.updater import helper_entry
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "updates" / "transactions" / ("a" * 64) / "install_plan.json"
            path.parent.mkdir(parents=True)
            path.write_text("fixture")
            plan = {
                "txn": "a" * 64,
                "old_pid": 1,
                "old_pid_start_time": "old",
                "old_executable_path": "/old",
                "target_app": str(Path(tmp) / "Applications/Lulu.app"),
                "staged_app": tmp, "target_parent": tmp,
            }
            transaction = __import__("unittest.mock", fromlist=["Mock"]).Mock()
            transaction.load_plan.return_value = plan
            transaction.execute_local.return_value = "installed"
            store = __import__("unittest.mock", fromlist=["Mock"]).Mock()
            store.load.return_value = UpdateSnapshot()
            with patch.object(helper_entry, "InstallTransaction", return_value=transaction), patch.object(
                helper_entry, "StateStore", return_value=store
            ), patch.object(helper_entry, "process_identity", return_value=None), patch.object(
                helper_entry, "cleanup_successful_update", side_effect=OSError("cleanup fixture")
            ) as cleanup, patch.object(helper_entry, "record_cleanup_failure") as failure_receipt:
                self.assertEqual(helper_entry.run_helper(str(path)), 0)
            cleanup.assert_called_once_with(path.resolve().parents[2], plan)
            transaction.acquire.assert_called_once_with(plan["txn"])
            failure_receipt.assert_called_once()
            store.save.assert_called_once()

    def test_transaction_crash_recovery_matrix(self):
        for crash in ("after_copy","after_backup","after_install"):
            with self.subTest(crash=crash),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); _parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
                path,_=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
                self.assertEqual(tx.execute_local(path,launch=lambda *_:None,wait_ready=lambda *_:False,old_pid_validator=lambda *_:True,crash_at=crash),"crashed")
                self.assertEqual(tx.recover(path),"rolled_back"); self.assertTrue((target/"old").exists())

    def test_plan_ready_and_concurrency_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            with self.assertRaises(UpdateError): tx.acquire("a"*64)
            raw=json.loads(path.read_text()); raw["unknown"]=1; path.write_text(json.dumps(raw)); os.chmod(path,0o600)
            with self.assertRaises(UpdateError): tx.load_plan(path)
            tx.release()

    def test_install_location_staged_pid_and_rollback_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            wrong=root/"wrong"/"Lulu.app"; wrong.mkdir(parents=True)
            with self.assertRaises(UpdateError): tx.create_plan(target=target,staged_app=wrong,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            with self.assertRaises(UpdateError) as caught: tx.execute_local(path,launch=lambda *_:None,wait_ready=lambda *_:False,old_pid_validator=lambda *_:False)
            self.assertEqual(caught.exception.code,"pid_reused"); tx.release()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,_=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            with patch("src.core.updater.install._remove_tree_at",side_effect=OSError("fixture")):
                self.assertEqual(tx.execute_local(path,launch=lambda *_:None,wait_ready=lambda *_:False,old_pid_validator=lambda *_:True),"rolled_back")
            self.assertTrue((target/"old").exists())
            self.assertFalse(tx.lock.exists())
            self.assertEqual(StateStore(tx.root).load().ui_state, "install_error_rollback")

    def test_plan_sha_missing_ready_corruption_and_staged_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            raw=json.loads(path.read_text()); raw["plan_sha256"]="0"*64; path.write_text(json.dumps(raw)); os.chmod(path,0o600)
            with self.assertRaises(UpdateError): tx.load_plan(path)
            ready=Path(plan["ready_path"]); ready.write_text("{"); os.chmod(ready,0o600)
            self.assertFalse(tx.validate_ready(ready,plan,pid_validator=lambda *_:True)); tx.release()

    def test_parent_path_replacement_cannot_escape_dirfd(self):
        from src.core.updater import install as install_module
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); parent,target,staged=self._apps(root); outside=root/"outside"; outside.mkdir(); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,_=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha); original=install_module._copy_app_to_dirfd
            def race(source,parent_fd,name):
                result=original(source,parent_fd,name); moved=root/"Applications-moved"; parent.rename(moved); parent.symlink_to(outside,target_is_directory=True); return result
            with patch.object(install_module,"_copy_app_to_dirfd",side_effect=race),self.assertRaises(UpdateError): tx.execute_local(path,launch=lambda *_:None,wait_ready=lambda *_:False,old_pid_validator=lambda *_:True)
            self.assertEqual(list(outside.iterdir()),[]); tx.release()

    def test_recover_rejects_replaced_parent_without_external_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); parent,target,staged=self._apps(root); outside=root/"outside"; outside.mkdir(); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,_=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            self.assertEqual(tx.execute_local(path,launch=lambda *_:None,wait_ready=lambda *_:False,old_pid_validator=lambda *_:True,crash_at="after_backup"),"crashed")
            moved=root/"Applications-moved"; parent.rename(moved); parent.symlink_to(outside,target_is_directory=True)
            self.assertEqual(tx.recover(path),"failed_security_rejected"); self.assertEqual(list(outside.iterdir()),[]); self.assertTrue(tx.lock.exists())

    def test_invalid_ready_is_consumed_but_failed_plan_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha)
            def launch(_target,current): write_ready(Path(current["ready_path"]),current,pid=9,pid_start_time="foreign",executable_path=str(target/"Contents/MacOS/Lulu"))
            result=tx.execute_local(path,launch=launch,wait_ready=lambda ready,current,_timeout:tx.validate_ready(ready,current,pid_validator=lambda *_:False),old_pid_validator=lambda *_:True)
            self.assertEqual(result,"rolled_back"); self.assertFalse(Path(plan["ready_path"]).exists()); self.assertTrue(path.exists()); self.assertFalse(tx.lock.exists())

    def test_ready_nonce_pid_path_and_replay_rejections(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); _parent,target,staged=self._apps(root); tx=InstallTransaction(root/"updates",allowed_targets={target},verify_system=False); sha=__import__("src.core.updater.archive",fromlist=["app_file_manifest"]).app_file_manifest(staged)[1]
            path,plan=tx.create_plan(target=target,staged_app=staged,version="0.2.4",build="20260712.1",old_pid=1,old_pid_start_time="s",old_executable_path="/old",staged_sha=sha); ready=Path(plan["ready_path"])
            executable=str(Path(plan["target_app"])/"Contents/MacOS/Lulu")
            pid_validator=lambda pid,*_args: pid==2
            expected=(2,"n",executable); write_ready(ready,plan,pid=2,pid_start_time="n",executable_path=executable); self.assertFalse(tx.validate_ready(ready,plan,pid_validator=pid_validator)); self.assertTrue(tx.validate_ready(ready,plan,pid_validator=pid_validator,expected_child=expected))
            for key,value in (("nonce","0"*64),("txn","1"*64),("pid",9),("executable_path","/tmp/evil")):
                raw=json.loads(ready.read_text()); raw[key]=value; ready.write_text(json.dumps(raw)); os.chmod(ready,0o600)
                with self.subTest(key=key): self.assertFalse(tx.validate_ready(ready,plan,pid_validator=pid_validator,expected_child=expected))
                ready.unlink(); write_ready(ready,plan,pid=2,pid_start_time="n",executable_path=executable)
            with self.assertRaises(UpdateError): write_ready(ready,plan,pid=2,pid_start_time="n",executable_path=executable)
            tx.release()


if __name__ == "__main__": unittest.main()
