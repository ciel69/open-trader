"""Идентификаторы ордеров исполнителя `tbx-<plan_n>-<action_n>` (06 п. 6.5). Владелец: WP2.

Не пересекается с `pending.LINK_RE` (прежние ордера). Длина <= 36 символов (лимит orderLinkId Bybit).
Чистые функции: только `re`.
"""
import re

PREFIX = "tbx-"
MAX_LEN = 36
_RE = re.compile(r"tbx-([0-9]+)-([0-9]+)")   # [0-9], а не \d: юникодные цифры не принимаем
_RE_V = re.compile(r"tbx-([a-z][a-z0-9]{1,11})-([0-9]+)-([0-9]+)")    # ссылка биржи кроме основной: tbx-<биржа>-<план>-<действие> (параллельные исполнители)
DEFAULT_VENUE = "bybit"


def make(plan_n, action_n, venue=None):
    """-> "tbx-<plan_n>-<action_n>"; для биржи кроме основной "tbx-<биржа>-<plan_n>-<action_n>". Номера: целые >= 0 (bool не принимается); длина <= 36, иначе ValueError.

    >>> make(12, 3)
    'tbx-12-3'
    >>> make(12, 3, "okx")
    'tbx-okx-12-3'
    """
    for v in (plan_n, action_n):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError("linkid.make: номера должны быть целыми >= 0, получено %r" % (v,))
    if venue in (None, "", DEFAULT_VENUE):
        s = "%s%d-%d" % (PREFIX, plan_n, action_n)
    else:
        if not re.fullmatch(r"[a-z][a-z0-9]{1,11}", str(venue)):
            raise ValueError("linkid.make: биржа %r" % (venue,))
        s = "%s%s-%d-%d" % (PREFIX, venue, plan_n, action_n)
    if len(s) > MAX_LEN:
        raise ValueError("linkid.make: длина %d > %d" % (len(s), MAX_LEN))
    return s


def parse(link):
    """-> (plan_n, action_n) или None (чужая/битая ссылка, не строка, длиннее лимита).

    >>> parse("tbx-12-3")
    (12, 3)
    >>> parse("BB-M-SOL") is None
    True
    """
    if not isinstance(link, str) or len(link) > MAX_LEN:
        return None
    m = _RE.fullmatch(link)
    if not m:
        m = _RE_V.fullmatch(link)
        return (int(m.group(2)), int(m.group(3))) if m else None
    return int(m.group(1)), int(m.group(2))


def venue_of(link):
    """-> id биржи ссылки ("bybit" для ссылок без префикса биржи) или None для чужой ссылки.

    >>> venue_of("tbx-okx-1-2"), venue_of("tbx-1-2"), venue_of("x")
    ('okx', 'bybit', None)
    """
    if not isinstance(link, str) or len(link) > MAX_LEN:
        return None
    m = _RE_V.fullmatch(link)
    if m:
        return m.group(1)
    return DEFAULT_VENUE if _RE.fullmatch(link) else None


def is_ours(link):
    """True, если link начинается с префикса `tbx-` (строка).

    >>> is_ours("tbx-1-1"), is_ours("x")
    (True, False)
    """
    return isinstance(link, str) and link.startswith(PREFIX)
