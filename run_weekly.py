#!/usr/bin/env python3
"""
run_weekly.py

Thin wrapper that runs the pipeline end-to-end:

1) Fetch latest PubMed articles -> writes output JSON (latest + archive)
2) Summarise + email -> sends HTML digest and updates sent PMID state

This keeps fetch and email logic in their own scripts while allowing a single
command (and later a single Cloud Run entrypoint).

Usage (local):
  python run_weekly.py --days 7 --max 300 --email you@domain.com

Notes:
- Assumes fetch_cardiology_pubmed.py and summarise_and_email.py are in the same repo root.
- Passes through optional flags for fetch; summarise/email reads env vars and latest JSON.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def run_cmd(cmd: list[str]) -> None:
    """Run a command, stream output, and raise if it fails."""
    print("\n▶ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Run fetch + summarise/email pipeline")
    ap.add_argument("--days", type=int, default=7, help="Look back this many days (default: 7)")
    ap.add_argument("--max", type=int, default=300, help="Max PMIDs to retrieve (default: 300)")
    ap.add_argument("--email", type=str, default=os.getenv("NCBI_EMAIL"), help="NCBI contact email (or set NCBI_EMAIL)")
    ap.add_argument("--api-key", type=str, default=os.getenv("NCBI_API_KEY"), help="NCBI API key (or set NCBI_API_KEY)")
    ap.add_argument("--out", type=str, default=os.getenv("LATEST_JSON", "output/cardiology_recent.json"),
                    help="Stable output JSON filename for fetch step (default: output/cardiology_recent.json)")
    ap.add_argument("--include-no-abstract", action="store_true", help="Include articles without abstracts in fetch step")
    ap.add_argument("--no-dedupe", action="store_true", help="Disable dedupe in fetch step")
    ap.add_argument("--dry-run-email", action="store_true", help="Do not send email; generate HTML preview only")
    ap.add_argument("--test-mode", action="store_true",
                    help="Test mode: skip all state file reading/writing in both fetch and email steps")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent
    fetch_script = repo_root / "fetch_cardiology_pubmed.py"
    email_script = repo_root / "summarise_and_email.py"

    if not fetch_script.exists():
        print(f"❌ Missing: {fetch_script}", file=sys.stderr)
        return 1
    if not email_script.exists():
        print(f"❌ Missing: {email_script}", file=sys.stderr)
        return 1

    if not args.email:
        print("❌ NCBI email is required. Pass --email or set NCBI_EMAIL.", file=sys.stderr)
        return 1

    # Ensure LATEST_JSON env var is consistent for the summarise/email step
    os.environ["LATEST_JSON"] = args.out

    try:
        # 1) Fetch step
        fetch_cmd = [sys.executable, str(fetch_script), "--days", str(args.days), "--max", str(args.max), "--out", args.out, "--email", args.email]
        if args.api_key:
            fetch_cmd += ["--api-key", args.api_key]
        if args.include_no_abstract:
            fetch_cmd += ["--include-no-abstract"]
        if args.no_dedupe:
            fetch_cmd += ["--no-dedupe"]
        if args.test_mode:
            fetch_cmd += ["--test-mode"]

        run_cmd(fetch_cmd)

        # 2) Summarise + email step
        email_cmd = [sys.executable, str(email_script)]
        if args.dry_run_email:
            email_cmd += ["--dry-run"]
        if args.test_mode:
            email_cmd += ["--test-mode"]

        run_cmd(email_cmd)

        print("\n✅ Pipeline completed successfully.")
        return 0

    except subprocess.CalledProcessError as e:
        print(f"\n❌ Pipeline failed (exit code {e.returncode}).", file=sys.stderr)
        return e.returncode


if __name__ == "__main__":
    raise SystemExit(main())

# ------------------------------------------------------------------------------
# Example usage:
#
# # Standard run (fetches articles, sends email, updates state files)
# python run_weekly.py --days 7 --max 300
#
# # Test mode (no state files read/written - can run repeatedly)
# python run_weekly.py --test-mode
#
# # Test mode + dry run (no state files, no email sent, just HTML preview)
# python run_weekly.py --test-mode --dry-run-email
#
# # Full test with custom parameters
# python run_weekly.py --test-mode --dry-run-email --days 14 --max 100
# ------------------------------------------------------------------------------