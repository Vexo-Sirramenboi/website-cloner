
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

BASE_URL = "https://youretheblessed.com/"
ROOT = Path("youretheblessed.com")
OUTPUT = Path("found_urls.txt")

PATTERNS = [
    re.compile(r"""https?://[^\s"'`<>\\]+"""),
    re.compile(r"""["'`](/[^"'`\s<>]{1,300})["'`]"""),
]

REGEX_FRAGMENTS = (
    ".*", ".+", ".*?", ".+?", "([^", "(?:",
    "\\", "$", "^", "[", "]", "{", "}",
)

found = set()
files = list(ROOT.rglob("*.js"))

print(f"Scanning {len(files)} JavaScript files...")

for path in files:
    try:
        source = path.read_text(
            encoding="utf-8",
            errors="ignore",
        )
    except OSError:
        continue

    for pattern in PATTERNS:
        for match in pattern.findall(source):
            value = match.rstrip("),;.")

            if not value or any(
                marker in value for marker in REGEX_FRAGMENTS
            ):
                continue

            if value.startswith("http://") or value.startswith("https://"):
                parsed = urlparse(value)

                if parsed.hostname != "youretheblessed.com":
                    continue
            else:
                parsed = urlparse(value)

                if not parsed.path.startswith("/"):
                    continue

                value = urljoin(BASE_URL, value)
                parsed = urlparse(value)

                if parsed.hostname != "youretheblessed.com":
                    continue

            if parsed.path in ("", "/") and not parsed.query:
                continue

            found.add(value)

OUTPUT.write_text(
    "\n".join(sorted(found)) + ("\n" if found else ""),
    encoding="utf-8",
)

print(f"\nTotal candidate URLs: {len(found)}")
print(f"Results saved to: {OUTPUT}")
