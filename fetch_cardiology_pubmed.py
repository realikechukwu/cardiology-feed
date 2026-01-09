#!/usr/bin/env python3
"""
Enhanced cardiology article fetcher with filtering and classification.
Focuses on original research and substantive content for weekly digests.
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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("⚠️  python-dotenv not installed. Install with: pip install python-dotenv", file=sys.stderr)

# Configure journals
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

# Publication types to prioritize (original research and reviews)
PRIORITY_PUB_TYPES = {
    "Clinical Trial",
    "Randomized Controlled Trial",
    "Multicenter Study",
    "Meta-Analysis",
    "Systematic Review",
    "Observational Study",
    "Comparative Study",
    "Review",
}

# Publication types to exclude (non-substantive content)
EXCLUDE_PUB_TYPES = {
    "Editorial",
    "Comment",
    "Letter",
    "News",
    "Published Erratum",
    "Retraction of Publication",
    "Retracted Publication",
}

EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL_NAME = "cardiology-research-digest"


def http_get(url: str, timeout: int = 30, headers: Optional[Dict[str, str]] = None) -> bytes:
    hdrs = {"User-Agent": f"{TOOL_NAME}/1.0"}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read()


def build_journal_query(journals: List[str]) -> str:
    parts = [f'"{j}"[jour]' for j in journals]
    return "(" + " OR ".join(parts) + ")"


def esearch_pmids(
    query: str,
    days: int,
    max_results: int,
    api_key: Optional[str],
    email: str,
    retstart: int = 0,
) -> Tuple[List[str], int]:
    end = datetime.now(timezone.utc).date()
    start = (datetime.now(timezone.utc) - timedelta(days=days)).date()

    params = {
        "db": "pubmed",
        "term": f'{query} AND ("{start}"[dp] : "{end}"[dp])',
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
    y = article.findtext(".//ArticleDate/Year")
    m = article.findtext(".//ArticleDate/Month")
    d = article.findtext(".//ArticleDate/Day")
    if y and m and d:
        return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    
    y = article.findtext(".//JournalIssue/PubDate/Year")
    m = article.findtext(".//JournalIssue/PubDate/Month")
    d = article.findtext(".//JournalIssue/PubDate/Day")
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


def classify_article(pub_types: List[str], has_abstract: bool) -> str:
    """Classify article based on publication type and content availability."""
    pub_types_set = set(pub_types)
    
    # Check for excluded types first
    if pub_types_set & EXCLUDE_PUB_TYPES:
        return "excluded"
    
    # Check for priority research types
    if pub_types_set & PRIORITY_PUB_TYPES:
        return "priority" if has_abstract else "priority_no_abstract"
    
    # Has abstract but generic type
    if has_abstract:
        return "standard"
    
    # No abstract, generic type
    return "low_priority"


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

    # Classify the article
    category = classify_article(pub_types, bool(abstract))
    
    # Extract authors (first 3)
    authors = []
    for author_elem in article.findall(".//AuthorList/Author")[:3]:
        last = author_elem.findtext("LastName") or ""
        first = author_elem.findtext("ForeName") or ""
        if last and first:
            authors.append(f"{last} {first[0]}")
        elif last:
            authors.append(last)
    
    return {
        "pmid": pmid,
        "doi": doi,
        "title": title,
        "journal": journal,
        "pub_date": pub_date,
        "abstract": abstract,
        "publication_types": pub_types,
        "category": category,
        "authors": authors,
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

        time.sleep(sleep_s)
    return results


def filter_and_categorize(articles: List[Dict[str, Any]], include_no_abstract: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """Filter and categorize articles for digest."""
    categorized = {
        "priority": [],
        "standard": [],
        "needs_review": [],
        "excluded": []
    }
    
    for article in articles:
        cat = article["category"]
        
        if cat == "excluded":
            categorized["excluded"].append(article)
        elif cat == "priority":
            categorized["priority"].append(article)
        elif cat == "priority_no_abstract":
            if include_no_abstract:
                categorized["needs_review"].append(article)
            else:
                categorized["excluded"].append(article)
        elif cat == "standard":
            categorized["standard"].append(article)
        else:  # low_priority
            if include_no_abstract:
                categorized["needs_review"].append(article)
            else:
                categorized["excluded"].append(article)
    
    return categorized


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch and filter cardiology research articles")
    ap.add_argument("--days", type=int, default=7, help="Look back this many days (default: 7)")
    ap.add_argument("--max", type=int, default=300, help="Max PMIDs to retrieve (default: 300)")
    ap.add_argument("--out", type=str, default="output/cardiology_recent.json", help="Output JSON filename")
    ap.add_argument("--include-no-abstract", action="store_true", help="Include articles without abstracts")
    ap.add_argument("--api-key", type=str, default=None, help="NCBI API key")
    ap.add_argument("--email", type=str, default=None, help="Contact email for NCBI")
    args = ap.parse_args()

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
        print("⚠️  No API key (requests will be slower)")

    output_path = Path(args.out)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    query = build_journal_query(TOP_10_JOURNALS)

    print(f"\n🔍 Searching for articles from last {args.days} days...")
    pmids, count = esearch_pmids(
        query=query,
        days=args.days,
        max_results=args.max,
        api_key=api_key,
        email=email,
    )
    
    if not pmids:
        print("No PMIDs found.")
        return 0

    print(f"✓ Found {len(pmids)} articles (total available: {count})")

    print(f"📥 Fetching article details...")
    articles = efetch_details(pmids, api_key=api_key, email=email)

    # Filter and categorize
    categorized = filter_and_categorize(articles, args.include_no_abstract)
    
    # Print statistics
    print(f"\n📊 Article Classification:")
    print(f"  Priority Research: {len(categorized['priority'])}")
    print(f"  Standard Articles: {len(categorized['standard'])}")
    print(f"  Needs Review: {len(categorized['needs_review'])}")
    print(f"  Excluded (editorials/letters): {len(categorized['excluded'])}")

    # Prepare digest (priority + standard)
    digest_articles = categorized["priority"] + categorized["standard"]
    
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": args.days,
        "journals": TOP_10_JOURNALS,
        "total_fetched": len(articles),
        "digest_count": len(digest_articles),
        "statistics": {
            "priority": len(categorized["priority"]),
            "standard": len(categorized["standard"]),
            "needs_review": len(categorized["needs_review"]),
            "excluded": len(categorized["excluded"])
        },
        "articles": digest_articles,
        "excluded_articles": categorized["excluded"] if args.include_no_abstract else []
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Saved {len(digest_articles)} digestible articles to {args.out}")
    
    # Show sample of priority articles
    if categorized["priority"]:
        print(f"\n🌟 Sample Priority Research:")
        for article in categorized["priority"][:3]:
            print(f"  • {article['title'][:80]}...")
            print(f"    {article['journal']} | {', '.join(article['publication_types'])}")
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())