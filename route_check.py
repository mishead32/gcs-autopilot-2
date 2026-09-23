"""Every link and every button must point at a route that exists.

A form posting to a URL nobody registered fails silently in development and
shows the user a bare {"detail":"Not Found"} in production. That happened once
with "Add company", which posted to /branches while the route lived at
/admin/branches. This walks every template, pulls out every href and every
form action, and checks each against the app's real routing table.

Run it on its own:      python route_check.py
It also runs as part of smoke_test.py, so it can't be forgotten.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from app.main import app

TEMPLATES = Path(__file__).resolve().parent / "app" / "templates"

# A Jinja expression inside a URL is some id we can't know here — treat any
# {{ ... }} and any {param} in a route as the same wildcard segment.
JINJA = re.compile(r"\{\{.*?\}\}")
PARAM = re.compile(r"\{[^}/]+\}")

ATTR = re.compile(r"""(?:href|action)\s*=\s*"([^"]*)\"""")

# Links that deliberately leave the app or don't hit a route.
SKIP_PREFIXES = ("http://", "https://", "mailto:", "tel:", "#", "javascript:")


def normalise(url: str) -> str | None:
    """A comparable shape: query and fragment dropped, ids blanked out."""
    url = url.strip()
    if not url or url.startswith(SKIP_PREFIXES):
        return None
    url = url.split("?")[0].split("#")[0]
    if not url.startswith("/"):
        return None                      # relative link, nothing to check
    if url.startswith("/static/"):
        return None
    url = JINJA.sub("*", url)
    return url.rstrip("/") or "/"


def _walk(routes, prefix: str = ""):
    """Yield every path, following included routers.

    This FastAPI version keeps an included router as a single wrapper object
    rather than flattening its routes into app.routes, so a naive pass over
    app.routes sees only a handful of paths and would call every real link
    broken. Follow the wrapper to the router it holds.
    """
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            ctx = getattr(r, "include_context", None)
            yield from _walk(inner.routes, prefix + getattr(ctx, "prefix", ""))
            continue
        nested = getattr(r, "routes", None)
        path = getattr(r, "path", None)
        if nested and path:
            yield prefix + path
            continue
        if path:
            yield prefix + path


def route_shapes() -> set[str]:
    return {PARAM.sub("*", p).rstrip("/") or "/" for p in _walk(app.routes)}


def scan() -> list[tuple[str, str, int]]:
    """Returns (template, url, line) for every URL that has no route."""
    known = route_shapes()
    bad = []
    for tpl in sorted(TEMPLATES.rglob("*.html")):
        for lineno, line in enumerate(tpl.read_text(encoding="utf-8").splitlines(), 1):
            for raw in ATTR.findall(line):
                url = normalise(raw)
                if url is None:
                    continue
                if url in known:
                    continue
                # a wildcard in the template may line up with a literal route
                # segment (e.g. /tasks/* against /tasks/new) — accept that too
                pattern = "^" + re.escape(url).replace(r"\*", "[^/]+") + "$"
                if any(re.match(pattern, k) for k in known):
                    continue
                bad.append((tpl.name, raw, lineno))
    return bad


def main() -> int:
    bad = scan()
    if not bad:
        n = len(route_shapes())
        print(f"  PASS  every template link and button matches one of "
              f"{n} registered routes")
        return 0
    for tpl, url, line in bad:
        print(f"  FAIL  {tpl}:{line} points at {url} — no such route")
    return 1


if __name__ == "__main__":
    sys.exit(main())
