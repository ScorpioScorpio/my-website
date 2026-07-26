#!/usr/bin/env python3
"""
Urgent check (every 15 minutes): a fast, narrow scan for anything
genuinely NEW — a freshly published executive order — that hasn't been
seen before. Only calls Hugging Face when something new is actually
found, so it stays cheap to run this often. Writes data/urgent.json,
which the site polls to show a dismissible alert banner.

This is deliberately separate from update_feed.py (the full 6-hour
digest): that job does a broad sweep and rebuilds the whole list; this
one only ever asks "is there something brand new right now?"
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from feed_common import get_client, is_relevant, safe_summarize, safe_translate, fetch_executive_orders_multi

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "urgent.json"
MAX_SEEN_IDS_KEPT = 300     # just a memory of what we've already alerted on
EO_LOOKBACK_DAYS = 3        # Federal Register dates are day-granularity, not timestamped


def load_existing():
    if DATA_PATH.exists():
        try:
            return json.loads(DATA_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"generated_at": None, "latest": None, "seen_ids": []}


def candidate_executive_orders():
    # per_page is generous because the Federal Register API's term filter is a broad
    # full-text match, not a topical one — most of what it returns gets dropped below.
    results = fetch_executive_orders_multi(per_page=20)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=EO_LOOKBACK_DAYS)).date().isoformat()
    items = []
    for r in results:
        date = r.get("signing_date") or r.get("publication_date") or ""
        if date < cutoff:
            continue
        title = r.get("title", "")
        abstract = r.get("abstract") or title
        if not is_relevant(f"{title}. {abstract}"):
            continue
        items.append({
            "id": f"eo-{r.get('document_number')}",
            "type": "executive_order",
            "title_en": title,
            "date": date,
            "url": r.get("html_url"),
            "source": "Federal Register (official)",
            "raw_text": r.get("abstract") or title,
        })
    return items


def main():
    existing = load_existing()
    seen_ids = set(existing.get("seen_ids", []))

    candidates = candidate_executive_orders()
    new_items = [c for c in candidates if c["id"] not in seen_ids]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest": existing.get("latest"),
        "seen_ids": list(seen_ids)[-MAX_SEEN_IDS_KEPT:],
    }

    if not new_items:
        print("No new urgent items.")
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    # Only summarize/translate the single most important new item to stay fast + cheap.
    top = new_items[0]
    print(f"New urgent item: {top['title_en'][:70]}")

    client = get_client()
    summary_en = safe_summarize(client, top["raw_text"])
    title_es = safe_translate(client, top["title_en"])
    summary_es = safe_translate(client, summary_en)

    latest = {
        "id": top["id"],
        "type": top["type"],
        "date": top["date"],
        "url": top["url"],
        "source": top["source"],
        "title_en": top["title_en"],
        "title_es": title_es,
        "summary_en": summary_en,
        "summary_es": summary_es,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }

    for item in new_items:
        seen_ids.add(item["id"])

    output["latest"] = latest
    output["seen_ids"] = list(seen_ids)[-MAX_SEEN_IDS_KEPT:]

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote urgent alert to {DATA_PATH}")


if __name__ == "__main__":
    main()
