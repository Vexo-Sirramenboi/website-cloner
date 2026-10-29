
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

BASE_URL = "https://youretheblessed.com/"
ROOT = Path("youretheblessed.com")
OUTPUT = Path("found_urls.txt")

PATTERNS = [
    # Absolute HTTP and HTTPS URLs
    re.compile(r"""https?://[^\s"'`<>\\]+"""),

    # Quoted root-relative paths
    re.compile(r"""["'`](/[^"'`\s<>]{1,300})["'`]"""),

    # Common URL-bearing JavaScript properties
    re.compile(
        r"""(?:url|href|endpoint|api|src|path)\s*[:=]\s*
        ["'`]([^"'`]{1,300})["'`]""",
        re.IGNORECASE | re.VERBOSE,
    ),
]

def clean_candidate(value):
    value = value.strip().rstrip("),;.")

    if not value:
        return None

    # Reject obvious regular-expression fragments.
    if any(char in value for char in ("\n", "\r", "\t")):
        return None

    if value.startswith(("//", "#", "?", "\\")):
        return None

    if value.startswith(("data:", "javascript:", "blob:")):
        return None

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)

        if parsed.hostname != "youretheblessed.com":
            return None

        return value

    # Keep only root-relative paths or plausible relative URLs.
    if value.startswith("/"):
        result = urljoin(BASE_URL, value)
    else:
        # Avoid treating arbitrary JavaScript strings as URLs.
        if not value.startswith(("./", "../")):
            return None
        result = urljoin(BASE_URL, value)

    parsed = urlparse(result)

    if parsed.hostname != "youretheblessed.com":
        return None

    if not parsed.path or parsed.path == "/":
        return None

    return result


def main():
    if not ROOT.is_dir():
        print(f"ERROR: Folder not found: {ROOT}")
        print("Make sure the folder is committed to your repository.")
        return

    files = sorted(ROOT.rglob("*.js"))
    found = set()

    print(f"Scanning {len(files)} JavaScript files...\n")

    for path in files:
        try:
            source = path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        except OSError as exc:
            print(f"Could not read {path}: {exc}")
            continue

        for pattern in PATTERNS:
            for match in pattern.findall(source):
                candidate = clean_candidate(match)

                if candidate:
                    found.add(candidate)

    OUTPUT.write_text(
        "\n".join(sorted(found))
        + ("\n" if found else ""),
        encoding="utf-8",
    )

    print("=" * 50)
    print("DISCOVERED URL CANDIDATES")
    print("=" * 50)

    if found:
        for url in sorted(found):
            print(url)
    else:
        print("No candidate URLs found.")

    print(f"\nTotal candidate URLs: {len(found)}")
    print(f"Results saved to: {OUTPUT.resolve()}")


if __name__ == "__main__":
    main()
