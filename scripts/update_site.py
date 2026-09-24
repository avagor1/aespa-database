from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
HTML_FILE = ROOT / "index.html"
REPORT_FILE = ROOT / "data" / "automation_candidates.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; aespa-database-updater/1.0; "
    "+https://github.com/)"
)

SOURCES = {
    "aespa_japan_news": "https://aespa-official.jp/news/",
    "aespa_japan_schedule": "https://aespa-official.jp/schedule/",
    "weverse_notices": "https://weverse.io/aespa/notice",
    "weverse_shop": "https://shop.weverse.io/en/shop/MXN/artists/133/notices",
    "youtube_feed": "https://www.youtube.com/feeds/videos.xml?channel_id=UC9GtSLeksfK4yuJ_g1lgQbg",
}

EXCLUDED_VIDEO_WORDS = (
    "teaser",
    "making",
    "behind",
    "reaction",
    "performance",
    "choreography",
    "live",
    "dance practice",
    "interview",
    "shorts",
)


def fetch(url: str) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def clean_text(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize(value: str) -> str:
    value = unescape(value).lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def classify_news(title: str) -> str:
    t = normalize(title)
    if any(x in t for x in ("tour", "concert", "live", "fan meeting", "festival", "公演", "ライブ", "ツアー")):
        return "cc"
    if any(x in t for x in ("release", "album", "single", "comeback", "music video", " mv", "リリース", "アルバム", "シングル", "発売", "mv")):
        return "co"
    if any(x in t for x in ("ambassador", "brand", "collab", "collaboration", "コラボ", "アンバサダー", "ブランド")):
        return "cl"
    if any(x in t for x in ("karina", "winter", "giselle", "ningning", "カリナ", "ウィンター", "ジゼル", "ニンニン")):
        return "ma"
    if any(x in t for x in ("notice", "announcement", "お知らせ", "決定", "開催決定")):
        return "an"
    return "ou"


def parse_japan_news(html: str) -> list[dict[str, str]]:
    anchors = []
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = urljoin(SOURCES["aespa_japan_news"], m.group(1))
        title = clean_text(m.group(2))
        if title and href.startswith("https://aespa-official.jp/news/"):
            anchors.append((m.start(), href, title))

    dates = list(re.finditer(r"20\d{2}\.\d{1,2}\.\d{1,2}", html))
    items = []

    for d in dates:
        raw = d.group(0)
        y, m, day = [int(x) for x in raw.split(".")]
        date_iso = f"{y:04d}-{m:02d}-{day:02d}"
        candidates = [a for a in anchors if d.end() <= a[0] <= d.end() + 2200]
        candidates.sort(key=lambda x: x[0])
        chosen = None
        for a in candidates:
            title = a[2]
            if len(title) < 5:
                continue
            if normalize(title) in {"news", "next", "previous", "home", "profile"}:
                continue
            chosen = a
            break
        if not chosen:
            continue
        item = {
            "source": "aespa Japan Official",
            "source_key": "aj",
            "kind": "news",
            "date": date_iso,
            "title": chosen[2],
            "url": chosen[1],
            "category": classify_news(chosen[2]),
        }
        if not any(normalize(x["title"]) == normalize(item["title"]) for x in items):
            items.append(item)

    items.sort(key=lambda x: x["date"], reverse=True)
    return items


def parse_generic_notice_page(html: str, base_url: str, source_name: str) -> list[dict[str, str]]:
    items = []
    seen = set()
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        href = urljoin(base_url, m.group(1))
        title = clean_text(m.group(2))
        if not title or len(title) < 10:
            continue
        key = (href, normalize(title))
        if key in seen:
            continue
        seen.add(key)
        if "/notice" not in href and "/notices" not in href:
            continue
        items.append({
            "source": source_name,
            "kind": "notice",
            "title": title,
            "url": href,
        })
    return items[:40]


def parse_youtube_feed(xml_text: str) -> list[dict[str, str]]:
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    root = ET.fromstring(xml_text)
    items = []
    for entry in root.findall("atom:entry", ns):
        title = entry.findtext("atom:title", default="", namespaces=ns).strip()
        video_id = entry.findtext("yt:videoId", default="", namespaces=ns).strip()
        published = entry.findtext("atom:published", default="", namespaces=ns).strip()
        updated = entry.findtext("atom:updated", default="", namespaces=ns).strip()
        link = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        if not title or not link:
            continue
        low = normalize(title)
        looks_like_mv = "mv" in low or "music video" in low
        excluded = any(word in low for word in EXCLUDED_VIDEO_WORDS)
        items.append({
            "source": "aespa Official YouTube",
            "kind": "video",
            "title": title,
            "url": link,
            "video_id": video_id,
            "published": published,
            "updated": updated,
            "likely_mv": looks_like_mv and not excluded,
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        })
    return items


def extract_current_news(html: str) -> tuple[str, list[list], str]:
    """Return news-script block, current D array, and SR object text."""
    marker = html.find("/* news */")
    if marker == -1:
        raise RuntimeError("Could not find the News script block in index.html")
    end = html.find("</script>", marker)
    if end == -1:
        raise RuntimeError("News script block is malformed")
    block = html[marker:end]

    sr_match = re.search(r"var\s+SR\s*=\s*(\{.*?\});", block, re.S)
    d_match = re.search(r"var\s+D\s*=\s*(\[.*?\]);", block, re.S)
    if not sr_match or not d_match:
        raise RuntimeError("Could not find News source map or data array")

    data = json.loads(d_match.group(1))
    return block, data, sr_match.group(1)


def update_news_block(html: str, candidates: list[dict[str, str]]) -> tuple[str, int]:
    marker = html.find("/* news */")
    end = html.find("</script>", marker)
    block = html[marker:end]

    sr_match = re.search(r"var\s+SR\s*=\s*(\{.*?\});", block, re.S)
    d_match = re.search(r"var\s+D\s*=\s*(\[.*?\]);", block, re.S)
    if not sr_match or not d_match:
        raise RuntimeError("Could not find News data")

    data = json.loads(d_match.group(1))
    existing_titles = {normalize(x[2]) for x in data if isinstance(x, list) and len(x) >= 3}

    new_rows = []
    for item in candidates:
        title = item["title"]
        if normalize(title) in existing_titles:
            continue
        desc = "Official update from aespa Japan."
        new_rows.append([
            item.get("category", "ou"),
            item["date"],
            title,
            desc,
            "aj",
        ])
        existing_titles.add(normalize(title))

    if not new_rows:
        return html, 0

    merged = new_rows + data
    # Keep the array bounded so it does not grow forever.
    merged = merged[:160]

    new_sr = sr_match.group(1)
    if '"aj"' not in new_sr:
        new_sr = new_sr[:-1] + ',"aj":["aespa Japan Official","https://aespa-official.jp/news/"]}'

    new_block = re.sub(
        r"var\s+SR\s*=\s*(\{.*?\});",
        "var SR=" + new_sr + ";",
        block,
        count=1,
        flags=re.S,
    )
    new_block = re.sub(
        r"var\s+D\s*=\s*(\[.*?\]);",
        "var D=" + json.dumps(merged, ensure_ascii=False, separators=(",", ":")) + ";",
        new_block,
        count=1,
        flags=re.S,
    )

    updated = html[:marker] + new_block + html[end:]
    return updated, len(new_rows)


def collect() -> dict:
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sources": {},
        "candidates": [],
        "errors": [],
    }

    for key in ("aespa_japan_news", "aespa_japan_schedule", "weverse_notices", "weverse_shop", "youtube_feed"):
        try:
            html = fetch(SOURCES[key])
            result["sources"][key] = {"ok": True, "url": SOURCES[key]}

            if key == "aespa_japan_news":
                result["candidates"].extend(parse_japan_news(html))
            elif key == "youtube_feed":
                result["candidates"].extend(parse_youtube_feed(html))
            elif key == "weverse_notices":
                result["candidates"].extend(
                    parse_generic_notice_page(html, SOURCES[key], "Weverse aespa")
                )
            elif key == "weverse_shop":
                result["candidates"].extend(
                    parse_generic_notice_page(html, SOURCES[key], "Weverse Shop")
                )
            elif key == "aespa_japan_schedule":
                result["candidates"].extend(
                    parse_generic_notice_page(html, SOURCES[key], "aespa Japan Schedule")
                )

        except Exception as exc:
            result["sources"][key] = {"ok": False, "url": SOURCES[key]}
            result["errors"].append({"source": key, "error": str(exc)})

    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply safe News updates to index.html")
    args = parser.parse_args()

    if not HTML_FILE.exists():
        raise SystemExit("index.html was not found at the repository root.")

    result = collect()

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # De-duplicate candidates by (kind, title, url)
    unique = {}
    for item in result["candidates"]:
        unique[(item.get("kind"), normalize(item.get("title", "")), item.get("url", ""))] = item
    result["candidates"] = list(unique.values())

    if args.apply:
        original = HTML_FILE.read_text(encoding="utf-8")
        japan_news = [x for x in result["candidates"] if x.get("kind") == "news" and x.get("source_key") == "aj"]
        updated, added = update_news_block(original, japan_news)
        if added:
            HTML_FILE.write_text(updated, encoding="utf-8")
        result["applied"] = {
            "news_entries_added": added,
            "index_updated": bool(added),
        }
    else:
        result["applied"] = {
            "news_entries_added": 0,
            "index_updated": False,
        }

    REPORT_FILE.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps({
        "sources_ok": sum(1 for x in result["sources"].values() if x["ok"]),
        "sources_total": len(result["sources"]),
        "candidates": len(result["candidates"]),
        "applied": result["applied"],
        "errors": len(result["errors"]),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

