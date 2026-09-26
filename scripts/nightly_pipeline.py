#!/usr/bin/env python3
"""
TechFEST'26 SLIET — Automated Nightly Unstop Scraper & Lead Distributor
File: scripts/nightly_pipeline.py

Responsibilities:
1. Pre-flight Token Health & Expiration Check (JWT decode + live Unstop API call).
2. GitHub Bot & Telegram Alerts if token is expired, invalid, missing, or unauthorized.
3. Automatically closes any open alert issue once a valid token is provided.
4. Scrapes candidate registrations across all 63 Unstop events with pagination.
5. Deduplicates candidates across events and against existing database & ledger (pushed_phones.txt).
6. Excludes host institution (SLIET) internal candidates from external outreach.
7. Distributes the new batch evenly across all 8 callers, balancing workloads.
8. Writes updated CALLING_DATA to index.html and appends phones to pushed_phones.txt.
9. Automatically syncs newly assigned leads to Google Sheets via Apps Script Webhook API.
10. Writes execution report to GitHub Actions Step Summary ($GITHUB_STEP_SUMMARY).
"""

import os
import re
import sys
import json
import time
import base64
import datetime
import argparse
import requests
from pathlib import Path

# Load local .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

CALLER_NAMES = [
    "Balwant",
    "Shreya Mahanti",
    "Ritik Kumar",
    "Priyanshi Deshwal",
    "Ayansh Sahay",
    "Aahna",
    "Supriyaaa",
    "palak"
]

DEFAULT_GOOGLE_SHEET_API = "https://script.google.com/macros/s/AKfycbwQ8Jg5jKJNHho5QqnbLRI1R3n87ZLYaC7gUkgJJ0gRukbb2FrmOszChsIpANuEigV2Dw/exec"
REPO_DEFAULT = "sagar-anmol/techfest-calling-app"
OWNER_HANDLE = "sagar-anmol"

# ==============================================================================
# 1. TOKEN HEALTH VERIFICATION & NOTIFICATIONS
# ==============================================================================

def check_token_health(token: str) -> dict:
    """Verify JWT validity, expiration, and test live Unstop API."""
    if not token or not token.strip():
        return {
            "valid": False,
            "status": "missing",
            "hours_remaining": 0,
            "details": "UNSTOP_TOKEN is not provided in environment variables or inputs."
        }

    token = token.strip().strip('"').strip("'")

    # 1. Parse JWT payload for expiration
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWT format (expected 3 dot-separated parts)")
        payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.b64decode(payload_b64).decode("utf-8", errors="ignore"))
    except Exception as e:
        return {
            "valid": False,
            "status": "malformed",
            "hours_remaining": 0,
            "details": f"Malformed Bearer token payload: {e}"
        }

    exp = claims.get("exp")
    now = time.time()
    hours_remaining = 0
    if exp:
        exp_dt = datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
        hours_remaining = round((exp - now) / 3600, 1)
        if now >= exp:
            return {
                "valid": False,
                "status": "expired",
                "hours_remaining": 0,
                "details": f"Token expired on {exp_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC ({abs(hours_remaining):.1f} hours ago)."
            }

    # 2. Test live Unstop endpoint
    test_url = "https://unstop.com/api/v1/manage-candidates/1742408/get-candidates-count"
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    try:
        resp = requests.get(test_url, headers=headers, timeout=12)
        if resp.status_code == 200:
            return {
                "valid": True,
                "status": "valid",
                "hours_remaining": hours_remaining,
                "details": f"Token active. Valid for ~{hours_remaining} more hours."
            }
        elif resp.status_code in (401, 403):
            return {
                "valid": False,
                "status": "unauthorized",
                "hours_remaining": hours_remaining,
                "details": f"Unstop rejected token with HTTP {resp.status_code} (Unauthorized / Forbidden)."
            }
        else:
            return {
                "valid": True,
                "status": "warning",
                "hours_remaining": hours_remaining,
                "details": f"Test endpoint returned HTTP {resp.status_code}, proceeding with caution."
            }
    except Exception as e:
        return {
            "valid": True,
            "status": "network_glitch",
            "hours_remaining": hours_remaining,
            "details": f"Network glitch during token pre-flight test ({e}). Proceeding to attempt scrape."
        }

def notify_token_alert(status: str, details: str, gh_token: str = None, repo: str = REPO_DEFAULT,
                       tg_token: str = None, tg_chat: str = None):
    """
    Dispatch alerts via:
    1. GitHub Issue created by github-actions[bot] tagging @sagar-anmol (triggers push/email alerts).
    2. GitHub Actions step summary ($GITHUB_STEP_SUMMARY).
    3. Telegram bot message (if credentials provided).
    """
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    issue_title = "🚨 URGENT: Unstop Token Expired or Needs Renewal"
    issue_body = f"""## 🚨 Unstop Token Failure Alert

The automated **11:00 PM Nightly Outreach Scraper** failed to authenticate with Unstop.

### 📋 Diagnostic Summary
- **Failure Status**: `{status.upper()}`
- **Details**: {details}
- **Detected At**: `{timestamp}`
- **Impact**: Automatic lead scraping and distribution across caller queues is temporarily paused until a fresh Bearer token is provided.

---

### 🛠️ How to Renew Token in 60 Seconds:
1. Log into [unstop.com](https://unstop.com) as an organizer / competition admin.
2. Open Browser DevTools (**F12** or **Inspect** -> **Network** tab).
3. Refresh the page or click any event management button.
4. Filter requests by `api/v1/manage-candidates`.
5. Copy your fresh authorization token (the long string after `Bearer `).
6. Go to this GitHub repository:
   👉 **[Settings -> Secrets and variables -> Actions](https://github.com/{repo}/settings/secrets/actions)**
7. Update the secret or repository variable **`UNSTOP_TOKEN`** with your new token.
8. *(Optional)* Go to **Actions** -> **Nightly Unstop Lead Scraper** -> **Run workflow** to sync immediately.

*Once a valid token is provided, this alert issue will automatically be marked as resolved and closed.*

CC: @{OWNER_HANDLE}
"""

    # 1. GitHub Issue Notification
    if gh_token:
        try:
            gh_headers = {
                "Authorization": f"Bearer {gh_token}",
                "Accept": "application/vnd.github+json"
            }
            # Check for existing open issue with Unstop Token in title
            list_url = f"https://api.github.com/repos/{repo}/issues?state=open"
            list_res = requests.get(list_url, headers=gh_headers, timeout=10)
            existing_issues = [
                iss for iss in (list_res.json() if list_res.status_code == 200 else [])
                if "Unstop Token" in iss.get("title", "")
            ]

            if existing_issues:
                issue_num = existing_issues[0]["number"]
                comment_url = f"https://api.github.com/repos/{repo}/issues/{issue_num}/comments"
                comment_body = f"⏰ **Update at {timestamp}**:\nToken check still failing with status `{status}`: {details}\nPlease update the `UNSTOP_TOKEN` secret."
                requests.post(comment_url, headers=gh_headers, json={"body": comment_body}, timeout=10)
                print(f"✓ Appended update comment to open GitHub Issue #{issue_num}.")
            else:
                create_url = f"https://api.github.com/repos/{repo}/issues"
                payload = {
                    "title": issue_title,
                    "body": issue_body
                }
                c_res = requests.post(create_url, headers=gh_headers, json=payload, timeout=10)
                if c_res.status_code == 201:
                    print(f"✓ Created GitHub notification Issue #{c_res.json().get('number')} for @{OWNER_HANDLE}.")
                else:
                    print(f"Note: Could not create GitHub Issue (HTTP {c_res.status_code}: {c_res.text}).")
        except Exception as e:
            print(f"Warning: Failed to create GitHub Issue notification: {e}")

    # 2. Telegram Alert
    if tg_token and tg_chat:
        try:
            tg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            tg_msg = (
                f"🚨🚨🚨 <b>URGENT: UNSTOP TOKEN ALERT</b> 🔴🔴🔴\n\n"
                f"The automated <b>11:00 PM Nightly Outreach Scraper</b> was halted.\n\n"
                f"⚠️ <b>Status</b>: <code>{status}</code>\n"
                f"ℹ️ <b>Details</b>: {details}\n"
                f"⏰ <b>Time</b>: {timestamp}\n\n"
                f"👉 <b>Action Required</b>:\n"
                f"1. Log into unstop.com and copy your fresh Bearer token from DevTools.\n"
                f"2. Update the <code>UNSTOP_TOKEN</code> secret in GitHub repository settings.\n\n"
                f"<i>Lead distribution will resume automatically on the next scheduled run.</i>"
            )
            requests.post(tg_url, json={"chat_id": tg_chat, "text": tg_msg, "parse_mode": "HTML"}, timeout=10)
            print("✓ Telegram urgent alert dispatched successfully.")
        except Exception as e:
            print(f"Warning: Failed to dispatch Telegram alert: {e}")

    # 3. GitHub Actions Step Summary
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"\n## 🚨 UNSTOP TOKEN ALERT\n\n> [!CAUTION]\n> **Authentication Failed**: {details}\n>\n> Please renew `UNSTOP_TOKEN` in GitHub repository secrets.\n\n")
        except Exception:
            pass

def resolve_token_alert_issue(gh_token: str, repo: str = REPO_DEFAULT):
    """Automatically close any open alert issues once a valid token is used."""
    if not gh_token:
        return
    try:
        gh_headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json"
        }
        list_url = f"https://api.github.com/repos/{repo}/issues?state=open"
        res = requests.get(list_url, headers=gh_headers, timeout=10)
        if res.status_code != 200:
            return
        issues = [
            iss for iss in res.json()
            if "Unstop Token" in iss.get("title", "")
        ]
        for issue in issues:
            num = issue.get("number")
            close_url = f"https://api.github.com/repos/{repo}/issues/{num}"
            requests.patch(close_url, headers=gh_headers, json={
                "state": "closed",
                "state_reason": "completed"
            }, timeout=10)
            # Add resolution comment
            comment_url = f"https://api.github.com/repos/{repo}/issues/{num}/comments"
            requests.post(comment_url, headers=gh_headers, json={
                "body": "✅ **RESOLVED**: A fresh Unstop token has been verified and the nightly scraping pipeline executed successfully. Closing issue."
            }, timeout=10)
            print(f"✓ Automatically closed resolved GitHub Issue #{num}.")
    except Exception as e:
        print(f"Note: Could not close previous alert issue: {e}")


# ==============================================================================
# 2. SCRAPING, DATA NORMALIZATION & FILTERING
# ==============================================================================

def clean_phone(raw_phone: str) -> str:
    """Format phone numbers cleanly with +country code (defaults to +91 for 10-digit Indian numbers)."""
    if not raw_phone:
        return ""
    cleaned = re.sub(r"[\s\-\.\(\)\[\]]", "", str(raw_phone))
    if not cleaned:
        return ""
    if not cleaned.startswith("+"):
        if len(cleaned) == 10 and cleaned.isdigit():
            cleaned = "+91" + cleaned
        elif len(cleaned) == 12 and cleaned.startswith("91") and cleaned.isdigit():
            cleaned = "+" + cleaned
        else:
            cleaned = "+" + cleaned
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 10:
        return ""
    return cleaned

def clean_name(raw_name: str) -> str:
    """Format candidate name nicely without escape artifacts."""
    if not raw_name:
        return "Candidate"
    cleaned = raw_name.replace(r"\_", "_").replace("\\", "").strip()
    return " ".join(cleaned.split()).title()

def is_host_college_student(cand: dict) -> bool:
    """Check if candidate belongs to SLIET (host institution) to exclude from external outreach calling."""
    org = cand.get("college", "").lower()
    email = cand.get("email", "").lower()

    if any(k in org for k in ["sliet", "longowal", "sant longowal"]):
        return True
    if "@sliet.ac.in" in email:
        return True
    return False

def scrape_unstop_registrations(token: str, event_map: dict) -> dict:
    """
    Fetch all registrations across all configured Unstop event IDs.
    Aggregates multi-event candidates under a single phone number profile.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    candidates = {}
    total_events = len(event_map)
    print(f"Initiating registration fetch across {total_events} Unstop events...")

    for idx, (event_name, event_id) in enumerate(event_map.items(), start=1):
        event_id = str(event_id)
        page = 1
        per_page = 100
        event_count = 0

        while True:
            url = f"https://unstop.com/api/v1/manage-candidates/{event_id}/get-all-registrations?page={page}&per_page={per_page}"
            try:
                resp = requests.post(url, headers=headers, json={}, timeout=20)
                if resp.status_code in (401, 403):
                    raise PermissionError(f"Unstop authentication failed on event {event_id} (HTTP {resp.status_code})")
                if resp.status_code != 200:
                    break

                res_json = resp.json()
                data_obj = res_json.get("data", {})
                entries = data_obj.get("data", [])
                if not entries:
                    break

                for item in entries:
                    event_count += 1
                    players = item.get("players", [])
                    target_users = []

                    if players and isinstance(players, list):
                        for p in players:
                            u = p.get("user", {}) if isinstance(p, dict) else {}
                            if u:
                                target_users.append(u)
                    if not target_users:
                        u = item.get("user", {})
                        if u:
                            target_users.append(u)

                    for u in target_users:
                        phone = clean_phone(u.get("mobile", ""))
                        if not phone:
                            continue

                        name = clean_name(f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("name", ""))
                        email = u.get("email", "").strip().lower()
                        college = (u.get("organisation", "") or u.get("organization", "") or u.get("college", "") or "").strip()

                        if phone not in candidates:
                            candidates[phone] = {
                                "name": name,
                                "college": college or "Other",
                                "mobile": phone,
                                "email": email,
                                "events": set([event_name])
                            }
                        else:
                            candidates[phone]["events"].add(event_name)
                            if candidates[phone]["name"] == "Candidate" and name != "Candidate":
                                candidates[phone]["name"] = name
                            if candidates[phone]["college"] == "Other" and college:
                                candidates[phone]["college"] = college
                            if not candidates[phone]["email"] and email:
                                candidates[phone]["email"] = email

                page += 1
                time.sleep(0.08)  # Gentle pacing

            except PermissionError:
                raise
            except Exception as e:
                print(f"  Note: Issue fetching event {event_id} ({event_name}): {e}")
                break

        if idx % 10 == 0 or idx == total_events:
            print(f"  Progress: Evaluated {idx}/{total_events} events ({len(candidates):,} candidates identified so far)...")

    return candidates


# ==============================================================================
# 3. DEDUPLICATION & BATCH DISTRIBUTION
# ==============================================================================

def load_pushed_phones(ledger_file: Path) -> set:
    """Load existing phones from pushed_phones.txt ledger."""
    pushed = set()
    if ledger_file.exists():
        with open(ledger_file, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                c = line.strip()
                if c and not c.startswith("#"):
                    pushed.add(c)
    return pushed

def extract_calling_data_from_index(index_path: Path):
    """Extract CALLING_DATA object and line positions from index.html."""
    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()

    pattern = r"(let CALLING_DATA = )(\{[\s\S]*?\})(;\s*const TEAM_ROSTER =)"
    match = re.search(pattern, content)
    if not match:
        raise ValueError("Could not find 'let CALLING_DATA = ...' pattern in index.html")

    data = json.loads(match.group(2))
    return data, content, match.start(2), match.end(2)

def filter_and_distribute(scraped_candidates: dict, calling_data: dict, pushed_phones: set, exclude_sliet: bool = True):
    """
    Deduplicates scraped candidates against:
    1. pushed_phones.txt
    2. All existing caller queues in calling_data (phones and emails)
    3. Host college (SLIET) if exclude_sliet is True.

    Distributes new candidates evenly across all 8 callers.
    """
    # Collect all existing phones and emails
    existing_phones = set(pushed_phones)
    existing_emails = set()
    max_id = 0

    for caller, leads in calling_data.items():
        for lead in leads:
            p = clean_phone(lead.get("mobile", ""))
            if p:
                existing_phones.add(p)
            em = lead.get("email", "").strip().lower()
            if em:
                existing_emails.add(em)
            lead_id = lead.get("id") or 0
            if isinstance(lead_id, int) and lead_id > max_id:
                max_id = lead_id

    # Filter candidates
    new_candidates = []
    skipped_duplicates = 0
    skipped_sliet = 0

    for phone, cand in scraped_candidates.items():
        if phone in existing_phones:
            skipped_duplicates += 1
            continue
        if cand["email"] and cand["email"] in existing_emails:
            skipped_duplicates += 1
            continue
        if exclude_sliet and is_host_college_student(cand):
            skipped_sliet += 1
            continue

        sorted_events = ", ".join(sorted(list(cand["events"])))
        cand_record = {
            "name": cand["name"],
            "college": cand["college"] if cand["college"] else "Other",
            "mobile": cand["mobile"],
            "email": cand["email"],
            "events": sorted_events
        }
        new_candidates.append(cand_record)

    print(f"\nDeduplication Summary:")
    print(f"• Total candidates scraped: {len(scraped_candidates):,}")
    print(f"• Skipped duplicates (already assigned): {skipped_duplicates:,}")
    print(f"• Skipped internal SLIET students: {skipped_sliet:,}")
    print(f"• Net new unique external candidates to assign: {len(new_candidates):,}")

    if not new_candidates:
        return calling_data, [], max_id

    # Distribute new candidates across all 8 callers, keeping workloads balanced
    newly_assigned_records = []
    current_id = max_id

    for cand in new_candidates:
        current_id += 1
        # Pick the caller with the fewest total leads
        target_caller = min(CALLER_NAMES, key=lambda c: len(calling_data.get(c, [])))

        lead_entry = {
            "id": current_id,
            "name": cand["name"],
            "college": cand["college"],
            "mobile": cand["mobile"],
            "email": cand["email"],
            "events": cand["events"],
            "callStatus": "Pending",
            "payStatus": "Pending",
            "amount": "",
            "crNumber": "",
            "expectedDate": "",
            "remarks": "",
            "attended": False
        }

        if target_caller not in calling_data:
            calling_data[target_caller] = []
        calling_data[target_caller].append(lead_entry)

        # For Google Sheet sync payload
        sheet_entry = dict(lead_entry)
        sheet_entry["callerName"] = target_caller
        newly_assigned_records.append(sheet_entry)

    return calling_data, newly_assigned_records, current_id


# ==============================================================================
# 4. GOOGLE SHEET SYNCHRONIZATION
# ==============================================================================

def sync_new_leads_to_google_sheet(sheet_api_url: str, new_leads: list):
    """Pushes newly assigned leads to Google Sheets via the Apps Script Webhook API."""
    if not sheet_api_url or not new_leads:
        return

    print(f"Streaming {len(new_leads)} new leads into Google Sheet tabs via Apps Script API...")
    payload = {
        "action": "addBatchLeads",
        "source": "GitHub Actions Nightly Outreach Scraper (11:00 PM IST)",
        "leads": new_leads
    }

    try:
        resp = requests.post(sheet_api_url, json=payload, timeout=30)
        res_json = resp.json() if resp.status_code == 200 else {}
        if res_json.get("success"):
            print("✓ Google Sheet synchronization complete: All leads appended to respective caller tabs.")
        else:
            print(f"Warning: Sheet API response: {resp.text[:150]}")
    except Exception as e:
        print(f"Warning: Failed to sync leads to Google Sheet Webhook API: {e}")


# ==============================================================================
# 5. MAIN PIPELINE CONTROLLER
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="Nightly Unstop Lead Scraper & Assignment Pipeline")
    parser.add_argument("--token", default=os.getenv("UNSTOP_TOKEN"), help="Unstop Bearer Token")
    parser.add_argument("--dry-run", action="store_true", default=os.getenv("DRY_RUN", "false").lower() == "true",
                        help="Perform scrape & deduplication without committing changes")
    parser.add_argument("--sheet-api", default=os.getenv("GOOGLE_SHEET_API_URL", DEFAULT_GOOGLE_SHEET_API),
                        help="Google Apps Script Web App Endpoint")
    parser.add_argument("--root-dir", default=".", help="Root directory containing techfest-calling-app")
    args = parser.parse_args()

    root = Path(args.root_dir).resolve()
    app_dir = root if (root / "index.html").exists() else root / "techfest-calling-app"
    index_file = app_dir / "index.html"
    demo_file = root / "demo.html"
    ledger_file = app_dir / "pushed_phones.txt"
    config_file = app_dir / "config" / "unstop_opp_map.json"

    gh_token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY", REPO_DEFAULT)
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")

    print("================================================================================")
    print("🌙 TechFEST'26 SLIET — Automated Nightly Unstop Scraper (11:00 PM IST Run)")
    print(f"Timestamp: {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"App Directory: {app_dir}")
    print("================================================================================\n")

    # Step 1: Pre-flight Token Health Check
    token_status = check_token_health(args.token)
    print(f"Pre-flight Token Status: {token_status['status'].upper()} — {token_status['details']}")

    if not token_status["valid"]:
        notify_token_alert(
            status=token_status["status"],
            details=token_status["details"],
            gh_token=gh_token,
            repo=repo,
            tg_token=tg_token,
            tg_chat=tg_chat
        )
        sys.exit(2)

    # Token is valid: close any open alert issue
    resolve_token_alert_issue(gh_token=gh_token, repo=repo)

    # Step 2: Load Event Map
    if not config_file.exists():
        print(f"❌ Error: Config file not found at {config_file}")
        sys.exit(1)

    with open(config_file, "r", encoding="utf-8") as f:
        event_map = json.load(f)

    # Step 3: Scrape Registrations from Unstop
    try:
        scraped_candidates = scrape_unstop_registrations(token=args.token, event_map=event_map)
    except PermissionError as pe:
        notify_token_alert(
            status="unauthorized",
            details=str(pe),
            gh_token=gh_token,
            repo=repo,
            tg_token=tg_token,
            tg_chat=tg_chat
        )
        sys.exit(2)

    # Step 4: Load Existing Database & Ledger
    calling_data, html_content, start_pos, end_pos = extract_calling_data_from_index(index_file)
    pushed_phones = load_pushed_phones(ledger_file)

    # Step 5: Deduplicate and Evenly Distribute New Batch
    updated_calling_data, newly_assigned, final_max_id = filter_and_distribute(
        scraped_candidates=scraped_candidates,
        calling_data=calling_data,
        pushed_phones=pushed_phones,
        exclude_sliet=True
    )

    new_count = len(newly_assigned)
    total_leads = sum(len(v) for v in updated_calling_data.values())

    # Output variables for GitHub Actions
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        try:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write(f"new_leads_count={new_count}\n")
                f.write(f"total_leads_count={total_leads}\n")
                f.write(f"hours_remaining={token_status.get('hours_remaining', 'N/A')}\n")
        except Exception:
            pass

    # Print caller distribution summary
    print("\nCaller Workload Distribution:")
    for c in CALLER_NAMES:
        print(f"  • {c:18}: {len(updated_calling_data.get(c, []))} total leads")

    if new_count == 0:
        print("\n✓ No new external candidates detected. Database remains in pristine state.")
        summary_md = f"""### 🌙 Nightly Unstop Scraper (11:00 PM IST)
- **Status**: ✅ Completed successfully
- **New Leads Found**: 0 (all evaluated candidates already present in database)
- **Total Leads in Calling Queue**: {total_leads}
- **Token Validity Remaining**: ~{token_status.get('hours_remaining', 'N/A')} hours
"""
    else:
        print(f"\n🎉 Successfully allocated {new_count} new unique candidates across the 8 callers!")
        summary_md = f"""### 🌙 Nightly Unstop Scraper (11:00 PM IST)
- **Status**: ✅ Successfully Scraped & Distributed
- **New Candidates Added**: **{new_count}**
- **New Total Candidates**: **{total_leads}**
- **Token Validity Remaining**: ~{token_status.get('hours_remaining', 'N/A')} hours

#### Caller Distribution:
| Caller | Total Assigned Leads |
| :--- | :--- |
"""
        for c in CALLER_NAMES:
            summary_md += f"| **{c}** | {len(updated_calling_data.get(c, []))} |\n"

    # Step Summary
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"\n{summary_md}\n")
        except Exception:
            pass

    if args.dry_run:
        print("\n[DRY RUN] Skipping file writes and sheet updates.")
        return

    if new_count > 0:
        # Step 6: Write updated CALLING_DATA back to index.html and demo.html
        formatted_json = json.dumps(updated_calling_data, indent=2)
        new_html = html_content[:start_pos] + formatted_json + html_content[end_pos:]
        with open(index_file, "w", encoding="utf-8") as f:
            f.write(new_html)
        print(f"✓ Updated {index_file} with fresh CALLING_DATA.")

        if demo_file.parent.exists():
            with open(demo_file, "w", encoding="utf-8") as f:
                f.write(new_html)
            print(f"✓ Synchronized mirror: {demo_file}.")

        # Append new phones to ledger
        with open(ledger_file, "a", encoding="utf-8") as f:
            for item in newly_assigned:
                f.write(f"{item['mobile']}\n")
        print(f"✓ Appended {new_count} numbers to {ledger_file}.")

        # Step 7: Push to Google Sheet Webhook API
        sync_new_leads_to_google_sheet(args.sheet_api, newly_assigned)

if __name__ == "__main__":
    main()
