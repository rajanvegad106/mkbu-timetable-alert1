import os
import re
import json
import hashlib
from pathlib import Path
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

BASE_URL = "https://www.mkbhavuni.edu.in/mkbhavuniweb/"
SITEMAP_URL = "https://www.mkbhavuni.edu.in/sitemap.xml"
STATE_FILE = Path("data/site_seen.json")
TIMEOUT = 25
MAX_PAGES = 100
MAX_FILES = 300
MAX_FILE_MB = 45
MAX_ALERTS = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MKBUWebsiteMonitor/1.0)"
}
session = requests.Session()
session.headers.update(HEADERS)

DOC_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".csv"
}


def is_official(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme in ("http", "https")
        and (host == "mkbhavuni.edu.in" or host.endswith(".mkbhavuni.edu.in"))
    )


def clean_url(url):
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def send_telegram(message):
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


def load_state():
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(value):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def file_extension(url):
    return Path(urlparse(url).path).suffix.lower()


def page_hash(html):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover():
    queue = deque([(clean_url(BASE_URL), 0),
                   ("https://www.mkbhavuni.edu.in/mkbhavuniweb/timetable.php", 0)])
    seen_pages = set()
    pages = {}
    files = {}

    # Sitemap is an additional discovery source; failures are non-fatal.
    try:
        response = session.get(SITEMAP_URL, timeout=TIMEOUT)
        if response.ok and "xml" in response.headers.get("content-type", "").lower():
            xml = BeautifulSoup(response.text, "xml")
            for loc in xml.find_all("loc"):
                candidate = clean_url(loc.get_text(strip=True))
                if is_official(candidate):
                    if file_extension(candidate) in DOC_EXTENSIONS:
                        files[candidate] = Path(urlparse(candidate).path).name
                    elif len(queue) < MAX_PAGES:
                        queue.append((candidate, 0))
    except Exception as exc:
        print(f"Sitemap unavailable: {exc}")

    while queue and len(seen_pages) < MAX_PAGES and len(files) < MAX_FILES:
        url, depth = queue.popleft()
        if url in seen_pages or not is_official(url):
            continue
        if file_extension(url) in DOC_EXTENSIONS:
            files[url] = Path(urlparse(url).path).name or url
            continue

        seen_pages.add(url)
        try:
            response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            final_url = clean_url(response.url)
            if not is_official(final_url):
                continue
            content_type = response.headers.get("content-type", "").lower()
            if "html" not in content_type and "text/" not in content_type:
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            title = soup.title.get_text(" ", strip=True) if soup.title else final_url
            pages[final_url] = {
                "title": title[:180],
                "hash": page_hash(response.text),
            }

            for anchor in soup.find_all("a", href=True):
                target = clean_url(urljoin(final_url, anchor["href"].strip()))
                if not is_official(target):
                    continue
                ext = file_extension(target)
                label = anchor.get_text(" ", strip=True) or Path(urlparse(target).path).name or target
                if ext in DOC_EXTENSIONS:
                    if len(files) < MAX_FILES:
                        files[target] = label[:180]
                elif depth < 2 and target not in seen_pages and len(queue) < MAX_PAGES * 3:
                    queue.append((target, depth + 1))
        except Exception as exc:
            print(f"Page skipped: {url} ({exc})")

    return pages, files


def hash_file(url):
    response = session.get(url, timeout=TIMEOUT, allow_redirects=True, stream=True)
    response.raise_for_status()
    if not is_official(response.url):
        raise RuntimeError("File redirected outside MKBU domain")

    digest = hashlib.sha256()
    size = 0
    for chunk in response.iter_content(chunk_size=1024 * 128):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_FILE_MB * 1024 * 1024:
            raise RuntimeError("File exceeds configured size limit")
        digest.update(chunk)
    return digest.hexdigest()


def main():
    previous = load_state()
    try:
        pages, files = discover()
    except Exception as exc:
        print(f"MKBU discovery failed: {exc}")
        return

    current = {}
    for url, item in pages.items():
        current["page:" + url] = {
            "title": item["title"], "hash": item["hash"], "url": url,
            "kind": "page",
        }

    for url, title in files.items():
        try:
            current["file:" + url] = {
                "title": title, "hash": hash_file(url), "url": url,
                "kind": "file",
            }
        except Exception as exc:
            print(f"File skipped: {url} ({exc})")

    if not current:
        print("No MKBU pages/files verified; previous history preserved.")
        return

    # First run establishes a quiet baseline and avoids a burst of old alerts.
    if not previous:
        save_state(current)
        send_telegram(
            "MKBU full website monitor connected.\n"
            f"Pages checked: {len(pages)}\n"
            f"Documents checked: {len(files)}\n"
            "Initial baseline saved. Future changes and new uploads will alert here."
        )
        print(f"Initial website baseline saved: {len(current)} items")
        return

    alerts = []
    for key, item in current.items():
        old = previous.get(key)
        if old is None:
            alerts.append(("NEW", item))
        elif old.get("hash") != item.get("hash"):
            alerts.append(("UPDATED", item))

    # Notify first; persist only after notifications have been attempted.
    for status, item in alerts[:MAX_ALERTS]:
        label = "New upload" if status == "NEW" and item["kind"] == "file" else (
            "Updated document" if item["kind"] == "file" else
            ("New page" if status == "NEW" else "Page changed")
        )
        send_telegram(
            f"MKBU WEBSITE ALERT\n{label}\n"
            f"{item['title']}\n{item['url']}"
        )

    # Retain prior entries if a page/file was temporarily unreachable.
    merged = dict(previous)
    merged.update(current)
    save_state(merged)

    print(
        f"Pages checked: {len(pages)}; documents checked: {len(files)}; "
        f"alerts: {len(alerts)}"
    )
    if len(alerts) > MAX_ALERTS:
        print(f"{len(alerts) - MAX_ALERTS} more alerts remain for a later run.")


if __name__ == "__main__":
    main()
