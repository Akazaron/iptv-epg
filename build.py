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
OUT_PROXY = os.path.join(HERE, "epg-proxy.xml.gz")


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


# ---------------------------------------------------------------------------
# Second artifact: epg-proxy.xml.gz - case-folded for a case-insensitive proxy
#
# epg.xml.gz deliberately fans one upstream guide out to several provider ids,
# and 43 of those pairs differ ONLY by case (PremierSports1.uk + premiersports1.uk).
# TiviMate matches ids case-SENSITIVELY, so it needs both spellings and that
# file must not change.
#
# A tuliprox proxy collapses ids case-INsensitively, so each of those pairs
# merges and every programme in it appears twice. This second artifact folds
# every id to lowercase up front and merges the collisions, so a given
# (channel id, programme start) survives exactly once.
#
# Nothing here touches OUT / epg.xml.gz: the fold builds new parent elements
# and never mutates the ones the first tree holds.
# ---------------------------------------------------------------------------


def _norm(value: str) -> str:
    """Collapse whitespace so 'A  B' and 'A B' compare equal."""
    return " ".join((value or "").split())


def _norm_instant(value: str) -> str:
    """Normalise an XMLTV timestamp to an absolute UTC instant.

    Sources express the same moment as both '+0000' and '+0100', so raw start
    strings would let a genuine duplicate slip through as two distinct keys.
    """
    try:
        return parse_xmltv_time(value).astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")
    except (ValueError, AttributeError):
        return _norm(value)


def signature(el: ET.Element, skip: tuple = ()) -> str:
    """A whitespace- and timezone-insensitive fingerprint of an element tree.

    Used to tell a true duplicate (drop it) from two sources genuinely
    disagreeing about the same slot (drop it too, but say so out loud).
    """
    parts: list = []

    def walk(e: ET.Element) -> None:
        attrs = []
        for k, v in sorted(e.attrib.items()):
            if k in skip:
                continue
            attrs.append((k, _norm_instant(v) if k in ("start", "stop") else _norm(v)))
        parts.append((e.tag, tuple(attrs), _norm(e.text)))
        for child in e:
            walk(child)
        parts.append(("/", e.tag))

    walk(el)
    return repr(parts)


def _rebrand(el: ET.Element, attr: str, value: str) -> ET.Element:
    """Copy `el` with one attribute changed, sharing its children by reference.

    Children are only ever read (serialised), never mutated, so sharing them
    between the two output trees is safe and avoids deep-copying ~146k nodes.
    """
    new = ET.Element(el.tag, dict(el.attrib))
    new.set(attr, value)
    new.text, new.tail = el.text, el.tail
    new.extend(list(el))
    return new


def build_proxy_tree(out_channels: dict, out_programmes: list) -> tuple:
    """Fold every channel id to lowercase, merging rather than repeating.

    Returns (tv element, stats dict). First occurrence wins, which keeps the
    SOURCES order as the precedence order - the same "first source wins" rule
    the channel merge above already uses.
    """
    channels: dict[str, ET.Element] = {}
    names: dict[str, set] = {}
    for cid in sorted(out_channels):
        low = cid.lower()
        ch = out_channels[cid]
        if low not in channels:
            channels[low] = _rebrand(ch, "id", low)
            names[low] = {_norm(dn.text) for dn in channels[low].findall("display-name")}
            continue
        # Merge: keep the first element, but adopt any display-name spelling
        # the duplicate carried that we do not already have.
        for dn in ch.findall("display-name"):
            if _norm(dn.text) not in names[low]:
                names[low].add(_norm(dn.text))
                channels[low].append(dn)

    programmes: list[ET.Element] = []
    seen: dict[tuple, str] = {}
    dropped_identical = 0
    conflicts: list[tuple] = []

    for pr in out_programmes:
        cid = pr.get("channel")
        if not cid:
            continue
        key = (cid.lower(), _norm_instant(pr.get("start")))
        sig = signature(pr, skip=("channel",))
        if key in seen:
            if seen[key] == sig:
                dropped_identical += 1
            else:
                conflicts.append((key, _norm(pr.findtext("title"))))
            continue
        seen[key] = sig
        programmes.append(_rebrand(pr, "channel", key[0]))

    tv = ET.Element("tv", {
        "generator-info-name": "neural-nest epg builder (case-folded for proxy)",
        "generator-info-url": "https://github.com/",
    })
    for low in sorted(channels):
        tv.append(channels[low])
    for pr in programmes:
        tv.append(pr)

    return tv, {
        "channels": len(channels),
        "programmes": len(programmes),
        "channels_merged": len(out_channels) - len(channels),
        "dropped_identical": dropped_identical,
        "conflicts": conflicts,
    }


def write_proxy(out_channels: dict, out_programmes: list) -> dict:
    tv, stats = build_proxy_tree(out_channels, out_programmes)
    buf = io.BytesIO()
    ET.ElementTree(tv).write(buf, encoding="utf-8", xml_declaration=True)
    payload = buf.getvalue()
    with gzip.open(OUT_PROXY, "wb", compresslevel=9) as fh:
        fh.write(payload)
    stats["raw"] = len(payload)
    stats["gz"] = os.path.getsize(OUT_PROXY)
    return stats


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

    # Second artifact. epg.xml.gz is already on disk and is never revisited.
    px = write_proxy(out_channels, out_programmes)
    print(
        f"\nwrote {OUT_PROXY}  (case-folded, for a case-insensitive proxy)\n"
        f"  channels  : {px['channels']:,} "
        f"({px['channels_merged']:,} case-variant ids merged away)\n"
        f"  programmes: {px['programmes']:,} "
        f"({px['dropped_identical']:,} duplicate rows merged)\n"
        f"  raw       : {px['raw']:,} bytes\n"
        f"  gzipped   : {px['gz']:,} bytes"
    )
    if px["conflicts"]:
        # Same channel, same start, different content: two sources disagree.
        # First-in-SOURCES-order wins, but never silently - a real schedule
        # conflict (rather than richer metadata) is something to go look at.
        by_channel: dict[str, int] = {}
        for (cid, _start), _title in px["conflicts"]:
            by_channel[cid] = by_channel.get(cid, 0) + 1
        top = sorted(by_channel.items(), key=lambda kv: -kv[1])[:10]
        print(
            f"  conflicts : {len(px['conflicts']):,} slots where sources disagreed "
            f"across {len(by_channel)} channels - kept the first, dropped the rest"
        )
        for cid, n in top:
            print(f"::warning::proxy fold: {cid} had {n} conflicting slots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
