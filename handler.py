"""
Naukri Profile Auto-Updater — AWS Lambda Handler
Triggered hourly by EventBridge Scheduler.

Flow:
  1. Fetch credentials from SSM Parameter Store
  2. Fetch resume PDF + headline.txt from S3
  3. Launch headless Chromium (via playwright)
  4. Login to Naukri
  5. Delete old resume → upload new PDF
  6. Update profile headline
"""

import os
import json
import logging
import tempfile
import time
import boto3

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ── Config from Lambda Environment Variables ───────────────────────────────────
S3_BUCKET         = os.environ["S3_BUCKET"]
S3_RESUME_KEY     = os.environ.get("S3_RESUME_KEY",   "resume.pdf")
S3_HEADLINE_KEY   = os.environ.get("S3_HEADLINE_KEY", "headline.txt")
AWS_REGION        = os.environ.get("AWS_REGION_NAME", "ap-south-1")

# SSM parameter names injected by Terraform as env vars
SSM_EMAIL_PARAM    = os.environ["SSM_EMAIL_PARAM"]
SSM_PASSWORD_PARAM = os.environ["SSM_PASSWORD_PARAM"]


def get_ssm_secret(param_name: str) -> str:
    """Fetch a SecureString from SSM Parameter Store."""
    ssm = boto3.client("ssm", region_name=AWS_REGION)
    response = ssm.get_parameter(Name=param_name, WithDecryption=True)
    return response["Parameter"]["Value"]


# Fetch credentials from SSM at cold start (cached for warm invocations)
log.info("Fetching credentials from SSM...")
NAUKRI_EMAIL    = get_ssm_secret(SSM_EMAIL_PARAM)
NAUKRI_PASSWORD = get_ssm_secret(SSM_PASSWORD_PARAM)
log.info("Credentials loaded.")

NAUKRI_LOGIN_URL   = "https://www.naukri.com/nlogin/login"
NAUKRI_PROFILE_URL = "https://www.naukri.com/mnjuser/profile"

TIMEOUT = 20_000   # ms — Playwright timeout


# ─────────────────────────────────────────────────────────────────────────────
# S3
# ─────────────────────────────────────────────────────────────────────────────

def s3_download(key: str, local_path: str) -> None:
    s3 = boto3.client("s3", region_name=AWS_REGION)
    log.info(f"S3 download: s3://{S3_BUCKET}/{key} -> {local_path}")
    s3.download_file(S3_BUCKET, key, local_path)


def fetch_assets(tmpdir: str) -> tuple:
    """Returns (resume_path, headline_text)."""
    resume_path   = os.path.join(tmpdir, "resume.pdf")
    headline_path = os.path.join(tmpdir, "headline.txt")

    s3_download(S3_RESUME_KEY,   resume_path)
    s3_download(S3_HEADLINE_KEY, headline_path)

    with open(headline_path) as f:
        headline = f.read().strip()

    log.info(f"Headline: {headline[:100]}")
    return resume_path, headline


# ─────────────────────────────────────────────────────────────────────────────
# Browser helpers (Playwright)
# ─────────────────────────────────────────────────────────────────────────────

def build_browser(playwright):
    """Launch headless Chromium suitable for Lambda."""
    return playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
            "--no-zygote",
            "--disable-blink-features=AutomationControlled",
        ],
    )


def dismiss_popup(page) -> None:
    """Close modal/overlay if present."""
    try:
        close = page.locator(
            "//span[contains(@class,'cross') or contains(@class,'close')] | "
            "//button[contains(@class,'close')]"
        ).first
        if close.is_visible(timeout=3_000):
            close.click()
            time.sleep(1)
    except Exception:
        pass


def screenshot(page, label: str) -> None:
    """Save a debug screenshot to /tmp and upload to S3."""
    path = f"/tmp/naukri_{label}_{int(time.time())}.png"
    try:
        page.screenshot(path=path)
        log.info(f"Screenshot saved: {path}")
        try:
            boto3.client("s3", region_name=AWS_REGION).upload_file(
                path, S3_BUCKET, f"debug/{os.path.basename(path)}"
            )
            log.info(f"Screenshot uploaded to s3://{S3_BUCKET}/debug/")
        except Exception as e:
            log.warning(f"Could not upload screenshot: {e}")
    except Exception as e:
        log.warning(f"Screenshot failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Naukri actions
# ─────────────────────────────────────────────────────────────────────────────

def find_element_any(page, selectors: list, timeout: int = 10_000):
    """Try multiple selectors in order, return first visible one."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout)
            log.info(f"Found element with selector: {sel}")
            return el
        except Exception:
            log.info(f"Selector not found: {sel}")
    return None


def login(page) -> None:
    log.info("Logging in to Naukri...")
    page.goto(NAUKRI_LOGIN_URL, wait_until="domcontentloaded", timeout=TIMEOUT)

    # Wait for page to fully settle
    time.sleep(5)
    screenshot(page, "login_page_loaded")
    log.info(f"Page title: {page.title()}")
    log.info(f"Page URL: {page.url}")

    dismiss_popup(page)

    # Email field - try every known Naukri selector
    email_selectors = [
        "input#usernameField",
        "input[name='username']",
        "input[placeholder='Enter your active Email ID / Username']",
        "input[placeholder*='Email']",
        "input[placeholder*='email']",
        "input[placeholder*='Username']",
        "input[type='email']",
        "input[type='text']",
        "form input:first-of-type",
    ]
    email_input = find_element_any(page, email_selectors, timeout=15_000)
    if email_input is None:
        screenshot(page, "email_field_not_found")
        raise RuntimeError("Could not find email input field. Check screenshot in S3 debug/.")

    email_input.click()
    email_input.fill("")
    email_input.type(NAUKRI_EMAIL, delay=50)
    time.sleep(1)

    # Password field
    pwd_selectors = [
        "input#passwordField",
        "input[name='password']",
        "input[placeholder='Enter your password']",
        "input[placeholder*='password']",
        "input[placeholder*='Password']",
        "input[type='password']",
    ]
    pwd_input = find_element_any(page, pwd_selectors, timeout=10_000)
    if pwd_input is None:
        screenshot(page, "password_field_not_found")
        raise RuntimeError("Could not find password input field. Check screenshot in S3 debug/.")

    pwd_input.click()
    pwd_input.fill("")
    pwd_input.type(NAUKRI_PASSWORD, delay=50)
    time.sleep(1)

    screenshot(page, "before_submit")

    # Submit button
    submit_selectors = [
        "button[type='submit']",
        "button:has-text('Login')",
        "button:has-text('Sign In')",
        "input[type='submit']",
        "button.loginButton",
        "[data-ga-track*='login']",
    ]
    submit_btn = find_element_any(page, submit_selectors, timeout=10_000)
    if submit_btn is None:
        screenshot(page, "submit_btn_not_found")
        raise RuntimeError("Could not find login submit button. Check screenshot in S3 debug/.")

    submit_btn.click()

    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    time.sleep(5)

    current = page.url
    log.info(f"Post-login URL: {current}")
    screenshot(page, "post_login")

    if "nlogin" in current.lower() or "login" in current.lower():
        raise RuntimeError(
            "Login failed - still on login page. "
            "Check credentials in SSM or OTP may be required. "
            "See screenshot in S3 debug/."
        )
    log.info("Login successful.")



def update_resume(page, resume_path: str) -> None:
    log.info("Navigating to profile page for resume update...")
    page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=TIMEOUT)
    time.sleep(3)
    dismiss_popup(page)

    # Delete existing resume
    delete_sel = (
        "[class*='resumeDeleteIcon'], [class*='deleteResume'], "
        "[class*='delete'][class*='resume'] span, "
        "#attachCVDelete"
    )
    try:
        delete_btn = page.locator(delete_sel).first
        if delete_btn.is_visible(timeout=6_000):
            delete_btn.click()
            time.sleep(1)
            confirm_sel = "button:has-text('Delete'), button:has-text('Yes'), button:has-text('Confirm')"
            try:
                confirm = page.locator(confirm_sel).first
                if confirm.is_visible(timeout=5_000):
                    confirm.click()
                    time.sleep(2)
                    log.info("Old resume deleted.")
            except Exception:
                log.info("No confirm dialog — delete may be immediate.")
        else:
            log.info("No resume to delete.")
    except Exception as e:
        log.info(f"Delete step skipped: {e}")

    # Upload new resume
    upload_sel = "input[type='file'][accept*='.pdf'], input[type='file']#attachCV, input[type='file'][class*='resume']"
    upload_input = page.locator(upload_sel).first
    upload_input.wait_for(state="attached", timeout=TIMEOUT)
    upload_input.set_input_files(resume_path)
    time.sleep(5)

    success_sel = "text=/successfully/i, text=/uploaded/i, [class*='success'], [class*='uploadSuccess']"
    try:
        page.locator(success_sel).first.wait_for(timeout=10_000)
        log.info("Resume uploaded successfully.")
    except Exception:
        screenshot(page, "resume_upload")
        log.warning("Could not confirm upload success — check screenshot in S3 debug/.")


def update_headline(page, headline: str) -> None:
    log.info("Updating profile headline...")
    page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=TIMEOUT)
    time.sleep(3)
    dismiss_popup(page)

    edit_sel = (
        "#lazyResumeHead [class*='edit'], #lazyResumeHead [class*='pencil'], "
        "[class*='resumeHeadline'] [class*='edit'], "
        "#resumeHeadline [class*='edit']"
    )
    try:
        edit_btn = page.locator(edit_sel).first
        edit_btn.wait_for(timeout=TIMEOUT)
        edit_btn.click()
        time.sleep(2)
    except Exception as e:
        screenshot(page, "headline_edit_btn")
        raise RuntimeError(f"Could not find headline edit button: {e}")

    textarea_sel = "textarea#resumeHeadlineTxt, textarea[placeholder*='headline']"
    try:
        textarea = page.locator(textarea_sel).first
        textarea.wait_for(timeout=TIMEOUT)
        textarea.fill(headline)
        time.sleep(1)

        save_btn = page.locator(
            "button[type='submit']:has-text('Save'), button[class*='saveButton']:has-text('Save')"
        ).first
        save_btn.click()
        time.sleep(3)
        log.info("Headline updated successfully.")
    except Exception as e:
        screenshot(page, "headline_save")
        raise RuntimeError(f"Failed to update headline: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    log.info("=== Naukri Updater Lambda invoked ===")
    log.info(f"Event: {json.dumps(event)}")

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmpdir:
        resume_path, headline = fetch_assets(tmpdir)

        with sync_playwright() as pw:
            browser = build_browser(pw)
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
            )
            page = ctx.new_page()

            try:
                login(page)
                update_resume(page, resume_path)
                update_headline(page, headline)
                result = {"status": "success", "message": "Profile updated successfully."}
            except Exception as e:
                log.exception("Update cycle failed")
                screenshot(page, "fatal_error")
                result = {"status": "error", "message": str(e)}
            finally:
                browser.close()

    log.info(f"Result: {result}")
    return result
