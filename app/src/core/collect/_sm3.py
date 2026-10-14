"""纯 Python SM3 哈希(国密公开标准 GM/T 0004-2012)。

自写实现,零第三方依赖——用于替掉 a_bogus 签名对 gmssl 的依赖。
SM3 是公开发布的标准算法(类比 SHA-256),实现以标准测试向量校验。
"""

from __future__ import annotations

_IV = [0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
       0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E]
_MASK = 0xFFFFFFFF


def _rotl(x: int, n: int) -> int:
    n &= 31
    x &= _MASK
    return ((x << n) | (x >> (32 - n))) & _MASK


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _ff(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | (x & z) | (y & z))


def _gg(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | (~x & z))


def _cf(v: list[int], block: bytes) -> list[int]:
    """压缩函数:处理一个 512-bit 分组。"""
    w = list(int.from_bytes(block[i:i + 4], "big") for i in range(0, 64, 4))
    for j in range(16, 68):
        w.append(_p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15)) ^ _rotl(w[j - 13], 7) ^ w[j - 6])
    w1 = [w[j] ^ w[j + 4] for j in range(64)]

    a, b, c, d, e, f, g, h = v
    for j in range(64):
        tj = 0x79CC4519 if j < 16 else 0x7A879D8A
        ss1 = _rotl((_rotl(a, 12) + e + _rotl(tj, j)) & _MASK, 7)
        ss2 = ss1 ^ _rotl(a, 12)
        tt1 = (_ff(a, b, c, j) + d + ss2 + w1[j]) & _MASK
        tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & _MASK
        d = c
        c = _rotl(b, 9)
        b = a
        a = tt1
        h = g
        g = _rotl(f, 19)
        f = e
        e = _p0(tt2)

    return [(x ^ y) & _MASK for x, y in zip((a, b, c, d, e, f, g, h), v)]


def _pad(msg: bytes) -> bytes:
    bit_len = len(msg) * 8
    msg += b"\x80"
    while len(msg) % 64 != 56:
        msg += b"\x00"
    return msg + bit_len.to_bytes(8, "big")


def sm3_digest(msg: bytes) -> bytes:
    """返回 32 字节 SM3 摘要。"""
    data = _pad(bytes(msg))
    v = list(_IV)
    for i in range(0, len(data), 64):
        v = _cf(v, data[i:i + 64])
    return b"".join(x.to_bytes(4, "big") for x in v)


def sm3_hex(msg: bytes) -> str:
    return sm3_digest(msg).hex()
