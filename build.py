#!/usr/bin/env python3
"""
Build a single corrected XMLTV guide for a TiviMate Xtream Codes playlist.

Why this exists
---------------
The IPTV provider publishes an EPG that is ~1 day deep, leaves most channels
with no tvg-id at all, and points several major channels at ids that hold no
programmes.  Meanwhile good public UK/IE guides exist, but they use different
channel-id conventions, so TiviMate (which matches strictly on tvg-id) finds
nothing.

This script merges the public sources and *rewrites* their channel ids to the
ids the provider actually sends, producing one file TiviMate can consume.

No credentials are used or required.  The provider's id list is baked into
mapping.json, generated once from the panel's channel list.  Regenerate that
file only if the provider renumbers its channels.
"""

import gzip
import io
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

SOURCES = [
    ("freeview",   "https://raw.githubusercontent.com/dp247/Freeview-EPG/master/epg.xml"),
    ("epgshareUK", "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz"),
    ("epgshareIE", "https://epgshare01.online/epgshare01/epg_ripper_IE1.xml.gz"),
    ("openepgUK",  "https://www.open-epg.com/files/unitedkingdom1.xml"),
]

UA = "Mozilla/5.0 (compatible; epg-builder/1.0)"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "epg.xml.gz")


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def main() -> int:
    # utf-8-sig: tolerate a BOM, which some editors and PowerShell add
    with open(os.path.join(HERE, "mapping.json"), encoding="utf-8-sig") as fh:
        mapping = json.load(fh)
    print(f"mapping entries: {len(mapping)}")

    # target-id -> list of provider ids that should receive its programmes
    fanout: dict[str, list[str]] = {}
    for provider_id, source_id in mapping.items():
        fanout.setdefault(source_id, []).append(provider_id)

    out_channels: dict[str, ET.Element] = {}
    out_programmes: list[ET.Element] = []
    seen_prog = 0
    rewritten = 0

    for tag, url in SOURCES:
        try:
            raw = fetch(url)
        except Exception as exc:                       # noqa: BLE001
            print(f"  !! {tag}: download failed ({exc}) - skipping", file=sys.stderr)
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            print(f"  !! {tag}: parse failed ({exc}) - skipping", file=sys.stderr)
            continue

        n_ch = n_pr = 0
        for ch in root.findall("channel"):
            cid = ch.get("id")
            if not cid:
                continue
            # keep the original id (verbatim matches still work)
            if cid not in out_channels:
                out_channels[cid] = ch
                n_ch += 1
            # and emit a clone under every provider id that maps to it
            for pid in fanout.get(cid, []):
                if pid in out_channels:
                    continue
                clone = ET.fromstring(ET.tostring(ch))
                clone.set("id", pid)
                out_channels[pid] = clone

        for pr in root.findall("programme"):
            cid = pr.get("channel")
            if not cid:
                continue
            out_programmes.append(pr)
            n_pr += 1
            seen_prog += 1
            for pid in fanout.get(cid, []):
                clone = ET.fromstring(ET.tostring(pr))
                clone.set("channel", pid)
                out_programmes.append(clone)
                rewritten += 1

        print(f"  {tag:<11} channels={n_ch:<5} programmes={n_pr}")

    if not out_programmes:
        print("FATAL: no programmes collected - refusing to write an empty guide",
              file=sys.stderr)
        return 1

    tv = ET.Element("tv", {
        "generator-info-name": "neural-nest epg builder",
        "generator-info-url": "https://github.com/",
    })
    for cid in sorted(out_channels):
        tv.append(out_channels[cid])
    for pr in out_programmes:
        tv.append(pr)

    buf = io.BytesIO()
    ET.ElementTree(tv).write(buf, encoding="utf-8", xml_declaration=True)
    payload = buf.getvalue()

    with gzip.open(OUT, "wb", compresslevel=9) as fh:
        fh.write(payload)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(
        f"\nwrote {OUT}\n"
        f"  built     : {stamp}\n"
        f"  channels  : {len(out_channels):,}\n"
        f"  programmes: {len(out_programmes):,} "
        f"({seen_prog:,} original + {rewritten:,} rewritten)\n"
        f"  raw       : {len(payload):,} bytes\n"
        f"  gzipped   : {os.path.getsize(OUT):,} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
