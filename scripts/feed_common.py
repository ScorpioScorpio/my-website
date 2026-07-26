"""
Shared helpers for the immigration executive-order pipeline.
Used by both update_feed.py (full digest, every 6h) and check_urgent.py
(fast new-item check, every 15 min).
"""

import os
import re
import sys

import requests
from huggingface_hub import InferenceClient

SUMMARY_MODEL = "facebook/bart-large-cnn"
TRANSLATE_MODEL = "Helsinki-NLP/opus-mt-en-es"

KEYWORDS = [
    "immigra", "asylum", "refugee", "deport", "border", "visa",
    "citizenship", "ICE", "DHS", "undocumented", "migrant", "naturaliz",
    "alien",
]

# Separate Federal Register searches to run and merge, since its term filter is a
# full-text match against a single phrase — an EO about e.g. asylum policy won't
# surface from a search for "immigration" unless that word also appears in it.
EO_SEARCH_TERMS = ["immigration", "asylum", "refugee", "deportation", "ICE"]

# Word boundary required before each keyword (but not after, so stems like
# "immigra" still match "immigration"/"immigrant") — plain substring matching
# let short acronyms like "ICE" match inside unrelated words like "service".
_KEYWORD_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in KEYWORDS) + ")",
    re.IGNORECASE,
)


def get_client():
    token = os.environ.get("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return InferenceClient(token=token)


def is_relevant(text):
    return bool(_KEYWORD_PATTERN.search(text))


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


def fetch_executive_orders_multi(terms=EO_SEARCH_TERMS, per_page=20):
    """Runs fetch_executive_orders once per term and merges the results, deduped by
    document number, newest first."""
    by_doc_number = {}
    for term in terms:
        for r in fetch_executive_orders(term=term, per_page=per_page):
            by_doc_number[r.get("document_number")] = r
    results = list(by_doc_number.values())
    results.sort(key=lambda r: r.get("signing_date") or r.get("publication_date") or "", reverse=True)
    return results
