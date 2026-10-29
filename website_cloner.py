
#!/usr/bin/env python3
"""Playwright-powered static website archiver.

Runs on GitHub Actions; no local installation is required.
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
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)
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
    ".wasm",
}

SKIP_SCHEMES = (
    "data:", "blob:", "javascript:", "mailto:", "tel:",
    "about:", "file:", "intent:", "ftp:",
)

CSS_URL = re.compile(
    r"""url\(\s*(?:(["'])(.*?)\1|([^)]*?))\s*\)""",
    re.I,
)

CSS_IMPORT = re.compile(
    r"""@import\s+(?:url\(\s*)?["']([^"']+)["']""",
    re.I,
)

JS_IMPORT = re.compile(
    r"""(?:\bimport\s*(?:\(\s*)?|\bexport\s+[^;]*?\bfrom\s*)
        ["']([^"'\\]+)["']""",
    re.M | re.X,
)


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    return value.strip(". ") or "_"


def normalize(url):
    """Normalize an HTTP(S) URL and reject malformed input."""
    try:
        url = urllib.parse.urldefrag(str(url).strip())[0]
        parsed = urllib.parse.urlsplit(url)

        if (
            parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None

        scheme = parsed.scheme.lower()
        host = parsed.hostname.lower()
        port = parsed.port

        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        netloc = host

        if port and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            netloc = f"{host}:{port}"

        path = urllib.parse.quote(
            urllib.parse.unquote(parsed.path or "/"),
            safe="/%:@!$&'()*+,;=-._~",
        )

        return urllib.parse.urlunsplit(
            (scheme, netloc, path, parsed.query, "")
        )

    except (ValueError, TypeError, UnicodeError):
        return None


def safe_join(base, ref):
    """Resolve a URL reference without crashing on malformed input."""
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


def local_path(url, root, base_host, is_page=False):
    """Convert a URL to a safe local file path."""
    parsed = urllib.parse.urlsplit(url)

    parts = [
        safe_name(urllib.parse.unquote(part))
        for part in parsed.path.split("/")
        if part
    ]

    if not parts:
        parts = ["index.html"] if is_page else ["index"]

    elif parsed.path.endswith("/"):
        parts.append("index.html" if is_page else "index")

    elif is_page and not Path(parts[-1]).suffix:
        parts[-1] += ".html"

    if parsed.query:
        digest = hashlib.sha256(
            parsed.query.encode("utf-8")
        ).hexdigest()[:12]

        stem, ext = os.path.splitext(parts[-1])
        parts[-1] = f"{stem}__q_{digest}{ext}"

    if (parsed.hostname or "").lower() != base_host:
        parts = [
            "_external",
            safe_name(parsed.hostname or "unknown"),
        ] + parts

    root = Path(root)
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
        scroll=True,
        max_scrolls=25,
        max_asset_bytes=50 * 1024 * 1024,
    ):
        if not urllib.parse.urlsplit(url).scheme:
            url = "https://" + url

        self.start = normalize(url)

        if not self.start:
            raise ValueError(f"Invalid starting URL: {url}")

        parsed = urllib.parse.urlsplit(self.start)

        self.base_host = parsed.hostname.lower()
        self.start_origin = (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
        )

        self.root = Path(output)
        self.depth = 0 if single_page else max(0, depth)
        self.single_page = single_page
        self.no_media = no_media
        self.delay = max(0, delay)
        self.limit = max(1, max_resources)
        self.timeout = max(1, timeout)
        self.external_assets = external_assets
        self.use_sitemaps = use_sitemaps
        self.scroll = scroll
        self.max_scrolls = max(1, max_scrolls)
        self.max_asset_bytes = max(1024, max_asset_bytes)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (compatible; WebsiteArchiveBot/2.0)"
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
        self.discovered = set()
        self.visited_sitemaps = set()
        self.saved_paths = {}
        self.redirects = {}
        self.browser_assets = set()

        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def same_origin(self, url):
        try:
            parsed = urllib.parse.urlsplit(url)
            return (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
            ) == self.start_origin
        except (ValueError, TypeError):
            return False

    def allowed_resource(self, url, kind):
        if self.same_origin(url):
            return True

        # Never crawl HTML pages on other origins.
        if kind == "page":
            return False

        return self.external_assets

    def enqueue(self, ref, depth, base_url, kind="resource"):
        full = safe_join(base_url, ref)

        if not full:
            return

        parsed = urllib.parse.urlsplit(full)

        if not parsed.netloc:
            return

        if not self.allowed_resource(full, kind):
            return

        # External resources must have a recognized asset extension.
        if kind == "resource" and not self.same_origin(full):
            if Path(parsed.path).suffix.lower() not in ASSET_EXTENSIONS:
                return

        if full in self.done or full in self.queued:
            return

        if len(self.done) + len(self.queued) >= self.limit:
            return

        self.queued.add(full)
        self.queue.append((full, depth, kind))
        self.discovered.add(full)

    def start_browser(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=True)

        self.context = self.browser.new_context(
            ignore_https_errors=False,
            service_workers="allow",
            viewport={"width": 1440, "height": 1000},
        )

        self.page = self.context.new_page()
        self.page.set_default_timeout(self.timeout * 1000)

        self.page.on("request", self.on_request)

    def on_request(self, request):
        """Record browser-requested assets for later downloading."""
        try:
            url = normalize(request.url)

            if (
                url
                and self.allowed_resource(url, "resource")
                and request.resource_type in {
                    "stylesheet", "script", "image", "font",
                    "media", "manifest", "texttrack",
                }
            ):
                self.browser_assets.add(url)

        except Exception as exc:
            log.debug("Request observation failed: %s", exc)

    def scroll_page(self):
        """Scroll gradually to trigger common lazy-loading behavior."""
        previous_height = -1
        stable_steps = 0

        for _ in range(self.max_scrolls):
            try:
                height = self.page.evaluate("""
                    () => {
                        const height =
                            document.documentElement.scrollHeight;

                        window.scrollBy(
                            0,
                            Math.max(300, window.innerHeight * 0.8)
                        );

                        return height;
                    }
                """)

                if height == previous_height:
                    stable_steps += 1
                else:
                    stable_steps = 0

                previous_height = height

                if stable_steps >= 3:
                    break

                self.page.wait_for_timeout(200)

            except Exception:
                break

        try:
            self.page.evaluate("window.scrollTo(0, 0)")
            self.page.wait_for_timeout(200)
        except Exception:
            pass

    def render_page(self, url):
        """Render a page with Chromium and return its HTML."""
        try:
            response = self.page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.timeout * 1000,
            )

        except PlaywrightTimeoutError:
            log.warning("Navigation timed out: %s", url)

        except Exception as exc:
            log.warning("Navigation failed: %s (%s)", url, exc)
            return None

        if response is not None and response.status >= 400:
            log.warning("HTTP %s: %s", response.status, url)

        # Some sites never become network-idle, so this wait is bounded.
        try:
            self.page.wait_for_load_state(
                "networkidle",
                timeout=min(5000, self.timeout * 1000),
            )
        except PlaywrightTimeoutError:
            pass

        if self.scroll:
            self.scroll_page()

        try:
            final_url = normalize(self.page.url)

            if not final_url or not self.same_origin(final_url):
                log.warning("Redirect outside starting origin: %s", url)
                return None

            self.redirects[url] = final_url
            return self.page.content()

        except Exception as exc:
            log.warning("Could not read rendered HTML: %s", exc)
            return None

    def discover_css(self, text, base, depth):
        for match in CSS_URL.finditer(text):
            ref = (match.group(2) or match.group(3) or "").strip()
            ref = ref.strip("'\"")

            if ref:
                self.enqueue(ref, depth, base, "resource")

        for match in CSS_IMPORT.finditer(text):
            self.enqueue(
                match.group(1).strip(), depth, base, "resource"
            )

    def discover_js(self, text, base, depth):
        """Discover plausible static JavaScript imports."""
        for match in JS_IMPORT.finditer(text):
            ref = match.group(1).strip()
            parsed = urllib.parse.urlsplit(ref)

            if (
                ref.startswith(("./", "../", "/"))
                or Path(parsed.path).suffix.lower() in ASSET_EXTENSIONS
            ):
                self.enqueue(ref, depth, base, "resource")

    def discover_html(self, soup, url, depth):
        base_tag = soup.find("base", href=True)
        base = safe_join(url, base_tag["href"]) if base_tag else url
        base = base or url

        # Discover same-origin navigation links.
        if depth < self.depth and not self.single_page:
            for tag in soup.find_all("a", href=True):
                self.enqueue(tag["href"], depth + 1, base, "page")

        # Discover common image, media, and lazy-load attributes.
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
                if tag.get(attr):
                    self.enqueue(tag[attr], depth, base, "resource")

            for attr in ("srcset", "data-srcset"):
                value = tag.get(attr)

                if value:
                    for item in value.split(","):
                        bits = item.strip().split()

                        if bits:
                            self.enqueue(
                                bits[0], depth, base, "resource"
                            )

        # Stylesheets, icons, preloads, and manifests.
        for tag in soup.find_all("link", href=True):
            rel = [str(x).lower() for x in tag.get("rel", [])]

            if any(x in rel for x in (
                "stylesheet", "icon", "preload", "modulepreload",
                "manifest", "prefetch", "apple-touch-icon",
            )):
                self.enqueue(tag["href"], depth, base, "resource")

        for tag in soup.find_all("script", src=True):
            self.enqueue(tag["src"], depth, base, "resource")

        # Inline CSS may contain images, fonts, or imported stylesheets.
        for tag in soup.find_all("style"):
            self.discover_css(tag.get_text() or "", base, depth)

        for tag in soup.find_all(style=True):
            self.discover_css(tag.get("style", ""), base, depth)

    def discover_rendered_page(self, url, depth):
        """Collect links and assets visible after JavaScript runs."""
        try:
            links = self.page.locator("a[href]").evaluate_all(
                "elements => elements.map(element => element.href)"
            )

            if depth < self.depth and not self.single_page:
                for link in links:
                    self.enqueue(link, depth + 1, url, "page")

            for asset in list(self.browser_assets):
                self.enqueue(asset, depth, url, "resource")

        except Exception as exc:
            log.debug("Rendered discovery failed: %s", exc)

    def discover_sitemap(self, url):
        """Discover same-origin pages from a sitemap."""
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

            if len(response.content) > 10 * 1024 * 1024:
                log.warning("Sitemap exceeds size limit: %s", url)
                return

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

            full = safe_join(url, (element.text or "").strip())

            if not full or not self.same_origin(full):
                continue

            if urllib.parse.urlsplit(full).path.lower().endswith(".xml"):
                if len(self.visited_sitemaps) < 20:
                    self.discover_sitemap(full)
            else:
                self.enqueue(full, 0, url, "page")

            if len(self.done) + len(self.queued) >= self.limit:
                return

    def relative_asset(self, url, path):
        """Return the relative local path for a downloaded resource."""
        try:
            target = local_path(
                url,
                self.root,
                self.base_host,
                is_page=False,
            )

            return os.path.relpath(
                target, path.parent
            ).replace("\\", "/")

        except (ValueError, OSError):
            return None

    def rewrite_css(self, text, url, path):
        """Rewrite CSS URLs only when their assets were downloaded."""

        def replace_url(match):
            quote = match.group(1) or ""
            ref = (
                match.group(2) or match.group(3) or ""
            ).strip().strip("'\"")

            if (
                not ref
                or ref.startswith("#")
                or ref.lower().startswith(SKIP_SCHEMES)
            ):
                return match.group(0)

            full = safe_join(url, ref)

            if (
                not full
                or full not in self.done
                or not self.allowed_resource(full, "resource")
            ):
                return match.group(0)

            local = self.relative_asset(full, path)

            if not local:
                return match.group(0)

            return f"url({quote}{local}{quote})"

        text = CSS_URL.sub(replace_url, text)

        def replace_import(match):
            ref = match.group(1).strip()
            full = safe_join(url, ref)

            if (
                not full
                or full not in self.done
                or not self.allowed_resource(full, "resource")
            ):
                return match.group(0)

            local = self.relative_asset(full, path)

            if not local:
                return match.group(0)

            return match.group(0).replace(ref, local, 1)

        return CSS_IMPORT.sub(replace_import, text)

    def rewrite_html(self, soup, url, path):
        """Rewrite references to assets saved in the archive."""

        resource_attrs = (
            "src", "poster", "data-src", "data-lazy-src",
            "data-original", "data-url", "data-original-src",
            "data-src-url",
        )

        for tag in soup.find_all(True):
            for attr in resource_attrs:
                value = tag.get(attr)

                if not value or value.startswith("#"):
                    continue

                full = safe_join(url, value)

                if (
                    not full
                    or full not in self.done
                    or not self.allowed_resource(full, "resource")
                ):
                    continue

                local = self.relative_asset(full, path)

                if local:
                    tag[attr] = local

            # Rewrite downloaded asset links, but keep page navigation
            # links intact.
            value = tag.get("href")

            if value:
                full = safe_join(url, value)

                if (
                    full
                    and full in self.done
                    and Path(
                        urllib.parse.urlsplit(full).path
                    ).suffix.lower() in ASSET_EXTENSIONS
                    and self.allowed_resource(full, "resource")
                ):
                    local = self.relative_asset(full, path)

                    if local:
                        tag["href"] = local

            for attr in ("srcset", "data-srcset"):
                value = tag.get(attr)

                if not value:
                    continue

                rewritten_items = []

                for item in value.split(","):
                    bits = item.strip().split()

                    if not bits:
                        continue

                    full = safe_join(url, bits[0])

                    if full and full in self.done:
                        local = self.relative_asset(full, path)

                        if local:
                            bits[0] = local

                    rewritten_items.append(" ".join(bits))

                tag[attr] = ", ".join(rewritten_items)

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

        # Remove base tags so they do not override local paths.
        for tag in soup.find_all("base"):
            tag.decompose()

        return soup

    def save(self, path, data):
        """Create parent directories and save a file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        try:
            shown = path.relative_to(self.root)
        except ValueError:
            shown = path

        log.info("Saved: %s", shown)

    def fetch_resource(self, url):
        """Download one resource with a maximum response-size limit."""
        try:
            with self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                stream=True,
            ) as response:
                response.raise_for_status()

                final_url = normalize(response.url)

                if (
                    not final_url
                    or not self.allowed_resource(
                        final_url, "resource"
                    )
                ):
                    raise ValueError(
                        "Redirect to a disallowed resource"
                    )

                content_length = response.headers.get(
                    "Content-Length"
                )

                if content_length:
                    try:
                        if int(content_length) > self.max_asset_bytes:
                            raise ValueError(
                                "Resource exceeds size limit"
                            )
                    except ValueError as exc:
                        if str(exc) == "Resource exceeds size limit":
                            raise

                chunks = []
                size = 0

                for chunk in response.iter_content(64 * 1024):
                    if not chunk:
                        continue

                    size += len(chunk)

                    if size > self.max_asset_bytes:
                        raise ValueError(
                            "Resource exceeds size limit"
                        )

                    chunks.append(chunk)

                return (
                    final_url,
                    b"".join(chunks),
                    response.headers.get(
                        "Content-Type", ""
                    ).lower(),
                )

        except (requests.RequestException, ValueError) as exc:
            log.warning(
                "Resource download failed: %s (%s)",
                url,
                exc,
            )
            return None, None, None

    def process_resource(self, url):
        """Download and save a resource, processing CSS and JS."""
        final_url, data, content_type = self.fetch_resource(url)

        if data is None:
            self.failed.add(url)
            return

        suffix = Path(
            urllib.parse.urlsplit(final_url).path
        ).suffix.lower()

        if self.no_media and suffix in MEDIA:
            return

        try:
            path = local_path(
                url,
                self.root,
                self.base_host,
                is_page=False,
            )

            if "text/css" in content_type or suffix == ".css":
                text = data.decode("utf-8", errors="replace")

                self.discover_css(text, final_url, 0)

                rewritten = self.rewrite_css(
                    text, final_url, path
                )

                self.save(path, rewritten.encode("utf-8"))

            elif (
                "javascript" in content_type
                or "ecmascript" in content_type
                or suffix in (".js", ".mjs", ".cjs")
            ):
                text = data.decode("utf-8", errors="replace")

                self.discover_js(text, final_url, 0)
                self.save(path, data)

            else:
                self.save(path, data)

            self.saved_paths[url] = str(path)

            if final_url != url:
                self.saved_paths[final_url] = str(path)

        except (ValueError, OSError) as exc:
            self.failed.add(url)
            log.warning(
                "Could not save resource %s: %s", url, exc
            )

    def process_page(self, url, depth):
        """Render a page and save its resulting HTML."""
        html = self.render_page(url)

        if html is None:
            self.failed.add(url)
            return

        try:
            final_url = self.redirects.get(url, url)

            path = local_path(
                url,
                self.root,
                self.base_host,
                is_page=True,
            )

            soup = BeautifulSoup(html, "lxml")

            self.discover_html(soup, final_url, depth)
            self.discover_rendered_page(final_url, depth)

            rewritten = self.rewrite_html(
                soup, final_url, path
            )

            self.save(
                path,
                str(rewritten).encode("utf-8"),
            )

            self.saved_paths[url] = str(path)
            self.saved_paths[final_url] = str(path)

        except (ValueError, OSError) as exc:
            self.failed.add(url)
            log.warning(
                "Could not save page %s: %s", url, exc
            )

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)

        self.start_browser()

        try:
            self.enqueue(self.start, 0, self.start, "page")

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
            log.info("Output: %s", self.root)
            log.info("Depth: %d | Limit: %d", self.depth, self.limit)

            while self.queue and len(self.done) < self.limit:
                url, depth, kind = self.queue.popleft()
                self.queued.discard(url)

                if url in self.done:
                    continue

                self.done.add(url)

                log.info(
                    "Processing [%d/%d] depth=%d type=%s: %s",
                    len(self.done),
                    self.limit,
                    depth,
                    kind,
                    url,
                )

                if kind == "page":
                    self.process_page(url, depth)
                else:
                    self.process_resource(url)

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
                "saved_files": self.saved_paths,
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
                    "Some URLs remain queued: %d", len(self.queue)
                )

        finally:
            if self.context:
                self.context.close()

            if self.browser:
                self.browser.close()

            if self.playwright:
                self.playwright.stop()

            self.session.close()


def main():
    parser = argparse.ArgumentParser(
        description="Archive a JavaScript-rendered website with Playwright."
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
        help="Download only the starting page and discovered resources",
    )

    parser.add_argument(
        "--no-media",
        action="store_true",
        help="Skip common media files",
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
        default=30,
        help="Page and request timeout in seconds",
    )

    parser.add_argument(
        "--external-assets",
        action="store_true",
        help="Allow downloading recognized assets from external origins",
    )

    parser.add_argument(
        "--sitemaps",
        action="store_true",
        help="Discover pages from sitemap files",
    )

    parser.add_argument(
        "--no-scroll",
        action="store_true",
        help="Disable automatic scrolling for lazy-loaded content",
    )

    parser.add_argument(
        "--max-scrolls",
        type=int,
        default=25,
        help="Maximum scrolling steps per page",
    )

    parser.add_argument(
        "--max-asset-mb",
        type=float,
        default=50,
        help="Maximum individual resource size in MB",
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
            scroll=not args.no_scroll,
            max_scrolls=args.max_scrolls,
            max_asset_bytes=max(
                1024, int(args.max_asset_mb * 1024 * 1024)
            ),
        )

        cloner.run()

    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
