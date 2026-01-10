#!/usr/bin/env python3
"""
Summarise latest cardiology digest JSON with OpenAI and email as HTML via Gmail SMTP.

- Reads the stable latest JSON produced by fetch_cardiology_pubmed.py
- Prioritises RCTs for hero summaries
- Selects top items for LLM summaries
- Renders HTML "hero card" email
- Sends via Gmail SMTP (App Password recommended)
- Updates state/sent_pmids.json ONLY after successful email send
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import ssl
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from openai import OpenAI


# ----------------------------
# Config + small helper utils
# ----------------------------
def read_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_sent_pmids(state_path: Path) -> set[str]:
    if not state_path.exists():
        return set()
    try:
        data = read_json(state_path)
        pmids = data.get("sent_pmids", [])
        if not isinstance(pmids, list):
            return set()
        return {str(p).strip() for p in pmids if str(p).strip()}
    except Exception:
        return set()


def save_sent_pmids(state_path: Path, sent_pmids: set[str]) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sent_pmids": sorted(sent_pmids),
    }
    write_json(state_path, payload)


def strip_control_chars(s: str) -> str:
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", s)


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


@dataclass
class Article:
    pmid: str
    title: str
    journal: str
    pub_date: str
    url: str
    abstract: str
    publication_types: List[str]
    category: str
    authors: List[str]


def parse_articles(latest_payload: Dict[str, Any]) -> List[Article]:
    items = latest_payload.get("articles", [])
    out: List[Article] = []
    for a in items:
        out.append(Article(
            pmid=str(a.get("pmid", "")).strip(),
            title=str(a.get("title", "")).strip(),
            journal=str(a.get("journal", "")).strip(),
            pub_date=str(a.get("pub_date", "")).strip(),
            url=str(a.get("url", "")).strip(),
            abstract=str(a.get("abstract", "")).strip(),
            publication_types=list(a.get("publication_types", []) or []),
            category=str(a.get("category", "")).strip(),
            authors=list(a.get("authors", []) or []),
        ))
    return out


# ----------------------------
# Study type detection + selection
# ----------------------------
PRIORITY_STUDY_TYPES = {
    "randomized controlled trial",
    "randomised controlled trial",
    "clinical trial",
    "meta-analysis",
    "systematic review",
    "multicenter study",
    "observational study",
    "cohort study",
}

RCT_TERMS = {
    "randomized controlled trial",
    "randomised controlled trial",
}


def is_rct(a: Article) -> bool:
    """Check if article is specifically a randomised controlled trial."""
    pub_types_lower = {pt.lower().strip() for pt in a.publication_types}

    # Check publication types for RCT
    if pub_types_lower & RCT_TERMS:
        return True

    # Fallback: check title/abstract for RCT indicators
    text_lower = (a.title + " " + a.abstract).lower()
    rct_phrases = ["randomized controlled", "randomised controlled", "randomly assigned", "random assignment"]
    return any(phrase in text_lower for phrase in rct_phrases)


def is_priority_study(a: Article) -> bool:
    """Check if article is a high-priority study type (RCT, meta-analysis, systematic review, large cohort)."""
    pub_types_lower = {pt.lower().strip() for pt in a.publication_types}

    # Check publication types
    if pub_types_lower & PRIORITY_STUDY_TYPES:
        return True

    # Fallback: check title/abstract for priority study indicators
    text_lower = (a.title + " " + a.abstract).lower()
    priority_phrases = [
        "randomized", "randomised", "meta-analysis", "meta analysis",
        "systematic review", "cohort study", "multicenter", "multicentre",
        "registry", "nationwide", "population-based"
    ]
    return any(phrase in text_lower for phrase in priority_phrases)


def select_for_summary(
    articles: List[Article],
    max_summaries: int,
    min_abstract_chars: int = 200,
) -> Tuple[List[Article], List[Article]]:
    """
    Returns (to_summarise, headlines_only).

    Priority order:
    1. Priority studies (RCTs, meta-analyses, systematic reviews, large cohorts) with abstracts
    2. Other priority category articles with abstracts
    3. Standard articles with abstracts
    """
    priority_studies = [a for a in articles if is_priority_study(a)]
    non_priority_studies = [a for a in articles if not is_priority_study(a)]

    other_priority = [a for a in non_priority_studies if a.category == "priority"]
    standard = [a for a in non_priority_studies if a.category == "standard"]

    # Order: priority studies first, then other priority, then standard
    ordered = priority_studies + other_priority + standard

    # Filter for summarisation eligibility (must have abstract)
    eligible = [a for a in ordered if len(a.abstract) >= min_abstract_chars]
    to_sum = eligible[:max_summaries]

    to_sum_pmids = {a.pmid for a in to_sum}
    headlines = [a for a in ordered if a.pmid not in to_sum_pmids]

    return to_sum, headlines


# ----------------------------
# OpenAI summarisation
# ----------------------------
SUMMARY_SCHEMA: Dict[str, Any] = {
    "name": "editorial_note",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "study_type": {"type": "string"},
            "finding": {"type": "string"},
            "so_what": {"type": "string"},
        },
        "required": ["study_type", "finding", "so_what"],
    },
    "strict": True,
}


def normalize_study_type(study_type: str) -> str:
    """Normalize study type to sentence case for consistent formatting."""
    if not study_type:
        return study_type
    # Handle common acronyms that should stay uppercase
    acronyms = {"rct": "RCT", "rcts": "RCTs"}
    words = study_type.lower().split()
    result = []
    for i, word in enumerate(words):
        if word in acronyms:
            result.append(acronyms[word])
        elif i == 0:
            result.append(word.capitalize())
        else:
            result.append(word)
    return " ".join(result)


def summarise_one(client: OpenAI, model: str, a: Article) -> Dict[str, Any]:
    """
    Uses OpenAI Chat Completions API with strict JSON schema output.
    """
    system = (
        "You are writing a brief editorial note for a cardiology digest. "
        "Return JSON with exactly three fields:\n"
        "- study_type: Classify the design using one of these exact formats: "
        "'RCT', 'Meta-analysis', 'Systematic review', 'Prospective cohort', "
        "'Retrospective cohort', 'Case-control', 'Case series', 'Narrative review', "
        "'Guideline', or 'Other'. Use sentence case (e.g., 'Meta-analysis' not 'META-ANALYSIS').\n"
        "- finding: The primary result or conclusion. For trials and observational "
        "studies, include effect size, CI, and p-value if reported. For reviews, "
        "state the main synthesis or conclusion.\n"
        "- so_what: One sentence on why a clinician should care. What does this "
        "change, confirm, or challenge in practice?\n\n"
        "If a detail is not in the abstract, write 'Not reported'. "
        "Be precise. No hype words like 'breakthrough' or 'game-changing'. "
        "Use only information from the provided abstract."
    )

    user = f"""TITLE: {a.title}
JOURNAL: {a.journal}
PUB DATE: {a.pub_date}
PUBLICATION TYPES: {", ".join(a.publication_types) if a.publication_types else "Not specified"}
ABSTRACT:
{a.abstract}
"""

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": SUMMARY_SCHEMA,
        },
        temperature=0.2,
    )

    content = completion.choices[0].message.content
    if not content:
        raise ValueError("Empty response from OpenAI")

    return json.loads(content)


# ----------------------------
# HTML rendering
# ----------------------------
def hero_card_html(a: Article, s: Dict[str, Any]) -> str:
    """Minimal three-field card with RCT badge only for actual RCTs."""
    title = html_escape(strip_control_chars(a.title))
    journal = html_escape(a.journal)
    pub_date = html_escape(a.pub_date)
    url = html_escape(a.url)
    authors = html_escape(", ".join(a.authors)) if a.authors else ""

    # Badge only for actual RCTs (not all priority studies)
    rct_badge = ""
    if is_rct(a):
        rct_badge = (
            '<span style="display:inline-block; padding:3px 10px; '
            'background:#e8f5e9; color:#2e7d32; font-size:10px; '
            'font-weight:600; border-radius:4px; margin-left:10px; '
            'vertical-align:middle;">RCT</span>'
        )

    # Normalize study type to consistent formatting
    raw_study_type = s.get("study_type", "")
    study_type = html_escape(strip_control_chars(normalize_study_type(raw_study_type)))
    finding = html_escape(strip_control_chars(s.get("finding", "")))
    so_what = html_escape(strip_control_chars(s.get("so_what", "")))

    # Build meta line: journal · date · authors
    meta_parts = [p for p in [journal, pub_date, authors] if p]
    meta_line = " · ".join(meta_parts)

    return f"""
    <div style="border:1px solid #e0e0e0; border-radius:8px; padding:24px; margin:16px 0; background:#ffffff;">
      <div style="font-size:17px; font-weight:600; line-height:1.4; margin-bottom:6px;">
        <a href="{url}" style="color:#1a1a1a; text-decoration:none;">{title}</a>{rct_badge}
      </div>
      <div style="font-size:12px; color:#888; margin-bottom:20px;">
        {meta_line}
      </div>

      <div style="margin-bottom:16px;">
        <div style="font-size:11px; color:#888; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:5px;">Study Type</div>
        <div style="font-size:14px; line-height:1.5; color:#333;">{study_type}</div>
      </div>

      <div style="margin-bottom:16px;">
        <div style="font-size:11px; color:#888; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:5px;">Finding</div>
        <div style="font-size:14px; line-height:1.5; color:#333;">{finding}</div>
      </div>

      <div style="background:#f9f9f9; padding:14px; border-radius:6px; border-left:3px solid #666;">
        <div style="font-size:11px; color:#888; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:5px;">So What?</div>
        <div style="font-size:14px; line-height:1.5; color:#1a1a1a; font-weight:500;">{so_what}</div>
      </div>
    </div>
    """


def headlines_html(items: List[Article]) -> str:
    """Headlines section for non-summarised articles."""
    if not items:
        return "<div style='color:#888; font-size:14px; padding:8px 0;'>No additional headlines this week.</div>"

    lis = []
    for a in items:
        title = html_escape(strip_control_chars(a.title))
        journal = html_escape(a.journal)
        pub_date = html_escape(a.pub_date)
        url = html_escape(a.url)
        authors = html_escape(", ".join(a.authors)) if a.authors else ""

        rct_badge = ""
        if is_rct(a):
            rct_badge = (
                '<span style="display:inline-block; padding:2px 6px; '
                'background:#e8f5e9; color:#2e7d32; font-size:9px; '
                'font-weight:600; border-radius:3px; margin-left:6px;">RCT</span>'
            )

        # Build meta line: journal · date · authors
        meta_parts = [p for p in [journal, pub_date, authors] if p]
        meta_line = " · ".join(meta_parts)

        lis.append(f"""
            <li style='margin:10px 0; padding:10px 0; border-bottom:1px solid #f0f0f0; line-height:1.5;'>
                <a href='{url}' style='color:#2c2c2c; text-decoration:none; font-size:14px;'>{title}</a>{rct_badge}
                <div style='color:#888; font-size:12px; margin-top:4px;'>{meta_line}</div>
            </li>
        """)
    return "<ul style='list-style:none; padding:0; margin:0;'>" + "".join(lis) + "</ul>"


def format_human_date(iso_date: str) -> str:
    """Convert ISO datetime string to human-readable format."""
    try:
        dt = datetime.fromisoformat(iso_date.replace("Z", "+00:00"))
        return dt.strftime("%B %d, %Y")
    except Exception:
        return iso_date


def build_email_html(
    subject: str,
    generated_at: str,
    summary_cards: str,
    headlines_block: str,
    total_articles: int,
    featured_count: int,
    rct_count: int,
) -> str:
    """Email template."""
    human_date = format_human_date(generated_at)

    rct_note = ""
    if rct_count > 0:
        rct_note = f" · {rct_count} RCT{'s' if rct_count != 1 else ''}"

    return f"""\
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>{html_escape(subject)}</title>
  </head>
  <body style="margin:0; padding:0; background:#f5f5f5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">
    <div style="max-width:680px; margin:0 auto; padding:24px 16px;">

      <!-- Header -->
      <div style="background:#ffffff; border:1px solid #e0e0e0; border-radius:8px; padding:24px; margin-bottom:20px;">
        <div style="font-size:24px; font-weight:700; margin-bottom:6px; color:#1a1a1a;">
          Weekly Cardiology Digest
        </div>
        <div style="color:#666; font-size:13px;">
          {html_escape(human_date)} · {total_articles} articles · {featured_count} featured{rct_note}
        </div>
      </div>

      <!-- Featured Studies -->
      <div style="margin-bottom:20px;">
        <div style="font-size:18px; font-weight:600; margin-bottom:12px; color:#1a1a1a; padding-left:2px;">
          Featured Studies
        </div>
        {summary_cards if summary_cards else "<div style='color:#888; font-size:14px; padding:16px; background:#fff; border:1px solid #e0e0e0; border-radius:8px;'>No featured studies this week.</div>"}
      </div>

      <!-- Headlines -->
      <div style="background:#ffffff; border:1px solid #e0e0e0; border-radius:8px; padding:20px; margin-bottom:20px;">
        <div style="font-size:16px; font-weight:600; margin-bottom:12px; color:#1a1a1a;">
          Other Papers
        </div>
        {headlines_block}
      </div>

      <!-- Footer -->
      <div style="color:#999; font-size:11px; line-height:1.5; text-align:center; padding:16px;">
        Summaries automatically generated from abstracts. Refer to original publications for full details.
      </div>
    </div>
  </body>
</html>
"""


# ----------------------------
# Gmail SMTP sending
# ----------------------------
def send_gmail_html(
    smtp_user: str,
    smtp_app_password: str,
    to_addrs: list[str],
    from_addr: str,
    subject: str,
    html_body: str,
) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    # Primary recipient is the sender, others are BCC'd (not visible in headers)
    msg["To"] = from_addr

    msg.attach(MIMEText("Your email client does not support HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # All recipients receive the email, but only sender is visible in To:
    all_recipients = list(set([from_addr] + to_addrs))

    context = ssl.create_default_context()

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(smtp_user, smtp_app_password)
        server.sendmail(from_addr, all_recipients, msg.as_string())


# ----------------------------
# Main
# ----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Summarise latest digest JSON and send as HTML email")
    ap.add_argument("--latest-json", type=str, default=os.getenv("LATEST_JSON", "output/cardiology_recent.json"))
    ap.add_argument("--sent-state", type=str, default=os.getenv("SENT_STATE", "state/sent_pmids.json"))
    ap.add_argument("--max-summaries", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true", help="Do not send email; write HTML preview to output/email_preview.html")
    ap.add_argument("--subject", type=str, default=None)
    ap.add_argument("--test-mode", action="store_true",
                    help="Test mode: skip sent_pmids.json reading/writing")
    args = ap.parse_args()

    latest_path = Path(args.latest_json)
    sent_state_path = Path(args.sent_state)

    if not latest_path.exists():
        print(f"❌ Latest JSON not found: {latest_path}", file=sys.stderr)
        return 1

    payload = read_json(latest_path)
    generated_at = str(payload.get("generated_at", datetime.now(timezone.utc).isoformat()))
    run_date = str(payload.get("run_date", ""))

    articles = parse_articles(payload)

    if not articles:
        print("ℹ️ No articles in latest digest JSON. Nothing to summarise.")
        return 0

    # Filter already-sent articles (skip in test mode)
    if args.test_mode:
        sent_pmids = set()
        unsent = [a for a in articles if a.pmid]
        print("🧪 Test mode: ignoring sent_pmids.json, processing all articles")
    else:
        sent_pmids = load_sent_pmids(sent_state_path)
        unsent = [a for a in articles if a.pmid and a.pmid not in sent_pmids]

    if not unsent:
        print("ℹ️ All items in latest JSON are already marked as sent. Nothing to email.")
        return 0

    # Count RCTs for reporting
    rct_count = sum(1 for a in unsent if is_rct(a))

    to_sum, headlines_only = select_for_summary(unsent, max_summaries=args.max_summaries)

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        print("❌ OPENAI_API_KEY missing in environment/.env", file=sys.stderr)
        return 1

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    print(f"📡 Using model: {model}")

    client = OpenAI(api_key=openai_key)

    summaries: List[Tuple[Article, Dict[str, Any]]] = []
    for a in to_sum:
        try:
            print(f"  Summarising: {a.pmid} — {a.title[:60]}...")
            s = summarise_one(client, model, a)
            summaries.append((a, s))
        except Exception as e:
            print(f"⚠️ Summary failed for PMID {a.pmid}: {e}", file=sys.stderr)

    if not summaries and not headlines_only:
        print("⚠️ No summaries generated and no headlines. Skipping email.")
        return 0

    # Build HTML
    # Format date as "Jan 10, 2026"
    subject_date = format_human_date(generated_at).replace(" 0", " ").lstrip("0")  # Remove leading zeros
    try:
        from datetime import datetime as dt
        parsed = dt.fromisoformat(generated_at.replace("Z", "+00:00"))
        subject_date = parsed.strftime("%b %d, %Y").replace(" 0", " ")
    except Exception:
        pass
    subject = args.subject or f"Cardiology Weekly — {subject_date}"
    cards_html = "".join(hero_card_html(a, s) for a, s in summaries)
    headlines_block = headlines_html(headlines_only)

    html_body = build_email_html(
        subject=subject,
        generated_at=generated_at,
        summary_cards=cards_html,
        headlines_block=headlines_block,
        total_articles=len(unsent),
        featured_count=len(summaries),
        rct_count=rct_count,
    )

    if args.dry_run:
        preview_path = Path("output/email_preview.html")
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(html_body, encoding="utf-8")
        print(f"✅ Dry run: wrote HTML preview to {preview_path}")
        return 0

    # Send email
    smtp_user = os.getenv("GMAIL_SMTP_USER", "")
    smtp_app_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "")
    raw_to = os.getenv("EMAIL_TO", "")
    to_addrs = [e.strip() for e in raw_to.split(",") if e.strip()]
    from_addr = os.getenv("EMAIL_FROM", smtp_user)

    if not (smtp_user and smtp_app_password and from_addr and to_addrs):
        print("❌ Missing Gmail/email env vars. Need GMAIL_SMTP_USER, GMAIL_SMTP_APP_PASSWORD, EMAIL_TO, EMAIL_FROM.", file=sys.stderr)
        return 1

    send_gmail_html(
        smtp_user=smtp_user,
        smtp_app_password=smtp_app_password,
        to_addrs=to_addrs,
        from_addr=from_addr,
        subject=subject,
        html_body=html_body,
    )

    # Update sent PMIDs only after successful send (skip in test mode)
    if not args.test_mode:
        for a in unsent:
            if a.pmid:
                sent_pmids.add(a.pmid)
        save_sent_pmids(sent_state_path, sent_pmids)
        print(f"✅ Email sent to {', '.join(to_addrs)}. Marked {len(unsent)} PMIDs as sent.")
    else:
        print(f"✅ Email sent to {', '.join(to_addrs)}. 🧪 Test mode: state not updated.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
