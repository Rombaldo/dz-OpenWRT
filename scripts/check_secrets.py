#!/usr/bin/env python3
"""Ищет в проекте секреты и личные данные: ключи, токены, cookie входа, номера ученика, файлы с данными
и личные слова из закрытого списка (data/private-words.txt: имя ребёнка, школа, логин).

    check-secrets.sh staged    - то, что добавлено в коммит (запускается хуком перед каждым коммитом)
    check-secrets.sh tree      - все файлы проекта, которые отслеживает git
    check-secrets.sh history   - ВСЯ история git (запустить перед отправкой на GitHub)
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Файлы, которых в git быть не должно ни при каких условиях
NAMES = re.compile(r"(^|/)(data/|session\.json$|credentials\.json$|login-state\.json$|ids\.json$|diary\.json$|history\.json$|"
                   r"config\.json$|known_hosts$|\.env$|\.dnevnik-token$|[^/]*router_key[^/]*$|[^/]*\.pem$|[^/]*\.key$|[^/]*\.token$)")
# Явные секреты: ищем везде
STRONG = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|"
                    r"AKIA[0-9A-Z]{16}|[Bb]earer +[A-Za-z0-9._-]{30,}")
# Похоже на данные входа или ученика (в тестах значения выдуманные)
WEAK = re.compile(r"""(DnevnikAuth_[a-z]|dnevnik_sst|a_r_p_i)["']? *[:=] *["']?[A-Za-z0-9%._-]{16,}|"""
                  r"""personId["']? *[:=] *["']?[0-9]{10,}""")
SELF = ("scripts/check-secrets.sh", "scripts/check_secrets.py")

# Настоящие адреса сети Tailscale выдают вашу сеть: адрес узла 100.64.0.0/10, IPv6 узла (fd7a:115c:a1e0:…) и имя сети вида tailXXXXXX.ts.net.
# В примерах и тестах допустимы только выдуманные: 100.64.0.x и общий для всех префикс fd7a:115c:a1e0:: (двоеточия подряд).
CGNAT4 = re.compile(r"(?<![\d.])100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}(?!\d|\.\d)")
TS6 = re.compile(r"fd7a:115c:a1e0:[0-9a-f]{1,4}:[0-9a-f]{1,4}:", re.I)
TAILNET = re.compile(r"\btail[0-9a-f]{5,8}\.ts\.net\b", re.I)
# Старые находки, уже попавшие в историю до появления этого правила (версии 0.20.0 и 0.21.0). В текущих файлах их нет; из истории их убрать можно
# только переписав её, поэтому они учтены по отпечатку строки (сами значения здесь не записаны) и не мешают проверке истории. Новые находки ловятся.
KNOWN_OLD = {"621b63e09d45ffea", "935bdaca126e4688"}


def real_addresses(line):
    hits = [m.group(0) for m in CGNAT4.finditer(line) if not m.group(0).startswith("100.64.0.")]
    return hits + [m.group(0) for m in TS6.finditer(line)] + [m.group(0) for m in TAILNET.finditer(line)]


def fingerprint(entry):
    return hashlib.sha256(entry.encode("utf-8")).hexdigest()[:16]


def private_regex():
    """Личные слова: по одному в строке, обычным текстом; регистр и пробелы не важны."""
    f = ROOT / "data" / "private-words.txt"
    if not f.exists():
        return None
    parts = []
    for line in f.read_text(encoding="utf-8").splitlines():
        w = line.strip()
        if w and not w.startswith("#"):
            parts.append(r"\s*".join(re.escape(x) for x in w.split()))
    return re.compile("|".join(parts), re.I) if parts else None


def git(*args):
    r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    return r.stdout.decode("utf-8", "replace")


def added_lines(patch):
    """Из текста изменений достаёт пары (файл, добавленная строка)."""
    path, out = "", []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((path, line[1:]))
    return out


def collect(mode):
    if mode == "staged":
        names = git("diff", "--cached", "--name-only", "--diff-filter=AM").split("\n")
        return names, added_lines(git("diff", "--cached", "-U0"))
    if mode == "tree":
        names = git("ls-files").split("\n")
        lines = []
        for n in names:
            if not n:
                continue
            try:
                data = (ROOT / n).read_bytes()
            except OSError:
                continue
            if b"\0" in data[:2000]:
                continue
            lines += [(n, l) for l in data.decode("utf-8", "replace").splitlines()]
        return names, lines
    if mode == "history":
        names = git("log", "--all", "--name-only", "--pretty=format:").split("\n")
        return names, added_lines(git("log", "--all", "-p", "-U0", "--format="))
    raise SystemExit(f"Использование: {sys.argv[0]} staged|tree|history")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "staged"
    names, lines = collect(mode)
    private = private_regex()
    fail = False

    def report(kind, text, hits):
        nonlocal fail
        fail = True
        print(f"СТОП [{kind}]: {text}")
        for h in hits[:5]:
            print("    " + (h if len(h) <= 76 else h[:75] + "…"))

    bad = sorted({n for n in names if n and NAMES.search(n)})
    if bad:
        report("файлы", "в git попали файлы с личными данными или ключами:", bad)
    strong = [f"{p}: {l.strip()}" for p, l in lines if p not in SELF and STRONG.search(l)]
    if strong:
        report("ключи и токены", "в тексте найден ключ или токен:", strong)
    weak = [f"{p}: {l.strip()}" for p, l in lines if p not in SELF and not p.startswith(("tests/", ".githooks/")) and WEAK.search(l)]
    if weak:
        report("вход", "в тексте похоже на cookie входа или номер ученика:", weak)
    if private:
        words = [f"{p}: {l.strip()}" for p, l in lines if p not in SELF and private.search(l)]
        if words:
            report("личные слова", "в тексте найдены слова из вашего закрытого списка data/private-words.txt:", words)
    addr = [f"{p}: {l.strip()}" for p, l in lines if p not in SELF and real_addresses(l)]
    if mode == "history":
        addr = [a for a in addr if fingerprint(a) not in KNOWN_OLD]
    if addr:
        report("адреса сети", "в тексте найден настоящий адрес или имя сети Tailscale (в примерах только 100.64.0.x и fd7a:115c:a1e0::):", addr)
    if not fail:
        print(f"Чисто: секретов и личных данных не найдено ({mode}).")
    sys.exit(1 if fail else 0)


main()
