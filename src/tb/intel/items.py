"""Заголовки новостей: очистка, дедупликация, хранилище `runtime/intel/items.jsonl` (07 п. 3.3-3.4, уровень 1). Владелец: WP13.

ВАЖНО: текст третьих лиц живёт ТОЛЬКО здесь и в файле items.jsonl; торговый мозг, шлюз, mcp, память и шина его не видят (тест слоёв).
Исходные (не очищенные) тексты на диск не пишутся. Читают файл: дашборд владельца и будущий карантинный читатель WP13b.
Строка: {"ref":"N-3fa9c2d1","src":"coindesk","ts":..,"title":"<=160","snip":"<=280","url":"<=300","flags":["inj_suspect"],"seen":["domain"],"reader":{...}?}
"""
import hashlib
import html
import json
import os
import re
import time
import unicodedata

from tb import paths
from tb.core import filelock

TITLE_LEN = 160
SNIP_LEN = 280
URL_LEN = 300
RAW_CAP = 4000                 # входной текст режется до очистки (защита от гигантских полей)
RETENTION_DAYS = 14
DEDUP_WINDOW_S = 86400.0
JACCARD_MIN = 0.8
MAX_SEEN = 5

_BIDI = set(chr(c) for c in list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)) + [0x200E, 0x200F, 0x061C])
_INVIS = set(chr(c) for c in list(range(0x200B, 0x2010)) + [0x2060, 0xFEFF, 0x00AD, 0x180E])
_RE_TAG = re.compile(r"<[^>]{0,500}>")
_RE_TAGOPEN = re.compile(r"<[a-zA-Z/!?][^>]*$")                      # незакрытый тег в конце строки
_RE_URL = re.compile(r"(?i)\b(?:https?|ftp|file|data|javascript)://\S+|\bwww\.\S+")
_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_RE_LONGTOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")                 # base64/hex/ключи без пробелов
_RE_SPACE = re.compile(r"\s+")
_RE_FENCE = re.compile(r"`{3,}|~{3,}")
INJ_PATTERNS = (
    r"ignore\b.{0,40}\b(instruction|rule|prompt|above|previous)", r"disregard\b.{0,40}\b(instruction|rule|prompt|above|previous)",
    r"forget\b.{0,40}\b(instruction|rule|prompt|above|previous)", r"system\s*prompt", r"you\s+are\s+now", r"\bassistant\s*:", r"\bsystem\s*:",
    r"tool[_ ]?call", r"<\|", r"\|>", r"```", r"set\s+(the\s+)?(stop|sl|tp)\b", r"close\s+all", r"new\s+instructions?", r"override\b.{0,30}\b(rule|limit|risk)",
    r"jailbreak", r"function[_ ]?call", r"api[_ ]?key",
    r"игнорируй", r"игнорировать\s+(все\s+)?(предыдущ|инструкц)", r"забудь\s+(все\s+)?(предыдущ|инструкц|правил)", r"системн\w+\s+промпт", r"ты\s+теперь",
    r"закрой\s+все", r"закрыть\s+все\s+позиц", r"установи\s+стоп", r"поставь\s+стоп", r"отключи\s+(стоп|лимит|риск)",
)
_RE_INJ = re.compile("|".join("(?:%s)" % p for p in INJ_PATTERNS), re.I | re.S)


def _strip_chars(s):
    out = []
    for ch in s:
        if ch in "\r\n\t\v\f":
            out.append(" ")
            continue
        o = ord(ch)
        if ch in _BIDI or ch in _INVIS or 0xE0000 <= o <= 0xE007F:
            continue
        if unicodedata.category(ch)[0] == "C":
            continue
        out.append(ch)
    return "".join(out)


def _cut(s, n):
    """Обрезка по границе слова до n символов (с многоточием)."""
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    sp = cut.rfind(" ")
    if sp >= n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,;:.-") + "…"


def inj_suspect(text):
    """Эвристика подозрения на инъекцию в ИСХОДНОМ тексте (проверяется до очистки и по очищенному: флаг не удаляет элемент)."""
    if not isinstance(text, str):
        return False
    probe = _RE_SPACE.sub(" ", html.unescape(unicodedata.normalize("NFKC", _strip_chars(text[:RAW_CAP]))))
    joined = _RE_SPACE.sub(" ", _RE_TAG.sub("", probe))          # теги, вставленные внутрь слов (ign<b>ore</b>), склеиваются
    return bool(_RE_INJ.search(probe) or _RE_INJ.search(joined))


def clean_text(text, max_len):
    """Уровень 1: NFKC; управляющие, bidi, невидимые, теги U+E0000; HTML-теги и сущности -> текст; URL, e-mail, длинные base64/hex удалены;
    пробелы схлопнуты; длина до max_len по границе слова."""
    if not isinstance(text, str):
        return ""
    s = text[:RAW_CAP]
    s = unicodedata.normalize("NFKC", s)
    s = _strip_chars(s)
    for _ in range(2):                                   # двойное кодирование сущностей (&amp;lt;)
        s = html.unescape(s)
        s = _RE_TAG.sub(" ", s)
    s = _RE_TAGOPEN.sub(" ", s)
    s = _strip_chars(s)                                  # сущности могли выдать управляющие символы
    s = s.replace("<", " ").replace(">", " ")
    s = _RE_URL.sub(" ", s)
    s = _RE_EMAIL.sub(" ", s)
    s = _RE_FENCE.sub(" ", s)
    s = _RE_LONGTOKEN.sub(" ", s)
    s = _RE_SPACE.sub(" ", s).strip()
    return _cut(s, max_len)


def clean_url(url):
    """Только http(s), без управляющих и пробелов, до URL_LEN; иначе ''. Дашборд всё равно показывает только домен."""
    if not isinstance(url, str):
        return ""
    u = _strip_chars(url.strip())[:URL_LEN + 1]
    if not re.match(r"^https?://[A-Za-z0-9.-]+(?::\d+)?(?:[/?#][^\s<>\"']*)?$", u) or len(u) > URL_LEN:
        return ""
    return u


def domain_of(url):
    m = re.match(r"^https?://([A-Za-z0-9.-]+)", url or "")
    if not m:
        return ""
    h = m.group(1).lower()
    return h[4:] if h.startswith("www.") else h


def norm_title(title):
    """Нормализация для ref: нижний регистр, без цифр и пунктуации, пробелы схлопнуты."""
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"[^\w\s]|[\d_]", " ", t, flags=re.U)
    return _RE_SPACE.sub(" ", t).strip()


def make_ref(title, group):
    h = hashlib.sha256((norm_title(title) + "|" + (group or "")).encode("utf-8")).hexdigest()
    return "N-" + h[:8]


def trigrams(title):
    t = " " + norm_title(title) + " "
    return set(t[i:i + 3] for i in range(max(0, len(t) - 2)))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def _prefix(tg):
    """Префикс множества триграмм для префиксного фильтра Жаккара (порядок: лексикографический)."""
    n = len(tg)
    p = n - int(-(-JACCARD_MIN * n // 1)) + 1                 # |x| - ceil(t*|x|) + 1
    return sorted(tg)[:max(1, p)]


def make_item(src, group, title, snip, url, ts, now):
    """Сырые поля источника -> очищенный элемент или None (пустой заголовок после очистки)."""
    raw_flag = inj_suspect(title) or inj_suspect(snip)
    t = clean_text(title, TITLE_LEN)
    if len(t) < 3:
        return None
    s = clean_text(snip, SNIP_LEN)
    flags = []
    if raw_flag or inj_suspect(t) or inj_suspect(s):
        flags.append("inj_suspect")
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = now
    if not (ts == ts) or ts > now + 3600 or ts < now - 90 * 86400:
        ts = now
    u = clean_url(url)
    it = {"ref": make_ref(t, group), "src": str(src)[:30], "ts": int(ts), "title": t, "snip": s, "url": u, "flags": flags}
    return it


class ItemStore(object):
    """Файл runtime/intel/items.jsonl (один писатель: сервис). Дедуп: одинаковый ref или Жаккар триграмм >= 0.8 за 24 ч.
    Дубль с ДРУГОГО домена добавляется в seen существующего элемента (подтверждение второго независимого источника)."""

    def __init__(self, root=None, retention_days=RETENTION_DAYS):
        self.root = root
        self.retention_days = retention_days
        self.path = paths.runtime("intel", "items.jsonl", root=root)
        self._recent = []                      # [(ts, ref, trigrams, domain, index_in_file_cache)]
        self._loaded = False

    def _read_all(self):
        out = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(r, dict) and isinstance(r.get("ref"), str) and isinstance(r.get("title"), str):
                        out.append(r)
        except OSError:
            pass
        return out

    def load(self, limit=None, since=None):
        rows = self._read_all()
        if since is not None:
            rows = [r for r in rows if r.get("ts", 0) >= since]
        rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return rows[:limit] if limit else rows

    def _entry(self, r_ts, ref, title, dom):
        tg = trigrams(title)
        return (r_ts, ref, tg, dom, _prefix(tg))

    def _index(self, now):
        if not self._loaded:
            self._recent = [self._entry(r.get("ts", 0), r["ref"], r["title"], domain_of(r.get("url", "")) or r.get("src", ""))
                            for r in self._read_all() if r.get("ts", 0) >= now - DEDUP_WINDOW_S]
            self._loaded = True
            self._reindex()
        keep = [x for x in self._recent if x[0] >= now - DEDUP_WINDOW_S]
        if len(keep) != len(self._recent):
            self._recent = keep
            self._reindex()

    def _reindex(self):
        self._by_ref, self._idx = {}, {}
        for e in self._recent:
            self._put(e)

    def _put(self, e):
        self._by_ref.setdefault(e[1], e)
        for t in e[4]:
            self._idx.setdefault(t, []).append(e)

    def _find(self, it, tg):
        e = self._by_ref.get(it["ref"])
        if e is not None:
            return e
        seen = set()
        for t in _prefix(tg):                              # префиксный фильтр: порог Жаккара 0.8 требует общего триграмма в префиксах
            for e in self._idx.get(t, ()):
                if id(e) not in seen:
                    seen.add(id(e))
                    if jaccard(tg, e[2]) >= JACCARD_MIN:
                        return e
        return None

    def add_many(self, items, now=None):
        """-> {"new": n, "dup": n, "confirmed": n}. confirmed = дубль с другого домена (записывается в seen)."""
        now = time.time() if now is None else now
        self._index(now)
        res = {"new": 0, "dup": 0, "confirmed": 0}
        fresh, confirm = [], {}
        self.last_fresh = fresh                              # новые элементы последнего вызова (для быстрого канала news_flash)
        for it in items:
            dom = domain_of(it.get("url", "")) or it.get("src", "")
            e = self._find(it, trigrams(it["title"]))
            if e is not None:
                res["dup"] += 1
                if e[3] != dom:
                    res["confirmed"] += 1
                    confirm.setdefault(e[1], set()).add(dom)
                continue
            fresh.append(it)
            ne = self._entry(it["ts"], it["ref"], it["title"], dom)
            self._recent.append(ne)
            self._put(ne)
        if fresh:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                for it in fresh:
                    f.write(json.dumps(it, ensure_ascii=False, separators=(",", ":")) + "\n")
            res["new"] = len(fresh)
        if confirm:
            self._merge_seen(confirm)
        return res

    def _merge_seen(self, confirm):
        with filelock.FileLock(self.path + ".lock", wait=5.0):
            rows = self._read_all()
            for r in rows:
                add = confirm.get(r["ref"])
                if add:
                    seen = list(r.get("seen") or [])
                    for d in sorted(add):
                        if d not in seen and len(seen) < MAX_SEEN:
                            seen.append(d)
                    r["seen"] = seen
            self._write(rows)

    def _write(self, rows):
        text = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
        filelock.atomic_write(self.path, text)

    def prune(self, now=None):
        """Ротация: убирает элементы старше retention_days. -> сколько удалено."""
        now = time.time() if now is None else now
        with filelock.FileLock(self.path + ".lock", wait=5.0):
            rows = self._read_all()
            keep = [r for r in rows if r.get("ts", 0) >= now - self.retention_days * 86400]
            if len(keep) != len(rows):
                self._write(keep)
        return len(rows) - len(keep)

    def stats(self, now=None):
        now = time.time() if now is None else now
        rows = self._read_all()
        return {"n": len(rows), "last_24h": sum(1 for r in rows if r.get("ts", 0) >= now - 86400),
                "inj_suspect": sum(1 for r in rows if "inj_suspect" in (r.get("flags") or []))}
