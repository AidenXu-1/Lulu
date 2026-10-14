import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from src.core.synthesis import runtime_packages as rp


class RuntimeHealthDeadlineTests(unittest.TestCase):
    def test_cold_import_can_finish_after_old_ninety_second_limit(self):
        child = Mock(returncode=0)
        child.poll.side_effect = [None, 0, 0]
        with patch.object(rp.subprocess, 'Popen', return_value=child), \
             patch.object(rp, '_runtime_python_in', return_value=Path('/fixture/python')), \
             patch.object(rp.time, 'monotonic', side_effect=[0, 95]), \
             patch.object(rp.time, 'sleep'), patch.object(rp.os, 'killpg') as kill:
            rp.check_runtime_health('local_qwen3_tts', Path('/fixture'))
            kill.assert_not_called()

    def test_check_remains_bounded_and_cancellable(self):
        for cancel, elapsed, error in ((False, 301, RuntimeError), (True, 1, rp.CancelledError)):
            with self.subTest(cancel=cancel):
                child = Mock(returncode=None)
                child.poll.return_value = None
                with patch.object(rp.subprocess, 'Popen', return_value=child), \
                     patch.object(rp, '_runtime_python_in', return_value=Path('/fixture/python')), \
                     patch.object(rp.time, 'monotonic', side_effect=[0, elapsed]), \
                     patch.object(rp.os, 'killpg'), self.assertRaises(error):
                    rp.check_runtime_health('local_qwen3_tts', Path('/fixture'), lambda:cancel)
                child.wait.assert_called_once()
