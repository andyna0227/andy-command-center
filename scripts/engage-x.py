#!/usr/bin/env python3
"""Automates daily X (Twitter) engagement for @thetypesetterr."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import google.generativeai as genai
from playwright.sync_api import Error, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PROFILE_PATH = os.path.expanduser("~/.openclaw/browser/openclaw")
ENGAGEMENT_LOG = Path("~/.openclaw/workspace/engagement-log.md").expanduser()
MAX_REPLIES_PER_RUN = 5
SEARCH_URL = (
    "https://x.com/search?q=%23buildinpublic+AI+min_faves%3A50&f=live&src=typed_query"
)
TARGET_ACCOUNTS = ["levelsio", "marc_louvion", "patio11"]
GEMINI_API_KEY = "AIzaSyA1ul_iq4oc4W3MHpnU35Mod6hGUEYXuXc"
GEMINI_MODEL = "gemini-2.0-flash"
EASTERN = ZoneInfo("America/New_York")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reply to relevant X posts")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions instead of posting replies",
    )
    return parser.parse_args()


def ensure_log_for_today() -> None:
    today_header = f"## {dt.datetime.now(EASTERN).date().isoformat()}"
    if not ENGAGEMENT_LOG.exists():
        ENGAGEMENT_LOG.write_text(f"{today_header}\n", encoding="utf-8")
        return
    contents = ENGAGEMENT_LOG.read_text(encoding="utf-8")
    if today_header not in contents:
        with ENGAGEMENT_LOG.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n{today_header}\n")


def load_logged_urls() -> set[str]:
    if not ENGAGEMENT_LOG.exists():
        return set()
    urls = set(
        re.findall(r"https://x.com/[^\s]+", ENGAGEMENT_LOG.read_text(encoding="utf-8"))
    )
    return urls


def append_log_entry(handle: str, url: str, reply: str) -> None:
    ensure_log_for_today()
    entry = f"- **X** | @{handle} | {url} | Reply: \"{reply}\"\n"
    with ENGAGEMENT_LOG.open("a", encoding="utf-8") as log_file:
        log_file.write(entry)


def configure_gemini():
    genai.configure(api_key=GEMINI_API_KEY)
    return genai.GenerativeModel(GEMINI_MODEL)


def generate_reply(model, tweet_text: str) -> Optional[str]:
    persona = (
        "You are Jay Frey — a nail salon owner in New Jersey who taught himself to build AI software. "
        "Your X handle is @thetypesetterr. You build AI tools and write about prompting. "
        "Generate a reply to this tweet that is: 1-2 sentences max, genuine, adds real value, "
        "no self-promotion, natural human voice. Sometimes ask a follow-up question.\n"
        f"Tweet: {tweet_text}\n"
        "Reply (just the reply text, nothing else):"
    )
    try:
        response = model.generate_content(persona)
    except Exception as exc:  # noqa: BLE001
        print(f"[gemini] Failed to generate reply: {exc}", file=sys.stderr)
        return None
    reply_text = (response.text or "").strip()
    if not reply_text:
        print("[gemini] Empty reply returned", file=sys.stderr)
        return None
    return reply_text


def wait_for_feed(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PlaywrightTimeoutError:
        pass
    try:
        page.wait_for_selector("article[data-testid='tweet']", timeout=20000)
    except PlaywrightTimeoutError:
        print('[scrape] tweet selector timeout; continuing', file=sys.stderr)
    page.wait_for_timeout(2000)


def parse_article(article) -> Optional[Dict[str, str]]:
    url_element = article.query_selector("a[href*='/status/']")
    text_element = article.query_selector("div[data-testid='tweetText']")
    handle_element = article.query_selector("div[data-testid='User-Names'] a div span")
    time_element = article.query_selector("time")
    if not (url_element and text_element and handle_element and time_element):
        return None
    url = url_element.get_attribute("href")
    if not url:
        return None
    if url.startswith("/"):
        url = f"https://x.com{url}"
    text = text_element.inner_text().strip()
    handle = handle_element.inner_text().strip().lstrip("@")
    timestamp = time_element.get_attribute("datetime") or ""
    return {"url": url, "text": text, "handle": handle, "timestamp": timestamp}


def scrape_search_posts(page: Page, limit: int = 8) -> List[Dict[str, str]]:
    print(f"[scrape] Loading search feed: {SEARCH_URL}")
    page.goto(SEARCH_URL, wait_until="domcontentloaded")
    wait_for_feed(page)
    articles = page.query_selector_all("article[data-testid='tweet']")
    posts: List[Dict[str, str]] = []
    for article in articles:
        post = parse_article(article)
        if post:
            posts.append(post)
        if len(posts) >= limit:
            break
    print(f"[scrape] Collected {len(posts)} posts from search")
    return posts


def scrape_account_posts(context, handle: str, today: dt.date) -> List[Dict[str, str]]:
    url = f"https://x.com/{handle}"
    page = context.new_page()
    try:
        print(f"[scrape] Checking @{handle} timeline")
        page.goto(url, wait_until="domcontentloaded")
        wait_for_feed(page)
        posts = []
        for article in page.query_selector_all("article[data-testid='tweet']"):
            post = parse_article(article)
            if not post:
                continue
            timestamp = post.get("timestamp")
            if not timestamp:
                continue
            try:
                post_dt = dt.datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                ).astimezone(EASTERN)
            except ValueError:
                continue
            if post_dt.date() == today:
                posts.append(post)
        return posts
    finally:
        page.close()


def post_reply(page: Page, post: Dict[str, str], reply_text: str) -> bool:
    print(f"[reply] Navigating to {post['url']}")
    page.goto(post["url"], wait_until="domcontentloaded")
    try:
        page.wait_for_selector("[data-testid='reply']", timeout=15000)
        page.get_by_test_id("reply").first.click()
        box = page.get_by_test_id("tweetTextarea_0")
        box.click()
        box.fill("")
        box.type(reply_text, delay=25)
        submit = page.locator("[data-testid='tweetButton']")
        submit.click()
        page.wait_for_timeout(3000)
        return True
    except PlaywrightTimeoutError:
        print("[reply] Timed out interacting with reply UI", file=sys.stderr)
    except Error as exc:
        print(f"[reply] Playwright error: {exc}", file=sys.stderr)
    return False


def main() -> None:
    args = parse_args()
    ensure_log_for_today()
    already_replied = load_logged_urls()
    today_date = dt.datetime.now(EASTERN).date()
    model = configure_gemini()

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_PATH,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        search_page = browser.new_page()
        posts = scrape_search_posts(search_page)
        search_page.close()

        for handle in TARGET_ACCOUNTS:
            posts.extend(scrape_account_posts(browser, handle, today_date))

        unique_posts: Dict[str, Dict[str, str]] = {}
        for post in posts:
            unique_posts.setdefault(post["url"], post)
        posts_to_consider = list(unique_posts.values())

        actions = []
        for post in posts_to_consider:
            if len(actions) >= MAX_REPLIES_PER_RUN:
                break
            if post["url"] in already_replied:
                print(f"[skip] Already engaged: {post['url']}")
                continue
            reply_text = generate_reply(model, post["text"])
            if not reply_text:
                continue
            actions.append((post, reply_text))

        print(f"[plan] Prepared {len(actions)} engagements (max {MAX_REPLIES_PER_RUN})")

        if args.dry_run:
            for post, reply_text in actions:
                print(f"[dry-run] @{post['handle']} | {post['url']}\n  -> {reply_text}\n")
            browser.close()
            return

        page = browser.new_page()
        replies_posted = 0
        for post, reply_text in actions:
            success = post_reply(page, post, reply_text)
            if success:
                replies_posted += 1
                append_log_entry(post["handle"], post["url"], reply_text)
                print(f"[done] Replied to {post['url']}")
            if replies_posted >= MAX_REPLIES_PER_RUN:
                break
        page.close()
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
