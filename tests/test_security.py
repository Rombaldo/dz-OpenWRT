"""Проверки безопасности: вход, секреты, чужие тексты на странице."""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import core

ROOT = Path(__file__).resolve().parent.parent


def cookie(name, domain, value="v" * 20, **kw):
    c = {"name": name, "value": value, "domain": domain, "path": "/", "expires": 4102444800, "httpOnly": True, "secure": True}
    c.update(kw)
    return c


class CookieTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session.json"
        self.path.write_text(json.dumps([
            cookie("DnevnikAuth_a", ".dnevnik.ru"),
            cookie("dnevnik_sst", "dnevnik.ru"),
            cookie("esia_session", "esia.gosuslugi.ru"),     # Госуслуги: нам не нужны
            cookie("sid", ".gosuslugi.ru"),
            cookie("_ym_uid", ".yandex.ru"),
            cookie("sneaky", "dnevnik.ru.evil.com"),
            cookie("sneaky2", "evildnevnik.ru"),
        ]))

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_dnevnik_cookies_are_loaded(self):
        names = sorted(c.name for c in core.load_jar(self.path))
        self.assertEqual(names, ["DnevnikAuth_a", "dnevnik_sst"])

    def test_only_dnevnik_cookies_are_saved(self):
        names = sorted(c["name"] for c in core.jar_to_list(core.load_jar(self.path)))
        self.assertEqual(names, ["DnevnikAuth_a", "dnevnik_sst"])

    def test_cookies_go_only_to_dnevnik(self):
        jar = core.load_jar(self.path)

        def sent(url):
            r = urllib.request.Request(url)
            jar.add_cookie_header(r)
            return r.get_header("Cookie")

        self.assertIn("DnevnikAuth_a", sent("https://dnevnik.ru/api/v2/marks/diary"))
        self.assertIsNone(sent("https://b5.csdnevnik.ru/file.pdf"))      # сервер файлов cookie не получает
        self.assertIsNone(sent("https://evil.example.com/"))
        self.assertIsNone(sent("https://dnevnik.ru.evil.com/"))
        self.assertIsNone(sent("http://dnevnik.ru/api"))                 # по незащищённому соединению не отправляем

    def test_login_files_are_private(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "data"
            core.write_private(d / "session.json", "[]")
            self.assertEqual(stat.S_IMODE(os.stat(d / "session.json").st_mode), 0o600)

    def test_tls_is_verified(self):
        ctx = core.make_ssl_context()
        import ssl
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


@unittest.skipIf(shutil.which("git") is None or shutil.which("bash") is None, "нужны git и bash")
class SecretScannerTests(unittest.TestCase):
    """Сканер секретов проверяем во временном репозитории с выдуманными «секретами»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        (self.repo / "scripts").mkdir()
        for f in ("check-secrets.sh", "check_secrets.py"):
            shutil.copy(ROOT / "scripts" / f, self.repo / "scripts")
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "t")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *a):
        return subprocess.run(["git", *a], cwd=self.repo, capture_output=True, text=True)

    def scan(self, mode="staged"):
        r = subprocess.run(["bash", "scripts/check-secrets.sh", mode], cwd=self.repo, capture_output=True, text=True)
        return r.returncode, r.stdout

    def add(self, name, text="x\n"):
        p = self.repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.git("add", "-f", name)

    def test_clean_repo_passes(self):
        self.add("a.py", "print('привет')\n")
        self.assertEqual(self.scan()[0], 0)

    def test_private_key_is_caught(self):
        self.add("a.txt", "-----BEGIN " + "OPENSSH PRIVATE KEY-----\nAAAA\n")   # строку собираем частями, чтобы сам тест не выглядел как ключ
        code, out = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("ключ", out)

    def test_github_token_is_caught(self):
        self.add("a.sh", "TOKEN=ghp_" + "A" * 36 + "\n")
        self.add("b.sh", "T=github_pat_" + "B" * 40 + "\n")
        self.assertEqual(self.scan()[0], 1)

    def test_variable_token_is_fine(self):
        self.add("install.sh", 'wget --header="Authorization: Bearer $TOKEN" "$URL"\n')
        self.assertEqual(self.scan()[0], 0)

    def test_data_files_are_caught_by_name(self):
        for name in ["data/diary.json", "session.json", "credentials.json", "x/login-state.json", "x/ids.json", "config.json", "k.pem", "data/router_key", "known_hosts", ".env"]:
            with self.subTest(name=name):
                self.git("reset", "-q")
                self.add(name)
                self.assertEqual(self.scan()[0], 1, name)

    def test_login_cookie_in_code_is_caught(self):
        self.add("a.py", 'c = {"DnevnikAuth_a": "' + "a1b2" * 8 + '"}\n')
        self.assertEqual(self.scan()[0], 1)
        self.git("reset", "-q")
        self.add("b.py", "personId = 9999999999999\n")
        self.assertEqual(self.scan()[0], 1)

    def test_history_scan_finds_old_leak(self):
        self.add("a.txt", "TOKEN=ghp_" + "C" * 36 + "\n")
        self.git("commit", "-q", "-m", "leak")
        self.git("rm", "-q", "-f", "a.txt")
        self.git("commit", "-q", "-m", "removed")
        self.assertEqual(self.scan("tree")[0], 0)        # в текущих файлах уже нет
        self.assertEqual(self.scan("history")[0], 1)     # но в истории остался

    def test_private_words_from_the_closed_list_are_caught(self):
        (self.repo / "data").mkdir()
        (self.repo / "data" / "private-words.txt").write_text("# комментарий\nСекретноеИмя\nШкола № 99\n")   # файл лежит в закрытой папке, в git не добавляется
        self.add("README.md", "Привет, Школа № 7!\n")
        self.assertEqual(self.scan()[0], 0)                       # посторонний текст не мешает
        self.add("README.md", "Пример: секретноеимя учится в школа №99\n")
        code, out = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("личные слова", out)
        self.git("commit", "-q", "-m", "x")
        self.assertEqual(self.scan("tree")[0], 1)
        self.assertEqual(self.scan("history")[0], 1)

    # --- адреса сети Tailscale: значения собираем частями, чтобы сам тест не выглядел как утечка ---
    def test_real_tailscale_address_is_caught_but_invented_examples_pass(self):
        real4 = "100." + "90.12.34"
        self.add("a.py", f'addr = "{real4}"\n')
        code, out = self.scan()
        self.assertEqual(code, 1)
        self.assertIn("адреса сети", out)
        self.git("reset", "-q")
        for ok in ("100.64.0.7", "100.63.1.1", "100.128.1.1", "1100.90.1.1", "8.8.8.8", "192.168.1.50", "100.64.0"):        # выдуманные, вне диапазона и обычные
            with self.subTest(ok=ok):
                self.git("reset", "-q")
                self.add("b.py", f'x = "{ok}"\n')
                self.assertEqual(self.scan()[0], 0, ok)

    def test_real_tailscale_ipv6_and_tailnet_name_are_caught(self):
        for bad in ("fd7a:115c:a1e0:" + "ab12:4843:cd96:6255:d3b", "my." + "tail" + "1a2b3c" + ".ts.net", "TAIL" + "1A2B3C.TS.NET"):
            with self.subTest(bad=bad):
                self.git("reset", "-q")
                self.add("a.md", f"адрес {bad}\n")
                self.assertEqual(self.scan()[0], 1, bad)
        for ok in ("fd7a:115c:a1e0::1", "dnevnik.example.ts.net", "https://dnevnik.<ваша-сеть>.ts.net"):
            with self.subTest(ok=ok):
                self.git("reset", "-q")
                self.add("b.md", f"адрес {ok}\n")
                self.assertEqual(self.scan()[0], 0, ok)

    def test_old_findings_are_exempt_in_history_only_by_exact_line_fingerprint(self):
        line = 'addr = "' + "100." + '90.12.34"'
        self.add("a.py", line + "\n")
        self.git("commit", "-q", "-m", "old")
        self.assertEqual(self.scan("history")[0], 1)                                     # новая находка в истории ловится
        self.assertEqual(self.scan("tree")[0], 1)                                        # и в файлах
        import hashlib
        fp = hashlib.sha256(f"a.py: {line}".encode("utf-8")).hexdigest()[:16]
        script = self.repo / "scripts" / "check_secrets.py"
        script.write_text(script.read_text(encoding="utf-8").replace("KNOWN_OLD = {", "KNOWN_OLD = {" + repr(fp) + ", ", 1), encoding="utf-8")
        self.assertEqual(self.scan("history")[0], 0)                                     # учтённая старая строка истории не мешает
        self.assertEqual(self.scan("tree")[0], 1)                                        # но пока она есть в файлах, это стоп
        self.add("c.py", 'other = "' + "100." + '90.12.35"\n')                          # другая строка отпечатку не соответствует
        self.git("commit", "-q", "-m", "new")
        self.assertEqual(self.scan("history")[0], 1)

    def test_known_old_fingerprints_do_not_contain_values_and_match_nothing_current(self):
        src = (ROOT / "scripts" / "check_secrets.py").read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r"KNOWN_OLD = \{([^}]*)\}", src)
        self.assertTrue(m)
        for fp in _re.findall(r'"([^"]+)"', m.group(1)):
            self.assertRegex(fp, r"^[0-9a-f]{16}$")                                      # только отпечатки, а не сами строки

    def test_project_itself_is_clean(self):
        for mode in ("tree", "history"):
            r = subprocess.run(["bash", "scripts/check-secrets.sh", mode], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)


if __name__ == "__main__":
    unittest.main()
