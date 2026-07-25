"""
Shared helpers for the immigration news pipeline.
Used by both update_feed.py (full digest, every 6h) and check_urgent.py
(fast new-item check, every 15 min).
"""

import hashlib
import os
import sys

import requests
from huggingface_hub import InferenceClient

SUMMARY_MODEL = "facebook/bart-large-cnn"
TRANSLATE_MODEL = "Helsinki-NLP/opus-mt-en-es"

KEYWORDS = [
    "immigra", "asylum", "refugee", "deport", "border", "visa",
    "citizenship", "ICE", "DHS", "undocumented", "migrant", "naturaliz",
]


def get_client():
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return InferenceClient(token=token)


def is_relevant(text):
    text_lower = text.lower()
    return any(k.lower() in text_lower for k in KEYWORDS)


def stable_id(text):
    """Deterministic id for an entry, unlike builtin hash() which is randomized per process."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def safe_summarize(client, text, max_length=90, min_length=25):
    text = text.strip()
    if len(text) < 200:
        return text
    try:
        result = client.summarization(
            text[:3000],
            model=SUMMARY_MODEL,
            parameters={"max_length": max_length, "min_length": min_length},
        )
        return result.summary_text if hasattr(result, "summary_text") else result["summary_text"]
    except Exception as e:
        print(f"  [warn] summarization failed, using truncated text: {e}", file=sys.stderr)
        return (text[:280] + "…") if len(text) > 280 else text


def safe_translate(client, text):
    try:
        result = client.translation(text, model=TRANSLATE_MODEL)
        return result.translation_text if hasattr(result, "translation_text") else result["translation_text"]
    except Exception as e:
        print(f"  [warn] translation failed, keeping English: {e}", file=sys.stderr)
        return text


def fetch_executive_orders(term="immigration", per_page=20):
    """Returns raw Federal Register results, newest first. Callers filter by date/id themselves."""
    url = "https://www.federalregister.gov/api/v1/documents.json"
    params = {
        "conditions[type][]": "PRESDOCU",
        "conditions[presidential_document_type]": "executive_order",
        "conditions[term]": term,
        "order": "newest",
        "per_page": per_page,
        "fields[]": [
            "title", "abstract", "html_url", "publication_date",
            "signing_date", "executive_order_number", "document_number",
        ],
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        print(f"  [error] Federal Register fetch failed: {e}", file=sys.stderr)
        return []
