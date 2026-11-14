"""Общие сетевые помощники роутера (router/netlib.sh) и их копия в router/install.sh: подставные команды, настоящий sh, без роутера."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NETLIB = ROOT / "router" / "netlib.sh"
INSTALL = ROOT / "router" / "install.sh"
BEGIN, END = "# >>> netlib", "# <<< netlib"


def put(path, text):
    path.write_text(text)
    path.chmod(0o755)


def block(text):
    return text[text.index(BEGIN):text.index(END) + len(END)]


class NetlibBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn-netlib-"))
        self.bin, self.fake = self.tmp / "bin", self.tmp / "fake"
        self.bin.mkdir()
        self.fake.mkdir()
        # netstat: прокси «слушает», только если есть файл-флаг
        put(self.bin / "netstat", '#!/bin/sh\n[ -f "$FAKE/proxy_listen" ] && echo "tcp 0 0 127.0.0.1:7890 0.0.0.0:* LISTEN"\nexit 0\n')
        # «капризная» команда: прямое подключение не удаётся, через прокси из окружения удаётся; все вызовы пишет в журнал
        put(self.bin / "flaky", '#!/bin/sh\necho "flaky $* proxy=${https_proxy:-none}" >> "$FAKE/calls"\n'
                                '[ -n "$https_proxy" ] || { echo "Connection refused" >&2; exit 1; }\nexit 0\n')
        put(self.bin / "alwaysfail", '#!/bin/sh\necho "alwaysfail proxy=${https_proxy:-none}" >> "$FAKE/calls"\nexit 1\n')
        # wget: пишет, какой прокси увидел
        put(self.bin / "wget", '#!/bin/sh\necho "wget $* https=${https_proxy:-none} http=${http_proxy:-none} HTTP=${HTTP_PROXY:-none}" >> "$FAKE/calls"\nexit 0\n')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, snippet, **env):
        e = {"PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE": str(self.fake), "HOME": str(self.tmp)}
        e.update({k: str(v) for k, v in env.items()})
        r = subprocess.run(["sh", "-c", f'. "{NETLIB}"\n{snippet}'], capture_output=True, text=True, env=e, timeout=30)
        return r.returncode, r.stdout + r.stderr

    def calls(self):
        f = self.fake / "calls"
        return f.read_text().splitlines() if f.exists() else []

    def listen(self, yes=True):
        flag = self.fake / "proxy_listen"
        flag.write_text("1") if yes else flag.unlink(missing_ok=True)


class FilesInSyncTests(unittest.TestCase):
    def test_syntax(self):
        for f in (NETLIB, INSTALL):
            self.assertEqual(subprocess.run(["sh", "-n", str(f)]).returncode, 0, f)

    def test_installer_carries_an_exact_copy_of_the_library(self):
        """install.sh запускают прямо из интернета и подключить файл он не может: копия обязана совпадать буква в букву."""
        self.assertEqual(block(INSTALL.read_text(encoding="utf-8")), block(NETLIB.read_text(encoding="utf-8")))

    def test_installer_uses_the_helpers_instead_of_the_old_fragile_code(self):
        s = INSTALL.read_text(encoding="utf-8")
        self.assertIn("LANIP=$(lan_ip)", s)
        self.assertNotIn("network.lan.ipaddr | cut", s.split(END)[1])                  # адрес только через общий помощник
        self.assertIn('name_resolves "dnevnik.$DOMAIN" "$LANIP"', s)
        self.assertIn("local_get http://127.0.0.1:8765/index.html", s)
        self.assertNotIn("wget -q -T 3 -O /dev/null http://127.0.0.1", s.split(END)[1])   # локальная проверка не идёт через прокси
        self.assertEqual(s.count("net_try wget"), 2)                                    # обе загрузки (с токеном и без)
        self.assertIn("net_try apk add", s)
        self.assertIn("net_try opkg install", s)
        self.assertIn("drop_proxy", s.split(END)[1])
        self.assertIn("https_proxy=http://127.0.0.1:7890", s)                           # в ошибке сказано, что делать

    def test_other_scripts_load_the_library(self):
        for name in ("remote.sh", "push.sh"):
            s = (ROOT / "router" / name).read_text(encoding="utf-8")
            self.assertIn("netlib.sh", s, name)
            self.assertIn("net_try", s, name)
            self.assertIn("https_proxy=http://127.0.0.1:7890", s, name)

    def test_library_is_installed_with_the_program(self):
        s = INSTALL.read_text(encoding="utf-8")
        self.assertIn('cp -r "$SRC/web" "$SRC/router" "$APP/"', s)                      # вся папка router, значит и netlib.sh
        self.assertIn('chmod 755 "$APP"/router/*.sh', s)


class NetTryTests(NetlibBase):
    def test_direct_connection_works_no_proxy_is_touched(self):
        self.listen()
        code, out = self.sh('net_try true; echo "rc=$? env=${https_proxy:-none}"')
        self.assertIn("rc=0 env=none", out)
        self.assertNotIn("Прямое подключение", out)

    def test_blocked_directly_retries_through_the_local_proxy_and_says_so(self):
        self.listen()
        code, out = self.sh('net_try flaky one two; echo "rc=$? env=${https_proxy:-none}"')
        self.assertIn("rc=0 env=http://127.0.0.1:7890", out)
        self.assertIn("Прямое подключение не получилось", out)
        self.assertIn("настройки роутера не меняются", out)
        self.assertEqual(self.calls(), ["flaky one two proxy=none", "flaky one two proxy=http://127.0.0.1:7890"])

    def test_without_a_listening_proxy_it_just_fails_once(self):
        self.listen(False)
        code, out = self.sh('net_try flaky; echo "rc=$?"')
        self.assertIn("rc=1", out)
        self.assertNotIn("Прямое подключение", out)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("proxy=none", self.calls()[0])

    def test_a_proxy_chosen_by_the_user_is_respected_not_overridden(self):
        self.listen()
        code, out = self.sh('net_try alwaysfail; echo "rc=$?"', https_proxy="http://10.0.0.5:3128")
        self.assertIn("rc=1", out)
        self.assertEqual(self.calls(), ["alwaysfail proxy=http://10.0.0.5:3128"])       # повтора через наш прокси нет

    def test_failure_through_the_proxy_too_is_reported_as_failure(self):
        self.listen()
        code, out = self.sh('net_try alwaysfail; echo "rc=$?"')
        self.assertIn("rc=1", out)
        self.assertEqual(len(self.calls()), 2)

    def test_custom_proxy_address(self):
        self.listen()
        (self.bin / "netstat").write_text('#!/bin/sh\necho "tcp 0 0 127.0.0.1:1080 0.0.0.0:* LISTEN"\n')
        code, out = self.sh('net_try flaky; echo "env=${https_proxy:-none}"', DNEVNIK_PROXY="127.0.0.1:1080")
        self.assertIn("env=http://127.0.0.1:1080", out)

    def test_drop_proxy_removes_only_what_we_set(self):
        self.listen()
        code, out = self.sh('net_try flaky >/dev/null; drop_proxy; echo "env=${https_proxy:-none}${http_proxy:-none}${HTTP_PROXY:-none}${HTTPS_PROXY:-none}"')
        self.assertIn("env=nonenonenonenone", out)
        code, out = self.sh('drop_proxy; echo "env=${https_proxy:-none}"', https_proxy="http://10.0.0.5:3128")
        self.assertIn("env=http://10.0.0.5:3128", out)                                  # чужой прокси остаётся

    def test_works_under_set_e_like_the_installer(self):
        self.listen()
        code, out = self.sh('set -e\nnet_try sh -c "true" || true\nnet_try flaky >/dev/null\necho alive')
        self.assertIn("alive", out)
        code, out = self.sh('set -e\nnet_try alwaysfail || echo "failed-but-alive"')
        self.assertIn("failed-but-alive", out)


class LocalGetTests(NetlibBase):
    def test_requests_to_the_router_itself_ignore_any_proxy(self):
        code, out = self.sh('local_get http://127.0.0.1:8765/index.html; echo "rc=$?"',
                            https_proxy="http://127.0.0.1:7890", http_proxy="http://127.0.0.1:7890",
                            HTTP_PROXY="http://127.0.0.1:7890", HTTPS_PROXY="http://127.0.0.1:7890", ALL_PROXY="x")
        self.assertIn("rc=0", out)
        self.assertEqual(self.calls(), ["wget -q -T 3 -O /dev/null http://127.0.0.1:8765/index.html https=none http=none HTTP=none"])

    def test_the_callers_environment_is_left_alone(self):
        code, out = self.sh('local_get http://127.0.0.1:8765/; echo "after=${https_proxy:-none}"', https_proxy="http://127.0.0.1:7890")
        self.assertIn("after=http://127.0.0.1:7890", out)                               # снято только внутри вызова

    def test_failure_is_reported(self):
        put(self.bin / "wget", "#!/bin/sh\nexit 4\n")
        code, out = self.sh('local_get http://127.0.0.1:1/ && echo up || echo down')
        self.assertIn("down", out)


class LanIpTests(NetlibBase):
    UBUS = '#!/bin/sh\n[ -f "$FAKE/ubus_ip" ] && printf \'{"up": true, "ipv4-address": [{"address": "%s", "mask": 24}]}\\n\' "$(cat "$FAKE/ubus_ip")"\nexit 0\n'
    # jsonfilter на роутере есть всегда; здесь подставной достаёт адрес из той же строки
    JSONFILTER = "#!/bin/sh\nsed -n 's/.*\"address\": \"\\([0-9.]*\\)\".*/\\1/p'\n"

    def setUp(self):
        super().setUp()
        put(self.bin / "ubus", self.UBUS)
        put(self.bin / "jsonfilter", self.JSONFILTER)
        put(self.bin / "uci", '#!/bin/sh\n[ -f "$FAKE/uci_ip" ] && cat "$FAKE/uci_ip"\nexit 0\n')

    def test_address_given_by_dhcp_is_found_through_ubus(self):
        (self.fake / "ubus_ip").write_text("192.168.50.20")                            # в настройках адреса нет (proto dhcp)
        code, out = self.sh("lan_ip")
        self.assertEqual(out.strip(), "192.168.50.20")

    def test_static_address_from_settings_when_ubus_has_nothing(self):
        (self.fake / "uci_ip").write_text("192.168.1.1/24\n")
        code, out = self.sh("lan_ip")
        self.assertEqual(out.strip(), "192.168.1.1")

    def test_what_the_router_really_has_wins_over_the_setting(self):
        (self.fake / "ubus_ip").write_text("192.168.73.1")
        (self.fake / "uci_ip").write_text("192.168.1.1")
        code, out = self.sh("lan_ip")
        self.assertEqual(out.strip(), "192.168.73.1")

    def test_nothing_known_gives_an_empty_answer_without_killing_the_script(self):
        code, out = self.sh("set -e\nA=$(lan_ip)\necho \"[$A]\"")
        self.assertEqual(code, 0, out)
        self.assertIn("[]", out)

    def test_missing_commands_do_not_kill_the_script(self):
        for n in ("ubus", "jsonfilter", "uci"):
            (self.bin / n).unlink()
        code, out = self.sh("set -e\nA=$(lan_ip)\necho \"[$A]\"", PATH=f"{self.bin}:/usr/bin:/bin")
        self.assertEqual(code, 0, out)
        self.assertIn("[]", out)


class NameResolvesTests(NetlibBase):
    def setUp(self):
        super().setUp()
        put(self.bin / "nslookup", '#!/bin/sh\nprintf "Server: 127.0.0.1\\nAddress: 127.0.0.1:53\\n\\nName: dnevnik.lan\\nAddress: %s\\n" "$(cat "$FAKE/answer")"\n')

    def test_name_pointing_at_the_router_is_ok(self):
        (self.fake / "answer").write_text("192.168.50.20")
        code, out = self.sh('name_resolves dnevnik.lan 192.168.50.20 && echo ok || echo no')
        self.assertIn("ok", out)

    def test_name_pointing_elsewhere_is_not_ok(self):
        (self.fake / "answer").write_text("192.168.50.1")
        code, out = self.sh('name_resolves dnevnik.lan 192.168.50.20 && echo ok || echo no')
        self.assertIn("no", out)

    def test_an_unknown_router_address_never_counts_as_success(self):
        """Раньше пустой адрес подходил к любому ответу, и установщик ложно писал «имя работает»."""
        (self.fake / "answer").write_text("192.168.50.1")
        code, out = self.sh('name_resolves dnevnik.lan "" && echo ok || echo no')
        self.assertIn("no", out)

    def test_a_longer_address_is_not_mistaken_for_ours(self):
        (self.fake / "answer").write_text("192.168.1.10")
        code, out = self.sh('name_resolves dnevnik.lan 192.168.1.1 && echo ok || echo no')
        self.assertIn("no", out)

    def test_works_inside_a_set_e_loop(self):
        (self.fake / "answer").write_text("192.168.1.10")
        code, out = self.sh('set -e\nOK=0\nfor i in 1 2; do name_resolves dnevnik.lan "" && { OK=1; break; }; done\necho "OK=$OK"')
        self.assertIn("OK=0", out)


if __name__ == "__main__":
    unittest.main()
