"""Рейтинг и экраны по образцу приложения Дневник.ру: лента оценок на главной, «Оценки», «Ваш рейтинг», списки, окно предмета, карточка оценки, страница урока."""
import unittest

from tests.test_marks_card import MarksUiBase, page_data


class RatingUiTests(MarksUiBase):
    def screen(self, page):
        return page.inner_text("#screen")

    def test_home_has_a_strip_of_latest_marks_with_short_labels(self):
        page = self.open(tab="hw")
        self.assertEqual(page.inner_text("#main h2.strip-h"), "Последние оценки")
        tiles = page.query_selector_all(".rstrip .rtile2")
        self.assertGreaterEqual(len(tiles), 5)
        first = tiles[0].inner_text()
        self.assertRegex(first.replace("\n", " ").upper(), r"(ОТВ|С/Р|П/Р).*ЗА \d+")          # «ОТВ, за 13 нояб.»
        tiles[0].click()
        page.wait_for_selector("#modal .markmodal")
        page.close()
        self.assertEqual(self.errors, [])

    def test_marks_tab_shows_plaques_cards_hint_tile_and_streak(self):
        page = self.open()
        self.assertEqual([x.strip().split()[0] for x in page.eval_on_selector_all(".plq", "e => e.map(x => x.innerText)")], [str(page_data()["rating"]["class"]["place"]), "35"])
        text = page.inner_text("#main")
        self.assertIn("Литература", text)
        lit = page.query_selector("xpath=//div[contains(@class,'scard')][.//div[@class='sc-name' and text()='Литература']]")
        self.assertIn("можно повысить", lit.inner_text().lower())                      # плитка с подсказкой сайта
        self.assertIn("рейтинг", lit.inner_text())                                     # значки мест по предмету
        self.assertTrue(page.query_selector(".scard.mid"))                              # цвет карточки от сайта: оранжевая у «Рус. языка»
        data = page_data()
        data["days"][-3]["lessons"][0]["marks"] += [{"value": "5", "work": "Ответ", "created": "2099-01-01T12:00:00", "id": "x1"}] * 3
        page.close()
        page = self.open(data)
        self.assertTrue(page.query_selector(".streak em"))                              # серия пятёрок: «подряд!»
        self.assertEqual(page.inner_text(".streak em"), "подряд!")
        page.close()

    def test_rating_screen_lists_open_and_go_back(self):
        page = self.open()
        page.click(".plq")
        page.wait_for_selector("#screen.scr-rating")
        text = self.screen(page)
        for need in ("Ваш рейтинг", "Рейтинг общий", "Место в классе", "Место в параллели", "Рейтинг по предметам", "(1 триместр)"):
            self.assertIn(need, text)
        subjects = page.eval_on_selector_all("#screen .srow .sn", "e => e.map(x => x.textContent)")
        self.assertEqual(subjects[:2], ["Англ. язык", "Музыка"])                         # по месту в параллели
        self.assertTrue(page.query_selector("#screen .crown"))                          # у первого места корона
        page.click('#screen [data-open-board=""][data-bmode="class"]')
        page.wait_for_selector("#screen.scr-board")
        self.assertIn("Персональный рейтинг", self.screen(page))
        rows_class = len(page.query_selector_all("#screen .brow"))
        self.assertEqual(rows_class, 34)                                                # только свой класс
        self.assertIn("Показать все", self.screen(page))
        page.click("#screen [data-bmode-toggle]")
        self.assertEqual(len(page.query_selector_all("#screen .brow")), 68)             # вся параллель
        self.assertEqual(len(page.query_selector_all("#screen .brow.me")), 1)           # своя строка одна
        page.click("#screen [data-scr-back]")
        page.wait_for_selector("#screen.scr-rating")
        page.go_back()                                                                  # кнопка «назад» телефона закрывает экран
        page.wait_for_function("document.getElementById('screen').innerHTML === ''")
        self.assertFalse(page.evaluate("document.body.classList.contains('has-screen')"))
        page.close()
        self.assertEqual(self.errors, [])

    def test_escape_closes_the_top_screen(self):
        page = self.open()
        page.click(".plq")
        page.wait_for_selector("#screen.scr-rating")
        page.keyboard.press("Escape")
        page.wait_for_function("document.getElementById('screen').innerHTML === ''")
        page.close()

    def test_subject_window_and_subject_rating(self):
        page = self.open()
        page.click("xpath=//div[contains(@class,'scard')][.//div[@class='sc-name' and text()='Рус. язык']]")
        page.wait_for_selector("#screen.scr-subject")
        text = self.screen(page)
        self.assertIn("В параллели", text)
        self.assertIn("повысить балл до четверки", text)                                 # подсказка сайта целиком
        self.assertIn("Поделиться", text)
        self.assertEqual(len(page.query_selector_all("#screen .brow")), 3)               # ученик и соседи
        self.assertIn("subj mid", page.get_attribute("#screen", "class"))                 # у «Рус. языка» сайт прислал Average: оранжевый
        page.click("#screen [data-open-board]")
        page.wait_for_selector("#screen.scr-board")
        self.assertIn("Рейтинг по предмету Рус. язык", self.screen(page))
        self.assertEqual(len(page.query_selector_all("#screen .brow")), 68)
        page.close()
        self.assertEqual(self.errors, [])

    def test_mark_card_like_the_app(self):
        page = self.open()
        page.click(".scard .chips [data-mark]")
        page.wait_for_selector("#modal .markmodal")
        text = page.inner_text("#modal")
        for need in ("Выставлено", "Ваша оценка", "Одноклассники", "12 учеников", "1 ученик", "Рейтинг", "Описание урока", "Все оценки по предмету"):
            self.assertIn(need, text)
        self.assertTrue(page.query_selector("#modal .mm-nav"))                          # листание между оценками предмета
        before = page.inner_text("#modal .mm-nav span")
        page.click('#modal [data-mark-nav="1"]')
        self.assertNotEqual(page.inner_text("#modal .mm-nav span"), before)
        page.click('#modal [data-mark-nav="-1"]')
        self.assertEqual(page.inner_text("#modal .mm-nav span"), before)
        page.click("#modal [data-open-subject]")                                        # «Все оценки по предмету»
        page.wait_for_selector("#screen.scr-subject")
        self.assertEqual(page.inner_html("#modal"), "")                                 # окно закрылось
        page.close()
        self.assertEqual(self.errors, [])

    def test_lesson_page_opens_from_the_mark_card_and_homework_check_works(self):
        page = self.open()
        page.click(".scard .chips [data-mark]")
        page.wait_for_selector("#modal .markmodal")
        page.click("#modal [data-open-lesson]")
        page.wait_for_selector("#screen.scr-lesson")
        text = self.screen(page)
        for need in ("УРОК", "Отметки"):
            self.assertIn(need, text)
        check = page.query_selector("#screen [data-lesson-done]")
        if check:                                                                       # у урока есть домашка
            check.click()
            page.wait_for_selector("#screen .ls-hw.done")
            self.assertTrue(page.evaluate("Object.keys(JSON.parse(localStorage.getItem('done') || '{}')).length") >= 1)
        page.close()
        self.assertEqual(self.errors, [])

    def test_without_rating_everything_still_works(self):
        data = page_data()
        data.pop("rating"), data.pop("markInfo")
        page = self.open(data)
        self.assertFalse(page.query_selector(".plq"))                                   # нет подписки: плиток рейтинга нет
        self.assertTrue(page.query_selector(".scard"))                                  # карточки предметов есть
        self.assertFalse(page.query_selector(".scard .sbar"))
        page.click(".scard .chips [data-mark]")
        page.wait_for_selector("#modal .markmodal")
        text = page.inner_text("#modal")
        self.assertNotIn("Одноклассники", text)
        self.assertNotIn("Рейтинг", text)
        self.assertIn("Выставлено", text)                                               # дата выставления считается по дневнику
        page.click("#modal [data-open-subject]")
        page.wait_for_selector("#screen.scr-subject")
        self.assertNotIn("В параллели", self.screen(page))
        page.close()
        self.assertEqual(self.errors, [])

    def test_no_other_children_names_anywhere(self):
        page = self.open()
        page.click(".plq")
        page.wait_for_selector("#screen.scr-rating")
        page.click('#screen [data-open-board=""][data-bmode="all"]')
        page.wait_for_selector("#screen.scr-board")
        for foreign in ("Чужая", "Фамилия", "@"):
            self.assertNotIn(foreign, self.screen(page))
        page.close()

    def test_font_size_has_a_smaller_choice(self):
        page = self.open()
        page.click("#gear")
        labels = page.eval_on_selector_all('[data-set="scale"]', "e => e.map(x => x.textContent)")
        self.assertEqual(labels, ["Мини", "Мелко", "Обычно", "Крупно", "Очень"])
        page.click('[data-set="scale"][data-val="0.8"]')
        self.assertEqual(page.evaluate("document.documentElement.style.zoom"), "0.8")
        page.reload()
        page.wait_for_selector("nav.tabs")
        self.assertEqual(page.evaluate("document.documentElement.style.zoom"), "0.8")      # выбор запомнился
        page.close()

    def test_classes_view_best_subjects_and_history(self):
        page = self.open()
        self.assertIn("неделю назад", page.inner_text(".plaques"))                        # прежнее место под плиткой
        page.click(".plq")
        page.wait_for_selector("#screen.scr-rating")
        self.assertTrue(page.query_selector("#screen svg.spark polyline"))                 # график мест по дням
        self.assertIn("Ваши лучшие места по предмету", self.screen(page))
        self.assertLessEqual(len(page.query_selector_all("#screen .srow")), 3)
        page.click("#screen [data-open-subjects]")
        page.wait_for_selector("#screen.scr-subjects")
        self.assertEqual(len(page.query_selector_all("#screen .srow")), 7)                 # полный список предметов
        page.click("#screen [data-scr-back]")
        page.wait_for_selector("#screen.scr-rating")
        page.click('#screen [data-rview="cls"]')
        self.assertIn("Ваш класс 5А", self.screen(page))
        self.assertTrue(page.query_selector("#screen .cb.mine"))                          # свой класс выделен
        page.click("#screen .crow >> nth=1")
        page.wait_for_selector("#screen.scr-cls")
        text = self.screen(page)
        for need in ("Места классов", "Топ учеников 5А", "Ваш средний балл"):
            self.assertIn(need, text)
        page.close()
        self.assertEqual(self.errors, [])

    def test_mark_sheet_closes_with_phone_back_and_has_a_ring(self):
        page = self.open()
        page.click(".scard .chips [data-mark]")
        page.wait_for_selector("#modal .markmodal .donut circle")
        page.go_back()                                                                  # «назад» на телефоне закрывает карточку, а не уходит со страницы
        page.wait_for_function("document.querySelector('#modal').innerHTML.trim() === ''")
        self.assertIn("127.0.0.1", page.url)
        page.close()

    def test_tap_on_the_day_title_goes_back_to_today_and_strip_swipe_keeps_the_day(self):
        data = page_data()
        page = self.open(data, tab="hw")
        page.evaluate("setSetting('hwMode', 'pages'); render()")
        start = page.evaluate("hwDate")
        page.click('[data-nav="1"]'); page.click('[data-nav="1"]')
        self.assertNotEqual(page.evaluate("hwDate"), start)
        self.assertIn("к сегодня", page.inner_text(".nav .ttl"))
        page.click(".nav .ttl")
        self.assertEqual(page.evaluate("hwDate"), start)                                # нажали на дату — снова сегодня
        swipe = """(sel) => { const el = document.querySelector(sel), r = el.getBoundingClientRect();
            const t = (x) => new Touch({ identifier: 1, target: el, clientX: x, clientY: r.top + 30 });
            el.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, touches: [t(300)], changedTouches: [t(300)] }));
            el.dispatchEvent(new TouchEvent('touchend', { bubbles: true, touches: [], changedTouches: [t(90)] })); }"""
        page.evaluate(swipe, ".rstrip")
        self.assertEqual(page.evaluate("hwDate"), start)                                # лента оценок листается сама, день не меняется
        page.evaluate(swipe, "#plan-body")
        self.assertNotEqual(page.evaluate("hwDate"), start)                             # а по самому дню — листает
        page.close()
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
