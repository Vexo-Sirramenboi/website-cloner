# 🌐 Website Cloner

A Python tool that downloads a complete website — including HTML, CSS, JavaScript, images, fonts, and other media — to a local directory. All URLs are rewritten to relative paths so the cloned site works offline in your browser.

## Features

- **Full page download** — HTML, CSS, JS, images, fonts, icons, videos, and more
- **URL rewriting** — All asset references are rewritten to relative local paths so the site works offline
- **CSS parsing** — Extracts and downloads nested assets from CSS files (`url()`, `@import`, fonts, background images)
- **Single-page mode** — Download only one page and its direct assets without following links
- **Recursive crawling** — Optionally crawl an entire site up to a configurable depth
- **Auto-named output** — Output directory is derived from the URL automatically (e.g. `example.com`)
- **Deduplication** — Files are only downloaded once, even if referenced multiple times
- **Polite crawling** – Configurable delay between requests

## Requirements

- Python 3.8+
- [requests](https://pypi.org/project/requests/)
- [beautifulsoup4](https://pypi.org/project/beautifulsoup4/)
- [lxml](https://pypi.org/project/lxml/)

## Installation

```bash
git clone https://github.com/Vexo-Sirramenboi/website-clone.git
cd website-cloner
pip install -r requirements.txt
```

## Quick Start

```bash
# Download a single page with all its assets
python website_cloner.py https://example.com --single-page

# Crawl an entire site (depth 3)
python website_cloner.py https://example.com --depth 3

# Custom output directory
python website_cloner.py https://example.com --single-page --output ./my-clone
```

## Usage

```
python website_cloner.py <URL> [OPTIONS]
```

| Option          | Default     | Description                                                        |
| --------------- | ----------- | ------------------------------------------------------------------ |
| `URL`           | —           | URL of the website to clone (**required**)                         |
| `--output`, `-o`| URL-derived | Output directory (default: domain name, e.g. `example.com`)        |
| `--depth`, `-d` | `5`         | Maximum crawl depth for recursive mode                             |
| `--single-page` | `false`     | Only download the given page and its direct assets                 |
| `--no-media`    | `false`     | Skip images, videos, fonts, etc.                                   |
| `--delay`       | `0.3`       | Delay between requests in seconds                                  |

## Examples

### Single page (no crawling)

Downloads only `example.com/login/` and all assets referenced on that page — CSS, JavaScript, images, fonts. Does **not** follow links to other pages.

```bash
python website_cloner.py https://example.com/login --single-page
```

Output:
```
www.example.com/
├── index.html          # the login page
├── css/
│   ├── layout.css
│   └── auth.css
├── js/
│   └── login.js
└── img/
    └── logo.svg
```

### Full site crawl

Recursively crawls and downloads the entire site, following same-domain links up to the specified depth.

```bash
python website_cloner.py https://example.com --depth 3
```

### Skip media files

Download only HTML and CSS — useful when you only need the page structure.

```bash
python website_cloner.py https://example.com --single-page --no-media
```

## How It Works

1. **Fetch** the HTML page
2. **Parse** all asset references: `<img>`, `<link>`, `<script>`, `<source>`, `<style>`, inline `srcset`, and CSS `url()`
3. **Download** each asset, preserving the URL's directory structure
4. **Rewrite** all asset URLs in the HTML to relative local paths (e.g. `css/style.css`)
5. **Rewrite** all `url()` references in downloaded CSS files as well
6. **Queue** same-domain HTML pages for recursive crawling (if not in single-page mode)
7. **Save** everything to the output directory

## Limitations

- **JavaScript-rendered content**: Pages that load content dynamically via JavaScript (e.g. SPAs, AJAX) won't have their dynamic content captured. Only the initial HTML is downloaded.
- **Server-side resources**: Server-side logic (PHP, Python, databases, APIs) is obviously not captured.
- **Authentication**: Login-protected pages are not supported.
- **Rate limits**: Some servers may block or rate-limit rapid requests. Use `--delay` to increase the time between requests.

## License
GNU GENERAL PUBLIC LICENSE
