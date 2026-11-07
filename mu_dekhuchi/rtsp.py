from __future__ import annotations

import hashlib
import re
import secrets
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

DEFAULT_TIMEOUT = 15.0
KEEPALIVE_SECONDS = 30.0
HIKVISION_EXTENSION_PROFILE = 0x4000
NAL_TYPE_STAP_A = 24
NAL_TYPE_FU_A = 28


class RtspError(RuntimeError):
    """RTSP transport or protocol failure."""


class RtspAuthError(RtspError):
    """The device rejected the credentials."""


@dataclass(frozen=True)
class SdpInfo:
    codec: str
    control: str
    vps: bytes | None
    sps: bytes | None
    pps: bytes | None
    raw: str


@dataclass(frozen=True)
class RtpPacket:
    payload: bytes
    timestamp: int
    marker: bool
    flagged: bool


@dataclass(frozen=True)
class _Response:
    status: int
    reason: str
    headers: dict[str, list[str]]
    body: bytes

    def header(self, name: str) -> str | None:
        values = self.headers.get(name.lower())
        return values[0] if values else None


def parse_challenge(values: str | list[str]) -> dict[str, str] | None:
    """Parse a WWW-Authenticate header, preferring Digest over Basic."""
    candidates = values if isinstance(values, list) else [values]
    basic: dict[str, str] | None = None
    for value in candidates:
        if not value:
            continue
        scheme = value.strip().split(maxsplit=1)[0].lower()
        if scheme == "digest":
            params: dict[str, str] = {}
            for key, quoted, bare in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', value):
                params[key.lower()] = quoted or bare
            return {"scheme": "digest", **params}
        if scheme == "basic":
            basic = {"scheme": "basic"}
    return basic


def build_authorization(
    challenge: dict[str, str] | None,
    method: str,
    uri: str,
    username: str,
    password: str,
    state: dict[str, int] | None = None,
    cnonce: str | None = None,
) -> str | None:
    """Build an Authorization value; `state` carries the nonce count."""
    if not challenge:
        return None
    if challenge.get("scheme") == "basic":
        import base64

        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        return f"Basic {token}"

    state = state if state is not None else {}
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    algorithm = challenge.get("algorithm", "MD5").upper()
    qop = challenge.get("qop", "")
    selected_qop = "auth" if "auth" in {part.strip() for part in qop.split(",")} else None
    cnonce = cnonce or secrets.token_hex(8)

    ha1 = _md5(f"{username}:{realm}:{password}")
    if algorithm == "MD5-SESS":
        ha1 = _md5(f"{ha1}:{nonce}:{cnonce}")
    ha2 = _md5(f"{method}:{uri}")

    header = f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
    if selected_qop:
        state["nc"] = state.get("nc", 0) + 1
        nc = f"{state['nc']:08x}"
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{selected_qop}:{ha2}")
        header += f', qop={selected_qop}, nc={nc}, cnonce="{cnonce}", response="{response}"'
    else:
        header += f', response="{_md5(f"{ha1}:{nonce}:{ha2}")}"'
    if challenge.get("opaque"):
        header += f', opaque="{challenge["opaque"]}"'
    if challenge.get("algorithm"):
        header += f", algorithm={challenge['algorithm']}"
    return header


def parse_sdp(sdp: str) -> SdpInfo | None:
    """Extract the first video track's codec, control URL and parameter sets."""
    in_video = False
    control = ""
    codec = "h264"
    vps: bytes | None = None
    sps: bytes | None = None
    pps: bytes | None = None

    for line in sdp.splitlines():
        if line.startswith("m="):
            if in_video:
                break
            in_video = line.startswith("m=video")
            continue
        if not in_video:
            continue
        if line.startswith("a=control:"):
            control = line.split(":", 1)[1].strip()
        elif line.startswith("a=rtpmap:"):
            upper = line.upper()
            if "H265" in upper or "HEVC" in upper:
                codec = "hevc"
            elif "H264" in upper:
                codec = "h264"
        elif line.startswith("a=fmtp:"):
            # H.264
            sprop = re.search(r"sprop-parameter-sets=([^;\s]+)", line)
            if sprop:
                parts = sprop.group(1).split(",")
                sps = _decode_base64(parts[0])
                pps = _decode_base64(parts[1]) if len(parts) > 1 else None
            # HEVC (RFC 7798)
            if codec == "hevc":
                vps_m = re.search(r"sprop-vps=([^;\s]+)", line)
                sps_m = re.search(r"sprop-sps=([^;\s]+)", line)
                pps_m = re.search(r"sprop-pps=([^;\s]+)", line)
                if vps_m:
                    vps = _decode_base64(vps_m.group(1).split(",")[0])
                if sps_m:
                    sps = _decode_base64(sps_m.group(1).split(",")[0])
                if pps_m:
                    pps = _decode_base64(pps_m.group(1).split(",")[0])

    if not in_video and not control:
        return None
    return SdpInfo(codec=codec, control=control or "*", vps=vps, sps=sps, pps=pps, raw=sdp)


def resolve_control(base_url: str, control: str) -> str:
    if not control or control == "*":
        return base_url
    if control.lower().startswith("rtsp://"):
        return control
    return f"{base_url.rstrip('/')}/{control.lstrip('/')}"


def parse_rtp(packet: bytes) -> RtpPacket | None:
    if len(packet) < 12 or (packet[0] >> 6) != 2:
        return None

    has_extension = bool(packet[0] & 0x10)
    has_padding = bool(packet[0] & 0x20)
    csrc_count = packet[0] & 0x0F
    marker = bool(packet[1] & 0x80)
    timestamp = int.from_bytes(packet[4:8], "big")

    offset = 12 + csrc_count * 4
    flagged = False
    if has_extension:
        if len(packet) < offset + 4:
            return None
        profile = int.from_bytes(packet[offset : offset + 2], "big")
        words = int.from_bytes(packet[offset + 2 : offset + 4], "big")
        flagged = profile == HIKVISION_EXTENSION_PROFILE
        offset += 4 + words * 4

    end = len(packet) - (packet[-1] if has_padding else 0)
    if offset >= end:
        return None
    return RtpPacket(packet[offset:end], timestamp, marker, flagged)


class NalAssembler:
    """Turn RTP payloads into complete H.264 NAL units."""

    def __init__(self) -> None:
        self._fragments: list[bytes] | None = None
        self._fragment_flagged = False

    def feed(self, packet: RtpPacket) -> list[tuple[bytes, int, bool]]:
        payload = packet.payload
        if not payload:
            return []
        nal_type = payload[0] & 0x1F

        if 1 <= nal_type <= 23:
            self._fragments = None
            return [(payload, packet.timestamp, packet.flagged)]

        if nal_type == NAL_TYPE_STAP_A:
            return self._unpack_stap_a(payload, packet)

        if nal_type == NAL_TYPE_FU_A:
            return self._unpack_fu_a(payload, packet)

        return []

    def _unpack_stap_a(self, payload: bytes, packet: RtpPacket) -> list[tuple[bytes, int, bool]]:
        self._fragments = None
        units: list[tuple[bytes, int, bool]] = []
        offset = 1
        while offset + 2 <= len(payload):
            size = int.from_bytes(payload[offset : offset + 2], "big")
            offset += 2
            if size == 0 or offset + size > len(payload):
                break
            units.append((payload[offset : offset + size], packet.timestamp, packet.flagged))
            offset += size
        return units

    def _unpack_fu_a(self, payload: bytes, packet: RtpPacket) -> list[tuple[bytes, int, bool]]:
        if len(payload) < 2:
            return []
        start = bool(payload[1] & 0x80)
        end = bool(payload[1] & 0x40)

        if start:
            header = bytes([(payload[0] & 0xE0) | (payload[1] & 0x1F)])
            self._fragments = [header, payload[2:]]
            self._fragment_flagged = packet.flagged
        elif self._fragments is not None:
            self._fragments.append(payload[2:])

        if not end or self._fragments is None:
            return []

        nal = b"".join(self._fragments)
        flagged = self._fragment_flagged
        self._fragments = None
        return [(nal, packet.timestamp, flagged)]


HEVC_NAL_AP = 48
HEVC_NAL_FU = 49


class HevcNalAssembler:
    """Turn RTP payloads into complete HEVC (H.265) NAL units (RFC 7798)."""

    def __init__(self) -> None:
        self._fragments: list[bytes] | None = None
        self._fragment_header: bytes | None = None
        self._fragment_flagged = False

    def feed(self, packet: RtpPacket) -> list[tuple[bytes, int, bool]]:
        payload = packet.payload
        if len(payload) < 2:
            return []
        nal_type = (payload[0] >> 1) & 0x3F

        # Single NAL unit packet (types 0-47)
        if nal_type <= 47:
            self._fragments = None
            return [(payload, packet.timestamp, packet.flagged)]

        if nal_type == HEVC_NAL_AP:
            return self._unpack_ap(payload, packet)

        if nal_type == HEVC_NAL_FU:
            return self._unpack_fu(payload, packet)

        # PACI and others: drop for now
        return []

    def _unpack_ap(self, payload: bytes, packet: RtpPacket) -> list[tuple[bytes, int, bool]]:
        self._fragments = None
        units: list[tuple[bytes, int, bool]] = []
        offset = 2  # skip payload header
        while offset + 2 <= len(payload):
            size = int.from_bytes(payload[offset : offset + 2], "big")
            offset += 2
            if size == 0 or offset + size > len(payload):
                break
            units.append((payload[offset : offset + size], packet.timestamp, packet.flagged))
            offset += size
        return units

    def _unpack_fu(self, payload: bytes, packet: RtpPacket) -> list[tuple[bytes, int, bool]]:
        if len(payload) < 3:
            return []
        fu_header = payload[2]
        start = bool(fu_header & 0x80)
        end = bool(fu_header & 0x40)
        fu_type = fu_header & 0x3F

        if start:
            # Reconstruct 2-byte HEVC NAL header: keep F and layer/tid bits, replace type
            nal_header = bytes([(payload[0] & 0x81) | (fu_type << 1), payload[1]])
            self._fragment_header = nal_header
            self._fragments = [payload[3:]]
            self._fragment_flagged = packet.flagged
        elif self._fragments is not None:
            self._fragments.append(payload[3:])
            # If any fragment is flagged, treat as flagged? Or keep start's flag.
            if packet.flagged:
                self._fragment_flagged = True

        if not end or self._fragments is None or self._fragment_header is None:
            return []

        nal = self._fragment_header + b"".join(self._fragments)
        flagged = self._fragment_flagged
        self._fragments = None
        self._fragment_header = None
        return [(nal, packet.timestamp, flagged)]


class RtspClient:
    """Minimal RTSP client for RTP-over-TCP interleaved H.264/H.265."""

    def __init__(
        self,
        url: str,
        username: str = "",
        password: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = "mu-dekhuchi/2.0",
    ) -> None:
        parts = urlsplit(url)
        if not parts.hostname:
            raise RtspError(f"Invalid RTSP URL: {url}")

        self._host = parts.hostname
        self._port = parts.port or 554
        self._path = parts.path or "/"
        self._query = parts.query
        self.base_url = urlunsplit(
            (parts.scheme or "rtsp", f"{self._host}:{self._port}", self._path, self._query, "")
        )
        self.username = username or unquote(parts.username or "")
        self.password = password or unquote(parts.password or "")
        self.timeout = timeout
        self.user_agent = user_agent

        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._cseq = 0
        self._challenge: dict[str, str] | None = None
        self._auth_state: dict[str, int] = {}
        self._session = ""
        self._play_url = self.base_url
        self._assembler: NalAssembler | HevcNalAssembler = NalAssembler()
        self.codec = "h264"
        self._on_nal: Callable[[bytes, int, bool], None] | None = None
        self._closed = False
        self._lock = threading.Lock()

    def start(self) -> SdpInfo:
        self._connect()
        response = self._request("DESCRIBE", self.base_url, {"Accept": "application/sdp"})
        info = parse_sdp(response.body.decode("utf-8", "replace"))
        if info is None:
            raise RtspError("SDP contains no video track")
        self.codec = info.codec
        self._assembler = HevcNalAssembler() if info.codec == "hevc" else NalAssembler()

        base = (
            response.header("content-base") or response.header("content-location") or self.base_url
        )
        control_url = resolve_control(base, info.control)
        setup = self._request(
            "SETUP",
            control_url,
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        session_header = setup.header("session")
        if not session_header:
            raise RtspError("SETUP returned no Session header")
        self._session = session_header.split(";")[0].strip()

        self._play_url = base
        self._request("PLAY", base, {"Range": "npt=0.000-"})
        self._require_socket().settimeout(1.0)
        return info

    def run(
        self,
        on_nal: Callable[[bytes, int, bool], None],
        stop_event: threading.Event,
    ) -> None:
        self._on_nal = on_nal
        last_keepalive = time.monotonic()
        sock = self._require_socket()

        while not stop_event.is_set() and not self._closed:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                if time.monotonic() - last_keepalive >= KEEPALIVE_SECONDS:
                    self._request("GET_PARAMETER", self._play_url, {})
                    last_keepalive = time.monotonic()
                continue
            if not chunk:
                raise RtspError("RTSP connection closed by the device")
            self._buffer.extend(chunk)
            self._drain_interleaved()

    def stop(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with suppress(OSError):
                if self._session and self._socket:
                    self._send("TEARDOWN", self.base_url, {})
            if self._socket:
                with suppress(OSError):
                    self._socket.shutdown(socket.SHUT_RDWR)
                self._socket.close()
                self._socket = None

    def _connect(self) -> None:
        self._socket = socket.create_connection((self._host, self._port), timeout=self.timeout)
        self._socket.settimeout(self.timeout)

    def _send(self, method: str, url: str, headers: dict[str, str]) -> None:
        self._cseq += 1
        lines = [
            f"{method} {url} RTSP/1.0",
            f"CSeq: {self._cseq}",
            f"User-Agent: {self.user_agent}",
        ]
        if self._session:
            lines.append(f"Session: {self._session}")
        authorization = build_authorization(
            self._challenge, method, url, self.username, self.password, self._auth_state
        )
        if authorization:
            lines.append(f"Authorization: {authorization}")
        lines.extend(f"{key}: {value}" for key, value in headers.items())
        self._require_socket().sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

    def _request(
        self, method: str, url: str, headers: dict[str, str], retried: bool = False
    ) -> _Response:
        self._send(method, url, headers)
        response = self._read_response()

        if response.status == 401 and not retried:
            challenge_header = response.headers.get("www-authenticate")
            self._challenge = parse_challenge(challenge_header or [])
            self._auth_state = {}
            if not self._challenge:
                raise RtspAuthError("Device rejected login without a usable challenge")
            return self._request(method, url, headers, retried=True)

        if response.status == 401:
            raise RtspAuthError("RTSP authentication failed")
        if not 200 <= response.status < 300:
            raise RtspError(f"RTSP {method} failed: {response.status} {response.reason}")
        return response

    def _read_response(self) -> _Response:
        while True:
            if self._buffer[:1] == b"$" and not self._consume_interleaved():
                self._fill()
                continue

            header_end = self._buffer.find(b"\r\n\r\n")
            if header_end == -1:
                self._fill()
                continue

            header_text = self._buffer[:header_end].decode("utf-8", "replace")
            lines = header_text.split("\r\n")
            status_line = lines[0].split(" ", 2)
            if len(status_line) < 2:
                raise RtspError(f"Malformed RTSP response: {lines[0]!r}")

            headers: dict[str, list[str]] = {}
            for line in lines[1:]:
                key, separator, value = line.partition(":")
                if separator:
                    headers.setdefault(key.strip().lower(), []).append(value.strip())

            content_length = int((headers.get("content-length") or ["0"])[0])
            total = header_end + 4 + content_length
            while len(self._buffer) < total:
                self._fill()

            body = bytes(self._buffer[header_end + 4 : total])
            del self._buffer[:total]
            return _Response(int(status_line[1]), status_line[2], headers, body)

    def _fill(self) -> None:
        try:
            chunk = self._require_socket().recv(65536)
        except TimeoutError as error:
            raise RtspError("Timed out waiting for RTSP response") from error
        if not chunk:
            raise RtspError("RTSP connection closed")
        self._buffer.extend(chunk)

    def _drain_interleaved(self) -> None:
        while self._consume_interleaved():
            pass

    def _consume_interleaved(self) -> bool:
        buf = self._buffer
        if not buf or buf[0] != 0x24:  # '$'
            return False
        if len(buf) < 4:
            return False
        length = int.from_bytes(buf[2:4], "big")
        frame_len = 4 + length
        if len(buf) < frame_len:
            return False

        channel = buf[1]
        if channel == 0 and self._on_nal:
            self._handle_rtp(bytes(buf[4:frame_len]))
        del buf[:frame_len]
        return True

    def _handle_rtp(self, packet: bytes) -> None:
        rtp = parse_rtp(packet)
        if rtp is None:
            return
        callback = self._on_nal
        if callback is None:
            return
        for nal, timestamp, flagged in self._assembler.feed(rtp):
            callback(nal, timestamp, flagged)

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise RtspError("RTSP socket is not connected")
        return self._socket


def _md5(value: str) -> str:
    return hashlib.md5(value.encode()).hexdigest()


def _decode_base64(value: str) -> bytes | None:
    import base64

    try:
        return base64.b64decode(value)
    except ValueError:
        return None
