#!/usr/bin/env python3
"""
xtream2m3u.py - Turn an Xtream Codes login into an M3U playlist.

Usage:
    python xtream2m3u.py http://provider.com:8080 USERNAME PASSWORD
    python xtream2m3u.py http://provider.com USER PASS --movies --series -o my.m3u
    python xtream2m3u.py http://provider.com USER PASS --no-uk   (drop all UK groups)

Live TV goes to playlist.m3u (-o to rename). Movies, series and adult content
(--movies, --series, --keep-adult) go to separate files next to it:
playlist_movies.m3u, playlist_series.m3u, playlist_adult.m3u (--single puts
everything in one file). Any file over --max-mb (default 95, under GitHub's
100 MB limit) is split into _part2, _part3...

By default live TV is filtered to US, Canada, UK and sports groups, and
adult categories are removed everywhere (--keep-adult keeps them). Only
category names are checked, not channel or title names. Groups starting with a code in
DEFAULT_EXCLUDE (AR, FR, DE, AU, ...) are always dropped, even sports, and so
are Canadian French groups ("CA| ... FR"). Names are tidied: "EN" is dropped
from the end of group names and NAME_PREFIXES ("US:", "CA EN:", ...) from
the start of channel names. Use --countries, --no-sports, --exclude or --all to change the
live TV filter. Movie and series categories are kept only if their name
starts with a word in VOD_ALLOW (ENGLISH, EN, NETFLIX, DISNEY+, ...)
(--vod-all keeps them all), and series episodes are fetched --workers at a
time (default 10). --list-categories saves every category name, marked
KEEP or DROP, to categories.txt.

The TV guide is saved to EPG.xml (--epg-file to rename, --no-epg to skip),
trimmed to just the live channels kept in the playlist. Channels in the
groups listed in GAME_EPG_GROUPS (NHL, TSN+, Sportsnet, Sportsnet+,
Paramount+, FIFA+, UFC, PPV EVENT, CBC and ESPN+ PPV) get guide entries made from
their names, e.g. "NHL 01: 7pm Mammoth @ Canadiens".

Only uses the Python standard library - nothing to install.
"""
import argparse
import concurrent.futures
import datetime
import gzip
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (xtream2m3u)"


def api(base, user, pw, action=None, **extra):
    params = {"username": user, "password": pw}
    if action:
        params["action"] = action
    params.update(extra)
    url = f"{base}/player_api.php?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def clean(s):
    return str(s or "").replace('"', "'").replace("\n", " ").strip()


def extinf(name, group, logo="", tvg_id=""):
    return (f'#EXTINF:-1 tvg-id="{clean(tvg_id)}" tvg-name="{clean(name)}" '
            f'tvg-logo="{clean(logo)}" group-title="{clean(group)}",{clean(name)}')


def format_expiry(exp):
    """Turn the panel's Unix timestamp into a readable date plus days left."""
    if exp in (None, "", "0", 0, "null"):
        return "Never (unlimited)"
    try:
        ts = int(float(exp))
    except (TypeError, ValueError):
        return str(exp)
    when = datetime.datetime.fromtimestamp(ts)
    days = (when.date() - datetime.date.today()).days
    if days > 0:
        left = f"{days} day{'s' if days != 1 else ''} left"
    elif days == 0:
        left = "expires today"
    else:
        left = f"EXPIRED {-days} day{'s' if days != -1 else ''} ago"
    return f"{when:%a %b %d, %Y %I:%M %p} ({left})"


def xmltv_time(t):
    """Parse an XMLTV time like '20261107220000 +0100'. Returns None if invalid."""
    t = str(t or "").strip()
    try:
        when = datetime.datetime.strptime(t[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    m = re.search(r"([+-])(\d\d):?(\d\d)$", t[14:].strip())
    if m:
        off = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
        when -= off if m.group(1) == "+" else -off
    return when


# Groups whose channel names carry the event, and the label shown in the guide.
# Name styles understood (times are Eastern unless the name says otherwise):
#   "NHL 01: 7pm Mammoth @ Canadiens"                          12-hour, today
#   "LIVE EVENT 06 - 11am/2pm Buatsi v Hutchinson 2"           first time of a range
#   "Boxing 6: Canelo vs Mbilli UK 2:00pm"                     UK time
#   "LaLiga: Valencia vs. Real Madrid @ Nov 7 14:30 :TSN+ 57"  date + 24-hour
#   "NHL: Montreal vs Toronto @ Nov 7 7:00 PM :Sportsnet+ 18"  date + 12-hour
#   "LIVE | ESPOIR FC VS. AS FAN | Sat 07 Nov 10:15 EST (US)"  FIFA+
#   "PPV EVENT 10: UFC FN Acosta vs. Walker (11.7 8:00 PM ET)" month.day
#   "CBC 8: sports|USPORTS Mens Rugby - 19/10 14:00"           day/month
#   "UFC 01 : ... start:2026-11-08 01:55:00 stop:2026-11-08 06:00:00"  CET
#   "US (ESPN+ 040) | North Dakota vs. Winthrop Nov 07 12:00PM ET (2026-11-07 12:00:05)"
GAME_EPG_GROUPS = {
    "US| NHL PPV": "NHL",
    "CA| TSN+ PPV": "TSN+",
    "CA| SPORTSNET PPV": "Sportsnet",
    "CA| SPORTSNET+ PPV": "Sportsnet+",
    "US| PARAMOUNT+ PPV": "Paramount+",
    "US| FIFA+ PPV": "FIFA+",
    "US| UFC PPV": "UFC",
    "US| PPV EVENT": "PPV",
    "US| PPV EVENT ⁽ᴮᴷ⁾": "PPV",
    "CA| CBC PPV": "CBC",
    "US| ESPN+ PPV": "ESPN+",
    "US| ESPN+ PPV ⱽᴵᴾ": "ESPN+",
}
GAME_HOURS = 3  # default length of an event in the guide
LABEL_HOURS = {"FIFA+": 2}  # default length per group label
# Shorter shows, matched against the start of the event title (upper case)
SHORT_SHOWS = {"SPORTSCENTRE": 1, "SC:": 1, "SPORTSNET CENTRAL": 1, "ESPN COLLEGE GAMEDAY": 3}
UFC_TZ = "CET"  # the UFC PPV start:/stop: times are Central European time

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
MON = r"(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?"
STARTSTOP_RE = re.compile(r"^(.*?)\s*start:\s*(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::\d\d)?"
                          r"(?:\s*stop:\s*(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d))?", re.I)
FIFA_RE = re.compile(r"\|\s*([^|]+?)\s*\|\s*(?:[A-Z]{3}\s+)?(\d{1,2})\s+" + MON +
                     r"\s+(\d{1,2}):(\d\d)\s*(EST|EDT|ET|GMT|UTC|BST|UK)?", re.I)
PAREN_RE = re.compile(r"^(.*?)\s*\(\s*(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d\d)\s*([ap])\.?m\.?"
                      r"\s*(ET|EST|EDT|GMT|UTC|UK)?\s*\)", re.I)
ISO_RE = re.compile(r"^(.*?)\s*\((\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)(?::\d\d)?\)\s*$")
DMY_RE = re.compile(r"^(.*?)\s*-\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d\d)\s*$")
DATED_RE = re.compile(r"^(.*?)\s*@\s*" + MON + r"\s+(\d{1,2})\s+(\d{1,2}):(\d\d)(?:\s*([ap])\.?m\b)?", re.I)
ZONED12_RE = re.compile(r"^(.*?)\s+(UK|GMT|BST|ET|EST|EDT|PT|PST|PDT)\s+(\d{1,2})(?::(\d\d))?\s*([ap])m\s*$", re.I)
TIME12_RE = re.compile(r"(?<![\d.:/])(\d{1,2})(?::(\d\d))?\s*(am|pm)?"
                       r"(?:\s*/\s*\d{1,2}(?::\d\d)?\s*(am|pm))?\s+(\S.*)$", re.I)
# "Boxing 1 |", "LIVE EVENT 20 -", "PPV EVENT 01:", "UFC 00 :", "CBC 1: sports|"
SLOT_RE = re.compile(r"^\s*[A-Za-z+ ]*?\d{1,3}\s*[:|\-]\s*(?:sports\s*\|\s*)?", re.I)


def xmltv_fmt(dt):
    return dt.strftime("%Y%m%d%H%M%S %z")


def local_time(y, mo, d, h=0, mi=0):
    # Naive local time -> aware, with the right daylight-saving offset for that date
    return datetime.datetime(y, mo, d, h, mi).astimezone()


def eu_summer(y, mo, d):
    # EU/UK summer time: last Sunday of March to last Sunday of October
    def last_sunday(month):
        dt = datetime.date(y, month, 31)
        return dt - datetime.timedelta(days=(dt.weekday() + 1) % 7)
    return last_sunday(3) <= datetime.date(y, mo, d) < last_sunday(10)


def zoned_time(y, mo, d, h, mi, tz=None):
    """Time in a named zone -> your local time. No zone / ET means local."""
    tz = (tz or "").upper()
    offsets = {"UTC": 0, "GMT": 0, "BST": 1, "PST": -8, "PDT": -7}
    if tz in ("UK", "CET"):
        off = (1 if tz == "UK" else 2) if eu_summer(y, mo, d) else (0 if tz == "UK" else 1)
    elif tz == "PT":
        off = -7 if local_time(y, mo, d).utcoffset() == datetime.timedelta(hours=-4) else -8
    elif tz in offsets:
        off = offsets[tz]
    else:
        return local_time(y, mo, d, h, mi)
    zone = datetime.timezone(datetime.timedelta(hours=off))
    return datetime.datetime(y, mo, d, h, mi, tzinfo=zone).astimezone()


def guess_year(mo):
    now = datetime.date.today()
    return now.year + (1 if mo - now.month < -6 else -1 if mo - now.month > 6 else 0)


def hour24(h, ampm):
    h = int(h)
    return h % 12 + (12 if ampm.lower().startswith("p") else 0) if ampm else h


def tidy_title(t):
    t = re.sub(r"^\s*[A-Z]{2}\s*\([^)]*\)\s*\|\s*", "", str(t or ""))  # "US (ESPN+ 001) |"
    t = SLOT_RE.sub("", t)
    # trailing "Nov 07 2:55AM ET" (the time is read from elsewhere in the name)
    t = re.sub(r"\s+" + MON + r"\s+\d{1,2}\s+\d{1,2}:\d\d\s*[AP]M\s*(?:ET|EST|EDT)?\s*$", "", t, flags=re.I)
    m = re.search(r"\s*\(([^()]*)\)\s*$", t)  # "Jaguars vs. Ravens (Jaguars vs. Ravens)"
    if m and m.group(1).strip() and m.group(1).strip() in t[:m.start()]:
        t = t[:m.start()]
    t = re.sub(r"^\s*(?:LIVE|NEXT|ENDED|END)\s*\|\s*", "", t, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip(" -|:")


def parse_event(name):
    """Find (start, end_or_None, title) in a channel name, or None."""
    today = datetime.date.today()
    m = STARTSTOP_RE.search(name)
    if m:
        start = zoned_time(*map(int, m.group(2, 3, 4, 5, 6)), UFC_TZ)
        end = zoned_time(*map(int, m.group(7, 8, 9, 10, 11)), UFC_TZ) if m.group(7) else None
        return start, end, m.group(1)
    m = ISO_RE.search(name)
    if m:
        y = int(m.group(2))
        if y > today.year + 1:  # placeholder date on an empty slot, e.g. 2098-12-31
            return None, None, ""
        return local_time(y, *map(int, m.group(3, 4, 5, 6))), None, m.group(1)
    m = FIFA_RE.search(name)
    if m:
        mo = MONTHS[m.group(3).upper()[:3]]
        return zoned_time(guess_year(mo), mo, int(m.group(2)), int(m.group(4)),
                          int(m.group(5)), m.group(6)), None, m.group(1)
    m = PAREN_RE.search(name)
    if m:
        mo = int(m.group(2))
        return zoned_time(guess_year(mo), mo, int(m.group(3)), hour24(m.group(4), m.group(6)),
                          int(m.group(5)), m.group(7)), None, m.group(1)
    m = DMY_RE.search(name)
    if m:
        mo = int(m.group(3))
        return local_time(guess_year(mo), mo, int(m.group(2)), int(m.group(4)),
                          int(m.group(5))), None, m.group(1)
    m = DATED_RE.search(name)
    if m:
        mo = MONTHS[m.group(2).upper()[:3]]
        return local_time(guess_year(mo), mo, int(m.group(3)), hour24(m.group(4), m.group(6)),
                          int(m.group(5))), None, m.group(1)
    m = ZONED12_RE.search(name)
    if m:
        return zoned_time(today.year, today.month, today.day, hour24(m.group(3), m.group(5)),
                          int(m.group(4) or 0), m.group(2)), None, m.group(1)
    pos = 0
    while True:  # first "7pm" / "9:25am" / "11am/2pm" style time in the name
        m = TIME12_RE.search(name, pos)
        if not m:
            return None
        ampm = m.group(3) or m.group(4)
        if ampm:
            return local_time(today.year, today.month, today.day, hour24(m.group(1), ampm),
                              int(m.group(2) or 0)), None, m.group(5)
        pos = m.start() + 1


def event_length(title, label=""):
    up = title.upper()
    if " IN 30" in up:
        return datetime.timedelta(minutes=30)
    for prefix, hours in SHORT_SHOWS.items():
        if up.startswith(prefix):
            return datetime.timedelta(hours=hours)
    return datetime.timedelta(hours=LABEL_HOURS.get(label, GAME_HOURS))


def game_programmes(name, label="NHL"):
    """Guide entries for one event channel, built from its name."""
    now = datetime.datetime.now()
    day = local_time(now.year, now.month, now.day)
    day_end = day + datetime.timedelta(days=1, hours=6)
    name = name.strip()

    try:
        found = parse_event(name)
    except ValueError:  # impossible date such as 31/02
        found = None
    title = tidy_title(found[2]) if found else ""
    if not found or not title:
        rest = tidy_title(name)
        if (not rest or found or name.endswith(("-", ":")) or name.startswith(":")
                or "NO EVENT" in name.upper() or re.fullmatch(r"[A-Za-z+ ]*\d{1,3}", name)):
            return [(day, day_end, "No event scheduled", f"Nothing on this {label} channel right now.")]
        return [(day, day_end, rest, name)]  # a normal channel, e.g. "BOX OFFICE HD"

    start, end, _ = found
    if not end or end <= start:
        end = start + event_length(title, label)
    nice = title.replace(" @ ", " at ")
    when = start.strftime("%a %b %d, %I:%M %p").replace(" 0", " ")
    out = []
    if start > day:
        out.append((day, start, f"Up next: {title}", f"{label}: {nice} starts {when}."))
    out.append((start, end, title, f"{label}: {nice}"))
    out.append((end, max(day_end, end + datetime.timedelta(hours=1)), f"Ended: {title}",
                f"{label}: {nice} has ended."))
    return out


def game_epg_xml(extra):
    """XML for generated channels: extra is a list of (tvg_id, name, logo, label)."""
    chans, progs = [], []
    for cid, name, logo, label in extra:
        ch = ET.Element("channel", id=cid)
        ET.SubElement(ch, "display-name").text = name
        if logo:
            ET.SubElement(ch, "icon", src=logo)
        ch.tail = "\n"
        chans.append(ET.tostring(ch, encoding="utf-8"))
        for start, stop, title, desc in game_programmes(name, label):
            pr = ET.Element("programme", start=xmltv_fmt(start), stop=xmltv_fmt(stop), channel=cid)
            ET.SubElement(pr, "title").text = title
            ET.SubElement(pr, "desc").text = desc
            ET.SubElement(pr, "category").text = "Sports"
            pr.tail = "\n"
            progs.append(ET.tostring(pr, encoding="utf-8"))
    return chans, progs


def save_epg(base, user, pw, keep_ids, path, extra=()):
    """Download xmltv.php and save a clean copy, keeping only channels in keep_ids.

    Cleans up common provider problems so strict players (e.g. TiviMate)
    accept it: duplicate channels and programmes, programmes for channels
    that were never declared, bad or backwards start/stop times, and
    channels with no programmes at all. If keep_ids is empty the full
    guide is kept. extra adds generated game channels (see game_epg_xml).
    Returns (channels, programmes) written.
    """
    params = urllib.parse.urlencode({"username": user, "password": pw})
    req = urllib.request.Request(f"{base}/xmltv.php?{params}", headers={"User-Agent": UA})
    channels = {}      # lower id -> (real id, element bytes)
    programmes = []    # (lower id, start, element)
    seen = set()
    with urllib.request.urlopen(req, timeout=300) as r:
        src = r
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or \
                r.headers.get("Content-Type", "").endswith("gzip"):
            src = gzip.GzipFile(fileobj=r)
        ctx = ET.iterparse(src, events=("start", "end"))
        _, root = next(ctx)
        attrs = "".join(f' {k}="{clean(v)}"' for k, v in root.attrib.items())
        for ev, el in ctx:
            if ev != "end" or el.tag not in ("channel", "programme"):
                continue
            if el.tag == "channel":
                cid = str(el.get("id") or "").strip()
                low = cid.lower()
                if cid and (not keep_ids or low in keep_ids) and low not in channels:
                    el.tail = "\n"
                    channels[low] = (cid, ET.tostring(el, encoding="utf-8"))
            else:
                low = str(el.get("channel") or "").strip().lower()
                start, stop = xmltv_time(el.get("start")), el.get("stop")
                ok_time = start and (stop is None or ((xmltv_time(stop) or start) > start))
                key = (low, el.get("start"))
                if low and ok_time and (not keep_ids or low in keep_ids) and key not in seen:
                    seen.add(key)
                    el.tail = "\n"
                    programmes.append((low, el))
                    root.clear()
                    continue
            root.clear()  # free memory as we go

    # Only keep channels that have programmes, and programmes whose channel is declared
    used = {low for low, _ in programmes if low in channels}
    extra_chans, extra_progs = game_epg_xml(extra)
    tmp = path + ".tmp"
    with open(tmp, "wb") as out:
        out.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<tv{attrs}>\n'.encode("utf-8"))
        for low, (cid, data) in channels.items():
            if low in used:
                out.write(data)
        for data in extra_chans:
            out.write(data)
        progs = len(extra_progs)
        for low, el in programmes:
            if low in used:
                el.set("channel", channels[low][0])  # match the declared id exactly
                out.write(ET.tostring(el, encoding="utf-8"))
                progs += 1
        for data in extra_progs:
            out.write(data)
        out.write(b"</tv>\n")
    os.replace(tmp, path)
    return len(used) + len(extra_chans), progs


# ---------------------------------------------------------------- name cleanup
# Prefixes removed from the start of live channel names, e.g. "CA EN: CTV" -> "CTV".
# Longer ones first so "CA EN:" wins over "CA:". Add more here (e.g. "TV", "RK").
NAME_PREFIXES = ["CA EN", "US", "CA", "UK"]
NAME_PREFIX_RE = re.compile(r"^\s*(?:%s)\s*:\s*" % "|".join(map(re.escape, NAME_PREFIXES)), re.I)


def tidy_name(name):
    return NAME_PREFIX_RE.sub("", name).strip() or name


def tidy_group(group):
    # "CA| GENERAL EN" -> "CA| GENERAL"
    return re.sub(r"\s+EN\s*$", "", str(group or ""), flags=re.I).strip() or group


# ---------------------------------------------------------------- filters
# Words in a group/category name that identify each country.
COUNTRY_TOKENS = {
    "us": {"US", "USA"},
    "ca": {"CA", "CAN", "CANADA", "CANADIAN"},
    "uk": {"UK", "GB", "GBR", "BRITISH", "ENGLAND", "SCOTLAND", "WALES"},
}
COUNTRY_PHRASES = {
    "us": ["UNITED STATES", "AMERICAN"],
    "uk": ["UNITED KINGDOM", "GREAT BRITAIN"],
}
# Groups tagged with these are always dropped, even sports (--exclude to change)
EXCLUDE_TOKENS = {
    "ar": {"AR", "ARA", "ARAB", "ARABIC", "ARABIA", "عربي"},
    "it": {"IT", "ITA", "ITALY", "ITALIA", "ITALIAN"},
    "be": {"BE", "BEL", "BELGIUM", "BELGIQUE", "BELGIE"},
    "uk": {"UK", "GB", "GBR"},
}
# Group name prefixes dropped by default (edit this list to change it)
DEFAULT_EXCLUDE = ("ar,br,mx,cr,id,kr,asia,wt,ku,tr,nz,au,al,is,fi,no,dk,se,pl,"
                   "ch,pt,es,fr,it,be,nl,at,de,ts,ie,gr,hr")
# Movie/series categories are an allow-list: only names starting with one of
# these words are kept (your provider names foreign ones "ESPAÑA SERIES",
# "FRANCE ANIME", "DE - ACTION", ...). Add a word here to keep more.
VOD_ALLOW = ["ENGLISH", "EN", "US", "USA", "UK", "CANADA", "CA",
             "AMAZON", "APPLE", "APPLE+", "DISNEY", "DISNEY+", "DISCOVERY", "DISCOVERY+",
             "HBO", "MAX", "MARVEL", "NETFLIX", "NICKELODEON", "PARAMOUNT", "PARAMOUNT+",
             "PEACOCK", "SHOWTIME", "SKY", "HULU", "STARZ", "AMC", "BBC", "CRAVE",
             "CRUNCHYROLL", "FIFA+", "DREAMWORKS", "UNIVERSAL", "PIXAR", "JAMES",
             "TOP", "SOCCER", "WWE", "UFC"]
# ...unless the name also contains one of these (foreign versions of those brands)
VOD_DENY = ["MENA", "ARABIA", "ARABIC", "ASIA", "(TR)", "(FR)", "(NL)", "(DE)",
            "(ES)", "(IT)", "(PT)", "(AR)"]
# Groups containing any of these words anywhere are also dropped
EXCLUDE_WORDS = {"UKRAINE", "UKRAINIAN"}
SPORTS_WORDS = ["SPORT", "NFL", "NBA", "NHL", "MLB", "MLS", "NCAA", "UFC", "PPV",
                "ESPN", "DAZN", "F1", "FORMULA", "GOLF", "TENNIS", "SOCCER",
                "FOOTBALL", "BOXING", "WWE", "RUGBY", "CRICKET", "RACING"]
ADULT_WORDS = ["ADULT", "XXX", "18+", "+18", "PORN", "EROTIC", "PLAYBOY", "HUSTLER",
               "BRAZZERS", "PENTHOUSE", "VIVID", "REDLIGHT", "SEXY", "NSFW",
               "FOR ADULTS", "XXL", "HOT CLUB", "DORCEL", "BLUE HUSTLER", "ONLYFANS"]


def tokens(text):
    return set(re.split(r"[^A-Z0-9+]+", str(text).upper())) - {""}


def is_adult(*texts, flag=None):
    if str(flag) == "1":
        return True
    for t in texts:
        up = re.sub(r"ADULT[\s\-_]*SWIM", "", str(t or "").upper())  # cartoon channel, not adult
        toks = tokens(up)
        for w in ADULT_WORDS:
            if (" " in w and w in up) or w in toks or (w in ("XXX", "PORN", "ADULT") and w in up):
                return True
    return False


def is_sports(group):
    up = str(group or "").upper()
    toks = tokens(up)
    return any(w in up if w == "SPORT" else w in toks for w in SPORTS_WORDS)


def country_of(group, wanted):
    up = str(group or "").upper()
    toks = tokens(up)
    for c in wanted:
        if toks & COUNTRY_TOKENS.get(c, {c.upper()}):
            return True
        if any(p in up for p in COUNTRY_PHRASES.get(c, [])):
            return True
    return False


def excluded(group, codes):
    # Look only at the first word of the group name, e.g. "FR| SPORTS" -> "FR"
    words = [w for w in re.split(r"[^\w+]+", str(group or "").upper()) if w]
    if not words:
        return False
    if EXCLUDE_WORDS & set(words):
        return True
    first = words[0]
    return any(first in EXCLUDE_TOKENS.get(c, {c.upper()}) for c in codes)


def is_french_canada(group):
    # Canadian French groups, e.g. "CA| GENERAL FR", "CA| SPORTS FR"
    words = [w for w in re.split(r"[^\w+]+", str(group or "").upper()) if w]
    return len(words) > 1 and words[0] in COUNTRY_TOKENS["ca"] and words[-1] == "FR"


def is_separator(name):
    # Dummy "##### US NEWS #####" style entries some providers include
    s = str(name or "").strip()
    return not s or bool(re.match(r"^[#\-=*~_|.\s]{3,}", s)) or bool(re.search(r"[#=*~]{3,}\s*$", s))


def cat_map(base, user, pw, action):
    try:
        return {str(c["category_id"]): c["category_name"] for c in api(base, user, pw, action) or []}
    except Exception:
        return {}


def main():
    p = argparse.ArgumentParser(description="Xtream Codes login -> M3U playlist")
    p.add_argument("server", help="Server URL, e.g. http://provider.com:8080")
    p.add_argument("username")
    p.add_argument("password")
    p.add_argument("-o", "--output", default="playlist.m3u")
    p.add_argument("--format", choices=["ts", "m3u8"], default="ts",
                   help="Live stream format (default ts; try m3u8 if streams won't play)")
    p.add_argument("--movies", action="store_true", help="Include movies (VOD)")
    p.add_argument("--series", action="store_true",
                   help="Include TV series episodes (slow: one request per show)")
    p.add_argument("--countries", default="us,ca,uk",
                   help="Live TV countries to keep, comma-separated (default us,ca,uk)")
    p.add_argument("--no-sports", action="store_true",
                   help="Don't keep sports groups from other countries")
    p.add_argument("--all", action="store_true",
                   help="Keep every country (adult content is still removed)")
    p.add_argument("--exclude", default=DEFAULT_EXCLUDE,
                   help="Group prefixes to always drop, even sports, comma-separated")
    p.add_argument("--no-uk", action="store_true",
                   help="Remove all UK groups, including UK sports")
    p.add_argument("--workers", type=int, default=10,
                   help="Series shows fetched at once (default 10; lower it if the provider complains)")
    p.add_argument("--vod-all", action="store_true",
                   help="Keep movie/series categories from every language")
    p.add_argument("--keep-adult", action="store_true",
                   help="Don't remove adult channels, movies and series")
    p.add_argument("--single", action="store_true",
                   help="Put everything in one .m3u instead of separate movies/series/adult files")
    p.add_argument("--max-mb", type=float, default=95,
                   help="Split any .m3u bigger than this into _part2, _part3... (default 95; GitHub's limit is 100)")
    p.add_argument("--list-categories", action="store_true",
                   help="Save all category names to categories.txt (KEEP/DROP) and stop")
    p.add_argument("--epg-file", default="EPG.xml", help="Where to save the TV guide (default EPG.xml)")
    p.add_argument("--no-epg", action="store_true", help="Don't download the TV guide")
    a = p.parse_args()

    def adult(group):
        # Adult categories are removed; channel/movie/show names are never checked,
        # so e.g. "Dirty Sexy Money" in a normal category is kept
        return False if a.keep_adult else is_adult(group)
    wanted = [c.strip().lower() for c in a.countries.split(",") if c.strip()]
    drop = [c.strip().lower() for c in a.exclude.split(",") if c.strip()]
    if a.no_uk:
        wanted = [c for c in wanted if c != "uk"]
        drop.append("uk")

    def keep_live(group):
        if excluded(group, drop) or is_french_canada(group):
            return False
        if a.all:
            return True
        return country_of(group, wanted) or (not a.no_sports and is_sports(group))

    allow = {w.upper() for w in VOD_ALLOW}

    def keep_vod(group):
        # Movie/series categories: keep only VOD_ALLOW names, minus VOD_DENY
        up = str(group or "").upper()
        if is_adult(group):
            return a.keep_adult  # adult categories: only with --keep-adult
        if a.all or a.vod_all:
            return True
        if any(w in up for w in VOD_DENY):
            return False
        first = (re.split(r"[\s\-|:]+", up.strip()) or [""])[0]
        return first in allow

    base = a.server.rstrip("/")
    if not base.startswith("http"):
        base = "http://" + base
    u, pw = a.username, a.password

    # Check the login
    info = api(base, u, pw)
    ui = info.get("user_info", {}) if isinstance(info, dict) else {}
    if str(ui.get("auth")) != "1":
        sys.exit("Login failed - check the server URL, username and password.")
    print(f"Logged in. Status: {ui.get('status')}")
    print(f"Expires: {format_expiry(ui.get('exp_date'))}")

    if a.list_categories:
        out = []
        for title, action, keep in (("LIVE", "get_live_categories", keep_live),
                                    ("MOVIES", "get_vod_categories", keep_vod),
                                    ("SERIES", "get_series_categories", keep_vod)):
            cats = cat_map(base, u, pw, action)
            kept = sum(1 for g in cats.values() if keep(g))
            out.append(f"===== {title}: keeping {kept} of {len(cats)}")
            out += [f"{'KEEP' if keep(g) else 'DROP'}  {g}" for g in sorted(cats.values(), key=str.upper)]
            out.append("")
            print(f"{title}: keeping {kept} of {len(cats)} categories")
        with open("categories.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(out))
        print("Saved categories.txt")
        return

    lines = ["#EXTM3U"]          # live TV
    movie_lines = ["#EXTM3U"]
    series_lines = ["#EXTM3U"]
    adult_lines = ["#EXTM3U"]    # only filled with --keep-adult
    movie_tag = series_tag = ""  # group prefix, only needed when everything shares one file
    if a.single:                 # everything into the one file
        movie_lines = series_lines = adult_lines = lines
        movie_tag, series_tag = "Movies: ", "Series: "
    q = urllib.parse.quote
    U, P = q(u, safe=""), q(pw, safe="")

    # Live TV
    cats = cat_map(base, u, pw, "get_live_categories")
    live = api(base, u, pw, "get_live_streams") or []
    kept, seen, epg_ids, game_chans = 0, set(), set(), []
    game_groups = {g.upper(): label for g, label in GAME_EPG_GROUPS.items()}
    for s in live:
        name = clean(s.get("name"))
        group = cats.get(str(s.get("category_id")), "Live")
        if adult(group):  # adult filtering looks at the category only
            continue
        if is_separator(name):
            continue
        if not keep_live(group) and not (a.keep_adult and is_adult(group)):
            continue
        key = (group.lower(), name.lower())
        if key in seen:  # drop exact duplicates
            continue
        seen.add(key)
        tvg_id = s.get("epg_channel_id")
        show = tidy_name(name)
        if group.upper() in game_groups:  # guide made from the channel name
            tvg_id = f"game.{s['stream_id']}"
            game_chans.append((tvg_id, show, s.get("stream_icon"), game_groups[group.upper()]))
        out = adult_lines if is_adult(group) else lines
        out.append(extinf(show, tidy_group(group), s.get("stream_icon"), tvg_id))
        out.append(f"{base}/live/{U}/{P}/{s['stream_id']}.{a.format}")
        if s.get("epg_channel_id") and tvg_id == s.get("epg_channel_id"):
            epg_ids.add(str(s["epg_channel_id"]).lower())
        kept += 1
    print(f"Live channels: kept {kept} of {len(live)}")

    # Movies
    if a.movies:
        cats = cat_map(base, u, pw, "get_vod_categories")
        vod = api(base, u, pw, "get_vod_streams") or []
        kept = 0
        good = {cid for cid, g in cats.items() if keep_vod(g)}
        print(f"Movie categories: keeping {len(good)} of {len(cats)}")
        for s in vod:
            cid = str(s.get("category_id"))
            group = cats.get(cid, "Other")
            if cats and cid not in good:
                continue
            if adult(group):
                continue
            ext = s.get("container_extension") or "mp4"
            out = adult_lines if is_adult(group) else movie_lines
            out.append(extinf(s.get("name"), movie_tag + tidy_group(group), s.get("stream_icon")))
            out.append(f"{base}/movie/{U}/{P}/{s['stream_id']}.{ext}")
            kept += 1
        print(f"Movies: kept {kept} of {len(vod)}")

    # Series
    if a.series:
        cats = cat_map(base, u, pw, "get_series_categories")
        shows = api(base, u, pw, "get_series") or []
        good = {cid for cid, g in cats.items() if keep_vod(g)}
        print(f"Series categories: keeping {len(good)} of {len(cats)}")
        # Filter first, so we only ask for episodes of shows we keep
        wanted_shows = [sh for sh in shows
                        if (not cats or str(sh.get("category_id")) in good)
                        and not adult(cats.get(str(sh.get("category_id")), ""))]
        print(f"Series: fetching episodes for {len(wanted_shows)} of {len(shows)} shows "
              f"({a.workers} at a time)")

        def fetch(show):
            for attempt in range(2):  # one retry on a hiccup
                try:
                    return api(base, u, pw, "get_series_info", series_id=show["series_id"])
                except Exception:
                    pass
            return None

        count = failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
            for i, (show, detail) in enumerate(zip(wanted_shows, pool.map(fetch, wanted_shows)), 1):
                if i % 25 == 0 or i == len(wanted_shows):
                    print(f"\rSeries {i}/{len(wanted_shows)}", end="", flush=True)
                if not isinstance(detail, dict):
                    failed += 1
                    continue
                cat_name = cats.get(str(show.get("category_id")), "Other")
                group = series_tag + tidy_group(cat_name)
                out = adult_lines if is_adult(cat_name) else series_lines
                eps = detail.get("episodes") or {}
                if isinstance(eps, list):  # some panels return a list instead of a dict
                    eps = {str(n): e for n, e in enumerate(eps)}
                for season, items in eps.items():
                    for e in items or []:
                        try:
                            title = (f"{show.get('name')} S{int(e.get('season', season) or 0):02d}"
                                     f"E{int(e.get('episode_num') or 0):02d}")
                        except (TypeError, ValueError):
                            title = f"{show.get('name')} {e.get('title') or ''}".strip()
                        ext = e.get("container_extension") or "mp4"
                        out.append(extinf(title, group, show.get("cover")))
                        out.append(f"{base}/series/{U}/{P}/{e['id']}.{ext}")
                        count += 1
        print(f"\nSeries episodes: {count}" + (f" ({failed} shows failed to load)" if failed else ""))

    limit = int(a.max_mb * 1024 * 1024)

    def save_m3u(path, blocks):
        """Write blocks (lists of lines kept together) to path, splitting into
        path_part2, path_part3... so no file is bigger than --max-mb."""
        root, ext = os.path.splitext(path)
        parts, cur, size = [], [], 0
        for b in blocks:
            bsize = sum(len(x.encode("utf-8")) + 1 for x in b)
            if cur and size + bsize > limit:
                parts.append(cur)
                cur, size = [], 0
            cur.append(b)
            size += bsize
        parts.append(cur)
        for n, part in enumerate(parts, 1):
            name = path if n == 1 else f"{root}_part{n}{ext}"
            entries = sum(len(b) for b in part) // 2
            with open(name, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for b in part:
                    f.write("\n".join(b) + "\n")
            print(f"Saved {name} ({entries} entries, {os.path.getsize(name) / 1048576:.1f} MB)")

    def pairs(items):  # ["#EXTM3U", info, url, info, url...] -> [[info, url], ...]
        return [items[i:i + 2] for i in range(1, len(items), 2)]

    stem, ext = os.path.splitext(a.output)
    ext = ext or ".m3u"
    save_m3u(a.output, pairs(lines))
    if not a.single:
        if a.movies:
            save_m3u(f"{stem}_movies{ext}", pairs(movie_lines))
        if a.series:
            save_m3u(f"{stem}_series{ext}", pairs(series_lines))
        if a.keep_adult:
            save_m3u(f"{stem}_adult{ext}", pairs(adult_lines))

    # TV guide
    if not a.no_epg:
        print("Downloading TV guide...")
        try:
            chans, progs = save_epg(base, u, pw, epg_ids, a.epg_file, game_chans)
            print(f"Saved {a.epg_file} ({chans} channels, {progs} programmes)")
        except Exception as e:
            print(f"Couldn't save the TV guide: {e}")
            if os.path.exists(a.epg_file + ".tmp"):
                os.remove(a.epg_file + ".tmp")
    print(f"EPG guide URL: {base}/xmltv.php?username={U}&password={P}")


if __name__ == "__main__":
    main()
