"""飞书受管音视频附件：只用临时目录与假网络。"""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core import feishu
from core.collect import bitable_media, export
from core.collect.models import CollectedItem


def wav_bytes(extra: int = 0) -> bytes:
    pcm = b"\x00\x00" * (2 + extra)
    return (
        b"RIFF"
        + (36 + len(pcm)).to_bytes(4, "little")
        + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + b"\x01\x00\x01\x00\x80\xbb\x00\x00\x00\x77\x01\x00\x02\x00\x10\x00"
        + b"data"
        + len(pcm).to_bytes(4, "little")
        + pcm
    )


def mp4_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42" + b"hdlr\x00\x00\x00\x00vide" + b"x" * 16


def managed_item(root: Path, work_id: str, payload: bytes, suffix: str) -> CollectedItem:
    stable_key = f"douyin:{work_id}"
    digest = hashlib.sha256(payload).hexdigest()
    identity = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:24]
    relative = f"douyin/{identity}-{digest[:20]}{suffix}"
    (root / "douyin").mkdir(parents=True, exist_ok=True)
    (root / relative).write_bytes(payload)
    return CollectedItem(
        work_id,
        title=f"作品 {work_id}",
        media_status="downloaded",
        media_local_path=relative,
    )


class Response:
    def __init__(self, payload, *, status_code=200, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.json_calls = 0

    def json(self):
        self.json_calls += 1
        return self.payload


class TestManagedMediaPreflight(unittest.TestCase):
    def test_only_stable_audio_video_are_frozen_without_remote_access(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "media"
            audio = managed_item(root, "audio", wav_bytes(), ".wav")
            video = managed_item(root, "video", mp4_bytes(), ".mp4")
            fake = managed_item(root, "fake", b"PK\x03\x04archive", ".mp4")
            missing = replace(
                managed_item(root, "missing", wav_bytes(1), ".wav"),
                media_local_path="douyin/missing.wav",
            )
            outside = Path(raw) / "outside.wav"
            outside.write_bytes(wav_bytes(2))
            symlink = managed_item(root, "symlink", wav_bytes(2), ".wav")
            (root / symlink.media_local_path).unlink()
            os.symlink(outside, root / symlink.media_local_path)
            hard = managed_item(root, "hard", wav_bytes(3), ".wav")
            os.link(root / hard.media_local_path, Path(raw) / "second-link.wav")
            url_only = CollectedItem(
                "url",
                play_url="https://signed.example.test/media?token=secret",
                media_status="url_only",
            )
            with patch("requests.get") as get:
                candidates, issues = bitable_media.prepare_bitable_media(
                    (audio, video, fake, missing, symlink, hard, url_only),
                    root=root,
                )
            get.assert_not_called()

        self.assertEqual([item.mime_type for item in candidates], ["audio/wav", "video/mp4"])
        self.assertEqual({item.record_index for item in candidates}, {0, 1})
        self.assertEqual(len(issues), 4)
        self.assertEqual(
            {item.reason_code for item in issues},
            {"media_type_mismatch", "media_file_rejected"},
        )
        rendered = repr(candidates) + repr(issues)
        self.assertNotIn(str(root), rendered)
        self.assertNotIn("signed.example.test", rendered)

    def test_size_hard_gate_and_export_never_writes_attachment_before_upload(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "media"
            item = managed_item(root, "large", wav_bytes(8), ".wav")
            with patch.object(bitable_media, "BITABLE_MEDIA_MAX_BYTES", 8):
                preflight = export.prepare_bitable_export([item], media_root=root)
        self.assertEqual(preflight.media_candidates, ())
        self.assertEqual(preflight.media_skipped[0].reason_code, "media_file_too_large")
        self.assertNotIn("音视频附件", preflight.records[0]["fields"])

    def test_verified_stream_detects_post_preflight_replacement(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "media"
            item = managed_item(root, "change", wav_bytes(), ".wav")
            candidate = bitable_media.prepare_bitable_media((item,), root=root)[0][0]
            (root / item.media_local_path).write_bytes(wav_bytes(9))
            with self.assertRaises(bitable_media.BitableMediaFileError) as raised:
                with bitable_media.verified_bitable_media_stream(candidate):
                    pass
        self.assertEqual(raised.exception.reason_code, "media_file_changed")
        self.assertNotIn(str(root), str(raised.exception))


class TestMediaFieldContract(unittest.TestCase):
    def test_existing_exact_field_is_reused_and_wrong_type_fails_closed(self):
        exact = {"items": [{"field_name": "音视频附件", "type": 17, "field_id": "FIELD1"}]}
        wrong = {"items": [{"field_name": "音视频附件", "type": 1, "field_id": "FIELDX"}]}
        with patch.object(feishu, "_api", return_value=exact) as api:
            ready = feishu.ensure_bitable_media_field("APP", "TBL", confirmed=False, token="TOKEN")
        self.assertEqual(ready.status, "ready")
        self.assertNotIn("FIELD1", repr(ready))
        self.assertEqual(api.call_count, 1)
        with patch.object(feishu, "_api", return_value=wrong) as api:
            conflict = feishu.ensure_bitable_media_field("APP", "TBL", confirmed=True, token="TOKEN")
        self.assertEqual((conflict.status, api.call_count), ("conflict", 1))

    def test_missing_field_requires_confirmation(self):
        with patch.object(feishu, "_api", return_value={"items": []}) as api:
            receipt = feishu.ensure_bitable_media_field(
                "APP", "TBL", confirmed=False, token="TOKEN"
            )
        self.assertEqual(receipt.status, "confirmation_required")
        self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))

    def test_uncertain_creation_relists_and_reuses_same_client_token_once(self):
        posts = []
        lists = iter([
            {"items": []},
            {"items": []},
            {"items": [{"field_name": "音视频附件", "type": 17, "field_id": "FIELD1"}]},
        ])

        def api(method, path, _token, **kwargs):
            if method == "GET":
                return next(lists)
            posts.append((path, kwargs["json"]))
            if len(posts) == 1:
                raise feishu.FeishuTransportError(
                    retryable_not_sent=False,
                    uncertain=True,
                )
            return {}

        with (
            patch.object(feishu, "_api", side_effect=api),
            patch.object(feishu, "uuid4", return_value="stable-field-token"),
            patch.object(feishu.time, "sleep"),
        ):
            receipt = feishu.ensure_bitable_media_field(
                "APP", "TBL", confirmed=True, token="TOKEN"
            )
        self.assertEqual((receipt.status, receipt.attempts), ("ready", 2))
        self.assertEqual(posts[0], posts[1])
        self.assertIn("client_token=stable-field-token", posts[0][0])
        self.assertEqual(posts[0][1], {
            "field_name": "音视频附件", "type": 17, "property": None,
        })


class TestMediaUploadStateMachine(unittest.TestCase):
    def setUp(self):
        feishu._bitable_media_last_start = 0.0
        receipt_temp = tempfile.TemporaryDirectory()
        self.addCleanup(receipt_temp.cleanup)
        isolated_root = Path(receipt_temp.name) / "default-receipts"
        original_init = feishu.BitableMediaReceiptStore.__init__

        def isolated_init(instance, root=None):
            original_init(instance, isolated_root if root is None else root)

        receipt_store_patch = patch.object(
            feishu.BitableMediaReceiptStore,
            "__init__",
            new=isolated_init,
        )
        receipt_store_patch.start()
        self.addCleanup(receipt_store_patch.stop)

    def _candidate(self, raw: str, *, extra: int = 0):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "media"
        item = managed_item(root, raw, wav_bytes(extra), ".wav")
        return bitable_media.prepare_bitable_media((item,), root=root)[0][0]

    def test_exact_size_mode_boundaries(self):
        self.assertEqual(feishu._BITABLE_FILE_UPLOAD_ALL_MAX, 20_971_520)
        self.assertEqual(feishu._BITABLE_FILE_BLOCK_SIZE, 4_194_304)
        self.assertEqual(feishu._BITABLE_FILE_MAX_BYTES, 2_000_000_000)
        self.assertEqual(feishu._bitable_media_mode(0), "invalid")
        self.assertEqual(feishu._bitable_media_mode(1), "upload_all")
        self.assertEqual(feishu._bitable_media_mode(20_971_520), "upload_all")
        self.assertEqual(feishu._bitable_media_mode(20_971_520, True), "multipart")
        self.assertEqual(feishu._bitable_media_mode(20_971_521), "multipart")
        self.assertEqual(feishu._bitable_media_mode(2_000_000_000), "multipart")
        self.assertEqual(feishu._bitable_media_mode(2_000_000_001), "too_large")

    def test_upload_all_exact_payload_then_updates_only_original_record(self):
        candidate = self._candidate("all")
        captured = {}

        def post(url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            captured["bytes"] = kwargs["files"]["file"][1].read()
            return Response({"code": 0, "data": {"file_token": "MOCK_FILE_TOKEN"}})

        api_calls = []

        def api(method, path, token, **kwargs):
            api_calls.append((method, path, token, kwargs))
            return {}

        with (
            patch("requests.post", side_effect=post) as request,
            patch.object(feishu, "_api", side_effect=api),
            patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0),
            patch.object(feishu, "uuid4", return_value="stable-record-token"),
        ):
            receipt = feishu.upload_and_attach_bitable_media(
                "APP1", "TBL1", "REC1", candidate, token="MOCK_USER_TOKEN"
            )
        self.assertEqual(receipt.status, "attached")
        self.assertEqual(receipt.mode, "upload_all")
        self.assertNotIn("MOCK_FILE_TOKEN", repr(receipt))
        self.assertEqual(request.call_count, 1)
        self.assertTrue(captured["url"].endswith("/drive/v1/medias/upload_all"))
        files = captured["kwargs"]["files"]
        self.assertEqual(set(files), {
            "file_name", "parent_type", "parent_node", "size", "checksum", "extra", "file",
        })
        self.assertEqual(files["parent_type"], (None, "bitable_file"))
        self.assertEqual(files["parent_node"], (None, "APP1"))
        self.assertEqual(json.loads(files["extra"][1]), {"drive_route_token": "APP1"})
        self.assertEqual(files["checksum"], (None, candidate.adler32))
        self.assertEqual(hashlib.sha256(captured["bytes"]).hexdigest(), candidate.sha256)
        update = next(call for call in api_calls if call[0] == "POST")
        self.assertIn("/records/batch_update?client_token=stable-record-token", update[1])
        self.assertNotIn("batch_create", update[1])
        self.assertEqual(update[3]["json"], {
            "records": [{
                "record_id": "REC1",
                "fields": {"音视频附件": [{"file_token": "MOCK_FILE_TOKEN"}]},
            }]
        })

    def test_multipart_parts_checksums_finish_and_private_24h_receipt(self):
        candidate = self._candidate("parts", extra=20)
        calls = []

        def post(url, **kwargs):
            calls.append((url.rsplit("/", 1)[-1], kwargs))
            if url.endswith("upload_prepare"):
                return Response({"code": 0, "data": {
                    "upload_id": "MOCK_UPLOAD_ID", "block_size": 16,
                    "block_num": (candidate.size + 15) // 16,
                }})
            if url.endswith("upload_finish"):
                return Response({"code": 0, "data": {"file_token": "MOCK_FILE_TOKEN"}})
            return Response({"code": 0, "data": {}})

        receipt_root = Path(self.temp.name) / "receipts"
        with (
            patch("requests.post", side_effect=post),
            patch.object(feishu, "_api", return_value={}),
            patch.object(feishu, "_BITABLE_FILE_UPLOAD_ALL_MAX", 1),
            patch.object(feishu, "_BITABLE_FILE_BLOCK_SIZE", 16),
            patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0),
        ):
            receipt = feishu.upload_and_attach_bitable_media(
                "APP1",
                "TBL1",
                "REC1",
                candidate,
                token="MOCK_USER_TOKEN",
                receipt_store=feishu.BitableMediaReceiptStore(receipt_root),
            )
        self.assertEqual(receipt.status, "attached")
        self.assertEqual(receipt.mode, "multipart")
        parts = [kwargs["files"] for name, kwargs in calls if name == "upload_part"]
        self.assertEqual(receipt.confirmed_parts, len(parts))
        for seq, part in enumerate(parts):
            blob = part["file"][1]
            self.assertEqual(part["seq"], (None, str(seq)))
            self.assertEqual(part["size"], (None, str(len(blob))))
            self.assertEqual(part["checksum"], (None, str(__import__("zlib").adler32(blob) & 0xFFFFFFFF)))
        prepare = next(kwargs["json"] for name, kwargs in calls if name == "upload_prepare")
        self.assertEqual(prepare["parent_type"], "bitable_file")
        self.assertEqual(prepare["parent_node"], "APP1")
        self.assertEqual(json.loads(prepare["extra"]), {"drive_route_token": "APP1"})
        finish = next(kwargs["json"] for name, kwargs in calls if name == "upload_finish")
        self.assertEqual(finish, {"upload_id": "MOCK_UPLOAD_ID", "block_num": len(parts)})

        receipt_path = next(receipt_root.glob("*.json"))
        stored = json.loads(receipt_path.read_text("utf-8"))
        self.assertEqual(stored["status"], "attached")
        self.assertEqual(stored["reason_code"], "")
        self.assertGreaterEqual(stored["updated_at"], stored["created_at"])
        self.assertEqual(stored["expires_at"] - stored["updated_at"], 86_400)
        self.assertEqual(stored["confirmed_parts"], list(range(len(parts))))
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)
        rendered = receipt_path.read_text("utf-8")
        self.assertNotIn(str(candidate.managed_root), rendered)
        self.assertNotIn("MOCK_USER_TOKEN", rendered)

    def test_retry_matrix_is_finite_and_uncertain_never_replays(self):
        attempts = []

        def rate_limited():
            attempts.append(1)
            if len(attempts) == 1:
                raise feishu.FeishuApiError(99991400, "rate", retry_after=3)
            return {"ok": True}

        with patch.object(feishu.time, "sleep") as sleep:
            data, used = feishu._run_bitable_media_step(
                "part", rate_limited, is_cancelled=lambda: False
            )
        self.assertEqual((data, used, len(attempts)), ({"ok": True}, 2, 2))
        sleep.assert_called_once_with(3)

        import requests

        candidate = self._candidate("uncertain")
        for side_effect in (
            requests.exceptions.ReadTimeout("secret URL must not leak"),
            None,
        ):
            with self.subTest(side_effect=type(side_effect).__name__):
                if side_effect is None:
                    response = Response(
                        {"code": 0, "data": {"file_token": "MUST_NOT_USE"}},
                        status_code=307,
                    )
                    mocked = patch("requests.post", return_value=response)
                else:
                    mocked = patch("requests.post", side_effect=side_effect)
                with mocked as post, patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0):
                    receipt = feishu.upload_and_attach_bitable_media(
                        "APP", "TBL", "REC", candidate, token="MOCK"
                    )
                self.assertEqual((receipt.status, post.call_count), ("uncertain", 1))
                self.assertNotIn("secret", repr(receipt))
                self.assertNotIn("MUST_NOT_USE", repr(receipt))

    def test_106_retry_codes_and_proven_not_sent_connection_retry_once(self):
        for code in (1061045, 1061006):
            calls = []

            def request():
                calls.append(code)
                if len(calls) == 1:
                    raise feishu.FeishuApiError(code, "retryable")
                return {"ok": True}

            with patch.object(feishu.time, "sleep"):
                data, attempts = feishu._run_bitable_media_step(
                    "part", request, is_cancelled=lambda: False
                )
            self.assertEqual((data, attempts, len(calls)), ({"ok": True}, 2, 2))

        import requests

        calls = []

        def connect_timeout_once():
            calls.append(1)
            if len(calls) == 1:
                raise requests.exceptions.ConnectTimeout("not sent")
            return {"ok": True}

        with patch.object(feishu.time, "sleep"):
            data, attempts = feishu._run_bitable_media_step(
                "prepare", connect_timeout_once, is_cancelled=lambda: False
            )
        self.assertEqual((data, attempts, len(calls)), ({"ok": True}, 2, 2))

    def test_http_429_honors_reset_and_5xx_is_uncertain_without_body_echo(self):
        responses = [
            Response(
                {"code": 99991400, "msg": "rate"},
                status_code=429,
                headers={"x-ogw-ratelimit-reset": "2.5"},
            ),
            Response({"code": 0, "data": {"ok": True}}),
        ]
        with (
            patch("requests.post", side_effect=responses) as post,
            patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0),
            patch.object(feishu.time, "sleep") as sleep,
        ):
            data, attempts = feishu._run_bitable_media_step(
                "prepare",
                lambda: feishu._post_bitable_media_once(
                    "MOCK", "/drive/v1/medias/upload_prepare", json_body={"x": 1}
                ),
                is_cancelled=lambda: False,
            )
        self.assertEqual((data, attempts, post.call_count), ({"ok": True}, 2, 2))
        sleep.assert_called_once_with(2.5)

        response = Response(
            {"code": 1, "msg": "MOCK_TOKEN /Users/private response body"},
            status_code=503,
        )
        with (
            patch("requests.post", return_value=response) as post,
            patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0),
        ):
            with self.assertRaises(feishu._BitableMediaStepError) as raised:
                feishu._run_bitable_media_step(
                    "finish",
                    lambda: feishu._post_bitable_media_once(
                        "MOCK", "/drive/v1/medias/upload_finish", json_body={}
                    ),
                    is_cancelled=lambda: False,
                )
        self.assertEqual((raised.exception.status, post.call_count), ("uncertain", 1))
        self.assertNotIn("MOCK_TOKEN", str(raised.exception))
        self.assertNotIn("/Users/private", str(raised.exception))

    def test_cancel_between_parts_stops_before_next_part_and_finish(self):
        candidate = self._candidate("cancel-parts", extra=24)
        cancelled = {"value": False}
        paths = []

        def post(url, **_kwargs):
            name = url.rsplit("/", 1)[-1]
            paths.append(name)
            if name == "upload_prepare":
                return Response({"code": 0, "data": {
                    "upload_id": "MOCK_UPLOAD_ID",
                    "block_size": 16,
                    "block_num": (candidate.size + 15) // 16,
                }})
            if name == "upload_part":
                cancelled["value"] = True
                return Response({"code": 0, "data": {}})
            raise AssertionError("取消后不得 finish")

        with (
            patch("requests.post", side_effect=post),
            patch.object(feishu, "_BITABLE_FILE_UPLOAD_ALL_MAX", 1),
            patch.object(feishu, "_BITABLE_FILE_BLOCK_SIZE", 16),
            patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0),
        ):
            receipt = feishu.upload_and_attach_bitable_media(
                "APP",
                "TBL",
                "REC",
                candidate,
                token="MOCK",
                is_cancelled=lambda: cancelled["value"],
                receipt_store=feishu.BitableMediaReceiptStore(
                    Path(self.temp.name) / "cancel-receipts"
                ),
            )
        self.assertEqual(receipt.status, "cancelled")
        self.assertEqual(paths.count("upload_part"), 1)
        self.assertNotIn("upload_finish", paths)
        stored = json.loads(next((Path(self.temp.name) / "cancel-receipts").glob("*.json")).read_text("utf-8"))
        self.assertEqual(stored["reason_code"], "media_upload_cancelled")

    def test_uncertain_record_update_reads_back_before_same_key_retry(self):
        candidate = self._candidate("readback")
        posts = []

        def api(method, path, _token, **kwargs):
            if method == "GET":
                return {"record": {"fields": {"音视频附件": [
                    {"file_token": "MOCK_FILE_TOKEN"}
                ]}}}
            posts.append((path, kwargs["json"]))
            raise feishu.FeishuTransportError(
                retryable_not_sent=False,
                uncertain=True,
            )

        with (
            patch.object(
                feishu,
                "_post_bitable_media_once",
                return_value={"file_token": "MOCK_FILE_TOKEN"},
            ),
            patch.object(feishu, "_api", side_effect=api),
            patch.object(feishu, "uuid4", return_value="stable-update-token"),
        ):
            receipt = feishu.upload_and_attach_bitable_media(
                "APP", "TBL", "REC", candidate, token="MOCK"
            )
        self.assertEqual(receipt.status, "attached")
        self.assertEqual(len(posts), 1)
        self.assertIn("client_token=stable-update-token", posts[0][0])

    def test_global_slot_serializes_cover_and_media_requests_and_spaces_starts(self):
        active = 0
        maximum = 0
        start = threading.Event()

        def request():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(0.01)
            active -= 1

        def worker():
            start.wait()
            feishu._bitable_media_request(request)

        with patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0):
            threads = [threading.Thread(target=worker) for _ in range(3)]
            for thread in threads:
                thread.start()
            start.set()
            for thread in threads:
                thread.join(1)
        self.assertEqual(maximum, 1)

        clock = [10.0]
        waits = []

        def monotonic():
            return clock[0]

        def sleep(wait):
            waits.append(wait)
            clock[0] += wait

        feishu._bitable_media_last_start = 0.0
        with (
            patch.object(feishu.time, "monotonic", side_effect=monotonic),
            patch.object(feishu.time, "sleep", side_effect=sleep),
        ):
            feishu._bitable_media_request(lambda: None)
            feishu._bitable_media_request(lambda: None)
        self.assertEqual(waits, [0.25])

    def test_receipt_cleanup_deletes_only_proven_expired_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "receipts"
            store = feishu.BitableMediaReceiptStore(root)
            now = 2_000_000.0

            expired_hash = "a" * 64
            active_hash = "b" * 64
            store.write(expired_hash, {
                "schema_version": 1,
                "intent_hash": expired_hash,
                "created_at": now - 90_000,
                "expires_at": now - 3_600,
            })
            store.write(active_hash, {
                "schema_version": 1,
                "intent_hash": active_hash,
                "created_at": now - 100,
                "expires_at": now + 86_300,
            })

            malformed = root / f"{'c' * 64}.json"
            malformed.write_text("{broken", encoding="utf-8")
            malformed.chmod(0o600)
            unknown = root / "keep-me.json"
            unknown.write_text("{}", encoding="utf-8")
            unknown.chmod(0o600)
            temporary = root / f".{expired_hash}.writer.tmp"
            temporary.write_text("partial", encoding="utf-8")
            temporary.chmod(0o600)
            outside = Path(raw) / "outside.json"
            outside.write_text("do not delete", encoding="utf-8")
            link = root / f"{'d' * 64}.json"
            os.symlink(outside, link)

            result = store.cleanup_expired(now=now)

            self.assertEqual(result.deleted, 1)
            self.assertEqual(result.kept_unexpired, 1)
            self.assertEqual(result.preserved_unknown, 4)
            self.assertFalse((root / f"{expired_hash}.json").exists())
            self.assertTrue((root / f"{active_hash}.json").exists())
            self.assertTrue(malformed.exists())
            self.assertTrue(unknown.exists())
            self.assertTrue(temporary.exists())
            self.assertTrue(link.is_symlink())
            self.assertEqual(outside.read_text("utf-8"), "do not delete")

    def test_receipt_cleanup_rejects_a_symlinked_root_without_touching_target(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            outside = base / "outside"
            outside.mkdir()
            protected = outside / f"{'a' * 64}.json"
            protected.write_text("protected", encoding="utf-8")
            protected.chmod(0o600)
            linked_root = base / "receipts"
            os.symlink(outside, linked_root, target_is_directory=True)
            store = feishu.BitableMediaReceiptStore(linked_root)

            with self.assertRaises(OSError):
                store.cleanup_expired(now=2_000_000.0)
            result = store.cleanup_expired_best_effort(now=2_000_000.0)

            self.assertEqual(result.errors, 1)
            self.assertEqual(protected.read_text("utf-8"), "protected")

    def test_receipt_cleanup_preserves_file_replaced_before_delete(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "receipts"
            store = feishu.BitableMediaReceiptStore(root)
            now = 2_000_000.0
            intent_hash = "e" * 64
            target = root / f"{intent_hash}.json"
            detached = root / "detached-original.json"
            payload = {
                "schema_version": 1,
                "intent_hash": intent_hash,
                "created_at": now - 90_000,
                "expires_at": now - 3_600,
            }
            store.write(intent_hash, payload)
            real_stat = os.stat
            target_stats = 0

            def replace_before_delete(path, *args, **kwargs):
                nonlocal target_stats
                if path == target.name and kwargs.get("dir_fd") is not None:
                    target_stats += 1
                    if target_stats == 2:
                        target.rename(detached)
                        target.write_text(json.dumps(payload), encoding="utf-8")
                        target.chmod(0o600)
                return real_stat(path, *args, **kwargs)

            with patch.object(feishu.os, "stat", side_effect=replace_before_delete):
                result = store.cleanup_expired(now=now)

            self.assertEqual(result.deleted, 0)
            self.assertEqual(result.preserved_unknown, 1)
            self.assertTrue(target.exists())
            self.assertTrue(detached.exists())

    def test_multipart_upload_runs_best_effort_cleanup_before_receipt_writes(self):
        candidate = self._candidate("cleanup-trigger", extra=20)
        receipt_root = Path(self.temp.name) / "receipts"
        store = feishu.BitableMediaReceiptStore(receipt_root)

        def post(url, **_kwargs):
            if url.endswith("upload_prepare"):
                return Response({"code": 0, "data": {
                    "upload_id": "MOCK_UPLOAD_ID",
                    "block_size": 16,
                    "block_num": (candidate.size + 15) // 16,
                }})
            if url.endswith("upload_finish"):
                return Response({"code": 0, "data": {"file_token": "MOCK_FILE_TOKEN"}})
            return Response({"code": 0, "data": {}})

        with (
            patch("requests.post", side_effect=post),
            patch.object(feishu, "_api", return_value={}),
            patch.object(feishu, "_BITABLE_FILE_UPLOAD_ALL_MAX", 1),
            patch.object(feishu, "_BITABLE_FILE_BLOCK_SIZE", 16),
            patch.object(feishu, "_BITABLE_MEDIA_MIN_INTERVAL", 0),
            patch.object(
                store,
                "cleanup_expired_best_effort",
                wraps=store.cleanup_expired_best_effort,
            ) as cleanup,
        ):
            receipt = feishu.upload_and_attach_bitable_media(
                "APP", "TBL", "REC", candidate, token="MOCK", receipt_store=store
            )

        self.assertEqual(receipt.status, "attached")
        cleanup.assert_called_once_with()


class TestStartupReceiptMaintenance(unittest.TestCase):
    def test_startup_maintenance_runs_receipt_cleanup_without_propagating_failures(self):
        from src import main as app_main
        from src.core import feishu as package_feishu
        from src.core import pipeline

        with (
            patch("src.core.audio.converter.ensure_ffmpeg_on_path"),
            patch.object(pipeline, "cleanup_stale_temp"),
            patch.object(
                package_feishu.BitableMediaReceiptStore,
                "cleanup_expired_best_effort",
                side_effect=RuntimeError("local cleanup failure"),
            ) as cleanup,
        ):
            app_main._run_startup_maintenance()

        cleanup.assert_called_once_with()


class TestExportPartialSettlement(unittest.TestCase):
    def test_media_failure_does_not_block_other_media_or_rows_and_cover(self):
        one = type("Candidate", (), {"record_index": 0, "stable_key": "x:1", "title": "一"})()
        two = type("Candidate", (), {"record_index": 1, "stable_key": "x:2", "title": "二"})()
        results = iter([
            feishu.BitableMediaAttachmentReceipt(
                "explicit_failed", "upload_all", 1, reason_code="media_upload_rejected"
            ),
            feishu.BitableMediaAttachmentReceipt("attached", "upload_all", 2),
        ])
        with (
            patch.object(feishu, "_user_token", return_value="MOCK"),
            patch.object(feishu, "create_wiki_bitable", return_value={
                "app_token": "APP", "node_token": "NODE", "url": "https://feishu.cn/wiki/NODE",
            }),
            patch.object(feishu, "_prepare_initial_bitable_table", return_value="TBL"),
            patch.object(
                feishu,
                "ensure_bitable_media_field",
                return_value=feishu.BitableMediaFieldReceipt("ready"),
            ),
            patch.object(
                feishu,
                "batch_create_records",
                return_value=feishu.BitableBatchReceipt(
                    2, 2, 0, record_id_by_index=((0, "REC0"), (1, "REC1"))
                ),
            ),
            patch.object(feishu, "upload_and_attach_bitable_media", side_effect=results) as upload,
        ):
            receipt = feishu.export_collection_to_new_bitable(
                "SPACE",
                "PARENT",
                "标题",
                "采集结果",
                export.BITABLE_FIELDS,
                ({"fields": {"点赞": 1}}, {"fields": {"点赞": 2}}),
                media_candidates=(one, two),
            )
        self.assertEqual(upload.call_count, 2)
        self.assertEqual(
            (receipt.row_success, receipt.media_success, receipt.media_failed, receipt.media_uncertain),
            (2, 1, 1, 0),
        )
        self.assertFalse(receipt.complete)
        for secret in ("file_token", "MOCK", "/Users/"):
            self.assertNotIn(secret, receipt.safe_details())

    def test_batch_media_defers_safe_uncertainty_then_restarts_once_as_multipart(self):
        one = type("Candidate", (), {"record_index": 0, "stable_key": "x:1", "title": "一"})()
        two = type("Candidate", (), {"record_index": 1, "stable_key": "x:2", "title": "二"})()
        results = iter([
            feishu.BitableMediaAttachmentReceipt(
                "uncertain",
                "multipart",
                1,
                confirmed_parts=4,
                reason_code="media_part_uncertain",
            ),
            feishu.BitableMediaAttachmentReceipt("attached", "multipart", 2),
            feishu.BitableMediaAttachmentReceipt("attached", "multipart", 2),
        ])
        with (
            patch.object(feishu, "_user_token", return_value="MOCK"),
            patch.object(feishu, "create_wiki_bitable", return_value={
                "app_token": "APP", "node_token": "NODE", "url": "https://feishu.cn/wiki/NODE",
            }),
            patch.object(feishu, "_prepare_initial_bitable_table", return_value="TBL"),
            patch.object(
                feishu,
                "ensure_bitable_media_field",
                return_value=feishu.BitableMediaFieldReceipt("ready"),
            ),
            patch.object(
                feishu,
                "batch_create_records",
                return_value=feishu.BitableBatchReceipt(
                    2, 2, 0, record_id_by_index=((0, "REC0"), (1, "REC1"))
                ),
            ),
            patch.object(
                feishu, "upload_and_attach_bitable_media", side_effect=results
            ) as upload,
        ):
            receipt = feishu.export_collection_to_new_bitable(
                "SPACE",
                "PARENT",
                "标题",
                "采集结果",
                export.BITABLE_FIELDS,
                ({"fields": {}}, {"fields": {}}),
                media_candidates=(one, two),
            )

        self.assertEqual(
            [call.args[3].record_index for call in upload.call_args_list],
            [0, 1, 0],
        )
        self.assertTrue(all(call.kwargs["connection_unstable"] for call in upload.call_args_list))
        self.assertEqual(
            (receipt.media_success, receipt.media_failed, receipt.media_uncertain),
            (2, 0, 0),
        )
        self.assertEqual(receipt.issues, ())
        self.assertTrue(receipt.complete)

    def test_finish_uncertainty_is_not_replayed(self):
        candidate = type(
            "Candidate", (), {"record_index": 0, "stable_key": "x:1", "title": "一"}
        )()
        with (
            patch.object(feishu, "_user_token", return_value="MOCK"),
            patch.object(feishu, "create_wiki_bitable", return_value={
                "app_token": "APP", "node_token": "NODE", "url": "https://feishu.cn/wiki/NODE",
            }),
            patch.object(feishu, "_prepare_initial_bitable_table", return_value="TBL"),
            patch.object(
                feishu,
                "ensure_bitable_media_field",
                return_value=feishu.BitableMediaFieldReceipt("ready"),
            ),
            patch.object(
                feishu,
                "batch_create_records",
                return_value=feishu.BitableBatchReceipt(
                    1, 1, 0, record_id_by_index=((0, "REC0"),)
                ),
            ),
            patch.object(
                feishu,
                "upload_and_attach_bitable_media",
                return_value=feishu.BitableMediaAttachmentReceipt(
                    "uncertain",
                    "multipart",
                    2,
                    confirmed_parts=5,
                    reason_code="media_finish_uncertain",
                ),
            ) as upload,
        ):
            receipt = feishu.export_collection_to_new_bitable(
                "SPACE",
                "PARENT",
                "标题",
                "采集结果",
                export.BITABLE_FIELDS,
                ({"fields": {}},),
                media_candidates=(candidate,),
            )

        self.assertEqual(upload.call_count, 1)
        self.assertEqual((receipt.media_success, receipt.media_uncertain), (0, 1))
        self.assertEqual(receipt.issues[0].reason_code, "media_finish_uncertain")

    def test_safe_restart_failure_is_settled_once(self):
        candidate = type(
            "Candidate", (), {"record_index": 0, "stable_key": "x:1", "title": "一"}
        )()
        results = iter([
            feishu.BitableMediaAttachmentReceipt(
                "uncertain", "multipart", 1, reason_code="media_prepare_uncertain"
            ),
            feishu.BitableMediaAttachmentReceipt(
                "explicit_failed", "multipart", 1, reason_code="media_upload_rejected"
            ),
        ])
        with (
            patch.object(feishu, "_user_token", return_value="MOCK"),
            patch.object(feishu, "create_wiki_bitable", return_value={
                "app_token": "APP", "node_token": "NODE", "url": "https://feishu.cn/wiki/NODE",
            }),
            patch.object(feishu, "_prepare_initial_bitable_table", return_value="TBL"),
            patch.object(
                feishu,
                "ensure_bitable_media_field",
                return_value=feishu.BitableMediaFieldReceipt("ready"),
            ),
            patch.object(
                feishu,
                "batch_create_records",
                return_value=feishu.BitableBatchReceipt(
                    1, 1, 0, record_id_by_index=((0, "REC0"),)
                ),
            ),
            patch.object(
                feishu, "upload_and_attach_bitable_media", side_effect=results
            ) as upload,
        ):
            receipt = feishu.export_collection_to_new_bitable(
                "SPACE",
                "PARENT",
                "标题",
                "采集结果",
                export.BITABLE_FIELDS,
                ({"fields": {}},),
                media_candidates=(candidate,),
            )

        self.assertEqual(upload.call_count, 2)
        self.assertEqual(
            (receipt.media_success, receipt.media_failed, receipt.media_uncertain),
            (0, 1, 0),
        )
        self.assertEqual(len(receipt.issues), 1)
        self.assertEqual(receipt.issues[0].reason_code, "media_upload_rejected")


if __name__ == "__main__":
    unittest.main()
