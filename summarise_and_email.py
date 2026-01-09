#!/usr/bin/env python3
"""
Summarise latest cardiology digest JSON with OpenAI and email as HTML via Gmail SMTP.

- Reads the stable latest JSON produced by fetch_cardiology_pubmed.py
- Selects top items (priority first) for LLM summaries
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

# OpenAI SDK (new-style). Install: pip install openai
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
    # Keeps email rendering sane
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
# Selection logic (simple MVP)
# ----------------------------
def select_for_summary(
    articles: List[Article],
    max_summaries: int,
    min_abstract_chars: int = 200,
) -> Tuple[List[Article], List[Article]]:
    """
    Returns (to_summarise, headlines_only)
    - prioritises category == "priority", then "standard"
    - requires an abstract of at least min_abstract_chars for LLM summarisation
    """
    priority = [a for a in articles if a.category == "priority"]
    standard = [a for a in articles if a.category == "standard"]

    eligible = [a for a in (priority + standard) if len(a.abstract) >= min_abstract_chars]
    to_sum = eligible[:max_summaries]

    to_sum_pmids = {a.pmid for a in to_sum if a.pmid}
    headlines = [a for a in (priority + standard) if a.pmid not in to_sum_pmids]

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
            "editorial_headline": {"type": "string"},
            "clinical_question": {"type": "string"},
            "what_was_done": {"type": "string"},
            "key_findings": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 2,
                "maxItems": 3
            },
            "interpretation": {"type": "string"},
            "limitations": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 2
            },
            "take_home_message": {"type": "string"},
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 6
            },
        },
        "required": [
            "editorial_headline",
            "clinical_question",
            "what_was_done",
            "key_findings",
            "interpretation",
            "limitations",
            "take_home_message",
            "tags",
        ],
    },
    "strict": True,
}


def summarise_one(client: OpenAI, model: str, a: Article) -> Dict[str, Any]:
    """
    Uses OpenAI Chat Completions API with strict JSON schema output.
    """

    system = (
        "You are writing a JACC-style editorial note for a weekly cardiology digest. "
        "Be concise, clinically neutral, and evidence-faithful. "
        "Use ONLY the provided title and abstract. "
        "Do not invent numbers or details. "
        "If a detail is not reported in the abstract, write exactly: 'Not reported in abstract'. "
        "Avoid hype words (e.g., breakthrough, game-changing, promising). "
        "Write short declarative sentences. "
        "Return JSON matching the schema."
    )

    user = f"""TITLE: {a.title}
JOURNAL: {a.journal}
PUB DATE: {a.pub_date}
PUBLICATION TYPES: {", ".join(a.publication_types) if a.publication_types else "Not reported"}
ABSTRACT:
{a.abstract}
LINK: {a.url}
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
# HTML rendering (IMPROVED)
# ----------------------------
def hero_card_html(a: Article, s: Dict[str, Any]) -> str:
    """Enhanced minimalist card design with better spacing and typography"""
    title = html_escape(strip_control_chars(a.title))
    journal = html_escape(a.journal)
    pub_date = html_escape(a.pub_date)
    url = html_escape(a.url)

    authors = ", ".join(a.authors) if a.authors else ""
    authors = html_escape(authors)

    def line(label: str, value: str) -> str:
        value = html_escape(strip_control_chars(value))
        return f"""
        <div style="margin:16px 0; padding-bottom:12px; border-bottom:1px solid #f0f0f0;">
          <div style="font-size:11px; color:#999; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px;">{label}</div>
          <div style="font-size:14px; line-height:1.6; color:#2c2c2c;">{value}</div>
        </div>
        """

    def bullets(label: str, items: list[str]) -> str:
        if not items:
            return line(label, "Not reported in abstract")
        lis = "".join(
            f"<li style='margin:8px 0; line-height:1.6; color:#2c2c2c;'>{html_escape(strip_control_chars(str(x)))}</li>"
            for x in items
        )
        return f"""
        <div style="margin:16px 0; padding-bottom:12px; border-bottom:1px solid #f0f0f0;">
          <div style="font-size:11px; color:#999; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:8px;">{label}</div>
          <ul style="margin:0; padding-left:20px; font-size:14px; line-height:1.6; list-style-type:disc;">
            {lis}
          </ul>
        </div>
        """

    tags = s.get("tags", [])
    tags_html = ""
    if tags:
        pills = []
        for t in tags[:6]:
            t = html_escape(strip_control_chars(str(t)))
            pills.append(f'<span style="display:inline-block; padding:4px 12px; margin:4px 8px 4px 0; background:#f8f8f8; border-radius:16px; font-size:11px; color:#666; font-weight:500;">{t}</span>')
        tags_html = f'<div style="margin-top:20px; padding-top:16px; border-top:1px solid #f0f0f0;">{"".join(pills)}</div>'

    return f"""
    <div style="border:1px solid #e8e8e8; border-radius:8px; padding:24px; margin:20px 0; background:#ffffff; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
      <div style="font-size:18px; font-weight:700; line-height:1.4; margin-bottom:8px; color:#1a1a1a;">
        <a href="{url}" style="text-decoration:none; color:#1a1a1a;">{title}</a>
      </div>
      <div style="font-size:12px; color:#888; margin-bottom:20px; padding-bottom:16px; border-bottom:2px solid #f5f5f5;">
        <span style="font-weight:600;">{journal}</span> · {pub_date}{(" · " + authors) if authors else ""}
      </div>

      {line("Headline", s.get("editorial_headline",""))}
      {line("Clinical question", s.get("clinical_question",""))}
      {line("What was done", s.get("what_was_done",""))}
      {bullets("Key findings", s.get("key_findings", []))}
      {line("Interpretation", s.get("interpretation",""))}
      {bullets("Limitations / what's missing", s.get("limitations", []))}
      
      <div style="margin:16px 0;">
        <div style="font-size:11px; color:#999; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:6px;">Take-home message</div>
        <div style="font-size:15px; line-height:1.6; color:#1a1a1a; font-weight:500; background:#f9f9f9; padding:16px; border-radius:6px; border-left:3px solid #666;">
          {html_escape(strip_control_chars(s.get("take_home_message","")))}
        </div>
      </div>

      {tags_html}
    </div>
    """


def headlines_html(items: List[Article]) -> str:
    """Improved headlines section with better visual hierarchy"""
    if not items:
        return "<div style='color:#999; font-size:14px; font-style:italic; padding:12px 0;'>No additional headlines this week.</div>"

    lis = []
    for a in items:
        title = html_escape(strip_control_chars(a.title))
        journal = html_escape(a.journal)
        url = html_escape(a.url)
        lis.append(f"""
            <li style='margin:12px 0; padding:12px 0; border-bottom:1px solid #f5f5f5; line-height:1.5;'>
                <a href='{url}' style='color:#2c2c2c; text-decoration:none; font-size:14px; font-weight:500; display:block; margin-bottom:4px;'>{title}</a>
                <span style='color:#888; font-size:12px;'>{journal}</span>
            </li>
        """)
    return "<ul style='list-style:none; padding:0; margin:0;'>" + "".join(lis) + "</ul>"


def build_email_html(
    subject: str,
    generated_at: str,
    summary_cards: str,
    headlines_block: str,
    total_new: int,
    archive_hint: str = "",
) -> str:
    """Enhanced email template with modern minimalist design"""
    return f"""\
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
    <title>{html_escape(subject)}</title>
  </head>
  <body style="margin:0; padding:0; background:#f5f5f5; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;">
    <div style="max-width:720px; margin:0 auto; padding:32px 16px;">
      
      <!-- Header -->
      <div style="background:#ffffff; border:1px solid #e8e8e8; border-radius:8px; padding:28px; margin-bottom:24px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:28px; font-weight:800; margin-bottom:8px; color:#1a1a1a; letter-spacing:-0.5px;">
          Weekly Cardiology Digest
        </div>
        <div style="color:#666; font-size:13px; line-height:1.5;">
          <div style="margin-bottom:4px;">📅 {html_escape(generated_at)}</div>
          <div>📊 {total_new} new articles this week</div>
        </div>
        {f"<div style='margin-top:12px; padding-top:12px; border-top:1px solid #f0f0f0; color:#888; font-size:11px;'>{html_escape(archive_hint)}</div>" if archive_hint else ""}
      </div>

      <!-- Featured Studies Section -->
      <div style="margin-bottom:24px;">
        <div style="font-size:20px; font-weight:700; margin-bottom:16px; color:#1a1a1a; padding-left:4px;">
          ⭐ Featured Studies
        </div>
        {summary_cards if summary_cards else "<div style='color:#999; font-size:14px; padding:20px; background:#fff; border:1px solid #e8e8e8; border-radius:8px;'>No featured studies this week.</div>"}
      </div>

      <!-- Headlines Section -->
      <div style="background:#ffffff; border:1px solid #e8e8e8; border-radius:8px; padding:24px; margin-bottom:24px; box-shadow:0 1px 3px rgba(0,0,0,0.04);">
        <div style="font-size:18px; font-weight:700; margin-bottom:16px; color:#1a1a1a;">
          📰 Other Notable Papers
        </div>
        {headlines_block}
      </div>

      <!-- Footer -->
      <div style="color:#999; font-size:11px; line-height:1.6; text-align:center; padding:20px 16px;">
        <div style="margin-bottom:8px;">
          Summaries are automatically generated from PubMed abstracts using structured AI analysis.
        </div>
        <div>
          Always refer to the original publications for complete details and verification.
        </div>
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
    msg["To"] = ", ".join(to_addrs)

    msg.attach(MIMEText("Your email client does not support HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    context = ssl.create_default_context()

    # Gmail SMTP: STARTTLS on 587
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(smtp_user, smtp_app_password)
        server.sendmail(from_addr, to_addrs, msg.as_string())


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
        # You can choose to email a 'no new items' notice; for now, exit cleanly.
        return 0

    # Optional: avoid re-sending if these PMIDs were already emailed
    sent_pmids = load_sent_pmids(sent_state_path)
    unsent = [a for a in articles if a.pmid and a.pmid not in sent_pmids]

    if not unsent:
        print("ℹ️ All items in latest JSON are already marked as sent. Nothing to email.")
        return 0

    to_sum, headlines_only = select_for_summary(unsent, max_summaries=args.max_summaries)

    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        print("❌ OPENAI_API_KEY missing in environment/.env", file=sys.stderr)
        return 1

    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    client = OpenAI(api_key=openai_key)

    summaries: List[Tuple[Article, Dict[str, Any]]] = []
    for a in to_sum:
        try:
            s = summarise_one(client, model, a)
            summaries.append((a, s))
        except Exception as e:
            print(f"⚠️ Summary failed for PMID {a.pmid}: {e}", file=sys.stderr)

    # Build HTML
    subject = args.subject or f"Weekly Cardiology Digest ({run_date}) — {len(unsent)} new"
    cards_html = "".join(hero_card_html(a, s) for a, s in summaries) or \
                 "<div style='color:#666; font-size:14px;'>No editorial notes generated this run.</div>"

    headlines_block = headlines_html(headlines_only)

    archive_hint = f"Source: {latest_path.name}"
    html_body = build_email_html(
        subject=subject,
        generated_at=generated_at,
        summary_cards=cards_html,
        headlines_block=headlines_block,
        total_new=len(unsent),
        archive_hint=archive_hint,
    )

    if args.dry_run:
        preview_path = Path("output/email_preview.html")
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(html_body, encoding="utf-8")
        print(f"✅ Dry run: wrote HTML preview to {preview_path}")
        return 0

    # Send email (Gmail SMTP)
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

    # ONLY after successful send: update sent pmids
    for a in unsent:
        if a.pmid:
            sent_pmids.add(a.pmid)
    save_sent_pmids(sent_state_path, sent_pmids)

    print(
        f"✅ Email sent to {', '.join(to_addrs)}. "
        f"Marked {len(unsent)} PMIDs as sent in {sent_state_path}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())