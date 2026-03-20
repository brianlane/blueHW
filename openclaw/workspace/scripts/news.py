#!/usr/bin/env python3
"""Fetch headlines from major RSS feeds. No API keys needed."""
import urllib.request
import xml.etree.ElementTree as ET
import sys
import json
from datetime import datetime

FEEDS = {
    "reuters": "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "bbc": "https://feeds.bbci.co.uk/news/business/rss.xml",
    "cnbc": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147",
    "ap": "https://rsshub.app/apnews/topics/business",
    "marketwatch": "https://feeds.marketwatch.com/marketwatch/topstories/",
}

def fetch_feed(name, url, limit=5):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml = resp.read()
        root = ET.fromstring(xml)
        items = []
        for item in root.iter("item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            pubDate = item.findtext("pubDate", "")
            if title:
                items.append({"title": title.strip(), "link": link.strip(), "date": pubDate.strip()})
            if len(items) >= limit:
                break
        return items
    except Exception as e:
        return [{"error": str(e)}]

def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else None
    sources = sys.argv[2].split(",") if len(sys.argv) > 2 else list(FEEDS.keys())
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5

    print(f"=== NEWS DIGEST {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC ===\n")
    for name in sources:
        if name not in FEEDS:
            continue
        items = fetch_feed(name, FEEDS[name], limit)
        print(f"[{name.upper()}]")
        for i, item in enumerate(items, 1):
            if "error" in item:
                print(f"  Error: {item['error']}")
            else:
                title = item["title"]
                if topic and topic.lower() not in title.lower():
                    continue
                print(f"  {i}. {title}")
                if item.get("date"):
                    print(f"     {item['date']}")
        print()

if __name__ == "__main__":
    main()
