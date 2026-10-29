#!/usr/bin/env python3
"""Bounded static website downloader with URL-based paths and JS/CSS discovery.

Install: py -m pip install requests beautifulsoup4 lxml
Usage:   py website_cloner_fixed.py https://example.com --depth 2
Only archive websites you own or have permission to copy. This is not a browser;
dynamic APIs and runtime-generated assets may not be captured.
"""
import argparse
import hashlib
import logging
import os
import re
import time
import urllib.parse
from collections import deque
from pathlib import Path

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("website_cloner")
MEDIA = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif", ".mp4", ".webm", ".ogg", ".mp3", ".wav", ".flac", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".pdf", ".zip", ".gz", ".tar", ".rar"}
CSS_URL = re.compile(r'''url\(\s*(["']?)(.*?)\1\s*\)''', re.I)
CSS_IMPORT = re.compile(r'''@import\s+(?:url\(\s*)?(["'])(.*?)\1\s*\)?''', re.I)

JS_ASSET = re.compile(
    r"""["'`]((
        (?:https?:)?//[^"'`\s]+
        |
        (?:\.\.?/|/)?[^"'`\s?#]+
    )\.(?:js|mjs|cjs|css|wasm|json|map|"
    r"""r"""woff2?|ttf|eot|otf|svg|png|jpe?g|webp|avif|gif|"
    r"""r"""ico|bmp|mp4|webm|mp3|pdf)(?:\?[^"'`\s]*)?)["'`]""",
    re.I | re.X,
)
SKIP = ("data:", "blob:", "javascript:", "mailto:", "tel:", "about:")


def safe_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(". ") or "_"


def normalize(url):
    url, _ = urllib.parse.urldefrag(url.strip())
    p = urllib.parse.urlsplit(url)
    if not p.scheme or not p.hostname:
        return url
    scheme, host = p.scheme.lower(), p.hostname.lower()
    port = p.port
    netloc = f"{host}:{port}" if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)) else host
    path = urllib.parse.quote(urllib.parse.unquote(p.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    return urllib.parse.urlunsplit((scheme, netloc, path, p.query, ""))


def local_path(url, root, base_host):
    p = urllib.parse.urlsplit(url)
    parts = [safe_name(urllib.parse.unquote(x)) for x in p.path.split("/") if x]
    if not parts:
        parts = ["index.html"]
    if not Path(parts[-1]).suffix:
        parts[-1] += ".html"
    if p.query:
        digest = hashlib.sha1(p.query.encode()).hexdigest()[:10]
        stem, ext = os.path.splitext(parts[-1])
        parts[-1] = f"{stem}__q_{digest}{ext}"
    if (p.hostname or "").lower() != base_host:
        parts = ["_external", safe_name(p.hostname or "unknown")] + parts
    target = root.joinpath(*parts)
    resolved_root, resolved_target = root.resolve(), target.resolve()
    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise ValueError(f"Unsafe output path: {url}")
    return target


class WebsiteCloner:
    def __init__(self, url, output, depth=2, single_page=False, no_media=False, delay=.25, max_resources=2000, timeout=25):
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
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36", "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
        self.queue = deque()
        self.queued = set()
        self.done = set()
        self.failed = set()

    def same_origin(self, url):
        p = urllib.parse.urlsplit(url)
        return p.scheme in ("http", "https") and p.netloc.lower() == self.origin_netloc

    def enqueue(self, url, depth, base_url):
        if not url or url.strip().lower().startswith(SKIP):
            return
        full = normalize(urllib.parse.urljoin(base_url, url))
        p = urllib.parse.urlsplit(full)
        if p.scheme not in ("http", "https") or not p.netloc or not self.same_origin(full):
            return
        if full not in self.done and full not in self.queued and len(self.done) + len(self.queued) < self.limit:
            self.queued.add(full)
            self.queue.append((full, depth))

    def discover_css(self, text, base, depth):
        for rx in (CSS_URL, CSS_IMPORT):
            for m in rx.finditer(text):
                ref = m.group(2).strip().strip("'\"")
                if ref and not ref.startswith(SKIP) and not ref.startswith("#"):
                    self.enqueue(ref, depth, base)

    def discover_js(self, text, base, depth):
        # Resolve every match against the URL of the JavaScript file, never its local path.
        count = 0
        for m in JS_ASSET.finditer(text):
            self.enqueue(m.group(1), depth, base)
            count += 1
            if count >= 500:
                break


    def discover_html(self, soup, url, depth):
        base_tag = soup.find("base", href=True)
        base_url = (
            urllib.parse.urljoin(url, base_tag["href"])
            if base_tag else url
        )

        # Discover linked HTML pages.
        if depth < self.depth and not self.single_page:
            for tag in soup.find_all("a", href=True):
                self.enqueue(tag["href"], depth + 1, base_url)

        # Discover embedded resources and lazy-loaded media.
        resource_tags = [
            "img", "source", "video", "audio", "track",
            "embed", "iframe", "object", "input"
        ]

        resource_attrs = [
            "src", "data-src", "data-lazy-src",
            "data-original", "data-url", "data-original-src",
            "poster", "data"
        ]

        for tag in soup.find_all(resource_tags):
            for attr in resource_attrs:
                value = tag.get(attr)
                if value:
                    self.enqueue(value, depth, base_url)

            # Discover responsive images.
            srcset = tag.get("srcset") or tag.get("data-srcset")
            if srcset:
                for item in srcset.split(","):
                    bits = item.strip().split()
                    if bits:
                        self.enqueue(bits[0], depth, base_url)

        # Discover stylesheets, icons, manifests and preloaded files.
        for tag in soup.find_all("link", href=True):
            rel = " ".join(tag.get("rel", [])).lower()

            if any(x in rel for x in (
                "stylesheet", "icon", "preload",
                "modulepreload", "manifest",
                "apple-touch-icon", "prefetch"
            )):
                self.enqueue(tag["href"], depth, base_url)

        # Discover external JavaScript.
        for tag in soup.find_all("script", src=True):
            self.enqueue(tag["src"], depth, base_url)

        # Discover URLs in inline CSS.
        for tag in soup.find_all("style"):
            self.discover_css(
                tag.get_text() or "", base_url, depth
            )

        for tag in soup.find_all(style=True):
            self.discover_css(
                tag.get("style", ""), base_url, depth
            )

        # Discover meta-refresh redirects.
        for tag in soup.find_all(
            "meta", attrs={"http-equiv": True}
        ):
            if tag.get("http-equiv", "").lower() == "refresh":
                content = tag.get("content", "")
                match = re.search(
                    r"""url\s*=\s*['"]?(.+?)['"]?$""",
                    content,
                    re.I
                )
                if match:
                    self.enqueue(
                        match.group(1), depth + 1, base_url
                    )

    def rel(self, asset_url, from_path):
        try:
            return os.path.relpath(local_path(asset_url, self.root, self.base_host), from_path.parent).replace("\\", "/")
        except (ValueError, OSError):
            return None

    def rewrite_css(self, text, url, path):
        def sub(m):
            ref = m.group(2).strip().strip("'\"")
            if not ref or ref.startswith(SKIP) or ref.startswith("#"): return m.group(0)
            full = normalize(urllib.parse.urljoin(url, ref))
            if not self.same_origin(full): return m.group(0)
            relative = self.rel(full, path)
            return f"url({m.group(1)}{relative}{m.group(1)})" if relative else m.group(0)
        return CSS_URL.sub(sub, text)

    def rewrite_html(self, soup, url, path):
        for tag in soup.find_all(True):
            for attr in ("src", "href", "poster", "data-src", "data-lazy-src", "action"):
                value = tag.get(attr)
                if not value or value.strip().lower().startswith(SKIP): continue
                full = normalize(urllib.parse.urljoin(url, value))
                if self.same_origin(full):
                    relative = self.rel(full, path)
                    if relative: tag[attr] = relative
            if tag.get("srcset"):
                out = []
                for item in tag["srcset"].split(","):
                    bits = item.strip().split()
                    if bits:
                        full = normalize(urllib.parse.urljoin(url, bits[0]))
                        if self.same_origin(full):
                            relative = self.rel(full, path)
                            if relative: bits[0] = relative
                        out.append(" ".join(bits))
                tag["srcset"] = ", ".join(out)
            if tag.get("style"):
                tag["style"] = self.rewrite_css(tag["style"], url, path)
        for tag in soup.find_all("style"):
            original = tag.get_text()
            if original:
                tag.clear()
                tag.append(self.rewrite_css(original, url, path))
        return soup

    def save(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        log.info("Saved: %s", path.relative_to(self.root))

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.enqueue(self.start, 0, self.start)
        log.info("Starting %s", self.start)
        log.info("Output: %s | HTML depth: %d | resource limit: %d", self.root, self.depth, self.limit)
        while self.queue and len(self.done) < self.limit:
            url, depth = self.queue.popleft()
            self.queued.discard(url)
            if url in self.done: continue
            self.done.add(url)
            log.info("Fetching [%d]: %s", depth, url)
            try:
                response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                response.raise_for_status()
            except requests.RequestException as exc:
                self.failed.add(url)
                log.warning("Failed: %s (%s)", url, exc)
                continue
            final_url = normalize(response.url)
            path = local_path(url, self.root, self.base_host)
            ctype = response.headers.get("Content-Type", "").lower()
            suffix = path.suffix.lower()
            if self.no_media and suffix in MEDIA: continue
            data = response.content
            if "text/html" in ctype or suffix in (".html", ".htm"):
                soup = BeautifulSoup(response.text, "lxml")
                self.discover_html(soup, final_url, depth)
                self.save(path, str(self.rewrite_html(soup, final_url, path)).encode("utf-8"))
            elif "text/css" in ctype or suffix == ".css":
                text = data.decode(response.encoding or "utf-8", errors="replace")
                self.discover_css(text, final_url, depth)
                self.save(path, self.rewrite_css(text, final_url, path).encode("utf-8"))
            elif any(x in ctype for x in ("javascript", "ecmascript")) or suffix in (".js", ".mjs"):
                text = data.decode(response.encoding or "utf-8", errors="replace")
                self.discover_js(text, final_url, depth)
                self.save(path, data)
            else:
                self.save(path, data)
            time.sleep(self.delay)
        log.info("Finished: %d unique URLs; %d failed.", len(self.done), len(self.failed))
        if self.queue: log.warning("Resource limit reached; %d URLs remain queued.", len(self.queue))
        for url in sorted(self.failed)[:20]: log.warning("Failed URL: %s", url)


def main():
    parser = argparse.ArgumentParser(description="Download a static copy of a website for authorized archiving.")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", default=None)
    parser.add_argument( "-d", "--depth", type=int, default=4, help="Maximum HTML link depth (default: 4)")
    parser.add_argument("--single-page", action="store_true")
    parser.add_argument("--no-media", action="store_true")
    parser.add_argument("--delay", type=float, default=.25)
    parser.add_argument("--max-resources", type=int, default=5000, help="Maximum URLs to fetch (default: 5000)")
    parser.add_argument("--timeout", type=float, default=25)
    args = parser.parse_args()
    candidate = args.url if "://" in args.url else "https://" + args.url
    host = urllib.parse.urlsplit(candidate).hostname or "cloned_site"
    output = args.output or safe_name(host)
    WebsiteCloner(candidate, output, args.depth, args.single_page, args.no_media, args.delay, args.max_resources, args.timeout).run()

if __name__ == "__main__":
    main()
