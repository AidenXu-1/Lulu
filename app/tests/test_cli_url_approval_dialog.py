from __future__ import annotations

import unittest
from io import BytesIO


class CliUrlApprovalDialogRedTests(unittest.TestCase):
    def test_no_real_pointer_click_cannot_confirm(self) -> None:
        from src.ui.cli_url_approval import PointerConfirmationGate

        gate = PointerConfirmationGate()

        self.assertFalse(gate.confirm(spontaneous=False, button="left"))
        self.assertFalse(gate.confirm(spontaneous=True, button="keyboard"))
        self.assertFalse(gate.confirm(spontaneous=True, button="programmatic"))
        self.assertFalse(gate.confirmed)

    def test_programmatic_confirmation_is_serialized_as_cancelled(self) -> None:
        from src.core.cli_runtime.link_host_bridge import (
            decode_host_frame,
            encode_host_frame,
        )
        from src.core.link_transcription_application import (
            LinkTranscriptionApplicationService,
        )
        from src.ui.cli_url_approval import run_link_approval_host

        plan = LinkTranscriptionApplicationService(
            installation_id="installation-dialog-test"
        ).prepare("https://example.com/dialog")
        request = {
            "schema_id": "lulu.link-approval-host-request/v1",
            "schema_revision": 1,
            "request_id": "request-dialog-test",
            "host_identity": {
                "bundle_id": "com.lulu.transcribe.trusted-test",
                "product_version": "test",
                "product_build": "test",
                "profile": "trusted_test",
                "source_commit": "test",
                "source_tree": "test",
                "identity_digest": "test",
                "signature_digest": "test",
            },
            "plan": plan,
        }
        output = BytesIO()

        exit_code = run_link_approval_host(
            stdin=BytesIO(encode_host_frame(request)),
            stdout=output,
            dialog_runner=lambda _plan: {
                "decision": "confirmed",
                "pointer_verified": False,
            },
        )

        self.assertEqual(exit_code, 0)
        response = decode_host_frame(output.getvalue())
        self.assertEqual(response["decision"], "cancelled")
        self.assertEqual(response["action_source"], "none")
        self.assertEqual(response["action_nonce"], "")


if __name__ == "__main__":
    unittest.main()
