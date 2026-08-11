# Corrected IPTV EPG

A single XMLTV guide for a TiviMate **Xtream Codes** playlist whose provider ships a
poor EPG.

## The problem this solves

The provider's own `xmltv.php` is:

- **~1 day deep** — 94,021 programmes over a 2-day window
- **mostly unmapped** — only 14.5% of its 57,082 live streams carry a `tvg-id`
  at all, and only 7.2% point at an id that holds any programmes
- **wrong in places** — `Channel4.uk`, `Channel5.uk` and `BBCNews.uk` are all
  published on live channels and all return **zero** programmes
- **padded with a placeholder** — **446 channels** share the id `TS`, whose
  "programmes" are literally titled `TimeShift 14`, `TimeShift 15`, …

Good public UK/IE guides exist, but they use different channel-id conventions.
TiviMate matches **strictly on `tvg-id`**, so adding them straight up fixes
almost nothing: across 3,959 provider ids there were only **21 verbatim
matches**.

This repo merges the public sources and **rewrites their channel ids to the ids
the provider actually sends**, producing one file TiviMate can use.

## Result

| | Live-TV channels with a guide |
|---|---|
| Provider alone | 214 / 712 — **30.1%** *(plus 62 showing fake `TimeShift` filler)* |
| All four public sources added separately | ~300 — 42% |
| **This file, on its own** | **366 — 51.4%** |

It also replaces four EPG sources with one, and carries **6+ days** of data
instead of the provider's one.

## Use it

In TiviMate: **Settings ▸ EPG ▸ EPG sources ▸ Add source**

```
https://raw.githubusercontent.com/<YOUR-USER>/<YOUR-REPO>/epg/epg.xml.gz
```

Keep the provider's own EPG enabled **below** it — the top source wins per
channel and lower ones fill gaps.

### For a case-insensitive consumer (tuliprox etc.)

```
https://raw.githubusercontent.com/<YOUR-USER>/<YOUR-REPO>/epg/epg-proxy.xml.gz
```

Same guide, **channel ids folded to lowercase and the collisions merged**.

`epg.xml.gz` deliberately publishes 43 id pairs that differ only by case
(`PremierSports1.uk` *and* `premiersports1.uk`), because TiviMate matches
`tvg-id` **case-sensitively** and needs whichever spelling the provider sends.
A consumer that folds case instead sees those pairs as one channel and shows
**every programme twice** — `bbc1.uk` arrives as 290 programmes over 145 real
slots. `epg-proxy.xml.gz` does the fold up front and merges, so each
(channel, start) appears exactly once: 1,313 channels / 137,502 programmes
against the main file's 1,359 / 146,344.

**Use one or the other, not both** — they are the same guide under two id
conventions.

## How it works

- `build.py` downloads the four upstream sources, applies `mapping.json`, and
  writes `epg.xml.gz`, then folds a second copy down to `epg-proxy.xml.gz`.
  **No credentials are used or needed.**
- `.github/workflows/build.yml` runs it daily at 04:20 UTC and force-pushes both
  files to an orphan `epg` branch, so the repo never accumulates a
  multi-megabyte binary in its history.
- The workflow refuses to publish **either** file if it is under 1 MB, is not
  valid gzip, is not valid XML, or has fewer than 100 channels / 10,000
  programmes — a dead upstream source can't quietly replace a working guide with
  an empty one. `epg-proxy.xml.gz` additionally has to prove every id is
  lowercase and no (channel, start) slot repeats, so the fold can't silently
  regress into the double-listing it exists to prevent.

### Upstream sources

| Source | Why |
|---|---|
| [Freeview-EPG](https://github.com/dp247/Freeview-EPG) | Best UK terrestrial coverage, ~6.4 days, and the only one carrying `BBCOneNorthernIreland.uk` / `BBCTwoNorthernIreland.uk` |
| epgshare01 UK1 / IE1 | Sky, TNT, Virgin Media, RTE |
| open-epg UK1 | fills a few remaining gaps |

`epg.lat` is deliberately absent — it is a byte-identical mirror of epgshare01.
`iptv-org` no longer publishes prebuilt guides, and `xmltv.se` is dead.

## `mapping.json`

`provider tvg-id` → `upstream channel id`. 114 entries, generated once from the
panel's channel list by normalising channel names and matching them across
sources.

Entries were auto-applied only where the provider's own id text corroborated the
name match; the rest were reviewed by hand. `mapping-review-unapplied.csv` holds
the candidates that were **not** applied — some are correct and just need
eyeballing, others were rejected outright (`W.uk` was matching `QuestRedPlus1.uk`,
which is simply wrong).

**Regenerate it only if the provider renumbers its channels.** It does not need
to change when schedules change.

## What this cannot fix

A `tvg-id` is the only hook TiviMate has, so two groups are permanently out of
reach of *any* EPG file:

- channels the provider sends with **no `tvg-id`**
- channels sharing the placeholder **`TS`** id — one id cannot carry two
  different schedules

Those need a per-channel **Assign EPG** in the app. Most of them are duplicate
copies of channels that already work, so favouriting the copy that has a real id
is usually the cheaper fix.
