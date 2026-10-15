import ctypes
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from types import SimpleNamespace
import unittest

from src.core.license.store import _MacOSExplicitKeyring


class MacOSCredentialReadTests(unittest.TestCase):
    def backend(self, read, *, decode=lambda _data: "value"):
        backend = object.__new__(_MacOSExplicitKeyring)
        refs = {"keychain": object(), "search": object(), "query": object()}
        released = []
        backend._keychain_ref = lambda: refs["keychain"]
        backend._search_list = lambda _keychain: refs["search"]
        backend._release = lambda ref: released.append(
            ref.value if isinstance(ref, ctypes.c_void_p) else ref
        ) if ref else None

        def raise_for_status(status):
            if status:
                raise RuntimeError(status)

        backend.api = SimpleNamespace(
            k_=lambda value: value,
            create_query=lambda **_values: refs["query"],
            SecItemCopyMatching=read,
            cfstr_to_str=decode,
            error=SimpleNamespace(item_not_found=-25300),
            Error=SimpleNamespace(raise_for_status=raise_for_status),
        )
        return backend, refs, released

    def test_pending_system_read_is_not_reentered_by_another_backend(self):
        first_entered, release_first, second_attempted, second_entered = (Event() for _ in range(4))
        count_lock = Lock()
        count = 0

        def read(_query, result):
            nonlocal count
            with count_lock:
                count += 1
                number = count
            if number == 1:
                first_entered.set()
                if not release_first.wait(2):
                    raise RuntimeError("test read was not released")
            else:
                second_entered.set()
            result._obj.value = 64
            return 0

        first, _, _ = self.backend(read)
        second, _, _ = self.backend(read)
        def retry():
            second_attempted.set()
            return second.get_password("Lulu", "test-account")

        with ThreadPoolExecutor(max_workers=2) as pool:
            initial = pool.submit(first.get_password, "Lulu", "test-account")
            self.assertTrue(first_entered.wait(1))
            repeated = pool.submit(retry)
            self.assertTrue(second_attempted.wait(1))
            try:
                self.assertFalse(second_entered.wait(0.15), "retry entered Security while authorization is pending")
            finally:
                release_first.set()
            self.assertEqual(initial.result(2), "value")
            self.assertEqual(repeated.result(2), "value")
        self.assertEqual(count, 2, "authorization state must still be read fresh")

    def test_all_query_objects_are_released_for_each_system_result(self):
        for status in (0, -25300, -25293):
            with self.subTest(status=status):
                def read(_query, result):
                    if status == 0:
                        result._obj.value = 64
                    return status
                backend, refs, released = self.backend(read)
                if status == -25293:
                    with self.assertRaises(RuntimeError):
                        backend.get_password("Lulu", "test-account")
                else:
                    self.assertEqual(backend.get_password("Lulu", "test-account"), "value" if status == 0 else None)
                for ref in refs.values():
                    self.assertEqual(released.count(ref), 1)
                self.assertEqual(released.count(64), 1 if status == 0 else 0)


if __name__ == "__main__":
    unittest.main()
