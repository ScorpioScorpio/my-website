#!/usr/bin/env python3
"""
Fetches recent immigration-related executive orders (Federal Register API)
and immigration news (RSS feeds), summarizes + translates each new item
with Hugging Face models, and writes/updates data/updates.json.

Safe to run repeatedly: it skips items already present in updates.json,
so only genuinely new items cost an API call.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import feedparser
from huggingface_hub import InferenceClient

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "updates.json"
MAX_ITEMS_KEPT = 60          # trim the feed file to this many most-recent items
FED_REGISTER_LOOKBACK = 30   # days to look back on Federal Register for EOs
SUMMARY_MODEL = "facebook/bart-large-cnn"
TRANSLATE_MODEL = "Helsinki-NLP/opus-mt-en-es"

# Keywords used to decide whether a document/news item is immigration-related
KEYWORDS = [
    "immigra", "asylum", "refugee", "deport", "border", "visa",
    "citizenship", "ICE", "DHS", "undocumented", "migrant", "naturaliz",
]

# Curated immigration-focused RSS feeds (add/remove as you like)
RSS_FEEDS = [
    "https://www.uscis.gov/news/rss-feeds/all-news",
    "https://www.dhs.gov/news-releases/press-releases/feed",
    "https://immigrationimpact.com/feed/",
]

HF_TOKEN = os.environ.get("HF_TOKEN")
if not HF_TOKEN:
    print("ERROR: HF_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

client = InferenceClient(token=HF_TOKEN)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_existing():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"generated_at": None, "items": []}
    return {"generated_at": None, "items": []}


def is_relevant(text):
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in KEYWORDS)


def safe_summarize(text, max_length=90, min_length=25):
    """Summarize text with Hugging Face; fall back to a truncated version on failure."""
    text = text.strip()
    if len(text) < 200:
        # Too short to meaningfully summarize — just return as-is.
        return text
    try:
        result = client.summarization(
            text[:3000],  # keep well within model input limits
            model=SUMMARY_MODEL,
            parameters={"max_length": max_length, "min_length": min_length},
        )
        return result.summary_text if hasattr(result, "summary_text") else result["summary_text"]
    except Exception as e:
        print(f"  [warn] summarization failed, using truncated text: {e}", file=sys.stderr)
        return (text[:280] + "…") if len(text) > 280 else text


def safe_translate(text):
    """Translate English text to Spanish; fall back to English on failure."""
    try:
        result = client.translation(text, model=TRANSLATE_MODEL)
        return result.translation_text if hasattr(result, "translation_text") else result["translation_text"]
    except Exception as e:
        print(f"  [warn] translation failed, keeping English: {e}", file=sys.stderr)
        return text


# ---------------------------------------------------------------------------
# Source: Federal Register — Executive Orders
# ---------------------------------------------------------------------------

def fetch_executive_orders():
    print("Fetching executive orders from Federal Register…")
    url = "https://www.federalregister.gov/api/v1/documents.json"
    params = {
        "conditions[type][]": "PRESDOCU",
        "conditions[presidential_document_type]": "executive_order",
        "conditions[term]": "immigration",
        "order": "newest",
        "per_page": 20,
        "fields[]": [
            "title", "abstract", "html_url", "publication_date",
            "signing_date", "executive_order_number", "document_number",
        ],
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        print(f"  [error] Federal Register fetch failed: {e}", file=sys.stderr)
        return []

    items = []
    for r in results:
        title = r.get("title", "")
        abstract = r.get("abstract") or title
        items.append({
            "id": f"eo-{r.get('document_number')}",
            "type": "executive_order",
            "title_en": title,
            "date": r.get("signing_date") or r.get("publication_date"),
            "url": r.get("html_url"),
            "source": "Federal Register (official)",
            "raw_text": abstract,
        })
    return items


# ---------------------------------------------------------------------------
# Source: curated RSS feeds
# ---------------------------------------------------------------------------

def fetch_news():
    print("Fetching immigration news from RSS feeds…")
    items = []
    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as e:
            print(f"  [warn] could not read {feed_url}: {e}", file=sys.stderr)
            continue

        source_name = parsed.feed.get("title", feed_url)
        for entry in parsed.entries[:15]:
            title = entry.get("title", "")
            summary_raw = entry.get("summary", "") or title
            combined = f"{title}. {summary_raw}"
            if not is_relevant(combined):
                continue
            entry_id = entry.get("id") or entry.get("link")

            # feedparser gives us a pre-parsed time struct when it can — use
            # that instead of slicing the raw string, which is not reliably
            # in YYYY-MM-DD format across feeds.
            time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if time_struct:
                date_str = datetime(*time_struct[:6], tzinfo=timezone.utc).date().isoformat()
            else:
                date_str = datetime.now(timezone.utc).date().isoformat()

            items.append({
                "id": f"news-{abs(hash(entry_id))}",
                "type": "news",
                "title_en": title,
                "date": date_str,
                "url": entry.get("link"),
                "source": source_name,
                "raw_text": summary_raw,
            })
    return items


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    existing = load_existing()
    existing_ids = {item["id"] for item in existing.get("items", [])}

    candidates = fetch_executive_orders() + fetch_news()
    new_items = [c for c in candidates if c["id"] not in existing_ids]

    print(f"Found {len(candidates)} candidate items, {len(new_items)} are new.")

    processed = []
    for item in new_items:
        print(f"Processing: {item['title_en'][:70]}")
        summary_en = safe_summarize(item["raw_text"])
        title_es = safe_translate(item["title_en"])
        summary_es = safe_translate(summary_en)
        processed.append({
            "id": item["id"],
            "type": item["type"],
            "date": item["date"],
            "url": item["url"],
            "source": item["source"],
            "title_en": item["title_en"],
            "title_es": title_es,
            "summary_en": summary_en,
            "summary_es": summary_es,
        })
        time.sleep(1)  # be polite to the free inference tier

    all_items = processed + existing.get("items", [])
    # sort newest first, trim
    all_items.sort(key=lambda x: x.get("date") or "", reverse=True)
    all_items = all_items[:MAX_ITEMS_KEPT]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": all_items,
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(all_items)} items to {DATA_PATH}")


if __name__ == "__main__":
    main()
