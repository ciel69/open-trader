"""Приватность и очистка текстов памяти (08 п. 1.1, 1.3, 6.2, 6.5).

В случаях и уроках нет ключей, адресов, путей, абсолютных сумм в USDT (equity, pnl, margin): только R, проценты, доли.
clean_own: NFKC, управляющие и невидимые символы, URL, длинные base64/hex удаляются, суммы маскируются, пробелы схлопываются, обрезка.
inj_suspect: эвристика инъекции в тексте; подозрительная строка заменяется на [redacted] (safe_text).
privacy_scan: рекурсивная проверка любого объекта (запрещённые ключи, паттерны ключей, пути, URL, суммы).
"""
import re
import unicodedata

REDACTED = "[redacted]"
AMT = "[amt]"
FORBIDDEN_KEYS = frozenset((
    "pnl_usdt", "pnl", "equity", "margin", "notional", "balance", "risk_usdt", "fees_usdt", "funding_usdt", "fee_open", "qty", "capital",
    "wallet", "address", "api_key", "apikey", "api_secret", "secret", "token", "password", "passwd", "private_key", "seed", "mnemonic",
))
RE_URL = re.compile(r"(?i)\b(?:https?|ftp|wss?)://\S+|\bwww\.\S+")
RE_LONG = re.compile(r"(?<![A-Za-z0-9+/_=-])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9+/_=-])")
RE_KEYLIKE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bAKIA[0-9A-Z]{12,}|\b0x[0-9a-fA-F]{40}\b|BYBIT_API|API_SECRET|API_KEY|-----BEGIN")
RE_PATH = re.compile(r"(?:^|[\s\"'(])(?:/Users/|/home/|/etc/|/var/|~/|[A-Za-z]:\\|\.config/)")
RE_AMOUNT = re.compile(r"(?i)(?<![\w.])\d[\d\s.,]*\s*(?:usdt|usdc|usd|\$|долл\w*)(?![A-Za-z])|\$\s*\d[\d.,]*")
INJ = [re.compile(p, re.I) for p in (
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)", r"disregard\s+(?:all|the|any|previous)", r"system\s+prompt",
    r"(?:^|\W)(?:system|assistant|developer|user)\s*:", r"you\s+are\s+now", r"new\s+instructions?", r"</?\s*(?:system|instructions?|tool|assistant)\b",
    r"игнорируй", r"забудь\s+(?:все|всё|предыдущ)", r"системн\w+\s+(?:промпт|инструкц)", r"нов\w+\s+инструкц", r"отмени\s+правил", r"```",
    r"tool_?call|function_?call", r"\b(?:execute|run)\s+(?:the\s+)?(?:command|code|script)", r"выполни\s+(?:команду|код)",
    r"\b(?:open|close)_?position\b.*\bnow\b",
)]


def clean_own(s, maxlen=200):
    """Очищает собственный текст системы (см. модуль); None -> ''."""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else ("" if unicodedata.category(c) in ("Cf", "Cs", "Co", "Cn") else c) for c in s)
    s = RE_URL.sub(" ", s)
    s = RE_LONG.sub(" ", s)
    s = RE_KEYLIKE.sub(" ", s)
    s = RE_AMOUNT.sub(AMT, s)
    s = " ".join(s.split())
    return s[:maxlen].rstrip() if maxlen else s


def inj_suspect(s):
    t = unicodedata.normalize("NFKC", str(s or ""))
    return any(p.search(t) for p in INJ)


def safe_text(s, maxlen=200):
    """-> (текст, подозрение). Подозрительная строка заменяется на [redacted] (случай получает флаг inj_suspect)."""
    t = clean_own(s, maxlen)
    if inj_suspect(s) or inj_suspect(t):
        return REDACTED, True
    return t, False


def privacy_scan(obj, path="$"):
    """-> список нарушений (пусто = чисто): 'путь: причина'. Работает с любым JSON-подобным объектом."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k).lower()
            if ks in FORBIDDEN_KEYS:
                out.append("%s.%s: запрещённый ключ" % (path, k))
            out.extend(privacy_scan(v, "%s.%s" % (path, k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(privacy_scan(v, "%s[%d]" % (path, i)))
    elif isinstance(obj, str):
        if RE_KEYLIKE.search(obj):
            out.append("%s: похоже на ключ" % path)
        if RE_LONG.search(obj):
            out.append("%s: длинная строка base64/hex" % path)
        if RE_URL.search(obj):
            out.append("%s: URL" % path)
        if RE_PATH.search(obj):
            out.append("%s: путь к файлу" % path)
        if RE_AMOUNT.search(obj):
            out.append("%s: абсолютная сумма" % path)
    return out
