from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
HTML_FILE = ROOT / "index.html"
REPORT_FILE = ROOT / "data" / "automation_report.json"

UA = "Mozilla/5.0 (compatible; aespa-database-updater/2.0; +https://github.com/)"

SOURCES = {
    "jp_news": "https://aespa-official.jp/news/",
    "jp_discography": "https://aespa-official.jp/discography/",
    "jp_schedule": "https://aespa-official.jp/schedule/",
    "weverse": "https://weverse.io/aespa/notice",
    "weverse_shop": "https://shop.weverse.io/en/shop/MXN/artists/133/notices",
    "youtube_feed": "https://www.youtube.com/feeds/videos.xml?channel_id=UC9GtSLeksfK4yuJ_g1lgQbg",
    "awards_wiki_api": "https://en.wikipedia.org/w/api.php?action=parse&page=List_of_awards_and_nominations_received_by_Aespa&prop=wikitext&format=json",
}


def fetch(url: str, accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8") -> str:
    req = Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with urlopen(req, timeout=40) as r:
            return r.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"Fetch failed: {url}: {exc}") from exc


def clean_text(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def norm(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value).lower()).strip()


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

def parse_jp_news(html: str) -> list[dict]:
    anchors = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = urljoin(SOURCES["jp_news"], m.group(1))
        title = clean_text(m.group(2))
        if title and href.startswith("https://aespa-official.jp/news/"):
            anchors.append((m.start(), href, title))

    out, seen = [], set()
    for d in re.finditer(r"20\d{2}\.\d{1,2}\.\d{1,2}", html):
        y, mo, da = map(int, d.group(0).split("."))
        date = f"{y:04d}-{mo:02d}-{da:02d}"
        candidates = [a for a in anchors if d.end() <= a[0] <= d.end() + 2200]
        candidates.sort(key=lambda x: x[0])
        chosen = next((a for a in candidates if len(a[2]) >= 6 and norm(a[2]) not in {"news", "next", "previous"}), None)
        if not chosen:
            continue
        key = norm(chosen[2])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "date": date,
            "title": chosen[2],
            "url": chosen[1],
            "source": "aespa Japan Official",
            "source_key": "aj",
        })
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

    sr_obj = json.loads(sr.group(1))
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

def parse_discography_index(html: str) -> list[str]:
    links = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+/discography/[^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = urljoin(SOURCES["jp_discography"], m.group(1))
        if not href.startswith("https://aespa-official.jp/discography/") or href.rstrip("/") == SOURCES["jp_discography"].rstrip("/"):
            continue
        links.append(href)
    return list(dict.fromkeys(links))


def parse_discography_detail(html: str, url: str) -> dict | None:
    text = clean_text(html)
    # Heading/label patterns seen on aespa Japan official pages.
    kind = "Album"
    for candidate in ("Digital Single", "Single", "Japan 1st Mini Album", "1st Japanese Mini Album", "Mini Album", "Full Album"):
        if candidate.lower() in text.lower():
            kind = candidate
            break
    title = None
    m = re.search(r"##\s+([^\n]+)", text)
    if m:
        title = m.group(1).strip()
    if not title:
        m = re.search(r"\b(?:Digital Single|Full Album|Mini Album|Japan 1st Mini Album)\b\s+([^\n]+)", text, re.I)
        if m:
            title = m.group(1).strip()
    if not title:
        # fallback from page slug
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        title = slug.replace("-", " ").title()
    date = None
    m = re.search(r"20\d{2}\.\d{1,2}\.\d{1,2}", text)
    if m:
        y, mo, da = map(int, m.group(0).split("."))
        date = f"{y:04d}-{mo:02d}-{da:02d}"
    if not date:
        m = re.search(r"20\d{2}/\d{1,2}/\d{1,2}", text)
        if m:
            y, mo, da = map(int, m.group(0).split("/"))
            date = f"{y:04d}-{mo:02d}-{da:02d}"
    if not date or not title:
        return None
    year = int(date[:4])

    # Artwork: use the first image under the discography page when it is clearly hosted by aespa Japan.
    artwork = ""
    for im in re.finditer(r'<img\b[^>]*src=["\']([^"\']+)["\']', html, re.I | re.S):
        src = urljoin(url, im.group(1))
        if "aespa-official.jp" in src and src.lower().endswith((".webp", ".jpg", ".jpeg", ".png")):
            artwork = src
            break

    tracks = []
    for m in re.finditer(r"(?:^|\s)(?:0?\d)\.\s*([^\n]+)", text):
        t = m.group(1).strip()
        if 1 < len(t) < 120 and not any(x in t.lower() for x in ("price", "release", "image", "tracklist")):
            tracks.append(t)
    # Deduplicate and trim to plausible tracklist entries.
    seen = set()
    clean_tracks = []
    for t in tracks:
        t = re.sub(r"\s+", " ", t)
        if t not in seen:
            seen.add(t)
            clean_tracks.append(t)
    return {"title": title, "year": year, "date": date, "kind": kind, "artwork": artwork, "tracks": clean_tracks, "url": url}


def itunes_duration(track: str) -> str | None:
    url = "https://itunes.apple.com/search?term=" + quote(f"aespa {track}") + "&entity=song&limit=8&country=US"
    try:
        payload = json.loads(fetch(url, accept="application/json,text/plain,*/*"))
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
    if not releases:
        return html, 0
    # Script 1 contains the Music data. Insert new A(...) lines before 'var X=['.
    marker = "var X=[\"aespa\"];"
    pos = html.find(marker)
    if pos < 0:
        raise RuntimeError("Music insertion point not found")

    existing_rel = set(re.findall(r'A\("([^"]+)"', html[:pos]))
    art_entries = []
    dur_entries = []
    additions = []

    for rel in releases:
        title = rel["title"]
        if title in existing_rel:
            continue
        if not rel["tracks"]:
            continue
        lang = "Japanese" if "japan" in rel["kind"].lower() or "japanese" in rel["kind"].lower() else "Korean"
        kind = rel["kind"]
        arr = []
        for track in rel["tracks"]:
            # The site's A() parser uses | as note separator, so escape by omitting pipe characters.
            track = track.replace("|", " — ")
            arr.append(track)
            dur = itunes_duration(track)
            if dur:
                dur_entries.append((track, dur))
        js_arr = ",".join(json.dumps(x, ensure_ascii=False) for x in arr)
        additions.append(f'A({json.dumps(title, ensure_ascii=False)},{rel["year"]},{json.dumps(kind, ensure_ascii=False)},{json.dumps(lang)}/Korean/,{""})')
        # Replace the malformed helper line with a concrete helper call below.
        additions[-1] = f'A({json.dumps(title, ensure_ascii=False)},{rel["year"]},{json.dumps(kind, ensure_ascii=False)},{json.dumps(lang)},{json.dumps(arr, ensure_ascii=False)})'
        if rel.get("artwork"):
            art_entries.append((title, rel["artwork"]))
        existing_rel.add(title)

    if not additions and not art_entries and not dur_entries:
        return html, 0

    # We need real JS calls. Convert additions to valid source lines.
    insertion = "\n".join(additions) + "\n" if additions else ""
    html = html[:pos] + insertion + html[pos:]

    # Update RELEASE_ART object.
    m = re.search(r"var RELEASE_ART=\{(.*?)\};", html, re.S)
    if art_entries and m:
        body = m.group(1).rstrip()
        for rel, art in art_entries:
            if re.search(r'"' + re.escape(rel) + r'"\s*:', body):
                body = re.sub(r'"' + re.escape(rel) + r'"\s*:\s*"[^"]*"', json.dumps(rel, ensure_ascii=False) + ":" + json.dumps(art, ensure_ascii=False), body)
            else:
                if body and not body.rstrip().endswith(","):
                    body += ","
                body += "\n " + json.dumps(rel, ensure_ascii=False) + ":" + json.dumps(art, ensure_ascii=False)
        html = html[:m.start(1)] + body + html[m.end(1):]

    # Update STATIC_DURATIONS object.
    m = re.search(r"var STATIC_DURATIONS=\{(.*?)\};", html, re.S)
    if dur_entries and m:
        body = m.group(1).rstrip()
        for track, dur in dur_entries:
            if re.search(r'"' + re.escape(track) + r'"\s*:', body):
                body = re.sub(r'"' + re.escape(track) + r'"\s*:\s*"[^"]*"', json.dumps(track, ensure_ascii=False) + ":" + json.dumps(dur), body)
            else:
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
                date = dt.strftime("%Y-%m-%d")
            except Exception:
                continue
            key = (date, norm(city))
            if key in seen or len(city) < 3:
                continue
            seen.add(key)
            out.append({"date": date, "title": f"SYNK : COMPLæXITY — {city}", "desc": "Official tour date from Weverse.", "source_key": "wv"})
    return out


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
    sr = json.loads(sm.group(1))
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
    sr = json.loads(sm.group(1))
    data = json.loads(dm.group(1))
    existing = {norm(x[2]) for x in data if isinstance(x, list) and len(x) >= 3}
    add = []
    for a in awards:
        title = f"2026 award update: {a['text']}"
        if norm(title) in existing:
            continue
        add.append(["aw", "2026", title, "Parsed from the current Wikipedia awards table; verify against the award organizer before treating it as final.", "wa"])
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

def discover_release_details(index_html: str) -> list[dict]:
    links = parse_discography_index(index_html)
    out = []
    for link in links:
        try:
            detail = parse_discography_detail(fetch(link), link)
            if detail:
                out.append(detail)
        except Exception:
            continue
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Only report candidates; do not modify index.html")
    args = parser.parse_args()

    if not HTML_FILE.exists():
        raise SystemExit("index.html not found at repository root")

    html = HTML_FILE.read_text(encoding="utf-8")
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sources_ok": 0,
        "sources_total": len(SOURCES),
        "errors": [],
        "candidates": {},
        "applied": {},
    }

    raw = {}
    for key, url in SOURCES.items():
        try:
            raw[key] = fetch(url, accept="application/json,text/plain,*/*" if key.endswith("api") else "text/html,application/xml;q=0.9,*/*;q=0.8")
            report["sources_ok"] += 1
        except Exception as exc:
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

    # Music
    releases = discover_release_details(raw["jp_discography"]) if "jp_discography" in raw else []
    report["candidates"]["music_releases"] = len(releases)
    if not args.dry_run and releases:
        html, n = update_music(html, releases)
    else:
        n = 0
    report["applied"]["music_releases_added"] = n

    # Calendar: Weverse tour notice + only clearly dated official news is handled here.
    tour = parse_weverse_tour(raw["weverse"]) if "weverse" in raw else []
    report["candidates"]["calendar"] = len(tour)
    if not args.dry_run and tour:
        html, n = update_calendar(html, tour)
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

    # Awards: use current Wikipedia awards table as a structured fallback, but mark it as such in the entry text.
    awards = []
    if "awards_wiki_api" in raw:
        try:
            awards = parse_awards_wikitext(raw["awards_wiki_api"])
        except Exception as exc:
            report["errors"].append({"source": "awards_wiki_api_parse", "error": str(exc)})
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


