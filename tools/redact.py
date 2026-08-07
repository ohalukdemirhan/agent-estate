"""Anonymising projection and structural leak guard.

Two jobs, deliberately kept in one file so they cannot drift apart:

  1. `project_*`  — turn registry rows into publication-safe rows (allowlisted
     fields, stable pseudonyms, shape-only free text).
  2. `scan`       — refuse to let anything through that *looks* like an
     identifier, whatever the projection did.

The guard is structural on purpose. A denylist of real names, hosts and
project identifiers would itself be the disclosure the moment it were
committed, so estate-specific strings live outside the repository and are
passed in with --denylist. See ANONYMITY.md.
"""

from __future__ import annotations

import hashlib
import re
import string
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Structural leak patterns
# --------------------------------------------------------------------------

# Domains that may legitimately appear in published output: documentation
# links and RFC-reserved placeholders. Everything else that looks like a
# hostname is treated as an estate identifier.
ALLOWED_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "github.com",
    "opensource.org",
    "modelcontextprotocol.io",
    "claude.ai",
}

# Literal strings that match a pattern but are documentation placeholders,
# reserved ranges, or the project's own anonymous commit identity.
ALLOWED_LITERALS = {
    "you@example.com",
    "local@domain.tld",
    "agent-estate@users.noreply.github.com",
    "0.0.0.0",
    "127.0.0.1",
    "127.0.0.11",
}

# RFC 5737 documentation ranges — these exist precisely so examples do not
# have to name a real host.
_DOC_IP_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")

_TLD = r"(?:com|net|org|io|ai|app|dev|co|uk|ltd|zone|me|sh|xyz|cloud|tech|de|fr|tr|nl|es|it)"

LEAK_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("ipv4", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
                        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("hex-secret", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("bearer-token", re.compile(r"\b(?:sk|pk|ghp|gho|xox[baprs])[-_][A-Za-z0-9_-]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("home-path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")),
    ("domain", re.compile(rf"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+{_TLD}\b", re.I)),
    ("store-id", re.compile(r"(?<![\d.])\d{9,12}(?![\d.])")),
]


@dataclass(frozen=True)
class Leak:
    pattern: str
    line: int
    excerpt: str

    def __str__(self) -> str:  # pragma: no cover - human output only
        return f"line {self.line}: [{self.pattern}] {self.excerpt}"


def _allowed(pattern: str, match: str) -> bool:
    if match in ALLOWED_LITERALS:
        return True
    if pattern == "ipv4":
        return match.startswith(_DOC_IP_PREFIXES)
    if pattern == "domain":
        host = match.lower().rstrip(".")
        # A subdomain of an allowed domain is allowed; a lookalike is not.
        return any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS)
    return False


def scan(text: str, extra_terms: list[str] | None = None) -> list[Leak]:
    """Return every structural identifier found in `text`.

    `extra_terms` holds estate-specific strings (real project names, hosts)
    read from a denylist file kept outside the repository. Matching is
    case-insensitive and substring-based, because a real name shows up in
    slugs and hyphenations as often as in prose.
    """
    leaks: list[Leak] = []
    lines = text.splitlines()

    for number, line in enumerate(lines, start=1):
        for name, pattern in LEAK_PATTERNS:
            for match in pattern.findall(line):
                value = match if isinstance(match, str) else match[0]
                if _allowed(name, value):
                    continue
                leaks.append(Leak(name, number, value[:80]))

    for term in extra_terms or []:
        term = term.strip()
        if not term or term.startswith("#"):
            continue
        needle = term.lower()
        for number, line in enumerate(lines, start=1):
            if needle in line.lower():
                leaks.append(Leak("denylist", number, term[:80]))

    return leaks


# --------------------------------------------------------------------------
# Redaction of free text
# --------------------------------------------------------------------------

BLOCK = "█"


def redact_text(text: str, extra_terms: list[str] | None = None) -> str:
    """Replace every structural identifier with a redaction block.

    Used on free-text fields that are published in shape only. The output is
    still passed through `scan` before anything is written — redaction is a
    convenience, the guard is the guarantee.
    """
    def _sub(match: re.Match[str]) -> str:
        value = match.group(0)
        return value if value in ALLOWED_LITERALS else BLOCK * min(len(value), 8)

    out = text
    for name, pattern in LEAK_PATTERNS:
        if name == "domain":
            out = pattern.sub(
                lambda m: m.group(0) if _allowed("domain", m.group(0))
                else BLOCK * 6,
                out,
            )
        else:
            out = pattern.sub(_sub, out)

    for term in extra_terms or []:
        term = term.strip()
        if term and not term.startswith("#"):
            out = re.sub(re.escape(term), BLOCK * 6, out, flags=re.I)

    return out


# --------------------------------------------------------------------------
# Stable pseudonyms
# --------------------------------------------------------------------------

def pseudonym(kind: str, name: str, salt: str) -> str:
    """Deterministic, non-reversible label for a real name.

    Same estate and salt produce the same label across publishes, so the
    page stays readable over time; a different estate produces different
    labels. Nothing in the published output reverses to a name — the salt
    lives outside the repository.
    """
    digest = hashlib.sha256(f"{salt}:{kind}:{name}".encode()).digest()
    if kind == "project":
        return "holding-" + string.ascii_uppercase[digest[0] % 26]
    return f"{kind}-{digest[0] % 90 + 10:02d}"


# Model names identify a vendor and a price tier, neither of which is an
# estate identifier — but publishing the exact string dates the record and
# invites vendor-shaped inference. Generalise to a class.
_MODEL_CLASSES = (
    (("opus", "gpt-5", "fable", "mythos"), "frontier, reasoning"),
    (("sonnet", "gpt-4", "gemini-pro"), "mid-tier, fast"),
    (("haiku", "mini", "flash", "nano"), "small, cheap"),
    (("image", "dall", "vision", "tts", "whisper"), "modality-specific"),
)


def model_class(model: str | None) -> str:
    if not model:
        return "unstated"
    lowered = model.lower()
    for needles, label in _MODEL_CLASSES:
        if any(needle in lowered for needle in needles):
            return label
    return "unclassified"


# --------------------------------------------------------------------------
# Row projections — allowlists, not blocklists
# --------------------------------------------------------------------------

def project_agent(row: dict, index: int) -> dict:
    return {
        "name": f"agent-{index:02d}",
        "role": row.get("role") or "unstated",
        "model": model_class(row.get("model")),
        "capabilities": row.get("capabilities") or [],
        "status": row.get("status") or "unknown",
    }


def project_finding(row: dict, salt: str, denylist: list[str]) -> dict:
    return {
        "severity": int(row.get("severity") or 0),
        "confidence": int(row.get("confidence") or 0),
        "kind": row.get("kind") or "unstated",
        "title": redact_text(row.get("title") or "", denylist),
        "status": row.get("status") or "open",
        "evidence": len(row.get("evidence_signal_ids") or []),
    }


def project_signal(row: dict, salt: str, denylist: list[str]) -> dict:
    return {
        "kind": row.get("kind") or "unstated",
        "metric": row.get("metric") or "—",
        "value": redact_text(str(row.get("value") or "—"), denylist),
        # The payload is never published: it is the field most likely to
        # carry a bundle id, a reviewer name or a hostname.
        "note": "payload withheld",
    }
