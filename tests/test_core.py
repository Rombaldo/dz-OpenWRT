import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import core


# ---------------------------------------------------------------- разбор данных
def raw_day(lessons, day=(2026, 11, 11)):
    ts = int(datetime(*day, tzinfo=timezone.utc).timestamp())
    return {"date": str(ts), "lessons": lessons}


def raw_lesson(**kw):
    base = {
        "id": "lesson-1", "number": 1, "hours": {"startHour": "8", "startMinute": "00", "endHour": "8", "endMinute": "40"},
        "isCanceled": False, "isEmpty": False, "theme": "Тема", "groupId": "g",
        "subject": {"id": "s1", "name": "Рус. язык"},
        "homework": {"text": "  упр 1  ", "attachments": []},
        "workMarks": [],
    }
    base.update(kw)
    return base


class NormDayTests(unittest.TestCase):
    def test_basic_fields(self):
        d = core.norm_day(raw_day([raw_lesson()]))
        self.assertEqual(d["date"], "2026-11-11")
        self.assertEqual(d["weekday"], "ср")
        l = d["lessons"][0]
        self.assertEqual((l["n"], l["start"], l["end"]), (1, "8:00", "8:40"))
        self.assertEqual((l["subject"], l["subjectId"]), ("Рус. язык", "s1"))
        self.assertEqual(l["homework"], "упр 1")

    def test_marks_are_flattened(self):
        wm = [{"workName": "Ответ", "marks": [{"value": "5", "createdDate": "2026-11-11T12:00:00"}]}]
        l = core.norm_day(raw_day([raw_lesson(workMarks=wm)]))["lessons"][0]
        self.assertEqual(l["marks"], [{"value": "5", "work": "Ответ", "created": "2026-11-11T12:00:00"}])

    def test_empty_lesson_without_subject_is_skipped(self):
        self.assertEqual(core.norm_day(raw_day([raw_lesson(isEmpty=True, subject=None)]))["lessons"], [])

    def test_canceled_flag(self):
        self.assertTrue(core.norm_day(raw_day([raw_lesson(isCanceled=True)]))["lessons"][0]["canceled"])

    def test_attachments_become_links(self):
        att = {"text": "см. файл", "attachments": [
            {"fileId": "42", "fileName": "Карта", "fileExtension": "PDF", "size": 1000, "fileLink": "https://b5.csdnevnik.ru/x.pdf?a=1"},
            {"fileId": "43", "fileName": "Чужой", "fileExtension": "pdf", "size": 1, "fileLink": "https://evil.example.com/a.pdf"},
            {"fileName": "без номера"},   # без числового id не берём
        ]}
        files = core.norm_day(raw_day([raw_lesson(homework=att)]))["lessons"][0]["files"]
        self.assertEqual(files[0], {"id": "42", "name": "Карта", "ext": "pdf", "size": 1000,
                                    "url": "https://b5.csdnevnik.ru/x.pdf?a=1"})
        self.assertEqual(files[1]["url"], "")     # ссылку с чужого сервера не отдаём
        self.assertEqual(len(files), 2)

    def test_missing_homework(self):
        l = core.norm_day(raw_day([raw_lesson(homework=None)]))["lessons"][0]
        self.assertEqual((l["homework"], l["files"]), ("", []))

    def test_trusted_link(self):
        self.assertTrue(core.trusted_link("https://b5.csdnevnik.ru/a.pdf?x=1"))
        self.assertTrue(core.trusted_link("https://dnevnik.ru/f"))
        for bad in ["http://b5.csdnevnik.ru/a.pdf", "https://evil.example.com/a.pdf", "https://csdnevnik.ru.evil.com/a",
                    "file:///etc/passwd", "javascript:alert(1)", "", None]:
            self.assertFalse(core.trusted_link(bad), bad)


# ---------------------------------------------------------------- обновление с поддельным сайтом
# Так страница Дневник.ру встраивает состояние: школьные записи, классы по годам (в т. ч. внеурочка, которая нам не нужна)
STATE_HTML = ('<script>window.state={"context":{"role":"Student","schoolMemberships":['
              '{"personId":"1111111111","firstName":"Аня","lastName":"Тестова","schoolId":"2222222222",'
              '"schoolName":"МБОУ \\"Школа № 1\\"","studyYear":2026,"groups":[{"groupId":"5001","groupName":"5А"}]},'
              '{"personId":"1111111111","firstName":"Аня","lastName":"Тестова","schoolId":"3333333333","schoolName":"Допобразование","studyYear":2025,"groups":[]}'
              ']},"groups":[{"id":"5001","name":"5А","studyYear":2026,"isFinalPeriodTabByDefault":false,"periods":['
              '{"id":"7001","name":"1 триместр","shortName":"тр","number":0,"isDefault":true},'
              '{"id":"7002","name":"2 триместр","shortName":"тр","number":1,"isDefault":false},'
              '{"id":"7003","name":"3 триместр","shortName":"тр","number":2,"isDefault":false}],"termReportPeriods":null,"periodTabName":"По триместрам"},'
              '{"id":"4001","name":"4А","studyYear":2025,"isFinalPeriodTabByDefault":true,"periods":['
              '{"id":"6001","name":"1 триместр","shortName":"тр","number":0,"isDefault":false},'
              '{"id":"6002","name":"2 триместр","shortName":"тр","number":1,"isDefault":false},'
              '{"id":"6003","name":"3 триместр","shortName":"тр","number":2,"isDefault":true}],"termReportPeriods":null,"periodTabName":"По триместрам"},'
              '{"id":"4999","name":"4А","studyYear":2025,"isFinalPeriodTabByDefault":true,"periods":['
              '{"id":"6999","name":"ПЕРИОД_ДЛЯ_ОДО","shortName":"п","number":0,"isDefault":true}],"termReportPeriods":null,"periodTabName":"По периодам"}'
              ']}</script>')
FINAL_JSON = {"subjects": [
    {"name": "Рус.  язык", "finalWorks": [
        {"type": "Period", "periodNumber": 0, "marks": [{"value": "4"}]}, {"type": "Period", "periodNumber": 1, "marks": [{"value": "3"}]},
        {"type": "Period", "periodNumber": 2, "marks": []}, {"type": "Year", "periodNumber": 0, "marks": [{"value": "4"}]}]},
    {"name": "Музыка", "finalWorks": [{"type": "Period", "periodNumber": 0, "marks": [{"value": "ЗЧ"}]},
                                       {"type": "Year", "periodNumber": 0, "marks": [{"value": "4"}, {"value": "5"}]}]}]}
TEACHER_HTML = ('<div id="ContentPartTeachers"><h3>Учитель</h3><ul class="peopleS"><li><div class="user_ava"></div> '
                '<a href="https://dnevnik.ru/v2/user/user?user=1" title="x" class="u">Иванова&nbsp;Мария  Петровна</a> <p>Учитель</p></li>'
                '<li><a href="/v2/user/user?user=2" class="u">Сидоров Пётр Иванович</a></li></ul></div><div>дальше</div>')
TERM_JSON = {"dateStart": 1756684800, "dateFinish": 1764288000, "subjects": [
    {"name": "Рус. язык", "average": {"value": "3,5"}, "works": [
        {"date": 200, "marks": [{"value": "3"}]}, {"date": 100, "marks": [{"value": "4"}, {"value": "2"}]}],
     "totalLessonNumber": 40, "countAbsence": 3, "countIllAbsence": 2, "countBeingLate": 1, "lessonLogEntries": [
         {"id": "e1", "lessonId": "l1", "value": "Б", "fullName": "Пропуск по болезни", "date": "1759708800", "lessonNumber": 2},
         {"id": "e2", "lessonId": "l2", "value": "Б", "fullName": "Пропуск по болезни", "date": "1759708800", "lessonNumber": 3},
         {"id": "e3", "lessonId": "l3", "value": "Н", "fullName": "Пропуск без уважительной причины", "date": "1760918400", "lessonNumber": 1},
         {"id": "e4", "lessonId": "l4", "value": "О", "fullName": "Опоздание", "date": "1761004800", "lessonNumber": 1}]},
    {"name": "Пустой предмет", "average": None, "works": [], "totalLessonNumber": 60, "lessonLogEntries": [
         {"id": "e1x", "lessonId": "l1", "value": "Б", "fullName": "Пропуск по болезни", "date": "1759708800", "lessonNumber": 2}]}]}


# ---------------------------------------------------------------- рейтинг (выдуманные данные в формате сайта)
def _rank(place, mark="4,40", change="+ 0,02", trend="Up", me=False, group="5А", cp=None, animal=7):
    row = {"contextPerson": {"id": "1111111111", "avatarUrl": "https://example.org/a.jpg"} if me else None,
           "isContextUser": me, "ranking": {"place": place, "metadata": {"isRestricted": False}},
           "averageMark": {"mark": mark, "metadata": {"isRestricted": False}},
           "averageMarkChange": {"value": change, "trend": trend} if change else None,
           "ratingLine": {"groupName": group, "scalePercent": 50, "imageUrl": f"https://example.org/animalrating/{animal}.png"}}
    if cp:
        row["contextGroupRanking"] = {"place": cp, "metadata": {"isRestricted": False}}
    return row


def parallel_rows():
    return [_rank(1, group="5Б", animal=3), _rank(2, group="5А", cp=1, animal=4), _rank(3, "4,42", "+ 0,01", "Up", True, "5А", 2, 9),
            _rank(4, group="5Б", animal=5), _rank(5, group="5А", cp=3, animal=6)]


def rating_feed(restricted=False):
    r = {"isRestricted": restricted}
    return {
        "commonGroupRatingEvent": {"trend": {"value": "Up"}, "groupRanking": {"place": 3, "metadata": r}},
        "commonParallelRatingEvent": {"trend": {"value": "Equal"}, "parallelRanking": {"place": 5, "metadata": r}},
        "topRatingEvent": {"title": {"value": "Чужая Фамилия попала в топ Литература"}},
        "personalRatingBySubjectEvents": [
            {"trend": {"value": "Down"}, "subject": {"id": "s1", "name": "Русский язык"}, "place": {"value": 4, "metadata": r}, "isPro": True},
            {"trend": {"value": "Up"}, "subject": {"id": "s9", "name": "Музыка"}, "place": {"value": 2, "metadata": {"isRestricted": False}}}]}


def rating_state(restricted=False):
    return {"childPersonId": "1111111111", "isPro": True,
            "groupRating": {"rankingPlaces": [_rank(2, "4,50", "- 0,03", "Down"), _rank(3, "4,42", "+ 0,01", "Up", True), _rank(3, "4,42")]},
            "parallelRating": {"rankingPlaces": [_rank(4, group="5Б"), _rank(5, "4,42", "+ 0,01", "Up", True), _rank(5)]},
            "subjectTop": {"title": "Ваш рейтинг по предметам", "subjects": [
                {"groupId": "5001", "ranking": {"place": 7, "metadata": {"isRestricted": restricted}}, "subject": {"id": "s1", "name": "Русский язык"},
                 "averageMark": {"mark": "3,07", "metadata": {"isRestricted": False}}, "averageMarkChange": {"value": "- 0,07", "trend": "Down"}},
                {"groupId": "5001", "ranking": {"place": 1, "metadata": {"isRestricted": False}}, "subject": {"id": "s9", "name": "Музыка"},
                 "averageMark": {"mark": "5,00", "metadata": {"isRestricted": False}}, "averageMarkChange": None}]},
            "commonRating": {"group": {"name": "x"}, "rankingPlaces": parallel_rows()},
            "commonInformation": {"groupRatingPlace": 3, "parallelRatingPlace": 5, "metadata": {"isRestricted": restricted}},
            "ratingDescription": {"body": "На основе данных о среднем балле учащихся в параллели (1 триместр)"}}


def rating_page(restricted=False):
    return "<html><script>window.__RATINGS__INITIAL__STATE__ = " + json.dumps(rating_state(restricted), ensure_ascii=False) + ";</script></html>"


def subject_page(sid="s1", restricted=False):
    r = {"isRestricted": restricted}
    hint = [{"text": "Получите ", "mark1": None, "mark2": None}, {"text": None, "mark1": {"value": 5, "count": 6, "mood": "Good", "metadata": {"isRestricted": False}}, "mark2": None},
            {"text": " или ", "mark1": None, "mark2": None}, {"text": None, "mark1": None, "mark2": {"value": 4, "count": 20, "mood": "Good", "metadata": {"isRestricted": False}}},
            {"text": ", чтобы повысить балл до четверки", "mark1": None, "mark2": None}]
    return {"commonRating": {"group": {"name": "x"}, "rankingPlaces": parallel_rows(), "metadata": r},
            "marksCorrection": {"blocks": hint, "metadata": r},
            "averageMark": {"value": "3,07", "mood": "Average", "text": "ср. балл общий", "metadata": r},
            "commonSubjectInformation": {"firstName": "Аня", "lastName": "Тестова", "groupRatingPlace": 4 if sid == "s1" else 2,
                                         "parallelRatingPlace": 7 if sid == "s1" else 1, "metadata": r}}


def mark_details_json(restricted=False):
    r = {"isRestricted": restricted}
    return {"markDetails": {"marks": [{"id": "9001", "value": "5", "mood": "Good"}], "markDateInfoText": "Выставлено 11 нояб. за урок 11 нояб."},
            "teacher": {"firstName": "Мария", "lastName": "Тестова", "messengerEntryPoint": {"chatJid": None}},
            "subject": {"id": "s1", "name": "Рус. язык", "averageMark": {"value": "3,07", "trend": "Up", "text": "ср. балл", "mood": "Average"}},
            "classmatesMarks": {"items": [{"mood": "Good", "studentCount": 18, "markNumber": 0, "value": "5", "percent": 56.25},
                                          {"mood": "Average", "studentCount": 6, "markNumber": 2, "value": "3", "percent": 18.75}]},
            "subjectGroupRatingWidget": {"place": 4, "ratingsUrl": "x", "metadata": r}, "subjectParallelRatingWidget": {"place": 7, "ratingsUrl": "x", "metadata": r},
            "hasActiveSubscription": True}


MARK_WORK = [{"workName": "Ответ на уроке", "workId": "w1", "workUrl": "", "marks": [{"id": "9001", "value": "5", "mood": "Good", "createdDate": "2026-11-11T07:00:00"}]}]


class FakeSite(http.server.BaseHTTPRequestHandler):
    good_token = "good"
    requests = 0
    login_posts = 0
    only_school = None          # если задано, дневник отдаёт уроки только для этой школы
    paths = []                  # какие адреса просили
    details_requests = 0
    lesson_requests = 0
    lesson_fails = False
    final_fails = False
    rating_mode = "ok"          # ok | restricted | fail | nopage (страницы рейтинга нет)
    with_marks = False          # дневник отдаёт у урока оценку с номером
    captcha = False             # форма входа просит капчу
    password = "secret"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        raw = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def login_form(self, error=""):
        captcha_id = "abc123" if FakeSite.captcha else ""
        return (f'<form class="login" method="POST"><input type="hidden" name="exceededAttempts" value="False">'
                f'<input type="hidden" name="ReturnUrl" value=""><input type="hidden" name="FingerprintId" value="">'
                f'<input type="hidden" name="Captcha.Id" value="{captcha_id}"><input type="text" name="login">'
                f'<input type="password" name="password"></form>{error}')

    def do_POST(self):
        FakeSite.login_posts += 1
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
        if self.path.startswith("/login/") and form.get("login") == ["kate"] and form.get("password") == [FakeSite.password] and not FakeSite.captcha:
            self._send(302, "", headers=[("Location", "/userfeed"),
                                         ("Set-Cookie", f"DnevnikAuth_a={FakeSite.good_token}; Path=/; HttpOnly"),
                                         ("Set-Cookie", "dnevnik_sst=fresh; Path=/; HttpOnly")])
        else:
            self._send(200, self.login_form('<div class="error">Ошибка в логине или пароле. Проверьте правильность заполнения полей.</div>'))

    def do_GET(self):
        FakeSite.requests += 1
        path = self.path.split("?")[0]
        if path == "/login/":
            return self._send(200, self.login_form())
        if path == "/userfeed":
            return self._send(200, "<html>лента</html>")
        authed = f"DnevnikAuth_a={FakeSite.good_token}" in (self.headers.get("Cookie") or "")
        FakeSite.paths.append(self.path)
        if path == "/v2/lesson":
            FakeSite.lesson_requests += 1
            if not authed:
                return self._send(401, "login")
            if FakeSite.lesson_fails:
                return self._send(500, "oops")
            return self._send(200, "<html>" + TEACHER_HTML + "</html>")
        if path.startswith("/marks/school/") and path.endswith("/period"):
            return self._send(200, STATE_HTML) if authed else self._send(401, "login")
        if path.startswith("/api/v2/marks/final/school/"):
            if not authed:
                return self._send(401, "login")
            if FakeSite.final_fails:
                return self._send(500, "oops")
            return self._send(200, json.dumps(FINAL_JSON), "application/json")
        if path.startswith("/api/v2/marks/school/") and "/person/" in path:
            if not authed:
                return self._send(401, "login")
            return self._send(200, json.dumps(TERM_JSON), "application/json")
        if path == "/ratings/feed-widget":
            if not authed:
                return self._send(401, "login")
            if FakeSite.rating_mode == "fail":
                return self._send(500, "oops")
            return self._send(200, json.dumps(rating_feed(FakeSite.rating_mode == "restricted")), "application/json")
        if path == "/ratings/subject":
            if not authed:
                return self._send(401, "login")
            if FakeSite.rating_mode == "fail":
                return self._send(500, "oops")
            sid = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("subjectId", [""])[0]
            return self._send(200, json.dumps(subject_page(sid, FakeSite.rating_mode == "restricted")), "application/json")
        if path == "/ratings/marks-details":
            if not authed:
                return self._send(401, "login")
            FakeSite.details_requests += 1
            if FakeSite.rating_mode == "fail":
                return self._send(500, "oops")
            return self._send(200, json.dumps(mark_details_json(FakeSite.rating_mode == "restricted")), "application/json")
        if path == "/statistics":
            if not authed or FakeSite.rating_mode == "nopage":
                return self._send(401 if not authed else 404, "no")
            return self._send(200, rating_page(FakeSite.rating_mode == "restricted"))
        if path == "/marks":
            if not authed:
                return self._send(401, "<html>login</html>")
            return self._send(200, '<a href="/statistics?childPersonId=1111111111">r</a> '
                                   '<a href="/v2/homework?school=3333333333">x</a> <a href="/v2/homework?school=2222222222">y</a>')
        if "/api/v2/marks/diary" not in self.path or not authed:
            return self._send(401, "<html>login</html>")
        if FakeSite.only_school and f"schoolId={FakeSite.only_school}" not in self.path:
            return self._send(200, json.dumps({"days": []}), "application/json")
        att = {"text": "читать", "attachments": [{"fileId": "7", "fileName": "ГЕО", "fileExtension": "pdf", "size": 5,
                                                  "fileLink": "https://b5.csdnevnik.ru/geo.pdf"}]}
        marks = {"workMarks": MARK_WORK} if FakeSite.with_marks else {}
        body = json.dumps({"days": [raw_day([raw_lesson(homework=att, **marks)]), raw_day([raw_lesson()], day=(2026, 11, 12))]}).encode()
        self._send(200, body, "application/json", headers=[("Set-Cookie", "dnevnik_sst=rotated; Path=/; Max-Age=86400; HttpOnly")])


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeSite.good_token = "good"
        FakeSite.only_school, FakeSite.captcha, FakeSite.password, FakeSite.login_posts = None, False, "secret", 0
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.www = root / "data", root / "www"
        self.data.mkdir(), self.www.mkdir()
        (self.www / "index.html").write_text("<script>window.__DATA__ = /*INLINE_DATA*/null;</script>")
        (self.data / "ids.json").write_text(json.dumps({"personId": "1", "schoolId": "2"}))
        self.write_session("good")
        self.out = self.www / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_session(self, token):
        cookies = [
            {"name": "DnevnikAuth_a", "value": token, "domain": "127.0.0.1", "path": "/", "expires": time.time() + 86400 * 14, "httpOnly": True},
            {"name": "dnevnik_sst", "value": "old", "domain": "127.0.0.1", "path": "/", "expires": time.time() + 3600, "httpOnly": True},
        ]
        (self.data / "session.json").write_text(json.dumps(cookies))

    def run_sync(self, **kw):
        return core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11), **kw)

    def test_successful_sync_writes_everything(self):
        payload = self.run_sync()
        self.assertEqual(payload["status"], {"needLogin": False, "error": None, "syncing": False, "canRestore": False})
        saved = json.loads(self.out.read_text())
        self.assertEqual([d["date"] for d in saved["days"]], ["2026-11-11", "2026-11-12"])
        self.assertEqual(saved["days"][0]["lessons"][0]["files"][0]["url"], "https://b5.csdnevnik.ru/geo.pdf")
        self.assertTrue((self.data / "history.json").exists())
        offline = (self.www / "offline.html").read_text()
        self.assertIn('"days"', offline)
        self.assertNotIn("/*INLINE_DATA*/null", offline)

    def test_rotated_cookie_is_saved_and_files_are_private(self):
        self.run_sync()
        names = {c["name"]: c["value"] for c in json.loads((self.data / "session.json").read_text())}
        self.assertEqual(names["dnevnik_sst"], "rotated")
        self.assertEqual(names["DnevnikAuth_a"], "good")
        for f in ("session.json", "history.json"):
            self.assertEqual(oct(os.stat(self.data / f).st_mode)[-3:], "600", f)

    def test_expired_login_keeps_old_data_and_flags_it(self):
        first = self.run_sync()
        FakeSite.good_token = "other"                      # сайт перестал узнавать вход
        with self.assertRaises(core.NeedLogin):
            self.run_sync()
        saved = json.loads(self.out.read_text())
        self.assertTrue(saved["status"]["needLogin"])
        self.assertEqual(len(saved["days"]), 2)           # старые данные остались
        self.assertEqual(saved["updatedAt"], first["updatedAt"])

    def test_no_session_means_login_needed(self):
        (self.data / "session.json").unlink()
        with self.assertRaises(core.NeedLogin):
            self.run_sync()
        self.assertTrue(json.loads(self.out.read_text())["status"]["needLogin"])

    def test_flash_writes_are_limited(self):
        self.run_sync()
        old = time.time() - 3600
        for f in ("history.json", "session.json"):
            os.utime(self.data / f, (old, old))
        self.run_sync(session_save_hours=6)
        self.assertAlmostEqual(os.stat(self.data / "history.json").st_mtime, old, delta=2)   # данные не менялись - не пишем
        self.assertAlmostEqual(os.stat(self.data / "session.json").st_mtime, old, delta=2)   # вход свежий - не пишем

    def test_page_files_are_readable_by_web_server_under_strict_umask(self):
        import stat
        old = os.umask(0o077)           # как было на роутере из-за чужого umask
        try:
            self.run_sync()
        finally:
            os.umask(old)
        for name in ("data.json", "offline.html"):
            mode = stat.S_IMODE(os.stat(self.www / name).st_mode)
            self.assertTrue(mode & 0o004, f"{name}: права {oct(mode)}, веб-сервер его не отдаст")
        # а вход и история при этом закрыты
        self.assertEqual(stat.S_IMODE(os.stat(self.data / "session.json").st_mode), 0o600)

    def test_rebuild_after_reboot_uses_saved_history(self):
        self.run_sync()
        self.out.unlink()                                   # как после перезагрузки роутера: оперативная память пуста
        core.rebuild(self.data, self.out)
        saved = json.loads(self.out.read_text())
        self.assertEqual(len(saved["days"]), 2)
        self.assertTrue(saved["updatedAt"])
        self.assertFalse(saved["status"]["needLogin"])
        (self.data / "session.json").unlink()
        core.rebuild(self.data, self.out)
        self.assertTrue(json.loads(self.out.read_text())["status"]["needLogin"])

    def test_server_error_keeps_data_and_reports(self):
        self.run_sync()
        bad = core.Client.diary
        core.Client.diary = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("дневник ответил 500"))
        try:
            with self.assertRaises(RuntimeError):
                self.run_sync()
        finally:
            core.Client.diary = bad
        saved = json.loads(self.out.read_text())
        self.assertFalse(saved["status"]["needLogin"])
        self.assertIn("500", saved["status"]["error"])
        self.assertEqual(len(saved["days"]), 2)


class PasswordLoginTests(unittest.TestCase):
    """Вход по логину и паролю (без Госуслуг и без Mac)."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeSite.good_token, FakeSite.only_school, FakeSite.captcha, FakeSite.password, FakeSite.login_posts = "good", None, False, "secret", 0
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data, self.out = self.root / "data", self.root / "www" / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, **kw):
        return core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11), **kw)

    def setup(self, login="kate", password="secret"):
        return core.setup_login(self.data, login, password, base=self.base)

    # --- форма входа
    def test_login_success_returns_session(self):
        jar = core.login_with_password("kate", "secret", self.base + "/login/", self.base)
        self.assertIn("DnevnikAuth_a", [c.name for c in jar])

    def test_wrong_password_is_reported(self):
        with self.assertRaises(core.LoginFailed) as cm:
            core.login_with_password("kate", "nope", self.base + "/login/", self.base)
        self.assertEqual(cm.exception.kind, "wrong")
        self.assertIn("Неверный логин или пароль", str(cm.exception))

    def test_captcha_is_reported_clearly(self):
        FakeSite.captcha = True
        with self.assertRaises(core.LoginFailed) as cm:
            core.login_with_password("kate", "secret", self.base + "/login/", self.base)
        self.assertEqual(cm.exception.kind, "captcha")

    def test_no_connection_is_not_a_wrong_password(self):
        with self.assertRaises(core.LoginFailed) as cm:
            core.login_with_password("kate", "secret", "http://127.0.0.1:9/login/", "http://127.0.0.1:9")
        self.assertEqual(cm.exception.kind, "network")

    def test_hidden_fields_are_parsed(self):
        html = '<input type="hidden" name="a.b" value="x&amp;y"><input name="t" type="text"><input value="9" name="z" type="hidden">'
        self.assertEqual(core.parse_login_form(html), {"a.b": "x&y", "z": "9"})

    def test_password_is_never_printed_in_messages(self):
        for pw in ("nope-pass-123", ""):
            ok, msg = self.setup(password=pw)
            self.assertFalse(ok)
            self.assertNotIn("nope-pass-123", msg)

    # --- номера ученика и школы
    def test_ids_are_found_and_the_right_school_is_picked(self):
        FakeSite.only_school = "2222222222"          # у второй школы уроков нет
        jar = core.login_with_password("kate", "secret", self.base + "/login/", self.base)
        ids = core.discover_ids(core.Client(jar, base=self.base), today=date(2026, 11, 11))
        self.assertEqual(ids, {"personId": "1111111111", "schoolId": "2222222222"})

    # --- настройка со страницы
    def test_setup_saves_login_and_loads_data(self):
        ok, msg = self.setup()
        self.assertTrue(ok, msg)
        for f in ("credentials.json", "session.json", "ids.json"):
            self.assertEqual(oct(os.stat(self.data / f).st_mode)[-3:], "600", f)
        self.assertEqual(json.loads((self.data / "credentials.json").read_text()), {"login": "kate", "password": "secret"})
        payload = self.sync()
        self.assertEqual(len(payload["days"]), 2)

    def test_failed_setup_saves_nothing_and_keeps_old_login(self):
        self.assertTrue(self.setup()[0])
        before = (self.data / "credentials.json").read_text()
        ok, msg = self.setup(password="wrong")
        self.assertFalse(ok)
        self.assertEqual((self.data / "credentials.json").read_text(), before)     # рабочий вход не затёрт

    def test_setup_rejects_empty_and_huge_input(self):
        self.assertFalse(self.setup("", "x")[0])
        self.assertFalse(self.setup("kate", "")[0])
        self.assertFalse(self.setup("k" * 500, "x")[0])
        self.assertFalse((self.data / "credentials.json").exists())

    # --- автоматический вход при обновлении
    def test_sync_logs_in_by_itself_when_session_expired(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").write_text("[]")           # вход «истёк»
        payload = self.sync()
        self.assertFalse(payload["status"]["needLogin"])
        names = {c["name"] for c in json.loads((self.data / "session.json").read_text())}
        self.assertIn("DnevnikAuth_a", names)                    # новый вход сохранён

    def test_sync_logs_in_with_only_a_password_and_no_session(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").unlink()
        (self.data / "ids.json").unlink()                        # даже номера ученика находит сам
        payload = self.sync()
        self.assertEqual(len(payload["days"]), 2)
        self.assertTrue((self.data / "ids.json").exists())

    def test_without_login_the_page_asks_for_it(self):
        with self.assertRaises(core.NeedLogin):
            self.sync()
        saved = json.loads(self.out.read_text())
        self.assertTrue(saved["status"]["needLogin"])
        self.assertIn("логин и пароль", saved["status"]["error"])

    def test_wrong_saved_password_is_not_retried_in_a_loop(self):
        self.assertTrue(self.setup()[0])
        FakeSite.password = "changed-on-site"                   # пароль на сайте сменили
        (self.data / "session.json").write_text("[]")
        with self.assertRaises(core.NeedLogin):
            self.sync()
        posts = FakeSite.login_posts
        with self.assertRaises(core.NeedLogin):
            self.sync()                                          # второй раз сайт не трогаем: чтобы не заблокировали
        self.assertEqual(FakeSite.login_posts, posts)
        self.assertTrue(json.loads(self.out.read_text())["status"]["needLogin"])
        # человек ввёл новый пароль: повтор разрешён
        FakeSite.password = "new-pass"
        self.assertTrue(self.setup(password="new-pass")[0])
        self.assertFalse(self.sync()["status"]["needLogin"])

    def test_no_internet_on_relogin_is_an_error_not_a_login_request(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").write_text("[]")
        with self.assertRaises(RuntimeError):
            core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11), login_url="http://127.0.0.1:9/login/")
        self.assertFalse(json.loads(self.out.read_text())["status"]["needLogin"])

    def test_rebuild_with_saved_password_does_not_ask_for_login(self):
        self.assertTrue(self.setup()[0])
        (self.data / "session.json").unlink()
        core.rebuild(self.data, self.out)
        self.assertFalse(json.loads(self.out.read_text())["status"]["needLogin"])


class StudyInfoTests(unittest.TestCase):
    """Имя ученика, школа, учебные годы, оценки по триместрам и итоговые."""

    IDS = {"personId": "1111111111", "schoolId": "2222222222"}

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeSite.good_token, FakeSite.only_school, FakeSite.captcha, FakeSite.password, FakeSite.login_posts = "good", "2222222222", False, "secret", 0
        FakeSite.paths, FakeSite.final_fails = [], False
        FakeSite.lesson_requests, FakeSite.lesson_fails = 0, False
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data, self.out = self.root / "data", self.root / "www" / "data.json"
        self.jar = core.login_with_password("kate", "secret", self.base + "/login/", self.base)
        self.client = core.Client(self.jar, base=self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_student_school_and_class_are_read_from_the_page(self):
        student, groups = core.parse_student(STATE_HTML, self.IDS)
        self.assertEqual(student, {"name": "Аня", "school": 'МБОУ "Школа № 1"', "class": "5А"})   # кавычки в названии школы целы
        self.assertNotIn("Тестова", json.dumps(student, ensure_ascii=False))                        # фамилию не храним
        self.assertEqual([(g["year"], g["className"]) for g in groups], [(2026, "5А"), (2025, "4А")])   # внеурочка не попала
        self.assertEqual([p["number"] for p in groups[0]["periods"]], [0, 1, 2])

    def test_student_of_another_person_is_ignored(self):
        student, _ = core.parse_student(STATE_HTML, {"personId": "999", "schoolId": "2222222222"})
        self.assertIsNone(student)

    def test_final_marks_are_parsed(self):
        rows, n = core.parse_final(FINAL_JSON, {"trimesters": True})
        self.assertEqual(n, 3)
        rus = next(r for r in rows if r["name"].startswith("Рус"))
        self.assertEqual(rus["name"], "Рус. язык")                       # двойной пробел убран
        self.assertEqual((rus["periods"], rus["year"]), (["4", "3", None], "4"))
        music = next(r for r in rows if r["name"] == "Музыка")
        self.assertEqual((music["periods"][0], music["year"]), ("ЗЧ", "4/5"))   # зачёт и две оценки подряд

    def test_term_marks_are_in_date_order_and_empty_subjects_dropped(self):
        term = core.parse_term(TERM_JSON, {"number": 0, "name": "1 триместр"})
        self.assertEqual(term["number"], 1)
        self.assertEqual([s["name"] for s in term["subjects"]], ["Рус. язык"])
        self.assertEqual(term["subjects"][0]["marks"], ["4", "2", "3"])
        self.assertEqual(term["subjects"][0]["average"], "3,5")

    # ---- посещаемость
    def test_attendance_totals_days_and_percent(self):
        a = core.parse_attendance(TERM_JSON)
        self.assertEqual((a["total"], a["absent"], a["ill"], a["late"], a["percent"]), (100, 3, 2, 1, 97))
        self.assertEqual([(d["date"], d["kind"], d["lessons"]) for d in a["days"]],
                         [("2025-10-06", "ill", [2, 3]), ("2025-10-20", "absent", [1]), ("2025-10-21", "late", [1])])
        self.assertEqual(a["days"][0]["name"], "Пропуск по болезни")                  # причину берём с сайта как есть

    def test_missed_lessons_know_their_subjects(self):
        a = core.parse_attendance(TERM_JSON)
        first = a["days"][0]
        self.assertEqual(first["items"], [{"n": 2, "subject": "Рус. язык"}, {"n": 3, "subject": "Рус. язык"}])   # один урок записан у двух предметов: берётся один

    def test_term_has_its_dates(self):
        term = core.parse_term(TERM_JSON, {"number": 0, "name": "1 триместр"})
        self.assertEqual((term["start"], term["finish"]), ("2025-09-01", "2025-11-28"))
        self.assertEqual(core.parse_term({"subjects": []}, {"number": 0, "name": "x"})["start"], None)

    def test_same_lesson_is_not_counted_twice(self):
        entry = {"id": "1", "lessonId": "same", "fullName": "Пропуск по болезни", "date": "1759708800", "lessonNumber": 2}
        data = {"subjects": [{"name": "a", "totalLessonNumber": 10, "lessonLogEntries": [entry]},
                             {"name": "b", "totalLessonNumber": 10, "lessonLogEntries": [dict(entry, id="2")]}]}   # один урок записан у двух предметов
        a = core.parse_attendance(data)                                                # счётчиков нет, считаем по журналу
        self.assertEqual((a["absent"], a["ill"]), (1, 1))

    def test_period_that_has_not_started_has_no_attendance(self):
        self.assertIsNone(core.parse_attendance({"subjects": [{"name": "x", "totalLessonNumber": 0}]}))
        self.assertIsNone(core.parse_attendance({"subjects": []}))

    def test_attendance_without_absences_is_100_percent(self):
        a = core.parse_attendance({"subjects": [{"name": "x", "totalLessonNumber": 50, "lessonLogEntries": []}]})
        self.assertEqual((a["absent"], a["late"], a["percent"], a["days"]), (0, 0, 100, []))

    def test_counters_are_rebuilt_from_the_log_if_site_sent_none(self):
        data = {"subjects": [{"name": "x", "totalLessonNumber": 10, "lessonLogEntries": [
            {"id": "1", "lessonId": "1", "fullName": "Пропуск по болезни", "date": "1759708800", "lessonNumber": 1},
            {"id": "2", "lessonId": "2", "fullName": "Опоздание", "date": "1759795200", "lessonNumber": 2}]}]}
        a = core.parse_attendance(data)
        self.assertEqual((a["absent"], a["ill"], a["late"], a["percent"]), (1, 1, 1, 90))

    def test_attendance_is_in_fetched_terms(self):
        _, years = core.fetch_study_info(self.client, self.IDS)
        self.assertEqual(years[1]["terms"][0]["attendance"]["absent"], 3)

    def test_years_saved_in_the_old_format_are_refetched_once(self):
        _, years = core.fetch_study_info(self.client, self.IDS, now=1_000_000)
        old = [dict(y) for y in years]
        old[1].pop("format")                                                           # как сохраняли до появления посещаемости
        FakeSite.paths = []
        core.fetch_study_info(self.client, self.IDS, old_years=old, now=1_000_000 + 3600)
        self.assertTrue([p for p in FakeSite.paths if "groupId=4001" in p])            # прошлый год запрошен заново

    def test_all_years_are_fetched(self):
        student, years = core.fetch_study_info(self.client, self.IDS)
        self.assertEqual(student["class"], "5А")
        self.assertEqual([(y["label"], y["className"], y["current"]) for y in years], [("2026/2027", "5А", True), ("2025/2026", "4А", False)])
        self.assertEqual(len(years[1]["terms"]), 3)
        self.assertEqual(years[1]["periodLabel"], "тр")
        self.assertTrue(all(y["final"] for y in years))

    def test_past_years_are_not_requested_again_for_30_days(self):
        _, years = core.fetch_study_info(self.client, self.IDS, now=1_000_000)
        FakeSite.paths = []
        core.fetch_study_info(self.client, self.IDS, old_years=years, now=1_000_000 + 3 * 3600)
        asked = [p for p in FakeSite.paths if "groupId=4001" in p]
        self.assertEqual(asked, [])                                                    # прошлый год взят из сохранённого
        self.assertTrue([p for p in FakeSite.paths if "groupId=5001" in p])            # текущий год обновился
        FakeSite.paths = []
        core.fetch_study_info(self.client, self.IDS, old_years=years, now=1_000_000 + 31 * 86400)
        self.assertTrue([p for p in FakeSite.paths if "groupId=4001" in p])            # через месяц обновляем и прошлый

    def test_failed_request_keeps_old_data(self):
        _, years = core.fetch_study_info(self.client, self.IDS)
        FakeSite.final_fails = True
        _, again = core.fetch_study_info(self.client, self.IDS, old_years=years, now=time.time() + 40 * 86400)
        self.assertEqual(len(again), 2)                                                # данные не пропали
        self.assertEqual(again[1], years[1])

    def sync(self):
        return core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11))

    def test_sync_stores_and_publishes_student_and_years(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        payload = self.sync()
        self.assertEqual(payload["student"]["name"], "Аня")
        self.assertEqual(len(payload["years"]), 2)
        for f in ("student.json", "years.json"):
            self.assertEqual(oct(os.stat(self.data / f).st_mode)[-3:], "600", f)
        saved = json.loads(self.out.read_text())
        self.assertEqual(saved["years"][0]["label"], "2026/2027")
        self.assertNotIn("Тестова", self.out.read_text())

    def test_sync_does_not_ask_for_study_info_too_often(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()
        FakeSite.paths = []
        self.sync()                                                                    # сразу второй раз
        self.assertEqual([p for p in FakeSite.paths if "/api/v2/marks/final/" in p], [])

    def test_new_data_format_is_loaded_at_once_not_in_three_hours(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()
        saved = json.loads((self.data / "years.json").read_text())
        for y in saved:
            y.pop("format")                                                            # как их сохраняла прежняя версия программы
            for term in y["terms"]:
                term.pop("attendance", None)
        core.write_private(self.data / "years.json", json.dumps(saved, ensure_ascii=False))   # а файл свежий: ему меньше 3 часов
        payload = self.sync()
        self.assertTrue(payload["years"][0]["terms"][0].get("attendance"))             # новое появилось сразу
        self.assertEqual(payload["years"][0]["format"], core.STUDY_FORMAT)

    def test_info_failure_does_not_break_the_diary(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        FakeSite.final_fails = True
        payload = self.sync()
        self.assertEqual(len(payload["days"]), 2)                                      # дневник обновился
        self.assertFalse(payload["status"]["needLogin"])

    def test_rebuild_keeps_student_and_years(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()
        self.out.unlink()
        core.rebuild(self.data, self.out)
        saved = json.loads(self.out.read_text())
        self.assertEqual(saved["student"]["school"], 'МБОУ "Школа № 1"')
        self.assertEqual(len(saved["years"]), 2)


class RatingTests(unittest.TestCase):
    """Рейтинг ученика и подробности оценок: только то, что сайт не закрыл подпиской и что не выдаёт других учеников."""

    IDS = {"personId": "1111111111", "schoolId": "2222222222"}

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeSite.good_token, FakeSite.only_school, FakeSite.captcha, FakeSite.password, FakeSite.login_posts = "good", "2222222222", False, "secret", 0
        FakeSite.paths, FakeSite.final_fails, FakeSite.rating_mode, FakeSite.with_marks, FakeSite.details_requests = [], False, "ok", True, 0
        FakeSite.lesson_requests, FakeSite.lesson_fails = 0, False
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data, self.out = self.root / "data", self.root / "www" / "data.json"
        self.client = core.Client(core.login_with_password("kate", "secret", self.base + "/login/", self.base), base=self.base)

    def tearDown(self):
        FakeSite.with_marks = False
        self.tmp.cleanup()

    def sync(self):
        return core.sync(self.data, self.out, base=self.base, today=date(2026, 11, 11))

    def requests_to(self, prefix):
        return [p for p in FakeSite.paths if p.startswith(prefix)]

    def parse(self, restricted=False, pages=True):
        pg = {"s1": subject_page("s1", restricted), "s9": subject_page("s9", restricted)} if pages else {}
        return core.parse_rating(rating_feed(restricted), rating_state(restricted), {"s1": "Рус. язык"}, pg)

    # ---- разбор рейтинга
    def test_places_totals_marks_and_period(self):
        r = self.parse()
        self.assertEqual(r["class"], {"place": 3, "trend": "up", "total": 3, "mark": "4,42", "change": "+0,01"})   # в классе 5А трое из пяти в параллели
        self.assertEqual(r["parallel"], {"place": 5, "trend": "same", "total": 5, "mark": "4,42", "change": "+0,01"})
        self.assertEqual(r["period"], "1 триместр")

    def test_parallel_board_has_places_classes_marks_and_no_people(self):
        r = self.parse()
        self.assertEqual(r["classes"], ["5Б", "5А"])
        self.assertEqual(r["board"][0], [1, 0, "4,40", "+0,02", 0, 0, 3])              # место, класс, балл, изменение, место в классе, это ученик, зверёк
        self.assertEqual(r["board"][2], [3, 1, "4,42", "+0,01", 2, 1, 9])              # строка самого ученика помечена
        self.assertEqual(sum(row[5] for row in r["board"]), 1)
        text = json.dumps(r, ensure_ascii=False)
        for foreign in ("Чужая", "Фамилия", "example.org", "avatar", "1111111111", "Тестова", "Аня"):
            self.assertNotIn(foreign, text)

    def test_subjects_have_names_places_hints_colour_and_boards(self):
        r = self.parse()
        rus, music = r["subjects"]
        self.assertEqual((rus["name"], rus["place"], rus["classPlace"], rus["mark"], rus["change"], rus["mood"], rus["trend"]),
                         ("Рус. язык", 7, 4, "3,07", "-0,07", "Average", "down"))      # название как в дневнике
        self.assertEqual(rus["hint"], ["Получите ", [5, 6], " или ", [4, 20], ", чтобы повысить балл до четверки"])
        self.assertEqual(len(rus["board"]), 5)
        self.assertEqual((music["name"], music["place"], music["classPlace"], music["mood"]), ("Музыка", 1, 2, "Average"))
        self.assertNotIn("change", music)

    def test_without_subject_pages_only_the_list_remains(self):
        r = self.parse(pages=False)
        self.assertEqual([(s["name"], s["place"]) for s in r["subjects"]], [("Рус. язык", 7), ("Музыка", 1)])
        self.assertEqual(r["subjects"][0]["classPlace"], 4)                             # место в классе из «Ленты»
        self.assertNotIn("board", r["subjects"][0])

    def test_places_without_the_statistics_page(self):
        r = core.parse_rating(rating_feed(), None)
        self.assertEqual(r["class"], {"place": 3, "trend": "up"})
        self.assertEqual(r["parallel"]["place"], 5)
        self.assertEqual([(s["name"], s.get("place"), s["classPlace"]) for s in r["subjects"]], [("Русский язык", None, 4), ("Музыка", None, 2)])
        self.assertNotIn("period", r)
        self.assertNotIn("board", r)

    def test_values_closed_by_subscription_are_skipped(self):
        r = self.parse(restricted=True)
        self.assertEqual([s["name"] for s in r["subjects"]], ["Музыка"])                # у «Рус. языка» место закрыто: предмета нет
        self.assertNotIn("hint", r["subjects"][0])                                      # подсказка закрыта: не показываем
        state = rating_state(restricted=True)
        state["groupRating"]["rankingPlaces"][1]["ranking"]["metadata"]["isRestricted"] = True
        state["parallelRating"]["rankingPlaces"][1]["ranking"]["metadata"]["isRestricted"] = True
        state["subjectTop"]["subjects"][1]["ranking"]["metadata"]["isRestricted"] = True
        feed = rating_feed(restricted=True)
        feed["personalRatingBySubjectEvents"] = []
        self.assertIsNone(core.parse_rating(feed, state))                               # всё закрыто: рейтинга нет

    def test_nothing_to_show(self):
        self.assertIsNone(core.parse_rating({}, None))
        self.assertIsNone(core.parse_rating_state("<html>нет данных</html>"))
        self.assertIsNone(core.parse_rating_state("<script>x = " + core.RATING_STATE_MARK + "{оборвано"))

    def test_odd_values_from_the_site_are_dropped(self):
        state = rating_state()
        state["commonRating"]["rankingPlaces"][0]["averageMarkChange"] = {"value": "<script>alert(1)</script>"}
        state["commonRating"]["rankingPlaces"][1]["ranking"]["place"] = "x"
        r = core.parse_rating(rating_feed(), state)
        self.assertEqual(r["board"][0][3], "")
        self.assertEqual(len(r["board"]), 4)                                            # строка без числового места пропущена

    # ---- подробности оценки
    def test_mark_details_class_distribution_and_places(self):
        d = core.parse_mark_details(mark_details_json())
        self.assertEqual(d, {"cm": [["5", 18, "Good"], ["3", 6, "Average"]], "avg": "3,07", "tr": "up", "mood": "Average", "pc": 4, "pp": 7})
        self.assertNotIn("Тестова", json.dumps(d, ensure_ascii=False))                  # имя учителя не берём: оно уже в списке учителей
        closed = core.parse_mark_details(mark_details_json(restricted=True))
        self.assertNotIn("pc", closed)
        self.assertEqual(core.parse_mark_details({}), {})

    def test_only_new_and_fresh_marks_are_asked_for(self):
        today = date(2026, 11, 11)
        now = time.mktime(datetime(2026, 11, 11, 12).timetuple())
        mk = lambda i, d: {"date": d, "lessons": [{"marks": [{"id": str(i), "value": "5", "created": d + "T08:00:00"}]}]}
        history = {"2026-11-10": mk(1, "2026-11-10"), "2026-09-10": mk(2, "2026-09-10"), "2026-11-05": mk(3, "2026-11-05"), "2026-09-11": {"date": "2026-09-11", "lessons": [{"marks": [{"value": "4"}]}]}}
        store = {"1": {"at": now - 3600}, "2": {"at": now - 99 * 3600}, "3": {"at": now - 7 * 3600}}
        self.assertEqual(core.marks_pending(history, store, now), ["3"])                # 1 свежая и недавно спрашивали; 2 старая; 3 свежая и давно; без номера не спрашиваем
        self.assertEqual(core.marks_pending(history, {}, now, limit=2), ["1", "3"])     # сначала самые новые, не больше limit
        self.assertEqual(core.marks_pending(history, {}, now, limit=10), ["1", "3", "2"])

    # ---- запросы к сайту и хранение
    def test_fetch_rating_from_the_site(self):
        r = core.fetch_rating(self.client, self.IDS, {"s1": "Рус. язык"})
        self.assertEqual(r["class"]["total"], 3)
        self.assertTrue(any("groupId=5001" in p for p in self.requests_to("/ratings/feed-widget")))     # текущий класс, не прошлогодний
        self.assertEqual(len(self.requests_to("/ratings/subject")), 2)                  # по запросу на предмет
        FakeSite.rating_mode = "nopage"
        self.assertEqual(core.fetch_rating(self.client, self.IDS)["class"], {"place": 3, "trend": "up"})
        FakeSite.good_token = "other"
        with self.assertRaises(core.NeedLogin):
            core.fetch_rating(self.client, self.IDS)

    def test_sync_stores_and_publishes_rating_and_mark_details(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        payload = self.sync()
        self.assertEqual(payload["rating"]["class"]["place"], 3)
        self.assertEqual(payload["markInfo"]["9001"]["cm"][0], ["5", 18, "Good"])
        self.assertNotIn("at", payload["markInfo"]["9001"])
        self.assertEqual(payload["days"][0]["lessons"][0]["marks"][0]["id"], "9001")     # номер оценки сохранён в дневнике
        for f in ("rating.json", "marks.json"):
            self.assertEqual(oct(os.stat(self.data / f).st_mode)[-3:], "600", f)
        text = self.out.read_text()
        for foreign in ("Чужая", "example.org", "Тестова"):
            self.assertNotIn(foreign, text)

    def test_rating_is_not_asked_too_often_but_new_marks_refresh_it(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()
        FakeSite.paths, FakeSite.details_requests = [], 0
        self.sync()                                                                      # сразу второй раз, оценок новых нет
        self.assertEqual(self.requests_to("/ratings/"), [])
        self.assertEqual(FakeSite.details_requests, 0)
        old = time.time() - 2 * 3600
        os.utime(self.data / "rating.json", (old, old))
        self.sync()                                                                      # прошло больше часа
        self.assertEqual(len(self.requests_to("/ratings/feed-widget")), 1)

    def test_fresh_mark_details_are_refreshed_after_hours(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()
        saved = json.loads((self.data / "marks.json").read_text())
        saved["items"]["9001"]["at"] = int(time.time()) - 7 * 3600
        core.write_private(self.data / "marks.json", json.dumps(saved))
        FakeSite.details_requests = 0
        self.sync()
        self.assertEqual(FakeSite.details_requests, 1)

    def test_old_history_without_mark_numbers_is_loaded_again_once(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        core.write_private(self.data / "history.json", json.dumps({"2026-11-11": {"date": "2026-11-11", "weekday": "ср", "lessons": [
            {"n": 1, "subject": "Рус. язык", "subjectId": "s1", "marks": [{"value": "5", "work": "Ответ на уроке", "created": "2026-11-11T07:00:00"}]}]}}))
        FakeSite.paths = []
        payload = self.sync()
        first = len(self.requests_to("/api/v2/marks/diary"))
        self.assertEqual(payload["days"][0]["lessons"][0]["marks"][0]["id"], "9001")
        self.assertEqual(json.loads((self.data / "marks.json").read_text())["v"], core.MARKS_FORMAT)
        FakeSite.paths = []
        self.sync()
        self.assertLess(len(self.requests_to("/api/v2/marks/diary")), first)             # второй раз весь период уже не запрашиваем

    def test_failure_keeps_the_old_rating_and_the_diary_works(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()
        old = time.time() - 2 * 3600
        os.utime(self.data / "rating.json", (old, old))
        FakeSite.rating_mode = "fail"
        payload = self.sync()
        self.assertEqual(payload["rating"]["class"]["place"], 3)                         # прежний остался
        self.assertEqual(payload["markInfo"]["9001"]["avg"], "3,07")
        self.assertEqual(len(payload["days"]), 2)
        self.assertFalse(payload["status"]["needLogin"])

    def test_closed_rating_is_remembered_and_not_asked_every_time(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()                                                                      # дневник загружен
        core.write_private(self.data / "rating.json", "{}")                              # как будто сайт рейтинг не отдаёт
        FakeSite.paths = []
        payload = self.sync()
        self.assertNotIn("rating", payload)
        self.assertEqual(self.requests_to("/ratings/feed-widget"), [])                   # {} свежий: час не прошёл

    def test_rebuild_and_failed_sync_keep_the_rating_and_details(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        self.sync()
        core.rebuild(self.data, self.out)
        saved = json.loads(self.out.read_text())
        self.assertEqual((saved["rating"]["class"]["place"], saved["markInfo"]["9001"]["pp"]), (3, 7))
        FakeSite.good_token, FakeSite.password = "other", "changed"                      # вход истёк, а пароль сменили
        with self.assertRaises(core.NeedLogin):
            self.sync()
        self.assertEqual(json.loads(self.out.read_text())["rating"]["class"]["place"], 3)

    def test_class_places_by_pupils_average(self):
        board = [[1, 0, "4,90", "", 0, 0, 1], [2, 1, "4,80", "", 1, 0, 2], [3, 1, "4,60", "", 2, 1, 3], [4, 0, "4,10", "", 0, 0, 4], [5, 2, "x", "", 0, 0, 5]]
        places, avgs = core.class_places(board)
        self.assertEqual(avgs, {0: 4.5, 1: 4.7})                                       # строки без числа не считаются
        self.assertEqual(places, {1: 1, 0: 2})
        self.assertEqual(core.class_places([[1, 0, "4,5"], [2, 1, "4,5"]])[0], {0: 1, 1: 1})   # равный балл — равное место

    def test_place_history_is_kept_per_day_and_restarts_with_a_new_period(self):
        path = self.root / "hist.json"
        r = self.parse()
        items = core.update_rating_history(path, r, date(2026, 11, 10))
        self.assertEqual(items[0]["c"], 3)
        self.assertEqual(items[0]["s"]["Рус. язык"][:2], [4, 7])
        r["class"]["place"] = 2
        items = core.update_rating_history(path, r, date(2026, 11, 10))               # тот же день: запись заменяется
        self.assertEqual([(x["d"], x["c"]) for x in items], [("2026-11-10", 2)])
        items = core.update_rating_history(path, r, date(2026, 11, 11))
        self.assertEqual(len(items), 2)
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "600")
        r["period"] = "2 триместр"
        self.assertEqual(len(core.update_rating_history(path, r, date(2026, 12, 2))), 1)   # новый период — история заново
        self.assertEqual(core.update_rating_history(path, None, date(2026, 12, 3)), [])

    def test_sync_publishes_place_history(self):
        self.assertTrue(core.setup_login(self.data, "kate", "secret", base=self.base)[0])
        payload = self.sync()
        self.assertEqual(payload["rating"]["history"][0]["d"], "2026-11-11")
        self.assertNotIn("history", json.loads((self.data / "rating.json").read_text()))   # история хранится отдельно
        self.assertIn("rating-history.json", core.LOGOUT_FILES)

    def test_logout_wipes_rating_and_mark_details(self):
        self.assertIn("rating.json", core.LOGOUT_FILES)
        self.assertIn("marks.json", core.LOGOUT_FILES)


class TeachersTests(unittest.TestCase):
    def test_short_name(self):
        self.assertEqual(core.short_name("Иванова Мария Петровна"), "Иванова М. П.")
        self.assertEqual(core.short_name("  Сидоров   Пётр "), "Сидоров П.")
        self.assertEqual(core.short_name("Иванова"), "Иванова")
        self.assertEqual(core.short_name(""), "")

    def test_parse_teachers_from_the_lesson_page(self):
        t = core.parse_teachers(TEACHER_HTML)
        self.assertEqual([x["name"] for x in t], ["Иванова Мария Петровна", "Сидоров Пётр Иванович"])    # &nbsp; и двойные пробелы убраны
        self.assertEqual(t[0], {"name": "Иванова Мария Петровна", "short": "Иванова М. П.", "role": "Учитель"})
        self.assertEqual(t[1]["role"], "")                                                              # должности может не быть

    def test_page_without_teachers_gives_empty_list(self):
        self.assertEqual(core.parse_teachers("<html>нет блока</html>"), [])
        self.assertEqual(core.parse_teachers('<div id="ContentPartTeachers"><ul class="peopleS"></ul></div>'), [])


class TeachersFetchTests(unittest.TestCase):
    IDS = {"personId": "1111111111", "schoolId": "2222222222"}

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        FakeSite.good_token, FakeSite.only_school, FakeSite.captcha, FakeSite.password = "good", "2222222222", False, "secret"
        FakeSite.lesson_requests, FakeSite.lesson_fails = 0, False
        self.client = core.Client(core.login_with_password("kate", "secret", self.base + "/login/", self.base), base=self.base)
        self.history = {
            "2026-11-09": {"date": "2026-11-09", "weekday": "пн", "lessons": [
                {"subject": "Рус. язык", "lessonId": "old-rus", "canceled": False}, {"subject": "Музыка", "lessonId": "m1", "canceled": False}]},
            "2026-11-10": {"date": "2026-11-10", "weekday": "вт", "lessons": [
                {"subject": "Рус. язык", "lessonId": "new-rus", "canceled": False}, {"subject": "Труд", "lessonId": "", "canceled": False},
                {"subject": "История", "lessonId": "h1", "canceled": True}]}}

    def test_one_request_per_subject_and_only_real_lessons(self):
        t = core.fetch_teachers(self.client, self.IDS, self.history, now=1000)
        self.assertEqual(sorted(t["bySubject"]), ["Музыка", "Рус. язык"])           # без номера урока и отменённых уроков предметов нет
        self.assertEqual(FakeSite.lesson_requests, 2)
        self.assertEqual(t["bySubject"]["Музыка"][0]["short"], "Иванова М. П.")

    def test_known_subjects_are_not_asked_again_for_a_week(self):
        t = core.fetch_teachers(self.client, self.IDS, self.history, now=1000)
        FakeSite.lesson_requests = 0
        core.fetch_teachers(self.client, self.IDS, self.history, old=t, now=1000 + 3600)
        self.assertEqual(FakeSite.lesson_requests, 0)
        core.fetch_teachers(self.client, self.IDS, self.history, old=t, now=1000 + 8 * 86400)
        self.assertEqual(FakeSite.lesson_requests, 2)                                 # через неделю проверяем заново

    def test_new_subject_is_picked_up_at_once(self):
        t = core.fetch_teachers(self.client, self.IDS, self.history, now=1000)
        self.history["2026-11-10"]["lessons"].append({"subject": "Физкультура", "lessonId": "f1", "canceled": False})
        FakeSite.lesson_requests = 0
        t2 = core.fetch_teachers(self.client, self.IDS, self.history, old=t, now=1000 + 60)
        self.assertEqual(FakeSite.lesson_requests, 1)                                 # запросили только новый предмет
        self.assertIn("Физкультура", t2["bySubject"])

    def test_failed_page_keeps_what_was_known(self):
        t = core.fetch_teachers(self.client, self.IDS, self.history, now=1000)
        FakeSite.lesson_fails = True
        t2 = core.fetch_teachers(self.client, self.IDS, self.history, old=t, now=1000 + 9 * 86400)
        self.assertEqual(t2["bySubject"], t["bySubject"])                              # прежние данные не потеряны

    def test_limit_per_run(self):
        for i in range(30):
            self.history["2026-11-10"]["lessons"].append({"subject": f"Предмет {i}", "lessonId": f"x{i}", "canceled": False})
        t = core.fetch_teachers(self.client, self.IDS, self.history, now=1000)
        self.assertEqual(FakeSite.lesson_requests, core.TEACHERS_PER_RUN)
        self.assertEqual(t["fetchedAt"], 0)                                            # не всё найдено: считаем, что ещё не закончили
        FakeSite.lesson_requests = 0
        t = core.fetch_teachers(self.client, self.IDS, self.history, old=t, now=1100)
        self.assertEqual(len(t["bySubject"]), 32)                                      # со второго раза добрали остальное
        self.assertGreater(t["fetchedAt"], 0)

    def test_only_dnevnik_pages_are_fetched_on_the_real_site(self):
        real = core.Client(core.http.cookiejar.CookieJar(), base=core.BASE)
        with self.assertRaises(RuntimeError):
            real.fetch_html("https://evil.example.com/v2/lesson?x=1")


class LogoutTests(unittest.TestCase):
    """Выход из аккаунта: два шага, резервная копия на сутки, возврат входа."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.out = Path(self.tmp.name) / "www" / "data.json"
        self.data.mkdir()
        for name in core.LOGOUT_FILES:
            (self.data / name).write_text(json.dumps({"name": name}))
        (self.data / "history.json").write_text(json.dumps({"2026-11-11": {"date": "2026-11-11", "weekday": "ср", "lessons": []}}))

    def tearDown(self):
        self.tmp.cleanup()

    def pending_age(self, seconds):
        """Делает вид, что первый шаг был seconds секунд назад."""
        pend = json.loads((self.data / "logout-pending.json").read_text())
        pend["at"] -= seconds
        (self.data / "logout-pending.json").write_text(json.dumps(pend))

    def logged_in_files(self):
        return [n for n in core.LOGOUT_FILES if (self.data / n).exists()]

    def test_confirm_right_after_begin_is_refused_and_nothing_is_removed(self):
        token = core.begin_logout(self.data)
        ok, msg = core.confirm_logout(self.data, token)                       # мгновенно: случайный двойной тап
        self.assertFalse(ok)
        self.assertEqual(len(self.logged_in_files()), len(core.LOGOUT_FILES))
        ok, _ = core.confirm_logout(self.data, token)                         # код был одноразовый
        self.assertFalse(ok)

    def test_wrong_or_missing_token_is_refused(self):
        core.begin_logout(self.data)
        self.pending_age(5)
        self.assertFalse(core.confirm_logout(self.data, "not-the-token")[0])
        self.assertFalse(core.confirm_logout(self.data, "")[0])
        self.assertFalse(core.confirm_logout(self.data, None)[0])
        self.assertEqual(len(self.logged_in_files()), len(core.LOGOUT_FILES))

    def test_stale_confirmation_is_refused(self):
        token = core.begin_logout(self.data)
        self.pending_age(300)
        self.assertFalse(core.confirm_logout(self.data, token)[0])
        self.assertEqual(len(self.logged_in_files()), len(core.LOGOUT_FILES))

    def test_proper_logout_moves_everything_to_a_private_backup(self):
        token = core.begin_logout(self.data)
        self.pending_age(5)
        ok, msg = core.confirm_logout(self.data, token)
        self.assertTrue(ok, msg)
        self.assertEqual(self.logged_in_files(), [])                          # на месте ничего не осталось
        self.assertTrue(core.backup_available(self.data))
        self.assertEqual(oct(os.stat(self.data / "backup").st_mode)[-3:], "700")
        self.assertEqual(oct(os.stat(self.data / "backup" / "session.json").st_mode)[-3:], "600")

    def test_page_after_logout_shows_no_personal_data_and_offers_restore(self):
        token = core.begin_logout(self.data)
        self.pending_age(5)
        core.confirm_logout(self.data, token)
        core.rebuild(self.data, self.out)
        saved = json.loads(self.out.read_text())
        self.assertTrue(saved["status"]["needLogin"])
        self.assertTrue(saved["status"]["canRestore"])
        self.assertEqual(saved["days"], [])
        for key in ("student", "years", "teachers"):
            self.assertNotIn(key, saved)                                       # ни оценок, ни имени, ни учителей

    def test_restore_brings_everything_back(self):
        token = core.begin_logout(self.data)
        self.pending_age(5)
        core.confirm_logout(self.data, token)
        ok, msg = core.restore_logout(self.data)
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.logged_in_files()), len(core.LOGOUT_FILES))
        self.assertFalse((self.data / "backup").exists())
        ok, msg = core.restore_logout(self.data)                               # второй раз возвращать нечего
        self.assertFalse(ok)
        self.assertIn("не выходили", msg)                                      # и сообщение честное

    def test_restore_does_not_overwrite_a_new_login(self):
        token = core.begin_logout(self.data)
        self.pending_age(5)
        core.confirm_logout(self.data, token)
        (self.data / "session.json").write_text('{"name": "new"}')             # после выхода уже вошли другим
        core.restore_logout(self.data)
        self.assertEqual(json.loads((self.data / "session.json").read_text()), {"name": "new"})

    def test_backup_lives_only_a_day(self):
        token = core.begin_logout(self.data)
        self.pending_age(5)
        core.confirm_logout(self.data, token)
        info = json.loads((self.data / "backup" / "at.json").read_text())
        info["at"] -= core.BACKUP_KEEP + 60
        (self.data / "backup" / "at.json").write_text(json.dumps(info))
        self.assertFalse(core.backup_available(self.data))
        self.assertFalse(core.restore_logout(self.data)[0])
        self.assertFalse((self.data / "backup").exists())                      # устаревшая копия удалена

    def test_rebuild_removes_an_expired_backup(self):
        token = core.begin_logout(self.data)
        self.pending_age(5)
        core.confirm_logout(self.data, token)
        info = json.loads((self.data / "backup" / "at.json").read_text())
        info["at"] -= core.BACKUP_KEEP + 60
        (self.data / "backup" / "at.json").write_text(json.dumps(info))
        core.rebuild(self.data, self.out)
        self.assertFalse((self.data / "backup").exists())
        self.assertFalse(json.loads(self.out.read_text())["status"]["canRestore"])


if __name__ == "__main__":
    unittest.main()
