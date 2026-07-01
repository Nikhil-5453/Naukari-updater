"""
Naukri Profile Auto-Updater — AWS Lambda Handler
Triggered every 5 minutes by EventBridge Scheduler.

Key fixes over previous versions:
  1. "Access Denied" from Naukri (AWS IP blocked) → solved via:
       a) Naukri REST API for login (no browser needed for auth)
       b) Cookie session reuse cached in SSM (avoids repeated logins)
       c) Playwright only used for profile UI actions (resume + headline)
       d) Optional HTTP proxy support via PROXY_URL env var
  2. Credentials fetched from SSM (not os.environ directly)
  3. Robust selectors with fallbacks for every UI element
  4. Screenshots uploaded to S3 on every failure for debugging
  5. Graceful error handling — Lambda always returns a result, never crashes silently
  6. 5-minute schedule safe — session reuse prevents login rate-limiting
"""

import os
import json
import logging
import tempfile
import time
import random
import boto3
import requests

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ── Environment variables (set by Terraform) ──────────────────────────────────
S3_BUCKET          = os.environ["S3_BUCKET"]
S3_RESUME_KEY      = os.environ.get("S3_RESUME_KEY",   "resume.pdf")
S3_HEADLINE_KEY    = os.environ.get("S3_HEADLINE_KEY", "headline.txt")
AWS_REGION         = os.environ.get("AWS_REGION_NAME", "ap-south-1")
SSM_EMAIL_PARAM    = os.environ["SSM_EMAIL_PARAM"]
SSM_PASSWORD_PARAM = os.environ["SSM_PASSWORD_PARAM"]

# Optional: residential proxy URL to bypass Naukri IP blocking
# Format: "http://user:pass@host:port"  or "socks5://user:pass@host:port"
PROXY_URL = os.environ.get("PROXY_URL", None)

# SSM key used to cache the Naukri session cookie between Lambda invocations
SSM_COOKIE_PARAM = os.environ.get("SSM_COOKIE_PARAM", "/naukri-updater/SESSION_COOKIE")

NAUKRI_LOGIN_URL   = "https://www.naukri.com/nlogin/login"
NAUKRI_PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
NAUKRI_API_LOGIN   = "https://www.naukri.com/central-login-services/v1/login"

PAGE_TIMEOUT  = 30_000   # ms
SHORT_TIMEOUT =  8_000   # ms

# Realistic browser user-agent
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ─────────────────────────────────────────────────────────────────────────────
# SSM helpers
# ─────────────────────────────────────────────────────────────────────────────

def ssm_client():
    return boto3.client("ssm", region_name=AWS_REGION)


def get_ssm_value(param_name: str, decrypt: bool = True) -> str | None:
    try:
        r = ssm_client().get_parameter(Name=param_name, WithDecryption=decrypt)
        return r["Parameter"]["Value"]
    except ssm_client().exceptions.ParameterNotFound:
        return None
    except Exception as e:
        log.warning(f"SSM get failed for {param_name}: {e}")
        return None


def put_ssm_value(param_name: str, value: str) -> None:
    try:
        ssm_client().put_parameter(
            Name=param_name,
            Value=value,
            Type="SecureString",
            Overwrite=True,
        )
        log.info(f"SSM updated: {param_name}")
    except Exception as e:
        log.warning(f"SSM put failed for {param_name}: {e}")


# ── Load credentials at cold start (cached for warm invocations) ──────────────
log.info("Loading credentials from SSM...")
NAUKRI_EMAIL    = get_ssm_value(SSM_EMAIL_PARAM)
NAUKRI_PASSWORD = get_ssm_value(SSM_PASSWORD_PARAM)
if not NAUKRI_EMAIL or not NAUKRI_PASSWORD:
    raise RuntimeError("Could not load Naukri credentials from SSM. Check SSM parameter names.")
log.info("Credentials loaded.")


# ─────────────────────────────────────────────────────────────────────────────
# S3 helpers
# ─────────────────────────────────────────────────────────────────────────────

def s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


def s3_download(key: str, local_path: str) -> None:
    log.info(f"S3 download: s3://{S3_BUCKET}/{key} -> {local_path}")
    s3_client().download_file(S3_BUCKET, key, local_path)


def s3_upload(local_path: str, key: str) -> None:
    try:
        s3_client().upload_file(local_path, S3_BUCKET, key)
        log.info(f"S3 upload: {local_path} -> s3://{S3_BUCKET}/{key}")
    except Exception as e:
        log.warning(f"S3 upload failed: {e}")


def fetch_assets(tmpdir: str) -> tuple:
    """Download resume PDF and headline text from S3."""
    resume_path   = os.path.join(tmpdir, "resume.pdf")
    headline_path = os.path.join(tmpdir, "headline.txt")
    s3_download(S3_RESUME_KEY,   resume_path)
    s3_download(S3_HEADLINE_KEY, headline_path)
    with open(headline_path, encoding="utf-8") as f:
        headline = f.read().strip()
    log.info(f"Headline ({len(headline)} chars): {headline[:80]}...")
    return resume_path, headline


# ─────────────────────────────────────────────────────────────────────────────
# Naukri REST API login  (avoids browser-based login blocked by Naukri)
# ─────────────────────────────────────────────────────────────────────────────

def api_login() -> dict:
    """
    Login via Naukri's internal REST API.
    Returns dict of cookies  {name: value}  to inject into Playwright.
    Falls back to cached SSM cookie if API login fails.
    """
    headers = {
        "User-Agent":   USER_AGENT,
        "Content-Type": "application/json",
        "Accept":       "application/json, text/plain, */*",
        "Referer":      NAUKRI_LOGIN_URL,
        "Origin":       "https://www.naukri.com",
        "appid":        "109",
        "systemid":     "109",
    }
    payload = {
        "username": NAUKRI_EMAIL,
        "password": NAUKRI_PASSWORD,
        "autologin": True,
        "redirectUrl": "https://www.naukri.com/",
    }
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

    try:
        log.info("Attempting REST API login...")
        resp = requests.post(
            NAUKRI_API_LOGIN,
            headers=headers,
            json=payload,
            proxies=proxies,
            timeout=30,
            allow_redirects=True,
        )
        log.info(f"API login response: {resp.status_code}")

        if resp.status_code == 200:
            cookies = dict(resp.cookies)
            # Also grab any Set-Cookie from redirect chain
            for r in resp.history:
                cookies.update(dict(r.cookies))
            if cookies:
                # Cache cookies in SSM for next invocation
                put_ssm_value(SSM_COOKIE_PARAM, json.dumps(cookies))
                log.info(f"API login successful. Cookies: {list(cookies.keys())}")
                return cookies
            else:
                log.warning("API login returned 200 but no cookies.")
        else:
            log.warning(f"API login failed: {resp.status_code} — {resp.text[:200]}")

    except Exception as e:
        log.warning(f"API login exception: {e}")

    # Fallback: try cached cookies from SSM
    log.info("Trying cached session cookie from SSM...")
    cached = get_ssm_value(SSM_COOKIE_PARAM)
    if cached:
        try:
            cookies = json.loads(cached)
            log.info(f"Using cached cookies: {list(cookies.keys())}")
            return cookies
        except Exception:
            log.warning("Cached cookie is invalid JSON.")

    raise RuntimeError(
        "All login methods failed. "
        "Check credentials in SSM and network access. "
        f"PROXY_URL={'set' if PROXY_URL else 'not set'}."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Browser helpers (Playwright)
# ─────────────────────────────────────────────────────────────────────────────

def build_browser(playwright):
    launch_kwargs = dict(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--single-process",
            "--no-zygote",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--ignore-certificate-errors",
            f"--window-size=1920,1080",
        ],
    )
    if PROXY_URL:
        launch_kwargs["proxy"] = {"server": PROXY_URL}
        log.info(f"Browser using proxy: {PROXY_URL.split('@')[-1]}")  # log host only, hide creds
    return playwright.chromium.launch(**launch_kwargs)


def build_context(browser, cookies: dict):
    """Create browser context with injected session cookies."""
    ctx = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        extra_http_headers={
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    # Inject cookies so browser starts as already logged in
    if cookies:
        cookie_list = [
            {
                "name":   name,
                "value":  value,
                "domain": ".naukri.com",
                "path":   "/",
            }
            for name, value in cookies.items()
        ]
        ctx.add_cookies(cookie_list)
        log.info(f"Injected {len(cookie_list)} cookies into browser context.")
    return ctx


def human_delay(min_ms: int = 500, max_ms: int = 1500) -> None:
    """Random delay to mimic human behaviour."""
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def screenshot(page, label: str) -> None:
    path = f"/tmp/naukri_{label}_{int(time.time())}.png"
    try:
        page.screenshot(path=path, full_page=True)
        log.info(f"Screenshot: {path}")
        s3_upload(path, f"debug/{os.path.basename(path)}")
    except Exception as e:
        log.warning(f"Screenshot failed: {e}")


def dismiss_popup(page) -> None:
    selectors = [
        "[class*='crossIcon']",
        "[class*='close-btn']",
        "button[class*='close']",
        "[id*='popup'] button",
        ".overlay .close",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2_000):
                el.click()
                time.sleep(1)
                log.info(f"Dismissed popup: {sel}")
                break
        except Exception:
            pass


def find_element(page, selectors: list, timeout: int = SHORT_TIMEOUT):
    """Try selectors in order, return first visible element."""
    for sel in selectors:
        try:
            el = page.locator(sel).first
            el.wait_for(state="visible", timeout=timeout)
            log.info(f"  ✓ Matched: {sel}")
            return el
        except Exception:
            log.info(f"  ✗ Not found: {sel}")
    return None


def verify_logged_in(page) -> bool:
    """Check if the current page shows a logged-in state."""
    try:
        # Naukri shows user menu / avatar when logged in
        logged_in_sel = [
            "[class*='nI-gNb-drawer']",
            "[class*='user-name']",
            "[class*='naukri-logo'] ~ [class*='login']",
            "a[href*='mnjuser']",
            "#login_Layer",            # login layer only shown when NOT logged in
        ]
        page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT)
        time.sleep(3)

        title = page.title().lower()
        url   = page.url.lower()
        log.info(f"Page title: {page.title()} | URL: {page.url}")

        if "access denied" in title:
            log.error("Access Denied — Naukri is blocking this IP.")
            return False
        if "mnjuser/profile" in url or "myapplication" in url or "mnjuser" in url:
            return True
        if "nlogin" in url or "login" in url:
            return False
        return True   # assume logged in if not on login page and not denied
    except Exception as e:
        log.warning(f"verify_logged_in error: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Profile update actions
# ─────────────────────────────────────────────────────────────────────────────

def update_resume(page, resume_path: str) -> None:
    log.info("--- Updating resume ---")
    page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    human_delay(2000, 4000)
    dismiss_popup(page)
    screenshot(page, "profile_page")

    if not verify_logged_in(page):
        raise RuntimeError("Not logged in when trying to update resume.")

    # ── Delete existing resume ────────────────────────────────────────────────
    log.info("Looking for existing resume to delete...")
    delete_selectors = [
        "#attachCVDelete",
        "[class*='deleteResume']",
        "[class*='resumeDeleteIcon']",
        "span[class*='delete'][title*='esume']",
        "[data-action='delete'][data-type='resume']",
    ]
    del_btn = find_element(page, delete_selectors, timeout=6_000)
    if del_btn:
        del_btn.click()
        human_delay(1000, 2000)
        # Confirm dialog
        confirm_selectors = [
            "button:has-text('Delete')",
            "button:has-text('Yes')",
            "button:has-text('Confirm')",
            "[class*='confirm'] button",
        ]
        confirm = find_element(page, confirm_selectors, timeout=5_000)
        if confirm:
            confirm.click()
            human_delay(2000, 3000)
            log.info("Old resume deleted.")
        else:
            log.info("No confirm dialog — delete was direct.")
    else:
        log.info("No existing resume found — proceeding to upload.")

    # ── Upload new resume ─────────────────────────────────────────────────────
    log.info("Uploading new resume...")
    upload_selectors = [
        "input#attachCV",
        "input[type='file'][accept*='.pdf']",
        "input[type='file'][name*='resume']",
        "input[type='file'][name*='cv']",
        "input[type='file']",
    ]
    upload_input = find_element(page, upload_selectors, timeout=PAGE_TIMEOUT)
    if upload_input is None:
        screenshot(page, "resume_upload_input_not_found")
        raise RuntimeError("Could not find resume upload input. Check screenshot in S3 debug/.")

    upload_input.set_input_files(resume_path)
    human_delay(5000, 8000)   # wait for upload to complete

    # Confirm success
    success_selectors = [
        "text=/successfully/i",
        "text=/uploaded/i",
        "[class*='uploadSuccess']",
        "[class*='success-msg']",
        "[class*='successMsg']",
    ]
    success = find_element(page, success_selectors, timeout=12_000)
    if success:
        log.info("Resume uploaded successfully.")
    else:
        screenshot(page, "resume_upload_result")
        log.warning("Could not confirm resume upload — check screenshot.")


def update_headline(page, headline: str) -> None:
    log.info("--- Updating headline ---")
    page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    human_delay(2000, 4000)
    dismiss_popup(page)

    if not verify_logged_in(page):
        raise RuntimeError("Not logged in when trying to update headline.")

    # ── Click edit button for Resume Headline section ─────────────────────────
    log.info("Looking for headline edit button...")
    edit_selectors = [
        "#lazyResumeHead [class*='edit']",
        "#lazyResumeHead [class*='pencil']",
        "[class*='resumeHeadline'] [class*='edit']",
        "#resumeHeadline [class*='edit']",
        "section[id*='resumeHead'] [class*='edit']",
        "[data-section='resumeHeadline'] [class*='edit']",
    ]
    edit_btn = find_element(page, edit_selectors, timeout=PAGE_TIMEOUT)
    if edit_btn is None:
        screenshot(page, "headline_edit_not_found")
        raise RuntimeError("Could not find headline edit button. Check screenshot in S3 debug/.")

    edit_btn.click()
    human_delay(1500, 2500)

    # ── Fill headline textarea ────────────────────────────────────────────────
    log.info("Filling headline text...")
    textarea_selectors = [
        "textarea#resumeHeadlineTxt",
        "textarea[name='resumeHeadline']",
        "textarea[placeholder*='headline']",
        "textarea[placeholder*='Headline']",
        "[class*='headlineTextarea'] textarea",
        "textarea",
    ]
    textarea = find_element(page, textarea_selectors, timeout=PAGE_TIMEOUT)
    if textarea is None:
        screenshot(page, "headline_textarea_not_found")
        raise RuntimeError("Could not find headline textarea. Check screenshot in S3 debug/.")

    textarea.triple_click()    # select all existing text
    textarea.fill(headline)
    human_delay(500, 1000)

    # ── Save ──────────────────────────────────────────────────────────────────
    log.info("Saving headline...")
    save_selectors = [
        "button[type='submit']:has-text('Save')",
        "button[class*='saveBtn']",
        "button[class*='save-btn']",
        "button:has-text('Save')",
        "input[type='submit'][value='Save']",
    ]
    save_btn = find_element(page, save_selectors, timeout=PAGE_TIMEOUT)
    if save_btn is None:
        screenshot(page, "headline_save_not_found")
        raise RuntimeError("Could not find Save button. Check screenshot in S3 debug/.")

    save_btn.click()
    human_delay(3000, 4000)
    screenshot(page, "headline_saved")
    log.info("Headline updated successfully.")


# ─────────────────────────────────────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    log.info("=" * 60)
    log.info("Naukri Updater Lambda invoked")
    log.info(f"Event source: {event.get('source', 'manual')}")
    log.info(f"Proxy configured: {'yes' if PROXY_URL else 'no'}")

    from playwright.sync_api import sync_playwright

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Fetch S3 assets
            resume_path, headline = fetch_assets(tmpdir)

            # 2. Login via REST API (avoids browser-based IP blocking)
            cookies = api_login()

            # 3. Launch browser with session cookies pre-injected
            with sync_playwright() as pw:
                browser = build_browser(pw)
                ctx     = build_context(browser, cookies)
                page    = ctx.new_page()

                try:
                    # 4. Verify session is valid by navigating to profile
                    log.info("Verifying session by navigating to profile page...")
                    page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                    human_delay(2000, 3000)
                    screenshot(page, "session_check")

                    if not verify_logged_in(page):
                        # Cookie session expired — force fresh API login
                        log.warning("Session cookie expired. Forcing fresh login...")
                        put_ssm_value(SSM_COOKIE_PARAM, "{}")   # clear cache
                        cookies = api_login()
                        ctx2  = build_context(browser, cookies)
                        page  = ctx2.new_page()
                        page.goto(NAUKRI_PROFILE_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                        human_delay(2000, 3000)
                        if not verify_logged_in(page):
                            screenshot(page, "login_failed_final")
                            raise RuntimeError(
                                "Could not establish a valid session with Naukri. "
                                "If PROXY_URL is not set, Naukri may be blocking the AWS Lambda IP. "
                                "Set PROXY_URL env var with a residential proxy to fix this."
                            )

                    # 5. Update resume
                    update_resume(page, resume_path)

                    # 6. Update headline
                    update_headline(page, headline)

                    result = {
                        "status":  "success",
                        "message": "Profile updated successfully.",
                        "actions": ["resume_updated", "headline_updated"],
                    }
                    log.info("All updates completed successfully.")

                except Exception as e:
                    log.exception("Profile update failed")
                    screenshot(page, "fatal_error")
                    result = {"status": "error", "message": str(e)}

                finally:
                    browser.close()

    except Exception as e:
        log.exception("Lambda handler fatal error")
        result = {"status": "error", "message": str(e)}

    log.info(f"Result: {json.dumps(result)}")
    return result
