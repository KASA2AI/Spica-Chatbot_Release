"""Small, strict parser for trusted-boundary ``.torrent`` metadata.

The parser never re-encodes the ``info`` dictionary: the v1 infohash is the
SHA-1 of its exact bencoded byte span.  Keeping this Qt-free and independent of
qBittorrent lets both the source and torrent adapters validate the same payload.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any

MAX_TORRENT_BYTES = 1024 * 1024
_MAX_DEPTH = 64
_FORBIDDEN_NETWORK_KEYS = frozenset({b"url-list", b"httpseeds", b"nodes"})


class TorrentMetadataError(ValueError):
    pass


@dataclass(frozen=True)
class TorrentMetadata:
    infohash: str
    trackers: tuple[str, ...]


class _Parser:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def value(self, pos: int, depth: int = 0) -> tuple[Any, int]:
        if depth > _MAX_DEPTH or pos >= len(self._payload):
            raise TorrentMetadataError("invalid bencode nesting")
        token = self._payload[pos:pos + 1]
        if token == b"i":
            end = self._payload.find(b"e", pos + 1)
            if end < 0:
                raise TorrentMetadataError("unterminated integer")
            raw = self._payload[pos + 1:end]
            digits = raw[1:] if raw.startswith(b"-") else raw
            if (not digits or not digits.isdigit() or raw == b"-0" or
                    (raw.startswith(b"0") and raw != b"0") or
                    (raw.startswith(b"-0"))):
                raise TorrentMetadataError("non-canonical integer")
            try:
                return int(raw), end + 1
            except ValueError as exc:
                raise TorrentMetadataError("invalid integer") from exc
        if token == b"l":
            result: list[Any] = []
            pos += 1
            while pos < len(self._payload) and self._payload[pos:pos + 1] != b"e":
                item, pos = self.value(pos, depth + 1)
                result.append(item)
            if pos >= len(self._payload):
                raise TorrentMetadataError("unterminated list")
            return result, pos + 1
        if token == b"d":
            result: dict[bytes, Any] = {}
            previous_key: bytes | None = None
            pos += 1
            while pos < len(self._payload) and self._payload[pos:pos + 1] != b"e":
                key, pos = self.value(pos, depth + 1)
                if not isinstance(key, bytes):
                    raise TorrentMetadataError("dictionary key is not bytes")
                if key in result:
                    raise TorrentMetadataError("duplicate dictionary key")
                if previous_key is not None and key < previous_key:
                    raise TorrentMetadataError("dictionary keys are not sorted")
                previous_key = key
                result[key], pos = self.value(pos, depth + 1)
            if pos >= len(self._payload):
                raise TorrentMetadataError("unterminated dictionary")
            return result, pos + 1
        if token < b"0" or token > b"9":
            raise TorrentMetadataError("invalid bencode token")
        colon = self._payload.find(b":", pos)
        if colon < 0:
            raise TorrentMetadataError("missing byte-string separator")
        raw_length = self._payload[pos:colon]
        if (not raw_length or not raw_length.isdigit() or
                (raw_length.startswith(b"0") and raw_length != b"0")):
            raise TorrentMetadataError("non-canonical byte-string length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise TorrentMetadataError("invalid byte-string length") from exc
        start, end = colon + 1, colon + 1 + length
        if end > len(self._payload):
            raise TorrentMetadataError("truncated byte string")
        return self._payload[start:end], end

    def root(self) -> tuple[dict[bytes, Any], tuple[int, int]]:
        if self._payload[:1] != b"d":
            raise TorrentMetadataError("torrent root is not a dictionary")
        result: dict[bytes, Any] = {}
        info_span: tuple[int, int] | None = None
        previous_key: bytes | None = None
        pos = 1
        while pos < len(self._payload) and self._payload[pos:pos + 1] != b"e":
            key, pos = self.value(pos, 1)
            if not isinstance(key, bytes):
                raise TorrentMetadataError("dictionary key is not bytes")
            if key in result:
                raise TorrentMetadataError("duplicate dictionary key")
            if previous_key is not None and key < previous_key:
                raise TorrentMetadataError("dictionary keys are not sorted")
            previous_key = key
            value_start = pos
            result[key], pos = self.value(pos, 1)
            if key == b"info":
                info_span = (value_start, pos)
        if pos >= len(self._payload) or pos + 1 != len(self._payload):
            raise TorrentMetadataError("invalid torrent root terminator")
        if info_span is None or not isinstance(result.get(b"info"), dict):
            raise TorrentMetadataError("missing info dictionary")
        return result, info_span


def inspect_torrent(payload: bytes) -> TorrentMetadata:
    if not isinstance(payload, bytes) or not payload:
        raise TorrentMetadataError("empty torrent payload")
    if len(payload) > MAX_TORRENT_BYTES:
        raise TorrentMetadataError("torrent payload exceeds size limit")
    root, (start, end) = _Parser(payload).root()
    if _FORBIDDEN_NETWORK_KEYS.intersection(root):
        raise TorrentMetadataError("torrent contains unsupported network sources")
    trackers: list[str] = []
    values: list[bytes] = []
    if b"announce" in root:
        announce = root[b"announce"]
        if not isinstance(announce, bytes):
            raise TorrentMetadataError("invalid announce value")
        values.append(announce)
    announce_list = root.get(b"announce-list", [])
    if not isinstance(announce_list, list):
        raise TorrentMetadataError("invalid announce-list value")
    for tier in announce_list:
        tier_values = tier if isinstance(tier, list) else [tier]
        if not tier_values or any(not isinstance(v, bytes) for v in tier_values):
            raise TorrentMetadataError("invalid announce-list tier")
        values.extend(tier_values)
    for value in values:
        try:
            url = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TorrentMetadataError("tracker URL is not UTF-8") from exc
        if url not in trackers:
            trackers.append(url)
    return TorrentMetadata(
        infohash=hashlib.sha1(payload[start:end]).hexdigest(),
        trackers=tuple(trackers),
    )


def validate_public_trackers(trackers: tuple[str, ...]) -> None:
    """Reject direct local tracker targets before handing bytes to qBittorrent.

    Hostname DNS rebinding still requires daemon/network-level mitigation; this
    boundary blocks credentials, local suffixes, IP literals and legacy numeric
    IPv4 spellings without introducing blocking DNS lookups into materialize.
    """
    if not trackers:
        raise TorrentMetadataError("torrent has no trackers")
    if len(trackers) > 64:
        raise TorrentMetadataError("torrent has too many trackers")
    for url in trackers:
        if not url or len(url) > 2048:
            raise TorrentMetadataError("invalid tracker URL length")
        try:
            parsed = urllib.parse.urlsplit(url)
            hostname = (parsed.hostname or "").rstrip(".").lower()
            _ = parsed.port  # force validation of an invalid/out-of-range port
        except ValueError as exc:
            raise TorrentMetadataError("invalid tracker URL") from exc
        if (parsed.scheme.lower() not in {"http", "https", "udp"}
                or not hostname or parsed.username or parsed.password
                or parsed.fragment
                or hostname == "localhost"
                or hostname.endswith((
                    ".localhost", ".local", ".localdomain", ".localdomain6",
                    ".internal", ".lan", ".home", ".home.arpa",
                ))):
            raise TorrentMetadataError("unsafe tracker URL")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            # URL parsers and socket stacks also accept legacy numeric IPv4
            # spellings (127.1, integer/octal/hex forms).  They are not valid
            # ``ipaddress`` literals, so normalize them without doing DNS.
            try:
                address = ipaddress.ip_address(socket.inet_aton(hostname))
            except (OSError, UnicodeError):
                continue
        if not address.is_global:
            raise TorrentMetadataError("tracker IP is not public")
