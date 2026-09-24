from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
HTML_FILE = ROOT / "index.html"
REPORT_FILE = ROOT / "data" / "automation_report.json"

UA = "aespa-database-updater/2.3 (https://github.com/avagor1/aespa-database)"

# Music releases older than this many days are NOT auto-added (the robot is meant
# to catch new releases, not to backfill the whole discography). Override with
# `--music-max-age-days N` (0 = no limit, useful once to backfill).
MUSIC_MAX_AGE_DAYS = 120

SOURCES = {
    "jp_news": "https://aespa-official.jp/news/",
    "jp_discography": "https://aespa-official.jp/discography/",
    "jp_schedule": "https://aespa-official.jp/schedule/",
    "weverse": "https://weverse.io/aespa/notice",
    "weverse_shop": "https://shop.weverse.io/en/shop/MXN/artists/133/notices",
    "youtube_feed": "https://www.youtube.com/feeds/videos.xml?channel_id=UC9GtSLeksfK4yuJ_g1lgQbg",
    "awards_wiki_raw": "https://en.wikipedia.org/w/index.php?title=List_of_awards_and_nominations_received_by_Aespa&action=raw",
}


def fetch(
    url: str,
    accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    attempts: int = 3,
    timeout: int = 60,
) -> str:
    """Fetch a source directly with retries and conservative rate-limit handling."""
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        req = Request(
            url,
            headers={
                "User-Agent": UA,
                "Accept": accept,
                "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After")
                try:
                    delay = max(5, min(int(retry_after), 60)) if retry_after else 10 * attempt
                except (TypeError, ValueError):
                    delay = 10 * attempt
                if attempt < attempts:
                    import time
                    print(f"429 for {url}; retrying in {delay}s (attempt {attempt}/{attempts})")
                    time.sleep(delay)
                    continue
            if exc.code in {500, 502, 503, 504} and attempt < attempts:
                import time
                delay = 3 * attempt
                print(f"HTTP {exc.code} for {url}; retrying in {delay}s (attempt {attempt}/{attempts})")
                time.sleep(delay)
                continue
            break
        except (TimeoutError, URLError) as exc:
            last_error = exc
            if attempt < attempts:
                import time
                delay = 3 * attempt
                print(f"Network error for {url}; retrying in {delay}s (attempt {attempt}/{attempts})")
                time.sleep(delay)
                continue
            break

    raise RuntimeError(f"Fetch failed after {attempts} attempts: {url}: {last_error}") from last_error


def fetch_via_jina(url: str, timeout: int = 45) -> str:
    """Fallback for sources that are reachable from Jina Reader but not CI directly.

    Jina Reader fetches the requested URL server-side and returns clean content.
    The free/basic endpoint does not require an API key; its documented basic
    rate limit is much higher than this script's three fallback requests/day.
    """
    reader_url = "https://r.jina.ai/" + url
    req = Request(
        reader_url,
        headers={
            "User-Agent": UA,
            "Accept": "text/plain, text/markdown, application/json;q=0.9, */*;q=0.8",
            "X-Engine": "direct",
            "X-Timeout": "45",
        },
    )
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def fetch_source(key: str, url: str) -> tuple[str, str]:
    """Fetch a configured source and fall back through Jina for aespa Japan pages."""
    if key.startswith("jp_"):
        # These three pages repeatedly time out from GitHub runners. Use a short
        # direct attempt first, then a server-side Reader fallback so a daily run
        # does not spend several minutes on repeated dead connections.
        try:
            return fetch(url, attempts=1, timeout=20), "direct"
        except Exception as direct_error:
            try:
                return fetch_via_jina(url, timeout=50), "jina"
            except Exception as jina_error:
                raise RuntimeError(
                    f"Direct fetch failed: {direct_error}; Jina fallback failed: {jina_error}"
                ) from jina_error

    accept = "application/json,text/plain,*/*" if key.endswith("api") else "text/html,application/xml;q=0.9,*/*;q=0.8"
    return fetch(url, accept=accept, attempts=3, timeout=60), "direct"

def clean_text(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value).lower()).strip()


def title_key(value: str) -> str:
    """Aggressive normalisation used to decide whether a release/song is already on the site.

    "Life’s Too Short (English Ver.)", "Life's Too Short — English Version" and
    "life's too short" all give the same key, so the robot does not re-add them.
    """
    v = unicodedata.normalize("NFKC", unescape(value)).lower()
    v = v.replace("\u2019", "'").replace("\u2018", "'")
    v = re.sub(r"\s*[\(\[（].*?[\)\]）]", "", v)   # (feat. x), (English Ver.)
    v = re.sub(r"\s+[\u2014\u2013-]\s+.*$", "", v)  # " — feat. X", " — English Version"
    return re.sub(r"[^\w]+", "", v)


def js_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def replace_once(text: str, pattern: str, repl: str) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"Could not replace required block: {pattern}")
    return new


def find_script_block(html: str, marker: str) -> tuple[int, int, str]:
    start = html.find(marker)
    if start < 0:
        raise RuntimeError(f"Missing script marker: {marker}")
    end = html.find("</script>", start)
    if end < 0:
        raise RuntimeError(f"Malformed script block: {marker}")
    return start, end, html[start:end]


# ---------------------------------------------------------------------------
# NEWS
# ---------------------------------------------------------------------------

def parse_jp_news(content: str) -> list[dict]:
    """Parse aespa Japan news from either HTML or Jina Reader Markdown."""
    out, seen = [], set()

    # Normal HTML path.
    anchors = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', content, re.I | re.S):
        href = urljoin(SOURCES["jp_news"], m.group(1))
        title = clean_text(m.group(2))
        if title and href.startswith("https://aespa-official.jp/news/"):
            anchors.append((m.start(), href, title))

    if anchors:
        for d in re.finditer(r"20\d{2}\.\d{1,2}\.\d{1,2}", content):
            y, mo, da = map(int, d.group(0).split("."))
            date_ = f"{y:04d}-{mo:02d}-{da:02d}"
            candidates = [a for a in anchors if d.end() <= a[0] <= d.end() + 2200]
            candidates.sort(key=lambda x: x[0])
            chosen = next((a for a in candidates if len(a[2]) >= 6 and norm(a[2]) not in {"news", "next", "previous"}), None)
            if not chosen:
                continue
            key = norm(chosen[2])
            if key in seen:
                continue
            seen.add(key)
            out.append({"date": date_, "title": chosen[2], "url": chosen[1], "source": "aespa Japan Official", "source_key": "aj"})
    else:
        # Jina Reader commonly returns Markdown links like:
        # [Title](https://aespa-official.jp/news/slug/)
        links = []
        for m in re.finditer(r'\[([^\]]{4,200})\]\((https://aespa-official\.jp/news/[^)]+)\)', content, re.I):
            title = clean_text(m.group(1))
            if title:
                links.append((m.start(), m.group(2), title))
        for d in re.finditer(r"20\d{2}[./]\d{1,2}[./]\d{1,2}", content):
            raw_date = d.group(0).replace("/", ".")
            y, mo, da = map(int, raw_date.split("."))
            date_ = f"{y:04d}-{mo:02d}-{da:02d}"
            candidates = [a for a in links if d.end() <= a[0] <= d.end() + 1800]
            candidates.sort(key=lambda x: x[0])
            chosen = next((a for a in candidates if len(a[2]) >= 6 and norm(a[2]) not in {"news", "next", "previous"}), None)
            if not chosen:
                continue
            key = norm(chosen[2])
            if key in seen:
                continue
            seen.add(key)
            out.append({"date": date_, "title": chosen[2], "url": chosen[1], "source": "aespa Japan Official", "source_key": "aj"})

    out.sort(key=lambda x: x["date"], reverse=True)
    return out

def classify_news(title: str) -> str:
    t = norm(title)
    if any(x in t for x in ("tour", "concert", "live", "fan meeting", "festival", "ツアー", "ライブ", "公演", "ファンミーティング")):
        return "cc"
    if any(x in t for x in ("release", "album", "single", "comeback", "mv", "リリース", "アルバム", "シングル", "発売")):
        return "co"
    if any(x in t for x in ("ambassador", "brand", "collab", "コラボ", "アンバサダー", "ブランド")):
        return "cl"
    if any(x in t for x in ("karina", "winter", "giselle", "ningning", "カリナ", "ウィンター", "ジゼル", "ニンニン")):
        return "ma"
    return "ou"




def parse_js_source_map(raw: str) -> dict[str, list[str]]:
    """Parse the simple JS source-map object used by the site.

    The site stores it as JavaScript, e.g.:
      {ai:["label","url"],wk:["label","url"]}
    which is valid JS but is not valid JSON because the property names are
    unquoted.
    """
    body = raw.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    pair_re = re.compile(
        r'([A-Za-z_$][\w$]*)\s*:\s*\[\s*("(?:\\.|[^"\\])*")\s*,\s*("(?:\\.|[^"\\])*")\s*\]'
    )
    out: dict[str, list[str]] = {}
    for m in pair_re.finditer(body):
        try:
            label = json.loads(m.group(2))
            url = json.loads(m.group(3))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid source-map string: {m.group(0)}") from exc
        out[m.group(1)] = [label, url]
    if not out:
        raise RuntimeError("Could not parse the JavaScript source map.")
    return out


def update_news(html: str, items: list[dict]) -> tuple[str, int]:
    start, end, block = find_script_block(html, "/* news */")
    sr = re.search(r"var\s+SR\s*=\s*(\{.*?\});", block, re.S)
    dm = re.search(r"var\s+D\s*=\s*(\[.*?\]);", block, re.S)
    if not sr or not dm:
        raise RuntimeError("News data block not found")
    data = json.loads(dm.group(1))
    existing = {norm(x[2]) for x in data if isinstance(x, list) and len(x) >= 3}
    add = []
    for item in items:
        key = norm(item["title"])
        if key in existing:
            continue
        add.append([classify_news(item["title"]), item["date"], item["title"], "Official update from aespa Japan.", "aj"])
        existing.add(key)

    if not add:
        return html, 0

    sr_obj = parse_js_source_map(sr.group(1))
    sr_obj["aj"] = ["aespa Japan Official", SOURCES["jp_news"]]
    merged = (add + data)[:200]
    block = re.sub(r"var\s+SR\s*=\s*\{.*?\};", "var SR=" + js_json(sr_obj) + ";", block, count=1, flags=re.S)
    block = re.sub(r"var\s+D\s*=\s*\[.*?\];", "var D=" + js_json(merged) + ";", block, count=1, flags=re.S)
    return html[:start] + block + html[end:], len(add)


# ---------------------------------------------------------------------------
# YOUTUBE / MVS
# ---------------------------------------------------------------------------

def parse_youtube_feed(xml_text: str) -> list[dict]:
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", ns):
        title = e.findtext("a:title", "", ns).strip()
        vid = e.findtext("yt:videoId", "", ns).strip()
        pub = e.findtext("a:published", "", ns).strip()
        if not title or not vid:
            continue
        low = norm(title)
        likely = (" mv" in f" {low}" or "music video" in low or "mv" == low[-2:]) and not any(x in low for x in ("teaser", "behind", "making", "reaction", "performance", "dance practice", "live", "shorts"))
        out.append({
            "title": title,
            "video_id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "published": pub,
            "likely_mv": likely,
        })
    return out


def clean_mv_title(title: str) -> str:
    m = re.search(r"[\u2018\u2019'\"](.+?)[\u2018\u2019'\"]", title)
    if m:
        return m.group(1).strip()
    t = re.sub(r"(?i)^aespa\s*(?:에스파)?\s*[-–:]?\s*", "", title).strip()
    t = re.sub(r"(?i)\s*[-–:]?\s*(?:official\s*)?(?:music\s*video|mv)\s*$", "", t).strip()
    return t


def update_mvs(html: str, videos: list[dict]) -> tuple[str, int]:
    start, end, block = find_script_block(html, "/* MV archive */")
    m = re.search(r"var\s+MV\s*=\s*(\[.*?\]);", block, re.S)
    if not m:
        raise RuntimeError("MV array not found")
    data = json.loads(m.group(1))
    existing_urls = {x.get("url") for x in data if isinstance(x, dict)}
    existing_titles = {norm(x.get("title", "")) for x in data if isinstance(x, dict)}
    add = []
    for v in videos:
        if not v["likely_mv"] or v["url"] in existing_urls:
            continue
        clean = clean_mv_title(v["title"])
        if not clean or norm(clean) in existing_titles:
            continue
        try:
            year = int(v["published"][:4])
        except Exception:
            year = datetime.now().year
        era = str(year)
        add.append({
            "title": clean,
            "year": year,
            "era": era,
            "type": "Music Video",
            "member": "aespa",
            "query": f"aespa {clean} official MV",
            "url": v["url"],
        })
        existing_urls.add(v["url"])
        existing_titles.add(norm(clean))
    if not add:
        return html, 0
    merged = data + add
    block = re.sub(r"var\s+MV\s*=\s*\[.*?\];", "var MV=" + js_json(merged) + ";", block, count=1, flags=re.S)
    return html[:start] + block + html[end:], len(add)


# ---------------------------------------------------------------------------
# MUSIC / OFFICIAL DISCOGRAPHY
# ---------------------------------------------------------------------------
#
# Layout of an aespa-official.jp release page (after tags are stripped):
#
#   Digital Single            <- release type
#   Dreams Come True          <- title (also the <title> of the page)
#   [cover image]             <- .../wp-content/uploads/....webp
#   2021.12.20                <- release date
#   1.Dreams Come True        <- tracklist ("1.", "01." or "・" prefixes)
#   BACK                      <- end of the useful content
#   Copyright © SM ...        <- footer (must never end up in a title!)
#
# The header of every page also contains the LINE account icon
# (/themes/kissntell/img/line.png). It is NOT a cover: covers are only taken
# from /wp-content/uploads/.

JP_CHARS = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
BOILERPLATE = re.compile(r"copyright|\u00a9|all rights reserved|https?://", re.I)
DATE_RE = re.compile(r"(20\d{2})\s*[./\uff0f-]\s*(\d{1,2})\s*[./\uff0f-]\s*(\d{1,2})")
TRACK_RE = re.compile(r"^\s*(?:0?\d{1,2}\s*[.\uff0e)]\s*|[\u30fb\u2022\u00b7*-]\s*)(?P<t>\S.*?)\s*$")


def clean_line(value: str) -> str:
    """One line of page text -> plain text (handles entities, Markdown links/escapes)."""
    value = unescape(value)
    value = re.sub(r"\\([\\`*_{}\[\]()#+.!-])", r"\1", value)
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value.strip("*_` ").strip()


def page_lines(content: str, url: str) -> tuple[list[str], list[str], str]:
    """Turn an HTML page or a Jina Markdown page into (text lines, image urls, page title).

    Every tag boundary becomes a line break, so the date, the tracklist and the
    footer can never be glued together into one giant string.
    """
    looks_html = bool(re.search(r"<\s*(?:html|body|div|h[1-6]|p|ul|li)\b", content, re.I))
    if looks_html:
        images: list[str] = []
        for tag in re.findall(r"<img\b[^>]*>", content, re.I | re.S):
            for src in re.findall(r"""(?:data-src|data-lazy-src|data-original|src)\s*=\s*["']([^"']+)["']""", tag, re.I):
                images.append(urljoin(url, unescape(src)))
        tm = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
        page_title = clean_line(re.sub(r"<[^>]+>", "", tm.group(1))) if tm else ""
        body = re.sub(r"<(script|style|noscript|head)\b[\s\S]*?</\1\s*>", "\n", content, flags=re.I)
        body = re.sub(r"<[^>]+>", "\n", body)
        raw_lines = body.splitlines()
    else:
        images = [urljoin(url, u) for u in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", content)]
        tm = re.search(r"^\s*title:\s*(.+)$", content, re.I | re.M)
        page_title = clean_line(tm.group(1)) if tm else ""
        body = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", content, flags=re.S)
        raw_lines = body.splitlines()
    lines = [clean_line(x) for x in raw_lines]
    return [x for x in lines if x], images, page_title


def clean_track(value: str) -> str:
    """Clean one tracklist entry: no Japanese TV/drama notes, no pipes, no odd apostrophes."""
    t = value.replace("\u2019", "'").replace("|", " \u2014 ")
    while True:
        new = re.sub(
            r"\s*[（(][^（()）]*[）)]\s*$",
            lambda m: "" if JP_CHARS.search(m.group(0)) else m.group(0),
            t,
        )
        if new == t:
            break
        t = new
    t = re.sub(r"\s+", " ", t).strip()
    return t if 0 < len(t) <= 90 else ""


def normalize_kind(kind: str) -> str:
    """'Japan 1st Mini Album' -> '1st Japanese mini album' (the site's own wording)."""
    k = kind.strip()
    m = re.match(r"(?i)^japan(?:ese)?\s+(\d+(?:st|nd|rd|th))\s+(.*)$", k)
    if m:
        return f"{m.group(1)} Japanese {m.group(2).lower()}"
    return k


def _heading_fallback(content: str) -> str:
    m = re.search(r"(?m)^###\s+(.+)$", content)
    if m:
        return clean_line(m.group(1))
    m = re.search(r"<h3[^>]*>(.*?)</h3>", content, re.I | re.S)
    if m:
        return clean_line(re.sub(r"<[^>]+>", "", m.group(1)))
    return ""


def parse_discography_index(content: str) -> list[dict]:
    """Return [{url, text, section}] for every release link on the discography page.

    `section` is the heading above the link (ALBUM / SINGLE / Blu-ray / DVD).
    """
    events: list[tuple[int, str, object]] = []
    for m in re.finditer(r"(?m)^#{3,5}\s*(.+?)\s*$", content):
        events.append((m.start(), "h", clean_line(m.group(1))))
    for m in re.finditer(r"<h[3-6][^>]*>(.*?)</h[3-6]>", content, re.I | re.S):
        events.append((m.start(), "h", clean_text(m.group(1))))
    for m in re.finditer(r"\[([^\]\n]{1,200})\]\((https://aespa-official\.jp/discography/[^)\s]+)\)", content):
        events.append((m.start(), "l", (clean_line(m.group(1)), m.group(2))))
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', content, re.I | re.S):
        events.append((m.start(), "l", (clean_text(m.group(2)), urljoin(SOURCES["jp_discography"], m.group(1)))))
    events.sort(key=lambda e: e[0])

    out, seen = [], set()
    section = ""
    for _pos, kind, payload in events:
        if kind == "h":
            section = str(payload)
            continue
        text, href = payload  # type: ignore[misc]
        if not re.match(r"^https://aespa-official\.jp/discography/[^/?#]+/?$", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({"url": href, "text": text, "section": section})
    return out


def parse_discography_detail(content: str, url: str, section: str = "") -> dict | None:
    lines, images, page_title = page_lines(content, url)

    # --- title: the page <title> ("Dreams Come True | aespa ..."), never a text blob
    title = re.split(r"\s*[|\uff5c]\s*", page_title)[0].strip() if page_title else ""
    if not title or title.lower() == "discography":
        title = _heading_fallback(content)
    if not title:
        title = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()
    title = title.replace("\u2019", "'").strip()

    # --- release date: first short line that looks like a date
    date_idx, date_iso = None, None
    for i, ln in enumerate(lines):
        if len(ln) > 40:
            continue
        m = DATE_RE.search(ln)
        if not m:
            continue
        try:
            date_iso = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except ValueError:
            continue
        date_idx = i
        break
    if date_idx is None or not date_iso:
        return None

    # --- release type: closest short "…Single"/"…Album" line above the date
    kind = ""
    for ln in reversed(lines[max(0, date_idx - 6):date_idx]):
        low = ln.lower()
        if (
            len(ln) <= 40
            and re.search(r"single|album|\bep\b", low)
            and "discography" not in low
            and title_key(ln) != title_key(title)
        ):
            kind = ln
            break
    if not kind:
        kind = "Single" if "single" in section.lower() else "Album"
    kind = normalize_kind(kind)

    # --- tracklist: consecutive numbered/bulleted lines right after the date
    tracks: list[str] = []
    for ln in lines[date_idx + 1:]:
        low = ln.lower()
        if low.startswith(("back", "copyright")) or ln.startswith("\u00a9"):
            break
        m = TRACK_RE.match(ln)
        if m:
            t = clean_track(m.group("t"))
            if t:
                tracks.append(t)
        elif tracks:
            break
    tracks = list(dict.fromkeys(tracks))

    # --- cover: only real uploads, never the header icons (LINE, X, ...)
    artwork = ""
    for src in images:
        path = src.split("?")[0].lower()
        if "/wp-content/uploads/" in path and path.endswith((".webp", ".jpg", ".jpeg", ".png")):
            artwork = src
            break

    return {
        "title": title,
        "year": int(date_iso[:4]),
        "date": date_iso,
        "kind": kind,
        "artwork": artwork,
        "tracks": tracks,
        "url": url,
        "section": section,
    }


def release_problem(rel: dict) -> str | None:
    """Sanity checks. Returns why a parsed release must NOT be added, or None if it is fine."""
    title = rel.get("title", "")
    if not title or len(title) > 80:
        return "title empty or too long"
    if BOILERPLATE.search(title) or "\n" in title:
        return "title contains page boilerplate (copyright, link...)"
    if title_key(title) in {"", "discography"}:
        return "title is not a release name"
    if not rel.get("date"):
        return "no release date"
    tracks = rel.get("tracks") or []
    if not tracks:
        return "no tracklist found"
    if len(tracks) > 40:
        return "implausible number of tracks"
    if any(BOILERPLATE.search(t) or len(t) > 90 for t in tracks):
        return "a track contains page boilerplate"
    return None


def known_music_keys(html: str) -> set[str]:
    """Normalised keys of every title already present in the site's Music data.

    Covers releases, tracks, English versions, OST / collab lists, etc.: any quoted
    string of the Music script (from `var S=[];` to the end of that <script>).
    """
    start = html.find("var S=[];")
    if start < 0:
        start = 0
    end = html.find("</script>", start)
    region = html[start:end if end > 0 else len(html)]
    keys: set[str] = set()
    for m in re.finditer(r'"((?:\\.|[^"\\])*)"', region):
        raw = m.group(1)
        try:
            s = json.loads('"' + raw + '"')
        except json.JSONDecodeError:
            s = raw
        k = title_key(s.split("|")[0])
        if len(k) >= 2:
            keys.add(k)
    return keys


def fetch_jp_detail(url: str) -> tuple[str, str]:
    """Fetch an aespa Japan detail page directly, then through Jina if needed."""
    try:
        return fetch(url, attempts=1, timeout=20), "direct"
    except Exception as direct_error:
        try:
            return fetch_via_jina(url, timeout=50), "jina"
        except Exception:
            raise direct_error


def discover_release_details(index_content: str, known: set[str]) -> tuple[list[dict], list[dict]]:
    """Fetch the detail page of every release that is not already on the site.

    Releases whose link text is already known are skipped without any request,
    which also keeps the daily run short.
    """
    releases: list[dict] = []
    errors: list[dict] = []
    for entry in parse_discography_index(index_content):
        if re.search(r"blu|dvd", entry["section"], re.I):
            continue  # video releases have no tracklist for the Music section
        if entry["text"] and title_key(entry["text"]) in known:
            continue
        try:
            page, _mode = fetch_jp_detail(entry["url"])
            rel = parse_discography_detail(page, entry["url"], entry["section"])
        except Exception as exc:
            errors.append({"source": entry["url"], "error": str(exc)})
            continue
        if rel:
            releases.append(rel)
        else:
            errors.append({"source": entry["url"], "error": "could not parse release page"})
    return releases, errors


def select_new_music(
    html: str,
    releases: list[dict],
    max_age_days: int = MUSIC_MAX_AGE_DAYS,
    today: date | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split parsed releases into (to add, skipped-with-reason)."""
    today = today or datetime.now(timezone.utc).date()
    known = known_music_keys(html)
    new: list[dict] = []
    skipped: list[dict] = []
    for rel in releases:
        title = str(rel.get("title", ""))
        problem = release_problem(rel)
        if problem:
            skipped.append({"title": title[:80], "reason": problem})
            continue
        tkey = title_key(title)
        track_keys = [title_key(t) for t in rel["tracks"]]
        if tkey in known or all(k in known for k in track_keys if k):
            skipped.append({"title": title, "reason": "already on the site"})
            continue
        if max_age_days:
            age = (today - date.fromisoformat(rel["date"])).days
            if age > max_age_days:
                skipped.append({"title": title, "reason": f"released {age} days ago (limit {max_age_days}); use --music-max-age-days 0 to backfill"})
                continue
        new.append(rel)
        known.add(tkey)
        known.update(k for k in track_keys if k)
    return new, skipped


def itunes_duration(track: str) -> str | None:
    url = "https://itunes.apple.com/search?term=" + quote(f"aespa {track}") + "&entity=song&limit=8&country=US"
    try:
        payload = json.loads(fetch(url, accept="application/json,text/plain,*/*", attempts=2, timeout=20))
    except Exception:
        return None
    best = None
    for item in payload.get("results", []):
        if str(item.get("artistName", "")).lower() != "aespa":
            continue
        name = norm(str(item.get("trackName", "")))
        if norm(track) == name or norm(track).replace(" — ", " ") in name or name in norm(track):
            best = item
            break
    ms = (best or {}).get("trackTimeMillis") if best else None
    if not ms:
        return None
    sec = round(ms / 1000)
    return f"{sec // 60}:{sec % 60:02d}"


def update_music(html: str, releases: list[dict]) -> tuple[str, int]:
    """Insert already-validated, already-deduplicated releases (see select_new_music)."""
    if not releases:
        return html, 0
    # Script 1 contains the Music data. Insert new A(...) lines before 'var X=['.
    marker = "var X=[\"aespa\"];"
    pos = html.find(marker)
    if pos < 0:
        raise RuntimeError("Music insertion point not found")

    art_entries = []
    dur_entries = []
    additions = []

    for rel in releases:
        title = rel["title"]
        kind = rel["kind"]
        lang = "Japanese" if "japan" in kind.lower() else "Korean"
        arr = []
        for track in rel["tracks"]:
            # The site's A() parser uses | as note separator, so pipes are removed upstream.
            arr.append(track)
            dur = itunes_duration(track)
            if dur:
                dur_entries.append((track, dur))
        additions.append(
            f'A({json.dumps(title, ensure_ascii=False)},{rel["year"]},'
            f'{json.dumps(kind, ensure_ascii=False)},{json.dumps(lang)},'
            f'{json.dumps(arr, ensure_ascii=False)});'
        )
        if rel.get("artwork"):
            art_entries.append((title, rel["artwork"]))

    html = html[:pos] + "\n".join(additions) + "\n" + html[pos:]

    # Update RELEASE_ART object.
    m = re.search(r"var RELEASE_ART=\{(.*?)\};", html, re.S)
    if art_entries and m:
        body = m.group(1).rstrip()
        for rel_name, art in art_entries:
            if re.search(r'"' + re.escape(rel_name) + r'"\s*:', body):
                body = re.sub(r'"' + re.escape(rel_name) + r'"\s*:\s*"[^"]*"', lambda _m: json.dumps(rel_name, ensure_ascii=False) + ":" + json.dumps(art, ensure_ascii=False), body)
            else:
                if body and not body.rstrip().endswith(","):
                    body += ","
                body += "\n " + json.dumps(rel_name, ensure_ascii=False) + ":" + json.dumps(art, ensure_ascii=False)
        html = html[:m.start(1)] + body + html[m.end(1):]

    # Update STATIC_DURATIONS object.
    m = re.search(r"var STATIC_DURATIONS=\{(.*?)\};", html, re.S)
    if dur_entries and m:
        body = m.group(1).rstrip()
        for track, dur in dur_entries:
            if re.search(r'"' + re.escape(track) + r'"\s*:', body):
                continue  # never overwrite a duration that is already on the site
            if body and not body.rstrip().endswith(","):
                body += ","
            body += "\n" + json.dumps(track, ensure_ascii=False) + ":" + json.dumps(dur)
        html = html[:m.start(1)] + body + html[m.end(1):]

    return html, len(additions)


# ---------------------------------------------------------------------------
# CALENDAR
# ---------------------------------------------------------------------------

def parse_weverse_tour(html: str) -> list[dict]:
    text = clean_text(html)
    out = []
    # Matches patterns such as: MANCHESTER January 14, 2027 / MANCHESTER 14 January 2027
    pats = [
        re.compile(r"(?:📍\s*)?([A-Za-zÀ-ÿ .,'’\-&]+)\s+(?:📅\s*)?([A-Z][a-z]+\s+\d{1,2},\s*20\d{2})"),
        re.compile(r"(?:📍\s*)?([A-Za-zÀ-ÿ .,'’\-&]+)\s+(?:📅\s*)?(\d{1,2}\s+[A-Z][a-z]+\s+20\d{2})"),
    ]
    seen = set()
    for pat in pats:
        for m in pat.finditer(text):
            city = re.sub(r"\s+", " ", m.group(1)).strip(" -")
            datestr = m.group(2).strip()
            try:
                for fmt in ("%B %d, %Y", "%d %B %Y"):
                    try:
                        dt = datetime.strptime(datestr, fmt)
                        break
                    except ValueError:
                        dt = None
                if not dt:
                    continue
                date_ = dt.strftime("%Y-%m-%d")
            except Exception:
                continue
            key = (date_, norm(city))
            if key in seen or len(city) < 3:
                continue
            seen.add(key)
            out.append({"date": date_, "title": f"SYNK : COMPLæXITY — {city}", "desc": "Official tour date from Weverse.", "source_key": "wv"})
    return out


def parse_jp_schedule(content: str) -> list[dict]:
    """Parse clearly dated items from aespa Japan Schedule, HTML or Markdown."""
    text = clean_text(content)
    out: list[dict] = []
    seen = set()

    # Dates in the schedule are often written as 2026.09.24 or 2026/09/24.
    date_matches = list(re.finditer(r"20\d{2}[./]\d{1,2}[./]\d{1,2}", text))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in content.splitlines() if ln.strip()]
    for m in date_matches:
        raw = m.group(0).replace("/", ".")
        y, mo, da = map(int, raw.split("."))
        date_ = f"{y:04d}-{mo:02d}-{da:02d}"
        window = text[max(0, m.start()-180):min(len(text), m.end()+260)]
        # Pick a concise title from nearby text, removing the raw date.
        title = re.sub(r"20\d{2}[./]\d{1,2}[./]\d{1,2}", "", window)
        title = re.sub(r"\s+", " ", title).strip(" -–—:·")
        if not title:
            continue
        # Avoid navigation/category-only snippets.
        if len(title) < 4 or norm(title) in {"schedule", "news", "aespa"}:
            continue
        # Cap to a usable title.
        title = title[:160]
        key = (date_, norm(title))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": date_,
            "title": title,
            "desc": "Official schedule entry from aespa Japan.",
            "source_key": "aj",
        })

    return out[:120]

def update_calendar(html: str, tour_items: list[dict]) -> tuple[str, int]:
    start, end, block = find_script_block(html, "/* calendar */")
    existing_calls = re.findall(r'A\("([^"]*)","([^"]*)","([^"]*)","([^"]*)","([^"]*)"\)', block)
    existing = {(d, norm(t)) for d, _c, t, _x, _s in existing_calls}
    add = []
    for x in tour_items:
        key = (x["date"], norm(x["title"]))
        if key in existing:
            continue
        add.append(f'A({json.dumps(x["date"])},"cc",{json.dumps(x["title"], ensure_ascii=False)},{json.dumps(x["desc"], ensure_ascii=False)},"wv");')
        existing.add(key)
    if not add:
        return html, 0
    pos = block.find("function gen(y)")
    if pos < 0:
        raise RuntimeError("Calendar insertion point not found")
    block = block[:pos] + "\n" + "\n".join(add) + "\n" + block[pos:]
    return html[:start] + block + html[end:], len(add)


# ---------------------------------------------------------------------------
# FASHION
# ---------------------------------------------------------------------------

def fashion_candidate(title: str) -> bool:
    t = norm(title)
    return any(x in t for x in (
        "ambassador", "brand", "magazine", "cover", "fashion week", "met gala", "prada", "gucci", "loewe", "chanel", "ralph lauren", "versace", "givenchy", "senka", "마크 & 로나", "アンバサダー", "雑誌", "表紙", "ブランド"
    ))


def fashion_category(title: str) -> str:
    t = norm(title)
    if "ambassador" in t or "アンバサダー" in t:
        return "am"
    if "cover" in t or "magazine" in t or "表紙" in t or "雑誌" in t:
        return "mg"
    if "met gala" in t:
        return "rc"
    if "fashion week" in t or any(x in t for x in ("prada", "gucci", "loewe", "versace")):
        return "fw"
    return "br"


def fashion_member(title: str) -> str:
    t = norm(title)
    for name in ("karina", "giselle", "winter", "ningning"):
        if name in t:
            return name
    if any(x in t for x in ("aespa", "メンバー")):
        return "group"
    return "group"


def update_fashion(html: str, news: list[dict]) -> tuple[str, int]:
    start, end, block = find_script_block(html, "/* fashion */")
    sm = re.search(r"var\s+SR\s*=\s*(\{.*?\});", block, re.S)
    dm = re.search(r"var\s+D\s*=\s*(\[.*?\]);", block, re.S)
    if not sm or not dm:
        raise RuntimeError("Fashion data block not found")
    sr = parse_js_source_map(sm.group(1))
    data = json.loads(dm.group(1))
    existing = {norm(x[2]) for x in data if isinstance(x, list) and len(x) >= 3}
    add = []
    for n in news:
        if not fashion_candidate(n["title"]):
            continue
        key = norm(n["title"])
        if key in existing:
            continue
        add.append([fashion_category(n["title"]), n["date"], n["title"], "Official update from aespa Japan.", "aj", fashion_member(n["title"])])
        existing.add(key)
    if not add:
        return html, 0
    sr["aj"] = ["aespa Japan Official", SOURCES["jp_news"]]
    merged = (add + data)[:140]
    block = re.sub(r"var\s+SR\s*=\s*\{.*?\};", "var SR=" + js_json(sr) + ";", block, count=1, flags=re.S)
    block = re.sub(r"var\s+D\s*=\s*\[.*?\];", "var D=" + js_json(merged) + ";", block, count=1, flags=re.S)
    return html[:start] + block + html[end:], len(add)


# ---------------------------------------------------------------------------
# AWARDS
# ---------------------------------------------------------------------------

def parse_awards_wikitext(payload: str) -> list[dict]:
    data = json.loads(payload)
    wikitext = data["parse"]["wikitext"]["*"]
    out = []
    current_year = None
    current_award = ""
    for line in wikitext.splitlines():
        m = re.search(r"\|\s*(20\d{2})\s*\|", line)
        if m:
            current_year = m.group(1)
        elif re.match(r"^!|^\|-", line):
            pass
        if "|" not in line or not current_year:
            continue
        cols = [c.strip() for c in line.strip().strip("|").split("||")]
        joined = " | ".join(cols)
        low = norm(joined)
        if current_year != "2026":
            continue
        if any(x in low for x in ("won", "nominated", "pending")):
            out.append({
                "year": "2026",
                "text": re.sub(r"\{\{[^}]+\}\}", "", joined).strip(),
            })
    # Deduplicate + require substantive content.
    seen = set()
    cleaned = []
    for x in out:
        k = norm(x["text"])
        if len(k) < 12 or k in seen:
            continue
        seen.add(k)
        cleaned.append(x)
    return cleaned[:40]


def update_awards(html: str, awards: list[dict]) -> tuple[str, int]:
    if not awards:
        return html, 0
    start, end, block = find_script_block(html, "/* achievements */")
    sm = re.search(r"var\s+SR\s*=\s*(\{.*?\});", block, re.S)
    dm = re.search(r"var\s+D\s*=\s*(\[.*?\]);", block, re.S)
    if not sm or not dm:
        raise RuntimeError("Achievements data block not found")
    sr = parse_js_source_map(sm.group(1))
    data = json.loads(dm.group(1))
    existing = {norm(x[2]) for x in data if isinstance(x, list) and len(x) >= 3}
    add = []
    for a in awards:
        title = f"2026 award update: {a['text']}"
        if norm(title) in existing:
            continue
        add.append(["aw", "2026", title, "Parsed from the current awards reference; verify against the award organizer before treating it as final.", "wa"])
        existing.add(norm(title))
    if not add:
        return html, 0
    merged = (add + data)[:100]
    block = re.sub(r"var\s+SR\s*=\s*\{.*?\};", "var SR=" + js_json(sr) + ";", block, count=1, flags=re.S)
    block = re.sub(r"var\s+D\s*=\s*\[.*?\];", "var D=" + js_json(merged) + ";", block, count=1, flags=re.S)
    return html[:start] + block + html[end:], len(add)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only report candidates; do not modify index.html")
    parser.add_argument(
        "--music-max-age-days",
        type=int,
        default=MUSIC_MAX_AGE_DAYS,
        help="Ignore music releases older than N days (default %(default)s; 0 = no limit, to backfill once)",
    )
    args = parser.parse_args()

    if not HTML_FILE.exists():
        raise SystemExit("index.html not found at repository root")

    html = HTML_FILE.read_text(encoding="utf-8")
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sources_ok": 0,
        "sources_total": len(SOURCES),
        "source_status": {},
        "errors": [],
        "candidates": {},
        "skipped": {},
        "added_titles": {},
        "applied": {},
    }

    raw = {}
    for key, url in SOURCES.items():
        try:
            raw[key], mode = fetch_source(key, url)
            report["sources_ok"] += 1
            report["source_status"][key] = "ok" if mode == "direct" else "ok_via_jina"
        except Exception as exc:
            report["source_status"][key] = "error"
            report["errors"].append({"source": key, "error": str(exc)})

    # News
    jp_news = parse_jp_news(raw["jp_news"]) if "jp_news" in raw else []
    report["candidates"]["news"] = len(jp_news)
    if not args.dry_run and jp_news:
        html, n = update_news(html, jp_news)
    else:
        n = 0
    report["applied"]["news_added"] = n

    # MVs
    yt = parse_youtube_feed(raw["youtube_feed"]) if "youtube_feed" in raw else []
    mv_candidates = [x for x in yt if x["likely_mv"]]
    report["candidates"]["mvs"] = len(mv_candidates)
    if not args.dry_run and mv_candidates:
        html, n = update_mvs(html, mv_candidates)
    else:
        n = 0
    report["applied"]["mvs_added"] = n

    # Music: only releases that are really new, validated, with a clean title.
    new_music: list[dict] = []
    if "jp_discography" in raw:
        known = known_music_keys(html)
        releases, disc_errors = discover_release_details(raw["jp_discography"], known)
        report["errors"].extend(disc_errors)
        new_music, skipped_music = select_new_music(html, releases, args.music_max_age_days)
        report["skipped"]["music"] = skipped_music
    report["candidates"]["music_releases"] = len(new_music)
    if not args.dry_run and new_music:
        html, n = update_music(html, new_music)
        report["added_titles"]["music"] = [r["title"] for r in new_music]
    else:
        n = 0
    report["applied"]["music_releases_added"] = n

    # Calendar: combine Weverse tour notices with clearly dated official Japan schedule entries.
    tour = parse_weverse_tour(raw["weverse"]) if "weverse" in raw else []
    jp_schedule = parse_jp_schedule(raw["jp_schedule"]) if "jp_schedule" in raw else []
    calendar_candidates = tour + [x for x in jp_schedule if x.get("title")]
    report["candidates"]["calendar"] = len(calendar_candidates)
    if not args.dry_run and calendar_candidates:
        html, n = update_calendar(html, calendar_candidates)
    else:
        n = 0
    report["applied"]["calendar_events_added"] = n

    # Fashion from clearly fashion-related official Japan news.
    fashion = [x for x in jp_news if fashion_candidate(x["title"])]
    report["candidates"]["fashion"] = len(fashion)
    if not args.dry_run and fashion:
        html, n = update_fashion(html, fashion)
    else:
        n = 0
    report["applied"]["fashion_entries_added"] = n

    # Awards: use the Wikimedia raw wikitext endpoint as a structured fallback.
    # This avoids the Action API endpoint that was returning 429 on shared CI runners.
    awards = []
    if "awards_wiki_raw" in raw:
        try:
            awards = parse_awards_wikitext(json.dumps({"parse": {"wikitext": {"*": raw["awards_wiki_raw"]}}}))
        except Exception as exc:
            report["errors"].append({"source": "awards_wiki_raw_parse", "error": str(exc)})
    report["candidates"]["awards"] = len(awards)
    if not args.dry_run and awards:
        html, n = update_awards(html, awards)
    else:
        n = 0
    report["applied"]["awards_entries_added"] = n

    report["applied"]["index_updated"] = bool(any(report["applied"].values())) and not args.dry_run

    if not args.dry_run and report["applied"]["index_updated"]:
        HTML_FILE.write_text(html, encoding="utf-8")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "sources_ok": report["sources_ok"],
        "sources_total": report["sources_total"],
        "candidates": sum(report["candidates"].values()),
        "applied": report["applied"],
        "errors": len(report["errors"]),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
