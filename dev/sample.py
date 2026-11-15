"""Выдуманные данные дневника для разработки и тестов. Настоящих данных здесь нет."""
import random
from datetime import date, datetime, timedelta

SLOTS = [("8:00", "8:40"), ("8:50", "9:30"), ("9:40", "10:20"), ("10:40", "11:20"), ("11:30", "12:10"), ("12:20", "13:00")]
WEEK = [  # расписание по дням недели (пн..пт)
    ["РоВ", "Математика", "Рус. язык", "История", "Литература", "Физкультура"],
    ["Рус. язык", "Род.язык", "Математика", "Англ. язык", "ОПД", "Музыка"],
    ["Рус. язык", "Математика", "Биология", "Литература", "История"],
    ["Рус. язык", "Математика", "Литература", "История", "Физкультура", "Англ. язык"],
    ["Рус. язык", "Математика", "Труд", "Труд", "География", "Физкультура"],
]
HOMEWORK = {
    "Рус. язык": ["упр 100, 101", "выучить правило, упр 105", "-"], "Математика": ["№ 3.20 (а, б)", "принести линейку", "без задания"],
    "История": ["Читать параграф 5", "Устно ответить на вопросы"], "Литература": ["Выучить стихотворение", "-"],
    "Англ. язык": ["Учить слова", "Упр. 3 стр. 12"], "Биология": ["Нет домашнего задания"], "География": ["изучить презентацию"],
    "Физкультура": ["Комплекс 2"], "Труд": ["Подготовить листы А4"],
}
SKILL = {"Рус. язык": 3.4, "Математика": 3.6, "История": 3.8, "Литература": 4.0, "Англ. язык": 4.9, "Музыка": 5.0,
         "Физкультура": 4.9, "География": 4.8, "Биология": 4.7, "Труд": 4.9, "ОПД": 5.0, "Род.язык": 3.5, "РоВ": 0}


CLASSES = ["5Б", "5А"]


def make_rating(rnd):
    """Список параллели и рейтинг по предметам: выдуманные места, без настоящих людей."""
    def board(me_place):
        rows, mark = [], 4.95
        for i in range(1, 69):
            mark = round(mark - rnd.random() * 0.03, 2)
            cls_i = 1 if i % 2 else 0
            rows.append([i, cls_i, f"{mark:.2f}".replace(".", ","), rnd.choice(["", "+0,01", "-0,02", "+0,04"]), 0, 0, rnd.randint(1, 22)])
        k = 0
        for r in rows:
            if r[1] == 1:
                k += 1
                r[4] = k
        me = rows[me_place - 1]
        me[1], me[5], me[6] = 1, 1, 0
        return rows, me
    main, me = board(35)
    me[4] = sum(1 for r in main[:35] if r[1] == 1)
    subjects = []
    names = [("Англ. язык", 1, "Good", []), ("Музыка", 1, "Good", []), ("Труд", 23, "Bad", []), ("Математика", 47, "Good", ["В следующий раз покорим новые вершины!"]),
             ("Литература", 54, "Good", ["Получите ", [5, 12], ", чтобы повысить балл до пятерки"]),
             ("Рус. язык", 59, "Average", ["Получите ", [5, 6], " или ", [4, 20], ", чтобы повысить балл до четверки"]),
             ("Род.язык", 54, "Good", ["Обратите внимание, по этому предмету мало оценок"])]
    for name, place, md, hint in names:
        b, mine = board(place)
        subjects.append({"id": f"s-{name}", "name": name, "trend": "same", "place": place, "classPlace": max(1, place // 2), "mark": "4,20", "change": "+0,05" if place % 2 else "-0,07",
                         "mood": md, "hint": hint or ["Идеальный результат, так держать!🔥"], "board": b})
    today = date.today()
    history = [{"d": (today - timedelta(days=k)).isoformat(), "c": me[4] + (k // 5) % 3, "p": 35 + (k // 4) % 4 - 1, "k": 2 if k > 10 else 1,
                "s": {x["name"]: [x["classPlace"] + (k // 6) % 2, x["place"] + (k // 7) % 3, 2 if k > 12 else 1] for x in subjects}}
               for k in range(21, 0, -1)]
    return {"period": "1 триместр", "history": history, "class": {"place": me[4], "trend": "up", "total": 34, "mark": "4,42", "change": "+0,01"},
            "parallel": {"place": 35, "trend": "same", "total": 68, "mark": "4,42", "change": "+0,01"},
            "classes": CLASSES, "board": main, "subjects": subjects}


def make_sample(today=None, seed=7):
    rnd = random.Random(seed)
    today = today or date.today()
    start = today - timedelta(days=today.weekday()) - timedelta(weeks=6)
    days, ids = [], []
    for i in range(9 * 7):
        d = start + timedelta(days=i)
        if d.weekday() > 4:
            continue
        lessons = []
        for n, subj in enumerate(WEEK[d.weekday()], 1):
            s, e = SLOTS[n - 1]
            marks = []
            if d <= today and SKILL.get(subj) and rnd.random() < 0.35:
                v = max(2, min(5, round(rnd.gauss(SKILL[subj], 0.8))))
                marks.append({"value": str(v), "work": rnd.choice(["Ответ на уроке", "Самостоятельная работа", "Практическая работа"]),
                              "created": datetime(d.year, d.month, d.day, 12, 0).isoformat(), "id": f"{9000 + len(ids)}"})
                ids.append(subj)
            lessons.append({
                "n": n, "start": s, "end": e, "subject": subj, "subjectId": f"s-{subj}", "groupId": "g1",
                "theme": f"Тема урока ({subj})", "homework": rnd.choice(HOMEWORK.get(subj, ["-"])),
                "files": [{"id": "1", "name": "Презентация. Географические исследования", "ext": "pdf", "size": 5149565,
                           "url": "https://b5.csdnevnik.ru/demo-1.pdf"},
                          {"id": "2", "name": "Карточки к уроку", "ext": "docx", "size": 20480, "url": ""}]
                if subj == "География" else [],
                "canceled": False, "marks": marks,
            })
        days.append({"date": d.isoformat(), "weekday": ["пн", "вт", "ср", "чт", "пт"][d.weekday()], "lessons": lessons})
    subjects = ["Англ. язык", "Математика", "Музыка", "Рус. язык"]
    no_absence = {"total": 120, "absent": 0, "ill": 0, "late": 0, "percent": 100, "days": []}
    with_absence = {"total": 249, "absent": 6, "ill": 5, "late": 1, "percent": 98, "days": [
        {"date": "2025-10-06", "kind": "ill", "name": "Пропуск по болезни", "lessons": [1, 2, 3, 4, 5],
         "items": [{"n": 1, "subject": "Рус. язык"}, {"n": 2, "subject": "Математика"}, {"n": 3, "subject": "Литература"},
                   {"n": 4, "subject": "История"}, {"n": 5, "subject": "Физкультура"}]},
        {"date": "2025-10-20", "kind": "absent", "name": "Пропуск без уважительной причины", "lessons": [2],
         "items": [{"n": 2, "subject": "Математика"}]},
        {"date": "2025-10-21", "kind": "late", "name": "Опоздание", "lessons": [1], "items": [{"n": 1, "subject": "Рус. язык"}]}]}
    teachers = {n: [{"name": f"{fam} Мария Петровна", "short": f"{fam} М. П.", "role": "Учитель"}]
                for n, fam in (("Рус. язык", "Иванова"), ("Математика", "Петрова"), ("История", "Сидорова"), ("Литература", "Иванова"),
                               ("Англ. язык", "Орлова"), ("Физкультура", "Громов"), ("Музыка", "Белова"), ("Биология", "Зайцева"))}
    cur_terms = [{"number": 1, "name": "1 триместр", "attendance": no_absence,
                  "start": (start).isoformat(), "finish": (today + timedelta(days=21)).isoformat(),
                  "subjects": [{"name": n, "marks": [str(rnd.choice([3, 4, 5])) for _ in range(6)], "average": "4,2"} for n in subjects]}]
    spans = {1: ("2025-09-01", "2025-11-28"), 2: ("2025-12-01", "2026-02-27"), 3: ("2026-03-02", "2026-05-29")}
    past_terms = [{"number": k, "name": f"{k} триместр", "attendance": with_absence if k == 1 else dict(no_absence, total=243),
                   "start": spans[k][0], "finish": spans[k][1],
                   "subjects": [{"name": n, "marks": [str(rnd.choice([3, 4, 5])) for _ in range(5)], "average": "4,1"} for n in subjects]}
                  for k in (1, 2, 3)]
    return {
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "student": {"name": "Аня", "school": 'МБОУ "Школа № 1"', "class": "5А"},
        "teachers": teachers,
        "rating": make_rating(rnd),
        "markInfo": {m["id"]: {"cm": [["5", 12, "Good"], ["4", 9, "Good"], ["3", 6, "Average"], ["2", 1, "Bad"]], "avg": "4,2", "tr": "up", "mood": "Good", "pc": 5, "pp": 21}
                     for d in days for l in d["lessons"] for m in l["marks"]},
        "years": [
            {"year": 2026, "label": "2026/2027", "className": "5А", "periodLabel": "тр", "columns": 3, "current": True,
             "terms": cur_terms, "final": [{"name": n, "periods": [None, None, None], "year": None} for n in subjects]},
            {"year": 2025, "label": "2025/2026", "className": "4А", "periodLabel": "тр", "columns": 3, "current": False,
             "terms": past_terms, "final": [{"name": n, "periods": ["4", "5", "3"], "year": "4"} for n in subjects]},
        ],
    }
