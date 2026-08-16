#!/usr/bin/env python3
"""
Scrape ankergames.net for games and produce a HydraLauncher JSON file.

Usage:
  pip install requests beautifulsoup4 python-dateutil lxml
  python scrape_ankergames.py --domain https://ankergames.net --output hydralauncher.json

Defaults:
  - Top-level "name" field defaults to "AnkerGames" (override with --name).
Notes:
  - The script respects robots.txt and uses sitemap.xml when present.
  - It looks for direct file hosts and direct archive links; adjust HOST_PATTERNS if you need extra hosts.
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from xml.etree import ElementTree as ET
from urllib.robotparser import RobotFileParser

# Known hosts and file extensions to treat as downloads
HOST_PATTERNS = [
    r"gofile\.io", r"gofile\.us", r"mega\.nz", r"drive\.google\.com",
    r"dropbox\.com", r"mediafire\.com", r"zippyshare\.com", r"sendspace\.com",
    r"weebly\.com", r"anonfiles\.com", r"filemoon\.sx", r"cdn-.*\.",
]
EXT_PATTERNS = [r"\.zip$", r"\.rar$", r"\.7z$", r"\.iso$", r"\.exe$", r"\.msi$", r"\.tar\.gz$"]

# Regex for file size (MB/GB/KB/TB)
SIZE_RE = re.compile(r'(\d+(?:[.,]\d+)?\s?(?:GB|MB|KB|TB))', re.IGNORECASE)

# Date patterns fallback (e.g., "August 15, 2026" or "15 Aug 2026")
# We'll prefer parsing by dateutil if any date-ish string is found near metadata/time tag.

USER_AGENT = "AnkerGamesScraper/1.0 (+https://example.com/)"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})

def is_same_domain(base_netloc, url):
    try:
        p = urlparse(url)
        return p.netloc == "" or p.netloc == base_netloc
    except Exception:
        return False

def fetch_url(url, timeout=20):
    try:
        r = session.get(url, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        # print(f"fetch error {url}: {e}", file=sys.stderr)
        return None

def parse_sitemap(sitemap_xml, base_url):
    urls = set()
    try:
        root = ET.fromstring(sitemap_xml)
    except Exception:
        return urls
    ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    # support <url><loc>...</loc></url>
    for loc in root.findall('.//sm:url/sm:loc', ns):
        if loc.text:
            urls.add(loc.text.strip())
    # support sitemap index (sitemap of sitemaps)
    for sm in root.findall('.//sm:sitemap/sm:loc', ns):
        if sm.text:
            # try to fetch nested sitemap
            nested = fetch_url(sm.text.strip())
            if nested and nested.text:
                urls.update(parse_sitemap(nested.text, base_url))
    return urls

def find_sitemap(domain):
    candidates = ["sitemap.xml", "sitemap_index.xml", "sitemap/sitemap.xml"]
    parsed = urlparse(domain)
    base = f"{parsed.scheme}://{parsed.netloc}"
    for cand in candidates:
        url = urljoin(base + "/", cand)
        r = fetch_url(url)
        if r and r.status_code == 200 and "xml" in r.headers.get("Content-Type", ""):
            return r.text
    return None

def discover_site_urls(domain, max_pages=5000, delay=0.5):
    parsed = urlparse(domain)
    base = f"{parsed.scheme}://{parsed.netloc}"
    robot_url = urljoin(base + "/", "robots.txt")
    rp = RobotFileParser()
    try:
        rp.set_url(robot_url)
        rp.read()
    except Exception:
        # fallback: assume allowed
        pass

    sitemap_xml = find_sitemap(domain)
    if sitemap_xml:
        urls = parse_sitemap(sitemap_xml, base)
        # filter to same host
        return [u for u in urls if is_same_domain(parsed.netloc, u)]

    # fallback BFS crawl
    to_visit = [base + "/"]
    seen = set()
    result_urls = set()
    while to_visit and len(seen) < max_pages:
        u = to_visit.pop(0)
        if u in seen:
            continue
        seen.add(u)
        if rp and hasattr(rp, "can_fetch"):
            try:
                if not rp.can_fetch(USER_AGENT, u):
                    # respect robots
                    continue
            except Exception:
                pass
        r = fetch_url(u)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "lxml")
        # add candidate pages (keep everything for now)
        result_urls.add(u)
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("javascript:") or href.startswith("mailto:"):
                continue
            full = urljoin(u, href)
            parsed_full = urlparse(full)
            # normalize remove fragments
            full = parsed_full._replace(fragment="").geturl()
            if not is_same_domain(parsed.netloc, full):
                continue
            if full not in seen and full not in to_visit:
                to_visit.append(full)
        time.sleep(delay)
    return list(result_urls)

def looks_like_download_link(href):
    if not href:
        return False
    for pat in HOST_PATTERNS:
        if re.search(pat, href, re.IGNORECASE):
            return True
    for ext in EXT_PATTERNS:
        if re.search(ext, href, re.IGNORECASE):
            return True
    return False

def extract_nearby_size(text):
    if not text:
        return None
    m = SIZE_RE.search(text)
    if m:
        return m.group(1).replace(",", ".").strip()
    return None

def parse_game_page(url, base_netloc):
    r = fetch_url(url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "lxml")
    # Title
    title = None
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        h1 = soup.find("h1")
        if h1 and h1.get_text(strip=True):
            title = h1.get_text(strip=True)
    if not title:
        # fallback to title tag
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

    # Find download links
    uris = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(url, href)
        if looks_like_download_link(full):
            uris.append(full)

    # dedupe uris while preserving order
    seen = set()
    uris_clean = []
    for u in uris:
        if u not in seen:
            seen.add(u)
            uris_clean.append(u)

    if not uris_clean:
        # no direct downloads found, skip this page as not a game page
        # but sometimes download link may be embedded in buttons or JS. We'll still attempt to extract metadata if any.
        pass

    # uploadDate: look for <time datetime=...>, or meta property article:published_time, or text like 'Posted on'
    upload_date = None
    time_tag = soup.find("time")
    if time_tag and time_tag.has_attr("datetime"):
        try:
            d = dateparser.parse(time_tag["datetime"])
            upload_date = d.isoformat()
        except Exception:
            pass
    if not upload_date:
        meta_time = soup.find("meta", property="article:published_time")
        if meta_time and meta_time.get("content"):
            try:
                d = dateparser.parse(meta_time["content"])
                upload_date = d.isoformat()
            except Exception:
                pass
    if not upload_date:
        # search for date-like strings near top meta
        meta_cont = soup.find(class_=re.compile(r"(post-meta|meta|date|posted|entry-meta)", re.I))
        if meta_cont:
            txt = meta_cont.get_text(" ", strip=True)
            try:
                d = dateparser.parse(txt, fuzzy=True)
                upload_date = d.isoformat()
            except Exception:
                upload_date = None
    if not upload_date:
        # fallback: look for any 4-digit year patterns in the document and parse small surrounding text
        m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[^\n]{0,40}\d{4}', r.text, re.I)
        if m:
            try:
                d = dateparser.parse(m.group(0), fuzzy=True)
                upload_date = d.isoformat()
            except Exception:
                pass
    # fileSize: search the whole page for patterns like "1.23 GB"
    file_size = None
    m = SIZE_RE.search(r.text)
    if m:
        file_size = m.group(1).replace(",", ".").strip()
    # also attempt to look near each download link for size
    if not file_size and uris_clean:
        for a in soup.find_all("a", href=True):
            full = urljoin(url, a["href"].strip())
            if full in uris_clean:
                # look in parent and siblings
                text_block = " ".join([
                    a.get_text(" ", strip=True) or "",
                    (a.parent.get_text(" ", strip=True) if a.parent else ""),
                    (a.parent.parent.get_text(" ", strip=True) if a.parent and a.parent.parent else ""),
                ])
                s = extract_nearby_size(text_block)
                if s:
                    file_size = s
                    break

    # Compose result (if at least title and uris exist)
    if not title:
        # skip pages with no title
        return None
    result = {
        "title": title,
        "uploadDate": upload_date if upload_date else None,
        "fileSize": file_size if file_size else None,
        "uris": uris_clean
    }
    return result

def main():
    parser = argparse.ArgumentParser(description="Scrape ankergames.net and produce hydralauncher JSON.")
    parser.add_argument("--domain", required=True, help="Base domain (e.g., https://ankergames.net)")
    parser.add_argument("--output", default="hydralauncher.json", help="Output JSON file")
    parser.add_argument("--name", default="AnkerGames", help="Top-level name field for JSON")
    parser.add_argument("--workers", type=int, default=8, help="Parallel workers")
    parser.add_argument("--maxpages", type=int, default=2000, help="Max pages to crawl if no sitemap")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between requests (seconds) for crawler")
    args = parser.parse_args()

    print("Discovering site URLs (this may take a while)...", file=sys.stderr)
    urls = discover_site_urls(args.domain, max_pages=args.maxpages, delay=args.delay)
    print(f"Discovered {len(urls)} URLs; parsing pages with {args.workers} workers...", file=sys.stderr)

    parsed = urlparse(args.domain)
    base_netloc = parsed.netloc

    downloads = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(parse_game_page, u, base_netloc): u for u in urls}
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                res = fut.result()
                if res:
                    # ensure at least one URI for inclusion
                    if res.get("uris"):
                        downloads.append(res)
            except Exception as e:
                # print(f"error parsing {u}: {e}", file=sys.stderr)
                pass

    # Sort by uploadDate descending if available
    def sort_key(item):
        return item.get("uploadDate") or ""
    downloads_sorted = sorted(downloads, key=sort_key, reverse=True)

    out = {
        "name": args.name,
        "downloads": downloads_sorted
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"Wrote {len(downloads_sorted)} items to {args.output}", file=sys.stderr)
