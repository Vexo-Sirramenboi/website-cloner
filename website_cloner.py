
#!/usr/bin/env python3
"""Recursive static website downloader with sitemap, HTML, CSS and JS discovery.

Install:
    python -m pip install requests beautifulsoup4 lxml

Usage:
    python website_cloner.py https://example.com --depth 10 --max-resources 20000

Only archive websites you own or have permission to copy.
This is not a browser; dynamic APIs and runtime-generated assets may be missed.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import deque
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("website_cloner")

MEDIA = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".bmp", ".avif", ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    ".flac", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf",
    ".zip", ".gz", ".tar", ".rar", ".7z", ".wasm",
}

ASSET_EXTENSIONS = {
    ".js", ".mjs", ".cjs", ".css", ".json", ".map", ".wasm",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".bmp", ".avif", ".mp4", ".webm", ".ogg", ".mp3", ".wav",
    ".flac", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf",
}

CSS_URL = re.compile(
    r"""url\(\s*(?:(["'])(.*?)\1|([^)]*?))\s*\)""",
    re.I,
)
CSS_IMPORT = re.compile(
    r"""@import\s+(?:url\(\s*)?["']([^"']+)["']""",
    re.I,
)

# Static ES module imports, exports and dynamic import("literal") calls.
JS_IMPORT = re.compile(
    r"""(?:\bimport\s*(?:\(\s*)?|\bexport\s+[^;]*?\bfrom\s*)["']([^"'\\]+)["']""",
    re.M,
)

# URLs in strings that look like files. This is heuristic, not a JS parser.
JS_FILE = re.compile(
    r"""["'`]((?:(?:https?:)?//|/|\./|\.\./)[^"'`\s<>]+)["'`]""",
    re.I,
)

SKIP = (
    "data:", "blob:", "javascript:", "mailto:", "tel:",
    "about:", "file:", "intent:",
)


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    return value.strip(". ") or "_"


def normalize(url):
    url, _ = urllib.parse.urldefrag(url.strip())
    p = urllib.parse.urlsplit(url)

    if not p.scheme or not p.hostname:
        return url

    scheme = p.scheme.lower()
    host = p.hostname.lower()
    port = p.port

    netloc = host
    if port and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    path = urllib.parse.quote(
        urllib.parse.unquote(p.path or "/"),
        safe="/%:@!$&'()*+,;=-._~",
    )

    return urllib.parse.urlunsplit(
        (scheme, netloc, path, p.query, "")
    )


def origin(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme.lower()}://{p.netloc.lower()}"


def local_path(url, root, base_host):
    p = urllib.parse.urlsplit(url)

    parts = [
        safe_name(urllib.parse.unquote(part))
        for part in p.path.split("/")
        if part
    ]

    if not parts:
        parts = ["index.html"]
    elif p.path.endswith("/"):
        parts.append("index.html")
    elif not Path(parts[-1]).suffix:
        parts[-1] += ".html"

    if p.query:
        digest = hashlib.sha256(
            p.query.encode()
        ).hexdigest()[:12]
        stem, ext = os.path.splitext(parts[-1])
        parts[-1] = f"{stem}__q_{digest}{ext}"

    if (p.hostname or "").lower() != base_host:
        parts = [
            "_external",
            safe_name(p.hostname or "unknown"),
        ] + parts

    target = root.joinpath(*parts)

    resolved_root = root.resolve()
    resolved_target = target.resolve()

    if (
        resolved_target != resolved_root
        and resolved_root not in resolved_target.parents
    ):
        raise ValueError(f"Unsafe output path: {url}")

    return target


class WebsiteCloner:
    def __init__(
        self,
        url,
        output,
        depth=4,
        single_page=False,
        no_media=False,
        delay=0.25,
        max_resources=5000,
        timeout=25,
        external_assets=True,
    ):
        if not urllib.parse.urlsplit(url).scheme:
            url = "https://" + url

        self.start = normalize(url)
        parsed = urllib.parse.urlsplit(self.start)

        self.origin_netloc = parsed.netloc.lower()
        self.base_host = (parsed.hostname or "").lower()
        self.root = Path(output)

        self.depth = 0 if single_page else max(0, depth)
        self.single_page = single_page
        self.no_media = no_media
        self.delay = max(0, delay)
        self.limit = max(1, max_resources)
        self.timeout = timeout
        self.external_assets = external_assets

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; WebsiteArchiveBot/1.0)"
            ),
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })

        retries = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
        )

        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.queue = deque()
        self.queued = set()
        self.done = set()
        self.failed = set()
        self.visited_sitemaps = set()
        self.discovered = set()

        # Maps the originally requested URL to its local file.
        self.saved_paths = {}

    def same_origin(self, url):
        p = urllib.parse.urlsplit(url)
        return (
            p.scheme in ("http", "https")
            and p.netloc.lower() == self.origin_netloc
        )

    def allowed_resource(self, url):
        if self.same_origin(url):
            return True

        if not self.external_assets:
            return False

        # External hosts are permitted for assets, not page crawling.
        return True

    def enqueue(self, url, depth, base_url, kind="resource"):
        if not url:
            return

        url = str(url).strip()

        if not url or url.lower().startswith(SKIP):
            return

        if url.startswith("#"):
            return

        try:
            full = normalize(urllib.parse.urljoin(base_url, url))
            parsed = urllib.parse.urlsplit(full)
        except (ValueError, TypeError):
            return

        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return

        # Keep pages on the starting origin. External assets may be
        # downloaded, but their HTML links are not recursively crawled.
        if not self.same_origin(full):
            if kind == "page" or not self.external_assets:
                return

        if full in self.done or full in self.queued:
            return

        if len(self.done) + len(self.queued) >= self.limit:
            return

        self.queued.add(full)
        self.queue.append((full, depth, kind))
        self.discovered.add(full)

    def discover_css(self, text, base, depth):
        for match in CSS_URL.finditer(text):
            ref = (match.group(2) or match.group(3) or "").strip()
            ref = ref.strip("'\"")

            if ref and not ref.startswith(SKIP):
                self.enqueue(ref, depth, base, "resource")

        for match in CSS_IMPORT.finditer(text):
            ref = match.group(1).strip()

            if ref and not ref.startswith(SKIP):
                self.enqueue(ref, depth, base, "resource")

    def discover_js(self, text, base, depth):
        refs = set()

        for match in JS_IMPORT.finditer(text):
            refs.add(match.group(1).strip())

        for match in JS_FILE.finditer(text):
            refs.add(match.group(1).strip())

        count = 0

        for ref in refs:
            if not ref or ref.startswith(SKIP):
                continue

            # Ignore fragments and non-file relative identifiers.
            parsed = urllib.parse.urlsplit(ref)
            suffix = Path(parsed.path).suffix.lower()

            if (
                ref.startswith(("./", "../", "/"))
                or ref.startswith(("http://", "https://", "//"))
                or suffix in ASSET_EXTENSIONS
            ):
                self.enqueue(ref, depth, base, "resource")
                count += 1

            if count >= 2000:
                break

    def discover_html(self, soup, url, depth):
        base_tag = soup.find("base", href=True)
        base_url = (
            urllib.parse.urljoin(url, base_tag["href"])
            if base_tag
            else url
        )

        # Crawl linked HTML pages within the original origin.
        if depth < self.depth and not self.single_page:
            for tag in soup.find_all("a", href=True):
                self.enqueue(
                    tag["href"], depth + 1, base_url, "page"
                )

        # Embedded files, frames and lazy-loaded resources.
        resource_tags = [
            "img", "source", "video", "audio", "track",
            "embed", "iframe", "object", "input",
        ]

        resource_attrs = [
            "src", "data-src", "data-lazy-src", "data-original",
            "data-url", "data-original-src", "data-src-url",
            "poster", "data", "srcset", "data-srcset",
        ]

        for tag in soup.find_all(resource_tags):
            for attr in resource_attrs:
                value = tag.get(attr)
                if not value:
                    continue

                if attr in ("srcset", "data-srcset"):
                    # A simple srcset parser. Unusual data URLs and
                    # commas inside URLs can require special handling.
                    for item in value.split(","):
                        bits = item.strip().split()
                        if bits:
                            self.enqueue(
                                bits[0], depth, base_url, "resource"
                            )
                else:
                    self.enqueue(
                        value, depth, base_url, "resource"
                    )

        # Stylesheets, icons, manifests, preload and prefetch.
        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower()

            if any(name in rel for name in (
                "stylesheet", "icon", "preload", "modulepreload",
                "manifest", "apple-touch-icon", "prefetch",
            )):
                self.enqueue(
                    tag["href"], depth, base_url, "resource"
                )

        # External scripts and module entry points.
        for tag in soup.find_all("script", src=True):
            self.enqueue(
                tag["src"], depth, base_url, "resource"
            )

        # Some applications expose additional URLs in metadata.
        for tag in soup.find_all("meta"):
            for attr in ("content", "href"):
                value = tag.get(attr)

                if (
                    value
                    and attr == "content"
                    and value.startswith(("http://", "https://"))
                ):
                    self.enqueue(
                        value, depth, base_url, "resource"
                    )

        # Inline CSS.
        for tag in soup.find_all("style"):
            self.discover_css(
                tag.get_text() or "", base_url, depth
            )

        for tag in soup.find_all(style=True):
            self.discover_css(
                tag.get("style", ""), base_url, depth
            )

        # Meta-refresh redirects.
        for tag in soup.find_all(
            "meta", attrs={"http-equiv": True}
        ):
            if tag.get("http-equiv", "").lower() == "refresh":
                content = tag.get("content", "")
                match = re.search(
                    r"""url\s*=\s*['"]?(.+?)['"]?$""",
                    content,
                    re.I,
                )

                if match:
                    self.enqueue(
                        match.group(1),
                        depth + 1,
                        base_url,
                        "page",
                    )

    def discover_sitemap(self, url, depth=0):
        if url in self.visited_sitemaps:
            return

        if len(self.done) + len(self.queued) >= self.limit:
            return

        self.visited_sitemaps.add(url)
        log.info("Reading sitemap: %s", url)

        try:
            response = self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)
        except (
            requests.RequestException,
            ET.ParseError,
            ValueError,
        ) as exc:
            log.warning("Sitemap unavailable: %s (%s)", url, exc)
            self.failed.add(url)
            return

        # Namespace-independent parsing for sitemap XML.
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1].lower() != "loc":
                continue

            loc = (element.text or "").strip()
            if not loc:
                continue

            full = normalize(urllib.parse.urljoin(url, loc))
            path = urllib.parse.urlsplit(full).path.lower()

            if path.endswith(".xml") or "sitemap" in path:
                if self.same_origin(full):
                    self.discover_sitemap(full, depth + 1)
            elif self.same_origin(full):
                self.enqueue(full, 0, url, "page")

            if len(self.done) + len(self.queued) >= self.limit:
                return

    def discover_robots(self):
        robots_url = urllib.parse.urljoin(self.start, "/robots.txt")

        try:
            response = self.session.get(
                robots_url,
                timeout=self.timeout,
                allow_redirects=True,
            )

            if response.status_code >= 400:
                return

            for line in response.text.splitlines():
                match = re.match(
                    r"^\s*Sitemap\s*:\s*(\S+)",
                    line,
                    re.I,
                )

                if match:
                    sitemap = normalize(
                        urllib.parse.urljoin(
                            robots_url, match.group(1)
                        )
                    )

                    if self.same_origin(sitemap):
                        self.discover_sitemap(sitemap)

        except requests.RequestException as exc:
            log.info("Could not read robots.txt: %s", exc)

    def discover_common_sitemaps(self):
        candidates = [
            urllib.parse.urljoin(self.start, "/sitemap.xml"),
            urllib.parse.urljoin(self.start, "/sitemap_index.xml"),
            urllib.parse.urljoin(self.start, "/wp-sitemap.xml"),
        ]

        for candidate in candidates:
            if len(self.done) + len(self.queued) >= self.limit:
                break

            self.discover_sitemap(candidate)

    def rel(self, asset_url, from_path):
        try:
            target = local_path(
                asset_url, self.root, self.base_host
            )
            return os.path.relpath(
                target, from_path.parent
            ).replace("\\", "/")
        except (ValueError, OSError):
            return None

    def rewrite_css(self, text, url, path):
        def replace(match):
            quote = match.group(1) or ""
            ref = (match.group(2) or match.group(3) or "").strip()
            ref = ref.strip("'\"")

            if not ref or ref.startswith(SKIP) or ref.startswith("#"):
                return match.group(0)

            full = normalize(urllib.parse.urljoin(url, ref))
            local = self.rel(full, path)

            if not local:
                return match.group(0)

            return f"url({quote}{local}{quote})"

        return CSS_URL.sub(replace, text)

    def rewrite_html(self, soup, url, path):
        attrs = (
            "src", "href", "poster", "data-src",
            "data-lazy-src", "data-original",
            "data-url", "data-original-src",
        )

        for tag in soup.find_all(True):
            for attr in attrs:
                value = tag.get(attr)

                if (
                    not value
                    or value.strip().lower().startswith(SKIP)
                    or value.startswith("#")
                ):
                    continue

                full = normalize(urllib.parse.urljoin(url, value))
                local = self.rel(full, path)

                if local:
                    tag[attr] = local

            for attr in ("srcset", "data-srcset"):
                value = tag.get(attr)
                if not value:
                    continue

                items = []

                for item in value.split(","):
                    bits = item.strip().split()

                    if not bits:
                        continue

                    full = normalize(
                        urllib.parse.urljoin(url, bits[0])
                    )
                    local = self.rel(full, path)

                    if local:
                        bits[0] = local

                    items.append(" ".join(bits))

                tag[attr] = ", ".join(items)

            if tag.get("style"):
                tag["style"] = self.rewrite_css(
                    tag["style"], url, path
                )

        for tag in soup.find_all("style"):
            original = tag.get_text()

            if original:
                tag.clear()
                tag.append(self.rewrite_css(original, url, path))

        # Remove the base URL so local relative paths resolve correctly.
        for tag in soup.find_all("base"):
            tag.decompose()

        return soup

    def save(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        try:
            relative = path.relative_to(self.root)
        except ValueError:
            relative = path

        log.info("Saved: %s", relative)

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)

        # Start with the target page and public sitemap indexes.
        self.enqueue(self.start, 0, self.start, "page")
        self.discover_robots()
        self.discover_common_sitemaps()

        log.info("Starting: %s", self.start)
        log.info(
            "Output: %s | depth: %d | limit: %d",
            self.root, self.depth, self.limit,
        )

        while self.queue and len(self.done) < self.limit:
            url, depth, kind = self.queue.popleft()
            self.queued.discard(url)

            if url in self.done:
                continue

            self.done.add(url)
            log.info(
                "Fetching [%d/%d] depth=%d: %s",
                len(self.done), self.limit, depth, url,
            )

            try:
                response = self.session.get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
                response.raise_for_status()
            except requests.RequestException as exc:
                self.failed.add(url)
                log.warning("Failed: %s (%s)", url, exc)
                continue

            final_url = normalize(response.url)

            try:
                path = local_path(
                    url, self.root, self.base_host
                )
            except ValueError as exc:
                self.failed.add(url)
                log.warning("%s", exc)
                continue

            ctype = response.headers.get(
                "Content-Type", ""
            ).lower()
            suffix = path.suffix.lower()

            if self.no_media and suffix in MEDIA:
                continue

            data = response.content

            # Use Content-Type as well as file extensions.
            is_html = (
                "text/html" in ctype
                or suffix in (".html", ".htm")
            )
            is_css = "text/css" in ctype or suffix == ".css"
            is_js = (
                any(x in ctype for x in (
                    "javascript", "ecmascript"
                ))
                or suffix in (".js", ".mjs", ".cjs")
            )
            is_xml = (
                "xml" in ctype
                or suffix in (".xml", ".xhtml")
            )

            if is_html:
                soup = BeautifulSoup(response.content, "lxml")
                self.discover_html(soup, final_url, depth)

                self.save(
                    path,
                    str(
                        self.rewrite_html(soup, final_url, path)
                    ).encode("utf-8"),
                )

            elif is_css:
                text = response.content.decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )

                self.discover_css(text, final_url, depth)
                rewritten = self.rewrite_css(
                    text, final_url, path
                )

                self.save(path, rewritten.encode("utf-8"))

            elif is_js:
                text = response.content.decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )

                self.discover_js(text, final_url, depth)
                self.save(path, data)

            elif is_xml:
                # Sitemap discovery is handled separately, but XML
                # resources are still saved as files.
                self.save(path, data)

            else:
                self.save(path, data)

            # Track redirects so reports identify the original URL.
            self.saved_paths[url] = str(path)

            time.sleep(self.delay)

        report = {
            "start_url": self.start,
            "downloaded_or_processed": len(self.done),
            "discovered": len(self.discovered),
            "failed_count": len(self.failed),
            "unprocessed_count": len(self.queue),
            "failed_urls": sorted(self.failed),
            "remaining_urls": [
                item[0] for item in self.queue
            ],
        }

        report_path = self.root / "_crawl_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )

        log.info(
            "Finished. Processed: %d | Failed: %d | Remaining: %d",
            len(self.done), len(self.failed), len(self.queue),
        )
        log.info("Crawl report: %s", report_path)

        for url in sorted(self.failed)[:50]:
            log.warning("Unfetched URL: %s", url)

        if self.queue:
            log.warning(
                "Resource limit reached or URLs remain queued: %d",
                len(self.queue),
            )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Recursively download an authorized static website archive."
        )
    )

    parser.add_argument("url")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument(
        "-d", "--depth", type=int, default=4,
        help="Maximum HTML link depth (default: 4)",
    )
    parser.add_argument("--single-page", action="store_true")
    parser.add_argument("--no-media", action="store_true")
    parser.add_argument("--delay", type=float, default=0.25)
    parser.add_argument(
        "--max-resources", type=int, default=5000,
        help="Maximum URLs to process (default: 5000)",
    )
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument(
        "--no-external-assets", action="store_true",
        help="Skip resources hosted on other domains",
    )

    args = parser.parse_args()

    candidate = (
        args.url
        if "://" in args.url
        else "https://" + args.url
    )

    host = (
        urllib.parse.urlsplit(candidate).hostname
        or "cloned_site"
    )
    output = args.output or safe_name(host)

    cloner = WebsiteCloner(
        candidate,
        output,
        depth=args.depth,
        single_page=args.single_page,
        no_media=args.no_media,
        delay=args.delay,
        max_resources=args.max_resources,
        timeout=args.timeout,
        external_assets=not args.no_external_assets,
    )
    cloner.run()


if __name__ == "__main__":
    main()
