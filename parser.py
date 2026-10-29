
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path("youretheblessed.com")
BASE_URL = "https://youretheblessed.com/"
OUTPUT = Path("found_urls.txt")

# Match quoted strings, including escaped characters.
STRING_RE = re.compile(
    r"""(["'`])((?:\\.|(?!\1)[^\\])*?)\1""",
    re.DOTALL,
)

ABSOLUTE_RE = re.compile(r"https?://[^\s\"'`<>\\]+", re.I)

# Obvious regex fragments that must never become routes.
BAD_PATHS = {
    "$", "&", "*", "\\", "g", "h", "l",
}

def clean_url(value):
    value = value.strip().rstrip(".,;:)")

    if not value or len(value) > 2048:
        return None

    if any(c in value for c in "\r\n\t"):
        return None

    if value.startswith(("//", "#", "?", "\\")):
        return None

    if value in BAD_PATHS:
        return None

    if re.search(r"\(\?[:=!<]|\\[dDsSwWbBAZ]|\\\(|\\\)|\[\^", value):
        return None

    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)

        if parsed.hostname != "youretheblessed.com":
            return None

        if parsed.path in ("", "/") and not parsed.query:
            return None

        return value

    if not value.startswith("/"):
        return None

    if value.startswith(("/$", "/*", "/&", "/\\")):
        return None

    result = urljoin(BASE_URL, value)
    parsed = urlparse(result)

    if parsed.hostname != "youretheblessed.com":
        return None

    if parsed.path in ("", "/"):
        return None

    return result


def main():
    if not ROOT.is_dir():
        print(f"ERROR: Folder not found: {ROOT}")
        return

    files = sorted(ROOT.rglob("*.js"))
    found = set()

    print(f"Scanning {len(files)} JavaScript files...")

    for path in files:
        try:
            source = path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        except OSError as exc:
            print(f"Could not read {path}: {exc}")
            continue

        # Extract absolute URLs separately.
        for match in ABSOLUTE_RE.findall(source):
            url = clean_url(match)
            if url:
                found.add(url)

        # Inspect quoted strings for relative routes.
        for match in STRING_RE.finditer(source):
            value = match.group(2)

            # Decode common escaped slashes.
            value = value.replace(r"\/", "/")

            url = clean_url(value)
            if url:
                found.add(url)

    OUTPUT.write_text(
        "\n".join(sorted(found))
        + ("\n" if found else ""),
        encoding="utf-8",
    )

    print("\nDISCOVERED URL CANDIDATES")
    print("=" * 40)

    for url in sorted(found):
        print(url)

    print(f"\nTotal candidate URLs: {len(found)}")
    print(f"Results saved to: {OUTPUT}")


if __name__ == "__main__":
    main()
