#!/usr/bin/env python3
"""Phase 0 probe: Mikan official-site RSS structure + magnet extractability.

Answers open questions #3 and #6 (RSS coverage; does RSS carry full magnet;
season/episode/subgroup parse shape). NOT production code -- one-off recon.
Writes a redacted sample to sample_mikan.json for the resolver golden cases.

Run: python docs/anime_watch/probes/probe_mikan.py "无职转生"
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE = "https://mikanani.me"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
HERE = Path(__file__).parent

# The 40-hex token in Mikan's Download/Episode URLs is hypothesised to be the
# BitTorrent v1 infohash (btih). If true we can build a magnet straight from
# RSS with no .torrent fetch (keeps the port magnet-only, no SSRF surface).
INFOHASH_RE = re.compile(r"([0-9a-fA-F]{40})")
# Batch/collection markers we must FILTER in v1 (D11: single-episode only).
BATCH_RE = re.compile(r"\[?\b(0?\d{1,3}\s*-\s*0?\d{1,3}|全\d+话|Complete|Fin|BDBOX|BD-?BOX|合集)\b", re.I)
# Episode number: "- 02", "- 12v2", "第02话", "[02]"
EP_RE = re.compile(r"(?:-\s*|第|\[)\s*(\d{1,3})(?:v\d)?\s*(?:话|話|集|\])?")
SEASON_RE = re.compile(r"(S(\d)|(\d)期|第(\d)季|(\d)nd Season|(\d)rd Season|Ⅱ|Ⅲ|Ⅳ)")


def infohash_from_url(url: str) -> str | None:
    m = INFOHASH_RE.search(url or "")
    return m.group(1).lower() if m else None


def build_magnet(infohash: str, name: str) -> str:
    dn = urllib.parse.quote(name)
    return f"magnet:?xt=urn:btih:{infohash}&dn={dn}"


def fetch_torrent_infohash(url: str) -> str | None:
    """Download the .torrent and compute the real btih to VERIFY the URL token."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        data = r.content
        # crude bencode: find 'info' dict and sha1 it. Use a minimal decoder.
        info = _bencode_info_slice(data)
        return hashlib.sha1(info).hexdigest() if info else None
    except Exception as e:  # noqa: BLE001 -- probe, report and move on
        print(f"    (torrent verify failed: {e})")
        return None


def _bencode_info_slice(data: bytes) -> bytes | None:
    """Return the raw bencoded value of the top-level 'info' key."""
    key = b"4:info"
    i = data.find(key)
    if i < 0:
        return None
    start = i + len(key)
    end = _bencode_skip(data, start)
    return data[start:end] if end else None


def _bencode_skip(data: bytes, i: int) -> int | None:
    c = data[i:i + 1]
    if c == b"d" or c == b"l":
        i += 1
        while data[i:i + 1] != b"e":
            if data[i:i + 1].isdigit() or data[i:i + 1] == b"i" or data[i:i + 1] in (b"d", b"l"):
                if data[i:i + 1] == b"i":
                    i = data.find(b"e", i) + 1
                elif data[i:i + 1] in (b"d", b"l"):
                    i = _bencode_skip(data, i)
                else:
                    i = _bencode_skip(data, i)  # string as key
                    i = _bencode_skip(data, i)  # value
            else:
                return None
        return i + 1
    if c.isdigit():
        colon = data.find(b":", i)
        length = int(data[i:colon])
        return colon + 1 + length
    if c == b"i":
        return data.find(b"e", i) + 1
    return None


def parse_title(title: str) -> dict:
    subgroup = None
    m = re.match(r"\s*\[([^\]]+)\]", title)
    if m:
        subgroup = m.group(1)
    season = None
    ms = SEASON_RE.search(title)
    if ms:
        season = ms.group(0)
    ep = None
    me = EP_RE.search(title)
    if me:
        ep = int(me.group(1))
    quality = None
    if re.search(r"1080p", title, re.I):
        quality = "1080p"
    elif re.search(r"720p", title, re.I):
        quality = "720p"
    subtitle = None
    if re.search(r"简繁", title):
        subtitle = "简繁"
    elif re.search(r"简体|CHS|GB", title):
        subtitle = "简体"
    return {
        "subgroup": subgroup, "season": season, "episode": ep,
        "quality": quality, "subtitle": subtitle,
        "is_batch": bool(BATCH_RE.search(title)),
    }


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else "无职转生"
    url = f"{BASE}/RSS/Search?searchstr={urllib.parse.quote(query)}"
    print(f"GET {url}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    items = root.findall(".//item")
    print(f"RSS items: {len(items)}\n")

    samples = []
    verified = 0
    for idx, it in enumerate(items):
        title = (it.findtext("title") or "").strip()
        link = it.findtext("link") or ""
        enc = it.find("enclosure")
        torrent_url = enc.get("url") if enc is not None else ""
        length = enc.get("length") if enc is not None else None
        ih = infohash_from_url(torrent_url) or infohash_from_url(link)
        parsed = parse_title(title)
        magnet = build_magnet(ih, title) if ih else None

        # Verify the URL token == real infohash on the FIRST 2 items only.
        verify = None
        if idx < 2 and ih and torrent_url:
            real = fetch_torrent_infohash(torrent_url)
            verify = (real == ih)
            if verify:
                verified += 1

        rec = {
            "title": title, "parsed": parsed,
            "infohash": ih, "magnet_prefix": (magnet[:60] + "...") if magnet else None,
            "size_bytes": int(length) if length else None,
            "url_token_is_infohash": verify,
        }
        samples.append(rec)
        if idx < 8:
            b = " [BATCH-FILTERED]" if parsed["is_batch"] else ""
            print(f"[{idx}] {title[:70]}{b}")
            print(f"     season={parsed['season']} ep={parsed['episode']} "
                  f"q={parsed['quality']} sub={parsed['subtitle']} sg={parsed['subgroup']}")
            print(f"     infohash={ih} verify={verify}")

    non_batch = [s for s in samples if not s["parsed"]["is_batch"]]
    print(f"\nSUMMARY: {len(items)} items, {len(non_batch)} single-episode "
          f"(after batch filter), infohash-verified {verified}/2 sampled")

    out = HERE / "sample_mikan.json"
    out.write_text(json.dumps(samples, ensure_ascii=False, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
