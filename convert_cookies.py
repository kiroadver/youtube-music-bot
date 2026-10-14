#!/usr/bin/env python3
"""Convert browser-exported JSON cookies to Netscape cookies.txt for yt-dlp."""
import json
import sys
from datetime import datetime, timezone


def convert(items: list[dict], out_path: str = "cookies.txt") -> int:
    """items: list of JSON cookie objects (Chrome/Edge/Firefox export)."""
    lines = [
        "# Netscape HTTP Cookie File",
        "# Exported for yt-dlp YouTube Music Bot",
    ]
    now = datetime.now(timezone.utc).timestamp()
    written = 0

    for c in items:
        domain = c.get("domain", "")
        host_only = bool(c.get("hostOnly", False))

        # Netscape format: "TRUE" means host-only domain, "FALSE" means subdomain match
        if host_only:
            domain_field = "TRUE"
            domain_out = domain.lstrip(".")
        else:
            domain_field = "FALSE"
            domain_out = domain

        name = c.get("name", "")
        value = c.get("value", "")
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure", False) else "FALSE"

        samesite_raw = c.get("sameSite") or ""
        samesite = {"no_restriction": "none", "lax": "lax", "strict": "strict"}.get(samesite_raw, "none")

        exp = c.get("expirationDate")
        if exp is None or c.get("session", False):
            exp_epoch = 0
        else:
            exp_epoch = float(exp)

        line = f"{domain_field}\t{domain_out}\t{name}\t{value}\t{exp_epoch:.0f}\t{path}\t{secure}\t{samesite}"
        lines.append(line)
        written += 1

    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return written


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "input.json"
    dst = sys.argv[2] if len(sys.argv) > 2 else "cookies.txt"
    with open(src) as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("cookies") or []
    n = convert(data, dst)
    print(f"Converted {n} cookies -> {dst}")
