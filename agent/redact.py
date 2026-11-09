"""Redaction of secrets and PII. Everything the diagnosis agent sees passes through `redact` first.

Pure functions, no I/O. This is a best-effort filter, not a guarantee: it exists so a secret that leaks into a log
line or a config value does not leave the cluster boundary for the LLM provider. The diagnoser identity cannot read
Secrets at all; this covers the cases where a credential is printed or stored somewhere readable anyway.
"""
import math
import re

MARK = "[REDACTED:{}]"

# (kind, pattern, group to replace or 0 for the whole match). Order matters: specific before generic.
_RULES = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S), 0),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), 0),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), 0),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), 0),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), 0),
    ("bearer", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{12,})"), 1),
    ("url-credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:([^/\s@]+)@"), 1),
    # key=value or key: value where the key names a secret
    ("secret-value", re.compile(
        r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credential)[A-Za-z0-9_.-]*)"
        r"""(["']?\s*[:=]\s*["']?)([^\s"',;}\]]{3,})"""), 3),
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), 0),
    ("ipv4", re.compile(r"\b(?!10\.96\.)(?:\d{1,3}\.){3}\d{1,3}\b"), 0),
    ("card", re.compile(r"\b(?:\d[ -]?){13,16}\b"), 0),
]
_TOKENISH = re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b")


def _entropy(s):
    if not s:
        return 0.0
    counts = {c: s.count(c) for c in set(s)}
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def _is_ip(text):
    return all(0 <= int(p) <= 255 for p in text.split("."))


def _luhn(digits):
    total, flip = 0, False
    for d in reversed(digits):
        n = int(d)
        if flip:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n; flip = not flip
    return total % 10 == 0


def redact(text):
    """Returns (clean_text, counts) where counts maps kind -> number of replacements."""
    if not isinstance(text, str):
        text = str(text)
    counts = {}

    def bump(kind):
        counts[kind] = counts.get(kind, 0) + 1

    for kind, rx, grp in _RULES:
        def sub(m, kind=kind, grp=grp):
            if kind == "ipv4" and not _is_ip(m.group(0)):
                return m.group(0)
            if kind == "card" and not _luhn(re.sub(r"\D", "", m.group(0))):
                return m.group(0)
            bump(kind)
            if grp == 0:
                return MARK.format(kind)
            s, e = m.span(grp)
            return m.group(0)[:s - m.start()] + MARK.format(kind) + m.group(0)[e - m.start():]
        text = rx.sub(sub, text)

    def tok(m):
        t = m.group(0)
        if ("REDACTED" in t or t.count("-") >= 2 or _entropy(t) < 3.5      # 2+ hyphens: a Kubernetes object name, not a key
                or not re.search(r"\d", t) or not re.search(r"[A-Za-z]", t)):
            return t
        bump("high-entropy")
        return MARK.format("high-entropy")
    text = _TOKENISH.sub(tok, text)
    return text, counts


def redact_obj(obj):
    """Redacts every string (and dict key left alone) in a JSON-like structure. Returns (clean_obj, counts)."""
    counts = {}

    def walk(o):
        if isinstance(o, str):
            clean, c = redact(o)
            for k, v in c.items():
                counts[k] = counts.get(k, 0) + v
            return clean
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [walk(v) for v in o]
        return o
    return walk(obj), counts
