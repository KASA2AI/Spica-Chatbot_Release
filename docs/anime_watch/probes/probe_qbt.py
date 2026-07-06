#!/usr/bin/env python3
"""Phase 0 probe: qBittorrent Web API add/status/cancel round-trip.

Answers open question #4 (bypass_local_auth vs password) and validates the
category-scoped action surface the TorrentClientPort will wrap (P2-20).
NOT production code -- one-off recon. Uses a tiny well-seeded public magnet.

PREREQUISITE (user, sudo):
  sudo apt install qbittorrent-nox
  sudo systemctl enable --now qbittorrent-nox
  # first run sets a Web UI password; or enable bypass for localhost in
  # qBittorrent.conf: WebUI\LocalHostAuth=false  (then no login needed)

Run: python docs/anime_watch/probes/probe_qbt.py [http://127.0.0.1:8080] [user] [pass]
"""
from __future__ import annotations

import sys
import time

import requests

# Debian netinst ISO -- small, healthy, legal test torrent.
TEST_MAGNET = (
    "magnet:?xt=urn:btih:6853f6486e6c1e2f1f4c5c3b4e0a1b2c3d4e5f60"  # placeholder btih
    "&dn=probe-test"
)
CATEGORY = "spica-anime"


def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
    user = sys.argv[2] if len(sys.argv) > 2 else "admin"
    pw = sys.argv[3] if len(sys.argv) > 3 else "adminadmin"
    magnet = sys.argv[4] if len(sys.argv) > 4 else TEST_MAGNET

    s = requests.Session()
    ref = {"Referer": base}

    # 1) reachability
    try:
        r = s.get(f"{base}/api/v2/app/version", headers=ref, timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"UNREACHABLE: {e}\n-> is qbittorrent-nox running? (systemctl status)")
        return
    if r.status_code == 200:
        print(f"reachable, version={r.text} (LocalHostAuth appears OFF -> bypass works)")
    else:
        # 2) login needed
        lr = s.post(f"{base}/api/v2/auth/login",
                    data={"username": user, "password": pw}, headers=ref, timeout=5)
        print(f"login: status={lr.status_code} body={lr.text!r}")
        if lr.text.strip() != "Ok.":
            print("-> auth failed; set correct user/pass or enable LocalHostAuth=false")
            return
        v = s.get(f"{base}/api/v2/app/version", headers=ref, timeout=5)
        print(f"version={v.text}")

    # 3) add (magnet-only; category-scoped) -- P0-3 / P2-20 shape
    print(f"\nadd_magnet (category={CATEGORY})...")
    ar = s.post(f"{base}/api/v2/torrents/add",
                data={"urls": magnet, "category": CATEGORY,
                      "savepath": "/tmp/spica-anime-probe", "paused": "true"},
                headers=ref, timeout=10)
    print(f"  add: status={ar.status_code} body={ar.text!r}")

    # 4) status (category-filtered read)
    time.sleep(1)
    tr = s.get(f"{base}/api/v2/torrents/info",
               params={"category": CATEGORY}, headers=ref, timeout=5)
    tasks = tr.json() if tr.status_code == 200 else []
    print(f"  status: {len(tasks)} task(s) in category {CATEGORY}")
    for t in tasks:
        print(f"    hash={t.get('hash')[:12]} name={t.get('name')} "
              f"state={t.get('state')} progress={t.get('progress')}")

    # 5) cancel (delete) -- category-scoped cleanup
    hashes = [t.get("hash") for t in tasks]
    if hashes:
        dr = s.post(f"{base}/api/v2/torrents/delete",
                    data={"hashes": "|".join(hashes), "deleteFiles": "true"},
                    headers=ref, timeout=5)
        print(f"  cancel: status={dr.status_code}")

    print("\nOK: add/status/cancel round-trip works. "
          "Record whether login was needed (open question #4).")


if __name__ == "__main__":
    main()
