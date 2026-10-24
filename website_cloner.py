#!/usr/bin/env python3
"""
Website Cloner
Downloads HTML, CSS, JavaScript, images, fonts, and other assets while
preserving URL paths. It also scans downloaded JavaScript for common
relative/root-relative JS and CSS dependencies (including many Vite chunk maps).

Usage:
    python website_cloner.py URL [--output DIR] [--depth N]
                                [--single-page] [--no-media]

Requirements:
    pip install requests beautifulsoup4 lxml

Only clone websites you own or have permission to archive. This is a static
downloader, not a full browser; runtime-generated URLs may not be discovered.
"""

import argparse
import logging
import mimetypes
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("website_cloner")

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".flac",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".gz", ".tar", ".rar",
}

CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")\s]+?)['"]?\s*\)""", re.I)
CSS_IMPORT_RE = re.compile(r"""@import\s+(?:url\(\s*)?['"]([^'"]+)['"]""", re.I)
# Matches common quoted module paths and Vite's generated chunk dependency maps.
JS_ASSET_RE = re.compile(
    r"""["']((?:\.\.?/|/)[^"'?#]+\.(?:js|mjs|css|wasm|json|woff2?|ttf|svg|png|jpe?g|webp))["']""",
    re.I,
)


def url_to_dirname(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return sanitize_filename(parsed.netloc.split(":")[0])


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = name.strip(". ")
    return name or "_"


def url_to_local_path(url: str, base_dir: Path) -> Path:
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if not parts or parts == [""]:
        parts = ["index.html"]

    last = parts[-1]
    if "." not in last:
        parts[-1] = last + ".html"

    safe_parts = [
        sanitize_filename(urllib.parse.unquote(part))
        for part in parts
    ]
    return base_dir.joinpath(*safe_parts)


def is_same_domain(url: str, base_domain: str) -> bool:
    return urllib.parse.urlparse(url).netloc == base_domain


def guess_extension(content_type: str, url: str) -> str:
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
    if ext:
        return ext
    return os.path.splitext(urllib.parse.urlparse(url).path)[1]


class WebsiteCloner:
    def __init__(
        self,
        start_url: str,
        output_dir: str = "cloned_site",
        max_depth: int = 5,
        download_media: bool = True,
        single_page: bool = False,
        delay: float = 0.3,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    ):
        self.start_url = start_url
        self.output_dir = Path(output_dir)
        self.max_depth = 0 if single_page else max_depth
        self.download_media = download_media
        self.single_page = single_page
        self.delay = max(0.0, delay)
        self.parsed_start = urllib.parse.urlparse(start_url)
        self.base_domain = self.parsed_start.netloc
        self.base_url = f"{self.parsed_start.scheme}://{self.base_domain}"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })

        self.downloaded: set[str] = set()
        self.queue: list[tuple[str, int]] = []
        self.failed: set[str] = set()

    def fetch(self, url: str) -> Optional[requests.Response]:
        try:
            response = self.session.get(url, timeout=30, allow_redirects=True)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            self.failed.add(url)
            log.warning("Failed to fetch %s — %s", url, exc)
            return None

    def extract_html_assets(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        assets: list[str] = []

        for tag in soup.find_all(["img", "source", "video", "audio"]):
            src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
            if src:
                assets.append(urllib.parse.urljoin(page_url, src))
            srcset = tag.get("srcset")
            if srcset:
                for part in srcset.split(","):
                    pieces = part.strip().split()
                    if pieces:
                        assets.append(urllib.parse.urljoin(page_url, pieces[0]))

        if not self.single_page:
            for tag in soup.find_all("a", href=True):
                full = urllib.parse.urljoin(page_url, tag["href"])
                parsed = urllib.parse.urlparse(full)
                if parsed.scheme in ("http", "https") and is_same_domain(full, self.base_domain):
                    assets.append(full)

        for tag in soup.find_all("link", href=True):
            assets.append(urllib.parse.urljoin(page_url, tag["href"]))

        for tag in soup.find_all("script", src=True):
            assets.append(urllib.parse.urljoin(page_url, tag["src"]))

        for style in soup.find_all("style"):
            text = style.get_text() or ""
            for match in CSS_URL_RE.finditer(text):
                assets.append(urllib.parse.urljoin(page_url, match.group(1)))
            for match in CSS_IMPORT_RE.finditer(text):
                assets.append(urllib.parse.urljoin(page_url, match.group(1)))

        for tag in soup.find_all(style=True):
            for match in CSS_URL_RE.finditer(tag["style"]):
                assets.append(urllib.parse.urljoin(page_url, match.group(1)))

        return list(dict.fromkeys(assets))

    def extract_css_assets(self, css_text: str, css_url: str) -> list[str]:
        found = []
        for pattern in (CSS_URL_RE, CSS_IMPORT_RE):
            for match in pattern.finditer(css_text):
                ref = match.group(1)
                if ref.startswith(("data:", "blob:", "#")):
                    continue
                found.append(urllib.parse.urljoin(css_url, ref))
        return list(dict.fromkeys(found))

    def extract_js_assets(self, js_text: str, js_url: str) -> list[str]:
        found = []
        for match in JS_ASSET_RE.finditer(js_text):
            ref = match.group(1)
            if ref.startswith("//"):
                continue
            full = urllib.parse.urljoin(js_url, ref)
            parsed = urllib.parse.urlparse(full)
            if parsed.scheme in ("http", "https") and parsed.netloc == self.base_domain:
                found.append(full)
        return list(dict.fromkeys(found))

    def save_file(self, content: bytes, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        log.info("  Saved: %s", local_path.relative_to(self.output_dir))

    def _asset_url_to_relative(self, asset_url: str, page_local_path: Path) -> Optional[str]:
        local_path = url_to_local_path(asset_url, self.output_dir)
        try:
            return os.path.relpath(local_path, page_local_path.parent).replace("\\", "/")
        except ValueError:
            return None

    def _rewrite_css_urls(self, css_text: str, css_file_url: str, css_local_path: Path) -> str:
        def replace(match):
            ref = match.group(1).strip("'\"")
            if ref.startswith(("data:", "blob:", "#")):
                return match.group(0)
            full = urllib.parse.urljoin(css_file_url, ref)
            parsed = urllib.parse.urlparse(full)
            if parsed.netloc != self.base_domain:
                return match.group(0)
            rel = self._asset_url_to_relative(full, css_local_path)
            return match.group(0).replace(match.group(1), rel) if rel else match.group(0)

        return CSS_URL_RE.sub(replace, css_text)

    def _rewrite_html_urls(self, soup: BeautifulSoup, page_url: str, page_local_path: Path):
        attrs = [
            ("img", "src"), ("img", "data-src"), ("img", "data-lazy-src"),
            ("source", "src"), ("source", "srcset"),
            ("video", "src"), ("audio", "src"),
            ("link", "href"), ("script", "src"),
        ]
        for selector, attr in attrs:
            for tag in soup.find_all(selector):
                value = tag.get(attr)
                if not value:
                    continue
                if attr == "srcset":
                    pieces_out = []
                    for part in value.split(","):
                        pieces = part.strip().split()
                        if pieces:
                            full = urllib.parse.urljoin(page_url, pieces[0])
                            if urllib.parse.urlparse(full).netloc == self.base_domain:
                                rel = self._asset_url_to_relative(full, page_local_path)
                                if rel:
                                    pieces[0] = rel
                            pieces_out.append(" ".join(pieces))
                    tag[attr] = ", ".join(pieces_out)
                else:
                    full = urllib.parse.urljoin(page_url, value)
                    if urllib.parse.urlparse(full).netloc == self.base_domain:
                        rel = self._asset_url_to_relative(full, page_local_path)
                        if rel:
                            tag[attr] = rel

        for style in soup.find_all("style"):
            if style.string:
                style.string.replace_with(
                    self._rewrite_css_urls(style.string, page_url, page_local_path)
                )
        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_urls(tag["style"], page_url, page_local_path)
        return soup

    def _should_download_media(self, url: str, local_path: Path) -> bool:
        if self.download_media:
            return True
        return local_path.suffix.lower() not in MEDIA_EXTENSIONS

    def process_html(self, html: str, page_url: str, depth: int, page_local_path: Path) -> str:
        soup = BeautifulSoup(html, "lxml")
        assets = self.extract_html_assets(soup, page_url)
        index = 0

        # Process a growing list: CSS and JS files can add more dependencies.
        while index < len(assets):
            asset_url = assets[index]
            index += 1
            asset_url, _fragment = urllib.parse.urldefrag(asset_url)
            parsed = urllib.parse.urlparse(asset_url)

            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            if asset_url in self.downloaded:
                continue
            if not self.download_media and Path(parsed.path).suffix.lower() in MEDIA_EXTENSIONS:
                continue

            self.downloaded.add(asset_url)
            response = self.fetch(asset_url)
            if response is None:
                continue

            # Use the final response URL for dependency resolution after redirects.
            final_url = response.url
            content_type = response.headers.get("Content-Type", "").lower()
            local_path = url_to_local_path(asset_url, self.output_dir)
            self.save_file(response.content, local_path)

            if "text/css" in content_type or local_path.suffix.lower() == ".css":
                text = response.content.decode("utf-8", errors="replace")
                assets.extend(self.extract_css_assets(text, final_url))
                rewritten = self._rewrite_css_urls(text, final_url, local_path)
                local_path.write_text(rewritten, encoding="utf-8")

            elif (
                "javascript" in content_type
                or local_path.suffix.lower() in (".js", ".mjs")
            ):
                js_text = response.content.decode("utf-8", errors="replace")
                assets.extend(self.extract_js_assets(js_text, final_url))

            elif (
                depth + 1 <= self.max_depth
                and is_same_domain(asset_url, self.base_domain)
                and ("text/html" in content_type or local_path.suffix.lower() in (".html", ".htm"))
            ):
                self.queue.append((asset_url, depth + 1))

            time.sleep(self.delay)

        soup = self._rewrite_html_urls(soup, page_url, page_local_path)
        return str(soup)

    def run(self) -> None:
        log.info("Starting URL: %s", self.start_url)
        log.info("Output directory: %s", self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.queue.append((self.start_url, 0))

        while self.queue:
            url, depth = self.queue.pop(0)
            url, _fragment = urllib.parse.urldefrag(url)
            if url in self.downloaded:
                continue

            self.downloaded.add(url)
            log.info("Fetching [depth %d]: %s", depth, url)
            response = self.fetch(url)
            if response is None:
                continue

            local_path = url_to_local_path(url, self.output_dir)
            content_type = response.headers.get("Content-Type", "").lower()

            if "text/html" in content_type or local_path.suffix.lower() in (".html", ".htm"):
                html = self.process_html(response.text, url, depth, local_path)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(html, encoding="utf-8")
                log.info("  Saved HTML: %s", local_path.relative_to(self.output_dir))
            else:
                self.save_file(response.content, local_path)

            time.sleep(self.delay)

        log.info("Finished. Processed %d URLs; %d fetches failed.",
                 len(self.downloaded), len(self.failed))
        if self.failed:
            log.warning("Failed URLs (first 30):")
            for url in sorted(self.failed)[:30]:
                log.warning("  %s", url)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clone a website's static files for offline/local archiving.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python website_cloner.py https://example.com\n"
            "  python website_cloner.py https://example.com --output ./my_clone --depth 2\n"
            "  python website_cloner.py https://example.com --single-page\n"
        ),
    )
    parser.add_argument("url", help="URL of the website to clone")
    parser.add_argument("-o", "--output", default=None, help="Output directory")
    parser.add_argument("-d", "--depth", type=int, default=5, help="HTML crawl depth (default: 5)")
    parser.add_argument("--single-page", action="store_true", help="Do not follow HTML links")
    parser.add_argument("--no-media", action="store_true", help="Skip images, fonts, video, and other media")
    parser.add_argument("--delay", type=float, default=0.3, help="Delay between requests (default: 0.3)")
    args = parser.parse_args()

    output_dir = args.output or url_to_dirname(args.url)
    cloner = WebsiteCloner(
        start_url=args.url,
        output_dir=output_dir,
        max_depth=args.depth,
        download_media=not args.no_media,
        single_page=args.single_page,
        delay=args.delay,
    )
    cloner.run()


if __name__ == "__main__":
    main()
