
import re
from pathlib import Path
from urllib.parse import urlparse

# Folder containing the downloaded website files
root = Path("youretheblessed.com")

# Patterns for finding URLs in JavaScript files
patterns = [
    re.compile(r'https?://[^\s"\'`<>]+'),
    re.compile(r'["\'`](/[^"\'`\s<>]{1,300})["\'`]'),
]

found = set()

if not root.exists():
    raise SystemExit(
        f"Folder not found: {root}\n"
        "Make sure youretheblessed.com is in the repository."
    )

# Scan all JavaScript files, including subfolders
js_files = list(root.rglob("*.js"))
print(f"Scanning {len(js_files)} JavaScript files...\n")

for path in js_files:
    try:
        source = path.read_text(
            encoding="utf-8",
            errors="ignore"
        )
    except OSError as exc:
        print(f"Could not read {path}: {exc}")
        continue

    for pattern in patterns:
        for match in pattern.findall(source):
            value = match.rstrip("),;")

            if value.startswith(("http://", "https://")):
                hostname = urlparse(value).hostname

                if hostname and hostname.lower() == "youretheblessed.com":
                    found.add(value)

            elif value.startswith("/") and not value.startswith("//"):
                found.add(
                    "https://youretheblessed.com" + value
                )

# Print sorted results
for url in sorted(found):
    print(url)

print(f"\nTotal candidate URLs: {len(found)}")

# Save results to a text file
output_file = Path("found_urls.txt")
output_file.write_text(
    "\n".join(sorted(found)) + ("\n" if found else ""),
    encoding="utf-8"
)

print(f"Results saved to: {output_file}")
