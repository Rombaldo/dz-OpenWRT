"""Тесты страницы «Службы → Дневник» в LuCI: серверная часть (rpcd-плагин), установка файлов, выбор --luci и сама страница."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "luci" / "rpcd" / "luci.dnevnik"
VIEW = ROOT / "luci" / "htdocs" / "view" / "dnevnik" / "status.js"
ACL = ROOT / "luci" / "acl.d" / "luci-app-dnevnik.json"
MENU = ROOT / "luci" / "menu.d" / "luci-app-dnevnik.json"

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None


def load_plugin():
    loader = importlib.machinery.SourceFileLoader("luci_dnevnik", str(PLUGIN))
    spec = importlib.util.spec_from_loader("luci_dnevnik", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class PluginCase(unittest.TestCase):
    """Плагин запускается как на роутере (отдельной программой), но все пути указывают во временную папку."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dnevnik-luci-"))
        self.home, self.www, self.run_dir = self.tmp / "home", self.tmp / "www", self.tmp / "run"
        for d in (self.home / "router", self.home / "data", self.www, self.run_dir):
            d.mkdir(parents=True)
        shutil.copy(ROOT / "core.py", self.home / "core.py")
        (self.home / "VERSION").write_text("0.12.0\n")
        (self.home / "config.json").write_text(json.dumps({"name": "", "other": 5}))
        self.cron = self.tmp / "crontab"
        self.cron.write_text("0 3 * * * /usr/bin/other\n*/20 6-22 * * * /root/dnevnik/router/sync.sh\n")
        (self.www / "data.json").write_text(json.dumps({
            "updatedAt": "2026-11-12T01:29:00", "days": [{}, {}, {}], "student": {"name": "Аня", "school": "Школа 1"},
            "status": {"needLogin": False, "error": None}}))
        sync = self.home / "router" / "sync.sh"
        sync.write_text(f"#!/bin/sh\necho started >> {self.tmp}/sync-ran\n")
        sync.chmod(0o755)
        install = self.home / "router" / "install.sh"
        install.write_text(f"#!/bin/sh\necho \"args: $@\" > {self.tmp}/install-args\nsleep 3\n")
        self.env = dict(os.environ, DNEVNIK_HOME=str(self.home), DNEVNIK_WWW=str(self.www), DNEVNIK_PRIV=str(self.www), DNEVNIK_TMP=str(self.run_dir),
                        DNEVNIK_CRON=str(self.cron), DNEVNIK_NO_SERVICES="1", DNEVNIK_DOMAIN="lan",
                        DNEVNIK_VERSION_URL=(self.tmp / "remote-version").as_uri())
        (self.tmp / "remote-version").write_text("0.12.0\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, method, args=None):
        r = subprocess.run([sys.executable, str(PLUGIN), "call", method], input=json.dumps(args or {}), capture_output=True,
                           text=True, env=self.env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)


class PluginTests(PluginCase):
    def test_list_methods_are_in_acl_and_in_the_page(self):
        r = subprocess.run([sys.executable, str(PLUGIN), "list"], capture_output=True, text=True, env=self.env)
        methods = set(json.loads(r.stdout))
        acl = json.loads(ACL.read_text())["luci-app-dnevnik"]
        allowed = set(acl["read"]["ubus"]["luci.dnevnik"]) | set(acl["write"]["ubus"]["luci.dnevnik"])
        self.assertEqual(methods, allowed)                                           # нет метода без права и права без метода
        js = VIEW.read_text()
        for m in methods:
            self.assertIn(f"method: '{m}'", js, m)                                   # страница вызывает только существующие методы
        self.assertEqual(set(acl["read"]["ubus"]["luci.dnevnik"]), {"status", "log", "check_update", "remote_info", "doctor", "devices_list"})   # чтение только безвредное: «проверить всё» ничего не меняет

    def test_family_password_methods(self):
        import auth
        data = self.home / "data"
        shutil.copy(ROOT / "auth.py", self.home / "auth.py")
        s = self.call("status")
        self.assertEqual((s["family"], s["familyHint"], s["familyChanged"]), (False, "", 0))
        for bad in ({"password": "   "}, {"password": "семейный-пароль-123", "hint": "тут семейный-пароль-123"}, {}):
            r = self.call("family_set", bad)
            self.assertFalse(r["ok"], bad)
            self.assertFalse(auth.enabled(data))
        r = self.call("family_set", {"password": "семейный-пароль-123", "hint": "имя кота"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(auth.check_password(data, "семейный-пароль-123"))
        s = self.call("status")
        self.assertEqual((s["family"], s["familyHint"]), (True, "имя кота"))
        self.assertGreater(s["familyChanged"], 0)
        self.assertNotIn("семейный-пароль-123", json.dumps(s, ensure_ascii=False) + json.dumps(r, ensure_ascii=False))
        cookie = "dnevnik_auth=" + auth.make_token(data)
        self.assertTrue(auth.authed(data, cookie))
        (self.run_dir / "dnevnik-auth-throttle").write_text(json.dumps({"fails": 9, "until": time.time() + 900}))
        r = self.call("family_set", {"password": "новый-пароль-4567", "hint": ""})              # забытый пароль: задаём новый, старый не нужен
        self.assertTrue(r["ok"], r)
        self.assertFalse(auth.authed(data, cookie))                                          # все устройства вышли
        self.assertEqual(auth.throttle_wait(self.run_dir / "dnevnik-auth-throttle"), 0)       # и блокировка перебора снята
        self.assertEqual(self.call("status")["familyHint"], "")
        self.assertTrue(self.call("family_off")["ok"])
        self.assertFalse(auth.enabled(data))
        self.assertTrue(self.call("family_off")["ok"])                                       # повторно безвредно

    def test_family_password_file_is_private(self):
        shutil.copy(ROOT / "auth.py", self.home / "auth.py")
        self.call("family_set", {"password": "семейный-пароль-123"})
        self.assertEqual(oct((self.home / "data" / "auth.json").stat().st_mode & 0o777), "0o600")

    def fake_tailscale(self, funnel=True):
        """Подставной tailscale в PATH плагина."""
        b = self.tmp / "bin"
        b.mkdir(exist_ok=True)
        text = "#!/bin/sh\ncase \"$1\" in\n  status) echo '{\"BackendState\": \"Running\"}' ;;\n  funnel) " + (
            "printf 'https://dnevnik.example.ts.net (Funnel on)\\n|-- / proxy http://127.0.0.1:8765\\n'" if funnel else "echo 'No serve config'") + " ;;\nesac\n"
        (b / "tailscale").write_text(text)
        (b / "tailscale").chmod(0o755)
        self.env["PATH"] = f"{b}:{self.env['PATH']}"

    def test_remote_info_without_tailscale(self):
        shutil.copy(ROOT / "router" / "doctor.py", self.home / "router" / "doctor.py")
        b = self.tmp / "emptybin"
        b.mkdir()
        self.env["PATH"] = f"{b}:/usr/bin:/bin"                                              # tailscale здесь нет
        r = self.call("remote_info")
        self.assertTrue(r["ok"])
        self.assertEqual((r["installed"], r["funnel"], r["url"], r["proxy"]), (False, False, "", ""))
        self.assertTrue(r["command"].endswith("router/remote.sh"))

    def test_remote_info_with_funnel_and_proxy(self):
        shutil.copy(ROOT / "router" / "doctor.py", self.home / "router" / "doctor.py")
        self.fake_tailscale(True)
        init = self.tmp / "ts-init"
        init.write_text("procd_append_param env HTTPS_PROXY=http://127.0.0.1:1 HTTP_PROXY=http://127.0.0.1:1\n")
        self.env["DNEVNIK_TS_INIT"] = str(init)
        r = self.call("remote_info")
        self.assertEqual((r["installed"], r["state"], r["funnel"], r["url"]), (True, "Running", True, "https://dnevnik.example.ts.net/"))
        self.assertEqual((r["proxy"], r["proxyAlive"]), ("127.0.0.1:1", False))

    def test_remote_info_without_the_helper_file_is_a_clear_answer(self):
        r = self.call("remote_info")                                                         # doctor.py в тестовой папке нет
        self.assertFalse(r["ok"])
        self.assertIn("Обновите программу", r["message"])
        self.assertFalse(self.call("doctor")["ok"])

    def test_doctor_method_returns_results_and_summary(self):
        shutil.copy(ROOT / "router" / "doctor.py", self.home / "router" / "doctor.py")
        self.fake_tailscale(False)
        self.env["DNEVNIK_PORT"] = "1"
        r = self.call("doctor")
        self.assertTrue(r["ok"], r)
        ids = {c["id"] for c in r["results"]}
        self.assertTrue({"system", "web", "cron", "internet", "family"} <= ids)
        self.assertIn(r["summary"]["verdict"], ("ok", "notes", "problems"))
        self.assertTrue(all(set(c) >= {"id", "status", "title", "detail", "fix"} for c in r["results"]))

    def copy_modules(self):
        for f in ("auth.py", "devices.py", "push.py", "notify.py"):
            shutil.copy(ROOT / f, self.home / f)
        self.env["DNEVNIK_SEEN"] = str(self.run_dir / "seen.json")

    def test_devices_list_collects_subscriptions_logins_and_last_seen(self):
        self.copy_modules()
        import auth
        import devices
        import push
        data = self.home / "data"
        auth.set_password(data, "семейный-пароль")
        push.save(data, {"subs": {"abc": {"endpoint": "https://web.push.apple.com/secret-token", "p256dh": "x", "auth": "y", "name": "Телефон Ани", "label": "iPhone · Safari",
                                          "created": 1_800_000_000, "prefs": {"types": {"mark": True, "homework": False}, "quiet": {"on": True}}, "pending": [{"id": 1}], "fail": 2}}})
        devices.record_login(data, False, "curl", "127.0.0.1", "a" * 12)
        devices.record_login(data, True, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Version/18 Mobile/15E Safari/604", "192.168.1.5", "b" * 12)
        devices.touch(self.run_dir / "seen.json", "b" * 12, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Version/18 Mobile/15E Safari/604", "192.168.1.5")
        r = self.call("devices_list")
        self.assertTrue(r["ok"])
        self.assertTrue(r["family"])
        self.assertEqual(r["failed24h"], 1)
        s = r["subs"][0]
        self.assertEqual((s["name"], s["label"], s["service"], s["pending"], s["fail"], s["types"], s["quiet"]), ("Телефон Ани", "iPhone · Safari", "Apple", 1, 2, ["mark"], True))
        self.assertEqual([e["ok"] for e in r["logins"]], [True, False])
        self.assertEqual((r["seen"][0]["dev"], r["seen"][0]["where"]), ("iPhone · Safari", "home"))
        self.assertNotIn("secret-token", json.dumps(r))                                      # адрес устройства с токеном в ответ не попадает

    def test_devices_list_on_a_fresh_router_is_empty_not_an_error(self):
        self.copy_modules()
        r = self.call("devices_list")
        self.assertEqual((r["ok"], r["subs"], r["logins"], r["seen"], r["failed24h"], r["family"]), (True, [], [], [], 0, False))

    def test_device_remove_and_logins_clear(self):
        self.copy_modules()
        import devices
        import push
        data = self.home / "data"
        push.save(data, {"subs": {"abc": {"endpoint": "https://fcm.googleapis.com/x", "prefs": {"types": {}}}}})
        devices.record_login(data, True, "x", "127.0.0.1", "a" * 12)
        devices.touch(self.run_dir / "seen.json", "a" * 12, "x", "127.0.0.1")
        self.assertFalse(self.call("device_remove", {"id": "нет"})["ok"])
        self.assertTrue(self.call("device_remove", {"id": "abc"})["ok"])
        self.assertEqual(push.load(data)["subs"], {})
        self.assertFalse(self.call("device_remove", {"id": "abc"})["ok"])                    # повторно: честно «уже нет»
        self.assertTrue(self.call("logins_clear")["ok"])
        self.assertEqual((devices.recent_logins(data), devices.seen_list(self.run_dir / "seen.json")), ([], []))
        self.assertTrue(self.call("logins_clear")["ok"])                                     # повторно безвредно

    def test_password_methods_are_write_only(self):
        acl = json.loads(ACL.read_text())["luci-app-dnevnik"]
        self.assertTrue({"family_set", "family_off"} <= set(acl["write"]["ubus"]["luci.dnevnik"]))
        self.assertFalse({"family_set", "family_off"} & set(acl["read"]["ubus"]["luci.dnevnik"]))

    def test_menu_points_to_the_page_file(self):
        menu = json.loads(MENU.read_text())["admin/services/dnevnik"]
        self.assertEqual(menu["action"]["path"], "dnevnik/status")
        self.assertTrue((ROOT / "luci" / "htdocs" / "view" / menu["action"]["path"]).with_suffix(".js").is_file())
        self.assertEqual(menu["depends"]["acl"], ["luci-app-dnevnik"])

    def test_unknown_method_is_refused(self):
        r = subprocess.run([sys.executable, str(PLUGIN), "call", "rm_rf"], input="{}", capture_output=True, text=True, env=self.env)
        self.assertNotEqual(r.returncode, 0)

    def test_status(self):
        s = self.call("status")
        self.assertEqual((s["version"], s["student"], s["school"], s["days"]), ("0.12.0", "Аня", "Школа 1", 3))
        self.assertEqual((s["every"], s["from"], s["to"]), (20, 6, 23))
        self.assertEqual(s["updatedAt"], "2026-11-12T01:29:00")
        self.assertFalse(s["needLogin"] or s["syncing"] or s["updating"] or s["canRestore"])
        self.assertEqual(s["url"], "http://dnevnik.lan")
        self.assertIsInstance(s["nextIn"], int)

    def test_status_without_page_data_does_not_crash(self):
        (self.www / "data.json").unlink()
        s = self.call("status")
        self.assertEqual((s["days"], s["student"], s["updatedAt"]), (0, "", None))
        (self.www / "data.json").write_text("{не json")
        self.assertEqual(self.call("status")["days"], 0)

    def test_status_shows_need_login_and_running_sync(self):
        page = json.loads((self.www / "data.json").read_text())
        page["status"] = {"needLogin": True, "error": "нужен вход"}
        (self.www / "data.json").write_text(json.dumps(page))
        (self.run_dir / "dnevnik-sync.lock").write_text(str(os.getpid()))            # процесс жив
        s = self.call("status")
        self.assertTrue(s["needLogin"] and s["syncing"])
        self.assertEqual(s["error"], "нужен вход")
        (self.run_dir / "dnevnik-sync.lock").write_text("999999999")                 # такого процесса нет
        self.assertFalse(self.call("status")["syncing"])

    def test_next_run_calculation(self):
        p = load_plugin()
        at = lambda h, m, s=0: datetime(2026, 11, 12, h, m, s)
        self.assertEqual(p.next_run_in(20, 6, 23, at(10, 5)), 15 * 60)
        self.assertEqual(p.next_run_in(20, 6, 23, at(10, 20)), 20 * 60)              # ровно в запуск: ждём следующий
        self.assertEqual(p.next_run_in(20, 6, 23, at(1, 29)), 4 * 3600 + 31 * 60)    # ночью: до 6:00
        self.assertEqual(p.next_run_in(20, 6, 23, at(22, 50)), 7 * 3600 + 10 * 60)   # после последнего: до 6:00 завтра
        self.assertEqual(p.next_run_in(60, 6, 23, at(10, 5)), 55 * 60)
        self.assertEqual(p.next_run_in(5, 0, 24, at(23, 58)), 120)

    def test_schedule_is_read_from_cron_even_if_unusual(self):
        p = load_plugin()
        for line, expect in (("*/10 7-21 * * * /root/dnevnik/router/sync.sh", (10, 7, 22)), ("0 6-22 * * * /root/dnevnik/router/sync.sh", (60, 6, 23)),
                             ("17 3 * * 1 /root/dnevnik/router/sync.sh", (20, 6, 23)), ("# */5 6-22 * * * /root/dnevnik/router/sync.sh", (20, 6, 23))):
            self.cron.write_text("0 3 * * * /usr/bin/other\n" + line + "\n")
            p.CRON = str(self.cron)
            self.assertEqual(p.read_schedule(), expect, line)
        self.cron.unlink()
        self.assertEqual(p.read_schedule(), (20, 6, 23))

    def test_set_config_changes_only_our_cron_line_and_the_name(self):
        r = self.call("set_config", {"every": 10, "from": 7, "to": 22, "name": "Катя"})
        self.assertTrue(r["ok"], r)
        lines = self.cron.read_text().splitlines()
        self.assertEqual(lines, ["0 3 * * * /usr/bin/other", "*/10 7-21 * * * /root/dnevnik/router/sync.sh"])
        self.assertEqual(json.loads((self.home / "config.json").read_text()), {"name": "Катя", "other": 5})   # чужие настройки целы
        s = self.call("status")
        self.assertEqual((s["every"], s["from"], s["to"], s["name"]), (10, 7, 22, "Катя"))
        self.assertEqual(self.call("set_config", {"every": 60, "from": 0, "to": 24, "name": ""})["ok"], True)
        self.assertIn("0 0-23 * * * /root/dnevnik/router/sync.sh", self.cron.read_text())
        self.assertEqual(self.cron.read_text().count("sync.sh"), 1)                   # строка одна, не дублируется
        self.assertEqual(oct(self.cron.stat().st_mode & 0o777), "0o600")

    def test_set_config_refuses_wrong_values_and_changes_nothing(self):
        before = (self.cron.read_text(), (self.home / "config.json").read_text())
        for bad in ({"every": 1, "from": 6, "to": 23}, {"every": 20, "from": 23, "to": 6}, {"every": 20, "from": 6, "to": 6},
                    {"every": 20, "from": -1, "to": 5}, {"every": 20, "from": 6, "to": 25}, {"every": "x", "from": 6, "to": 23},
                    {"every": 20, "from": 6}, {"every": "20; rm -rf /", "from": 6, "to": 23}, {}):
            self.assertFalse(self.call("set_config", dict(bad, name="x"))["ok"], bad)
        self.assertEqual((self.cron.read_text(), (self.home / "config.json").read_text()), before)

    def test_name_is_cleaned(self):
        self.call("set_config", {"every": 20, "from": 6, "to": 23, "name": '  <b>Ка"тя</b>\n\x07' + "я" * 80})
        name = json.loads((self.home / "config.json").read_text())["name"]
        self.assertTrue(name.startswith("bКатя/b"), name)
        self.assertNotRegex(name, r'[<>"&\x00-\x1f]')
        self.assertLessEqual(len(name), 40)

    def test_refresh_starts_sync_and_not_more_often_than_every_two_minutes(self):
        r = self.call("refresh")
        self.assertTrue(r["started"], r)
        for _ in range(30):
            if (self.tmp / "sync-ran").exists():
                break
            time.sleep(0.1)
        self.assertTrue((self.tmp / "sync-ran").exists())
        r2 = self.call("refresh")
        self.assertTrue(r2["ok"] and not r2["started"])
        self.assertEqual((self.tmp / "sync-ran").read_text().count("started"), 1)

    def test_check_update(self):
        (self.tmp / "remote-version").write_text("0.12.1\n")
        r = self.call("check_update")
        self.assertEqual((r["ok"], r["newer"], r["latest"], r["current"]), (True, True, "0.12.1", "0.12.0"))
        (self.tmp / "remote-version").write_text("0.12.0\n")
        self.assertFalse(self.call("check_update")["newer"])
        (self.tmp / "remote-version").write_text("0.9.9\n")                              # у нас новее: не предлагаем «обновиться» назад
        self.assertFalse(self.call("check_update")["newer"])
        (self.tmp / "remote-version").write_text("0.13.0\n")
        self.assertTrue(self.call("check_update")["newer"])
        (self.tmp / "remote-version").write_text("<html>вход</html>")
        self.assertFalse(self.call("check_update")["ok"])
        (self.tmp / "remote-version").unlink()
        self.assertFalse(self.call("check_update")["ok"])

    def test_update_runs_installer_with_luci_flag_and_not_twice(self):
        r = self.call("update")
        self.assertTrue(r["ok"], r)
        for _ in range(30):
            if (self.tmp / "install-args").exists():
                break
            time.sleep(0.1)
        self.assertEqual((self.tmp / "install-args").read_text().strip(), "args: --luci")   # страница LuCI при обновлении сохраняется
        self.assertTrue(self.call("status")["updating"])
        self.assertFalse(self.call("update")["ok"])                                        # второй запуск не принимается
        self.assertIn("обновление программы", self.call("log", {"which": "update"})["text"])

    # --- итог обновления программы и адрес по DHCP ---
    def write_update_log(self, text, pid=None):
        (self.run_dir / "dnevnik-update.log").write_text(text)
        if pid is not None:
            (self.run_dir / "dnevnik-update.pid").write_text(str(pid))

    def test_update_result_is_empty_when_nothing_was_started(self):
        self.assertEqual(self.call("status")["updateResult"], {})

    def test_update_result_success_failure_and_cut_off(self):
        self.write_update_log("--- 2026-11-14 21:55:13 обновление программы\n==> Проверяю Python…\n==> Готово: версия 0.21.2.\n    Страница: http://dnevnik.lan\n")
        r = self.call("status")["updateResult"]
        self.assertEqual((r["state"], r["version"]), ("ok", "0.21.2"))
        self.assertLess(r["age"], 60)
        self.write_update_log("--- x\n==> Скачиваю…\nОШИБКА: не скачалось (Rombaldo/dnevnik-OpenWRT, main).\n")
        r = self.call("status")["updateResult"]
        self.assertEqual(r["state"], "failed")
        self.assertIn("не скачалось", r["message"])
        self.write_update_log("--- x\n==> Скачиваю…\n")                                   # оборвалось (перезагрузка, пропало питание)
        r = self.call("status")["updateResult"]
        self.assertEqual(r["state"], "failed")
        self.assertIn("оборвалось", r["message"])

    def test_update_result_is_empty_while_the_installer_is_running(self):
        self.write_update_log("--- x\n==> Готово: версия 9.9.9.\n", pid=os.getpid())      # процесс жив: итога ещё нет
        s = self.call("status")
        self.assertTrue(s["updating"])
        self.assertEqual(s["updateResult"], {})

    def test_update_result_text_from_the_log_is_bounded(self):
        self.write_update_log("ОШИБКА: " + "я" * 5000 + "\n")
        self.assertLessEqual(len(self.call("status")["updateResult"]["message"]), 240)

    def test_address_given_by_dhcp_is_shown_through_ubus(self):
        """Раньше адрес по цифрам брался только из настроек и на роутере с lan по DHCP строка в LuCI была пустой."""
        bin_dir = self.tmp / "fakebin"
        bin_dir.mkdir()
        ubus = bin_dir / "ubus"
        ubus.write_text("#!/bin/sh\necho '{\"up\": true, \"ipv4-address\": [{\"address\": \"192.168.1.77\", \"mask\": 24}]}'\n")
        ubus.chmod(0o755)
        uci = bin_dir / "uci"
        uci.write_text("#!/bin/sh\nexit 1\n")                                              # в настройках адреса нет
        uci.chmod(0o755)
        self.env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        self.assertEqual(self.call("status")["urlIp"], "http://192.168.1.77:8765")

    def test_address_falls_back_to_the_setting_and_survives_garbage(self):
        bin_dir = self.tmp / "fakebin"
        bin_dir.mkdir()
        ubus = bin_dir / "ubus"
        ubus.write_text("#!/bin/sh\necho 'not json'\n")
        ubus.chmod(0o755)
        uci = bin_dir / "uci"
        uci.write_text("#!/bin/sh\n[ \"$3\" = network.lan.ipaddr ] && echo 192.168.5.1/24\nexit 0\n")
        uci.chmod(0o755)
        self.env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
        self.assertEqual(self.call("status")["urlIp"], "http://192.168.5.1:8765")

    # --- список изменений, запасной прокси, ход обновления ---
    CHANGELOG = ("# История версий\n\nФормат: номер версии.\n\n"
                 "## 0.12.3 — 2026-11-16\n- **Третье**: самое новое, с `кодом` и **жирным**.\n- Ещё одно.\n\n"
                 "## 0.12.2 — 2026-11-15\nОписание без пункта.\n- Второе изменение.\n\n"
                 "## 0.12.1 — 2026-11-14\n- Первое изменение.\n\n"
                 "## 0.12.0 — 2026-11-13\n- Уже установленная версия.\n\n## 0.11.0 — 2026-11-01\n- Старая.\n")

    def set_changelog(self, text=None):
        f = self.tmp / "remote-changelog"
        f.write_text(self.CHANGELOG if text is None else text)
        self.env["DNEVNIK_CHANGELOG_URL"] = f.as_uri()

    def test_check_update_lists_what_is_new_between_the_versions(self):
        self.set_changelog()
        (self.tmp / "remote-version").write_text("0.12.3\n")
        r = self.call("check_update")
        self.assertTrue(r["newer"], r)
        self.assertEqual([n["version"] for n in r["notes"]], ["0.12.3", "0.12.2", "0.12.1"])          # без 0.12.0 и старше
        self.assertEqual(r["notes"][0]["date"], "2026-11-16")
        self.assertEqual(r["notes"][0]["items"], ["Третье: самое новое, с кодом и жирным.", "Ещё одно."])  # разметка снята
        self.assertEqual(r["notes"][1]["items"], ["Второе изменение."])

    def test_notes_stop_at_the_latest_version_not_beyond(self):
        self.set_changelog()
        (self.tmp / "remote-version").write_text("0.12.1\n")
        self.assertEqual([n["version"] for n in self.call("check_update")["notes"]], ["0.12.1"])

    def test_no_notes_when_up_to_date_or_changelog_is_missing_or_broken(self):
        self.set_changelog()
        self.assertEqual(self.call("check_update")["notes"], [])                                   # 0.12.0 уже стоит
        (self.tmp / "remote-version").write_text("0.12.1\n")
        self.env["DNEVNIK_CHANGELOG_URL"] = (self.tmp / "no-such-file").as_uri()
        r = self.call("check_update")
        self.assertTrue(r["ok"] and r["newer"], r)                                                 # без списка обновиться можно
        self.assertEqual(r["notes"], [])
        self.set_changelog("мусор без заголовков\n- пункт\n")
        self.assertEqual(self.call("check_update")["notes"], [])

    def test_notes_are_bounded(self):
        many = "".join(f"## 0.13.{i} — d\n- " + "я" * 3000 + "\n" for i in range(1, 30)) + "## 0.12.9 — d\n" + "".join(f"- пункт {i}\n" for i in range(50))
        self.set_changelog(many)
        (self.tmp / "remote-version").write_text("0.13.29\n")
        notes = self.call("check_update")["notes"]
        self.assertLessEqual(len(notes), 6)
        self.assertTrue(all(len(i) <= 420 for n in notes for i in n["items"]))
        self.set_changelog("## 0.12.9 — d\n" + "".join(f"- пункт {i}\n" for i in range(50)))
        (self.tmp / "remote-version").write_text("0.12.9\n")
        self.assertLessEqual(len(self.call("check_update")["notes"][0]["items"]), 12)

    def serve_as_proxy(self, files):
        """Маленький «прокси»: на любой запрос отвечает по пути. Прямое обращение к недоступному имени вне прокси не удастся."""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = "/" + self.path.split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
                body = files.get(path)
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                self.wfile.write((body or "").encode("utf-8"))
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_check_update_goes_through_the_local_proxy_when_github_is_closed(self):
        port = self.serve_as_proxy({"/VERSION": "0.12.5\n", "/CHANGELOG.md": "## 0.12.5 — 2026-11-20\n- Через прокси.\n"})
        self.env.update(DNEVNIK_VERSION_URL="http://blocked.invalid/VERSION", DNEVNIK_CHANGELOG_URL="http://blocked.invalid/CHANGELOG.md",
                        DNEVNIK_PROXY=f"127.0.0.1:{port}")
        r = self.call("check_update")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["latest"], "0.12.5")
        self.assertEqual(r["notes"][0]["items"], ["Через прокси."])

    def test_check_update_without_a_proxy_reports_no_connection(self):
        self.env.update(DNEVNIK_VERSION_URL="http://blocked.invalid/VERSION", DNEVNIK_PROXY="127.0.0.1:9")    # на этом порту никто не слушает
        r = self.call("check_update")
        self.assertFalse(r["ok"])
        self.assertIn("GitHub", r["message"])

    def test_progress_follows_the_installer_log(self):
        self.write_update_log("--- 2026-11-14 21:55:13 обновление программы\n", pid=os.getpid())
        p = self.call("status")["updateProgress"]
        self.assertEqual(p["percent"], 3)
        steps = [("==> Проверяю Python…\n", 8), ("==> Ставлю пакеты: python3-codecs\n", 20), ("==> Скачиваю Rombaldo/dnevnik-OpenWRT (main) с GitHub…\n", 35),
                 ("==> Устанавливаю версию 0.21.3 (была: 0.21.2)…\n", 60), ("==> Включаю службу и расписание…\n", 72),
                 ("==> Ставлю страницу «Службы → Дневник» в LuCI…\n", 82), ("==> Готово: версия 0.21.3.\n", 92), ("==> Проверяю, что всё работает…\n", 96)]
        text = "--- x\n"
        last = 3
        for line, want in steps:
            text += line
            self.write_update_log(text, pid=os.getpid())
            p = self.call("status")["updateProgress"]
            self.assertEqual(p["percent"], want, line)
            self.assertGreaterEqual(p["percent"], last)                                          # назад полоса не едет
            self.assertTrue(p["text"])
            last = p["percent"]

    def test_progress_never_reaches_100_while_running_and_mentions_the_proxy(self):
        self.write_update_log("--- x\n==> Проверяю Python…\n!!  Прямое подключение не получилось. На роутере работает прокси (127.0.0.1:7890): повторяю через него.\n==> Скачиваю x…\n", pid=os.getpid())
        p = self.call("status")["updateProgress"]
        self.assertLess(p["percent"], 100)
        self.assertIn("через прокси", p["text"])

    def test_progress_is_empty_when_no_update_is_running(self):
        self.write_update_log("--- x\n==> Скачиваю…\n")                                       # процесса нет
        self.assertEqual(self.call("status")["updateProgress"], {})
        self.assertEqual(self.call("status")["updateResult"]["state"], "failed")

    def test_log_returns_the_tail(self):
        (self.run_dir / "dnevnik-sync.log").write_text("\n".join(f"строка {i}" for i in range(100)))
        text = self.call("log", {"which": "sync"})["text"]
        self.assertIn("строка 99", text)
        self.assertNotIn("строка 5\n", text + "\n")
        self.assertLessEqual(len(text.splitlines()), 40)
        self.assertEqual(self.call("log", {"which": "update"})["text"], "")

    def test_logout_needs_the_code_and_the_pause_and_can_be_restored(self):
        (self.home / "data" / "session.json").write_text('{"cookies": []}')
        (self.home / "data" / "ids.json").write_text("{}")
        self.assertFalse(self.call("logout_confirm", {"token": "выдуманный"})["ok"])       # без первого шага не выйти
        self.assertTrue((self.home / "data" / "session.json").exists())
        token = self.call("logout_begin")["token"]
        r = self.call("logout_confirm", {"token": token})                                  # слишком быстро
        self.assertFalse(r["ok"])
        self.assertTrue((self.home / "data" / "session.json").exists())
        token = self.call("logout_begin")["token"]
        time.sleep(2.3)
        self.assertFalse(self.call("logout_confirm", {"token": "чужой"})["ok"])
        self.assertTrue((self.home / "data" / "session.json").exists())
        token = self.call("logout_begin")["token"]
        time.sleep(2.3)
        r = self.call("logout_confirm", {"token": token})
        self.assertTrue(r["ok"], r)
        self.assertFalse((self.home / "data" / "session.json").exists())                   # вход спрятан
        self.assertTrue((self.home / "data" / "backup" / "session.json").exists())         # но не стёрт
        self.assertTrue(self.call("status")["canRestore"])
        self.assertTrue(self.call("logout_restore")["ok"])
        self.assertTrue((self.home / "data" / "session.json").exists())
        self.assertFalse(self.call("status")["canRestore"])

    def test_page_data_is_rebuilt_after_logout(self):
        (self.home / "data" / "session.json").write_text('{"cookies": []}')
        self.call("logout_begin")
        token = self.call("logout_begin")["token"]
        time.sleep(2.3)
        self.call("logout_confirm", {"token": token})
        page = json.loads((self.www / "data.json").read_text())
        self.assertTrue(page["status"]["needLogin"])
        self.assertEqual(page["days"], [])                                                   # прежнего профиля на странице больше нет


class InstallFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dnevnik-luci-inst-"))
        self.root = self.tmp / "rootfs"
        self.home = self.tmp / "home"
        shutil.copytree(ROOT / "luci", self.home / "luci")
        self.env = dict(os.environ, DNEVNIK_HOME=str(self.home), DNEVNIK_ROOTFS=str(self.root))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def luci(self, cmd):
        return subprocess.run(["sh", str(ROOT / "router" / "luci.sh"), cmd], capture_output=True, text=True, env=self.env, timeout=30)

    def files(self):
        return {
            "plugin": self.root / "usr/libexec/rpcd/luci.dnevnik", "menu": self.root / "usr/share/luci/menu.d/luci-app-dnevnik.json",
            "acl": self.root / "usr/share/rpcd/acl.d/luci-app-dnevnik.json", "view": self.root / "www/luci-static/resources/view/dnevnik/status.js",
        }

    def test_install_puts_exactly_four_files_with_right_permissions_and_remove_cleans_up(self):
        r = self.luci("install")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        f = self.files()
        self.assertTrue(all(p.is_file() for p in f.values()), f)
        self.assertEqual(oct(f["plugin"].stat().st_mode & 0o777), "0o755")                  # плагин запускает rpcd
        for k in ("menu", "acl", "view"):
            self.assertEqual(oct(f[k].stat().st_mode & 0o777), "0o644")
        self.assertEqual(len([p for p in self.root.rglob("*") if p.is_file()]), 4)           # больше ничего не трогаем
        self.assertEqual(self.luci("status").stdout.strip(), "installed")
        self.assertEqual(self.luci("install").returncode, 0)                                 # повторная установка безвредна
        self.assertEqual(self.luci("remove").returncode, 0)
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()], [])
        self.assertEqual(self.luci("status").stdout.strip(), "absent")
        self.assertEqual(self.luci("remove").returncode, 0)                                  # и удаление повторно

    def test_install_refuses_when_files_are_missing_instead_of_half_installing(self):
        (self.home / "luci" / "htdocs" / "view" / "dnevnik" / "status.js").unlink()
        r = self.luci("install")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()] if self.root.exists() else [], [])

    def test_unknown_command(self):
        self.assertEqual(self.luci("что-то").returncode, 2)

    def test_installer_syntax_and_flags(self):
        for script in ("install.sh", "luci.sh", "uninstall.sh"):
            self.assertEqual(subprocess.run(["sh", "-n", str(ROOT / "router" / script)]).returncode, 0, script)
        r = subprocess.run(["sh", str(ROOT / "router" / "install.sh"), "--bogus"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)                                                    # неизвестный ключ: понятная ошибка, ничего не делаем
        self.assertIn("--luci", r.stderr)
        text = (ROOT / "router" / "install.sh").read_text()
        self.assertIn("--no-luci", text)
        self.assertIn('LUCI=auto', text)

    def test_uninstall_also_removes_the_luci_page(self):
        self.assertIn("luci.sh remove", (ROOT / "router" / "uninstall.sh").read_text())

    def test_readme_has_both_install_commands(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn("router/install.sh | sh\n", readme)
        self.assertIn("router/install.sh | sh -s -- --luci", readme)
        self.assertIn("Службы → Дневник", readme)


STUB = """
window.calls = []; window.pollFns = [];
window.E = function(tag, attrs, children) {
  if (attrs === undefined || Array.isArray(attrs) || typeof attrs === 'string' || attrs instanceof Node) { children = attrs; attrs = null; }
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (typeof v === 'function') n.addEventListener(k, v);
    else if (k === 'class') n.className = v; else n.setAttribute(k, v);
  }
  const add = (c) => { if (c == null) return; if (Array.isArray(c)) c.forEach(add); else n.appendChild(c instanceof Node ? c : document.createTextNode(String(c))); };
  add(children); return n;
};
window.view = { extend: (o) => o };
window.rpc = { declare: (spec) => (...args) => { window.calls.push([spec.method, args]); return Promise.resolve(window.answer(spec.method, args)); } };
window.poll = { add: (fn, sec) => window.pollFns.push(fn) };
window.ui = {
  showModal: (title, body) => { const old = document.getElementById('modal'); if (old) old.remove(); const m = document.createElement('div'); m.id = 'modal'; m.innerHTML = '<h4></h4>'; m.firstChild.textContent = title;
    [].concat(body).forEach((b) => m.appendChild(b)); document.body.appendChild(m); },
  hideModal: () => { const m = document.getElementById('modal'); if (m) m.remove(); },
  addNotification: () => {}
};
window.L = { url: (p) => '/cgi-bin/luci/' + p, resource: (p) => '/luci-static/resources/' + p };
window.fetched = [];
window.fetch = (u, o) => { window.fetched.push([u, o]); return Promise.resolve({}); };
window.mount = async (code) => {
  const cls = new Function('view', 'rpc', 'ui', 'poll', 'E', code)(window.view, window.rpc, window.ui, window.poll, window.E);
  window.cls = cls;
  const data = await cls.load();
  document.body.innerHTML = ''; document.body.appendChild(cls.render(data));
};
undefined;
"""

STATUS = {"version": "0.12.0", "updatedAt": "2026-11-12T01:29:00", "days": 57, "needLogin": False, "error": None, "student": "Аня",
          "school": "Школа 1", "syncing": False, "updating": False, "canRestore": False, "every": 20, "from": 6, "to": 23, "name": "",
          "nextIn": 840, "now": 0, "url": "http://dnevnik.lan", "urlIp": "http://192.168.10.1:8765"}


@unittest.skipIf(sync_playwright is None, "Playwright не установлен")
class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.code = VIEW.read_text()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def open(self, status=None, answers=None):
        page = self.browser.new_page(viewport={"width": 900, "height": 900})
        self.errors = []
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.set_content("<body></body>")
        page.evaluate(STUB)
        page.evaluate("""([st, answers]) => { window.mockStatus = st; window.extra = answers || {};
            window.answer = (m, a) => (m in window.extra ? window.extra[m] : m === 'status' ? window.mockStatus : m === 'log' ? {text: 'строка журнала'} : {ok: true, message: 'ок'}); }""",
                      [dict(STATUS, **(status or {})), answers])
        page.evaluate("code => window.mount(code)", self.code)
        # настоящий переход и перезагрузку подменяем записью, иначе страница теста исчезла бы; интервалы короткие
        page.evaluate("() => { const c = window.cls; c.goTo = (u) => { window.navigated = u; }; c.reloadPage = () => { window.reloaded = true; }; c.logoutMs = 40; c.fastMs = 40; }")
        return page

    def names(self, page):
        return page.evaluate("window.calls.map(c => c[0])")

    def test_shows_state(self):
        page = self.open()
        text = page.inner_text(".dn")
        for needle in ("Работает", "0.12.0", "Аня (Школа 1)", "выполнен", "12 ноября, 01:29", "57", "через 14 мин"):
            self.assertIn(needle, text)
        self.assertEqual(page.get_attribute(".dn-v a", "href"), "http://dnevnik.lan")
        self.assertEqual(page.eval_on_selector_all(".dn-form select", "e => e.map(s => s.value)"), ["20", "6", "23"])
        self.assertIn("строка журнала", page.inner_text(".dn-log"))
        self.assertEqual(page.is_visible("text=Вернуть прежний вход"), False)
        self.assertEqual(self.errors, [])

    def test_states_need_login_syncing_and_restore(self):
        page = self.open({"needLogin": True, "canRestore": True})
        self.assertIn("Нужен вход", page.inner_text(".dn-pill"))
        self.assertTrue(page.is_disabled("text=Выйти из аккаунта"))
        self.assertTrue(page.is_visible("text=Вернуть прежний вход"))
        page.click("text=Вернуть прежний вход")
        self.assertIn("logout_restore", self.names(page))
        page = self.open({"syncing": True})
        self.assertIn("Обновляются данные", page.inner_text(".dn-pill"))
        self.assertTrue(page.is_disabled("text=Обновить сейчас"))
        page = self.open({"error": "сбой"})
        self.assertIn("Не удалось обновить", page.inner_text(".dn-pill"))
        self.assertEqual(self.errors, [])

    def test_server_text_is_not_executed(self):
        page = self.open({"student": '<img src=x onerror="window.pwned=1">', "school": "<b>x</b>", "name": '"><script>window.pwned=1</script>'})
        self.assertIsNone(page.query_selector(".dn img"))
        self.assertIsNone(page.query_selector(".dn b"))
        self.assertIn("<img", page.inner_text(".dn"))
        self.assertIsNone(page.evaluate("window.pwned"))
        self.assertEqual(self.errors, [])

    def test_refresh_button(self):
        page = self.open(answers={"refresh": {"ok": True, "started": True, "message": "Обновление запущено."}})
        page.click("text=Обновить сейчас")
        page.wait_for_selector(".dn-note.ok")
        self.assertIn("Обновление запущено", page.inner_text(".dn-note.ok"))
        page2 = self.open(answers={"refresh": {"ok": True, "started": False, "message": "Обновляли только что."}})
        page2.click("text=Обновить сейчас")
        page2.wait_for_selector(".dn-note.warn")

    def test_check_and_install_update(self):
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True}})
        self.assertFalse(page.is_visible("text=Обновить до 0.12.1"))
        page.click("text=Проверить новую версию")
        page.wait_for_selector("text=Обновить до 0.12.1")
        self.assertIn("Есть новая версия 0.12.1", page.inner_text(".dn"))
        page.click("text=Обновить до 0.12.1")
        self.assertNotIn("update", self.names(page))                                     # без подтверждения ничего не запускается
        page.click("#modal >> text=Отмена")
        self.assertNotIn("update", self.names(page))
        page.click("text=Обновить до 0.12.1"); page.click("#modal button.cbi-button-action")
        page.wait_for_function("window.calls.some(c => c[0] === 'update')")
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.0", "newer": False}})
        page.click("text=Проверить новую версию")
        page.wait_for_selector("text=последняя версия")
        self.assertFalse(page.is_visible("text=Обновить до"))
        page = self.open(answers={"check_update": {"ok": False, "message": "Нет связи с GitHub."}})
        page.click("text=Проверить новую версию")
        page.wait_for_selector("text=Нет связи с GitHub.")

    # --- итог обновления программы на странице (раньше «запущено» висело вечно) ---
    def poll_all(self, page):
        page.evaluate("Promise.all(window.pollFns.map(f => f()))")

    def test_update_finished_replaces_the_started_message_and_hides_the_stale_button(self):
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True}})
        page.click("text=Проверить новую версию"); page.wait_for_selector("text=Обновить до 0.12.1")
        page.click("text=Обновить до 0.12.1"); page.click("#modal button.cbi-button-action")
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: true})")
        self.poll_all(page)
        self.assertIn("Обновляется программа", page.inner_text(".dn-pill"))
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: false, version: '0.12.1', updateResult: {state: 'ok', version: '0.12.1', age: 3}})")
        self.poll_all(page)
        page.wait_for_function("window.navigated === '/cgi-bin/luci/admin/logout'")          # после установки страница сама выходит из LuCI
        self.assertFalse(page.is_visible("text=Обновить до"))
        self.assertNotIn("запущено", page.inner_text(".dn"))
        self.assertIn("Выходим из системы", page.inner_text(".dn"))
        self.assertEqual(self.errors, [])

    def test_update_that_finishes_before_the_first_poll_is_still_reported(self):
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True}})
        page.click("text=Проверить новую версию"); page.wait_for_selector("text=Обновить до 0.12.1")
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: false, version: '0.12.1', updateResult: {state: 'ok', version: '0.12.1', age: 1}})")
        page.click("text=Обновить до 0.12.1"); page.click("#modal button.cbi-button-action")
        page.wait_for_function("window.navigated === '/cgi-bin/luci/admin/logout'")

    def test_failed_update_is_shown_with_the_reason_and_opens_the_update_log(self):
        page = self.open({"updating": True})
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: false, updateResult: {state: 'failed', message: 'ОШИБКА: не скачалось', age: 2}})")
        self.poll_all(page)
        page.wait_for_selector(".dn-note.bad")
        self.assertIn("Обновление не удалось: ОШИБКА: не скачалось", page.inner_text(".dn-note.bad"))
        self.assertIn("update", page.evaluate("[...document.querySelectorAll('select')].map(s => s.value)"))

    def test_recent_result_is_shown_on_opening_but_an_old_one_is_not(self):
        page = self.open({"updateResult": {"state": "ok", "version": "0.12.0", "age": 90}})
        self.assertIn("Обновление завершено: установлена версия 0.12.0.", page.inner_text(".dn"))
        page = self.open({"updateResult": {"state": "ok", "version": "0.12.0", "age": 7200}})
        self.assertNotIn("Обновление завершено", page.inner_text(".dn"))
        page = self.open({"updateResult": {"state": "failed", "message": "ОШИБКА: x", "age": 10}})
        self.assertIn("Обновление не удалось", page.inner_text(".dn"))

    def test_no_result_while_it_is_still_running(self):
        page = self.open({"updating": True, "updateResult": {}})
        self.assertNotIn("Обновление завершено", page.inner_text(".dn"))
        self.assertNotIn("Обновление не удалось", page.inner_text(".dn"))

    def test_result_text_from_the_router_is_not_executed(self):
        page = self.open({"updateResult": {"state": "failed", "message": '<img src=x onerror="window.pwned=1">', "age": 5}})
        self.assertIsNone(page.query_selector(".dn img"))
        self.assertIsNone(page.evaluate("window.pwned"))
        self.assertIn("<img", page.inner_text(".dn"))

    # --- полоса хода, «Что нового», выход после обновления ---
    def test_progress_bar_shows_percent_and_stage_only_while_updating(self):
        page = self.open({"updating": True, "updateProgress": {"percent": 35, "text": "Скачиваю новую версию с GitHub…"}})
        self.assertEqual(page.evaluate("document.querySelector('.dn-fill').style.width"), "35%")
        self.assertIn("35% · Скачиваю новую версию", page.inner_text(".dn"))
        page = self.open()
        self.assertFalse(page.is_visible(".dn-bar"))

    def test_bar_moves_by_itself_while_the_update_runs(self):
        page = self.open({"updating": True, "updateProgress": {"percent": 20, "text": "Ставлю нужные пакеты…"}})
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updateProgress: {percent: 60, text: 'Раскладываю файлы…'}})")
        page.wait_for_function("document.querySelector('.dn-fill').style.width === '60%'")             # сам, без ручного опроса
        self.assertIn("Раскладываю файлы", page.inner_text(".dn"))

    def test_bar_is_clamped_and_survives_garbage(self):
        page = self.open({"updating": True, "updateProgress": {"percent": 500, "text": "x"}})
        self.assertEqual(page.evaluate("document.querySelector('.dn-fill').style.width"), "99%")
        page = self.open({"updating": True, "updateProgress": {}})
        self.assertEqual(page.evaluate("document.querySelector('.dn-fill').style.width"), "3%")
        self.assertEqual(self.errors, [])

    def test_after_install_the_page_refreshes_its_files_and_signs_out(self):
        page = self.open({"updating": True, "updateProgress": {"percent": 82, "text": "Обновляю страницу LuCI…"}})
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: false, version: '0.12.1', updateResult: {state: 'ok', version: '0.12.1', age: 1}})")
        page.wait_for_function("window.navigated === '/cgi-bin/luci/admin/logout'")
        fetched = page.evaluate("window.fetched")
        self.assertEqual(fetched[0][0], "/luci-static/resources/view/dnevnik/status.js")                  # свежие файлы страницы мимо кэша
        self.assertEqual(fetched[0][1]["cache"], "reload")
        self.assertEqual(page.evaluate("document.querySelector('.dn-fill').style.width"), "100%")
        self.assertIn("войдите снова", page.inner_text(".dn"))
        self.assertEqual(page.evaluate("window.navigated"), "/cgi-bin/luci/admin/logout")
        self.assertIsNone(page.evaluate("window.reloaded"))

    def test_sign_out_happens_once_not_on_every_poll(self):
        page = self.open({"updating": True})
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: false, updateResult: {state: 'ok', version: '0.12.1', age: 1}})")
        page.wait_for_function("window.navigated")
        page.evaluate("window.navigated = null")
        self.poll_all(page); self.poll_all(page)
        page.wait_for_timeout(300)
        self.assertIsNone(page.evaluate("window.navigated"))

    def test_without_luci_helpers_it_reloads_the_page_instead(self):
        page = self.open({"updating": True})
        page.evaluate("window.L = undefined")
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: false, updateResult: {state: 'ok', version: '0.12.1', age: 1}})")
        page.wait_for_function("window.reloaded === true")
        self.assertIsNone(page.evaluate("window.navigated"))

    def test_opening_the_page_after_an_update_does_not_sign_out_again(self):
        """После выхода и нового входа страница видит недавний итог: показывает его, но второй раз не выходит."""
        page = self.open({"updateResult": {"state": "ok", "version": "0.12.1", "age": 20}})
        page.wait_for_timeout(300)
        self.assertIn("Обновление завершено: установлена версия 0.12.1.", page.inner_text(".dn"))
        self.assertIsNone(page.evaluate("window.navigated"))
        self.assertIsNone(page.evaluate("window.reloaded"))

    def test_failed_update_does_not_sign_out_and_hides_the_bar(self):
        page = self.open({"updating": True, "updateProgress": {"percent": 35, "text": "Скачиваю…"}})
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {updating: false, updateResult: {state: 'failed', message: 'ОШИБКА: x', age: 1}})")
        page.wait_for_selector(".dn-note.bad")
        page.wait_for_timeout(300)
        self.assertFalse(page.is_visible(".dn-bar"))
        self.assertIsNone(page.evaluate("window.navigated"))

    def test_news_from_the_new_version_are_listed_after_the_check(self):
        notes = [{"version": "0.12.2", "date": "2026-11-15", "items": ["Первое изменение.", "Второе изменение."]},
                 {"version": "0.12.1", "date": "2026-11-14", "items": ["Старое изменение."]}]
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.2", "newer": True, "notes": notes}})
        self.assertFalse(page.is_visible(".dn-news"))
        page.click("text=Проверить новую версию")
        page.wait_for_selector(".dn-news")
        text = page.inner_text(".dn-news")
        for needle in ("Что нового", "Версия 0.12.2 · 2026-11-15", "Первое изменение.", "Второе изменение.", "Версия 0.12.1 · 2026-11-14", "Старое изменение."):
            self.assertIn(needle, text)
        self.assertEqual(page.eval_on_selector_all(".dn-news li", "e => e.length"), 3)

    def test_no_news_block_when_up_to_date_empty_or_rechecking(self):
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.0", "newer": False, "notes": []}})
        page.click("text=Проверить новую версию"); page.wait_for_selector("text=последняя версия")
        self.assertFalse(page.is_visible(".dn-news"))
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True, "notes": []}})
        page.click("text=Проверить новую версию"); page.wait_for_selector("text=Обновить до 0.12.1")
        self.assertFalse(page.is_visible(".dn-news"))                                        # список не пришёл: обновиться всё равно можно
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True}})
        page.click("text=Проверить новую версию"); page.wait_for_selector("text=Обновить до 0.12.1")   # старый плагин без поля notes
        self.assertFalse(page.is_visible(".dn-news"))

    def test_news_text_from_github_is_not_executed(self):
        notes = [{"version": "0.12.1", "date": "<b>x</b>", "items": ['<img src=x onerror="window.pwned=1">']}]
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True, "notes": notes}})
        page.click("text=Проверить новую версию"); page.wait_for_selector(".dn-news")
        self.assertIsNone(page.query_selector(".dn-news img"))
        self.assertIsNone(page.query_selector(".dn-news b b"))
        self.assertIsNone(page.evaluate("window.pwned"))
        self.assertIn("<img", page.inner_text(".dn-news"))

    def test_confirmation_tells_about_the_sign_out(self):
        page = self.open(answers={"check_update": {"ok": True, "current": "0.12.0", "latest": "0.12.1", "newer": True}})
        page.click("text=Проверить новую версию"); page.wait_for_selector("text=Обновить до 0.12.1")
        page.click("text=Обновить до 0.12.1")
        self.assertIn("выйдете из LuCI", page.inner_text("#modal"))

    def pform(self, page):
        return page.query_selector_all(".dn-pform input")

    def test_family_card_when_password_is_off(self):
        page = self.open()
        self.assertIn("Пароль не задан", page.inner_text(".dn-pill >> nth=1"))
        self.assertIn("открыт всем", page.inner_text(".dn"))
        self.assertEqual(page.inner_text("button:has-text('пароль') >> nth=0"), "Задать пароль")
        self.assertFalse(page.is_visible("button:has-text('Отключить пароль')"))
        self.assertEqual(len(self.pform(page)), 3)
        self.assertEqual(self.pform(page)[0].get_attribute("type"), "password")                  # пароль не показывается на экране
        self.assertEqual(self.errors, [])

    def test_family_card_when_password_is_on(self):
        page = self.open({"family": True, "familyHint": "имя кота", "familyChanged": 1762900000})
        self.assertIn("Пароль включён", page.inner_text(".dn"))
        self.assertTrue(page.is_visible("button:has-text('Сменить пароль')"))
        self.assertTrue(page.is_visible("button:has-text('Отключить пароль')"))
        self.assertEqual(page.input_value(".dn-pform input[type=text]"), "имя кота")             # текущая подсказка подставлена

    def test_family_set_sends_password_and_hint(self):
        page = self.open()
        page.fill(".dn-pform input >> nth=0", "семейный-пароль-123")
        page.fill(".dn-pform input >> nth=1", "семейный-пароль-123")
        page.fill(".dn-pform input >> nth=2", "имя кота")
        page.click("button:has-text('Задать пароль')")
        page.wait_for_function("window.calls.some(c => c[0] === 'family_set')")
        self.assertEqual(page.evaluate("window.calls.find(c => c[0] === 'family_set')[1]"), ["семейный-пароль-123", "имя кота"])
        page.wait_for_selector(".dn-note.ok")
        self.assertEqual(page.input_value(".dn-pform input >> nth=0"), "")                         # пароль с экрана стёрт
        self.assertEqual(page.input_value(".dn-pform input >> nth=1"), "")
        self.assertNotIn("семейный-пароль-123", page.content())
        self.assertEqual(self.errors, [])

    def test_family_set_checks_before_calling(self):
        page = self.open()
        page.click("button:has-text('Задать пароль')")                                            # пустой пароль
        page.wait_for_selector(".dn-note.bad:has-text('Введите пароль')")
        page.fill(".dn-pform input >> nth=0", "семейный-пароль-123")
        page.fill(".dn-pform input >> nth=1", "другой-пароль-12345")
        page.click("button:has-text('Задать пароль')")
        page.wait_for_selector(".dn-note.bad:has-text('не совпадают')")
        self.assertNotIn("family_set", self.names(page))                                          # на сервер ничего не ушло

    def test_family_set_accepts_a_one_character_password(self):
        page = self.open()
        self.assertIn("даже из одного символа", page.inner_text(".dn"))
        page.fill(".dn-pform input >> nth=0", "я")
        page.fill(".dn-pform input >> nth=1", "я")
        page.click("button:has-text('Задать пароль')")
        page.wait_for_function("window.calls.some(c => c[0] === 'family_set')")
        self.assertEqual(page.evaluate("window.calls.find(c => c[0] === 'family_set')[1]"), ["я", ""])

    def test_remote_card_when_funnel_is_on(self):
        page = self.open(answers={"remote_info": {"ok": True, "installed": True, "state": "Running", "funnel": True, "url": "https://dnevnik.example.ts.net/",
                                                  "proxy": "127.0.0.1:7890", "proxyAlive": True, "family": True, "command": "/root/dnevnik/router/remote.sh"}})
        text = page.inner_text(".dn")
        for needle in ("Доступно из интернета", "Tailscale: Running", "через прокси 127.0.0.1:7890", "Проверить всё", "Приглашение для семьи"):
            self.assertIn(needle, text)
        self.assertEqual(page.get_attribute("a:has-text('dnevnik.example.ts.net')", "href"), "https://dnevnik.example.ts.net/")
        self.assertFalse(page.is_visible("text=Чтобы дневник открывался не только дома"))      # включено, объяснять нечего
        self.assertEqual(self.errors, [])

    def test_remote_card_when_not_installed_shows_the_command(self):
        page = self.open(answers={"remote_info": {"ok": True, "installed": False, "state": "", "funnel": False, "url": "", "proxy": "", "command": "/root/dnevnik/router/remote.sh"}})
        self.assertIn("Только дома", page.inner_text(".dn"))
        self.assertIn("--remote", page.inner_text(".dn-code"))
        self.assertTrue(page.is_disabled("button:has-text('Приглашение для семьи')"))          # приглашать пока некуда

    def test_remote_card_when_installed_but_funnel_is_off_shows_the_short_command(self):
        page = self.open(answers={"remote_info": {"ok": True, "installed": True, "state": "Running", "funnel": False, "url": "", "proxy": "", "command": "/root/dnevnik/router/remote.sh"}})
        self.assertIn("Не включено", page.inner_text(".dn"))
        self.assertEqual(page.inner_text(".dn-code"), "sh /root/dnevnik/router/remote.sh")

    def test_proxy_that_does_not_answer_is_shown_as_a_warning(self):
        page = self.open(answers={"remote_info": {"ok": True, "installed": True, "state": "Running", "funnel": True, "url": "https://dnevnik.example.ts.net/",
                                                  "proxy": "127.0.0.1:7890", "proxyAlive": False, "command": "x"}})
        self.assertIn("прокси не отвечает", page.inner_text(".dn"))

    def test_invitation_text_has_the_address_and_no_password(self):
        page = self.open(answers={"remote_info": {"ok": True, "installed": True, "state": "Running", "funnel": True, "url": "https://dnevnik.example.ts.net/",
                                                  "proxy": "", "command": "x", "family": True}})
        self.assertFalse(page.is_visible("textarea"))
        page.click("button:has-text('Приглашение для семьи')")
        page.wait_for_selector("textarea", state="visible")
        text = page.input_value("textarea")
        for needle in ("https://dnevnik.example.ts.net/", "Ничего устанавливать не нужно", "На экран Домой", "2–3 попытки", "пароль семьи"):
            self.assertIn(needle, text)
        self.assertNotRegex(text, r"(?i)пароль:\s*\S")                                          # самого пароля в приглашении нет

    def test_doctor_button_shows_results_with_fixes(self):
        res = {"ok": True, "address": "", "summary": {"ok": 1, "warn": 1, "fail": 1, "info": 0, "verdict": "problems"}, "results": [
            {"id": "web", "status": "ok", "title": "Страница дневника на роутере", "detail": "отвечает", "fix": ""},
            {"id": "family", "status": "warn", "title": "Пароль семьи", "detail": "не задан", "fix": "задайте пароль"},
            {"id": "internet", "status": "fail", "title": "Связь роутера с dnevnik.ru", "detail": "нет", "fix": "проверьте интернет"}]}
        page = self.open(answers={"doctor": res})
        page.click("button:has-text('Проверить всё')")
        page.wait_for_selector(".dn-chk")
        rows = page.eval_on_selector_all(".dn-chk", "e => e.map(x => x.className + '|' + x.textContent)")
        self.assertEqual(len(rows), 3)
        self.assertIn("dn-chk fail", rows[2])
        self.assertIn("Что делать: проверьте интернет", rows[2])
        self.assertNotIn("Что делать", rows[0])                                                  # у зелёного пункта советов нет
        self.assertIn("есть проблемы", page.inner_text(".dn-note.bad").lower())
        self.assertEqual(self.errors, [])

    def test_doctor_failure_is_shown(self):
        page = self.open(answers={"doctor": {"ok": False, "message": "Проверка не удалась: тест"}})
        page.click("button:has-text('Проверить всё')")
        page.wait_for_selector(".dn-note.bad")
        self.assertIn("Проверка не удалась", page.inner_text(".dn-note.bad"))
        self.assertFalse(page.is_disabled("button:has-text('Проверить всё')"))                    # кнопка снова доступна

    def test_doctor_text_from_the_router_is_not_executed(self):
        evil = '<img src=x onerror="window.pwned=1">'
        res = {"ok": True, "address": "", "summary": {"verdict": "notes"}, "results": [{"id": "x", "status": "warn", "title": evil, "detail": evil, "fix": evil}]}
        page = self.open(answers={"doctor": res, "remote_info": {"ok": True, "installed": True, "funnel": True, "url": "https://x.ts.net/", "proxy": evil, "state": evil}})
        page.click("button:has-text('Проверить всё')")
        page.wait_for_selector(".dn-chk")
        self.assertIsNone(page.query_selector(".dn img"))
        self.assertIsNone(page.evaluate("window.pwned"))

    def test_family_set_error_from_server_is_shown(self):
        page = self.open(answers={"family_set": {"ok": False, "message": "Подсказка не должна содержать сам пароль."}})
        page.fill(".dn-pform input >> nth=0", "семейный-пароль-123")
        page.fill(".dn-pform input >> nth=1", "семейный-пароль-123")
        page.click("button:has-text('Задать пароль')")
        page.wait_for_selector(".dn-note.bad:has-text('не должна содержать')")

    def test_family_off_needs_confirmation(self):
        page = self.open({"family": True})
        page.click("button:has-text('Отключить пароль')")
        self.assertIn("Отключить пароль?", page.inner_text("#modal h4"))
        self.assertNotIn("family_off", self.names(page))                                          # от одного нажатия ничего не происходит
        page.click("#modal >> text=Отмена")
        self.assertNotIn("family_off", self.names(page))
        page.click("button:has-text('Отключить пароль')")
        page.click("#modal button.cbi-button-negative")
        page.wait_for_function("window.calls.some(c => c[0] === 'family_off')")

    DEVICES = {"ok": True, "now": 1_800_000_600, "family": True, "failed24h": 0,
               "subs": [{"id": "s1", "name": "Планшет Ани", "label": "Android (SM-X710) · Chrome", "service": "Google", "created": 1_800_000_000, "pending": 0, "fail": 0, "types": ["mark"], "quiet": False},
                        {"id": "s2", "name": "", "label": "iPhone · Safari", "service": "Apple", "created": 1_800_000_100, "pending": 2, "fail": 1, "types": [], "quiet": False}],
               "seen": [{"did": "a" * 12, "dev": "iPhone · Safari", "where": "internet", "ts": 1_800_000_570}, {"did": "b" * 12, "dev": "Windows · Chrome", "where": "home", "ts": 1_799_990_000}],
               "logins": [{"ts": 1_800_000_500, "ok": False, "dev": "Linux · Firefox", "where": "internet", "did": "c" * 12, "n": 3}, {"ts": 1_800_000_000, "ok": True, "dev": "iPhone · Safari", "where": "home", "did": "a" * 12, "n": 1}]}

    def devices_page(self, **over):
        return self.open(answers={"devices_list": dict(self.DEVICES, **over)})

    def test_devices_card_shows_subscriptions_seen_and_login_journal(self):
        page = self.devices_page()
        text = page.inner_text(".dn")
        for needle in ("Устройства и входы", "Планшет Ани · Android (SM-X710) · Chrome", "Google, подключено", "iPhone · Safari", "в очереди: 2", "сбоев подряд: 1",
                       "из интернета, последний раз только что", "из дома, последний раз 3 ч назад", "✗ Неверный пароль (×3): Linux · Firefox", "✓ Вошёл: iPhone · Safari", "Всё спокойно"):
            self.assertIn(needle, text)
        self.assertEqual(self.errors, [])

    def test_failed_attempts_turn_the_pill_to_a_warning(self):
        page = self.devices_page(failed24h=7)
        self.assertIn("Неверных попыток за сутки: 7", page.inner_text(".dn-card:has(.dn-title:has-text('Устройства и входы')) .dn-pill"))
        self.assertEqual(page.get_attribute(".dn-card:has(.dn-title:has-text('Устройства и входы')) .dn-pill", "class").split()[-1], "warn")

    def test_empty_lists_and_no_family_password_are_explained(self):
        page = self.devices_page(subs=[], seen=[], logins=[], family=False)
        text = page.inner_text(".dn")
        for needle in ("Пока ни одного устройства", "Пока никого", "Пока записей нет", "Пароль семьи не задан: входы не записываются"):
            self.assertIn(needle, text)

    def test_old_router_without_the_method_gives_a_hint_not_a_crash(self):
        page = self.open()                                                                      # ответ по умолчанию: {ok: true, message: 'ок'} без списков
        self.assertIn("Устройства и входы", page.inner_text(".dn"))
        page2 = self.open(answers={"devices_list": {"ok": False}})
        self.assertIn("Обновите программу", page2.inner_text(".dn"))
        self.assertEqual(self.errors, [])

    def test_removing_a_device_needs_confirmation(self):
        page = self.devices_page()
        page.click(".dn-card:has(.dn-title:has-text('Устройства и входы')) button:has-text('Отключить') >> nth=0")
        self.assertIn("Отключить устройство?", page.inner_text("#modal h4"))
        self.assertIn("Планшет Ани", page.inner_text("#modal"))
        self.assertNotIn("device_remove", self.names(page))                                      # от одного нажатия ничего не происходит
        page.click("#modal >> text=Отмена")
        self.assertNotIn("device_remove", self.names(page))
        page.click(".dn-card:has(.dn-title:has-text('Устройства и входы')) button:has-text('Отключить') >> nth=0")
        page.click("#modal button.cbi-button-negative")
        page.wait_for_function("window.calls.some(c => c[0] === 'device_remove')")
        self.assertEqual(page.evaluate("window.calls.find(c => c[0] === 'device_remove')[1]"), ["s1"])
        page.wait_for_selector(".dn-note.ok")

    def test_clearing_the_journal_needs_confirmation(self):
        page = self.devices_page()
        page.click("button:has-text('Очистить журнал')")
        self.assertIn("Очистить журнал входов?", page.inner_text("#modal h4"))
        self.assertNotIn("logins_clear", self.names(page))
        page.click("#modal >> text=Отмена")
        page.click("button:has-text('Очистить журнал')")
        page.click("#modal button.cbi-button-negative")
        page.wait_for_function("window.calls.some(c => c[0] === 'logins_clear')")

    def test_refresh_button_reloads_the_list(self):
        page = self.devices_page()
        before = self.names(page).count("devices_list")
        page.click("button:has-text('Обновить список')")
        page.wait_for_function(f"window.calls.filter(c => c[0] === 'devices_list').length > {before}")

    def test_device_text_from_the_router_is_not_executed(self):
        evil = '<img src=x onerror="window.pwned=1">'
        page = self.devices_page(subs=[dict(self.DEVICES["subs"][0], name=evil, label=evil)], seen=[{"did": "a" * 12, "dev": evil, "where": "home", "ts": 1}],
                                 logins=[{"ts": 1, "ok": True, "dev": evil, "where": "home", "did": "a" * 12, "n": 1}])
        self.assertIsNone(page.query_selector(".dn img"))
        self.assertIsNone(page.evaluate("window.pwned"))

    def test_save_settings_sends_numbers_and_name(self):
        page = self.open()
        page.select_option(".dn-form select >> nth=0", "10")
        page.select_option(".dn-form select >> nth=1", "7")
        page.select_option(".dn-form select >> nth=2", "21")
        page.fill(".dn-form input", "Катя")
        page.click("text=Сохранить")
        page.wait_for_function("window.calls.some(c => c[0] === 'set_config')")
        self.assertEqual(page.evaluate("window.calls.find(c => c[0] === 'set_config')[1]"), [10, 7, 21, "Катя"])
        page.wait_for_selector(".dn-note.ok")

    def test_save_error_is_shown(self):
        page = self.open(answers={"set_config": {"ok": False, "message": "Часы заданы неверно."}})
        page.click("text=Сохранить")
        page.wait_for_selector(".dn-note.bad")
        self.assertIn("Часы заданы неверно", page.inner_text(".dn-note.bad"))

    def test_logout_needs_two_confirmations_and_a_pause(self):
        page = self.open(answers={"logout_begin": {"ok": True, "token": "tok-9"}, "logout_confirm": {"ok": True, "message": "Вы вышли."}})
        page.click("text=Выйти из аккаунта")
        self.assertIn("Выйти из аккаунта?", page.inner_text("#modal h4"))
        self.assertEqual(self.names(page).count("logout_begin"), 0)                      # от одного нажатия ничего не происходит
        page.click("#modal >> text=Отмена")
        self.assertIsNone(page.query_selector("#modal"))
        page.click("text=Выйти из аккаунта"); page.click("#modal >> text=Да, выйти")
        page.wait_for_selector("text=Вы точно хотите выйти")
        self.assertEqual(self.names(page).count("logout_confirm"), 0)                    # код получен, но выхода ещё нет
        self.assertTrue(page.is_disabled("#modal >> text=Да, выйти из аккаунта"))          # защита от случайного второго нажатия
        page.wait_for_function("!document.querySelector('#modal button.cbi-button-negative').disabled", timeout=6000)
        page.click("#modal >> text=Да, выйти из аккаунта")
        page.wait_for_function("window.calls.some(c => c[0] === 'logout_confirm')")
        self.assertEqual(page.evaluate("window.calls.find(c => c[0] === 'logout_confirm')[1]"), ["tok-9"])
        self.assertEqual(self.errors, [])

    def test_logout_second_step_can_be_cancelled(self):
        page = self.open(answers={"logout_begin": {"ok": True, "token": "t"}})
        page.click("text=Выйти из аккаунта"); page.click("#modal >> text=Да, выйти")
        page.wait_for_selector("text=Вы точно хотите выйти")
        page.click('#modal button >> text="Нет, оставить как есть"')
        self.assertIsNone(page.query_selector("#modal"))
        page.wait_for_timeout(3300)
        self.assertEqual(self.names(page).count("logout_confirm"), 0)

    def test_page_polls_and_survives_a_dead_backend(self):
        page = self.open()
        page.evaluate("window.mockStatus = Object.assign({}, window.mockStatus, {version: '0.12.1', needLogin: true})")
        page.evaluate("window.pollFns[0]()")
        page.wait_for_function("document.querySelector('.dn-pill').textContent === 'Нужен вход'")
        self.assertIn("0.12.1", page.inner_text(".dn"))

    def test_phone_width_has_no_horizontal_scroll(self):
        page = self.open()
        page.set_viewport_size({"width": 375, "height": 800})
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 376)


if __name__ == "__main__":
    unittest.main()
