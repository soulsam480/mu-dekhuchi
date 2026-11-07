from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAX_ENCRYPTED = 4096


class HikDecryptor:
    """Hikvision encrypted-stream NAL decryptor.

    H.264 (header_size=1): ``[NAL header] + AES-128-ECB(original NAL, first 4 KB) + rest``.
    Decrypting the first block reproduces the clear NAL header; that sanity check
    prevents touching unencrypted streams.

    H.265/HEVC (header_size=2): experimental - decrypts aligned AES-ECB blocks of the
    payload after the 2-byte NAL header (Hikvision main-stream encryption scheme varies
    by firmware; this is best-effort).
    """

    def __init__(self, verification_code: str, header_size: int = 1) -> None:
        self.key = bytearray(16)
        if verification_code:
            raw = verification_code.encode("utf-8")[:16]
            self.key[: len(raw)] = raw
        self.key = bytes(self.key)
        self.enabled = bool(verification_code)
        self.saw_flag = False
        self.decrypted = 0
        self.header_size = header_size

    def _decrypt(self, block: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(self.key), modes.ECB())
        decryptor = cipher.decryptor()
        return decryptor.update(block) + decryptor.finalize()

    def process(self, nal: bytes, flagged: bool) -> bytes:
        if not self.enabled or len(nal) < 16 + self.header_size:
            return nal
        if flagged:
            self.saw_flag = True
        if self.saw_flag and not flagged:
            return nal

        body = nal[self.header_size :]
        count = min((len(body) // 16) * 16, MAX_ENCRYPTED)
        if count < 16:
            return nal
        first = self._decrypt(body[:16])
        if self.header_size == 1:
            # H.264: decrypted first block begins with the clear NAL header byte
            if first[: self.header_size] != nal[: self.header_size]:
                return nal
            tail = self._decrypt(body[16:count]) if count > 16 else b""
            self.decrypted += 1
            return first + tail + body[count:]

        # HEVC / experimental: decrypt payload blocks after 2-byte header.
        # Try H.264-style sanity check first (some devices/firmware may use it),
        # otherwise apply payload decryption without the H.264 header check.
        tail = self._decrypt(body[16:count]) if count > 16 else b""
        if first[:2] == nal[:2]:
            self.decrypted += 1
            return first + tail + body[count:]
        self.decrypted += 1
        return nal[:2] + first + tail + body[count:]

    @staticmethod
    def fingerprint(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()[:8]
