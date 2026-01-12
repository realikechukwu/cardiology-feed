# Weekly Cardiology Research Digest

A fully automated pipeline that:
- fetches recent cardiology research from PubMed,
- filters and classifies high-value studies,
- generates concise structured summaries from the abstract,
- emails a formatted weekly digest via Gmail,
- and **deduplicates papers across weeks** using a persisted state file.

The system is designed to run:
- locally (for development and testing), and
- automatically once per week using **GitHub Actions**.


## What this project does

On each run, the pipeline:

1. Queries PubMed for articles published in the last *N* days from selected cardiology journals.
2. Extracts titles, abstracts, authors, publication types, and metadata.
3. Classifies articles into:
   - priority research,
   - standard articles,
   - excluded (editorials, letters, etc.).
4. Deduplicates articles across runs using a persisted `seen_pmids.json` state file.
5. Uses the OpenAI API to generate concise, evidence-faithful summaries.
6. Sends an HTML email digest via Gmail (with per-subscriber personalization).
7. Records which PMIDs were sent so they are **never re-emailed** (`sent_pmids.json`).


## Repository structure

```text
.
├── README.md
├── requirements.txt
├── fetch_cardiology_pubmed.py
├── summarise_and_email.py
├── run_weekly.py
│
├── state/
│   ├── seen_pmids.json
│   └── sent_pmids.json
│
├── output/
│   ├── cardiology_recent.json
│   └── email_preview.html
│
├── scripts/
│   └── feedback_handler.gs   # Google Apps Script for feedback
│
├── .github/
│   └── workflows/
│       └── weekly_digest.yml
│
├── .gitignore
└── venv/              # local only, NOT committed
```

## Prerequisites

### Required accounts
- **NCBI (PubMed)** – free, for E-utilities access
- **OpenAI** – for article summarisation
- **Gmail account** – for sending emails (via SMTP app password)
- **GitHub** – for scheduled automation


## Local setup (development & testing)

### 1. Clone the repository
```bash
git clone https://github.com/your-username/your-repo.git
cd your-repo
```

### 2. Create and activate a virtual environment

```code
python3 -m venv venv
source venv/bin/activate
```
### 3. Install dependencies
```code
pip install -r requirements.txt
```
## Environment variables
Create a .env file locally (not committed):

```code
# PubMed
NCBI_EMAIL=you@example.com
NCBI_API_KEY=optional_but_recommended

# OpenAI
OPENAI_API_KEY=sk-...

# Gmail SMTP
GMAIL_SMTP_USER=yourgmail@gmail.com
GMAIL_SMTP_APP_PASSWORD=xxxxxxxxxxxxxxxx
EMAIL_FROM=yourgmail@gmail.com
EMAIL_TO=recipient1@gmail.com,recipient2@gmail.com

# Optional: Google Sheets subscribers (overrides EMAIL_TO)
GOOGLE_SHEET_ID=your_sheet_id
GOOGLE_CREDENTIALS={"type":"service_account",...}

# Optional: delay (seconds) between per-recipient sends
EMAIL_SEND_DELAY=1.5
```
## Notes:
- Gmail requires a Google App Password (not your normal password).
- EMAIL_TO can be a comma-separated list.

## Running locally
### Run the full weekly pipeline

```code
python run_weekly.py --days 7 --max 300
#this can be run without the arguments, as they are the default in the function
```

## What happens
- Fetches recent cardiology articles
- Generates summaries
- Sends email
- Updates state/seen_pmids.json and state/sent_pmids.json

You should receive the digest email if new articles are found.

## Deduplication logic (important)

The files:

```code
state/seen_pmids.json
state/sent_pmids.json
```

Stores all PMIDs that have already been seen in fetches and emailed.
- They are committed to the repository
- seen_pmids.json is updated after successful fetch
- sent_pmids.json is updated after a successful email send
- This guarantees:
  - no duplicate emails across weeks
  - reproducible state
  - safe retries

## GitHub Actions automation

Workflow file

```code
.github/workflows/weekly_digest.yml
```

## Schedule

Runs automatically:
- Every Sunday at 07:00 UTC

Manual runs are enabled via the Actions tab.


## GitHub Secrets setup

Add the following secrets in:

```code
Repo → Settings → Secrets and variables → Actions
```


| Secret name               | Value                              |
|---------------------------|------------------------------------|
| NCBI_EMAIL                | Your email                         |
| NCBI_API_KEY              | PubMed API key (optional)          |
| OPENAI_API_KEY            | OpenAI API key                     |
| GMAIL_SMTP_USER           | Gmail address                      |
| GMAIL_SMTP_APP_PASSWORD   | Gmail app password                 |
| EMAIL_FROM                | Sender email                       |
| EMAIL_TO                  | Comma-separated recipients         |
| GOOGLE_SHEET_ID           | Google Sheet ID (optional)         |
| GOOGLE_CREDENTIALS        | Service account JSON (optional)    |
| EMAIL_SEND_DELAY          | Delay in seconds (optional)        |
| FEEDBACK_WEBHOOK_URL      | Google Apps Script URL (optional)  |


## Important:
- Secret names must not include =
- Values may include commas (for EMAIL_TO)
  

## GitHub Actions permissions

In:

```text
Settings → Actions → General → Workflow permissions
```

Enable:
- Read and write permissions

Do not enable:
- “Allow GitHub Actions to create and approve pull requests”
  

## How to verify it’s working

After a successful run:
1. You receive the email digest.
2. A commit appears on main:

```code
github-actions[bot] Update sent PMIDs after weekly digest
```

3. state/sent_pmids.json is updated.
4. state/seen_pmids.json is updated.
5. Future runs do not resend the same papers.


## Customisation

Common adjustments:
- Change journals in fetch_cardiology_pubmed.py (consider adding high impact general medical journals like Lancet, NEJM, BMJ and Nature with Cardiology-specific keywords and/or MeSh
- Adjust lookback window (--days)
- Update email recipients via GitHub Secrets
- Change schedule in weekly_digest.yml


## Feedback feature (optional)

Users can mark articles as "useful" or "not relevant" directly from the email. This enables:
- **Personal reading list**: Saved articles appear in a "Your Saves" section in the next digest
- **Improved relevance**: Feedback data can inform future article prioritization

### Setup

1. **Deploy the Google Apps Script**
   - Open your Google Sheet (same one used for subscribers)
   - Go to Extensions > Apps Script
   - Paste the contents of `scripts/feedback_handler.gs`
   - Deploy > New deployment > Web app
   - Execute as: Me, Who has access: Anyone
   - Copy the Web App URL

2. **Add the environment variable**
   ```
   FEEDBACK_WEBHOOK_URL=https://script.google.com/macros/s/your-script-id/exec
   ```
   Add to `.env` locally and to GitHub Secrets for Actions.

3. **How it works**
   - Each article in the digest shows: `Was this useful? Yes · No`
   - Clicking logs feedback to a "feedback" sheet tab
   - Next week's digest shows a "Your Saves" section with articles marked "Yes"

### Data stored

The feedback sheet contains:
| timestamp | user | pmid | title | vote |
|-----------|------|------|-------|------|
| 2026-01-10T... | user@example.com | 12345678 | Article title... | yes |

No additional costs - uses existing Google Sheets integration.


## Design philosophy
- Simple, explicit state (Git-tracked JSON)
- No external databases
- Fail-safe deduplication
- Human-readable outputs
- Minimal cloud dependencies

This keeps the system transparent, auditable, and easy to maintain.


## Troubleshooting
- No email sent: check GitHub Actions logs.
- No commit created: ensure state/sent_pmids.json is tracked and not ignored.
- Missing module error: confirm the dependency exists in requirements.txt.


## License

Personal / educational use.
Adapt freely for private or academic workflows.



## Acknowledgements
- NCBI PubMed E-utilities
- OpenAI API
- GitHub Actions
