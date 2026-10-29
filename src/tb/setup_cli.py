"""`./tb setup`: пошаговый помощник владельца (запуск ТОЛЬКО самим владельцем в Terminal.app, нужен TTY).

  ./tb setup            интерактивно: ключ подписи, токен дашборда (печатается ОДИН раз), токен агента шлюза `api` (в файл, не на экран),
                        ключ DeepSeek (скрытый ввод getpass, запись в файл 0600 без печати), проверка прав, итоговая таблица
  ./tb setup --check    только таблица «что готово / чего нет» (без TTY, ничего не меняет, значений не читает и не печатает)

Правила: существующее НЕ перезаписывается без явного подтверждения (слово `да` / `yes`); ключи и значения никогда не печатаются и не логируются
(в выводе только имена файлов и признаки есть/нет). Права: POSIX chmod 0600/0700, Windows icacls (иначе предупреждение).
Ключ DeepSeek: имя файла и переменная берутся из config/providers.json (deepseek.key_file / key_env), формат файла `ИМЯ=значение` (строка KEY=VALUE).
"""
import getpass
import json
import os
import stat
import sys

from tb import paths
from tb.control import auth, cli as ctl_cli
from tb.gateway import registry
from tb.llm import keys as llm_keys

AGENT = "api"
PROVIDER = "deepseek"
YES = ("да", "д", "yes", "y")


def _is_tty():
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def keys_dir(environ=None):
    return auth.keys_dir(environ)


def provider_key_spec(root=None):
    """-> (key_file, key_env) провайдера DeepSeek из providers.json или значения по умолчанию."""
    try:
        from tb.core import localcfg
        p = (localcfg.read("providers", root).get("providers") or {}).get(PROVIDER) or {}
        return p.get("key_file") or "deepseek.env", p.get("key_env") or "DEEPSEEK_API_KEY"
    except (OSError, ValueError, AttributeError):
        return "deepseek.env", "DEEPSEEK_API_KEY"


def restrict(path, is_dir=False):
    """POSIX: 0600 (каталог 0700). Windows: icacls (cli.harden). -> True, если права ограничены."""
    if sys.platform.startswith("win"):
        return bool(ctl_cli.harden(path))
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
        return True
    except OSError:
        return False


def perms_ok(path):
    """-> True | False | None (None: не проверяемо, Windows). Только режим доступа, без чтения содержимого."""
    if os.name != "posix":
        return None
    try:
        return (os.stat(path).st_mode & 0o077) == 0
    except OSError:
        return None


def write_secret_file(path, text):
    """Атомарная запись файла с секретом, права 0600 с самого создания."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    restrict(path)


def has_provider_key(root=None, environ=None):
    """Есть ли непустой ключ DeepSeek в файле (значение не возвращается)."""
    kf, ke = provider_key_spec(root)
    try:
        return bool(llm_keys.present(kf, ke, environ)[0])
    except Exception:
        return False


def agent_state(root=None, environ=None):
    """-> {"agent": bool (есть в agents.json), "hash": bool, "file": bool}: только наличие."""
    try:
        with open(paths.config("agents.json", root=root), encoding="utf-8") as f:
            ag = (json.load(f).get("agents") or {}).get(AGENT)
    except (OSError, ValueError):
        ag = None
    tf = os.path.join(registry.token_dir(environ), AGENT + ".token")
    return {"agent": isinstance(ag, dict), "hash": bool(isinstance(ag, dict) and ag.get("token_hash")), "file": os.path.isfile(tf)}


def collect(root=None, environ=None):
    """Состояние для итоговой таблицы: [(пункт, ok|None, пояснение)]. Ничего не меняет, значений не читает."""
    env = os.environ if environ is None else environ
    kd = keys_dir(env)
    rows = []
    rows.append(("Каталог ключей", os.path.isdir(kd), kd if os.path.isdir(kd) else "нет каталога %s (создаст помощник)" % kd))
    st = auth.status(env)
    rows.append(("Ключ подписи (control.json)", st["key"], "есть" if st["key"] else "нет: шаг 1"))
    rows.append(("Токен дашборда", st["token"], "есть (в файле только хеш)" if st["token"] else "нет: шаг 2"))
    a = agent_state(root, env)
    ok = a["agent"] and a["hash"] and a["file"]
    rows.append(("Токен агента %s (шлюз)" % AGENT, ok, "готов" if ok else "нет агента в config/agents.json" if not a["agent"] else "нет хеша или файла токена: шаг 3"))
    kf, ke = provider_key_spec(root)
    pk = has_provider_key(root, env)
    rows.append(("Ключ DeepSeek (%s)" % kf, pk, "есть" if pk else "нет: шаг 4 (нужен только для живого мозга)"))
    bad = []
    for p in (auth.control_file(env), os.path.join(kd, kf), os.path.join(registry.token_dir(env), AGENT + ".token")):
        if os.path.isfile(p) and perms_ok(p) is False:
            bad.append(os.path.basename(p))
    rows.append(("Права файлов ключей", (not bad) if os.name == "posix" else None, "ok (0600)" if not bad and os.name == "posix" else "слишком открыты: %s (помощник исправит)" % ", ".join(bad) if bad else "Windows: проверьте вручную (icacls)"))
    return rows


def render(rows, out=print):
    out("")
    out("%-34s %-8s %s" % ("Что", "Статус", "Примечание"))
    for name, ok, note in rows:
        out("%-34s %-8s %s" % (name, {True: "готово", False: "НЕТ", None: "?"}[ok], note))
    out("")


class Helper(object):
    """Шаги помощника. ask/secret/out подменяются в тестах."""

    def __init__(self, root=None, environ=None, ask=input, secret=getpass.getpass, out=print):
        self.root = root
        self.env = os.environ if environ is None else environ
        self.ask, self.secret, self.out = ask, secret, out

    def yes(self, question, default=False):
        try:
            a = (self.ask("%s [%s]: " % (question, "Да/нет" if default else "да/Нет")) or "").strip().lower()
        except EOFError:
            return False
        return default if not a else a in YES

    def confirm_overwrite(self, what):
        """Перезапись существующего только по явному слову `да` (Enter = нет)."""
        return self.yes("%s уже есть. Перезаписать? Старое перестанет действовать" % what, default=False)

    def step_dir(self):
        kd = keys_dir(self.env)
        if not os.path.isdir(kd):
            os.makedirs(kd, exist_ok=True)
            self.out("создан каталог ключей: %s" % kd)
        restrict(kd, is_dir=True)

    def step_sign_key(self):
        self.out("Шаг 1. Ключ подписи (control.json): им подписываются команды управления, уровень риска и назначения моделей. Он общий для control и risk.")
        if auth.status(self.env)["key"]:
            self.out("  ключ подписи уже есть, не изменяется.")
            return False
        if not self.yes("  создать ключ подписи?", default=True):
            return False
        auth.init_key(self.env)
        restrict(auth.control_file(self.env))
        self.out("  ключ подписи создан (значение не показывается).")
        return True

    def step_dash_token(self):
        self.out("Шаг 2. Токен входа в дашборд (вкладка «Управление»). Печатается ОДИН раз, в файле только хеш.")
        st = auth.status(self.env)
        if not st["key"]:
            self.out("  нет ключа подписи: шаг пропущен.")
            return False
        if st["token"] and not self.confirm_overwrite("  токен дашборда"):
            self.out("  токен не изменён.")
            return False
        if not st["token"] and not self.yes("  создать токен?", default=True):
            return False
        tok = auth.new_token(self.env)
        restrict(auth.control_file(self.env))
        self.out("  ТОКЕН (сохраните в менеджере паролей, второй раз он не покажется):")
        self.out("  " + tok)
        return True

    def step_agent_token(self):
        self.out("Шаг 3. Токен агента шлюза `%s` (его использует API-мозг). Пишется в файл agents/%s.token (0600), на экран не выводится." % (AGENT, AGENT))
        a = agent_state(self.root, self.env)
        if not a["agent"]:
            self.out("  агента %s нет в config/agents.json: шаг пропущен." % AGENT)
            return False
        if a["hash"] and a["file"]:
            if not self.confirm_overwrite("  токен агента %s" % AGENT):
                self.out("  токен не изменён.")
                return False
        elif not self.yes("  создать токен агента?", default=True):
            return False
        tok = registry.new_token()
        registry.set_token_hash(AGENT, registry.hash_token(tok), self.root)
        path = registry.write_token_file(AGENT, tok, self.env)
        restrict(path)
        restrict(os.path.dirname(path), is_dir=True)
        self.out("  токен создан, файл: %s" % os.path.basename(path))
        return True

    def step_provider_key(self):
        kf, ke = provider_key_spec(self.root)
        path = os.path.join(keys_dir(self.env), kf)
        self.out("Шаг 4. Ключ DeepSeek. Нужен только для живого мозга; без него платформа и paper работают с заглушкой/без мозга.")
        self.out("  файл: %s, формат: одна строка %s=<ваш ключ> (без кавычек). Ключ берётся в кабинете DeepSeek." % (path, ke))
        self.out("  Включение мозга: подписчик api в личном слое config/local/subscriptions.json ({\"subscribers\": [{\"id\": \"api\", \"enabled\": true}]}), см. docs/QUICKSTART.md п. 4.")
        if has_provider_key(self.root, self.env):
            if not self.confirm_overwrite("  ключ DeepSeek"):
                self.out("  ключ не изменён.")
                return False
        else:
            a = (self.ask("  [1] ввести ключ сейчас (скрытый ввод), [2] создать пустой шаблон файла, Enter = пропустить: ") or "").strip()
            if a == "2":
                if os.path.exists(path):
                    self.out("  файл уже есть, шаблон не создаётся.")
                    return False
                write_secret_file(path, "# ключ DeepSeek: впишите значение после знака равенства, без кавычек\n%s=\n" % ke)
                self.out("  шаблон создан: %s; впишите ключ вручную (редактор), затем ./tb llm check." % path)
                return True
            if a != "1":
                return False
        val = (self.secret("  ключ (ввод скрыт): ") or "").strip()
        if not val:
            self.out("  пусто: ничего не записано.")
            return False
        write_secret_file(path, "%s=%s\n" % (ke, val))
        val = None
        self.out("  ключ записан в %s (значение не показывается). Проверка: ./tb llm check" % os.path.basename(path))
        return True

    def step_perms(self):
        self.out("Шаг 5. Права файлов ключей.")
        kd = keys_dir(self.env)
        kf, _ke = provider_key_spec(self.root)
        fixed = 0
        for p in (auth.control_file(self.env), os.path.join(kd, kf), os.path.join(registry.token_dir(self.env), AGENT + ".token")):
            if os.path.isfile(p) and perms_ok(p) is False:
                if restrict(p):
                    fixed += 1
                    self.out("  исправлены права: %s" % os.path.basename(p))
        if os.name != "posix":
            self.out("  Windows: права выставлены через icacls там, где это удалось; проверьте вручную, что файлы читает только ваш пользователь.")
        elif not fixed:
            self.out("  права в порядке.")

    def run(self):
        self.out("Помощник настройки traider-bot. Ключи и токены не печатаются (кроме токена дашборда, один раз). Enter = значение по умолчанию.")
        self.step_dir()
        self.step_sign_key()
        self.step_dash_token()
        self.step_agent_token()
        self.step_provider_key()
        self.step_perms()
        rows = collect(self.root, self.env)
        render(rows, self.out)
        self.out("Дальше: ./tb start   (затем открыть дашборд (для paper токен не нужен), нажать «Старт»; подробности docs/QUICKSTART.md)")
        return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv == ["--check"] or argv == ["check"]:
        render(collect())
        return 0
    if argv:
        print("использование: ./tb setup [--check]", file=sys.stderr)
        return 2
    if not _is_tty():
        print("./tb setup нужен интерактивный терминал (запускает владелец в Terminal.app). Только таблица: ./tb setup --check", file=sys.stderr)
        return 2
    try:
        return Helper().run()
    except (KeyboardInterrupt, EOFError):
        print("\nпрервано, сделанное сохранено", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
