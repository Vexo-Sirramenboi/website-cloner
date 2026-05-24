#!/usr/bin/env python3
"""
Website Cloner – downloads a full website including HTML, CSS, JS, images,
fonts, and other media assets, preserving the directory structure locally.

Usage:
    python website_cloner.py <URL> [--output DIR] [--depth N] [--single-page] [--no-media]

Requirements:
    pip install requests beautifulsoup4 lxml
"""

import argparse
import hashlib
import logging
import mimetypes
import os
import re
import sys
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# File extensions we always treat as "media" even if the MIME type is weird
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".flac",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".pdf", ".zip", ".gz", ".tar", ".rar",
}

CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")\s]+?)['"]?\s*\)""")


def url_to_dirname(url: str) -> str:
    """Convert a URL to a safe directory name (e.g. 'nbg-stockach.de')."""
    parsed = urllib.parse.urlparse(url)
    name = parsed.netloc
    # Remove port if present
    name = name.split(":")[0]
    return sanitize_filename(name)


def sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    if not name:
        name = '_'
    return name


def url_to_local_path(url: str, base_dir: Path) -> Path:
    """Convert a URL to a local file path inside *base_dir*."""
    parsed = urllib.parse.urlparse(url)
    # Build path from URL path
    parts = parsed.path.strip('/').split('/')
    if not parts or parts == ['']:
        parts = ['index.html']

    # If the last part has no extension and doesn't end with /, treat as .html
    last = parts[-1]
    if '.' not in last and not last.endswith('/'):
        parts[-1] = last + '.html'

    # Decode percent-encoding and sanitize each part
    safe_parts = []
    for p in parts:
        p = urllib.parse.unquote(p)
        p = sanitize_filename(p)
        safe_parts.append(p)

    return base_dir.joinpath(*safe_parts)


def is_same_domain(url: str, base_domain: str) -> bool:
    """Return True if *url* belongs to the same domain as *base_domain*."""
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc == base_domain


def is_subpath(url: str, base_url: str) -> bool:
    """Return True if *url* is under the same path prefix as *base_url*."""
    return url.startswith(base_url)


def guess_extension(content_type: str, url: str) -> str:
    """Guess a file extension from Content-Type header or URL."""
    ext = mimetypes.guess_extension(content_type.split(';')[0].strip())
    if ext:
        return ext
    # Fallback: try from URL path
    path = urllib.parse.urlparse(url).path
    _, ext2 = os.path.splitext(path)
    return ext2 or ""


# ---------------------------------------------------------------------------
# Core downloader
# ---------------------------------------------------------------------------

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
        self.delay = delay
        self.parsed_start = urllib.parse.urlparse(start_url)
        self.base_domain = self.parsed_start.netloc
        self.base_url = f"{self.parsed_start.scheme}://{self.base_domain}"

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        # Track what we've already downloaded to avoid duplicates
        self.downloaded: set[str] = set()
        # Queue of (url, depth) tuples to process
        self.queue: list[tuple[str, int]] = []

    # ----- network -----

    def fetch(self, url: str) -> Optional[requests.Response]:
        """GET *url* and return the response, or None on failure."""
        try:
            resp = self.session.get(url, timeout=30, allow_redirects=True)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            log.warning("Failed to fetch %s – %s", url, e)
            return None

    # ----- asset extraction -----

    def extract_html_assets(self, soup: BeautifulSoup, page_url: str) -> list[str]:
        """Return a list of asset URLs referenced in the HTML."""
        assets: list[str] = []

        # <img src>, <source src>, <video src>, <audio src>
        for tag in soup.find_all(["img", "source", "video", "audio"]):
            src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
            if src:
                assets.append(urllib.parse.urljoin(page_url, src))
            # <source srcset>
            srcset = tag.get("srcset")
            if srcset:
                for part in srcset.split(","):
                    u = part.strip().split()[0]
                    assets.append(urllib.parse.urljoin(page_url, u))

        # <a href> – only follow same-domain links when NOT in single-page mode
        if not self.single_page:
            for tag in soup.find_all("a", href=True):
                href = tag["href"]
                full = urllib.parse.urljoin(page_url, href)
                if is_same_domain(full, self.base_domain):
                    assets.append(full)

        # <link href> (stylesheets, icons, preload, etc.)
        for tag in soup.find_all("link", href=True):
            href = tag["href"]
            full = urllib.parse.urljoin(page_url, href)
            assets.append(full)

        # <script src>
        for tag in soup.find_all("script", src=True):
            assets.append(urllib.parse.urljoin(page_url, tag["src"]))

        # Inline <style> blocks – extract url() references
        for style in soup.find_all("style"):
            if style.string:
                for m in CSS_URL_RE.finditer(style.string):
                    assets.append(urllib.parse.urljoin(page_url, m.group(1)))

        # Inline style attributes
        for tag in soup.find_all(style=True):
            for m in CSS_URL_RE.finditer(tag["style"]):
                assets.append(urllib.parse.urljoin(page_url, m.group(1)))

        return assets

    def extract_css_assets(self, css_text: str, css_url: str) -> list[str]:
        """Return asset URLs found inside a CSS file (url(...) references)."""
        assets: list[str] = []
        for m in CSS_URL_RE.finditer(css_text):
            ref = m.group(1)
            # Skip data: URIs
            if ref.startswith("data:"):
                continue
            full = urllib.parse.urljoin(css_url, ref)
            assets.append(full)
        return assets

    # ----- saving -----

    def save_file(self, content: bytes, local_path: Path) -> None:
        """Write *content* to *local_path*, creating directories as needed."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if local_path.exists():
            return
        local_path.write_bytes(content)
        log.info("  Saved: %s", local_path.relative_to(self.output_dir))

    # ----- URL rewriting -----

    def _asset_url_to_relative(self, asset_url: str, page_local_path: Path) -> Optional[str]:
        """Map a full asset URL to a relative path from the HTML file's location."""
        local_path = url_to_local_path(asset_url, self.output_dir)
        try:
            rel = local_path.relative_to(page_local_path.parent)
        except ValueError:
            return None
        return str(rel).replace("\\", "/")

    def _rewrite_html_urls(self, soup: BeautifulSoup, page_local_path: Path) -> BeautifulSoup:
        """Rewrite all asset URLs in * soup* to relative local paths."""
        # <img src>, <source src>, <video src>, <audio src>
        for tag in soup.find_all(["img", "source", "video", "audio"]):
            src = tag.get("src") or tag.get("data-src") or tag.get("data-lazy-src")
            if src:
                full = urllib.parse.urljoin(page_local_path.as_posix(), src)
                rel = self._asset_url_to_relative(full, page_local_path)
                if rel:
                    tag["src"] = rel
                    if tag.get("data-src"):
                        tag["data-src"] = rel
                    if tag.get("data-lazy-src"):
                        tag["data-lazy-src"] = rel
            # srcset
            srcset = tag.get("srcset")
            if srcset:
                new_parts = []
                for part in srcset.split(","):
                    pieces = part.strip().split()
                    if pieces:
                        full = urllib.parse.urljoin(page_local_path.as_posix(), pieces[0])
                        rel = self._asset_url_to_relative(full, page_local_path)
                        if rel:
                            pieces[0] = rel
                        new_parts.append(" ".join(pieces))
                tag["srcset"] = ", ".join(new_parts)

        # <link href>
        for tag in soup.find_all("link", href=True):
            full = urllib.parse.urljoin(page_local_path.as_posix(), tag["href"])
            rel = self._asset_url_to_relative(full, page_local_path)
            if rel:
                tag["href"] = rel

        # <script src>
        for tag in soup.find_all("script", src=True):
            full = urllib.parse.urljoin(page_local_path.as_posix(), tag["src"])
            rel = self._asset_url_to_relative(full, page_local_path)
            if rel:
                tag["src"] = rel

        # Inline <style>
        for style in soup.find_all("style"):
            if style.string:
                style.string = self._rewrite_css_urls(style.string, page_local_path)

        # Inline style attributes
        for tag in soup.find_all(style=True):
            tag["style"] = self._rewrite_css_urls(tag["style"], page_local_path)

        return soup

    def _rewrite_css_urls(self, css_text: str, page_local_path: Path) -> str:
        """Rewrite url() references inside a CSS string to relative local paths."""
        def _replace(m):
            ref = m.group(1).strip("'\"")
            if ref.startswith("data:"):
                return m.group(0)
            full = urllib.parse.urljoin(page_local_path.as_posix(), ref)
            rel = self._asset_url_to_relative(full, page_local_path)
            if rel:
                return m.group(0).replace(m.group(1), rel)
            return m.group(0)

        return CSS_URL_RE.sub(_replace, css_text)

    # ----- processing -----

    def process_html(self, html: str, page_url: str, depth: int, page_local_path: Path) -> str:
        """Download all assets referenced in *html*, rewrite URLs to local paths."""
        soup = BeautifulSoup(html, "lxml")

        # Collect all asset URLs (exclude <a href> in single-page mode)
        assets = self.extract_html_assets(soup, page_url)

        # First pass: download all assets, build URL -> local_path map
        url_to_local: dict[str, Path] = {}
        for asset_url in assets:
            asset_url = asset_url.split("#")[0]
            if not asset_url or asset_url in self.downloaded:
                continue
            self.downloaded.add(asset_url)

            resp = self.fetch(asset_url)
            if resp is None:
                continue

            content_type = resp.headers.get("Content-Type", "")
            local_path = url_to_local_path(asset_url, self.output_dir)

            if "text/html" in content_type and not local_path.suffix:
                local_path = local_path.with_suffix(".html")

            self.save_file(resp.content, local_path)
            url_to_local[asset_url] = local_path

            # If it's CSS, also parse it for nested assets
            if "text/css" in content_type or local_path.suffix == ".css":
                nested = self.extract_css_assets(resp.text, asset_url)
                for nested_url in nested:
                    nested_url = nested_url.split("#")[0]
                    if nested_url in self.downloaded:
                        continue
                    self.downloaded.add(nested_url)
                    resp2 = self.fetch(nested_url)
                    if resp2:
                        np = url_to_local_path(nested_url, self.output_dir)
                        self.save_file(resp2.content, np)
                        url_to_local[nested_url] = np

            # Queue same-domain HTML pages for recursive crawling
            if (
                depth + 1 <= self.max_depth
                and is_same_domain(asset_url, self.base_domain)
                and ("text/html" in content_type or local_path.suffix in (".html", ".htm", ""))
            ):
                self.queue.append((asset_url, depth + 1))

            time.sleep(self.delay)

        # Second pass: rewrite URLs in HTML to relative paths
        soup = self._rewrite_html_urls(soup, page_local_path)

        # Also rewrite URLs in downloaded CSS files
        for asset_url, local_path in url_to_local.items():
            if local_path.suffix == ".css" and local_path.exists():
                css_text = local_path.read_text(encoding="utf-8", errors="replace")
                rewritten = self._rewrite_css_urls(css_text, local_path)
                local_path.write_text(rewritten, encoding="utf-8")

        return str(soup)

    def run(self) -> None:
        """Main entry point – start cloning from *self.start_url*."""
        mode = "single-page" if self.single_page else f"depth={self.max_depth}"
        log.info("Cloning: %s", self.start_url)
        log.info("Mode:    %s", mode)
        log.info("Output:  %s", self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.queue.append((self.start_url, 0))

        while self.queue:
            url, depth = self.queue.pop(0)
            url = url.split("#")[0]

            if url in self.downloaded:
                continue
            self.downloaded.add(url)

            log.info("Fetching [%d/%d]: %s", depth, self.max_depth, url)
            resp = self.fetch(url)
            if resp is None:
                continue

            content_type = resp.headers.get("Content-Type", "")
            local_path = url_to_local_path(url, self.output_dir)

            if "text/html" in content_type:
                html = self.process_html(resp.text, url, depth, local_path)
                local_path.parent.mkdir(parents=True, exist_ok=True)
                local_path.write_text(html, encoding="utf-8")
                log.info("  Saved: %s", local_path.relative_to(self.output_dir))
            else:
                self.save_file(resp.content, local_path)

            time.sleep(self.delay)

        log.info("Done! Downloaded %d files to %s", len(self.downloaded), self.output_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clone a website (HTML + CSS + media) to a local directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python website_cloner.py https://example.com
  python website_cloner.py https://example.com --output ./my_clone --depth 3
  python website_cloner.py https://example.com --single-page
  python website_cloner.py https://example.com --no-media
""",
    )
    parser.add_argument("url", help="URL of the website to clone")
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output directory (default: derived from URL, e.g. 'nbg-stockach.de')",
    )
    parser.add_argument(
        "-d", "--depth",
        type=int,
        default=5,
        help="Maximum crawl depth (default: 5)",
    )
    parser.add_argument(
        "--single-page",
        action="store_true",
        help="Only download the given page and its direct assets (CSS, JS, images, fonts). "
             "Do not follow links to other pages.",
    )
    parser.add_argument(
        "--no-media",
        action="store_true",
        help="Skip images, videos, fonts, etc.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.3,
        help="Delay between requests in seconds (default: 0.3)",
    )

    args = parser.parse_args()

    output_dir = args.output if args.output else url_to_dirname(args.url)

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
