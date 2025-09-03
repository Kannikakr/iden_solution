# iden_scraper.py
# Requires: pip install playwright && playwright install
import os
import json
import time
import argparse
from pathlib import Path
from typing import List, Dict, Any, Optional

from playwright.sync_api import sync_playwright, expect, TimeoutError as PWTimeoutError


# ---------- Configuration defaults (override via CLI) ----------
DEFAULT_STORAGE = "storage_state.json"
DEFAULT_OUTFILE = "products.json"

# ---- Selectors (adjust if your app differs) ----
# Login page fields & submit
SEL_USERNAME = '[name="username"], input#username, input[name="email"]'
SEL_PASSWORD = '[name="password"], input#password'
SEL_LOGIN_BTN = 'button:has-text("Sign in"), button:has-text("Log in"), [type="submit"]'

# Global navigation
SEL_NAV_SUBMIT = 'nav >> text=Submit Script, a:has-text("Submit Script")'
SEL_REPO_INPUT = 'input[name="repo"], input[type="url"], input[placeholder*="GitHub"]'
SEL_REPO_SUBMIT_BTN = 'button:has-text("Submit"), [type="submit"]'

# Wizard
SEL_WIZARD_NEXT = 'button:has-text("Next")'
# Optional explicit step labels if present (not strictly needed, we still just click Next 4x)
WIZARD_STEP_LABELS = [
    "Select Data Source",
    "Choose Category",
    "Select View Type",
    "View Products",
]

# Product table (we try robust ARIA roles first, then fall back)
SEL_TABLE_ARIA = 'role=table'
SEL_TABLE_FALLBACK = 'table'
SEL_TABLE_CONTAINER = '[data-testid="table-container"], .table-container, .ag-center-cols-container'
SEL_PAGINATION_NEXT = 'button:has-text("Next"), [aria-label="Next"], .pagination-next'
SEL_PAGINATION_DISABLED = '[disabled], [aria-disabled="true"]'

# In case of infinite scroll, we’ll look for any scrollable container
SCROLLABLE_CANDIDATES = [
    SEL_TABLE_CONTAINER,
    ".ReactVirtualized__Grid",
    ".infinite-scroll-component",
    ".ag-body-viewport",
]

# ---------- Helpers ----------
def env_or(arg_val: Optional[str], env_key: str) -> Optional[str]:
    return arg_val if arg_val else os.getenv(env_key)

def smart_click(page, selector: str, timeout_ms: int = 10000):
    loc = page.locator(selector)
    expect(loc).to_be_visible(timeout=timeout_ms)
    loc.click()

def wait_for_any(page, selectors: List[str], timeout_ms: int = 10000):
    """Wait until any of the selectors appears; return the first that is visible."""
    end = time.time() + (timeout_ms / 1000)
    last_err = None
    while time.time() < end:
        for sel in selectors:
            try:
                if page.locator(sel).first.is_visible():
                    return sel
            except PWTimeoutError as e:
                last_err = e
        page.wait_for_timeout(200)
    if last_err:
        raise last_err
    raise PWTimeoutError(f"None of selectors appeared: {selectors}")

# ---------- Session & Login ----------
def try_reuse_session(pw, base_url: str, storage_path: str, headless: bool):
    """Return (context, page, reused: bool)."""
    browser = pw.chromium.launch(headless=headless)
    ctx = None
    if Path(storage_path).exists():
        ctx = browser.new_context(storage_state=storage_path)
        page = ctx.new_page()
        page.goto(base_url, wait_until="load")
        # Heuristic: if we can see a nav element or anything that implies we're logged in
        # Adjust this condition to your app (e.g., presence of user avatar)
        if page.get_by_text("Logout").first.is_visible(timeout=2000) if page.get_by_text("Logout") else False:
            return ctx, page, True
        # If not clearly logged in, still return so caller can decide to re-login in same browser
        return ctx, page, False
    else:
        ctx = browser.new_context()
        page = ctx.new_page()
        page.goto(base_url, wait_until="load")
        return ctx, page, False

def perform_login(page, username: str, password: str, storage_path: str):
    # Wait for username & password fields
    user_field = page.locator(SEL_USERNAME).first
    pass_field = page.locator(SEL_PASSWORD).first
    expect(user_field).to_be_visible(timeout=15000)
    expect(pass_field).to_be_visible(timeout=15000)

    user_field.fill(username)
    pass_field.fill(password)

    # Click login
    page.locator(SEL_LOGIN_BTN).first.click()

    # Wait for a post-login signal: adjust to your app, maybe URL change or a nav element appears
    page.wait_for_load_state("networkidle")
    # Optional robust check: wait for something that exists only if logged in
    # If the app has a specific dashboard element, replace the line below with that selector
    page.wait_for_timeout(1000)  # small settle

    # Save storage
    page.context.storage_state(path=storage_path)

# ---------- Wizard Navigation ----------
def complete_wizard(page, next_selector: str = SEL_WIZARD_NEXT, clicks: int = 4):
    for i in range(clicks):
        # If the step label exists, wait for it (non-fatal if missing)
        if i < len(WIZARD_STEP_LABELS):
            try:
                step_label = WIZARD_STEP_LABELS[i]
                label_loc = page.get_by_text(step_label).first
                if label_loc.is_visible(timeout=2000):
                    pass  # label found; just proceed
            except Exception:
                pass
        # Click Next
        smart_click(page, next_selector, timeout_ms=15000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(300)  # small settle between steps

# ---------- Table Extraction ----------
def extract_table_rows(page) -> List[Dict[str, Any]]:
    """
    Attempts to read a data table generically:
    1) Prefer role=table
    2) Fallback to <table>
    Reads headers, then rows/cells.
    """
    rows_data: List[Dict[str, Any]] = []

    table = None
    if page.locator(SEL_TABLE_ARIA).first.count() > 0 and page.locator(SEL_TABLE_ARIA).first.is_visible():
        table = page.locator(SEL_TABLE_ARIA).first
    elif page.locator(SEL_TABLE_FALLBACK).first.count() > 0 and page.locator(SEL_TABLE_FALLBACK).first.is_visible():
        table = page.locator(SEL_TABLE_FALLBACK).first

    if table:
        # Try evaluate headers
        try:
            headers = table.locator("thead tr th, thead th, [role='columnheader']").all_text_contents()
            headers = [h.strip() or f"col_{i}" for i, h in enumerate(headers)]
        except Exception:
            headers = []

        # If no headers, try first row as headers
        if not headers:
            try:
                first_row_cells = table.locator("tr").nth(0).locator("th, td, [role='cell']").all_text_contents()
                headers = [c.strip() or f"col_{i}" for i, c in enumerate(first_row_cells)]
                row_start_idx = 1
            except Exception:
                headers = []
                row_start_idx = 0
        else:
            row_start_idx = 1  # likely header exists

        # Iterate rows
        row_count = table.locator("tbody tr, tr[role='row']").count()
        if row_count == 0:
            row_count = table.locator("tr").count()

        for r in range(row_start_idx, row_count):
            cells = table.locator("tbody tr, tr").nth(r).locator("th, td, [role='cell']").all_text_contents()
            # Normalize lengths
            if headers and len(cells) != len(headers):
                # pad/truncate
                if len(cells) < len(headers):
                    cells += [""] * (len(headers) - len(cells))
                else:
                    cells = cells[: len(headers)]
            if headers:
                row = {headers[i]: cells[i].strip() if i < len(cells) else "" for i in range(len(headers))}
            else:
                row = {f"col_{i}": (cells[i].strip() if i < len(cells) else "") for i in range(len(cells))}
            rows_data.append(row)

    return rows_data

def has_next_page(page) -> bool:
    next_btn = page.locator(SEL_PAGINATION_NEXT).first
    if next_btn.count() == 0:
        return False
    if next_btn.is_disabled():
        return False
    # disabled via attribute/class?
    try:
        if next_btn.locator(SEL_PAGINATION_DISABLED).count() > 0:
            return False
    except Exception:
        pass
    # Some UIs hide next when last page is reached
    return next_btn.is_visible()

def go_next_page(page):
    page.locator(SEL_PAGINATION_NEXT).first.click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(300)

def harvest_all_pages(page) -> List[Dict[str, Any]]:
    """Handles both classic pagination and infinite scroll / virtualized tables."""
    all_rows: List[Dict[str, Any]] = []
    seen_hashes = set()

    def add_rows(new_rows: List[Dict[str, Any]]):
        nonlocal all_rows, seen_hashes
        for row in new_rows:
            # Deduplicate by hashing the row dict
            h = json.dumps(row, sort_keys=True)
            if h not in seen_hashes:
                seen_hashes.add(h)
                all_rows.append(row)

    # First try classic pagination
    add_rows(extract_table_rows(page))
    paginated = False
    # Attempt a few pages
    for _ in range(200):  # safety cap
        if has_next_page(page):
            paginated = True
            go_next_page(page)
            add_rows(extract_table_rows(page))
        else:
            break

    if paginated:
        return all_rows

    # Try infinite scroll / lazy load
    # We’ll scroll the most likely container, falling back to window
    containers = [c for c in SCROLLABLE_CANDIDATES if page.locator(c).first.count() > 0]
    containers = containers or ["body"]

    previous_count = -1
    idle_rounds = 0
    for _ in range(100):  # safety cap
        add_rows(extract_table_rows(page))
        current_count = len(all_rows)
        if current_count == previous_count:
            idle_rounds += 1
        else:
            idle_rounds = 0
