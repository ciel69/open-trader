"""`./tb audit-public`: проверка дерева проекта перед публикацией в открытый репозиторий. ТОЛЬКО ЧТЕНИЕ, без сети, без запуска git.

Обходит дерево (кроме .git и всего, что скрывает .gitignore) и ищет: похожие на секреты строки и файлы, личные данные (пути домашних каталогов, e-mail, IP,
имя пользователя ОС, адреса кошельков, номера счетов), крупные и бинарные файлы, личные каталоги (state/, runtime/, data/, .claude/ ...), которые
.gitignore НЕ скрывает, упоминания личных настроек и отсутствие или нестандартность лицензии.
Найденные значения НИКОГДА не печатаются целиком: файл, строка, тип, первые 3 символа и длина.
Код возврата: 0 = нет находок уровня high/medium (low только предупреждения), 1 = есть high/medium.
Использование: ./tb audit-public [--json] [--root ПУТЬ] [--term СЛОВО ...] [--max-list N] [--strict]
  --term   дополнительное личное слово (имя, ник), ищется без учёта регистра; имя пользователя ОС добавляется само
  --strict код 1 и при находках low
"""
import fnmatch
import getpass
import json
import os
import re
import sys

MAX_TEXT = 1024 * 1024          # файлы больше не читаются и считаются крупными
MAX_BIG = 5 * 1024 * 1024       # выше: severity medium
PRIVATE = ("state", "runtime", "data", "config/local", ".claude", ".impeccable", "CLAUDE.local.md", ".idea", ".vscode", "venv", ".venv")
JUNK = (".DS_Store", "Thumbs.db")
SECRET_FILES = ("*.env", ".env", ".env.*", "*.token", "*.pem", "*.key", "id_rsa", "id_ed25519", "*.p12", "*.pfx")
SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
PLACEHOLDER_WORDS = ("example", "xxxx", "your", "changeme", "placeholder", "dummy", "fake", "sample", "test", "abcdef", "123456")

RE_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")
RE_AKIA = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
RE_GH = re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{30,}")
RE_PRIV = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
RE_ASSIGN = re.compile(r"""(?i)(?:api[_-]?key|secret|token|passw(?:or)?d|auth)\w*["']?\s*[:=]\s*["']?([A-Za-z0-9+/_\-=]{20,})""")
RE_SHA = re.compile(r"\bsha256:([0-9a-f]{64})\b")
RE_HEX = re.compile(r"\b[0-9a-fA-F]{40,}\b")
RE_B64 = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
RE_WALLET = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
RE_ACCT = re.compile(r"""(?i)(?:account|acct|uid|iban|wallet|card)\w*["']?\s*[:=]\s*["']?(\d{9,})""")
RE_UPATH = re.compile(r"(?:/Users/|/home/)([A-Za-z0-9._\-]+)|[A-Za-z]:\\+Users\\+([A-Za-z0-9._\-]+)")
RE_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b")
RE_IP = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
RE_PERSONAL = re.compile(r"CLAUDE\.local|settings\.local\.json")

OK_USERS = {"x", "me", "u", "user", "users", "name", "you", "username", "<name>", "$user", "%username%", "runner", "home", "foo", "bar", "tb", "someone", "owner"}
OK_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "anthropic.com", "users.noreply.github.com", "localhost", "test", "invalid")
LICENSE_MIT = "permission is hereby granted, free of charge"


def mask(value):
    value = str(value)
    return value[:3] + "***" + "(%d)" % len(value)


def is_placeholder(value):
    v = value.lower()
    if len(set(v)) <= 3:
        return True
    return any(w in v for w in PLACEHOLDER_WORDS)


def parse_gitignore(root):
    rules = []
    try:
        with open(os.path.join(root, ".gitignore"), encoding="utf-8") as f:
            for raw in f:
                p = raw.strip()
                if not p or p.startswith("#"):
                    continue
                neg = p.startswith("!")
                if neg:
                    p = p[1:]
                dir_only = p.endswith("/")
                p = p.rstrip("/")
                anchored = "/" in p
                p = p.lstrip("/")
                rules.append((p, neg, dir_only, anchored))
    except OSError:
        pass
    return rules


def ignored(rel, is_dir, rules):
    """Упрощённая семантика .gitignore: последнее совпавшее правило решает; родительский игнорируемый каталог тоже скрывает."""
    parts = rel.split("/")
    for i in range(1, len(parts) + 1):
        sub, sub_dir = "/".join(parts[:i]), (i < len(parts) or is_dir)
        res = None
        for pat, neg, dir_only, anchored in rules:
            if dir_only and not sub_dir:
                continue
            hit = fnmatch.fnmatch(sub, pat) if anchored else fnmatch.fnmatch(parts[i - 1], pat)
            if hit:
                res = not neg
        if res:
            return True
    return False


class Finding(dict):
    pass


def add(out, sev, cat, rule, rel, line=0, value=None, note=""):
    out.append({"severity": sev, "category": cat, "rule": rule, "file": rel, "line": line,
                "sample": mask(value) if value is not None else "", "note": note})


def scan_line(line, rel, ln, terms, low_zone, out):
    """Правила построчно. low_zone: тесты и документация: секретоподобные строки понижаются до low (там живут поддельные примеры)."""
    s_secret = "low" if low_zone else "high"
    for rx, rule in ((RE_SK, "sk-key"), (RE_AKIA, "aws-key"), (RE_GH, "github-token")):
        for m in rx.finditer(line):
            if not is_placeholder(m.group(0)):
                add(out, s_secret, "secret", rule, rel, ln, m.group(0))
    if RE_PRIV.search(line):
        add(out, s_secret, "secret", "private-key-block", rel, ln, "-----")
    for m in RE_ASSIGN.finditer(line):
        v = m.group(1)
        if re.search(r"\d", v) and re.search(r"[A-Za-z]", v) and not is_placeholder(v):
            add(out, "low" if low_zone else "medium", "secret", "key-assignment", rel, ln, v)
    for m in RE_SHA.finditer(line):
        add(out, "low", "secret", "token-hash", rel, ln, m.group(1), "хеш токена: не секрет, но лучше обнулить")
    for m in RE_HEX.finditer(line):
        if not is_placeholder(m.group(0)) and not RE_SHA.search(line):
            add(out, "low", "secret", "long-hex", rel, ln, m.group(0))
    for m in RE_B64.finditer(line):
        v = m.group(0)
        if re.search(r"\d", v) and re.search(r"[a-z]", v) and re.search(r"[A-Z]", v) and v.count("/") < 3 and not is_placeholder(v) and not RE_HEX.fullmatch(v):
            add(out, "low", "secret", "long-base64", rel, ln, v)
    for m in RE_WALLET.finditer(line):
        if not is_placeholder(m.group(0)):
            add(out, "medium", "personal", "wallet-address", rel, ln, m.group(0))
    for m in RE_ACCT.finditer(line):
        if not is_placeholder(m.group(1)):
            add(out, "medium", "personal", "account-number", rel, ln, m.group(1))
    for m in RE_UPATH.finditer(line):
        name = m.group(1) or m.group(2) or ""
        if name.lower() not in OK_USERS and not name.startswith("<"):
            add(out, "low" if low_zone else "medium", "personal", "home-path", rel, ln, name)
    for m in RE_EMAIL.finditer(line):
        dom = m.group(0).split("@", 1)[1].lower()
        if not any(dom == d or dom.endswith("." + d) for d in OK_EMAIL_DOMAINS):
            add(out, "medium", "personal", "email", rel, ln, m.group(0))
    for m in RE_IP.finditer(line):
        a = [int(x) for x in m.groups()]
        if max(a) <= 255 and a[0] not in (0, 127, 255) and not (a[0] == 192 and a[1] == 0 and a[2] == 2) and not (a[0] < 4 and a[1] < 4):
            add(out, "low", "personal", "ip-address", rel, ln, m.group(0))
    low = line.lower()
    has_path = any(f["file"] == rel and f["line"] == ln and f["rule"] == "home-path" for f in out[-6:])
    for t in terms:
        if t in low and not has_path:
            add(out, "medium", "personal", "personal-term", rel, ln, t, "слово из --term или имя пользователя ОС")
    for m in ([] if rel in (".gitignore", ".gitattributes") else RE_PERSONAL.finditer(line)):
        add(out, "low", "personal-settings", "local-settings-mention", rel, ln, m.group(0))


def is_binary(head):
    return b"\x00" in head


def audit(root, extra_terms=()):
    root = os.path.abspath(root)
    rules = parse_gitignore(root)
    terms = set(t.lower() for t in extra_terms if t)
    try:
        u = getpass.getuser()
        if len(u) >= 4:
            terms.add(u.lower())
    except Exception:
        pass
    home = os.path.basename(os.path.expanduser("~"))
    if len(home) >= 4:
        terms.add(home.lower())
    out, scanned = [], 0
    if not rules:
        add(out, "high", "gitignore", "missing-gitignore", ".gitignore", 0, None, "нет .gitignore (или пуст): личные каталоги не скрыты")
    for d, dn, fn in os.walk(root):
        rd = os.path.relpath(d, root).replace(os.sep, "/")
        rd = "" if rd == "." else rd
        keep = []
        for n in sorted(dn):
            rel = (rd + "/" + n) if rd else n
            if n == ".git":
                continue
            if ignored(rel, True, rules):
                continue
            if rel in PRIVATE:
                add(out, "high", "private-data", "private-dir-not-ignored", rel + "/", 0, None, "каталог с личными данными не скрыт .gitignore")
                continue
            if n == "__pycache__":
                add(out, "low", "junk", "pycache-not-ignored", rel + "/", 0)
                continue
            keep.append(n)
        dn[:] = keep
        for n in sorted(fn):
            rel = (rd + "/" + n) if rd else n
            if ignored(rel, False, rules):
                continue
            if rel in PRIVATE:
                add(out, "high", "private-data", "private-file-not-ignored", rel, 0, None, "личный файл не скрыт .gitignore")
                continue
            if n in JUNK:
                add(out, "low", "junk", "os-junk", rel, 0)
                continue
            if any(fnmatch.fnmatch(n, p) for p in SECRET_FILES):
                add(out, "high", "secret", "secret-file", rel, 0, n, "файл ключа или окружения")
                continue
            path = os.path.join(d, n)
            try:
                size = os.path.getsize(path)
                with open(path, "rb") as f:
                    head = f.read(4096)
            except OSError:
                continue
            if size > MAX_TEXT:
                add(out, "medium" if size > MAX_BIG else "low", "large-file", "large-file", rel, 0, None, "%d КиБ" % (size // 1024))
                continue
            if is_binary(head):
                add(out, "low", "binary", "binary-file", rel, 0, None, "%d Б" % size)
                continue
            scanned += 1
            low_zone = rel.startswith(("tests/", "docs/"))
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for ln, line in enumerate(f, 1):
                        if len(line) > 5000:
                            line = line[:5000]
                        if "audit: ok" in line:
                            continue
                        scan_line(line, rel, ln, terms, low_zone, out)
            except OSError:
                continue
    lic = os.path.join(root, "LICENSE")
    if not os.path.isfile(lic):
        add(out, "high", "license", "missing-license", "LICENSE", 0, None, "файл лицензии отсутствует")
    else:
        try:
            with open(lic, encoding="utf-8", errors="replace") as f:
                txt = f.read().lower()
        except OSError:
            txt = ""
        if "mit license" not in txt or LICENSE_MIT not in txt:
            add(out, "medium", "license", "nonstandard-license", "LICENSE", 0, None, "ожидается текст MIT")
    out.sort(key=lambda x: (SEV_ORDER[x["severity"]], x["category"], x["file"], x["line"]))
    return {"root_scanned_files": scanned, "findings": out}


def summarize(findings):
    cats, sevs, files = {}, {}, {}
    for f in findings:
        cats[f["category"]] = cats.get(f["category"], 0) + 1
        sevs[f["severity"]] = sevs.get(f["severity"], 0) + 1
        files.setdefault(f["file"], 0)
        files[f["file"]] += 1
    return {"total": len(findings), "by_severity": sevs, "by_category": cats, "files": len(files), "files_list": sorted(files)}


def main(argv):
    root, terms, as_json, strict, max_list = None, [], False, False, 200
    a = list(argv)
    while a:
        x = a.pop(0)
        if x == "--json":
            as_json = True
        elif x == "--strict":
            strict = True
        elif x == "--root" and a:
            root = a.pop(0)
        elif x == "--term" and a:
            terms.append(a.pop(0))
        elif x == "--max-list" and a:
            max_list = int(a.pop(0))
        elif x in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            print("неизвестный аргумент: %s (см. --help)" % x, file=sys.stderr)
            return 2
    if root is None:
        from tb import paths
        root = paths.root()
    res = audit(root, terms)
    summ = summarize(res["findings"])
    bad = summ["by_severity"].get("high", 0) + summ["by_severity"].get("medium", 0)
    code = 1 if (bad or (strict and summ["total"])) else 0
    if as_json:
        print(json.dumps({"ok": code == 0, "scanned_files": res["root_scanned_files"], "summary": summ, "findings": res["findings"]}, ensure_ascii=False, indent=1))
        return code
    for f in res["findings"][:max_list]:
        print("%-6s %-18s %s%s  %s%s" % (f["severity"], f["category"] + "/" + f["rule"], f["file"], (":%d" % f["line"]) if f["line"] else "",
                                         f["sample"], ("  [%s]" % f["note"]) if f["note"] else ""))
    if len(res["findings"]) > max_list:
        print("... ещё %d (увеличьте --max-list или используйте --json)" % (len(res["findings"]) - max_list))
    print("\nпросмотрено текстовых файлов: %d; находок: %d (high %d, medium %d, low %d); файлов с находками: %d"
          % (res["root_scanned_files"], summ["total"], summ["by_severity"].get("high", 0), summ["by_severity"].get("medium", 0),
             summ["by_severity"].get("low", 0), summ["files"]))
    print("по категориям: " + (", ".join("%s %d" % kv for kv in sorted(summ["by_category"].items())) or "нет"))
    print("итог: " + ("ЧИСТО (нет high/medium)" if code == 0 else "ЕСТЬ ЧТО ИСПРАВИТЬ перед публикацией"))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
