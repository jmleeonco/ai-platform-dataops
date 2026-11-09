"""Where this app is mounted for the current request (CONTRACT §79.2 d-a): `/` on the direct site (:8096), or the
path prefix a reverse proxy announces in `X-Forwarded-Prefix` (the hub serves the app under `/dataops-ui`).
`DATAOPS_ROOT_PATH` sets a fixed prefix when a proxy cannot send the header.

Every URL the app writes — template links, form actions, static assets, redirects, the CSRF cookie path, the
script fetches (`data-root` on <body>) — starts with `root` / `url()`, so the same pages work through both doors.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

PREFIX_RE = re.compile(r"(/[A-Za-z0-9._~-]+)+")
_ROOT: ContextVar[str] = ContextVar("dataops_root", default="")


def resolve(header: str | None, fallback: str = "") -> str:
    """The prefix to use: a well-formed X-Forwarded-Prefix, else the configured one, else '' (served at /)."""
    for value in (header, fallback):
        v = (value or "").strip().rstrip("/")
        if v and PREFIX_RE.fullmatch(v):
            return v
    return ""


def set_root(value: str):
    return _ROOT.set(value)


def reset(token) -> None:
    _ROOT.reset(token)


def root() -> str:
    return _ROOT.get()


def url(path: str) -> str:
    """`/runs/x` → `<prefix>/runs/x` for the current request."""
    return _ROOT.get() + path


class Root:
    """The Jinja global `root`: renders as the current prefix (`href="{{ root }}/runs/…"`)."""

    def __str__(self) -> str:
        return _ROOT.get()

    def __html__(self) -> str:             # validated by PREFIX_RE: nothing to escape
        return _ROOT.get()
