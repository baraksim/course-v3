#!/usr/bin/env python3
import argparse
import os
import re
import sys
import json
import time
import csv
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, FeatureNotFound
from tqdm import tqdm
import dateparser


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
}

HEBREW_STOPWORDS = set([
    "של","על","עם","גם","עוד","כך","זה","זו","לא","כן","או","אבל","אם","בלי",
    "שלו","שלה","שלי","יותר","פחות","כמו","מאוד","הוא","היא","הם","הן","את","אני",
    "אנחנו","אתם","אתן","זהו","זאת","יש","אין","כל","כלל","רק","אל","עד","אחרי",
    "לפני","כאשר","כי","שה","לפי","ללא","וכן","וכן","אותו","אותה","אותם","אותן",
    "פה","שם","ה","ו","ש","כ","ל","מ","ב"
])

WORD_RE = re.compile(r"[\u0590-\u05FF]+")
NUMERIC_ID_URL_RE = re.compile(r"^https?://(?:www\.)?inn\.co\.il/\d{4,9}(?:[/?#].*)?$")
NEWS_URL_RE = re.compile(r"^https?://(?:www\.)?inn\.co\.il/news/\d{3,9}(?:[/?#].*)?$")


def get_soup(html):
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def make_session():
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def fetch(session, url, max_retries=3, backoff=1.5):
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=25)
            if resp.status_code >= 400:
                raise requests.HTTPError(f"{resp.status_code} for {url}")
            resp.encoding = resp.apparent_encoding or resp.encoding or 'utf-8'
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(backoff ** attempt)
            else:
                raise
    if last_exc:
        raise last_exc
    raise RuntimeError("Unreachable")


def is_same_domain(url):
    try:
        return urlparse(url).netloc.endswith("inn.co.il")
    except Exception:
        return False


def extract_candidate_links(index_html, base_url):
    soup = get_soup(index_html)
    links = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        abs_url = urljoin(base_url, href)
        if not is_same_domain(abs_url):
            continue
        if NEWS_URL_RE.match(abs_url) or NUMERIC_ID_URL_RE.match(abs_url):
            links.add(abs_url.split("#")[0])

    return sorted(links)


def extract_text_from_container(container):
    parts = []
    for p in container.find_all(["p", "h2", "li"], recursive=True):
        txt = p.get_text(" ", strip=True)
        if txt:
            parts.append(txt)
    return "\n".join(parts)


def parse_article(html, url):
    soup = get_soup(html)

    # Title
    title = None
    og = soup.find("meta", attrs={"property": "og:title"})
    if og and og.get("content"):
        title = og["content"].strip()
    if not title:
        tw = soup.find("meta", attrs={"name": "twitter:title"})
        if tw and tw.get("content"):
            title = tw["content"].strip()
    if not title:
        h1 = soup.find(["h1", "title"])  # some pages may use <title>
        if h1:
            title = h1.get_text(" ", strip=True)
    title = title or ""

    # Date
    pub_iso = None
    meta_date = soup.find("meta", attrs={"property": "article:published_time"})
    if meta_date and meta_date.get("content"):
        pub_iso = meta_date["content"].strip()
    if not pub_iso:
        time_tag = soup.find("time")
        if time_tag and time_tag.get("datetime"):
            pub_iso = time_tag["datetime"].strip()
    published_at = None
    if pub_iso:
        try:
            dt = dateparser.parse(pub_iso)
            if dt:
                published_at = dt.isoformat()
        except Exception:
            published_at = None

    if not published_at:
        possible = soup.find(text=re.compile(r"\d{1,2}[/.]\d{1,2}[/.]\d{2,4}|\d{4}-\d{2}-\d{2}"))
        if possible:
            dt = dateparser.parse(str(possible), languages=["he", "en"])  # heuristic
            if dt:
                published_at = dt.isoformat()

    # Content
    content = ""
    candidates = []
    candidates.extend(soup.find_all(attrs={"itemprop": "articleBody"}))
    candidates.extend(soup.find_all(["article", "main"]))
    candidates.extend(soup.select(".article, .article-body, .content, .post-content, .text"))

    for c in candidates:
        content = extract_text_from_container(c)
        if len(content) > 200:
            break

    if len(content) < 200:
        content = extract_text_from_container(soup)

    return {
        "url": url,
        "title": title,
        "published_at": published_at,
        "content": content,
        "content_chars": len(content or ""),
    }


def tokenize_hebrew(text):
    tokens = [t for t in WORD_RE.findall(text) if len(t) > 1]
    cleaned = []
    for tok in tokens:
        if len(tok) > 2 and tok[0] in {"ו","ב","כ","ל","מ","ש"}:
            core = tok[1:]
            if core not in HEBREW_STOPWORDS and len(core) > 1:
                cleaned.append(core)
        if tok not in HEBREW_STOPWORDS:
            cleaned.append(tok)
    return cleaned


def build_summary(records):
    num_articles = len(records)
    num_with_content = sum(1 for r in records if (r.get("content_chars") or 0) > 0)

    # Dates
    dates = []
    for r in records:
        val = r.get("published_at")
        if not val:
            continue
        try:
            dt = dateparser.parse(val)
            if dt:
                dates.append(dt)
        except Exception:
            continue
    date_min = min(dates).isoformat() if dates else None
    date_max = max(dates).isoformat() if dates else None

    # Keywords
    contents = [r.get("content", "") for r in records if (r.get("content_chars") or 0) > 0]
    all_text = "\n".join(contents)
    tokens = tokenize_hebrew(all_text)
    top_words = dict(Counter(tokens).most_common(30)) if tokens else {}

    titles_text = "\n".join([r.get("title", "") for r in records])
    title_tokens = tokenize_hebrew(titles_text)
    top_title_words = dict(Counter(title_tokens).most_common(20)) if title_tokens else {}

    return {
        "num_articles": num_articles,
        "num_with_content": num_with_content,
        "date_min": date_min,
        "date_max": date_max,
        "top_words": top_words,
        "top_title_words": top_title_words,
    }


def write_csv(records, path):
    # Collect all keys
    keys = set()
    for r in records:
        keys.update(r.keys())
    fieldnames = [
        "url",
        "title",
        "published_at",
        "content",
        "content_chars",
    ]
    for k in sorted(keys):
        if k not in fieldnames:
            fieldnames.append(k)

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow(r)


def fetch_sitemap_urls(session, sitemap_index_url, limit=0):
    xml = fetch(session, sitemap_index_url)
    soup = get_soup(xml)
    loc_tags = soup.find_all("loc")
    urls = [t.get_text(strip=True) for t in loc_tags]
    # If this is a sitemap index (points to feeds), fetch each feed
    article_urls = []
    if any("/api/Google/" in u for u in urls):
        feed_urls = urls
        for fu in tqdm(feed_urls, desc="Fetching sitemap feeds", unit="feed"):
            try:
                feed_xml = fetch(session, fu)
                feed_soup = get_soup(feed_xml)
                for loc in feed_soup.find_all("loc"):
                    u = loc.get_text(strip=True)
                    if NEWS_URL_RE.match(u):
                        article_urls.append(u)
                    elif NUMERIC_ID_URL_RE.match(u):
                        article_urls.append(u)
            except Exception as exc:
                sys.stderr.write(f"Failed feed {fu}: {exc}\n")
            if limit and len(article_urls) >= limit:
                break
    else:
        # It is a direct urlset
        for loc in loc_tags:
            u = loc.get_text(strip=True)
            if NEWS_URL_RE.match(u) or NUMERIC_ID_URL_RE.match(u):
                article_urls.append(u)

    if limit and len(article_urls) > limit:
        article_urls = article_urls[:limit]
    # De-duplicate while preserving order
    seen = set()
    deduped = []
    for u in article_urls:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def main():
    parser = argparse.ArgumentParser(description="Scrape inn.co.il article links from a page and summarize")
    parser.add_argument("--url", required=False, help="Index/list page URL to extract article links from")
    parser.add_argument("--sitemap", action="store_true", help="Use sitemap to collect article URLs")
    parser.add_argument("--sitemap-url", default="https://www.inn.co.il/sitemap.xml", help="Sitemap index URL")
    parser.add_argument("--out", default="/workspace/data/inn_output", help="Output directory for CSV/JSON")
    parser.add_argument("--limit", type=int, default=0, help="Optional limit of articles to fetch (0=all)")
    args = parser.parse_args()

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)

    session = make_session()

    if args.sitemap:
        print(f"Fetching sitemap: {args.sitemap_url}")
        links = fetch_sitemap_urls(session, args.sitemap_url, limit=args.limit)
    else:
        if not args.url:
            print("--url is required when not using --sitemap", file=sys.stderr)
            sys.exit(2)
        print(f"Fetching index: {args.url}")
        index_html = fetch(session, args.url)
        links = extract_candidate_links(index_html, args.url)
        if args.limit > 0:
            links = links[: args.limit]

    print(f"Found {len(links)} candidate article URLs")

    records = []
    for url in tqdm(links, desc="Fetching articles", unit="article"):
        try:
            html = fetch(session, url)
            rec = parse_article(html, url)
            records.append(rec)
        except Exception as exc:
            sys.stderr.write(f"Failed {url}: {exc}\n")
            continue

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(out_dir, f"articles_{ts}.csv")
    json_path = os.path.join(out_dir, f"articles_{ts}.json")
    summary_path = os.path.join(out_dir, f"summary_{ts}.json")

    write_csv(records, csv_path)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    summary = build_summary(records)
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print("Saved:")
    print(f"  CSV: {csv_path}")
    print(f"  JSON: {json_path}")
    print(f"  SUMMARY: {summary_path}")


if __name__ == "__main__":
    main()