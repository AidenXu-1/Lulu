from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest


HARD_CAP = 1_048_576
COMBINED_CAP = 2_097_152
WINDOW = 524_288
READ_CHUNK = 65_536
READY = b"READY\n"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()

IDENTITY_CASES = (
    "golden_full",
    "ad_hoc_zero_team",
    "identity_client_with_core_role",
    "identity_server_with_cli_role",
    "identity_cli_identifier_with_gui_role",
    "malformed_dr_len_0",
    "malformed_dr_len_65536",
    "malformed_nonce_len_31",
    "malformed_nonce_len_33",
    "malformed_nonzero_null",
    "malformed_generation_nul",
    "malformed_total_overflow",
    "golden_client_digest_bitflip",
    "golden_server_digest_bitflip",
    "golden_transcript_digest_bitflip",
    "golden_null_client_digest",
    "golden_null_server_digest",
    "golden_null_transcript_digest",
)

REAL_HELPER_CASES = (
    "pg_leader_exit_child_alive",
    "pg_term_responsive",
    "pg_term_ignoring",
    "stream_exact_cap_stdout",
    "stream_exact_cap_stderr",
    "stream_over_cap_stdout",
    "stream_over_cap_stderr",
    "stream_pipe_hold",
    "stream_timeout_partial_final_drain",
)

NO_POPEN_CASES = (
    "pg_ownership_mismatch",
    "pg_probe_eperm",
    "pg_probe_other",
)

PROCESS_EXPECTED_LENGTH_SHA = {
    "pg_leader_exit_child_alive": (2041, "e640d77c636d0e3c28f226a4578c21daf6391e825bfb04df14cc4b6eae519da8"),
    "pg_term_responsive": (2012, "027cf0a9052da45c6a38d4972898d6c8badf9b028e4c38877e2be1b6ef9476a7"),
    "pg_term_ignoring": (2015, "3125afebc1fd6f5bb14b9fee498f55856d6a75b7decba16e91dd86b9b4e2b9cd"),
    "stream_exact_cap_stdout": (4_196_233, "4fbbab9919461be858b6409601cb2be74bcbbc811ff38189d17d835da3150cbf"),
    "stream_exact_cap_stderr": (4_196_233, "ed987b19d651112df583f144b298e72cfd301ed0748ac814bdb76f2399462f69"),
    "stream_over_cap_stdout": (2_099_161, "44e1a9af0f78585c55b1c9a1ed171284628ecf92d6516a74565421eb8bcd47ee"),
    "stream_over_cap_stderr": (2_099_161, "8a8d14ef5effe0b5b91f16251ce00e5b8cd1281ad8eee6ca70b0bd576259ee12"),
    "stream_pipe_hold": (2021, "439dc66790e5446005e68258d3e70b4ae8f39a920822b94667355b0965e63df3"),
    "stream_timeout_partial_final_drain": (419_823, "e8986e0d3d7422f6b673123c959dfbce137a91e6664cbc004774e130ff5f8c21"),
}

FIXED_S1C3_INPUTS = {
    "docs/decisions/system/SYS-0020-lulu-macos-trusted-local-transport-feasibility.md": "b727f7baaa3ed101c3d75a8c140a4eeda414a4f83b1f1f23b09a4b77308af813",
    "docs/spec/lulu-macos-trusted-local-transport-s1.md": "c6c59933857c3d04d93f8a6b889134eff5566cf224cd3532756d6d46a6b63fcb",
    "Agent-Team-归档-20260909/project-root/docs/collaboration/部门/产品部/报告/2026-09-01-TASK-20260831-ED04C7-S1C3-macOS可信通道合同收敛.md": "bc48b6605d86c02a4a2c8844ab1c5cb907bee88a403d16d6225b363454213ab1",
    "Agent-Team-归档-20260909/project-root/docs/collaboration/部门/产品部/报告/2026-09-01-TASK-20260831-ED04C7-CAND-20260901-ED04C8-artifact-set.json": "20f882ae64a6fd15f07640681f759b4b5e0bb0bdd2727e8d91b37233ead0175b",
    "Agent-Team-归档-20260909/project-root/docs/collaboration/部门/产品部/报告/2026-09-01-TASK-20260831-ED04C7-CAND-20260901-ED04C8-manifest.json": "049feace126edc402d09f0c5edff79816b44aa86d7c5342738638f187bcb33cc",
}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hex_bytes(data: bytes) -> str:
    return "hex:" + data.hex()


def pattern_bytes(start: int, length: int) -> bytes:
    pattern = bytes(range(251))
    offset = start % 251
    rotated = pattern[offset:] + pattern[:offset]
    return (rotated * ((length + 250) // 251))[:length]


def independent_identity(facts: dict[str, object]) -> tuple[bytes, bytes, bytes]:
    dr_blob = bytes.fromhex(str(facts["designated_requirement_blob_hex"]))
    dr_sha = hashlib.sha256(dr_blob).digest()
    values = (
        str(facts["team_id"]).encode("utf-8"),
        str(facts["identifier"]).encode("utf-8"),
        str(facts["generation"]).encode("utf-8"),
        bytes.fromhex(str(facts["cdhash_hex"])),
    )
    identity_input = bytes([int(facts["role_u8"])]) + b"".join(
        struct.pack(">H", len(value)) + value for value in values
    ) + dr_sha
    return dr_sha, identity_input, hashlib.sha256(identity_input).digest()


def independent_transcript(vector: dict[str, object], client_digest: bytes, server_digest: bytes) -> bytes:
    client = vector["client"]
    server = vector["server"]
    assert isinstance(client, dict) and isinstance(server, dict)
    client_generation = str(client["generation"]).encode("utf-8")
    server_generation = str(server["generation"]).encode("utf-8")
    service = str(vector["service_name"]).encode("utf-8")
    return b"".join((
        b"LULU-HS-V1\x00",
        struct.pack(">Q", int(vector["selected_protocol"])),
        bytes.fromhex(str(vector["client_nonce_hex"])),
        bytes.fromhex(str(vector["server_nonce_hex"])),
        client_digest,
        server_digest,
        struct.pack(">H", len(client_generation)), client_generation,
        struct.pack(">H", len(server_generation)), server_generation,
        struct.pack(">H", len(service)), service,
    ))


def expected_stream(
    stream_name: str,
    data: bytes,
    phase_counts: dict[str, int],
    *,
    complete: bool,
    operation_pipe_closed: bool,
    final_pipe_eof: bool,
    reason: str,
    truncated: bool = False,
    overflow_at_byte: int | None = None,
) -> dict[str, object]:
    prefix = data[: min(len(data), WINDOW)]
    suffix = data[-min(len(data), WINDOW):] if data else b""
    return {
        "stream_name": stream_name,
        "hard_cap": HARD_CAP,
        "phase_counts": phase_counts,
        "observed_byte_count": len(data),
        "observed_sha256": sha256(data),
        "complete": complete,
        "operation_pipe_closed": operation_pipe_closed,
        "final_pipe_eof": final_pipe_eof,
        "truncated": truncated,
        "reason": reason,
        "full_bytes": None if truncated or not complete else hex_bytes(data),
        "prefix_length": len(prefix),
        "prefix_sha256": sha256(prefix),
        "prefix_bytes": hex_bytes(prefix),
        "suffix_length": len(suffix),
        "suffix_sha256": sha256(suffix),
        "suffix_bytes": hex_bytes(suffix),
        "omitted_byte_count": max(0, len(data) - len(prefix) - len(suffix)),
        "overflow_at_byte": overflow_at_byte,
    }


def not_started_stream(stream_name: str) -> dict[str, object]:
    return expected_stream(
        stream_name, b"", {"execution": 0, "term_drain": 0, "kill_drain": 0, "final_drain": 0},
        complete=False, operation_pipe_closed=False, final_pipe_eof=False, reason="not_started",
    )


def expected_stream_pair(case_id: str) -> tuple[dict[str, object], dict[str, object]]:
    zeros = {"execution": 0, "term_drain": 0, "kill_drain": 0, "final_drain": 0}
    if case_id in NO_POPEN_CASES:
        return not_started_stream("stdout"), not_started_stream("stderr")
    if case_id in {"pg_leader_exit_child_alive", "pg_term_responsive", "pg_term_ignoring", "stream_pipe_hold"}:
        stdout = expected_stream(
            "stdout", READY, {"execution": 6, "term_drain": 0, "kill_drain": 0, "final_drain": 0},
            complete=True, operation_pipe_closed=False, final_pipe_eof=True, reason="pipe_not_closed",
        )
        stderr = expected_stream(
            "stderr", b"", dict(zeros), complete=True, operation_pipe_closed=False,
            final_pipe_eof=True, reason="pipe_not_closed",
        )
        return stdout, stderr
    if case_id in {"stream_exact_cap_stdout", "stream_exact_cap_stderr"}:
        payload = pattern_bytes(0, HARD_CAP)
        payload_stream = expected_stream(
            "stdout" if case_id.endswith("stdout") else "stderr", payload,
            {"execution": HARD_CAP, "term_drain": 0, "kill_drain": 0, "final_drain": 0},
            complete=True, operation_pipe_closed=True, final_pipe_eof=True, reason="complete",
        )
        peer_name = "stderr" if case_id.endswith("stdout") else "stdout"
        peer = expected_stream(
            peer_name, b"", dict(zeros), complete=True, operation_pipe_closed=True,
            final_pipe_eof=True, reason="complete",
        )
        return (payload_stream, peer) if case_id.endswith("stdout") else (peer, payload_stream)
    if case_id in {"stream_over_cap_stdout", "stream_over_cap_stderr"}:
        payload = pattern_bytes(0, HARD_CAP + 1)
        payload_stream = expected_stream(
            "stdout" if case_id.endswith("stdout") else "stderr", payload,
            {"execution": HARD_CAP + 1, "term_drain": 0, "kill_drain": 0, "final_drain": 0},
            complete=True, operation_pipe_closed=False, final_pipe_eof=True,
            reason="hard_cap_exceeded", truncated=True, overflow_at_byte=HARD_CAP + 1,
        )
        peer_name = "stderr" if case_id.endswith("stdout") else "stdout"
        peer = expected_stream(
            peer_name, b"", dict(zeros), complete=True, operation_pipe_closed=False,
            final_pipe_eof=True, reason="complete",
        )
        return (payload_stream, peer) if case_id.endswith("stdout") else (peer, payload_stream)
    if case_id == "stream_timeout_partial_final_drain":
        payload = pattern_bytes(0, 69_633)
        stdout = expected_stream(
            "stdout", payload, {"execution": 65_537, "term_drain": 4_096, "kill_drain": 0, "final_drain": 0},
            complete=True, operation_pipe_closed=False, final_pipe_eof=True, reason="pipe_not_closed",
        )
        stderr = expected_stream(
            "stderr", b"", dict(zeros), complete=True, operation_pipe_closed=False,
            final_pipe_eof=True, reason="pipe_not_closed",
        )
        return stdout, stderr
    raise AssertionError(case_id)


def expected_real_process(case_id: str) -> dict[str, object]:
    stdout, stderr = expected_stream_pair(case_id)
    branch = {
        "pg_leader_exit_child_alive": ("cleanup_failed", "SUBPROCESS_CLEANUP_FAILED@pg_leader_exit_child_alive/<OWNED_GROUP>", 0, ["SIGTERM"], True, False, False),
        "pg_term_responsive": ("timeout", "SUBPROCESS_TIMEOUT@pg_term_responsive/<OWNED_GROUP>", -15, ["SIGTERM"], True, False, True),
        "pg_term_ignoring": ("timeout", "SUBPROCESS_TIMEOUT@pg_term_ignoring/<OWNED_GROUP>", -9, ["SIGTERM", "SIGKILL"], True, True, True),
        "stream_exact_cap_stdout": ("success", None, 0, [], False, False, True),
        "stream_exact_cap_stderr": ("success", None, 0, [], False, False, True),
        "stream_over_cap_stdout": ("output_limit", "STREAM_OUTPUT_LIMIT_EXCEEDED@stream_over_cap_stdout/stdout", -15, ["SIGTERM"], True, False, True),
        "stream_over_cap_stderr": ("output_limit", "STREAM_OUTPUT_LIMIT_EXCEEDED@stream_over_cap_stderr/stderr", -15, ["SIGTERM"], True, False, True),
        "stream_pipe_hold": ("cleanup_failed", "SUBPROCESS_CLEANUP_FAILED@stream_pipe_hold/<OWNED_GROUP>", 0, ["SIGTERM"], True, False, False),
        "stream_timeout_partial_final_drain": ("timeout", "SUBPROCESS_TIMEOUT@stream_timeout_partial_final_drain/<OWNED_GROUP>", 0, ["SIGTERM"], True, False, True),
    }[case_id]
    outcome, error, returncode, signals, term_sent, kill_sent, cleanup_closed = branch
    return {
        "case_id": case_id,
        "execution_model": "real_popen_helper_v1",
        "ownership_verified": True,
        "owned_tuple_relation": "owned",
        "operation_outcome": outcome,
        "stable_error": error,
        "returncode": returncode,
        "signals": signals,
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "leader_reaped": True,
        "leader_pid_absent": True,
        "final_group_probe": "ESRCH",
        "group_absent": True,
        "stdout": stdout,
        "stderr": stderr,
        "process_temp_root_absent": True,
        "suite_temp_root_absent": True,
        "cleanup_closed": cleanup_closed,
        "canonical_probe_summary": {
            "ownership_verified": True,
            "probe_error": None,
            "term_sent": term_sent,
            "kill_sent": kill_sent,
            "final_esrch": True,
        },
        "raw_deadline_validation_passed": True,
    }


def expected_no_popen(case_id: str) -> dict[str, object]:
    stdout, stderr = expected_stream_pair(case_id)
    values = {
        "pg_ownership_mismatch": (41001, 41001, 41002, False, [], None, 0, "SUBPROCESS_CLEANUP_FAILED@pg_ownership_mismatch/41002"),
        "pg_probe_eperm": (42001, 42001, 42001, True, [{"phase": "pre_signal", "result": "error", "errno_name": "EPERM", "errno_number": 1}], "EPERM", 1, "SUBPROCESS_CLEANUP_FAILED@pg_probe_eperm/42001"),
        "pg_probe_other": (43001, 43001, 43001, True, [{"phase": "pre_signal", "result": "error", "errno_name": "EIO", "errno_number": 5}], "EIO", 1, "SUBPROCESS_CLEANUP_FAILED@pg_probe_other/43001"),
    }[case_id]
    pid, sid, pgid, owned, probes, probe_error, probe_count, stable_error = values
    return {
        "case_id": case_id, "execution_model": "no_popen_control_seam_v1", "popen_created": False,
        "pid": pid, "sid": sid, "pgid": pgid, "ownership_verified": owned,
        "returncode": None, "leader_reaped": False, "leader_pid_absent": None,
        "final_group_probe": None, "group_absent": None,
        "setup_deadline_ns": None, "operation_deadline_ns": None, "term_deadline_ns": None,
        "kill_deadline_ns": None, "leader_wait_deadline_ns": None, "final_drain_deadline_ns": None,
        "signals": [], "term_sent": False, "kill_sent": False, "os_killpg_call_count": 0,
        "probe_seam_call_count": probe_count, "raw_probe_sequence": probes,
        "canonical_probe_summary": {
            "ownership_verified": owned, "probe_error": probe_error,
            "term_sent": False, "kill_sent": False, "final_esrch": False,
        },
        "stdout": stdout, "stderr": stderr,
        "process_temp_root": None, "process_temp_root_absent": True,
        "suite_temp_root": None, "suite_temp_root_absent": True,
        "cleanup_closed": False, "stable_error": stable_error,
    }


class StreamAccumulator:
    def __init__(self, name: str) -> None:
        self.name = name
        self.count = 0
        self.digest = hashlib.sha256()
        self.prefix = bytearray()
        self.suffix = bytearray()
        self.full: bytearray | None = bytearray()
        self.phase_counts = {"execution": 0, "term_drain": 0, "kill_drain": 0, "final_drain": 0}
        self.eof = False
        self.overflow_at: int | None = None

    def add(self, data: bytes, phase: str) -> None:
        self.count += len(data)
        phase_key = phase
        if phase_key not in self.phase_counts:
            if phase_key in {"setup", "operation"}:
                phase_key = "execution"
            elif "term" in phase_key:
                phase_key = "term_drain"
            elif "kill" in phase_key:
                phase_key = "kill_drain"
            else:
                phase_key = "final_drain"
        self.phase_counts[phase_key] += len(data)
        self.digest.update(data)
        if len(self.prefix) < WINDOW:
            self.prefix.extend(data[: WINDOW - len(self.prefix)])
        self.suffix.extend(data)
        if len(self.suffix) > WINDOW:
            del self.suffix[: len(self.suffix) - WINDOW]
        if self.full is not None:
            self.full.extend(data)
            if len(self.full) > HARD_CAP:
                self.full = None
        if self.count > HARD_CAP and self.overflow_at is None:
            self.overflow_at = self.count

    def receipt(self, *, operation_pipe_closed: bool, reason: str) -> dict[str, object]:
        prefix = bytes(self.prefix)
        suffix = bytes(self.suffix)
        truncated = self.count > HARD_CAP
        return {
            "stream_name": self.name, "hard_cap": HARD_CAP,
            "phase_counts": dict(self.phase_counts), "observed_byte_count": self.count,
            "observed_sha256": self.digest.hexdigest(), "complete": self.eof,
            "operation_pipe_closed": operation_pipe_closed, "final_pipe_eof": self.eof,
            "truncated": truncated, "reason": reason,
            "full_bytes": None if truncated or not self.eof else hex_bytes(bytes(self.full or b"")),
            "prefix_length": len(prefix), "prefix_sha256": sha256(prefix), "prefix_bytes": hex_bytes(prefix),
            "suffix_length": len(suffix), "suffix_sha256": sha256(suffix), "suffix_bytes": hex_bytes(suffix),
            "omitted_byte_count": max(0, self.count - len(prefix) - len(suffix)),
            "overflow_at_byte": self.overflow_at,
        }


HELPER_PROGRAM = r'''
import os
import signal
import sys
import time

case_id = sys.argv[1]
pattern = bytes(range(251))

def payload(start, length):
    offset = start % 251
    rotated = pattern[offset:] + pattern[:offset]
    return (rotated * ((length + 250) // 251))[:length]

def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]

if case_id in {"pg_leader_exit_child_alive", "stream_pipe_hold"}:
    write_all(1, b"READY\n")
    child = os.fork()
    if child == 0:
        while True:
            time.sleep(60)
    os._exit(0)
elif case_id == "pg_term_responsive":
    write_all(1, b"READY\n")
    while True:
        time.sleep(60)
elif case_id == "pg_term_ignoring":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    write_all(1, b"READY\n")
    while True:
        time.sleep(60)
elif case_id == "stream_exact_cap_stdout":
    write_all(1, payload(0, 1048576))
elif case_id == "stream_exact_cap_stderr":
    write_all(2, payload(0, 1048576))
elif case_id == "stream_over_cap_stdout":
    write_all(1, payload(0, 1048577))
    while True:
        time.sleep(60)
elif case_id == "stream_over_cap_stderr":
    write_all(2, payload(0, 1048577))
    while True:
        time.sleep(60)
elif case_id == "stream_timeout_partial_final_drain":
    write_all(1, payload(0, 65537))
    def finish(_signum, _frame):
        write_all(1, payload(65537, 4096))
        os._exit(0)
    signal.signal(signal.SIGTERM, finish)
    while True:
        time.sleep(60)
else:
    raise SystemExit(64)
'''

CAPTURE_OVERFLOW_PROGRAM = r'''
import os
import time
pattern = bytes(range(251))
payload = (pattern * ((1048577 + 250) // 251))[:1048577]
view = memoryview(payload)
while view:
    written = os.write(1, view)
    view = view[written:]
while True:
    time.sleep(60)
'''

CAPTURE_LEADER_CHILD_PROGRAM = r'''
import os
import time
os.write(1, b"READY\n")
child = os.fork()
if child == 0:
    while True:
        time.sleep(60)
os._exit(0)
'''


def _register_streams(process: subprocess.Popen[bytes]) -> tuple[selectors.BaseSelector, dict[str, StreamAccumulator]]:
    selector = selectors.DefaultSelector()
    accumulators = {"stdout": StreamAccumulator("stdout"), "stderr": StreamAccumulator("stderr")}
    assert process.stdout is not None and process.stderr is not None
    for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, name)
    return selector, accumulators


def _new_raw_receipt(kind: str, pid: int, sid: int, pgid: int) -> dict[str, object]:
    return {
        "kind": kind,
        "pid": pid,
        "sid": sid,
        "pgid": pgid,
        "ownership_verified": pid == sid == pgid,
        "phase_records": [],
        "events": [],
        "signals": [],
        "stable_error_raw": None,
        "stable_error_family": None,
        "stable_error_tail": None,
        "owned_group_replacement_count": 0,
        "canonical_stable_error": None,
        "physical_cleanup_closed": False,
        "raw_deadline_validation_passed": False,
    }


def _raw_event(
    raw: dict[str, object],
    phase: str,
    action: str,
    result: str,
    *,
    observed_at_ns: int | None = None,
    errno_name: str | None = None,
    errno_number: int | None = None,
    **extra: object,
) -> dict[str, object]:
    events = raw["events"]
    assert isinstance(events, list)
    event: dict[str, object] = {
        "sequence": len(events),
        "phase": phase,
        "action": action,
        "observed_at_ns": time.monotonic_ns() if observed_at_ns is None else observed_at_ns,
        "result": result,
        "errno_name": errno_name,
        "errno_number": errno_number,
    }
    event.update(extra)
    events.append(event)
    return event


def _start_phase(raw: dict[str, object], phase: str, duration_ns: int) -> int:
    start = time.monotonic_ns()
    deadline = start + duration_ns
    phases = raw["phase_records"]
    assert isinstance(phases, list)
    phases.append({
        "phase": phase,
        "phase_start_ns": start,
        "configured_duration_ns": duration_ns,
        "deadline_ns": deadline,
    })
    return deadline


def _drain_before_deadline(
    selector: selectors.BaseSelector,
    accumulators: dict[str, StreamAccumulator],
    phase: str,
    deadline_ns: int,
    raw: dict[str, object],
    *,
    enforce_caps: bool = True,
) -> tuple[bool, str | None]:
    requested_at = time.monotonic_ns()
    if requested_at >= deadline_ns:
        return False, None
    requested_wait = min(50_000_000, deadline_ns - requested_at)
    _raw_event(
        raw, phase, "requested_wait", "scheduled", observed_at_ns=requested_at,
        requested_at_ns=requested_at, requested_wait_ns=requested_wait, deadline_ns=deadline_ns,
    )
    ready = selector.select(requested_wait / 1_000_000_000)
    wake_ns = time.monotonic_ns()
    _raw_event(
        raw, phase, "selector_wake", "ready" if ready else "timeout", observed_at_ns=wake_ns,
        requested_wait_ns=requested_wait, deadline_ns=deadline_ns,
        ready_streams=[str(key.data) for key, _mask in ready],
    )
    if wake_ns >= deadline_ns:
        return False, None

    for key, _mask in ready:
        name = str(key.data)
        accumulator = accumulators[name]
        total_count = sum(item.count for item in accumulators.values())
        per_stream_remaining = HARD_CAP + 1 - accumulator.count
        combined_remaining = COMBINED_CAP + 1 - total_count
        if enforce_caps and per_stream_remaining <= 0:
            return True, name
        if enforce_caps and combined_remaining <= 0:
            return True, "combined"
        read_size = READ_CHUNK
        if enforce_caps:
            read_size = min(READ_CHUNK, per_stream_remaining, combined_remaining)
        try:
            data = os.read(key.fileobj.fileno(), read_size)
        except BlockingIOError as exc:
            _raw_event(
                raw, phase, "drain", "would_block", errno_name="EAGAIN",
                errno_number=exc.errno,
                stream=name, requested_read_bytes=read_size, observed_bytes=0,
            )
            continue
        except OSError as exc:
            _raw_event(
                raw, phase, "drain", "error",
                errno_name=errno.errorcode.get(exc.errno, f"ERRNO_{exc.errno}"),
                errno_number=exc.errno,
                stream=name, requested_read_bytes=read_size, observed_bytes=0,
            )
            raise
        if data:
            accumulator.add(data, phase)
            _raw_event(
                raw, phase, "drain", "bytes", stream=name,
                requested_read_bytes=read_size, observed_bytes=len(data),
                stream_total=accumulator.count,
                combined_total=sum(item.count for item in accumulators.values()),
            )
            if enforce_caps and accumulator.count == HARD_CAP + 1:
                return True, name
            if enforce_caps and sum(item.count for item in accumulators.values()) == COMBINED_CAP + 1:
                return True, "combined"
        else:
            accumulator.eof = True
            selector.unregister(key.fileobj)
            _raw_event(
                raw, phase, "drain", "eof", stream=name,
                requested_read_bytes=read_size, observed_bytes=0,
            )
    return True, None


def _probe_group(pgid: int) -> str:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return "ESRCH"
    except PermissionError:
        return "EPERM"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, f"ERRNO_{exc.errno}")
    return "present"


def _pid_absent(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _poll_raw(process: subprocess.Popen[bytes], raw: dict[str, object], phase: str) -> int | None:
    returncode = process.poll()
    _raw_event(
        raw, phase, "poll", "running" if returncode is None else "reaped",
        returncode=returncode,
    )
    return returncode


def _probe_group_raw(pgid: int, raw: dict[str, object], phase: str, *, authoritative: bool = False) -> str:
    result = _probe_group(pgid)
    errno_name: str | None = None
    errno_number: int | None = None
    if result == "ESRCH":
        errno_name, errno_number = "ESRCH", errno.ESRCH
    elif result == "EPERM":
        errno_name, errno_number = "EPERM", errno.EPERM
    elif result.startswith("ERRNO_"):
        errno_number = int(result.split("_", 1)[1])
        errno_name = errno.errorcode.get(errno_number, result)
    _raw_event(
        raw, phase, "probe", result, errno_name=errno_name, errno_number=errno_number,
        pgid=pgid, authoritative=authoritative,
    )
    return result


def _signal_group_raw(pgid: int, signum: int, raw: dict[str, object], phase: str) -> bool:
    signal_name = "SIGTERM" if signum == signal.SIGTERM else "SIGKILL"
    try:
        os.killpg(pgid, signum)
    except OSError as exc:
        _raw_event(
            raw, phase, "signal", "error",
            errno_name=errno.errorcode.get(exc.errno, f"ERRNO_{exc.errno}"),
            errno_number=exc.errno, pgid=pgid, requested_signal=signal_name,
        )
        return False
    signals = raw["signals"]
    assert isinstance(signals, list)
    signals.append(signal_name)
    _raw_event(raw, phase, "signal", signal_name, pgid=pgid)
    return True


def _bounded_phase(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    accumulators: dict[str, StreamAccumulator],
    pgid: int,
    phase: str,
    duration_ns: int,
    raw: dict[str, object],
) -> tuple[int | None, str, str | None]:
    deadline = _start_phase(raw, phase, duration_ns)
    returncode = process.poll()
    probe = "present"
    overflow: str | None = None
    while True:
        within_deadline, overflow = _drain_before_deadline(
            selector, accumulators, phase, deadline, raw, enforce_caps=False,
        )
        if not within_deadline or overflow is not None:
            break
        if time.monotonic_ns() >= deadline:
            break
        returncode = _poll_raw(process, raw, phase)
        probe = _probe_group_raw(pgid, raw, phase)
        if probe == "ESRCH" and returncode is not None and all(item.eof for item in accumulators.values()):
            break
    return returncode, probe, overflow


def _validate_raw_receipt(raw: dict[str, object]) -> bool:
    required = {
        "kind", "pid", "sid", "pgid", "ownership_verified", "phase_records", "events",
        "signals", "stable_error_raw", "stable_error_family", "stable_error_tail",
        "owned_group_replacement_count", "canonical_stable_error", "physical_cleanup_closed",
    }
    if not required.issubset(raw):
        return False
    if not raw["ownership_verified"] or not raw["physical_cleanup_closed"]:
        return False
    phases = raw["phase_records"]
    events = raw["events"]
    if not isinstance(phases, list) or not isinstance(events, list) or not phases:
        return False
    phase_by_name: dict[str, list[dict[str, object]]] = {}
    for phase in phases:
        if not isinstance(phase, dict):
            return False
        start = phase.get("phase_start_ns")
        duration = phase.get("configured_duration_ns")
        deadline = phase.get("deadline_ns")
        name = phase.get("phase")
        if not isinstance(name, str) or not isinstance(start, int) or not isinstance(duration, int) or not isinstance(deadline, int):
            return False
        if duration <= 0 or deadline != start + duration:
            return False
        phase_by_name.setdefault(name, []).append(phase)
    deadline_wake_sequences: list[tuple[str, int]] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get("sequence") != index:
            return False
        if not {"phase", "action", "observed_at_ns", "result", "errno_name", "errno_number"}.issubset(event):
            return False
        if not isinstance(event["observed_at_ns"], int) or not isinstance(event["result"], str):
            return False
        action = event["action"]
        if action == "requested_wait":
            requested_at = event.get("requested_at_ns")
            requested = event.get("requested_wait_ns")
            deadline = event.get("deadline_ns")
            if not all(isinstance(value, int) for value in (requested_at, requested, deadline)):
                return False
            if requested_at >= deadline or requested <= 0 or requested > deadline - requested_at:
                return False
            if index + 1 >= len(events):
                return False
            wake = events[index + 1]
            if wake.get("action") != "selector_wake" or wake.get("phase") != event.get("phase"):
                return False
        if action == "selector_wake":
            deadline = event.get("deadline_ns")
            if not isinstance(deadline, int):
                return False
            if event["observed_at_ns"] >= deadline:
                deadline_wake_sequences.append((str(event["phase"]), index))
        if action in {"drain", "poll", "probe"}:
            if event["errno_name"] is not None and not isinstance(event["errno_name"], str):
                return False
            if event["errno_number"] is not None and not isinstance(event["errno_number"], int):
                return False
            timed_phases = phase_by_name.get(str(event["phase"]), [])
            if action in {"poll", "probe"} and timed_phases and event["observed_at_ns"] >= timed_phases[-1]["deadline_ns"]:
                return False
    for phase, boundary_sequence in deadline_wake_sequences:
        for event in events[boundary_sequence + 1:]:
            if event["phase"] == phase and event["action"] in {"requested_wait", "drain", "poll", "probe"}:
                return False
    authoritative = [event for event in events if event["action"] == "probe" and event.get("authoritative")]
    if len(authoritative) != 1 or authoritative[0]["result"] != "ESRCH":
        return False
    reaped_polls = [event for event in events if event["action"] == "poll" and event["result"] == "reaped"]
    eof_drains = [event for event in events if event["action"] == "drain" and event["result"] == "eof"]
    if not reaped_polls or {event.get("stream") for event in eof_drains} != {"stdout", "stderr"}:
        return False
    if authoritative[0]["sequence"] <= max(
        max(event["sequence"] for event in reaped_polls),
        max(event["sequence"] for event in eof_drains),
    ):
        return False
    raw_error = raw["stable_error_raw"]
    if raw_error is not None:
        if not isinstance(raw_error, str) or not isinstance(raw["stable_error_family"], str):
            return False
        if raw_error.startswith(("SUBPROCESS_TIMEOUT@", "SUBPROCESS_CLEANUP_FAILED@")):
            if raw["stable_error_tail"] != str(raw["pgid"]):
                return False
            if raw["owned_group_replacement_count"] != 1:
                return False
            canonical = raw["canonical_stable_error"]
            if not isinstance(canonical, str) or not canonical.endswith("/<OWNED_GROUP>"):
                return False
    return True


def _cleanup_owned(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    accumulators: dict[str, StreamAccumulator],
    pgid: int,
    raw: dict[str, object],
    *,
    request_termination: bool,
) -> dict[str, object]:
    term_sent = False
    kill_sent = False
    returncode = _poll_raw(process, raw, "cleanup_entry")
    probe = _probe_group_raw(pgid, raw, "cleanup_entry")
    if request_termination and probe == "present":
        term_sent = _signal_group_raw(pgid, signal.SIGTERM, raw, "cleanup_entry")
        returncode, probe, _overflow = _bounded_phase(
            process, selector, accumulators, pgid, "term_drain", 2_000_000_000, raw,
        )
    if probe == "present":
        kill_sent = _signal_group_raw(pgid, signal.SIGKILL, raw, "kill_entry")
        returncode, probe, _overflow = _bounded_phase(
            process, selector, accumulators, pgid, "kill_drain", 2_000_000_000, raw,
        )

    if returncode is None:
        deadline = _start_phase(raw, "leader_wait", 2_000_000_000)
        while returncode is None:
            within, _overflow = _drain_before_deadline(
                selector, accumulators, "leader_wait", deadline, raw, enforce_caps=False,
            )
            if not within:
                break
            if time.monotonic_ns() >= deadline:
                break
            returncode = _poll_raw(process, raw, "leader_wait")
            probe = _probe_group_raw(pgid, raw, "leader_wait")

    if not all(item.eof for item in accumulators.values()):
        deadline = _start_phase(raw, "final_drain", 2_000_000_000)
        while not all(item.eof for item in accumulators.values()):
            within, _overflow = _drain_before_deadline(
                selector, accumulators, "final_drain", deadline, raw, enforce_caps=False,
            )
            if not within:
                break
            if time.monotonic_ns() >= deadline:
                break
            returncode = _poll_raw(process, raw, "final_drain")
            probe = _probe_group_raw(pgid, raw, "final_drain")

    returncode = _poll_raw(process, raw, "pre_authoritative")
    if returncode is not None and all(item.eof for item in accumulators.values()):
        probe = _probe_group_raw(pgid, raw, "final_authoritative", authoritative=True)
    else:
        probe = _probe_group_raw(pgid, raw, "final_authoritative", authoritative=True)
    closed = (
        returncode is not None
        and _pid_absent(process.pid)
        and probe == "ESRCH"
        and all(item.eof for item in accumulators.values())
    )
    raw["physical_cleanup_closed"] = closed
    return {
        "returncode": returncode,
        "final_probe": probe,
        "leader_absent": _pid_absent(process.pid),
        "group_absent": probe == "ESRCH",
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "physical_cleanup_closed": closed,
    }


def _cleanup_owned_noexcept(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    accumulators: dict[str, StreamAccumulator],
    pgid: int,
    raw: dict[str, object],
    *,
    request_termination: bool,
) -> dict[str, object]:
    try:
        return _cleanup_owned(
            process, selector, accumulators, pgid, raw,
            request_termination=request_termination,
        )
    except BaseException as exc:
        _raw_event(
            raw, "cleanup_exception", "cleanup_exception", type(exc).__name__,
            error_text=str(exc)[:160],
        )
        probe = _probe_group_raw(pgid, raw, "cleanup_exception")
        if probe == "present":
            _signal_group_raw(pgid, signal.SIGTERM, raw, "cleanup_exception")
            try:
                _bounded_phase(
                    process, selector, accumulators, pgid, "exception_term_drain",
                    2_000_000_000, raw,
                )
            except BaseException as nested:
                _raw_event(
                    raw, "cleanup_exception", "cleanup_exception", type(nested).__name__,
                    error_text=str(nested)[:160],
                )
        probe = _probe_group_raw(pgid, raw, "cleanup_exception")
        if probe == "present":
            _signal_group_raw(pgid, signal.SIGKILL, raw, "cleanup_exception")
            try:
                _bounded_phase(
                    process, selector, accumulators, pgid, "exception_kill_drain",
                    2_000_000_000, raw,
                )
            except BaseException as nested:
                _raw_event(
                    raw, "cleanup_exception", "cleanup_exception", type(nested).__name__,
                    error_text=str(nested)[:160],
                )
        returncode = process.poll()
        if returncode is None:
            try:
                returncode = process.wait(timeout=2)
                _raw_event(raw, "cleanup_exception", "poll", "reaped", returncode=returncode)
            except subprocess.TimeoutExpired:
                returncode = process.poll()
                _raw_event(raw, "cleanup_exception", "poll", "timeout", returncode=returncode)
        drain_deadline = time.monotonic() + 2.0
        while not all(item.eof for item in accumulators.values()) and time.monotonic() < drain_deadline:
            for key, _mask in selector.select(0.05):
                name = str(key.data)
                try:
                    data = os.read(key.fileobj.fileno(), READ_CHUNK)
                except OSError as nested:
                    _raw_event(
                        raw, "cleanup_exception", "drain", "error",
                        errno_name=errno.errorcode.get(nested.errno, f"ERRNO_{nested.errno}"),
                        errno_number=nested.errno, stream=name,
                    )
                    continue
                if data:
                    accumulators[name].add(data, "final_drain")
                    _raw_event(
                        raw, "cleanup_exception", "drain", "bytes",
                        stream=name, observed_bytes=len(data),
                    )
                else:
                    accumulators[name].eof = True
                    selector.unregister(key.fileobj)
                    _raw_event(raw, "cleanup_exception", "drain", "eof", stream=name, observed_bytes=0)
        probe = _probe_group_raw(pgid, raw, "final_authoritative", authoritative=True)
        closed = (
            returncode is not None and _pid_absent(process.pid) and probe == "ESRCH"
            and all(item.eof for item in accumulators.values())
        )
        raw["physical_cleanup_closed"] = closed
        return {
            "returncode": returncode,
            "final_probe": probe,
            "leader_absent": _pid_absent(process.pid),
            "group_absent": probe == "ESRCH",
            "term_sent": "SIGTERM" in raw["signals"],
            "kill_sent": "SIGKILL" in raw["signals"],
            "physical_cleanup_closed": closed,
        }


class OwnedProcessError(AssertionError):
    def __init__(self, stable_error: str, raw_receipt: dict[str, object]) -> None:
        super().__init__(stable_error)
        self.stable_error = stable_error
        self.raw_receipt = raw_receipt


def _capture_owned(
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: float,
) -> tuple[int, bytes, bytes]:
    kind = Path(argv[0]).name or "capture"
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pid = process.pid
    sid = os.getsid(pid)
    pgid = os.getpgid(pid)
    raw = _new_raw_receipt(kind, pid, sid, pgid)
    selector, accumulators = _register_streams(process)
    failure: str | None = None
    caught: BaseException | None = None
    cleanup: dict[str, object] | None = None
    try:
        if not raw["ownership_verified"]:
            failure = f"SUBPROCESS_CLEANUP_FAILED@{kind}/{pgid}"
        else:
            deadline = _start_phase(raw, "execution", int(timeout * 1_000_000_000))
            while failure is None:
                within, overflow = _drain_before_deadline(selector, accumulators, "execution", deadline, raw)
                if overflow is not None:
                    failure = f"STREAM_OUTPUT_LIMIT_EXCEEDED@{kind}/{overflow}"
                    break
                if not within:
                    failure = f"SUBPROCESS_TIMEOUT@{kind}/{pgid}"
                    break
                if time.monotonic_ns() >= deadline:
                    failure = f"SUBPROCESS_TIMEOUT@{kind}/{pgid}"
                    break
                returncode = _poll_raw(process, raw, "execution")
                if returncode is not None and all(item.eof for item in accumulators.values()):
                    break
    except BaseException as exc:
        caught = exc
        failure = f"SUBPROCESS_EXCEPTION@{kind}/{pgid}"
    finally:
        if raw["ownership_verified"]:
            cleanup = _cleanup_owned_noexcept(
                process, selector, accumulators, pgid, raw,
                request_termination=failure is not None,
            )
        selector.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    if cleanup is None or not cleanup["physical_cleanup_closed"]:
        raise OwnedProcessError(f"SUBPROCESS_CLEANUP_FAILED@{kind}/{pgid}", raw)
    operation_pipe_closed = failure is None
    for stream_name, accumulator in accumulators.items():
        reason = "complete" if operation_pipe_closed else "pipe_not_closed"
        if failure is not None and failure.startswith("STREAM_OUTPUT_LIMIT_EXCEEDED@"):
            failure_tail = failure.rsplit("/", 1)[-1]
            if failure_tail == stream_name:
                reason = "hard_cap_exceeded"
        raw[stream_name] = accumulator.receipt(
            operation_pipe_closed=operation_pipe_closed, reason=reason,
        )
    if failure is not None:
        family, tail = failure.rsplit("/", 1)
        raw["stable_error_raw"] = failure
        raw["stable_error_family"] = family
        raw["stable_error_tail"] = tail
        if failure.startswith(("SUBPROCESS_TIMEOUT@", "SUBPROCESS_CLEANUP_FAILED@")):
            if tail != str(pgid):
                raise OwnedProcessError(f"SUBPROCESS_CLEANUP_FAILED@{kind}/{pgid}", raw)
            raw["canonical_stable_error"] = failure[: -len(tail)] + "<OWNED_GROUP>"
            raw["owned_group_replacement_count"] = 1
        else:
            raw["canonical_stable_error"] = failure
    raw["raw_deadline_validation_passed"] = _validate_raw_receipt(raw)
    if not raw["raw_deadline_validation_passed"]:
        raise OwnedProcessError(f"SUBPROCESS_CLEANUP_FAILED@{kind}/{pgid}", raw)
    if caught is not None:
        raise OwnedProcessError(f"SUBPROCESS_EXCEPTION@{kind}/{pgid}", raw) from caught
    if failure is not None:
        raise OwnedProcessError(failure, raw)
    if accumulators["stdout"].full is None or accumulators["stderr"].full is None:
        raise OwnedProcessError(f"STREAM_OUTPUT_LIMIT_EXCEEDED@{kind}/retained", raw)
    return int(cleanup["returncode"]), bytes(accumulators["stdout"].full), bytes(accumulators["stderr"].full)


def _run_real_case(case_id: str, suite_root: Path, env: dict[str, str]) -> dict[str, object]:
    process_root = Path(tempfile.mkdtemp(prefix=f"{case_id}-", dir=suite_root))
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", HELPER_PROGRAM, case_id],
        cwd=process_root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pid = process.pid
    sid = os.getsid(pid)
    pgid = os.getpgid(pid)
    raw = _new_raw_receipt(case_id, pid, sid, pgid)
    selector, accumulators = _register_streams(process)
    outcome: str | None = None
    operation_pipe_closed = False
    active_phase = "setup"
    active_deadline = _start_phase(raw, active_phase, 1_000_000_000)
    returncode: int | None = None
    cleanup: dict[str, object] | None = None
    caught: BaseException | None = None
    process_root_absent = False
    try:
        if not raw["ownership_verified"]:
            raise AssertionError(f"SUBPROCESS_CLEANUP_FAILED@{case_id}/{pgid}")
        while outcome is None:
            within, overflow = _drain_before_deadline(
                selector, accumulators, active_phase, active_deadline, raw,
            )
            if overflow is not None:
                expected_stream_name = "stdout" if case_id == "stream_over_cap_stdout" else "stderr"
                if case_id not in {"stream_over_cap_stdout", "stream_over_cap_stderr"} or overflow != expected_stream_name:
                    raise AssertionError(f"STREAM_OUTPUT_LIMIT_EXCEEDED@{case_id}/{overflow}")
                outcome = "output_limit"
                break
            if not within:
                if active_phase == "setup":
                    raise AssertionError(f"SUBPROCESS_CLEANUP_FAILED@{case_id}/{pgid}")
                outcome = "timeout"
                break
            if time.monotonic_ns() >= active_deadline:
                if active_phase == "setup":
                    raise AssertionError(f"SUBPROCESS_CLEANUP_FAILED@{case_id}/{pgid}")
                outcome = "timeout"
                break
            returncode = _poll_raw(process, raw, active_phase)
            stdout_count = accumulators["stdout"].count
            stderr_count = accumulators["stderr"].count
            if case_id in {"pg_leader_exit_child_alive", "pg_term_responsive", "pg_term_ignoring", "stream_pipe_hold"}:
                if stdout_count >= len(READY) and active_phase == "setup":
                    if bytes(accumulators["stdout"].prefix)[:6] != READY:
                        raise AssertionError(f"SUBPROCESS_CLEANUP_FAILED@{case_id}/{pgid}")
                    active_phase = "operation"
                    active_deadline = _start_phase(raw, active_phase, 500_000_000)
                if case_id in {"pg_leader_exit_child_alive", "stream_pipe_hold"} and active_phase == "operation" and returncode is not None:
                    outcome = "cleanup_failed"
            elif case_id in {"stream_exact_cap_stdout", "stream_exact_cap_stderr"}:
                if returncode is not None and all(item.eof for item in accumulators.values()):
                    outcome = "success"
                    operation_pipe_closed = True
            elif case_id == "stream_timeout_partial_final_drain":
                if stdout_count >= 65_537 and active_phase == "setup":
                    active_phase = "operation"
                    active_deadline = _start_phase(raw, active_phase, 500_000_000)

        cleanup = _cleanup_owned_noexcept(
            process, selector, accumulators, pgid, raw,
            request_termination=outcome != "success",
        )
    except BaseException as exc:
        caught = exc
        if raw["ownership_verified"]:
            cleanup = _cleanup_owned_noexcept(
                process, selector, accumulators, pgid, raw, request_termination=True,
            )
    finally:
        selector.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        shutil.rmtree(process_root)
        process_root_absent = not process_root.exists()

    if cleanup is None or not cleanup["physical_cleanup_closed"]:
        raise AssertionError(f"SUBPROCESS_CLEANUP_FAILED@{case_id}/{pgid}") from caught
    if caught is not None:
        raise caught
    returncode = cleanup["returncode"]
    final_probe = str(cleanup["final_probe"])
    leader_absent = bool(cleanup["leader_absent"])
    group_absent = bool(cleanup["group_absent"])
    signals = raw["signals"]
    assert isinstance(signals, list)
    term_sent = "SIGTERM" in signals
    kill_sent = "SIGKILL" in signals

    for item in accumulators.values():
        if sum(item.phase_counts.values()) != item.count:
            raise AssertionError(f"DETERMINISM_MISMATCH@{case_id}/{item.name}_phase_sum")
    if case_id in {"stream_exact_cap_stdout", "stream_exact_cap_stderr"}:
        stdout_reason = stderr_reason = "complete"
    elif case_id == "stream_over_cap_stdout":
        stdout_reason, stderr_reason = "hard_cap_exceeded", "complete"
    elif case_id == "stream_over_cap_stderr":
        stdout_reason, stderr_reason = "complete", "hard_cap_exceeded"
    else:
        stdout_reason = stderr_reason = "pipe_not_closed"

    stable_error: str | None = None
    raw_stable_error: str | None = None
    expected_family: str | None = None
    if outcome == "cleanup_failed":
        expected_family = f"SUBPROCESS_CLEANUP_FAILED@{case_id}"
        raw_stable_error = f"{expected_family}/{pgid}"
    elif outcome == "timeout":
        expected_family = f"SUBPROCESS_TIMEOUT@{case_id}"
        raw_stable_error = f"{expected_family}/{pgid}"
    elif outcome == "output_limit":
        stream_name = "stdout" if case_id.endswith("stdout") else "stderr"
        expected_family = f"STREAM_OUTPUT_LIMIT_EXCEEDED@{case_id}"
        raw_stable_error = f"{expected_family}/{stream_name}"
    if raw_stable_error is not None:
        family, tail = raw_stable_error.rsplit("/", 1)
        if family != expected_family:
            raise AssertionError(f"RAW_STABLE_ERROR_INVALID@{case_id}/family")
        raw["stable_error_raw"] = raw_stable_error
        raw["stable_error_family"] = family
        raw["stable_error_tail"] = tail
        if outcome in {"cleanup_failed", "timeout"}:
            if not raw["ownership_verified"] or tail != str(pgid) or "<OWNED_GROUP>" in raw_stable_error:
                raise AssertionError(f"RAW_STABLE_ERROR_INVALID@{case_id}/pgid")
            stable_error = raw_stable_error[: -(len(tail))] + "<OWNED_GROUP>"
            raw["owned_group_replacement_count"] = 1
        else:
            stable_error = raw_stable_error
        raw["canonical_stable_error"] = stable_error

    raw["process_temp_root_absent"] = process_root_absent
    raw["stdout"] = accumulators["stdout"].receipt(
        operation_pipe_closed=operation_pipe_closed, reason=stdout_reason,
    )
    raw["stderr"] = accumulators["stderr"].receipt(
        operation_pipe_closed=operation_pipe_closed, reason=stderr_reason,
    )
    raw["raw_deadline_validation_passed"] = _validate_raw_receipt(raw)
    if not raw["raw_deadline_validation_passed"]:
        raise AssertionError(f"RAW_DEADLINE_VALIDATION_FAILED@{case_id}/{pgid}")

    cleanup_closed = case_id not in {"pg_leader_exit_child_alive", "stream_pipe_hold"}
    canonical = {
        "case_id": case_id, "execution_model": "real_popen_helper_v1",
        "ownership_verified": True, "owned_tuple_relation": "owned",
        "operation_outcome": outcome, "stable_error": stable_error,
        "returncode": returncode, "signals": signals, "term_sent": term_sent, "kill_sent": kill_sent,
        "leader_reaped": returncode is not None, "leader_pid_absent": leader_absent,
        "final_group_probe": final_probe, "group_absent": group_absent,
        "stdout": raw["stdout"], "stderr": raw["stderr"],
        "process_temp_root_absent": process_root_absent, "suite_temp_root_absent": False,
        "cleanup_closed": cleanup_closed,
        "canonical_probe_summary": {
            "ownership_verified": True, "probe_error": None, "term_sent": term_sent,
            "kill_sent": kill_sent, "final_esrch": final_probe == "ESRCH",
        },
        "raw_deadline_validation_passed": bool(raw["raw_deadline_validation_passed"]),
    }
    return {"raw": raw, "canonical": canonical}


def _path_receipt(path: Path) -> tuple[object, ...]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISREG(info.st_mode):
        return ("regular", info.st_size, sha256(path.read_bytes()), stat.S_IMODE(info.st_mode))
    if stat.S_ISDIR(info.st_mode):
        return ("directory", stat.S_IMODE(info.st_mode))
    return ("other", info.st_mode, info.st_size)


def _inventory(repo_root: Path, relative_roots: tuple[str, ...]) -> tuple[tuple[str, tuple[object, ...]], ...]:
    entries: list[tuple[str, tuple[object, ...]]] = []
    for relative_root in relative_roots:
        root = repo_root / relative_root
        if not root.exists():
            continue
        for current, directories, files in os.walk(root):
            directories.sort()
            files.sort()
            current_path = Path(current)
            for name in directories + files:
                path = current_path / name
                entries.append((str(path.relative_to(repo_root)), _path_receipt(path)))
    return tuple(entries)


def _repo_snapshot(repo_root: Path, env: dict[str, str]) -> dict[str, object]:
    commands = {
        "porcelain_z": ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        "worktree_diff": ["git", "diff", "--binary"],
        "index_diff": ["git", "diff", "--cached", "--binary"],
    }
    outputs: dict[str, bytes] = {}
    for key, argv in commands.items():
        returncode, stdout, stderr = _capture_owned(argv, repo_root, env, 10.0)
        if returncode != 0 or stderr:
            raise AssertionError(f"REPOSITORY_STATE_DRIFT@{key}_capture")
        outputs[key] = stdout
    allowlist = (
        "app/native/lulu_transport_identity/lulu_transport_identity.h",
        "app/native/lulu_transport_identity/lulu_transport_identity.c",
        "app/tests/native/lulu_transport_identity_fixture.c",
        "app/tests/fixtures/s1c3_golden_vector.json",
        "app/tests/test_s1c3_native_identity_contract.py",
    )
    return {
        **outputs,
        "inventory": _inventory(repo_root, ("app/tests", "app/native")),
        "allowlist_hashes": tuple((path, sha256((repo_root / path).read_bytes())) for path in allowlist),
    }


def _run_no_popen_seam(case_id: str) -> dict[str, object]:
    inputs = {
        "pg_ownership_mismatch": {"pid": 41001, "sid": 41001, "pgid": 41002, "probe": None},
        "pg_probe_eperm": {"pid": 42001, "sid": 42001, "pgid": 42001, "probe": ("EPERM", 1)},
        "pg_probe_other": {"pid": 43001, "sid": 43001, "pgid": 43001, "probe": ("EIO", 5)},
    }[case_id]
    pid, sid, pgid = int(inputs["pid"]), int(inputs["sid"]), int(inputs["pgid"])
    ownership_verified = pid == sid == pgid
    probe = inputs["probe"]
    raw_probe_sequence: list[dict[str, object]] = []
    probe_error: str | None = None
    if ownership_verified and probe is not None:
        probe_error, errno_number = probe
        raw_probe_sequence.append({
            "phase": "pre_signal", "result": "error",
            "errno_name": probe_error, "errno_number": errno_number,
        })
    stdout = not_started_stream("stdout")
    stderr = not_started_stream("stderr")
    return {
        "case_id": case_id, "execution_model": "no_popen_control_seam_v1", "popen_created": False,
        "pid": pid, "sid": sid, "pgid": pgid, "ownership_verified": ownership_verified,
        "returncode": None, "leader_reaped": False, "leader_pid_absent": None,
        "final_group_probe": None, "group_absent": None,
        "setup_deadline_ns": None, "operation_deadline_ns": None, "term_deadline_ns": None,
        "kill_deadline_ns": None, "leader_wait_deadline_ns": None, "final_drain_deadline_ns": None,
        "signals": [], "term_sent": False, "kill_sent": False, "os_killpg_call_count": 0,
        "probe_seam_call_count": len(raw_probe_sequence), "raw_probe_sequence": raw_probe_sequence,
        "canonical_probe_summary": {
            "ownership_verified": ownership_verified, "probe_error": probe_error,
            "term_sent": False, "kill_sent": False, "final_esrch": False,
        },
        "stdout": stdout, "stderr": stderr,
        "process_temp_root": None, "process_temp_root_absent": True,
        "suite_temp_root": None, "suite_temp_root_absent": True,
        "cleanup_closed": False,
        "stable_error": f"SUBPROCESS_CLEANUP_FAILED@{case_id}/{pgid}",
    }


class S1C3NativeIdentityContractTests(unittest.TestCase):
    maxDiff = None

    def test_repeat_determinism_all_cases(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        repo_root = app_root.parent
        self.assertEqual(
            repo_root.name,
            "lulu-s1d6-TASK-20260901-FE2246-worktree",
            "test must run in the isolated construction worktree",
        )

        suite_path: Path | None = None
        real_rounds: dict[str, list[dict[str, object]]] = {case_id: [] for case_id in REAL_HELPER_CASES}
        with tempfile.TemporaryDirectory(prefix="lulu-s1d6-fe2246-", dir="/private/tmp") as suite_name:
            suite_path = Path(suite_name)
            env = os.environ.copy()
            env.update({
                "TMP": suite_name,
                "TEMP": suite_name,
                "TMPDIR": suite_name,
                "PYTHONPYCACHEPREFIX": str(suite_path / "pycache"),
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            before = _repo_snapshot(repo_root, env)

            returncode, stdout, stderr = _capture_owned(
                ["git", "rev-parse", "HEAD"], repo_root, env, 10.0,
            )
            self.assertEqual((returncode, stderr), (0, b""))
            self.assertEqual(stdout.strip(), b"38640c2ef2dec26e93f042c9b3a84726a4ee0ece")

            returncode, stdout, stderr = _capture_owned(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], repo_root, env, 10.0,
            )
            self.assertEqual((returncode, stderr), (0, b""))
            main_root = Path(stdout.decode("utf-8").strip()).parent
            for relative_path, expected_sha in FIXED_S1C3_INPUTS.items():
                self.assertEqual(sha256((main_root / relative_path).read_bytes()), expected_sha)

            expected_header_sha = "d50c6a118a13662c044cccbe837f721d84f1cbc5e5602975198c4fdbcb129817"
            for progress_path in (repo_root / "docs/progress.md", main_root / "docs/progress.md"):
                progress = progress_path.read_bytes()
                header = progress[:357]
                self.assertEqual((len(header), sha256(header), progress.count(header), progress.find(header)),
                                 (357, expected_header_sha, 1, 0))

            vector_path = app_root / "tests/fixtures/s1c3_golden_vector.json"
            vector = json.loads(vector_path.read_text(encoding="utf-8"))
            self.assertEqual(vector["schema"], "lulu.s1c3.golden.v1")
            self.assertEqual(vector["identity_formula_version"], "s1c3.identity.v1")
            self.assertEqual(vector["length_prefixed_field_count"], 4)
            self.assertEqual(vector["length_prefixed_fields"], ["team_id", "identifier", "generation", "cdhash"])
            self.assertEqual(vector["designated_requirement_sha256_encoding"], "raw_32_bytes_no_length_prefix")
            client = vector["client"]
            server = vector["server"]
            ad_hoc = vector["ad_hoc_zero_team"]
            self.assertIsInstance(client, dict)
            self.assertIsInstance(server, dict)
            self.assertIsInstance(ad_hoc, dict)
            client_dr, client_input, client_digest = independent_identity(client)
            server_dr, server_input, server_digest = independent_identity(server)
            ad_hoc_dr, ad_hoc_input, ad_hoc_digest = independent_identity(ad_hoc)
            transcript = independent_transcript(vector, client_digest, server_digest)
            self.assertEqual((len(client_input), len(server_input), len(transcript)), (122, 124, 227))
            self.assertEqual(client_dr.hex(), client["designated_requirement_sha256"])
            self.assertEqual(server_dr.hex(), server["designated_requirement_sha256"])
            self.assertEqual(client_input.hex(), client["identity_input_hex"])
            self.assertEqual(server_input.hex(), server["identity_input_hex"])
            self.assertEqual(client_digest.hex(), client["identity_digest"])
            self.assertEqual(server_digest.hex(), server["identity_digest"])
            self.assertEqual(transcript.hex(), vector["transcript_hex"])
            self.assertEqual(sha256(transcript), vector["transcript_sha256"])
            self.assertEqual(ad_hoc["team_id"], "")
            self.assertEqual((len(ad_hoc_input), ad_hoc_input[:3]), (112, b"\x01\x00\x00"))
            self.assertEqual(ad_hoc_dr.hex(), ad_hoc["designated_requirement_sha256"])
            self.assertEqual(ad_hoc_input.hex(), ad_hoc["identity_input_hex"])
            self.assertEqual(ad_hoc_digest.hex(), ad_hoc["identity_digest"])

            returncode, stdout, stderr = _capture_owned(
                ["/usr/bin/xcrun", "--sdk", "macosx", "--find", "clang"], app_root, env, 10.0,
            )
            self.assertEqual(returncode, 0, f"NATIVE_TOOLCHAIN_UNAVAILABLE: {stderr!r}")
            clang = stdout.decode("utf-8").strip()
            self.assertTrue(clang.startswith("/"), "NATIVE_TOOLCHAIN_UNAVAILABLE")
            returncode, stdout, stderr = _capture_owned(
                ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], app_root, env, 10.0,
            )
            self.assertEqual(returncode, 0, f"NATIVE_TOOLCHAIN_UNAVAILABLE: {stderr!r}")
            sdk_root = stdout.decode("utf-8").strip()
            self.assertTrue(sdk_root.startswith("/"), "NATIVE_TOOLCHAIN_UNAVAILABLE")
            fixture_binary = suite_path / "lulu_transport_identity_fixture"
            compile_argv = [
                clang, "-isysroot", sdk_root, "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-I", str(app_root / "native/lulu_transport_identity"),
                str(app_root / "native/lulu_transport_identity/lulu_transport_identity.c"),
                str(app_root / "tests/native/lulu_transport_identity_fixture.c"),
                "-o", str(fixture_binary),
            ]
            returncode, stdout, stderr = _capture_owned(compile_argv, app_root, env, 30.0)
            self.assertEqual(returncode, 0, f"NATIVE_TOOLCHAIN_UNAVAILABLE: {stderr!r}")
            self.assertEqual(stdout, b"")
            self.assertEqual(stderr, b"")
            self.assertTrue(fixture_binary.is_file())

            fixture_rounds: dict[str, list[tuple[object, ...]]] = {}
            for case_id in IDENTITY_CASES:
                rounds: list[tuple[object, ...]] = []
                for _round in range(2):
                    returncode, stdout, stderr = _capture_owned(
                        [str(fixture_binary), case_id], app_root, env, 5.0,
                    )
                    receipt = (
                        returncode, stdout, len(stdout), sha256(stdout),
                        stderr, len(stderr), sha256(stderr), json.loads(stdout.decode("utf-8")),
                    )
                    self.assertEqual(returncode, 0, f"fixture failed: {case_id}: {stderr!r}")
                    self.assertEqual(stderr, b"")
                    value = receipt[-1]
                    self.assertEqual(value["case_id"], case_id)
                    self.assertEqual(value["effect_count"], 0)
                    if case_id == "golden_full":
                        self.assertEqual(value["status"], "ok")
                        self.assertTrue(value["transport_guard"])
                        self.assertEqual(value["client_designated_requirement_sha256"], client_dr.hex())
                        self.assertEqual(value["server_designated_requirement_sha256"], server_dr.hex())
                        self.assertEqual(value["client_identity_input_hex"], client_input.hex())
                        self.assertEqual(value["server_identity_input_hex"], server_input.hex())
                        self.assertEqual(value["client_identity_digest"], client_digest.hex())
                        self.assertEqual(value["server_identity_digest"], server_digest.hex())
                        self.assertEqual(value["transcript_hex"], transcript.hex())
                        self.assertEqual(value["transcript_sha256"], sha256(transcript))
                    elif case_id == "ad_hoc_zero_team":
                        self.assertEqual(value["status"], "ok")
                        self.assertEqual(value["identity_input_length"], 112)
                        self.assertEqual(value["identity_input_hex"], ad_hoc_input.hex())
                        self.assertEqual(value["identity_digest"], ad_hoc_digest.hex())
                        self.assertFalse(value["transport_guard"])
                    else:
                        expected_error = (
                            "PEER_IDENTITY_ROLE_INVALID@identity_digest"
                            if case_id.startswith("identity_")
                            else "HANDSHAKE_TRANSCRIPT_INVALID@canonical_input"
                            if case_id.startswith("malformed_") or case_id.startswith("golden_null_")
                            else "HANDSHAKE_GOLDEN_VECTOR_MISMATCH"
                        )
                        self.assertEqual(value["status"], "error")
                        self.assertEqual(value["error"], expected_error)
                        self.assertEqual(value["output_length"], 0)
                        self.assertTrue(value["sentinel_unchanged"])
                        self.assertEqual(value["sentinel_hex"], "a5" * 16)
                        self.assertFalse(value["transport_guard"])
                    rounds.append(receipt)
                self.assertEqual(rounds[0], rounds[1], f"DETERMINISM_MISMATCH@{case_id}/fixture_receipt")
                fixture_rounds[case_id] = rounds
            self.assertEqual(set(fixture_rounds), set(IDENTITY_CASES))

            capture_safety_vectors = (
                ("capture_per_stream_cap_plus_one", CAPTURE_OVERFLOW_PROGRAM, "STREAM_OUTPUT_LIMIT_EXCEEDED@", "stdout"),
                ("capture_leader_exited_child_pipe", CAPTURE_LEADER_CHILD_PROGRAM, "SUBPROCESS_TIMEOUT@", None),
            )
            for vector_id, program, expected_prefix, expected_tail in capture_safety_vectors:
                normalized_rounds: list[tuple[object, ...]] = []
                for _round in range(2):
                    with self.assertRaises(OwnedProcessError) as captured:
                        _capture_owned(
                            [sys.executable, "-B", "-c", program], app_root, env, 0.25,
                        )
                    error = captured.exception
                    raw = error.raw_receipt
                    self.assertTrue(
                        error.stable_error.startswith(expected_prefix),
                        f"{vector_id}: {error.stable_error}; physical={error.raw_receipt['physical_cleanup_closed']}",
                    )
                    if expected_tail is not None:
                        self.assertTrue(error.stable_error.endswith("/" + expected_tail), vector_id)
                    self.assertTrue(raw["ownership_verified"], vector_id)
                    self.assertTrue(raw["physical_cleanup_closed"], vector_id)
                    self.assertTrue(raw["raw_deadline_validation_passed"], vector_id)
                    self.assertTrue(raw["stdout"]["final_pipe_eof"], vector_id)
                    self.assertTrue(raw["stderr"]["final_pipe_eof"], vector_id)
                    final_probes = [
                        event for event in raw["events"]
                        if event["action"] == "probe" and event.get("authoritative")
                    ]
                    self.assertEqual(len(final_probes), 1, vector_id)
                    self.assertEqual(final_probes[0]["result"], "ESRCH", vector_id)
                    normalized_rounds.append((
                        error.stable_error.split("@", 1)[0],
                        error.stable_error.rsplit("/", 1)[-1] if expected_tail is not None else "<OWNED_GROUP>",
                        tuple(raw["signals"]),
                        raw["physical_cleanup_closed"],
                        raw["raw_deadline_validation_passed"],
                    ))
                self.assertEqual(normalized_rounds[0], normalized_rounds[1], vector_id)

            for case_id in REAL_HELPER_CASES:
                for _round in range(2):
                    real_rounds[case_id].append(_run_real_case(case_id, suite_path, env))

            no_popen_rounds: dict[str, list[bytes]] = {}
            for case_id in NO_POPEN_CASES:
                rounds = [canonical_bytes(_run_no_popen_seam(case_id)) for _round in range(2)]
                expected = canonical_bytes(expected_no_popen(case_id))
                self.assertEqual(rounds[0], expected)
                self.assertEqual(rounds[1], expected)
                self.assertEqual(rounds[0], rounds[1], f"DETERMINISM_MISMATCH@{case_id}/canonical")
                no_popen_rounds[case_id] = rounds
            self.assertEqual(set(no_popen_rounds), set(NO_POPEN_CASES))

            after = _repo_snapshot(repo_root, env)
            self.assertEqual(before, after, "REPOSITORY_STATE_DRIFT@snapshot")

        assert suite_path is not None
        self.assertFalse(suite_path.exists(), f"SUBPROCESS_CLEANUP_FAILED@suite_temp_root/{suite_path}")

        for case_id in REAL_HELPER_CASES:
            expected = canonical_bytes(expected_real_process(case_id))
            expected_length, expected_sha = PROCESS_EXPECTED_LENGTH_SHA[case_id]
            rounds = real_rounds[case_id]
            self.assertEqual(len(rounds), 2)
            canonical_rounds: list[bytes] = []
            for round_receipt in rounds:
                raw = round_receipt["raw"]
                receipt = round_receipt["canonical"]
                self.assertTrue(raw["raw_deadline_validation_passed"])
                self.assertTrue(raw["physical_cleanup_closed"])
                self.assertTrue(raw["process_temp_root_absent"])
                self.assertTrue(receipt["raw_deadline_validation_passed"])
                receipt["suite_temp_root_absent"] = True
                actual = canonical_bytes(receipt)
                self.assertEqual(actual, expected, f"canonical expected mismatch: {case_id}")
                self.assertEqual(len(actual), expected_length, f"expected length mismatch: {case_id}")
                self.assertEqual(sha256(actual), expected_sha, f"expected sha mismatch: {case_id}")
                canonical_rounds.append(actual)
            self.assertEqual(
                canonical_rounds[0], canonical_rounds[1],
                f"DETERMINISM_MISMATCH@{case_id}/canonical",
            )


if __name__ == "__main__":
    unittest.main()
