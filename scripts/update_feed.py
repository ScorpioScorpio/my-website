#!/usr/bin/env python3
"""
Full digest run (every 6 hours): fetches recent immigration-related
executive orders (Federal Register API), summarizes + translates each
new item with Hugging Face models, and writes/updates data/updates.json.

Safe to run repeatedly: it skips items already present in updates.json,
so only genuinely new items cost an API call.
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from feed_common import get_client, is_relevant, safe_summarize, safe_translate, fetch_executive_orders_multi

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "updates.json"
MAX_ITEMS_KEPT = 60

client = get_client()


def load_existing():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"generated_at": None, "items": []}
    return {"generated_at": None, "items": []}


def fetch_eo_items():
    print("Fetching executive orders from Federal Register…")
    results = fetch_executive_orders_multi(per_page=20)
    items = []
    for r in results:
        title = r.get("title", "")
        abstract = r.get("abstract") or title
        if not is_relevant(f"{title}. {abstract}"):
            continue
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


def main():
    existing = load_existing()
    existing_ids = {item["id"] for item in existing.get("items", [])}

    candidates = fetch_eo_items()
    new_items = [c for c in candidates if c["id"] not in existing_ids]

    print(f"Found {len(candidates)} candidate items, {len(new_items)} are new.")

    processed = []
    for item in new_items:
        print(f"Processing: {item['title_en'][:70]}")
        summary_en = safe_summarize(client, item["raw_text"])
        title_es = safe_translate(client, item["title_en"])
        summary_es = safe_translate(client, summary_en)
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
        time.sleep(1)

    all_items = processed + existing.get("items", [])
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
