
#!/usr/bin/env python3
"""Controlled static website downloader.

Install:
    python -m pip install requests beautifulsoup4 lxml

Examples:
    python website_cloner.py https://example.com
    python website_cloner.py https://example.com --depth 5
    python website_cloner.py https://example.com --single-page
    python website_cloner.py https://example.com --sitemaps
    python website_cloner.py https://example.com --external-assets

By default:
- HTML pages are crawled only on the starting origin.
- External assets are skipped.
- Sitemaps are not crawled unless explicitly enabled.
- JavaScript discovery is limited to static file imports.
- Invalid URLs are ignored safely.

Only archive websites you own or have permission to copy.
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
    ".js", ".mjs", ".cjs", ".css", ".json",
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

# Only static import/export declarations and literal imports.
JS_IMPORT = re.compile(
    r"""(?:\bimport\s*(?:\(\s*)?|\bexport\s+[^;]*?\bfrom\s*)
        ["']([^"'\\]+)["']""",
    re.M | re.X,
)

SKIP_SCHEMES = (
    "data:", "blob:", "javascript:", "mailto:", "tel:",
    "about:", "file:", "intent:", "ftp:",
)


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    return value.strip(". ") or "_"


def normalize(url):
    """Normalize a URL, returning None for malformed URLs."""
    try:
        url = urllib.parse.urldefrag(str(url).strip())[0]
        p = urllib.parse.urlsplit(url)

        if not p.scheme or not p.hostname:
            return None

        scheme = p.scheme.lower()

        if scheme not in ("http", "https"):
            return None

        # Accessing .port can raise ValueError for malformed ports.
        port = p.port
        host = p.hostname.lower()

        # Preserve IPv6 hostname syntax.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

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

    except (ValueError, TypeError, UnicodeError):
        return None


def safe_join(base, ref):
    """Resolve a reference without allowing malformed URLs to crash."""
    if not ref:
        return None

    ref = str(ref).strip()

    if not ref or ref.startswith("#"):
        return None

    if ref.lower().startswith(SKIP_SCHEMES):
        return None

    try:
        return normalize(urllib.parse.urljoin(base, ref))
    except (ValueError, TypeError, UnicodeError):
        return None


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
            p.query.encode("utf-8")
        ).hexdigest()[:12]

        stem, ext = os.path.splitext(parts[-1])
        parts[-1] = f"{stem}__q_{digest}{ext}"

    hostname = (p.hostname or "").lower()

    if hostname != base_host:
        parts = [
            "_external",
            safe_name(hostname or "unknown"),
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
        depth=3,
        single_page=False,
        no_media=False,
        delay=0.25,
        max_resources=2000,
        timeout=25,
        external_assets=False,
        use_sitemaps=False,
    ):
        if not urllib.parse.urlsplit(url).scheme:
            url = "https://" + url

        self.start = normalize(url)

        if not self.start:
            raise ValueError(f"Invalid starting URL: {url}")

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
        self.use_sitemaps = use_sitemaps

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; WebsiteArchiveBot/1.0)",
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
        self.saved_paths = {}

    def same_origin(self, url):
        try:
            p = urllib.parse.urlsplit(url)
            return (
                p.scheme in ("http", "https")
                and p.netloc.lower() == self.origin_netloc
            )
        except (ValueError, TypeError):
            return False

    def allowed_resource(self, url, kind):
        if self.same_origin(url):
            return True

        # Never crawl external pages.
        if kind == "page":
            return False

        # External assets must be explicitly enabled.
        return self.external_assets

    def enqueue(self, ref, depth, base_url, kind="resource"):
        full = safe_join(base_url, ref)

        if not full:
            return

        try:
            parsed = urllib.parse.urlsplit(full)

            if (
                parsed.scheme not in ("http", "https")
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                return

        except (ValueError, TypeError):
            return

        if not self.allowed_resource(full, kind):
            return

        # Do not download arbitrary external pages as resources.
        # HTML navigation is always restricted to the starting origin.
        if kind == "resource" and not self.same_origin(full):
            suffix = Path(parsed.path).suffix.lower()

            if suffix not in ASSET_EXTENSIONS:
                return

        if full in self.done or full in self.queued:
            return

        if len(self.done) + len(self.queued) >= self.limit:
            return

        self.queued.add(full)
        self.queue.append((full, depth, kind))
        self.discovered.add(full)

    def discover_css(self, text, base, depth):
        refs = []

        for match in CSS_URL.finditer(text):
            ref = (match.group(2) or match.group(3) or "").strip()
            ref = ref.strip("'\"")

            if ref:
                refs.append(ref)

        for match in CSS_IMPORT.finditer(text):
            refs.append(match.group(1).strip())

        for ref in refs:
            self.enqueue(ref, depth, base, "resource")

    def discover_js(self, text, base, depth):
        """Follow only plausible static file imports.

        Arbitrary URLs inside JS strings are deliberately ignored.
        This prevents discovery of video URLs, analytics endpoints,
        documentation links, and unrelated external sites.
        """
        refs = set()

        for match in JS_IMPORT.finditer(text):
            ref = match.group(1).strip()

            if not ref:
                continue

            parsed = urllib.parse.urlsplit(ref)
            suffix = Path(parsed.path).suffix.lower()

            # Ignore bare package names such as "react".
            if (
                ref.startswith(("./", "../", "/"))
                or suffix in ASSET_EXTENSIONS
            ):
                refs.add(ref)

        for ref in refs:
            self.enqueue(ref, depth, base, "resource")

    def discover_html(self, soup, url, depth):
        base_tag = soup.find("base", href=True)
        base_url = safe_join(url, base_tag["href"]) if base_tag else url
        base_url = base_url or url

        # Crawl same-origin HTML navigation links only.
        if depth < self.depth and not self.single_page:
            for tag in soup.find_all("a", href=True):
                self.enqueue(
                    tag["href"], depth + 1, base_url, "page"
                )

        # Resources embedded in HTML.
        resource_tags = [
            "img", "source", "video", "audio", "track",
            "embed", "object", "input",
        ]

        resource_attrs = [
            "src", "data-src", "data-lazy-src", "data-original",
            "data-url", "data-original-src", "data-src-url",
            "poster", "data",
        ]

        for tag in soup.find_all(resource_tags):
            for attr in resource_attrs:
                value = tag.get(attr)

                if value:
                    self.enqueue(
                        value, depth, base_url, "resource"
                    )

            for attr in ("srcset", "data-srcset"):
                value = tag.get(attr)

                if not value:
                    continue

                for item in value.split(","):
                    bits = item.strip().split()

                    if bits:
                        self.enqueue(
                            bits[0], depth, base_url, "resource"
                        )

        # Stylesheets and resource hints.
        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower()

            if any(name in rel.split() for name in (
                "stylesheet", "icon", "preload", "modulepreload",
                "manifest", "prefetch",
            )) or "apple-touch-icon" in rel:
                self.enqueue(
                    tag["href"], depth, base_url, "resource"
                )

        # Actual script files, not URLs mentioned in the HTML.
        for tag in soup.find_all("script", src=True):
            self.enqueue(
                tag["src"], depth, base_url, "resource"
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

        # Do not follow arbitrary URLs from meta tags or refresh links.
        # They can point to unrelated sites or tracking endpoints.

    def discover_sitemap(self, url):
        if url in self.visited_sitemaps:
            return

        if len(self.done) + len(self.queued) >= self.limit:
            return

        self.visited_sitemaps.add(url)
        log.info("Reading sitemap: %s", url)

        try:
            response = self.session.get(
                url, timeout=self.timeout, allow_redirects=True
            )
            response.raise_for_status()
            root = ET.fromstring(response.content)

        except (
            requests.RequestException,
            ET.ParseError,
            ValueError,
        ) as exc:
            log.warning("Sitemap unavailable: %s (%s)", url, exc)
            return

        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1].lower() != "loc":
                continue

            loc = (element.text or "").strip()
            full = safe_join(url, loc)

            if not full or not self.same_origin(full):
                continue

            path = urllib.parse.urlsplit(full).path.lower()

            if path.endswith(".xml"):
                # Follow sitemap indexes, but limit recursion.
                if len(self.visited_sitemaps) < 20:
                    self.discover_sitemap(full)
            else:
                self.enqueue(full, 0, url, "page")

            if len(self.done) + len(self.queued) >= self.limit:
                return

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

            if (
                not ref
                or ref.startswith(SKIP_SCHEMES)
                or ref.startswith("#")
            ):
                return match.group(0)

            full = safe_join(url, ref)

            if not full:
                return match.group(0)

            # Keep external URLs intact unless external assets are enabled.
            if not self.allowed_resource(full, "resource"):
                return match.group(0)

            local = self.rel(full, path)

            if not local:
                return match.group(0)

            return f"url({quote}{local}{quote})"

        return CSS_URL.sub(replace, text)

    def rewrite_html(self, soup, url, path):
        attrs = (
            "src", "poster", "data-src", "data-lazy-src",
            "data-original", "data-url", "data-original-src",
        )

        for tag in soup.find_all(True):
            for attr in attrs:
                value = tag.get(attr)

                if not value or value.startswith("#"):
                    continue

                full = safe_join(url, value)

                if not full:
                    continue

                if not self.allowed_resource(full, "resource"):
                    continue

                # Only rewrite files that were actually processed.
                if full not in self.done:
                    continue

                local = self.rel(full, path)

                if local:
                    tag[attr] = local

            for attr in ("href", "srcset", "data-srcset"):
                value = tag.get(attr)

                if not value:
                    continue

                if attr == "href":
                    full = safe_join(url, value)

                    if not full:
                        continue

                    # Rewrite only downloaded local resources.
                    # Navigation links are handled separately.
                    if (
                        full in self.done
                        and self.same_origin(full)
                        and Path(
                            urllib.parse.urlsplit(full).path
                        ).suffix.lower() in ASSET_EXTENSIONS
                    ):
                        local = self.rel(full, path)

                        if local:
                            tag[attr] = local

                else:
                    items = []

                    for item in value.split(","):
                        bits = item.strip().split()

                        if not bits:
                            continue

                        full = safe_join(url, bits[0])

                        if full and full in self.done:
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
                tag.append(
                    self.rewrite_css(original, url, path)
                )

        # Remove the base URL so relative paths work locally.
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

        self.enqueue(self.start, 0, self.start, "page")

        # Sitemaps are optional to prevent unexpectedly huge crawls.
        if self.use_sitemaps:
            candidates = [
                urllib.parse.urljoin(self.start, "/sitemap.xml"),
                urllib.parse.urljoin(self.start, "/sitemap_index.xml"),
                urllib.parse.urljoin(self.start, "/wp-sitemap.xml"),
            ]

            robots_url = urllib.parse.urljoin(
                self.start, "/robots.txt"
            )

            try:
                response = self.session.get(
                    robots_url, timeout=self.timeout
                )

                if response.status_code < 400:
                    for line in response.text.splitlines():
                        match = re.match(
                            r"^\s*Sitemap\s*:\s*(\S+)",
                            line,
                            re.I,
                        )

                        if match:
                            sitemap = safe_join(
                                robots_url, match.group(1)
                            )

                            if sitemap and self.same_origin(sitemap):
                                candidates.append(sitemap)

            except requests.RequestException as exc:
                log.info("Could not read robots.txt: %s", exc)

            for candidate in dict.fromkeys(candidates):
                if len(self.done) + len(self.queued) >= self.limit:
                    break

                self.discover_sitemap(candidate)

        log.info("Starting: %s", self.start)
        log.info(
            "Output: %s | depth: %d | limit: %d | external assets: %s",
            self.root,
            self.depth,
            self.limit,
            self.external_assets,
        )

        while self.queue and len(self.done) < self.limit:
            url, depth, kind = self.queue.popleft()
            self.queued.discard(url)

            if url in self.done:
                continue

            self.done.add(url)

            log.info(
                "Fetching [%d/%d] depth=%d kind=%s: %s",
                len(self.done), self.limit, depth, kind, url,
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

            if not final_url:
                self.failed.add(url)
                log.warning("Invalid redirect destination: %s", url)
                continue

            # Do not follow redirects into unrelated HTML pages.
            ctype = response.headers.get(
                "Content-Type", ""
            ).lower()

            if (
                not self.same_origin(final_url)
                and "text/html" in ctype
            ):
                log.info("Skipping external HTML redirect: %s", final_url)
                continue

            try:
                path = local_path(
                    url, self.root, self.base_host
                )
            except ValueError as exc:
                self.failed.add(url)
                log.warning("%s", exc)
                continue

            suffix = path.suffix.lower()

            if self.no_media and suffix in MEDIA:
                continue

            data = response.content

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

            if is_html:
                soup = BeautifulSoup(data, "lxml")
                self.discover_html(soup, final_url, depth)

                rewritten = self.rewrite_html(
                    soup, final_url, path
                )

                self.save(
                    path,
                    str(rewritten).encode("utf-8"),
                )

            elif is_css:
                text = data.decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )

                self.discover_css(text, final_url, depth)

                rewritten = self.rewrite_css(
                    text, final_url, path
                )

                self.save(path, rewritten.encode("utf-8"))

            elif is_js:
                text = data.decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )

                self.discover_js(text, final_url, depth)
                self.save(path, data)

            else:
                self.save(path, data)

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
        report_path.write_text(
            json.dumps(report, indent=2),
            encoding="utf-8",
        )

        log.info(
            "Finished. Processed: %d | Failed: %d | Remaining: %d",
            len(self.done),
            len(self.failed),
            len(self.queue),
        )
        log.info("Crawl report: %s", report_path)

        for failed_url in sorted(self.failed)[:50]:
            log.warning("Unfetched URL: %s", failed_url)

        if self.queue:
            log.warning(
                "Resource limit reached or URLs remain queued: %d",
                len(self.queue),
            )


def main():
    parser = argparse.ArgumentParser(
        description="Download a controlled static website archive."
    )

    parser.add_argument("url")
    parser.add_argument("-o", "--output", default=None)

    parser.add_argument(
        "-d", "--depth",
        type=int,
        default=3,
        help="Maximum HTML link depth (default: 3)",
    )

    parser.add_argument(
        "--single-page",
        action="store_true",
        help="Download the start page and its discovered resources only",
    )

    parser.add_argument(
        "--no-media",
        action="store_true",
        help="Skip downloading common media files",
    )

    parser.add_argument(
        "--delay",
        type=float,
        default=0.25,
        help="Delay between downloads in seconds",
    )

    parser.add_argument(
        "--max-resources",
        type=int,
        default=2000,
        help="Maximum URLs to process (default: 2000)",
    )

    parser.add_argument(
        "--timeout",
        type=float,
        default=25,
    )

    parser.add_argument(
        "--external-assets",
        action="store_true",
        help="Allow downloading external CSS, JS, fonts and other assets",
    )

    parser.add_argument(
        "--sitemaps",
        action="store_true",
        help="Discover pages from sitemap files",
    )

    args = parser.parse_args()

    candidate = (
        args.url
        if "://" in args.url
        else "https://" + args.url
    )

    parsed = urllib.parse.urlsplit(candidate)

    if not parsed.hostname:
        parser.error(f"Invalid URL: {args.url}")

    output = args.output or safe_name(parsed.hostname)

    try:
        cloner = WebsiteCloner(
            candidate,
            output,
            depth=args.depth,
            single_page=args.single_page,
            no_media=args.no_media,
            delay=args.delay,
            max_resources=args.max_resources,
            timeout=args.timeout,
            external_assets=args.external_assets,
            use_sitemaps=args.sitemaps,
        )

        cloner.run()

    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
