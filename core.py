"""Общая часть: разбор данных Дневника и обновление по HTTP без браузера.

Только стандартная библиотека Python, поэтому работает и на Mac, и на роутере.
Вход через Госуслуги делается отдельно, в браузере на Mac (fetcher.py login):
оттуда берутся cookie входа (session.json) и номера ученика/школы (ids.json).
"""
import hashlib
import http.cookiejar
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

BASE = "https://dnevnik.ru"
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
COOKIE_LIFETIME = 30 * 24 * 3600
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
APP_DIR = Path(__file__).resolve().parent


class NeedLogin(Exception):
    """Вход истёк: нужно заново войти через Госуслуги."""


# ---------------------------------------------------------------- разбор данных
def ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def hm(h):
    return f"{int(h['startHour'])}:{h['startMinute']}", f"{int(h['endHour'])}:{h['endMinute']}"


def trusted_link(url):
    """Ссылки на файлы берём только с серверов Дневник.ру по защищённому соединению."""
    u = urlparse(url or "")
    h = u.hostname or ""
    return u.scheme == "https" and (h == "dnevnik.ru" or h.endswith(".dnevnik.ru") or h.endswith(".csdnevnik.ru"))


def norm_day(d):
    day = datetime.fromtimestamp(int(d["date"]), tz=timezone.utc).date()
    lessons = []
    for l in d.get("lessons", []):
        if l.get("isEmpty") and not l.get("subject"):
            continue
        start, end = hm(l["hours"]) if l.get("hours") else ("", "")
        hw = l.get("homework") or {}
        files = []
        for a in hw.get("attachments") or []:
            if isinstance(a, dict) and re.fullmatch(r"\d+", str(a.get("fileId") or "")):
                link = a.get("fileLink") or ""
                files.append({
                    "id": str(a["fileId"]),
                    "name": a.get("fileName") or a.get("name") or "файл",
                    "ext": str(a.get("fileExtension") or "").lower().lstrip("."),
                    "size": int(a.get("size") or 0),
                    "url": link if trusted_link(link) else "",   # файл открывается по ссылке, у нас не хранится
                })
        marks = []
        for w in l.get("workMarks") or []:
            for m in w.get("marks") or []:
                marks.append({"value": m.get("value"), "work": w.get("workName"), "created": m.get("createdDate"),
                              **({"id": str(m["id"])} if re.fullmatch(r"\d+", str(m.get("id") or "")) else {})})
        lessons.append({
            "n": l.get("number"), "start": start, "end": end, "lessonId": str(l.get("id") or ""),
            "subject": (l.get("subject") or {}).get("name", "?"),
            "subjectId": (l.get("subject") or {}).get("id"),
            "groupId": l.get("groupId"),
            "theme": l.get("theme") or "",
            "homework": (hw.get("text") or "").strip(),
            "files": files,
            "canceled": bool(l.get("isCanceled")),
            "marks": marks,
        })
    return {"date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "lessons": lessons}


# ---------------------------------------------------------------- cookie входа
def keep_cookie(c, also=None):
    """Нужны только cookie самого Дневника.ру. Всё остальное, что браузер насобирал при входе
    (Госуслуги, Яндекс и т.д.), мы не храним и никуда не отправляем.
    also - ещё один разрешённый адрес (нужен только тестам с поддельным сайтом)."""
    dom = (c.get("domain") if isinstance(c, dict) else c.domain).lstrip(".").lower()
    return dom == "dnevnik.ru" or dom.endswith(".dnevnik.ru") or (also is not None and dom == also)


def load_jar(path, also=None):
    """Читает cookie из session.json (формат Playwright) в обычный CookieJar."""
    p = Path(path)
    if not p.exists():
        raise NeedLogin("нет сохранённого входа")
    jar = http.cookiejar.CookieJar()
    for c in json.loads(p.read_text()):
        if not keep_cookie(c, also):
            continue
        dom = c["domain"]
        exp = int(c["expires"]) if c.get("expires", -1) and c["expires"] > 0 else None
        rest = {"HttpOnly": None} if c.get("httpOnly") else {}
        jar.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c["value"], None, False, dom, True, dom.startswith("."), c.get("path", "/"), True,
            bool(c.get("secure")), exp, False, None, None, rest))
    return jar


def jar_to_list(jar, also=None):
    now = time.time()
    out = []
    for c in jar:
        if not keep_cookie(c, also):
            continue
        out.append({
            "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
            "expires": c.expires if c.expires else now + COOKIE_LIFETIME,   # сессионные куки делаем «долгими»
            "httpOnly": c.has_nonstandard_attr("HttpOnly"), "secure": bool(c.secure),
        })
    return out


def write_private(path, text):
    """Пишет файл с правами только для владельца (там вход или личные данные)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def make_ssl_context():
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509_ca"):    # на некоторых системах нужно подсказать, где сертификаты
        for cand in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
            if os.path.exists(cand):
                ctx.load_verify_locations(cand)
                break
    return ctx


# ---------------------------------------------------------------- запросы к сайту
class Client:
    def __init__(self, jar, base=BASE, timeout=30):
        self.base, self.timeout = base, timeout
        handlers = [urllib.request.HTTPCookieProcessor(jar)]
        if base.startswith("https"):
            handlers.append(urllib.request.HTTPSHandler(context=make_ssl_context()))
        self.opener = urllib.request.build_opener(*handlers)

    def page(self, path):
        """Текст страницы сайта (нужен, чтобы найти номера ученика и школы)."""
        req = urllib.request.Request(self.base + path, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if "login.dnevnik.ru" in r.geturl():
                    raise NeedLogin("сайт отправил на страницу входа")
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NeedLogin("сессия истекла") from e
            raise RuntimeError(f"сайт ответил {e.code}") from e

    def fetch_html(self, url):
        """Страница по полному адресу (только серверы Дневник.ру)."""
        if self.base == BASE and not trusted_link(url):
            raise RuntimeError("адрес не с сайта Дневник.ру")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if "login.dnevnik.ru" in r.geturl():
                    raise NeedLogin("сайт отправил на страницу входа")
                return r.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NeedLogin("сессия истекла") from e
            raise RuntimeError(f"сайт ответил {e.code}") from e

    def json(self, path):
        req = urllib.request.Request(self.base + path, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                                 "Referer": f"{self.base}/marks"})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if "login.dnevnik.ru" in r.geturl():
                    raise NeedLogin("сайт отправил на страницу входа")
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NeedLogin("сессия истекла") from e
            raise RuntimeError(f"сайт ответил {e.code}") from e
        except ValueError as e:
            raise RuntimeError("сайт вернул не данные") from e

    def diary(self, ids, start, finish):
        url = (f"{self.base}/api/v2/marks/diary?personId={ids['personId']}&schoolId={ids['schoolId']}"
               f"&startDate={ts(start)}&finishDate={ts(finish)}&timestamp={int(time.time() * 1000)}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                                   "Referer": f"{self.base}/marks"})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                if "login.dnevnik.ru" in r.geturl():
                    raise NeedLogin("сайт отправил на страницу входа")
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise NeedLogin("сессия истекла") from e
            raise RuntimeError(f"дневник ответил {e.code}") from e
        try:
            return [norm_day(d) for d in json.loads(body)["days"]]
        except (ValueError, KeyError) as e:
            raise NeedLogin("сайт вернул не данные дневника (вход, скорее всего, истёк)") from e


# ---------------------------------------------------------------- ученик, учебные годы, итоговые оценки
def _json_at(html, start):
    """Разбирает JSON-значение (массив или объект), которое начинается в html с позиции start."""
    depth, in_str, esc = 0, False, False
    for i in range(start, len(html)):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except ValueError:
                    return None
    return None


def extract_json(html, key):
    """Достаёт JSON-значение по ключу из состояния, которое сайт встраивает в страницу."""
    m = re.search(r'"%s"\s*:\s*([\[{])' % re.escape(key), html)
    return _json_at(html, m.start(1)) if m else None


def json_objects(html, start_re):
    """Все JSON-объекты, которые в странице начинаются так, как описано в start_re."""
    out = []
    for m in re.finditer(start_re, html):
        obj = _json_at(html, m.start())
        if isinstance(obj, dict):
            out.append(obj)
    return out


def clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_student(html, ids):
    """Имя ученика (только имя), школа, список школьных классов по учебным годам. Возвращает (student, groups)."""
    mems = [m for m in (extract_json(html, "schoolMemberships") or []) if str(m.get("personId")) == str(ids["personId"])]
    main = next((m for m in mems if str(m.get("schoolId")) == str(ids["schoolId"])), mems[0] if mems else None)
    groups, seen = [], set()
    for g in json_objects(html, r'\{"id":"\d+","name":"[^"]+","studyYear":\d{4},'):
        periods = g.get("periods") or []
        if not periods or all("ОДО" in clean(p.get("name")) for p in periods):   # внеурочка и допобразование не нужны
            continue
        if g["id"] in seen:
            continue
        seen.add(g["id"])
        groups.append({"year": int(g["studyYear"]), "className": clean(g.get("name")), "groupId": str(g["id"]),
                       "trimesters": "триместр" in clean(g.get("periodTabName")).lower(),
                       "periods": [{"id": str(p["id"]), "number": int(p.get("number") or 0), "name": clean(p.get("name")),
                                    "default": bool(p.get("isDefault"))} for p in periods]})
    groups.sort(key=lambda x: -x["year"])
    if not main:
        return None, groups
    student = {"name": clean(main.get("firstName")), "school": clean(main.get("schoolName")),
               "class": groups[0]["className"] if groups else ""}
    return student, groups


def parse_final(data, group):
    """Итоговые оценки по предметам: за каждый триместр и за год."""
    rows, n = [], 3 if group["trimesters"] else 0
    for sub in data.get("subjects") or []:
        periods, year = {}, None
        for w in sub.get("finalWorks") or []:
            vals = [clean(mk.get("value")) for mk in w.get("marks") or [] if clean(mk.get("value"))]
            value = "/".join(vals) if vals else None
            if w.get("type") == "Year":
                year = value
            elif w.get("type") == "Period" and isinstance(w.get("periodNumber"), int):
                periods[w["periodNumber"]] = value
                n = max(n, w["periodNumber"] + 1)
        rows.append({"name": clean(sub.get("name")), "periods": periods, "year": year})
    n = n or 3
    for r in rows:
        r["periods"] = [r["periods"].get(i) for i in range(n)]
    rows.sort(key=lambda x: x["name"].lower())
    return rows, n


def parse_attendance(data):
    """Посещаемость за период: сколько уроков пропущено (по болезни, по другим причинам), опозданий и по каким дням."""
    subjects = data.get("subjects") or []
    total = sum(int(sub.get("totalLessonNumber") or 0) for sub in subjects)
    if not total:
        return None                                   # период ещё не начался
    absent = sum(int(sub.get("countAbsence") or 0) for sub in subjects)
    ill = sum(int(sub.get("countIllAbsence") or 0) for sub in subjects)
    late = sum(int(sub.get("countBeingLate") or 0) for sub in subjects)
    seen, groups = set(), {}
    for sub in subjects:
        for e in sub.get("lessonLogEntries") or []:
            lid = e.get("lessonId") or e.get("id")
            if lid in seen or not str(e.get("date") or "").isdigit():
                continue
            seen.add(lid)
            name = clean(e.get("fullName")) or clean(e.get("value"))
            low = name.lower()
            kind = "late" if "опозд" in low else "ill" if "болезн" in low else "absent"
            day = datetime.fromtimestamp(int(e["date"]), tz=timezone.utc).date().isoformat()
            g = groups.setdefault((day, kind), {"date": day, "kind": kind, "name": name, "lessons": [], "items": []})
            if isinstance(e.get("lessonNumber"), int):
                g["lessons"].append(e["lessonNumber"])
                g["items"].append({"n": e["lessonNumber"], "subject": clean(sub.get("name"))})
    days = sorted(groups.values(), key=lambda g: (g["date"], g["kind"]))
    for g in days:
        g["lessons"] = sorted(set(g["lessons"]))
        g["items"] = sorted({(i["n"], i["subject"]): i for i in g["items"]}.values(), key=lambda i: (i["n"], i["subject"]))
    if not absent and not late and days:              # счётчиков нет, но журнал есть: считаем по журналу
        absent = sum(max(1, len(g["lessons"])) for g in days if g["kind"] != "late")
        ill = sum(max(1, len(g["lessons"])) for g in days if g["kind"] == "ill")
        late = sum(max(1, len(g["lessons"])) for g in days if g["kind"] == "late")
    percent = max(0, min(100, round(100 * (total - absent) / total)))
    return {"total": total, "absent": absent, "ill": ill, "late": late, "percent": percent, "days": days}


def parse_term(data, period):
    """Оценки по предметам за один триместр: все оценки по порядку и средний балл."""
    subs = []
    for sub in data.get("subjects") or []:
        marks = []
        for w in sorted(sub.get("works") or [], key=lambda w: w.get("date") or 0):
            marks += [clean(m.get("value")) for m in w.get("marks") or [] if clean(m.get("value"))]
        avg = clean((sub.get("average") or {}).get("value")) or None
        if marks or avg:
            subs.append({"name": clean(sub.get("name")), "marks": marks, "average": avg})
    subs.sort(key=lambda x: x["name"].lower())
    def iso(ts_value):
        try:
            return datetime.fromtimestamp(int(ts_value), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    return {"number": period["number"] + 1, "name": period["name"], "subjects": subs, "attendance": parse_attendance(data),
            "start": iso(data.get("dateStart")), "finish": iso(data.get("dateFinish"))}


def fetch_study_info(client, ids, old_years=None, now=None):
    """Ученик, школа, все учебные годы: оценки по триместрам и итоговые.
    Прошлые годы почти не меняются: их повторно запрашиваем раз в 30 дней. Если запрос не удался, остаются прежние данные."""
    now = now or time.time()
    html = client.page(f"/marks/school/{ids['schoolId']}/student/{ids['personId']}/period")
    student, groups = parse_student(html, ids)
    old = {y.get("year"): y for y in (old_years or [])}
    years = []
    for idx, g in enumerate(groups):
        prev = old.get(g["year"])
        if idx > 0 and prev and prev.get("terms") is not None and prev.get("format") == STUDY_FORMAT and now - prev.get("fetchedAt", 0) < 30 * 86400:
            years.append(prev)
            continue
        base = f"/api/v2/marks/school/{ids['schoolId']}/person/{ids['personId']}"
        stamp = f"&timestamp={int(now * 1000)}"
        try:
            final_rows, n = parse_final(client.json(f"/api/v2/marks/final/school/{ids['schoolId']}/person/{ids['personId']}"
                                                    f"?groupId={g['groupId']}{stamp}"), g)
            terms = []
            for per in g["periods"]:
                try:
                    term = parse_term(client.json(f"{base}?groupId={g['groupId']}&periodId={per['id']}{stamp}"), per)
                except NeedLogin:
                    raise
                except Exception:
                    continue
                if term["subjects"] or term["attendance"] or per["default"]:
                    terms.append(term)
            years.append({"year": g["year"], "label": f"{g['year']}/{g['year'] + 1}", "className": g["className"],
                          "periodLabel": "тр" if g["trimesters"] else "пер.", "columns": n, "current": idx == 0,
                          "terms": terms, "final": final_rows, "fetchedAt": int(now), "format": STUDY_FORMAT})
        except NeedLogin:
            raise
        except Exception:
            if prev:
                years.append(prev)
    return student, years


# ---------------------------------------------------------------- рейтинг и подробности оценок
# Всё, что Дневник.ру сам показывает ученику на страницах «Рейтинг» и в карточке оценки. Закрытое подпиской (isRestricted)
# не берётся. Других учеников в списках знаем только по месту, классу, среднему баллу и номеру зверька-аватарки: ни имён, ни фото.
RATING_EVERY_HOURS = 1              # рейтинг пересчитывается вместе с оценками: раз в час и при новых оценках
MARKS_PER_RUN = 15                  # подробностей оценок за одно обновление не больше (остальные в следующий раз)
MARKS_RECENT_DAYS = 7               # свежие оценки перезапрашиваем: класс ещё получает оценки за ту же работу
MARKS_REFRESH_HOURS = 6
MARKS_FORMAT = 1                    # номер формата marks.json: при смене номера оценок в дневнике перезапрашиваем
RATING_TREND = {"Up": "up", "Down": "down", "Equal": "same"}
RATING_STATE_MARK = "__RATINGS__INITIAL__STATE__ = "


def _open(x):
    """Сайт показывает это значение (не закрыто подпиской). Закрытое не берём и не угадываем."""
    return not (((x or {}).get("metadata")) or {}).get("isRestricted")


def _num(x):
    return x if isinstance(x, int) and not isinstance(x, bool) and x > 0 else None


def _trend(x):
    if isinstance(x, dict):
        x = x.get("value")
    return RATING_TREND.get(x, "same")


def _change(x):
    """«+ 0,03» -> «+0,03»; всё, что не похоже на число, отбрасываем."""
    v = ((x or {}).get("value")) if isinstance(x, dict) else None
    if isinstance(v, str) and re.fullmatch(r"[+\-−]?\s?\d+([,.]\d+)?", v.strip()):
        return v.replace(" ", "").replace("−", "-")
    return ""


def _mood(x):
    return x if x in ("Good", "Average", "Bad") else ""


def parse_rating_state(html):
    """Данные страницы рейтинга («/statistics»): из них берём «из скольких», средний балл и список параллели. None, если их там нет."""
    i = html.find(RATING_STATE_MARK)
    if i < 0:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(html[i + len(RATING_STATE_MARK):])
    except ValueError:
        return None
    return state if isinstance(state, dict) else None


def parse_board(block, me_id, classes):
    """Список параллели: строки [место в параллели, номер класса в classes, средний балл, изменение, место в классе (0 нет), это ученик (0/1), зверёк].
    Имён и фото нет. classes пополняется названиями классов."""
    out = []
    for e in (block or {}).get("rankingPlaces") or []:
        place = _num((e.get("ranking") or {}).get("place")) if _open(e.get("ranking")) else None
        if not place:
            continue
        line = e.get("ratingLine") or {}
        g = clean(line.get("groupName"))
        if g not in classes:
            classes.append(g)
        mark = (e.get("averageMark") or {}).get("mark")
        mark = mark[:6] if _open(e.get("averageMark")) and isinstance(mark, str) else ""
        cp = (e.get("contextGroupRanking") or {})
        cplace = _num(cp.get("place")) if _open(cp) else None
        me = bool(e.get("isContextUser") or (me_id and str((e.get("contextPerson") or {}).get("id") or "") == me_id))
        am = re.search(r"/(\d{1,3})\.\w+$", str(line.get("imageUrl") or ""))
        out.append([place, classes.index(g), mark, _change(e.get("averageMarkChange")), cplace or 0, 1 if me else 0, int(am.group(1)) if am else 0])
    return out


def parse_hint(blocks):
    """Подсказка сайта («Получите 6×5 или 20×4, чтобы…»): список кусочков, строка или [оценка, сколько]."""
    out = []
    for b in blocks or []:
        if isinstance(b.get("text"), str) and b["text"].strip():
            out.append(re.sub(r"\s+", " ", b["text"])[:140])        # пробелы по краям нужны: куски склеиваются в одну фразу
        for k in ("mark1", "mark2"):
            m = b.get(k)
            if isinstance(m, dict) and _open(m) and _num(m.get("count")) and isinstance(m.get("value"), int):
                out.append([m["value"], m["count"]])
    return out


def parse_subject_page(r, me_id, classes):
    """Рейтинг по одному предмету (/ratings/subject): места, список параллели, средний балл, цвет и подсказка сайта."""
    r = r or {}
    info = r.get("commonSubjectInformation") or {}
    avg = r.get("averageMark") or {}
    out = {"board": parse_board(r.get("commonRating"), me_id, classes)}
    if _open(info):
        out["classPlace"] = _num(info.get("groupRatingPlace")) or 0
        out["place"] = _num(info.get("parallelRatingPlace")) or 0
    if _open(avg) and isinstance(avg.get("value"), str):
        out["mark"], out["mood"] = avg["value"][:6], _mood(avg.get("mood"))
    corr = r.get("marksCorrection") or {}
    if _open(corr):
        out["hint"] = parse_hint(corr.get("blocks"))
    return out


def parse_rating(feed, state=None, names=None, subject_pages=None):
    """Рейтинг самого ученика: плитки «в классе» и «в параллели», список параллели и по каждому предмету.

    feed: ответ «/ratings/feed-widget»; state: данные страницы рейтинга («из скольких», средний балл, список параллели, предметы);
    names: номер предмета -> название, как в дневнике; subject_pages: номер предмета -> ответ «/ratings/subject».
    Значения, закрытые подпиской, пропускаем. None, если брать нечего."""
    feed, state, names, subject_pages = feed or {}, state or {}, names or {}, subject_pages or {}
    me = str(state.get("childPersonId") or "")
    info = state.get("commonInformation") or {}
    classes = []

    def mine(block):
        for e in (block or {}).get("rankingPlaces") or []:
            if e.get("isContextUser") or (me and str((e.get("contextPerson") or {}).get("id") or "") == me):
                return e
        return None

    board = parse_board(state.get("commonRating"), me, classes)
    me_parallel = mine(state.get("commonRating"))
    my_class = ((me_parallel or {}).get("ratingLine") or {}).get("groupName")
    class_total = sum(1 for e in (state.get("commonRating") or {}).get("rankingPlaces") or [] if my_class and (e.get("ratingLine") or {}).get("groupName") == my_class)

    def side(event, rank_key, block_key, info_key, total):
        place = None
        rank = (event or {}).get(rank_key)
        if rank and _open(rank):
            place = _num(rank.get("place"))
        e = mine(state.get(block_key))
        if place is None and e and _open(e.get("ranking")):
            place = _num((e.get("ranking") or {}).get("place"))
        if place is None and _open(info):
            place = _num(info.get(info_key))
        if place is None:
            return None
        out = {"place": place, "trend": _trend((event or {}).get("trend"))}
        if total and total >= place:
            out["total"] = total
        if e:
            if _open(e.get("averageMark")) and isinstance(e["averageMark"].get("mark"), str):
                out["mark"] = e["averageMark"]["mark"][:6]
            change = _change(e.get("averageMarkChange"))
            if change:
                out["change"] = change
        return out

    out = {"class": side(feed.get("commonGroupRatingEvent"), "groupRanking", "groupRating", "groupRatingPlace", class_total),
           "parallel": side(feed.get("commonParallelRatingEvent"), "parallelRanking", "parallelRating", "parallelRatingPlace",
                            len(board) if me_parallel else 0),
           "subjects": []}
    trends = {str((e.get("subject") or {}).get("id")): _trend(e.get("trend")) for e in feed.get("personalRatingBySubjectEvents") or []}
    feed_place = {str((e.get("subject") or {}).get("id")): _num((e.get("place") or {}).get("value")) if _open(e.get("place")) else None
                  for e in feed.get("personalRatingBySubjectEvents") or []}
    top = (state.get("subjectTop") or {}).get("subjects") or []
    listed = [(str((t.get("subject") or {}).get("id")), t) for t in top if isinstance(t, dict)]
    seen = {sid for sid, _ in listed}
    for e in feed.get("personalRatingBySubjectEvents") or []:       # предмет есть в «Ленте», но нет в списке страницы
        sid = str((e.get("subject") or {}).get("id"))
        if sid not in seen:
            listed.append((sid, {"subject": e.get("subject")}))              # место в параллели тут неизвестно: в «Ленте» только место в классе
    for sid, t in listed:
        name = names.get(sid) or clean((t.get("subject") or {}).get("name"))
        if not name:
            continue
        sub = {"id": sid, "name": name, "trend": trends.get(sid, "same")}
        page = parse_subject_page(subject_pages.get(sid), me, classes) if sid in subject_pages else {}
        place = _num((t.get("ranking") or {}).get("place")) if _open(t.get("ranking")) else None
        place = place or page.get("place") or None
        cplace = page.get("classPlace") or feed_place.get(sid)
        if not place and not cplace:
            continue
        if place:
            sub["place"] = place
        if cplace:
            sub["classPlace"] = cplace
        am = t.get("averageMark") or {}
        mark = page.get("mark") or (am.get("mark")[:6] if _open(am) and isinstance(am.get("mark"), str) else "")
        if mark:
            sub["mark"] = mark
        change = _change(t.get("averageMarkChange"))
        if change:
            sub["change"] = change
        if page.get("mood"):
            sub["mood"] = page["mood"]
        if page.get("hint"):
            sub["hint"] = page["hint"]
        if page.get("board"):
            sub["board"] = page["board"]
        out["subjects"].append(sub)
    out["classes"] = classes
    if board:
        out["board"] = board
    m = re.search(r"\(([^()]{1,40})\)\s*$", clean((state.get("ratingDescription") or {}).get("body")))
    if m:
        out["period"] = m.group(1)
    return out if (out["class"] or out["parallel"] or out["subjects"]) else None



# История мест: сайт показывает только сегодняшние места, поэтому прежние копим сами (раз в день, в пределах учебного периода)
RATING_HISTORY_KEEP = 120


def _avg_num(x):
    try:
        return float(str(x).replace(",", "."))
    except (TypeError, ValueError):
        return None


def class_places(board):
    """Места классов параллели по среднему баллу их учеников (как «Рейтинг → Классы» в приложении): ({класс: место}, {класс: средний})."""
    marks = {}
    for r in board or []:
        v = _avg_num(r[2]) if len(r) > 2 else None
        if v is not None:
            marks.setdefault(r[1], []).append(v)
    avgs = {c: round(sum(v) / len(v), 2) for c, v in marks.items() if v}
    places, last, place = {}, None, 0
    for i, c in enumerate(sorted(avgs, key=lambda c: (-avgs[c], c))):
        if avgs[c] != last:
            place, last = i + 1, avgs[c]
        places[c] = place
    return places, avgs


def rating_snapshot(rating, day):
    """Одна запись истории: места ученика (в классе, в параллели), место его класса и то же по каждому предмету."""
    board = rating.get("board") or []
    me = next((r for r in board if len(r) > 5 and r[5]), None)
    snap = {"d": day.isoformat()}
    if (rating.get("class") or {}).get("place"):
        snap["c"] = rating["class"]["place"]
    if (rating.get("parallel") or {}).get("place"):
        snap["p"] = rating["parallel"]["place"]
    if me is not None:
        snap["k"] = class_places(board)[0].get(me[1], 0)
    subs = {}
    for sub in rating.get("subjects") or []:
        b = sub.get("board") or []
        mine = next((r for r in b if len(r) > 5 and r[5]), None)
        subs[sub["name"]] = [sub.get("classPlace") or 0, sub.get("place") or 0, class_places(b)[0].get(mine[1], 0) if mine else 0]
    if subs:
        snap["s"] = subs
    return snap


def update_rating_history(path, rating, day):
    """Дописывает (или заменяет сегодняшнюю) запись. С новым учебным периодом история начинается заново, как в приложении."""
    hist = read_json(path, {}) or {}
    period = (rating or {}).get("period") or ""
    items = hist.get("items") if isinstance(hist.get("items"), list) and hist.get("period") == period else []
    if not rating or not (rating.get("class") or rating.get("parallel") or rating.get("subjects")):
        return items
    snap = rating_snapshot(rating, day)
    items = [x for x in items if x.get("d") != snap["d"]] + [snap]
    items = sorted(items, key=lambda x: x["d"])[-RATING_HISTORY_KEEP:]
    new = {"period": period, "items": items}
    if new != hist:
        write_private(path, json.dumps(new, ensure_ascii=False))
    return items


def current_group(client, ids):
    """Номер текущего класса ученика на сайте (нужен для запросов рейтинга)."""
    html = client.page(f"/marks/school/{ids['schoolId']}/student/{ids['personId']}/period")
    _, groups = parse_student(html, ids)
    return groups[0]["groupId"] if groups else None


def fetch_rating(client, ids, names=None, gid=None):
    """Рейтинг ученика с сайта: то, что сайт сам показывает в «Ленте» и на странице «Рейтинг». Закрытое подпиской не получаем."""
    gid = gid or current_group(client, ids)
    if not gid:
        return None
    pid = ids["personId"]
    feed = client.json(f"/ratings/feed-widget?groupId={gid}&childPersonId={pid}&page=userfeed")
    state = None
    try:
        state = parse_rating_state(client.page(f"/statistics?childPersonId={pid}&groupId={gid}&subjectId=0&isGroup=True"))
    except NeedLogin:
        raise
    except Exception:
        pass                    # без этой страницы остаются места из «Ленты», только без «из скольких» и списка параллели
    pages = {}
    sids = [str((t.get("subject") or {}).get("id")) for t in ((state or {}).get("subjectTop") or {}).get("subjects") or [] if isinstance(t, dict)]
    sids += [str((e.get("subject") or {}).get("id")) for e in feed.get("personalRatingBySubjectEvents") or []]
    for sid in dict.fromkeys(sids):
        try:
            pages[sid] = client.json(f"/ratings/subject?childPersonId={pid}&groupId={gid}&subjectId={sid}")
        except NeedLogin:
            raise
        except Exception:
            pass                # по этому предмету останется только место в параллели
    return parse_rating(feed, state, names, pages)


def parse_mark_details(r):
    """Карточка оценки с сайта: как справился класс (сколько учеников какую оценку получили), места по предмету."""
    r = r or {}
    out = {}
    cm = []
    for i in ((r.get("classmatesMarks") or {}).get("items")) or []:
        if _open(i) and isinstance(i.get("value"), str) and _num(i.get("studentCount")):
            cm.append([clean(i["value"])[:4], i["studentCount"], _mood(i.get("mood"))])
    if cm:
        out["cm"] = cm
    avg = (r.get("subject") or {}).get("averageMark") or {}
    if isinstance(avg.get("value"), str):
        out["avg"], out["tr"], out["mood"] = avg["value"][:6], _trend({"value": avg.get("trend")}), _mood(avg.get("mood"))
    for key, w in (("pc", "subjectGroupRatingWidget"), ("pp", "subjectParallelRatingWidget")):
        pl = _num((r.get(w) or {}).get("place")) if _open(r.get(w)) else None
        if pl:
            out[key] = pl
    return out


def marks_pending(history, store, now, limit=MARKS_PER_RUN):
    """Номера оценок, по которым нужны подробности: новые и (для свежих) давно не обновлявшиеся, сначала самые новые."""
    todo = []
    for d in history.values():
        for l in d["lessons"]:
            for m in l.get("marks") or []:
                mid = m.get("id")
                if not mid:
                    continue
                have = store.get(mid)
                created = str(m.get("created") or d["date"])[:10]
                recent = (date.fromisoformat(d["date"]) >= date.fromtimestamp(now) - timedelta(days=MARKS_RECENT_DAYS)) if d.get("date") else False
                if have is None or (recent and now - have.get("at", 0) >= MARKS_REFRESH_HOURS * 3600):
                    todo.append((created, mid))
    todo.sort(reverse=True)
    return [mid for _, mid in todo[:limit]]


def fetch_mark_details(client, ids, gid, history, store, now=None, limit=MARKS_PER_RUN):
    """Дополняет store (номер оценки -> подробности). Сбой по одной оценке не мешает остальным; вход истёк -> NeedLogin."""
    now = now or time.time()
    for mid in marks_pending(history, store, now, limit):
        try:
            r = client.json(f"/ratings/marks-details?childPersonId={ids['personId']}&groupId={gid}&markId={mid}")
        except NeedLogin:
            raise
        except Exception:
            continue
        det = parse_mark_details(r)
        det["at"] = int(now)
        store[mid] = det
    present = {m.get("id") for d in history.values() for l in d["lessons"] for m in l.get("marks") or []}
    for mid in [k for k in store if k not in present]:
        del store[mid]
    return store


# ---------------------------------------------------------------- учителя
TEACHERS_EVERY_HOURS = 24 * 7       # учителя меняются редко: раз в неделю
TEACHERS_PER_RUN = 20               # за один раз не больше 20 запросов к сайту


def short_name(full):
    """«Иванова Мария Петровна» -> «Иванова М. П.»"""
    parts = clean(full).split(" ")
    return " ".join([parts[0]] + [p[0] + "." for p in parts[1:] if p]) if parts and parts[0] else ""


def parse_teachers(html):
    """Учителя со страницы урока: блок «Учитель» с ФИО и должностью."""
    m = re.search(r'id="ContentPartTeachers".*?<ul class="peopleS">(.*?)</ul>', html, re.S)
    if not m:
        return []
    out = []
    for li in re.findall(r"<li>(.*?)</li>", m.group(1), re.S):
        name = re.search(r'<a [^>]*class="u"[^>]*>([^<]+)</a>', li)
        if not name:
            continue
        role = re.search(r"<p>([^<]*)</p>", li)
        full = clean(name.group(1).replace("&nbsp;", " "))
        out.append({"name": full, "short": short_name(full), "role": clean(role.group(1)) if role else ""})
    return out


def lesson_url(base, school_id, lesson_id):
    host = "https://schools.dnevnik.ru" if base == BASE else base
    return f"{host}/v2/lesson?school={school_id}&lesson={lesson_id}"


def fetch_teachers(client, ids, history, old=None, now=None):
    """Учитель каждого предмета: берём по одному свежему уроку предмета. Уже найденные не запрашиваем повторно."""
    now = now or time.time()
    old = old or {}
    by_subject = dict(old.get("bySubject") or {})
    latest = {}
    for day in sorted(history, reverse=True):
        for l in history[day]["lessons"]:
            if l.get("lessonId") and not l.get("canceled") and l["subject"] not in latest:
                latest[l["subject"]] = l["lessonId"]
    fresh = now - old.get("fetchedAt", 0) < TEACHERS_EVERY_HOURS * 3600
    todo = [(name, lid) for name, lid in latest.items() if not (fresh and name in by_subject)]
    done = 0
    for name, lid in todo[:TEACHERS_PER_RUN]:
        try:
            html = client.fetch_html(lesson_url(client.base, ids["schoolId"], lid))
        except NeedLogin:
            raise
        except Exception:
            continue
        teachers = parse_teachers(html)
        if teachers or name not in by_subject:
            by_subject[name] = teachers
        done += 1
    complete = len(todo) <= TEACHERS_PER_RUN
    return {"fetchedAt": int(now) if complete else old.get("fetchedAt", 0), "bySubject": by_subject}


# ---------------------------------------------------------------- выход из аккаунта
LOGOUT_FILES = ["session.json", "credentials.json", "ids.json", "history.json", "student.json", "years.json", "teachers.json", "rating.json", "marks.json", "rating-history.json",
                "login-state.json"]
BACKUP_KEEP = 24 * 3600        # после выхода прежний вход можно вернуть в течение суток
LOGOUT_MIN_WAIT = 2            # второе подтверждение раньше, чем через 2 секунды, не принимается
LOGOUT_MAX_WAIT = 120          # и позже, чем через 2 минуты, тоже


def _rm_tree(path):
    path = Path(path)
    if not path.exists():
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for f in files:
            os.remove(os.path.join(root, f))
        for d in dirs:
            os.rmdir(os.path.join(root, d))
    os.rmdir(path)


def backup_available(data_dir):
    info = read_json(Path(data_dir) / "backup" / "at.json", None)
    return bool(info) and time.time() - info.get("at", 0) < BACKUP_KEEP


def purge_old_backup(data_dir):
    """Копия прежнего входа живёт сутки, потом удаляется."""
    backup = Path(data_dir) / "backup"
    if backup.exists() and not backup_available(data_dir):
        _rm_tree(backup)


def begin_logout(data_dir):
    """Первое подтверждение выхода: выдаёт разовый код. Сам по себе он ничего не удаляет."""
    data_dir = Path(data_dir)
    token = os.urandom(8).hex()
    write_private(data_dir / "logout-pending.json", json.dumps({"token": token, "at": time.time()}))
    return token


def confirm_logout(data_dir, token):
    """Второе подтверждение: выход возможен только с кодом первого шага и не раньше чем через пару секунд после него.
    Вход и сохранённые данные не стираются, а переезжают в резервную копию на сутки. Возвращает (успех, сообщение)."""
    data_dir = Path(data_dir)
    pending = read_json(data_dir / "logout-pending.json", None)
    try:
        (data_dir / "logout-pending.json").unlink()          # код одноразовый
    except OSError:
        pass
    if not pending or not token or pending.get("token") != token:
        return False, "Подтверждение не принято. Начните выход заново."
    age = time.time() - pending.get("at", 0)
    if age < LOGOUT_MIN_WAIT:
        return False, "Слишком быстро. Нажмите ещё раз, когда прочитаете вопрос."
    if age > LOGOUT_MAX_WAIT:
        return False, "Подтверждение устарело. Начните выход заново."
    backup = data_dir / "backup"
    _rm_tree(backup)
    backup.mkdir(mode=0o700, parents=True)
    os.chmod(backup, 0o700)
    for name in LOGOUT_FILES:
        if (data_dir / name).exists():
            os.replace(data_dir / name, backup / name)
            os.chmod(backup / name, 0o600)                 # в копии, как и везде, читает только владелец
    write_private(backup / "at.json", json.dumps({"at": time.time()}))
    return True, "Вы вышли из аккаунта. Прежний вход можно вернуть в течение суток."


def restore_logout(data_dir):
    """Возвращает вход, который был до выхода (в течение суток)."""
    data_dir = Path(data_dir)
    if not (data_dir / "backup").exists():
        return False, "Возвращать нечего: из аккаунта не выходили."
    if not backup_available(data_dir):
        _rm_tree(data_dir / "backup")
        return False, "Прежнего входа уже нет: прошло больше суток."
    backup = data_dir / "backup"
    for name in LOGOUT_FILES:
        if (backup / name).exists() and not (data_dir / name).exists():
            os.replace(backup / name, data_dir / name)
    _rm_tree(backup)
    return True, "Прежний вход возвращён. Загружаю дневник."


# ---------------------------------------------------------------- вход по логину и паролю
LOGIN_URL = "https://login.dnevnik.ru/login/"
STUDY_FORMAT = 3            # номер формата данных по годам: при смене сохранённое перезапрашивается
INFO_EVERY_HOURS = 3        # ученика и итоговые оценки обновляем не чаще раза в 3 часа
RELOGIN_PAUSE = 6 * 3600   # после неудачного входа повторяем не раньше чем через 6 часов: чтобы не заблокировали учётную запись


class LoginFailed(Exception):
    """Не удалось войти. kind: wrong (неверный логин/пароль), captcha, network, unknown."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _attrs(tag):
    return {m.group(1).lower(): (m.group(3) if m.group(3) is not None else (m.group(4) or ""))
            for m in re.finditer(r"""([\w:.-]+)\s*=\s*("([^"]*)"|'([^']*)')""", tag)}


def parse_login_form(page_html):
    """Скрытые поля формы входа (сайт может менять их значения, поэтому берём каждый раз со страницы)."""
    fields = {}
    for tag in re.findall(r"<input\b[^>]*>", page_html, re.I):
        a = _attrs(tag)
        if a.get("type", "").lower() == "hidden" and a.get("name"):
            fields[a["name"]] = a.get("value", "").replace("&amp;", "&")
    return fields


def login_with_password(login, password, login_url=LOGIN_URL, base=BASE, timeout=30):
    """Входит в Дневник.ру обычным логином и паролем (как форма на сайте). Возвращает CookieJar со входом."""
    jar = http.cookiejar.CookieJar()
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if login_url.startswith("https"):
        handlers.append(urllib.request.HTTPSHandler(context=make_ssl_context()))
    opener = urllib.request.build_opener(*handlers)
    headers = {"User-Agent": USER_AGENT}
    body = ""
    try:
        page = opener.open(urllib.request.Request(login_url, headers=headers), timeout=timeout).read().decode("utf-8", "ignore")
        data = parse_login_form(page)
        data.update({"login": login, "password": password})
        if not data.get("ReturnUrl"):
            data["ReturnUrl"] = base + "/userfeed"
        req = urllib.request.Request(login_url, data=urllib.parse.urlencode(data).encode(),
                                     headers={**headers, "Content-Type": "application/x-www-form-urlencoded", "Referer": login_url})
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError) as e:
        raise LoginFailed("network", "Нет связи с сайтом Дневник.ру. Проверьте интернет на роутере.") from e
    if any(c.name == "DnevnikAuth_a" for c in jar):
        return jar
    if parse_login_form(body).get("Captcha.Id"):
        raise LoginFailed("captcha", "Сайт просит ввести картинку-капчу. Один раз войдите на dnevnik.ru в браузере "
                                     "(введите капчу), потом повторите здесь.")
    if "Ошибка в логине или пароле" in body or 'name="password"' in body:
        raise LoginFailed("wrong", "Неверный логин или пароль. Нужны логин и пароль самого Дневник.ру, не Госуслуг: если пароля нет, задайте его в профиле на dnevnik.ru (Настройки → Логин и пароль → Изменить).")
    raise LoginFailed("unknown", "Сайт не принял вход. Попробуйте позже.")


def discover_ids(client, today=None):
    """Находит номера ученика и школы на страницах сайта (раньше их показывал браузер)."""
    today = today or date.today()
    html = client.page("/marks")
    persons = list(dict.fromkeys(re.findall(r"childPersonId=(\d{8,})", html)))
    schools = list(dict.fromkeys(re.findall(r"[?&]school=(\d{8,})", html) + re.findall(r"schoolId[=:\"']+(\d{8,})", html)))
    if not persons or not schools:
        raise RuntimeError("не нашёл на сайте номера ученика и школы")
    for pid in persons:               # у школьной учётной записи ученик один; у родителя берём первого ребёнка
        for sid in schools:
            days = client.diary({"personId": pid, "schoolId": sid}, today - timedelta(days=30), today + timedelta(days=7))
            if any(d["lessons"] for d in days):
                return {"personId": pid, "schoolId": sid}
    return {"personId": persons[0], "schoolId": schools[0]}


def setup_login(data_dir, login, password, base=BASE, login_url=None):
    """Проверяет логин и пароль на сайте и, если подошли, сохраняет их (файл закрыт правами 600).
    Возвращает (успех, сообщение для человека). Пароль никуда не печатается."""
    login, password = (login or "").strip(), password or ""
    if not login or not password:
        return False, "Введите логин и пароль."
    if len(login) > 200 or len(password) > 200:
        return False, "Слишком длинный логин или пароль."
    data_dir = Path(data_dir)
    login_url = login_url or (LOGIN_URL if base == BASE else base + "/login/")
    try:
        jar = login_with_password(login, password, login_url, base)
        extra = urlparse(base).hostname if base != BASE else None
        ids = discover_ids(Client(jar, base=base), today=None)
    except LoginFailed as e:
        return False, str(e)
    except NeedLogin:
        return False, "Сайт принял вход, но не открыл дневник. Попробуйте ещё раз."
    except Exception as e:
        return False, f"Вход прошёл, но данные ученика не нашлись ({type(e).__name__}). Напишите разработчику."
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    write_private(data_dir / "credentials.json", json.dumps({"login": login, "password": password}, ensure_ascii=False))
    write_private(data_dir / "session.json", json.dumps(jar_to_list(jar, also=extra), ensure_ascii=False))
    write_private(data_dir / "ids.json", json.dumps(ids))
    try:
        (data_dir / "login-state.json").unlink()
    except OSError:
        pass
    return True, "Вход выполнен. Загружаю дневник, это займёт около минуты."


# ---------------------------------------------------------------- обновление
def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def public_payload(history, updated_at, status, cfg=None, version=None, student=None, years=None, teachers=None, rating=None, mark_info=None, rating_history=None):
    days = [history[k] for k in sorted(history)]
    out = {"updatedAt": updated_at, "days": days, "status": status, "config": cfg or {}}
    if student:
        out["student"] = student
    if years:
        out["years"] = years
    if teachers and teachers.get("bySubject"):
        out["teachers"] = teachers["bySubject"]
    if rating:
        out["rating"] = dict(rating, history=rating_history[-60:]) if rating_history else rating
    if mark_info:
        ids_now = {m.get("id") for d in history.values() for l in d["lessons"] for m in l.get("marks") or []}
        info = {k: {a: b for a, b in v.items() if a != "at"} for k, v in mark_info.items() if k in ids_now}
        if info:
            out["markInfo"] = info
    if version:
        out["version"] = version
    return out


def write_public(out_path, payload):
    """Данные для страницы. Кладём туда же копию «для офлайна», если рядом есть index.html."""
    text = json.dumps(payload, ensure_ascii=False)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o644)       # это содержимое страницы: веб-сервер должен его отдавать при любом umask
    os.replace(tmp, out_path)
    index = out_path.parent / "index.html"
    if index.exists():
        blob = text.replace("</", "<\\/")
        tmp = out_path.with_name("offline.html.tmp")
        tmp.write_text(index.read_text(encoding="utf-8").replace("/*INLINE_DATA*/null", blob), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, out_path.with_name("offline.html"))


def sync(data_dir, out_path, base=BASE, today=None, session_save_hours=0, config_path=None, version_path=None, login_url=None):
    """Обновляет дневник. Возвращает словарь для страницы. Бросает NeedLogin, если войти не получилось.

    Если сохранённый вход истёк, а на роутере есть логин и пароль (credentials.json), заходит заново сам.

    data_dir: тут лежат session.json, ids.json, history.json, credentials.json (то, что должно пережить перезагрузку)
    out_path: куда писать data.json для страницы (на роутере это оперативная память)
    session_save_hours: как часто перезаписывать session.json (на роутере экономим флеш-память)
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)   # тут вход и личные данные: чужим пользователям компьютера не видно
    today = today or date.today()
    cfg = read_json(config_path, {}) if config_path else {}
    version = Path(version_path).read_text().strip() if version_path and Path(version_path).exists() else None
    history_path, session_path = data_dir / "history.json", data_dir / "session.json"
    ids_path, cred_path, state_path = data_dir / "ids.json", data_dir / "credentials.json", data_dir / "login-state.json"
    history = read_json(history_path, {})
    updated_prev = read_json(out_path, {}).get("updatedAt")
    extra = urlparse(base).hostname if base != BASE else None   # у настоящего сайта разрешён только Дневник.ру
    login_url = login_url or (LOGIN_URL if base == BASE else base + "/login/")

    purge_old_backup(data_dir)
    student_path, years_path, teachers_path = data_dir / "student.json", data_dir / "years.json", data_dir / "teachers.json"
    student, years, teachers = read_json(student_path, None), read_json(years_path, None), read_json(teachers_path, None)
    rating_path, marks_path = data_dir / "rating.json", data_dir / "marks.json"
    rating = read_json(rating_path, None)
    rhist_path = data_dir / "rating-history.json"
    rating_history = (read_json(rhist_path, {}) or {}).get("items") or []
    marks_file = read_json(marks_path, {})
    marks_store = marks_file.get("items") if isinstance(marks_file.get("items"), dict) else {}

    def fail(err, kind):
        status = {"needLogin": kind == "login", "error": str(err), "syncing": False, "canRestore": backup_available(data_dir)}
        write_public(out_path, public_payload(history, updated_prev, status, cfg, version, student, years, teachers, rating, marks_store, rating_history))

    def relogin():
        """Заходит заново по сохранённому логину и паролю."""
        creds = read_json(cred_path, None)
        if not creds:
            raise NeedLogin("Введите логин и пароль от Дневник.ру.")
        state = read_json(state_path, {})
        failed_at = state.get("failedAt", 0)
        if time.time() - failed_at < RELOGIN_PAUSE and cred_path.stat().st_mtime <= failed_at:
            raise NeedLogin(state.get("message") or "Вход не удался.")    # не долбим сайт неверным паролем
        try:
            jar = login_with_password(creds.get("login", ""), creds.get("password", ""), login_url, base)
        except LoginFailed as e:
            if e.kind == "network":
                raise RuntimeError(str(e)) from e                          # нет интернета: это не повод просить ввести пароль
            write_private(state_path, json.dumps({"failedAt": time.time(), "message": str(e)}, ensure_ascii=False))
            raise NeedLogin(f"{e} Введите логин и пароль заново.") from e
        try:
            state_path.unlink()
        except OSError:
            pass
        return jar

    def fetch(jar, ids):
        client = Client(jar, base=base)
        if not ids:
            ids = discover_ids(client, today)
            write_private(ids_path, json.dumps(ids))
        ids_known = marks_file.get("v") == MARKS_FORMAT           # у прежних версий оценки сохранены без номеров: один раз перезапрашиваем весь период
        first = (today - timedelta(days=14)) if history and ids_known else date(today.year if today.month >= 9 else today.year - 1, 9, 1)
        cur = first - timedelta(days=first.weekday())
        while cur <= today + timedelta(days=21):
            for d in client.diary(ids, cur, cur + timedelta(days=7)):
                history[d["date"]] = d
            cur += timedelta(days=7)

    before = hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest()
    relogged = False
    try:
        ids = read_json(ids_path, None)
        try:
            jar = load_jar(session_path, also=extra)
            fetch(jar, ids)
        except NeedLogin:
            jar = relogin()
            relogged = True
            fetch(jar, read_json(ids_path, None))
    except NeedLogin as e:
        fail(e, "login")
        raise
    except Exception as e:
        fail(e, "error")
        raise

    history_changed = hashlib.sha256(json.dumps(history, sort_keys=True).encode()).hexdigest() != before
    if history_changed:
        write_private(history_path, json.dumps(history, ensure_ascii=False))     # пишем, только если что-то изменилось
    # ученик, школа и итоговые оценки по годам: не чаще раза в 3 часа; сбой не мешает дневнику
    info_age_h = (time.time() - years_path.stat().st_mtime) / 3600 if years_path.exists() and student else 1e9
    stale_format = any(y.get("format") != STUDY_FORMAT for y in (years or []))     # данные сохранены старой версией программы
    if info_age_h >= INFO_EVERY_HOURS or stale_format:
        try:
            ids = read_json(ids_path, None)
            new_student, new_years = fetch_study_info(Client(jar, base=base), ids, years)
            for path, new, old in ((student_path, new_student, student), (years_path, new_years, years)):
                if new and new != old:
                    write_private(path, json.dumps(new, ensure_ascii=False))
                elif new:
                    os.utime(path)       # данные те же: просто отмечаем, что проверили
            student, years = new_student or student, new_years or years
        except NeedLogin:
            pass
        except Exception:
            pass
    # учителя по предметам: нужные запрашиваем по одному уроку, найденные хранятся неделю
    try:
        ids = read_json(ids_path, None)
        new_teachers = fetch_teachers(Client(jar, base=base), ids, history, teachers)
        if new_teachers.get("bySubject") and new_teachers != teachers:
            write_private(teachers_path, json.dumps(new_teachers, ensure_ascii=False))
        teachers = new_teachers
    except Exception:
        pass
    # рейтинг и подробности оценок: рейтинг раз в час и сразу при новых оценках, подробности по 15 оценок за раз; сбой не мешает дневнику
    rating_age_h = (time.time() - rating_path.stat().st_mtime) / 3600 if rating_path.exists() else 1e9
    rating_due = history_changed or rating_age_h >= RATING_EVERY_HOURS
    marks_due = bool(marks_pending(history, marks_store, time.time()))
    if rating_due or marks_due or marks_file.get("v") != MARKS_FORMAT:
        try:
            ids = read_json(ids_path, None)
            site = Client(jar, base=base)
            gid = current_group(site, ids)
            if rating_due and gid:
                names = {l["subjectId"]: l["subject"] for d in history.values() for l in d["lessons"] if l.get("subjectId")}
                new_rating = fetch_rating(site, ids, names, gid) or {}     # {}: сайт рейтинг не отдаёт (нет подписки); помним, чтобы не спрашивать каждый раз
                if new_rating != (rating or {}) or not rating_path.exists():
                    write_private(rating_path, json.dumps(new_rating, ensure_ascii=False))
                else:
                    os.utime(rating_path)
                rating = new_rating or None
                rating_history = update_rating_history(rhist_path, rating, today)
            if gid:
                fetch_mark_details(site, ids, gid, history, marks_store)
            write_private(marks_path, json.dumps({"v": MARKS_FORMAT, "items": marks_store}, ensure_ascii=False))
        except Exception:
            pass
    age_h = (time.time() - session_path.stat().st_mtime) / 3600 if session_path.exists() else 1e9
    if relogged or age_h >= session_save_hours:
        write_private(session_path, json.dumps(jar_to_list(jar, also=extra), ensure_ascii=False))
    payload = public_payload(history, datetime.now().isoformat(timespec="seconds"),
                             {"needLogin": False, "error": None, "syncing": False, "canRestore": False}, cfg, version, student, years, teachers, rating, marks_store, rating_history)
    write_public(out_path, payload)
    return payload


def rebuild(data_dir, out_path, config_path=None, version_path=None):
    """Собирает данные для страницы из сохранённой истории без обращения к сайту (после перезагрузки роутера)."""
    data_dir = Path(data_dir)
    hp = data_dir / "history.json"
    history = read_json(hp, {})
    updated = datetime.fromtimestamp(hp.stat().st_mtime).isoformat(timespec="seconds") if hp.exists() else None
    cfg = read_json(config_path, {}) if config_path else {}
    version = Path(version_path).read_text().strip() if version_path and Path(version_path).exists() else None
    purge_old_backup(data_dir)
    has_login = (data_dir / "session.json").exists() or (data_dir / "credentials.json").exists()
    status = {"needLogin": not has_login, "error": None if has_login else "Введите логин и пароль от Дневник.ру.", "syncing": False,
              "canRestore": backup_available(data_dir)}
    write_public(out_path, public_payload(history, updated, status, cfg, version, read_json(data_dir / "student.json", None),
                                          read_json(data_dir / "years.json", None), read_json(data_dir / "teachers.json", None),
                                          read_json(data_dir / "rating.json", None),
                                          (read_json(data_dir / "marks.json", {}) or {}).get("items"),
                                          (read_json(data_dir / "rating-history.json", {}) or {}).get("items")))
