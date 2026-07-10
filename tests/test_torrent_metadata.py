"""Strict ``.torrent`` boundary validation -- no network or qBittorrent."""

from __future__ import annotations

import pytest

from spica.anime.torrent_metadata import (
    TorrentMetadataError,
    inspect_torrent,
    validate_public_trackers,
)


def test_malformed_announce_list_raises_domain_error() -> None:
    payload = (
        b"d13:announce-listi1e"
        b"4:infod6:lengthi4e4:name7:ep1.mkvee"
    )

    with pytest.raises(TorrentMetadataError):
        inspect_torrent(payload)


@pytest.mark.parametrize("payload", [
    (
        b"d8:announce30:http://127.0.0.1:8080/announce"
        b"8:announce32:https://tracker.example/announce"
        b"4:infod6:lengthi4e4:name7:ep1.mkvee"
    ),
    (
        b"d8:announce32:https://tracker.example/announce"
        b"4:infod6:lengthi4e4:name7:ep1.mkv4:name7:ep2.mkvee"
    ),
])
def test_duplicate_dictionary_keys_are_rejected(payload: bytes) -> None:
    with pytest.raises(TorrentMetadataError):
        inspect_torrent(payload)


@pytest.mark.parametrize("payload", [
    (
        b"d4:infod6:lengthi4e4:name7:ep1.mkve"
        b"8:announce32:https://tracker.example/announcee"
    ),
    (
        b"d8:announce32:https://tracker.example/announce"
        b"4:infod4:name7:ep1.mkv6:lengthi4eee"
    ),
])
def test_dictionary_keys_must_be_strictly_increasing(payload: bytes) -> None:
    with pytest.raises(TorrentMetadataError):
        inspect_torrent(payload)


@pytest.mark.parametrize("integer", [b"i+1e", b"i 1e"])
def test_noncanonical_integer_spelling_is_rejected(integer: bytes) -> None:
    payload = (
        b"d8:announce32:https://tracker.example/announce"
        b"13:creation date" + integer
        + b"4:infod6:lengthi4e4:name7:ep1.mkvee"
    )

    with pytest.raises(TorrentMetadataError):
        inspect_torrent(payload)


def test_noncanonical_byte_string_length_is_rejected() -> None:
    payload = (
        b"d8 :announce32:https://tracker.example/announce"
        b"4:infod6:lengthi4e4:name7:ep1.mkvee"
    )

    with pytest.raises(TorrentMetadataError):
        inspect_torrent(payload)


@pytest.mark.parametrize("field", [b"url-list", b"httpseeds", b"nodes"])
def test_auxiliary_network_sources_are_rejected(field: bytes) -> None:
    value = b"http://127.0.0.1/private"
    payload = (
        b"d8:announce32:https://tracker.example/announce"
        + str(len(field)).encode("ascii") + b":" + field
        + str(len(value)).encode("ascii") + b":" + value
        + b"4:infod6:lengthi4e4:name7:ep1.mkvee"
    )

    with pytest.raises(TorrentMetadataError):
        inspect_torrent(payload)


@pytest.mark.parametrize("host", [
    "2130706433",
    "127.1",
    "0177.0.0.1",
    "0x7f000001",
])
def test_legacy_ipv4_spellings_cannot_target_loopback(host: str) -> None:
    with pytest.raises(TorrentMetadataError):
        validate_public_trackers((f"http://{host}:8080/announce",))


@pytest.mark.parametrize("host", [
    "localhost.localdomain",
    "tracker.lan",
    "tracker.home.arpa",
])
def test_local_dns_names_are_rejected(host: str) -> None:
    with pytest.raises(TorrentMetadataError):
        validate_public_trackers((f"http://{host}:8080/announce",))
