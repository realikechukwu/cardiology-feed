#!/usr/bin/env python3
"""
Fetch the most recent papers from a list of cardiology journals via PubMed E-utilities,
then save a JSON file containing PMID, title, journal, pub date, and abstract (when available).

Usage:
  python fetch_cardiology_pubmed.py --days 7 --max 200 --out output/cardiology_recent.json

Notes:
- Reads NCBI_EMAIL and NCBI_API_KEY from .env file
- PubMed query uses journal field [jour] + date of publication [dp].
- Abstracts may be missing for some records.
- This script is polite to NCBI by batching efetch requests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv not installed. Install with: pip install python-dotenv", file=sys.stderr)
    print("    Or set environment variables manually.", file=sys.stderr)


# -------------------------
# Configure your top journals
# -------------------------
TOP_10_JOURNALS = [
    "Circulation",
    "Journal of the American College of Cardiology",
    "European Heart Journal",
    "JAMA Cardiology",
    "Circulation: Heart Failure",
    "Circulation: Arrhythmia and Electrophysiology",
    "Heart",
    "American Heart Journal",
    "JACC: Heart Failure",
    "JACC: Cardiovascular Imaging",
]

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL_NAME = "local-pubmed-cardiology-fetch"


def http_get(url: str, timeout: int = 30, headers: Optional[Dict[str, str]] = None) -> bytes:
    hdrs = {"User-Agent": f"{TOOL_NAME}/1.0"}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def build_journal_query(journals: List[str]) -> str:
    # Quote journal names; query uses [jour] field.
    parts = [f"\"{j}\"[jour]" for j in journals]
    return "(" + " OR ".join(parts) + ")"


def esearch_pmids(
    query: str,
    days: int,
    max_results: int,
    api_key: Optional[str],
    email: str,
    retstart: int = 0,
) -> Tuple[List[str], int]:
    # Use mindate/maxdate with datetype=pdat for robustness
    end = datetime.now(timezone.utc).date()
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    params = {
        "db": "pubmed",
        "term": f"{query} AND (\"{start}\"[dp] : \"{end}\"[dp])",
        "retmode": "xml",
        "retmax": str(max_results),
        "retstart": str(retstart),
        "sort": "pub+date",
        "tool": TOOL_NAME,
        "email": email,
    }
    if api_key:
        params["api_key"] = api_key

    url = EUTILS_BASE + "esearch.fcgi?" + urlencode(params)
    xml_bytes = http_get(url)
    root = ET.fromstring(xml_bytes)

    count_text = root.findtext("Count") or "0"
    count = int(count_text)

    pmids = [elem.text for elem in root.findall("./IdList/Id") if elem.text]
    return pmids, count


def chunked(lst: List[str], n: int) -> List[List[str]]:
    return [lst[i : i + n] for i in range(0, len(lst), n)]


def _text(elem: Optional[ET.Element]) -> str:
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def parse_pubdate(article: ET.Element) -> str:
    # Try ArticleDate (electronic) then PubDate.
    y = article.findtext(".//ArticleDate/Year")
    m = article.findtext(".//ArticleDate/Month")
    d = article.findtext(".//ArticleDate/Day")
    if y and m and d:
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    # Fallback: Journal PubDate
    y = article.findtext(".//JournalIssue/PubDate/Year")
    m = article.findtext(".//JournalIssue/PubDate/Month")
    d = article.findtext(".//JournalIssue/PubDate/Day")
    # Month might be "Jan" etc.
    if y and m and d:
        mm = month_to_number(m)
        return f"{y}-{mm.zfill(2)}-{d.zfill(2)}"
    if y and m:
        mm = month_to_number(m)
        return f"{y}-{mm.zfill(2)}"
    if y:
        return y
    medline_date = article.findtext(".//JournalIssue/PubDate/MedlineDate")
    return medline_date or ""


def month_to_number(m: str) -> str:
    m = m.strip()
    if m.isdigit():
        return m
    mapping = {
        "Jan": "1", "Feb": "2", "Mar": "3", "Apr": "4", "May": "5", "Jun": "6",
        "Jul": "7", "Aug": "8", "Sep": "9", "Oct": "10", "Nov": "11", "Dec": "12",
    }
    return mapping.get(m[:3], "0")


def parse_abstract(article: ET.Element) -> str:
    # AbstractText can have multiple sections; concatenate with headings when present.
    abs_elems = article.findall(".//Abstract/AbstractText")
    if not abs_elems:
        return ""
    chunks: List[str] = []
    for a in abs_elems:
        label = a.attrib.get("Label") or a.attrib.get("NlmCategory") or ""
        txt = _text(a)
        if not txt:
            continue
        if label:
            chunks.append(f"{label}: {txt}")
        else:
            chunks.append(txt)
    return "\n".join(chunks).strip()


def parse_article(article: ET.Element) -> Dict[str, Any]:
    pmid = article.findtext(".//PMID") or ""
    title = _text(article.find(".//ArticleTitle"))
    journal = _text(article.find(".//Journal/Title")) or _text(article.find(".//MedlineJournalInfo/MedlineTA"))
    pub_date = parse_pubdate(article)
    abstract = parse_abstract(article)

    doi = ""
    for id_elem in article.findall(".//ArticleIdList/ArticleId"):
        if id_elem.attrib.get("IdType") == "doi" and id_elem.text:
            doi = id_elem.text.strip()
            break

    url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
    
    # Extract publication types
    pub_types = []
    for pt_elem in article.findall(".//PublicationTypeList/PublicationType"):
        if pt_elem.text:
            pub_types.append(pt_elem.text.strip())

    return {
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "journal": journal,
        "pub_date": pub_date,
        "abstract": abstract,
        "publication_types": pub_types,
        "url": url,
    }


def efetch_details(
    pmids: List[str],
    api_key: Optional[str],
    email: str,
    batch_size: int = 100,
    sleep_s: float = 0.34,
) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []

    for batch in chunked(pmids, batch_size):
        params = {
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
            "tool": TOOL_NAME,
            "email": email,
        }
        if api_key:
            params["api_key"] = api_key

        url = EUTILS_BASE + "efetch.fcgi?" + urlencode(params)
        xml_bytes = http_get(url)
        root = ET.fromstring(xml_bytes)

        for article in root.findall(".//PubmedArticle"):
            results.append(parse_article(article))

        time.sleep(sleep_s)  # be polite to NCBI
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Look back this many days (default: 7)")
    ap.add_argument("--max", type=int, default=200, help="Max number of PMIDs to retrieve (default: 200)")
    ap.add_argument("--out", type=str, default="output/cardiology_recent.json", help="Output JSON filename")
    ap.add_argument("--api-key", type=str, default=None, help="NCBI API key (overrides env var)")
    ap.add_argument("--email", type=str, default=None, help="Contact email for NCBI (overrides env var)")
    args = ap.parse_args()

    # Get email and API key from environment variables or command line
    email = args.email or os.getenv("NCBI_EMAIL")
    api_key = args.api_key or os.getenv("NCBI_API_KEY")

    if not email:
        print("❌ ERROR: No email provided!", file=sys.stderr)
        print("   Set NCBI_EMAIL in .env file or use --email flag", file=sys.stderr)
        return 1

    print(f"✓ Using email: {email}")
    if api_key:
        print(f"✓ API key found (length: {len(api_key)})")
    else:
        print("⚠️  No API key provided (requests will be slower)")

    # Create output directory if it doesn't exist
    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    query = build_journal_query(TOP_10_JOURNALS)

    # Pull PMIDs (up to max)
    print(f"\n🔍 Searching for articles from last {args.days} days...")
    pmids, count = esearch_pmids(
        query=query,
        days=args.days,
        max_results=args.max,
        api_key=api_key,
        email=email,
    )
    
    if not pmids:
        print("No PMIDs found for your query/time window.")
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "days": args.days,
                    "journals": TOP_10_JOURNALS,
                    "count": 0,
                    "articles": [],
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        return 0

    print(f"✓ Found {len(pmids)} articles (total available: {count})")

    # Fetch details (title/abstract/etc.)
    print(f"📥 Fetching article details...")
    articles = efetch_details(pmids, api_key=api_key, email=email)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "journals": TOP_10_JOURNALS,
        "pmid_count": len(pmids),
        "total_available": count,
        "articles": articles,
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"✅ Saved {len(articles)} articles to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())