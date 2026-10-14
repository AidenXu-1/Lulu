"""SM3 自写实现校验:用国标公开测试向量。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.collect._sm3 import sm3_hex


class TestSM3(unittest.TestCase):
    def test_vector_abc(self):
        # GM/T 0004-2012 标准向量
        self.assertEqual(
            sm3_hex(b"abc"),
            "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0",
        )

    def test_vector_512bit(self):
        # 标准向量:64 字节 "abcd"*16
        self.assertEqual(
            sm3_hex(b"abcd" * 16),
            "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732",
        )

    def test_empty(self):
        # 空串摘要长度 32 字节、确定值
        self.assertEqual(
            sm3_hex(b""),
            "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b",
        )


if __name__ == "__main__":
    unittest.main()
