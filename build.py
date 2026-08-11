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
from datetime import datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Synthetic "no guide data" channel - NOT upstream data
#
# The provider tags ~446 of its channels with epg_channel_id "TS" and feeds
# them filler programmes titled "TimeShift 14", "TimeShift 15", ... - a fake
# schedule that tells the viewer nothing but looks like a real one.
#
# TiviMate matches per channel, first source wins, and this file is ranked
# above the provider's own guide. So emitting a single honest "TS" channel
# here outranks the filler on every one of those channels at once.
#
# Everything below is fabricated by us on purpose. It is deliberately kept
# out of the merge loop and out of the coverage statistics so that no future
# reader mistakes it for something a source actually published.
# ---------------------------------------------------------------------------
PLACEHOLDER_ID = "TS"
PLACEHOLDER_TITLE = "No guide data"
PLACEHOLDER_DESC = (
    "The provider publishes no schedule for this channel. Any listing you "
    "might otherwise see here is filler, not a real programme guide."
)


def parse_xmltv_time(value: str) -> datetime:
    """Parse an XMLTV timestamp ('20260810220000 +0100') to an aware datetime.

    Sources mix '+0000' and '+0100' in the same file, so times must be
    compared as absolute instants - never as raw strings.
    """
    stamp, _, offset = value.strip().partition(" ")
    dt = datetime.strptime(stamp[:14], "%Y%m%d%H%M%S")
    if len(offset) >= 5 and offset[0] in "+-":
        shift = timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
        return dt.replace(tzinfo=timezone(shift if offset[0] == "+" else -shift))
    return dt.replace(tzinfo=timezone.utc)


def add_placeholder_channel(out_channels: dict, out_programmes: list) -> int:
    """Append the synthetic TS channel, covering the same window as the merge.

    One programme per 24h day: a single long block reads cleanly in the grid,
    whereas a title repeating every 30 minutes is just noise. Returns the
    number of fabricated programmes so the caller can keep its stats honest.
    """
    instants = []
    for pr in out_programmes:
        for attr in ("start", "stop"):
            if pr.get(attr):
                try:
                    instants.append(parse_xmltv_time(pr.get(attr)))
                except ValueError:
                    pass
    if not instants:
        return 0

    # Whole UTC days spanning the real guide's full extent.
    first = min(instants).astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    last = max(instants).astimezone(timezone.utc)

    ch = ET.Element("channel", {"id": PLACEHOLDER_ID})
    ET.SubElement(ch, "display-name").text = PLACEHOLDER_TITLE
    out_channels[PLACEHOLDER_ID] = ch

    fmt = "%Y%m%d%H%M%S +0000"
    made = 0
    day = first
    while day < last:
        nxt = day + timedelta(days=1)
        pr = ET.Element("programme", {
            "channel": PLACEHOLDER_ID,
            "start": day.strftime(fmt),
            "stop": nxt.strftime(fmt),
        })
        ET.SubElement(pr, "title", {"lang": "en"}).text = PLACEHOLDER_TITLE
        ET.SubElement(pr, "desc", {"lang": "en"}).text = PLACEHOLDER_DESC
        out_programmes.append(pr)
        made += 1
        day = nxt
    return made


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

    # Real coverage is measured BEFORE the synthetic channel is added, so the
    # placeholder can never flatter these numbers.
    real_channels = len(out_channels)
    real_programmes = len(out_programmes)
    placeholder_days = add_placeholder_channel(out_channels, out_programmes)

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
        f"  channels  : {real_channels:,}\n"
        f"  programmes: {real_programmes:,} "
        f"({seen_prog:,} original + {rewritten:,} rewritten)\n"
        f"  placeholder: +1 channel ({PLACEHOLDER_ID}), "
        f"{placeholder_days} days - synthetic, excluded from the counts above\n"
        f"  raw       : {len(payload):,} bytes\n"
        f"  gzipped   : {os.path.getsize(OUT):,} bytes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
