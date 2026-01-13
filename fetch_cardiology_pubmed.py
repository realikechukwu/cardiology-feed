#!/usr/bin/env python3
"""
Enhanced cardiology article fetcher with filtering, classification, dedupe state,
and date-stamped outputs for weekly digests.

- Fetches PubMed articles from configured journals in the last N days
- Extracts abstracts, publication types, authors
- Classifies and filters to produce a "digest" set
- Dedupes across runs using a local state file of seen PMIDs
- Writes:
  (1) a date-stamped output JSON (archive)
  (2) a stable "latest" output JSON at --out (overwrite)
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


# -------------------------
# Specialty Config Loader
# -------------------------
def load_specialty_config(specialty: str) -> Dict[str, Any]:
    """Load specialty configuration from specialties/{specialty}.json"""
    config_path = Path(__file__).parent / "specialties" / f"{specialty}.json"
    if not config_path.exists():
        raise FileNotFoundError(f"Specialty config not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_config_value(config: Dict[str, Any], key: str, default: Any = None) -> Any:
    """Get a config value with optional default."""
    return config.get(key, default)

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


def build_general_journal_cardiology_query(journals: List[str], mesh_terms: List[str], title_keywords: List[str]) -> str:
    """Build query for general journals filtered by cardiology MeSH terms or title keywords."""
    journal_part = "(" + " OR ".join([f'"{j}"[jour]' for j in journals]) + ")"
    mesh_part = "(" + " OR ".join([f'"{m}"[MeSH]' for m in mesh_terms]) + ")"
    title_part = "(" + " OR ".join([f'{k}[ti]' for k in title_keywords]) + ")"
    cardiology_filter = f"({mesh_part} OR {title_part})"
    return f"({journal_part} AND {cardiology_filter})"


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
    return [lst[i: i + n] for i in range(0, len(lst), n)]


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


# -------------------------
# NEW: Dedupe state helpers
# -------------------------
def load_seen_pmids(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pmids = data.get("seen_pmids", [])
        if not isinstance(pmids, list):
            return set()
        return {str(p).strip() for p in pmids if str(p).strip()}
    except Exception:
        # If state file is corrupted, fail safe by not deduping (but do not crash).
        return set()


def save_seen_pmids(state_path: Path, seen_pmids: set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "seen_pmids": sorted(seen_pmids),
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def dedupe_articles_by_pmid(articles: List[Dict[str, Any]], seen_pmids: set[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Return only articles whose PMID is not in seen_pmids. Also returns count removed."""
    new_articles: List[Dict[str, Any]] = []
    removed = 0
    for a in articles:
        pmid = (a.get("pmid") or "").strip()
        if not pmid:
            # If no PMID, keep it (rare) - but it won't be tracked in state
            new_articles.append(a)
            continue
        if pmid in seen_pmids:
            removed += 1
            continue
        new_articles.append(a)
    return new_articles, removed


def make_dated_output_path(base_out: Path, run_date: str) -> Path:
    """If base_out is output/foo.json -> output/foo_YYYY-MM-DD.json"""
    suffix = base_out.suffix if base_out.suffix else ".json"
    stem = base_out.stem if base_out.stem else "digest"
    return base_out.with_name(f"{stem}_{run_date}{suffix}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch and filter research articles by specialty")
    ap.add_argument("--specialty", type=str, default="cardiology",
                    help="Specialty to fetch (default: cardiology). Loads config from specialties/{specialty}.json")
    ap.add_argument("--days", type=int, default=7, help="Look back this many days (default: 7)")
    ap.add_argument("--max", type=int, default=300, help="Max PMIDs to retrieve (default: 300)")
    ap.add_argument("--out", type=str, default=None,
                    help="Stable output JSON filename. Defaults to output/{specialty}_recent.json")
    ap.add_argument("--include-no-abstract", action="store_true", help="Include articles without abstracts")
    ap.add_argument("--api-key", type=str, default=None, help="NCBI API key")
    ap.add_argument("--email", type=str, default=None, help="Contact email for NCBI")
    ap.add_argument("--state", type=str, default=None,
                    help="Path to dedupe state file. Defaults to state/{specialty}_seen_pmids.json")
    ap.add_argument("--no-dedupe", action="store_true",
                    help="Disable dedupe (always include items even if seen before)")
    ap.add_argument("--test-mode", action="store_true",
                    help="Test mode: skip all state file reading/writing (ignores seen_pmids.json entirely)")
    args = ap.parse_args()

    # Load specialty config
    try:
        config = load_specialty_config(args.specialty)
        print(f"📋 Specialty: {config.get('name', args.specialty)}")
    except FileNotFoundError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    # Set defaults based on specialty (backward compatible with cardiology)
    if args.specialty == "cardiology":
        default_out = "output/cardiology_recent.json"
        default_state = "state/seen_pmids.json"
    else:
        default_out = f"output/{args.specialty}_recent.json"
        default_state = f"state/{args.specialty}_seen_pmids.json"

    output_file = args.out or default_out
    state_file = args.state or default_state

    # Extract config values
    specialty_journals = config.get("specialty_journals", [])
    general_journals = config.get("general_journals", [])
    mesh_terms = config.get("mesh_terms", [])
    title_keywords = config.get("title_keywords", [])

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

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Query 1: Specialty-specific journals (all articles)
    specialty_query = build_journal_query(specialty_journals) if specialty_journals else None

    # Query 2: General journals with specialty filter
    general_query = None
    if general_journals and (mesh_terms or title_keywords):
        general_query = build_general_journal_cardiology_query(
            general_journals, mesh_terms, title_keywords
        )

    print(f"\n🔍 Searching for articles from last {args.days} days...")

    specialty_pmids = []
    specialty_count = 0
    general_pmids = []
    general_count = 0

    # Search specialty journals
    if specialty_query:
        print("  📚 Specialty journals...")
        specialty_pmids, specialty_count = esearch_pmids(
            query=specialty_query,
            days=args.days,
            max_results=args.max,
            api_key=api_key,
            email=email,
        )
        print(f"     Found {len(specialty_pmids)} articles (total available: {specialty_count})")

    # Search general journals with specialty filter
    if general_query:
        print("  🌐 General journals (specialty-filtered)...")
        general_pmids, general_count = esearch_pmids(
            query=general_query,
            days=args.days,
            max_results=args.max,
            api_key=api_key,
            email=email,
        )
        print(f"     Found {len(general_pmids)} articles (total available: {general_count})")

    # Merge PMIDs (avoid duplicates)
    all_pmids_set = set(specialty_pmids) | set(general_pmids)
    pmids = list(all_pmids_set)
    count = specialty_count + general_count

    if not pmids:
        print("No PMIDs found.")
        return 0

    print(f"✓ Total: {len(pmids)} unique articles")
    print("📥 Fetching article details...")
    articles = efetch_details(pmids, api_key=api_key, email=email)

    # Filter and categorize
    categorized = filter_and_categorize(articles, args.include_no_abstract)

    # Prepare digest (priority + standard)
    digest_articles = categorized["priority"] + categorized["standard"]

    # Dedupe across runs
    state_path = Path(state_file)
    skip_state = args.test_mode or args.no_dedupe
    seen_pmids = load_seen_pmids(state_path) if not skip_state else set()
    deduped_digest, removed_dupes = dedupe_articles_by_pmid(digest_articles, seen_pmids)

    # Update state with new PMIDs actually included (skip in test mode)
    if not skip_state:
        for a in deduped_digest:
            pmid = (a.get("pmid") or "").strip()
            if pmid:
                seen_pmids.add(pmid)
        save_seen_pmids(state_path, seen_pmids)

    # Print statistics
    print(f"\n📊 Article Classification (pre-dedupe):")
    print(f"  Priority Research: {len(categorized['priority'])}")
    print(f"  Standard Articles: {len(categorized['standard'])}")
    print(f"  Needs Review: {len(categorized['needs_review'])}")
    print(f"  Excluded (editorials/letters): {len(categorized['excluded'])}")

    if args.test_mode:
        print("\n🧪 Test mode: state file reading/writing disabled")
    elif args.no_dedupe:
        print("\n🟡 Dedupe: disabled")
    else:
        print(f"\n🧹 Dedupe: removed {removed_dupes} previously-seen articles")
        print(f"  State file: {state_path} (total seen: {len(seen_pmids)})")

    run_dt = datetime.now(timezone.utc)
    run_date = run_dt.date().isoformat()
    run_ts = run_dt.strftime("%Y-%m-%dT%H%M%SZ")

    dated_output_path = make_dated_output_path(output_path, run_ts)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_date": run_date,
        "days": args.days,
        "journals": {
            "specialty_specific": config["specialty_journals"],
            "general_filtered": config["general_journals"],
        },
        "total_fetched": len(articles),
        "digest_count_pre_dedupe": len(digest_articles),
        "digest_count": len(deduped_digest),
        "dedupe": {
            "enabled": (not args.no_dedupe),
            "state_path": str(state_path),
            "previously_seen_removed": removed_dupes,
        },
        "statistics": {
            "priority": len(categorized["priority"]),
            "standard": len(categorized["standard"]),
            "needs_review": len(categorized["needs_review"]),
            "excluded": len(categorized["excluded"]),
        },
        "articles": deduped_digest,
        "excluded_articles": categorized["excluded"] if args.include_no_abstract else [],
    }

    # Write date-stamped archive
    with open(dated_output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Write stable "latest" output (same content, overwritten each run)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Saved {len(deduped_digest)} new digestible articles")
    print(f"   Archive: {dated_output_path}")
    print(f"   Latest:  {output_path}")

    # Show sample of priority articles
    if categorized["priority"]:
        print(f"\n🌟 Sample Priority Research:")
        for article in categorized["priority"][:3]:
            print(f"  • {article['title'][:80]}...")
            pts = ", ".join(article.get("publication_types", []))
            print(f"    {article['journal']} | {pts}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())