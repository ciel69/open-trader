"""Быстрые новости (news_flash): детерминированный разбор заголовков БЕЗ LLM и реестр исходов. Владелец: платформа intel.

Заголовок нового элемента (после очистки и дедупликации ItemStore) проходит правила: класс (листинг, делистинг, взлом, ETF, регуляция, макро, ...), подсказка направления,
важность 1..5 и монеты (тикер пары XXXUSDT, cashtag $XXX, скобки (XXX), псевдонимы config/intel.json: flash.aliases), только из вселенной скана (runtime/prices.json и др.).
Нашлись класс и монета, новость свежая (не старше max_age_s), важность не ниже min_imp -> событие шины `news_flash` (wake) с числами и enum, БЕЗ текста заголовка.
Подсказка направления это эвристика по словам, не вывод: агент обязан сверить её с реакцией цены (brief.flash.move_pct) и со структурой.

Реестр state/news_flash.jsonl (только дописывание): {"ev":"flash","id","ts","cls","dir","imp","sym","src","px0"} и исходы {"ev":"outcome","id","sym","h","ret_pct","dir_pct"}
через horizons_min (по умолчанию 1, 5, 15 мин) по ценам из локальных файлов (core.ideas.price_book). По нему копится статистика «какие классы новостей имеют преимущество».
Сети нет, ничего не торгует.
"""
import hashlib
import json
import re
import time
from collections import deque

from tb import paths
from tb.core import ideas, newsflash

DEFAULTS = {"enabled": True, "min_imp": 3, "max_age_s": 900.0, "dedup_s": 1800.0, "horizons_min": [1, 5, 15], "max_syms": 2, "aliases": {},
            "macro_move_pct": 0.25, "macro_min_impact": 2, "macro_delay_s": 60.0, "macro_window_s": 900.0}
CLASSES = ("hack", "delisting", "listing", "etf", "regulation", "unlock", "maintenance", "macro")
DIRS = ("up", "down", "none")
MARKET_WIDE = ("macro", "regulation", "etf")          # без монеты в заголовке: событие по BTC как барометру рынка
BELLWETHER = "BTC"

# (класс, важность, подсказка направления, регулярное выражение по нижнему регистру); порядок важен: первое совпадение, делистинг раньше листинга
RULES = (
    ("hack", 5, "down", re.compile(r"\b(hack(ed|s)?|exploit(ed|s)?|drain(ed|s)?|stolen|breach(ed)?|rug ?pull|compromised)\b")),
    ("delisting", 4, "down", re.compile(r"\bdelist(s|ed|ing)?\b|monitoring tag|will (remove|suspend) .{0,40}(trading|pair)")),
    ("listing", 4, "up", re.compile(r"\b(new listing|will list|to list|lists|listing of|will launch .{0,40}(perpetual|futures|contract)|perpetual contract)\b")),
    ("etf", 4, "up", re.compile(r"\betf\b.{0,60}\b(approv\w*|greenlight\w*|launch\w*)\b|\b(approv\w*|greenlight\w*)\b.{0,60}\betf\b")),
    ("regulation", 4, "down", re.compile(r"\b(sec|cftc|doj)\b.{0,50}\b(sue[sd]?|lawsuit|charg\w*|fine[sd]?|ban(s|ned)?)\b|\bban(s|ned)?\b.{0,30}\bcrypto\w*")),
    ("unlock", 2, "down", re.compile(r"\b(token )?unlock(s|ed|ing)?\b")),
    ("maintenance", 2, "none", re.compile(r"\bmaintenance\b|\bsuspen(d|ds|ded|sion)\b|\boutage\b")),
    ("macro", 3, "none", re.compile(r"\b(cpi|nfp|fomc|powell|rate (cut|hike)|inflation)\b")),
)
RE_PAIR = re.compile(r"\b([A-Z0-9]{2,15})USDT\b")
RE_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
RE_PAREN = re.compile(r"\(([A-Z][A-Z0-9]{1,9})\)")
RE_BARE = re.compile(r"\b([A-Z]{3,10})\b")           # голый тикер заглавными: только из вселенной скана и не из стоп-списка слов
STOP_WORDS = frozenset(("USD", "USDT", "USDC", "SEC", "ETF", "ETFS", "CPI", "NFP", "FOMC", "FED", "CEO", "DOJ", "CFTC", "NYSE", "THE", "AND", "FOR", "NEW", "ALL", "OTC", "IPO",
                        "GDP", "PCE", "PPI", "FBI", "IRS", "EU", "UK", "US", "UAE", "AI", "NFT", "DAO", "DEX", "CEX", "KYC", "AML", "API", "ATH", "FUD", "TVL", "DEFI", "NOW"))
RE_SYM = re.compile(r"^[A-Z0-9]{2,20}$")


def classify(title, snip=""):
    """-> (класс, важность, подсказка направления) или None. Только заголовок и начало текста; сначала более «тяжёлые» классы."""
    low = ("%s %s" % (title or "", snip or "")).lower()
    for cls, imp, d, rx in RULES:
        if rx.search(low):
            return cls, imp, d
    return None


def extract_syms(title, snip, universe, aliases=None, limit=2):
    """Монеты из заголовка (порядок появления), только из вселенной скана; псевдонимы config: {'solana': 'SOL'}."""
    text = "%s %s" % (title or "", snip or "")
    found = []

    def add(s):
        s = (s or "").upper()
        if RE_SYM.match(s) and s in universe and s not in found:
            found.append(s)
    for m in RE_PAIR.finditer(text):
        add(m.group(1))
    for m in RE_CASHTAG.finditer(text):
        add(m.group(1))
    for m in RE_PAREN.finditer(text):
        add(m.group(1))
    for m in RE_BARE.finditer(title or ""):                  # только заголовок: в начале текста слишком много шума
        if m.group(1) not in STOP_WORDS:
            add(m.group(1))
    low = text.lower()
    for name, sym in (aliases or {}).items():
        if isinstance(name, str) and isinstance(sym, str) and re.search(r"\b%s\b" % re.escape(name.lower()), low):
            add(sym)
    return found[:limit]


def flash_id(ref, sym):
    return "f" + hashlib.sha1(("%s|%s" % (ref, sym)).encode("utf-8")).hexdigest()[:12]


ledger_path = newsflash.ledger_path
read_rows = newsflash.read_rows
recent_for = newsflash.recent_for
stats = newsflash.stats


class Flash(object):
    def __init__(self, root, cfg=None, clock=time.time, emit=None, log=None):
        self.root = root
        c = dict(DEFAULTS)
        c.update((cfg or {}).get("flash") or {})
        self.cfg = c
        self.clock = clock
        self.emit = emit
        self.log = log or (lambda m: None)
        self.pending = []                # [{id,sym,ts,px0,dir,hs:[ещё не снятые горизонты]}]
        self._last = {}                  # (cls, sym) -> ts последнего события (дедуп)
        self._btc = deque(maxlen=1800)   # (t, цена BTC) до макро-релиза: цена перед выходом цифры
        self._macro_done = set()         # id макро-событий, по которым реакция уже записана (переживает перезапуск через реестр)
        self._load(clock())

    def _load(self, now):
        """Восстановление после перезапуска: недавние flash и снятые исходы за последние max(horizons) минут и dedup_s."""
        hs = [int(h) for h in self.cfg["horizons_min"]]
        done = {}
        flashes = []
        for r in read_rows(self.root):
            if r.get("ev") == "flash" and r.get("src") == "calendar":
                self._macro_done.add(r.get("id"))
            if r.get("ev") == "flash" and now - float(r.get("ts") or 0) <= max(self.cfg["dedup_s"], max(hs) * 60 + 60):
                flashes.append(r)
            elif r.get("ev") == "outcome":
                done.setdefault(r.get("id"), set()).add(r.get("h"))
        for r in flashes:
            self._last[(r.get("cls"), r.get("sym"))] = max(self._last.get((r.get("cls"), r.get("sym")), 0.0), float(r.get("ts") or 0))
            left = [h for h in hs if h not in done.get(r.get("id"), set())]
            if left and r.get("px0"):
                self.pending.append({"id": r["id"], "sym": r["sym"], "ts": float(r["ts"]), "px0": float(r["px0"]), "dir": r.get("dir"), "hs": left})

    def universe(self, now):
        return set(ideas.price_book(self.root, now).keys())

    def process(self, items, now=None):
        """Новые элементы ItemStore -> список записанных flash (и события шины). Ошибка эмиссии не роняет сервис."""
        now = self.clock() if now is None else now
        out = []
        if not self.cfg.get("enabled", True) or not items:
            return out
        uni = self.universe(now)
        book = ideas.price_book(self.root, now)
        for it in items:
            if "inj_suspect" in (it.get("flags") or []):
                continue                                       # подозрение на инъекцию: в быстрый канал не пускаем
            age = now - float(it.get("ts") or now)
            if age > float(self.cfg["max_age_s"]):
                continue
            cl = classify(it.get("title"), it.get("snip"))
            if cl is None or cl[1] < int(self.cfg["min_imp"]):
                continue
            cls, imp, d = cl
            # псевдонимы («bitcoin» -> BTC) только для классов про рынок/регуляцию: «Bitcoin wallet fixes flaw» не взлом BTC
            syms = extract_syms(it.get("title"), it.get("snip"), uni, self.cfg.get("aliases") if cls in MARKET_WIDE else None, int(self.cfg["max_syms"]))
            if not syms and cls in MARKET_WIDE and BELLWETHER in uni:
                syms = [BELLWETHER]
            for sym in syms:
                if now - self._last.get((cls, sym), -1e18) < float(self.cfg["dedup_s"]):
                    continue
                self._last[(cls, sym)] = now
                px = (book.get(sym) or (None,))[0]
                fid = flash_id(it.get("ref"), sym)
                row = {"ev": "flash", "id": fid, "ts": round(now, 1), "cls": cls, "dir": d, "imp": imp, "sym": sym, "src": str(it.get("src") or "")[:30], "px0": px,
                       "age_s": int(age)}
                try:
                    ideas.append(ledger_path(self.root), row)
                except (OSError, TimeoutError) as e:
                    self.log("flash: не записано (%s)" % type(e).__name__)
                    continue
                if px:
                    self.pending.append({"id": fid, "sym": sym, "ts": now, "px0": float(px), "dir": d, "hs": [int(h) for h in self.cfg["horizons_min"]]})
                if self.emit is not None:
                    try:
                        self.emit("news_flash", {"cls": cls, "dir": d, "imp": imp, "src": _ident(row["src"]), "age_s": row["age_s"], "id": fid}, now, sym)
                    except (ValueError, OSError, TimeoutError) as e:
                        self.log("flash: событие не записано (%s)" % type(e).__name__)
                out.append(row)
        return out

    def macro_step(self, events, now=None):
        """Реакция цены BTC на макро-релиз из календаря (CPI, NFP, FOMC, ...). Без консенсус-прогноза «сюрприз» по цифре не посчитать, поэтому мерка реакция цены: цена BTC перед выходом
        (последняя за 2 минуты до ts) против цены через macro_delay_s после. Движение от macro_move_pct -> news_flash (macro, направление по знаку движения, важность 4); любая реакция
        пишется в реестр (emitted False у тихих), исходы через 1/5/15 минут от самого релиза. Каждое событие один раз. -> число записанных реакций."""
        now = self.clock() if now is None else now
        c = self.cfg
        hit = ideas.price_book(self.root, now).get(BELLWETHER)
        if hit and (not self._btc or now - self._btc[-1][0] >= 1.0):
            self._btc.append((now, hit[0]))
        n = 0
        for e in events or ():
            try:
                eid, ts, imp = str(e["id"]), float(e["ts"]), int(e.get("impact") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            rid = "fm" + hashlib.sha1(eid.encode("utf-8")).hexdigest()[:10]
            if rid in self._macro_done or imp < c["macro_min_impact"] or not (ts + c["macro_delay_s"] <= now <= ts + c["macro_window_s"]):
                continue
            pre = next((px for t, px in reversed(self._btc) if ts - 120.0 <= t <= ts), None)
            if pre is None or not hit:
                continue                                         # нет цены до релиза (платформа была выключена): реакцию не считаем
            self._macro_done.add(rid)
            move = 100.0 * (hit[0] / pre - 1.0)
            loud = abs(move) >= c["macro_move_pct"]
            d = "up" if move > 0 else "down" if move < 0 else "none"
            row = {"ev": "flash", "id": rid, "ts": round(ts, 1), "cls": "macro", "dir": d if loud else "none", "imp": 4 if loud else 2, "sym": BELLWETHER, "src": "calendar", "px0": pre,
                   "age_s": int(now - ts), "emitted": bool(loud and self.emit is not None)}
            try:
                ideas.append(ledger_path(self.root), row)
            except (OSError, TimeoutError) as ex:
                self.log("flash: макро-реакция не записана (%s)" % type(ex).__name__)
                continue
            self.pending.append({"id": rid, "sym": BELLWETHER, "ts": ts, "px0": float(pre), "dir": row["dir"], "hs": [int(h) for h in c["horizons_min"]]})
            if row["emitted"]:
                try:
                    self.emit("news_flash", {"cls": "macro", "dir": d, "imp": 4, "src": "calendar", "age_s": row["age_s"], "id": rid}, now, BELLWETHER)
                except (ValueError, OSError, TimeoutError) as ex:
                    self.log("flash: событие не записано (%s)" % type(ex).__name__)
            n += 1
        return n

    def track(self, now=None):
        """Исходы: через h минут после flash берём цену и пишем ret_pct (к px0) и dir_pct (по подсказке направления; none -> без знака). -> сколько записано."""
        now = self.clock() if now is None else now
        n = 0
        keep = []
        book = None
        for p in self.pending:
            left = []
            for h in p["hs"]:
                if now < p["ts"] + h * 60.0:
                    left.append(h)
                    continue
                if book is None:
                    book = ideas.price_book(self.root, now)
                hit = book.get(p["sym"])
                if hit is None or not p["px0"]:
                    continue                                   # нет цены: горизонт пропущен, не копим вечно
                ret = 100.0 * (hit[0] / p["px0"] - 1.0)
                sign = {"up": 1.0, "down": -1.0}.get(p["dir"])
                row = {"ev": "outcome", "id": p["id"], "sym": p["sym"], "h": h, "ret_pct": round(ret, 4), "dir_pct": round(ret * sign, 4) if sign else None}
                try:
                    ideas.append(ledger_path(self.root), row)
                    n += 1
                except (OSError, TimeoutError):
                    left.append(h)
            if left:
                keep.append(dict(p, hs=left))
        self.pending = keep
        return n


def _ident(s):
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", s or "")[:40]
    return s or "unknown"
