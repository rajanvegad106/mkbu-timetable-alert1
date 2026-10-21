
import os
import re
import json
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TIMETABLE_PAGE = (
    "https://www.mkbhavuni.edu.in/mkbhavuniweb/timetable.php"
)

STATE_FILE = Path("data/seen.json")
TIMEOUT = 8
MAX_PDF_MB = 45
MAX_LINKS_PER_RUN = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MKBUTimetableMonitor/1.0)"
}

session = requests.Session()
session.headers.update(HEADERS)


def official_url(url):
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return (
        p.scheme in ("http", "https")
        and (
            host == "mkbhavuni.edu.in"
            or host.endswith(".mkbhavuni.edu.in")
        )
    )


def telegram_message(message):
    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={
            "chat_id": CHAT_ID,
            "text": message[:4000],
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram message failed")


def telegram_pdf(body, filename, caption):
    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
        data={
            "chat_id": CHAT_ID,
            "caption": caption[:900],
        },
        files={
            "document": (filename, body, "application/pdf")
        },
        timeout=25,
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram PDF delivery failed")


def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_timetable_links():
    response = session.get(
        TIMETABLE_PAGE,
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    if not official_url(response.url):
        raise RuntimeError("Timetable page redirected outside MKBU domain")

    soup = BeautifulSoup(response.text, "html.parser")
    links = {}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        url = urljoin(response.url, href).split("#")[0]
        title = a.get_text(" ", strip=True) or url
        combined = f"{title} {url}".lower()

        if not official_url(url):
            continue

        is_pdf = (
            urlparse(url).path.lower().endswith(".pdf")
            or ".pdf?" in url.lower()
        )

        relevant = bool(re.search(
            r"timetable|time[\s_-]*table|b\.?\s*sc|"
            r"bachelor|semester|exam|examination|schedule|"
            r"programme|program|practical",
            combined,
        ))

        if is_pdf and relevant:
            links[url] = title

    # Avoid a false empty baseline during maintenance or page errors.
    if not links:
        raise RuntimeError(
            "No relevant PDF links found on timetable page; "
            "previous history preserved."
        )

    return dict(list(links.items())[:MAX_LINKS_PER_RUN])


def download_pdf(url):
    response = session.get(
        url,
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    if not official_url(response.url):
        raise RuntimeError("PDF redirected outside MKBU domain")

    body = response.content

    if not body.startswith(b"%PDF-"):
        raise RuntimeError("Link did not return a valid PDF")

    if len(body) > MAX_PDF_MB * 1024 * 1024:
        raise RuntimeError("PDF exceeds configured size limit")

    return body, hashlib.sha256(body).hexdigest(), response.url


def main():
    previous = load_state()

    # If the official page is unavailable, keep existing history.
    try:
        links = get_timetable_links()
    except Exception as exc:
        print(f"Timetable page unavailable: {exc}")
        print("Previous history preserved; next run will retry.")
        return

    current = {}
    alerts = []

    for url, title in links.items():
        try:
            body, digest, final_url = download_pdf(url)

            current[url] = {
                "title": title,
                "hash": digest,
                "final_url": final_url,
            }

            old = previous.get(url)

            # First run creates a baseline without sending old PDFs.
            if previous and (
                old is None or old.get("hash") != digest
            ):
                alerts.append((title, final_url, body))

        except Exception as exc:
            print(f"Skipped unavailable PDF: {url} ({exc})")
            time.sleep(0.2)

    # Do not erase good history if every PDF failed.
    if not current:
        print("No PDFs could be verified; previous history preserved.")
        return

    # Keep old entries that could not be checked this run.
    merged = dict(previous)
    merged.update(current)

    if not previous:
        save_state(current)
        telegram_message(
            "MKBU timetable monitor connected.\n"
            f"Source: {TIMETABLE_PAGE}\n"
            f"Valid PDFs checked: {len(current)}\n"
            "Initial baseline saved. Future detected changes will alert."
        )
        print(f"Initial baseline saved: {len(current)} PDFs")
        return

    # Deliver alerts before updating stored hashes.
    for title, url, body in alerts:
        caption = (
            f"MKBU timetable update\n{title}\n"
            f"Official source: {url}"
        )
        filename = Path(urlparse(url).path).name or "MKBU_Timetable.pdf"

        try:
            telegram_pdf(body, filename, caption)
        except Exception as exc:
            print(f"PDF delivery failed: {exc}")
            telegram_message(caption)

    save_state(merged)
    print(
        f"Valid PDFs checked: {len(current)}; "
        f"updates detected: {len(alerts)}"
    )


if __name__ == "__main__":
    main()
