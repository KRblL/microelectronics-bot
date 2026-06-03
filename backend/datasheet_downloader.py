import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiofiles
import aiohttp
from bs4 import BeautifulSoup


BASE_URL = "https://www.alldatasheet.com"
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "data/datasheets"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value or "component")


def extract_doc_id(download_url: str) -> str | None:
    match = re.search(r"/download/(\d+)/", download_url or "")
    return match.group(1) if match else None


def to_direct_download(url: str) -> str:
    return re.sub(
        r"^https?://www\.alldatasheet\.com/datasheet-pdf/download/",
        "https://pdf1.alldatasheet.com/datasheet-pdf/download/",
        url,
        flags=re.IGNORECASE,
    )


def extract_security_form(html: str, base_url: str) -> tuple[str | None, dict[str, Any] | None]:
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form", id="frmDn")
    if form is None:
        for item in soup.find_all("form"):
            action = item.get("action") or ""
            if "/datasheet-pdf/download/" in action:
                form = item
                break
    if form is None:
        return None, None

    digits = []
    for td in form.find_all("td"):
        text = td.get_text(strip=True)
        if len(text) == 1 and text.isdigit():
            digits.append(text)
    if len(digits) < 5:
        digits = [char for char in form.get_text(" ", strip=True) if char.isdigit()]
    code = "".join(digits)

    payload: dict[str, Any] = {}
    text_input_name = None
    input_tag = form.find("input", attrs={"name": "innum"})
    if input_tag is not None:
        text_input_name = "innum"

    for item in form.find_all("input"):
        name = item.get("name")
        if not name:
            continue
        item_type = (item.get("type") or "").lower()
        if item_type in ("hidden", "submit"):
            payload[name] = item.get("value", "")
        elif item_type in ("text", "tel", "") and text_input_name is None:
            text_input_name = name

    if not text_input_name or not code:
        return None, None

    payload[text_input_name] = code
    action = form.get("action") or ""
    return urljoin(base_url, action), payload


async def search_datasheet(part_number: str) -> dict[str, Any]:
    query = part_number.strip()
    url = f"{BASE_URL}/view.jsp?Searchword={query}"
    headers = {"User-Agent": "Mozilla/5.0"}
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.get(url, allow_redirects=True) as response:
            if response.status != 200:
                return {"status": "error", "items": []}
            html = await response.text()

    if "No Data Available" in html:
        return {"status": "not_found", "items": []}

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []
    last_manufacturer = None

    for row in soup.select("table.main tr.nv_td"):
        cols = row.find_all("td")
        if len(cols) == 4:
            manufacturer = cols[0].get_text(strip=True)
            part_col = cols[1]
            description_col = cols[3]
            last_manufacturer = manufacturer
        elif len(cols) == 3 and last_manufacturer:
            manufacturer = last_manufacturer
            part_col = cols[0]
            description_col = cols[2]
        else:
            continue

        part = part_col.get_text(strip=True)
        description = description_col.get_text(" ", strip=True)
        link_tag = part_col.find("a", href=True)
        download_link = None

        if link_tag:
            href = link_tag["href"]
            if "/download/" in href:
                download_link = "https:" + href if href.startswith("//") else href
            else:
                value = href.replace("/pdf/", "/download/")
                download_link = "https:" + value if value.startswith("//") else value

        if part.upper() == query.upper() and download_link:
            results.append(
                {
                    "manufacturer": manufacturer,
                    "part": part,
                    "description": description,
                    "link": download_link,
                }
            )

    if not results:
        return {"status": "not_found", "items": []}
    return {"status": "ok", "items": results}


async def download_pdf(download_url: str, part_number: str, manufacturer: str) -> str | None:
    doc_id = extract_doc_id(download_url) or str(int(time.time()))
    filename = f"{safe_filename(part_number)}_{safe_filename(manufacturer)}_{doc_id}.pdf"
    filepath = DOWNLOAD_DIR / filename
    page_url = re.sub(r"^https?://pdf\d+\.", "https://www.", download_url, flags=re.IGNORECASE)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": page_url,
        "Origin": BASE_URL,
    }
    timeout = aiohttp.ClientTimeout(total=180)

    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar()) as session:
        for attempt in range(5):
            try:
                async with session.get(page_url, headers=headers, allow_redirects=True) as response:
                    html = await response.text()
                    if response.status in (429, 500, 502, 503, 504):
                        await asyncio.sleep(5 * (2**attempt))
                        continue

                action_url, payload = extract_security_form(html, page_url)
                if action_url and payload:
                    async with session.post(action_url, data=payload, headers=headers, allow_redirects=True) as response:
                        content = await response.read()
                        content_type = response.headers.get("Content-Type", "").lower()
                        if response.status == 200 and ("application/pdf" in content_type or content.startswith(b"%PDF")):
                            async with aiofiles.open(filepath, "wb") as file:
                                await file.write(content)
                            return str(filepath)

                direct_url = to_direct_download(download_url)
                async with session.post(direct_url, data={"tmpinfo1aa": "abc"}, headers=headers, allow_redirects=True) as response:
                    content = await response.read()
                    content_type = response.headers.get("Content-Type", "").lower()
                    if response.status == 200 and ("application/pdf" in content_type or content.startswith(b"%PDF")):
                        async with aiofiles.open(filepath, "wb") as file:
                            await file.write(content)
                        return str(filepath)

                await asyncio.sleep(5 * (2**attempt))
            except aiohttp.ClientError:
                await asyncio.sleep(5 * (2**attempt))
    return None
