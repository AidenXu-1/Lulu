from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


class TrustedTestSafeE2EAuthorityRuntimeTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path]:
        from src.trusted_test_safe_e2e import create_private_run_root

        run_base = root / "runs"
        _private_dir(run_base)
        run_root = create_private_run_root(run_base)

        app = root / "Lulu-Trusted-Test-Safe-E2E.app"
        cli = app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E"
        resources = app / "Contents/Resources"
        ffmpeg = app / "Contents/Frameworks/imageio_ffmpeg/binaries/ffmpeg-test"
        _private_dir(cli.parent)
        _private_dir(resources)
        _private_dir(ffmpeg.parent)
        cli.write_bytes(b"synthetic-safe-cli")
        os.chmod(cli, 0o500)
        ffmpeg.write_bytes(b"synthetic-ffmpeg")
        os.chmod(ffmpeg, 0o500)
        (resources / "build_profile.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "profile": "trusted_test_safe_e2e",
                    "package_surface": "isolated_local_transcription_e2e/v1",
                    "source_commit": "a" * 40,
                    "source_tree": "b" * 40,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(resources / "build_profile.json", 0o400)

        model_seed = root / "model-seed"
        _private_dir(model_seed)
        sensevoice = model_seed / "SenseVoiceSmall"
        vad = model_seed / "fsmn-vad"
        _private_dir(sensevoice)
        _private_dir(vad)
        (sensevoice / "model.pt").write_bytes(b"offline-sensevoice-model")
        (vad / "model.pt").write_bytes(b"offline-vad-model")
        os.chmod(sensevoice / "model.pt", 0o400)
        os.chmod(vad / "model.pt", 0o400)
        source = root / "source.wav"
        source.write_bytes(b"RIFF-synthetic-audio")
        os.chmod(source, 0o400)
        return run_root, app, model_seed, source

    def test_authority_binds_cli_root_model_and_input_without_exposing_secrets(self) -> None:
        from src.trusted_test_safe_e2e import (
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            receipt = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority_path = Path(receipt["authority_path"])
            authority = load_safe_authority(
                authority_path,
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )
            self.assertEqual(Path(authority["run_root"]), run_root)
            self.assertTrue(Path(authority["input_path"]).is_relative_to(run_root))
            self.assertTrue(Path(authority["model_root"]).is_relative_to(run_root))
            sandbox = Path(authority["sandbox_profile_path"]).read_text(encoding="utf-8")
            self.assertIn("(deny network*)", sandbox)
            self.assertIn("(deny file-write*", sandbox)
            self.assertIn(str(authority["ffmpeg_path"]), sandbox)
            public = json.dumps(receipt, sort_keys=True)
            credential_raw = (run_root / "credentials/store.json").read_text(encoding="utf-8")
            for secret in json.loads(credential_raw).values():
                self.assertNotIn(secret, public)
                self.assertNotIn(secret, authority_path.read_text(encoding="utf-8"))

            os.chmod(Path(authority["input_path"]), 0o600)
            Path(authority["input_path"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "AUTHORITY_INPUT_MISMATCH"):
                load_safe_authority(
                    authority_path,
                    executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                    now_epoch=1_800_000_001,
                )

    def test_runtime_sandbox_allows_only_bound_candidate_and_ffmpeg(self) -> None:
        from src.trusted_test_safe_e2e import prepare_isolated_authority

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            executable = app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E"
            ffmpeg = app / "Contents/Frameworks/imageio_ffmpeg/binaries/ffmpeg-test"
            spoof = app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E-spoof"
            sandbox = (
                Path(prepared["authority_path"]).parent / "runtime.sb"
            ).read_text(encoding="utf-8")
            expected_rule = (
                "(deny process-exec (require-not (require-any "
                f"(literal {json.dumps(str(executable))}) "
                f"(literal {json.dumps(str(ffmpeg))}))))"
            )

            self.assertIn(expected_rule, sandbox)
            self.assertNotIn(json.dumps(str(spoof)), sandbox)
            self.assertIn('(require-not (literal "/dev/null"))', sandbox)

    def test_prepare_rejects_non_executable_ffmpeg_before_copying_inputs(self) -> None:
        from src.trusted_test_safe_e2e import prepare_isolated_authority

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            ffmpeg = app / "Contents/Frameworks/imageio_ffmpeg/binaries/ffmpeg-test"
            os.chmod(ffmpeg, 0o400)

            with self.assertRaisesRegex(RuntimeError, "CANDIDATE_FFMPEG_INVALID"):
                prepare_isolated_authority(
                    run_root=run_root,
                    candidate_app=app,
                    model_seed=model_seed,
                    source_file=source,
                    now_epoch=1_800_000_000,
                )

            self.assertFalse((run_root / "input/source.wav").exists())

    def test_prepare_rejects_non_executable_candidate_before_copying_inputs(self) -> None:
        from src.trusted_test_safe_e2e import prepare_isolated_authority

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            executable = app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E"
            os.chmod(executable, 0o400)

            with self.assertRaisesRegex(RuntimeError, "CANDIDATE_EXECUTABLE_INVALID"):
                prepare_isolated_authority(
                    run_root=run_root,
                    candidate_app=app,
                    model_seed=model_seed,
                    source_file=source,
                    now_epoch=1_800_000_000,
                )

            self.assertFalse((run_root / "input/source.wav").exists())

    def test_authority_reload_rejects_candidate_that_lost_executable_identity(self) -> None:
        from src.trusted_test_safe_e2e import (
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            executable = app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E"
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            os.chmod(executable, 0o400)

            with self.assertRaisesRegex(RuntimeError, "AUTHORITY_EXECUTABLE_MISMATCH"):
                load_safe_authority(
                    Path(prepared["authority_path"]),
                    executable_path=executable,
                    now_epoch=1_800_000_001,
                )

    def test_fake_core_run_writes_job_and_result_only_under_run_root(self) -> None:
        from src.core.models import Segment, Transcript
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            outside = base / "outside-sentinel"
            outside.write_text("unchanged", encoding="utf-8")
            before = outside.read_bytes()
            receipt = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority = load_safe_authority(
                Path(receipt["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )

            def fake_pipeline(source_path, *, engine_name, progress, is_cancelled):
                del progress, is_cancelled
                return Transcript(
                    text="隔离转录成功",
                    segments=[Segment(text="隔离转录成功", start=0.0, end=1.0)],
                    language="zh",
                    engine=engine_name,
                    source=source_path,
                )

            runtime = build_safe_runtime(
                authority,
                pipeline_runner=fake_pipeline,
                model_readiness_probe=lambda _engine: True,
                now_epoch=lambda: 1_800_000_001,
            )
            result = runtime.run(str(authority["input_path"]))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(outside.read_bytes(), before)
            result_files = list((run_root / "home/Library/Application Support/Lulu/jobs/results").glob("*.json"))
            self.assertEqual(len(result_files), 1)
            self.assertIn("隔离转录成功", result_files[0].read_text(encoding="utf-8"))

    def test_default_safe_readiness_accepts_authority_bound_model_without_formal_receipt(self) -> None:
        from src.core.models import Segment, Transcript
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority = load_safe_authority(
                Path(prepared["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )

            def fake_pipeline(source_path, *, engine_name, progress, is_cancelled):
                del progress, is_cancelled
                return Transcript(
                    text="authority模型可用",
                    segments=[Segment(text="authority模型可用", start=0.0, end=1.0)],
                    language="zh",
                    engine=engine_name,
                    source=source_path,
                )

            runtime = build_safe_runtime(
                authority,
                pipeline_runner=fake_pipeline,
                now_epoch=lambda: 1_800_000_001,
            )
            result = runtime.run(str(authority["input_path"]))

            self.assertEqual(result["status"], "succeeded")
            self.assertFalse(
                (run_root / "home/Library/Application Support/Lulu/.artifact-trust").exists()
            )

    def test_default_safe_pipeline_uses_authority_bound_sensevoice_engine(self) -> None:
        from src.core.models import Segment, Transcript
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        class FakeSenseVoiceEngine:
            def transcribe(self, wav_path, progress=None, is_cancelled=None):
                del progress, is_cancelled
                return Transcript(
                    text="真实引擎 seam 已到达",
                    segments=[Segment(text="真实引擎 seam 已到达", start=0.0, end=1.0)],
                    language="zh",
                    engine="local_sensevoice",
                    source=str(wav_path),
                )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority = load_safe_authority(
                Path(prepared["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )
            with (
                patch(
                    "src.core.audio.converter.to_wav_16k_mono",
                    side_effect=lambda source_path, _target: Path(source_path),
                ),
                patch(
                    "src.core.engines.local_sensevoice.LocalSenseVoiceEngine",
                    FakeSenseVoiceEngine,
                ),
            ):
                runtime = build_safe_runtime(
                    authority,
                    now_epoch=lambda: 1_800_000_001,
                )
                result = runtime.run(str(authority["input_path"]))

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(len(runtime.jobs()), 1)

    def test_default_safe_pipeline_discards_python_and_native_third_party_stdout(self) -> None:
        from src.core.models import Segment, Transcript
        from src.trusted_test_safe_e2e import _authority_bound_pipeline_runner

        class NoisySenseVoiceEngine:
            def transcribe(self, wav_path, progress=None, is_cancelled=None):
                del progress, is_cancelled
                print("THIRD_PARTY_PYTHON_STDOUT", flush=True)
                os.write(1, b"THIRD_PARTY_NATIVE_STDOUT\n")
                return Transcript(
                    text="stdout contract",
                    segments=[Segment(text="stdout contract", start=0.0, end=1.0)],
                    language="en",
                    engine="local_sensevoice",
                    source=str(wav_path),
                )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp) / "root"
            _private_dir(root)
            _private_dir(root / "tmp")
            source = root / "source.wav"
            source.write_bytes(b"RIFF-synthetic-audio")
            capture = Path(temp) / "captured.stdout"
            runner = _authority_bound_pipeline_runner(root, lambda _engine: True)

            with (
                patch(
                    "src.core.audio.converter.to_wav_16k_mono",
                    side_effect=lambda source_path, _target: Path(source_path),
                ),
                patch(
                    "src.core.engines.local_sensevoice.LocalSenseVoiceEngine",
                    NoisySenseVoiceEngine,
                ),
            ):
                saved_stdout = os.dup(1)
                capture_fd = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.dup2(capture_fd, 1)
                    os.close(capture_fd)
                    print("CLI_BEFORE_PIPELINE", flush=True)
                    transcript = runner(
                        source,
                        engine_name="local_sensevoice",
                        progress=lambda *_args: None,
                        is_cancelled=lambda: False,
                    )
                    print("CLI_AFTER_PIPELINE", flush=True)
                finally:
                    os.dup2(saved_stdout, 1)
                    os.close(saved_stdout)

            captured = capture.read_text(encoding="utf-8")
            self.assertEqual(transcript.text, "stdout contract")
            self.assertIn("CLI_BEFORE_PIPELINE", captured)
            self.assertIn("CLI_AFTER_PIPELINE", captured)
            self.assertNotIn("THIRD_PARTY_PYTHON_STDOUT", captured)
            self.assertNotIn("THIRD_PARTY_NATIVE_STDOUT", captured)

    def test_default_safe_pipeline_restores_stdout_after_noisy_failure(self) -> None:
        from src.trusted_test_safe_e2e import _authority_bound_pipeline_runner

        class NoisyFailingSenseVoiceEngine:
            def transcribe(self, wav_path, progress=None, is_cancelled=None):
                del wav_path, progress, is_cancelled
                print("THIRD_PARTY_PYTHON_STDOUT", flush=True)
                os.write(1, b"THIRD_PARTY_NATIVE_STDOUT\n")
                raise RuntimeError("PRIVATE_THIRD_PARTY_FAILURE")

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp) / "root"
            _private_dir(root)
            _private_dir(root / "tmp")
            source = root / "source.wav"
            source.write_bytes(b"RIFF-synthetic-audio")
            capture = Path(temp) / "captured.stdout"
            runner = _authority_bound_pipeline_runner(root, lambda _engine: True)

            with (
                patch(
                    "src.core.audio.converter.to_wav_16k_mono",
                    side_effect=lambda source_path, _target: Path(source_path),
                ),
                patch(
                    "src.core.engines.local_sensevoice.LocalSenseVoiceEngine",
                    NoisyFailingSenseVoiceEngine,
                ),
            ):
                saved_stdout = os.dup(1)
                capture_fd = os.open(capture, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.dup2(capture_fd, 1)
                    os.close(capture_fd)
                    with self.assertRaises(RuntimeError):
                        runner(
                            source,
                            engine_name="local_sensevoice",
                            progress=lambda *_args: None,
                            is_cancelled=lambda: False,
                        )
                    print('{"error":"SAFE_E2E_INTERNAL_ERROR","ok":false}', flush=True)
                finally:
                    os.dup2(saved_stdout, 1)
                    os.close(saved_stdout)

            captured = capture.read_text(encoding="utf-8")
            self.assertEqual(
                captured,
                '{"error":"SAFE_E2E_INTERNAL_ERROR","ok":false}\n',
            )
            self.assertNotIn("PRIVATE_THIRD_PARTY_FAILURE", captured)

    def test_authority_bound_readiness_rejects_digest_drift_before_job(self) -> None:
        from src.core.transcription_job_application import LocalTranscriptionJobError
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority = load_safe_authority(
                Path(prepared["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )
            model = Path(authority["model_root"]) / "SenseVoiceSmall/model.pt"
            os.chmod(model, 0o600)
            model.write_bytes(b"drifted")
            os.chmod(model, 0o400)
            runtime = build_safe_runtime(
                authority,
                pipeline_runner=lambda *_args, **_kwargs: self.fail("pipeline must not run"),
                now_epoch=lambda: 1_800_000_001,
            )

            with self.assertRaises(LocalTranscriptionJobError) as raised:
                runtime.run(str(authority["input_path"]))
            self.assertEqual(raised.exception.reason_code, "MODEL_NOT_READY")
            self.assertEqual(runtime.jobs(), ())

    def test_authority_bound_readiness_rejects_missing_shape_before_job(self) -> None:
        from src.core.transcription_job_application import LocalTranscriptionJobError
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            (model_seed / "fsmn-vad/model.pt").unlink()
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority = load_safe_authority(
                Path(prepared["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )
            runtime = build_safe_runtime(
                authority,
                pipeline_runner=lambda *_args, **_kwargs: self.fail("pipeline must not run"),
                now_epoch=lambda: 1_800_000_001,
            )

            with self.assertRaises(LocalTranscriptionJobError) as raised:
                runtime.run(str(authority["input_path"]))
            self.assertEqual(
                raised.exception.reason_code,
                "MODEL_NOT_READY",
            )
            self.assertEqual(runtime.jobs(), ())

    def test_authority_bound_readiness_rejects_unsafe_tree_before_job(self) -> None:
        from src.core.transcription_job_application import LocalTranscriptionJobError
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        for mutation in ("symlink", "special", "writable"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                dir="/private/tmp"
            ) as temp:
                base = Path(temp)
                run_root, app, model_seed, source = self._fixture(base)
                prepared = prepare_isolated_authority(
                    run_root=run_root,
                    candidate_app=app,
                    model_seed=model_seed,
                    source_file=source,
                    now_epoch=1_800_000_000,
                )
                authority = load_safe_authority(
                    Path(prepared["authority_path"]),
                    executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                    now_epoch=1_800_000_001,
                )
                model_root = Path(authority["model_root"])
                if mutation == "symlink":
                    target = model_root / "SenseVoiceSmall/model.pt"
                    target.unlink()
                    target.symlink_to(source)
                elif mutation == "special":
                    os.mkfifo(model_root / "unexpected.fifo", 0o400)
                else:
                    os.chmod(model_root / "SenseVoiceSmall/model.pt", 0o600)
                runtime = build_safe_runtime(
                    authority,
                    pipeline_runner=lambda *_args, **_kwargs: self.fail("pipeline must not run"),
                    now_epoch=lambda: 1_800_000_001,
                )

                with self.assertRaises(LocalTranscriptionJobError) as raised:
                    runtime.run(str(authority["input_path"]))
                self.assertEqual(raised.exception.reason_code, "MODEL_NOT_READY")
                self.assertEqual(runtime.jobs(), ())

    def test_authority_bound_readiness_rejects_missing_runtime_dependency_before_job(self) -> None:
        from src.core.transcription_job_application import LocalTranscriptionJobError
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority = load_safe_authority(
                Path(prepared["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )
            original_find_spec = importlib.util.find_spec
            runtime = build_safe_runtime(
                authority,
                pipeline_runner=lambda *_args, **_kwargs: self.fail("pipeline must not run"),
                now_epoch=lambda: 1_800_000_001,
            )
            with patch(
                "src.trusted_test_safe_e2e.importlib.util.find_spec",
                side_effect=lambda name: None if name == "funasr" else original_find_spec(name),
            ):
                with self.assertRaises(LocalTranscriptionJobError) as raised:
                    runtime.run(str(authority["input_path"]))

            self.assertEqual(
                raised.exception.reason_code,
                "LOCAL_TRANSCRIPTION_RUNTIME_INCOMPLETE",
            )
            self.assertEqual(runtime.jobs(), ())

    def test_main_maps_allowlisted_product_errors_without_details(self) -> None:
        from src.core.transcription_job_application import LocalTranscriptionJobError
        from src import trusted_test_safe_e2e as safe

        class FailedRuntime:
            def __init__(self, failure):
                self.failure = failure

            def run(self, _source_path):
                raise self.failure

        receipt = {
            "run_root": "/private/tmp/safe-root",
            "ffmpeg_path": "/private/tmp/safe-app/ffmpeg",
            "ffmpeg_sha256": "a" * 64,
            "input_path": "/private/tmp/safe-root/input/source.wav",
        }
        cases = (
            ("MODEL_NOT_READY", "MODEL_NOT_READY", "model_readiness"),
            ("AUTH_REQUIRED", "FRESH_CACHED_AUTH_REJECTED", "authorization"),
            ("NOT_FOUND", "SOURCE_NOT_FOUND", "source_validation"),
            (
                "RUNTIME_NOT_READY",
                "LOCAL_TRANSCRIPTION_RUNTIME_INCOMPLETE",
                "runtime_incomplete",
            ),
        )
        for code, reason, stage in cases:
            with self.subTest(reason=reason):
                output = io.StringIO()
                with (
                    patch.object(
                        safe,
                        "_frozen_safe_executable",
                        return_value=Path("/private/tmp/safe-app/cli"),
                    ),
                    patch.object(safe, "load_safe_authority", return_value=receipt),
                    patch.object(safe, "isolated_environment", return_value={}),
                    patch.object(safe, "install_runtime_guards"),
                    patch.object(
                        safe,
                        "build_safe_runtime",
                        return_value=FailedRuntime(
                            LocalTranscriptionJobError(code, reason)
                        ),
                    ),
                    redirect_stdout(output),
                ):
                    exit_code = safe.main(
                        ["--authority", "/private/tmp/safe-root/authority.json", "run"]
                    )

                self.assertEqual(exit_code, 65)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {
                        "ok": False,
                        "error": "SAFE_E2E_KNOWN_ERROR",
                        "stage": stage,
                        "reason": reason,
                    },
                )

    def test_safe_license_projection_does_not_import_formal_license_package(self) -> None:
        import builtins

        from src import trusted_test_safe_e2e as safe

        class Store:
            def read(self, _account):
                return json.dumps({"allowed": True, "expires_at": 1_800_000_010})

        projection = safe._SafeLicenseProjection(
            object(),
            Store(),
            lambda: 1_800_000_001,
        )
        original_import = builtins.__import__

        def reject_formal_license_import(name, globals=None, locals=None, fromlist=(), level=0):
            if "license" in name:
                raise AssertionError("formal license package must stay unreachable")
            return original_import(name, globals, locals, fromlist, level)

        with (
            patch.object(safe, "_validate_test_ticket"),
            patch("builtins.__import__", side_effect=reject_formal_license_import),
        ):
            status = projection.cached_status_projection()

        self.assertIs(status.allowed, True)
        self.assertEqual(status.code, "safe_test_allowed")
        self.assertEqual(status.__class__.__module__, "src.trusted_test_safe_e2e")

    def test_main_keeps_unknown_product_error_redacted(self) -> None:
        from src.core.transcription_job_application import LocalTranscriptionJobError
        from src import trusted_test_safe_e2e as safe

        class FailedRuntime:
            def run(self, _source_path):
                raise LocalTranscriptionJobError(
                    "INTEGRITY_ERROR",
                    "UNKNOWN_/private/tmp/secret-token",
                )

        receipt = {
            "run_root": "/private/tmp/safe-root",
            "ffmpeg_path": "/private/tmp/safe-app/ffmpeg",
            "ffmpeg_sha256": "a" * 64,
            "input_path": "/private/tmp/safe-root/input/source.wav",
        }
        output = io.StringIO()
        with (
            patch.object(safe, "_frozen_safe_executable", return_value=Path("/private/tmp/safe-app/cli")),
            patch.object(safe, "load_safe_authority", return_value=receipt),
            patch.object(safe, "isolated_environment", return_value={}),
            patch.object(safe, "install_runtime_guards"),
            patch.object(safe, "build_safe_runtime", return_value=FailedRuntime()),
            redirect_stdout(output),
        ):
            exit_code = safe.main(["--authority", "/private/tmp/safe-root/authority.json", "run"])

        self.assertEqual(exit_code, 70)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"ok": False, "error": "SAFE_E2E_INTERNAL_ERROR"},
        )

    def test_new_runtime_recovers_same_running_job_after_forced_interruption(self) -> None:
        from src.core.models import Segment, Transcript
        from src.trusted_test_safe_e2e import (
            build_safe_runtime,
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            authority = load_safe_authority(
                Path(prepared["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )

            def interrupted(*_args, **_kwargs):
                raise SystemExit(137)

            first = build_safe_runtime(
                authority,
                pipeline_runner=interrupted,
                model_readiness_probe=lambda _engine: True,
                now_epoch=lambda: 1_800_000_001,
            )
            with self.assertRaises(SystemExit):
                first.run(str(authority["input_path"]))
            running = first.jobs()
            self.assertEqual(len(running), 1)
            self.assertEqual(running[0].state.value, "running")
            job_id = running[0].job_id

            def completed(source_path, *, engine_name, progress, is_cancelled):
                del progress, is_cancelled
                return Transcript(
                    text="重启恢复成功",
                    segments=[Segment(text="重启恢复成功", start=0.0, end=1.0)],
                    language="zh",
                    engine=engine_name,
                    source=source_path,
                )

            second = build_safe_runtime(
                authority,
                pipeline_runner=completed,
                model_readiness_probe=lambda _engine: True,
                now_epoch=lambda: 1_800_000_002,
            )
            recovered = second.recover_and_run(str(authority["input_path"]))
            self.assertEqual(recovered["job_receipt"]["job_id"], job_id)
            self.assertEqual(recovered["status"], "succeeded")
            self.assertEqual(second.jobs()[0].state.value, "succeeded")

    def test_cleanup_only_removes_the_exact_authorized_run_root(self) -> None:
        from src.trusted_test_safe_e2e import (
            cleanup_isolated_root,
            load_safe_authority,
            prepare_isolated_authority,
        )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            base = Path(temp)
            run_root, app, model_seed, source = self._fixture(base)
            neighbor = base / "neighbor"
            neighbor.write_text("keep", encoding="utf-8")
            prepared = prepare_isolated_authority(
                run_root=run_root,
                candidate_app=app,
                model_seed=model_seed,
                source_file=source,
                now_epoch=1_800_000_000,
            )
            receipt = load_safe_authority(
                Path(prepared["authority_path"]),
                executable_path=app / "Contents/MacOS/Lulu-Trusted-Test-Safe-E2E",
                now_epoch=1_800_000_001,
            )
            result = cleanup_isolated_root(receipt)
            self.assertEqual(result["status"], "removed")
            self.assertFalse(run_root.exists())
            self.assertEqual(neighbor.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
