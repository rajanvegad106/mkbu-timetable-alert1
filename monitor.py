
import os
import re
import json
import hashlib
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

OFFICIAL_HOME = "https://www.mkbhavuni.edu.in/mkbhavuniweb/"
STATE_FILE = Path("data/seen.json")

TIMEOUT = 10
RETRIES = 2
MAX_PDF_MB = 45

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MKBUTimetableMonitor/1.0)"
}

session = requests.Session()
session.headers.update(HEADERS)


def fetch_page(url):
    last_error = None

    for attempt in range(RETRIES + 1):
        try:
            response = session.get(
                url,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < RETRIES:
                time.sleep(2)

    raise RuntimeError(
        f"Could not open {url} after retries: {last_error}"
    )


def telegram_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = session.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram message was not accepted")


def telegram_pdf(content, filename, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    response = session.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "caption": caption[:900],
        },
        files={
            "document": (filename, content, "application/pdf")
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    if not response.json().get("ok"):
        raise RuntimeError("Telegram PDF was not accepted")


def allowed_official_url(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    return (
        parsed.scheme in ("http", "https")
        and (
            host == "mkbhavuni.edu.in"
            or host.endswith(".mkbhavuni.edu.in")
        )
    )


def looks_like_timetable(text, url):
    combined = f"{text} {url}".lower()
    combined = combined.replace("_", " ").replace("%20", " ")

    return bool(re.search(
        r"time[\s-]*table|timetable|exam[\s-]*"
        r"(schedule|programme|program|date)|"
        r"examination[\s-]*(schedule|programme|program)|"
        r"b\.?\s?sc|bachelor of science",
        combined,
    ))


def collect_timetable_links():
    response = fetch_page(OFFICIAL_HOME)
    found = {}
    pages_to_scan = [(response.url, response.text)]

    soup = BeautifulSoup(response.text, "html.parser")

    for a in soup.find_all("a", href=True):
        title = a.get_text(" ", strip=True)
        url = urljoin(response.url, a["href"]).split("#")[0]

        if not allowed_official_url(url):
            continue

        if not looks_like_timetable(title, url):
            continue

        found[url] = title or url

        if (
            not url.lower().split("?")[0].endswith(".pdf")
            and len(pages_to_scan) < 12
        ):
            try:
                page = fetch_page(url)
                content_type = page.headers.get(
                    "Content-Type", ""
                ).lower()

                if (
                    "html" in content_type
                    or "<html" in page.text[:500].lower()
                ):
                    pages_to_scan.append((page.url, page.text))
            except Exception as exc:
                print(f"Could not scan page {url}: {exc}")

    for page_url, html in pages_to_scan[1:]:
        page_soup = BeautifulSoup(html, "html.parser")

        for a in page_soup.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            url = urljoin(page_url, a["href"]).split("#")[0]

            if (
                allowed_official_url(url)
                and looks_like_timetable(title, url)
            ):
                found[url] = title or url

    return found


def fetch_resource(url):
    response = fetch_page(url)
    body = response.content
    content_type = response.headers.get("Content-Type", "").lower()

    is_pdf = (
        response.url.lower().split("?")[0].endswith(".pdf")
        or url.lower().split("?")[0].endswith(".pdf")
        or "application/pdf" in content_type
        or body[:5] == b"%PDF-"
    )

    digest = hashlib.sha256(body).hexdigest()

    return response.url, body, is_pdf, digest


def load_previous():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    previous = load_previous()

    # If the main website is unavailable, preserve history.
    try:
        links = collect_timetable_links()
    except Exception as exc:
        print(f"MKBU website unavailable or under maintenance: {exc}")
        print("Previous data preserved. Will retry next scheduled run.")
        return

    current = {}
    changed = []

    for url, title in links.items():
        try:
            final_url, body, is_pdf, digest = fetch_resource(url)

            current[url] = {
                "title": title,
                "hash": digest,
                "final_url": final_url,
                "is_pdf": is_pdf,
            }

            if url in previous:
                if previous[url].get("hash") != digest:
                    changed.append((title, final_url, body, is_pdf))
            elif previous:
                changed.append((title, final_url, body, is_pdf))

        except Exception as exc:
            print(f"Could not check {url}: {exc}")

    # Do not overwrite good history if all resource checks failed.
    if links and not current:
        print("All resource checks failed. Previous data preserved.")
        return

    # On the first successful scan, establish a baseline.
    if not previous:
        STATE_FILE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        telegram_message(
            "MKBU timetable monitor connected.\n"
            f"Official source: {OFFICIAL_HOME}\n"
            f"Timetable-related links found: {len(current)}\n"
            "Initial baseline saved. Future detected changes will trigger alerts."
        )

        print(f"Baseline saved: {len(current)} links")
        return

    # Send detected updates before saving the new history.
    for title, url, body, is_pdf in changed:
        caption = (
            f"MKBU timetable update\n{title}\n"
            f"Official source: {url}"
        )

        if is_pdf and len(body) <= MAX_PDF_MB * 1024 * 1024:
            filename = Path(urlparse(url).path).name
            if not filename.lower().endswith(".pdf"):
                filename = "MKBU_Timetable.pdf"

            telegram_pdf(body, filename, caption)
        else:
            telegram_message(caption)

    STATE_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"Checked {len(current)} links; "
        f"changes detected: {len(changed)}"
    )


if __name__ == "__main__":
    main()
