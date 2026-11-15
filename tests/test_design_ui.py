"""Оформление: рисованные значки и картинки вместо смайликов, движение, доступность. Проверяется настоящим браузером."""
import re
import unittest

from tests.test_marks_card import MarksUiBase


class IconsAndArtTests(MarksUiBase):
    def test_subjects_have_drawn_icons_not_emoji(self):
        page = self.open(tab="plan")
        icons = page.eval_on_selector_all(".lesson .ic", "e => e.map(x => [x.querySelector('svg.si') !== null, x.textContent.trim()])")
        self.assertTrue(icons)
        for has_svg, text in icons:
            self.assertTrue(has_svg)
            self.assertEqual(text, "")                                             # внутри только рисунок, смайлика нет
        self.assertEqual(self.errors, [])

    def test_every_known_subject_has_its_own_icon_and_unknown_ones_get_a_book(self):
        page = self.open(tab="hw")
        names = ["Русский язык", "Математика", "Геометрия", "Литература", "Английский язык", "История", "География", "Биология",
                 "Музыка", "ИЗО", "Физкультура", "Труд", "Информатика", "РоВ", "Родной язык", "ОПД", "Что-то новое"]
        icons = page.evaluate("(names) => names.map((n) => subj(n).icon)", names)
        self.assertEqual(len(set(icons[:-1])), 16)                                  # у каждого из 16 предметов свой значок
        self.assertEqual(icons[-1], icons[0])                                       # неизвестный предмет: книга, как у русского
        self.assertTrue(all(i.startswith("<svg") and "aria-hidden" in i for i in icons))

    def test_each_day_of_the_week_has_its_own_picture(self):
        page = self.open(tab="hw")
        scenes = page.evaluate("""() => [...Array(7).keys()].map((i) => scene(`2026-11-${String(16 + i).padStart(2, "0")}`))""")
        self.assertEqual(len(set(scenes)), 7)
        self.assertTrue(all("viewBox" in s and 'aria-hidden="true"' in s for s in scenes))

    def test_day_cards_show_a_picture_that_does_not_catch_taps(self):
        page = self.open(tab="hw")
        self.assertGreaterEqual(page.locator(".day > svg.scene").count(), 1)
        self.assertEqual(page.eval_on_selector(".day > svg.scene", "e => getComputedStyle(e).pointerEvents"), "none")
        page.click(".day .lhead")                                                    # нажатие по уроку работает как раньше
        page.wait_for_selector(".lesson2.open")
        self.assertEqual(self.errors, [])

    def test_empty_states_have_a_picture_with_the_right_glyph(self):
        page = self.open(tab="hw")
        html = page.evaluate("""() => emptyArt("calendar")""")
        self.assertIn('class="em art"', html)
        self.assertNotRegex(html, r"[\U0001F300-\U0001FAFF]")                        # смайликов нет
        self.assertIn("<svg", page.evaluate("""() => emptyArt("нет-такого")"""))     # неизвестное название: запасной значок, без ошибок

    def test_subject_card_watermark_is_a_drawing_and_stays_out_of_the_way(self):
        page = self.open(tab="marks")
        self.assertTrue(page.locator(".scard .wm svg.si").count() >= 1)
        self.assertEqual(page.eval_on_selector(".scard .wm", "e => getComputedStyle(e).pointerEvents"), "none")


class PlacementTests(MarksUiBase):
    def test_animation_never_replaces_the_place_of_a_figure(self):
        """У рисунка место задано в родительской группе, а покачивается вложенная: иначе человечек «улетает» в левый угол."""
        page = self.open(tab="hw")
        bad = page.evaluate("""() => { const svg = document.createElement('div'); svg.innerHTML = schoolScene() + SCENES.map((s, i) => scene('2026-11-' + (16 + i))).join('');
          document.body.append(svg);
          return [...svg.querySelectorAll('.kid')].filter((k) => k.hasAttribute('transform') || !k.parentElement.getAttribute('transform')).length; }""")
        self.assertEqual(bad, 0)

    def test_figures_stand_in_their_place_on_the_login_picture(self):
        page = self.open(tab="hw")
        xs = page.evaluate("""() => { const d = document.createElement('div'); d.style.cssText = 'position:fixed;left:0;top:0;width:360px;height:132px'; d.innerHTML = schoolScene(); document.body.append(d);
          const r = d.getBoundingClientRect(); return [...d.querySelectorAll('.kid')].map((k) => Math.round(((k.getBoundingClientRect().x + k.getBoundingClientRect().width / 2) - r.x) / r.width * 100)); }""")
        self.assertEqual(len(xs), 2)
        self.assertTrue(20 < xs[0] < 34 and 66 < xs[1] < 80, xs)                          # слева около четверти, справа около трёх четвертей

    def test_login_and_password_cards_start_with_the_school_picture(self):
        page = self.open(tab="hw")
        html = page.evaluate("() => lockCard()")
        self.assertIn('class="card setup"><svg class="hero-art"', html)
        self.assertIn("hero-art", page.evaluate("() => settingsView()"))

    def test_schedule_day_picker_has_the_days_picture(self):
        page = self.open(tab="plan")
        self.assertEqual(page.locator(".nav > svg.scene").count(), 1)
        self.assertEqual(page.eval_on_selector(".nav > svg.scene", "e => getComputedStyle(e).pointerEvents"), "none")
        page.click('.nav button[data-nav="1"]')                                             # листание дней работает поверх картинки
        page.wait_for_selector(".nav > svg.scene")


class NothingSticksOutTests(MarksUiBase):
    """«Не растекается никуда»: на узком телефоне, обычном, планшете и широком экране ничего не выходит за край и не даёт горизонтальной прокрутки."""
    WIDTHS = (320, 390, 768, 1280)

    def overflowing(self, page, width):
        return page.evaluate("""(w) => [...document.querySelectorAll('body *')].filter((e) => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e);
          return r.width > 0 && cs.position !== 'fixed' && !e.closest('svg, .scene, .hero-art, #modal, .rstrip') && r.right > w + 1; }).map((e) => e.tagName + '.' + e.className + ':' + Math.round(e.getBoundingClientRect().right)).slice(0, 5)""", width)

    def test_every_tab_fits_every_width(self):
        for w in self.WIDTHS:
            ctx = self.browser.new_context(viewport={"width": w, "height": 900})
            page = ctx.new_page()
            page.goto(self.url)
            page.wait_for_selector("nav.tabs")
            for tab in ("hw", "marks", "plan"):
                page.click(f'nav.tabs button[data-tab="{tab}"]')
                page.wait_for_selector("#main .day, #main .scard, #main .lesson, #main .empty", timeout=8000)
                page.wait_for_timeout(500)                                                       # анимации входа закончились
                self.assertEqual(self.overflowing(page, w), [], f"{tab} при ширине {w}")
                self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), w, f"{tab} при ширине {w}")
            ctx.close()

    def test_settings_and_subject_screen_fit_every_width(self):
        for w in self.WIDTHS:
            ctx = self.browser.new_context(viewport={"width": w, "height": 900})
            page = ctx.new_page()
            page.goto(self.url)
            page.wait_for_selector("nav.tabs")
            page.click("#gear")
            page.wait_for_selector(".settings")
            page.wait_for_timeout(500)
            self.assertEqual(self.overflowing(page, w), [], f"настройки при ширине {w}")
            page.click('nav.tabs button[data-tab="marks"]')
            page.wait_for_selector(".scard")
            page.click(".scard")
            page.wait_for_selector("#screen .atile, #screen .scr-card")
            page.wait_for_timeout(600)
            self.assertEqual(self.overflowing(page, w), [], f"экран предмета при ширине {w}")
            ctx.close()


class MotionTests(MarksUiBase):
    def test_tab_pill_moves_to_the_chosen_tab(self):
        page = self.open(tab="hw")
        pos = lambda: page.evaluate("() => document.querySelector('nav.tabs').style.getPropertyValue('--i')")
        self.assertEqual(pos(), "0")
        page.click('nav.tabs button[data-tab="plan"]')
        page.wait_for_function("() => document.querySelector('nav.tabs').style.getPropertyValue('--i') === '2'")
        page.click('nav.tabs button[data-tab="marks"]')
        page.wait_for_function("() => document.querySelector('nav.tabs').style.getPropertyValue('--i') === '1'")
        self.assertEqual(self.errors, [])

    def test_page_slides_in_the_direction_of_the_tab_change(self):
        page = self.open(tab="hw")
        page.click('nav.tabs button[data-tab="plan"]')
        self.assertTrue(page.evaluate("() => document.querySelector('#main').classList.contains('slide-r')"))   # вперёд: слева направо
        page.click('nav.tabs button[data-tab="hw"]')
        self.assertTrue(page.evaluate("() => document.querySelector('#main').classList.contains('slide-l')"))

    def test_numbers_count_up_and_end_on_the_exact_value(self):
        page = self.open(tab="hw")
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")      # как у человека, а не у проверки
        page.reload()
        page.wait_for_selector("nav.tabs")
        sel = ".scard .avg2 strong, .scard .atile strong"
        page.click('nav.tabs button[data-tab="marks"]')
        page.wait_for_selector(".scard")
        page.wait_for_timeout(1100)
        final = page.inner_text(sel)                                                    # после анимации: точное число
        self.assertRegex(final, r"^\d,\d\d$")
        page.click('nav.tabs button[data-tab="plan"]')
        page.click('nav.tabs button[data-tab="marks"]')
        page.wait_for_selector(".scard")
        start = float(page.inner_text(sel).replace(",", "."))
        self.assertLess(start, float(final.replace(",", ".")))                          # сразу после входа число ещё «набегает»
        page.wait_for_timeout(1100)
        self.assertEqual(page.inner_text(sel), final)                                   # и заканчивает ровно на настоящем значении

    def test_motion_really_plays_frame_by_frame(self):
        """Не только «класс добавлен», а настоящее движение: значения меняются по кадрам плавно, «таблетка» с лёгким перелётом."""
        page = self.open(tab="hw")
        r = page.evaluate("""async () => {
          const pill = () => parseFloat(getComputedStyle(document.querySelector('nav.tabs'), '::before').transform.split(',')[4]) || 0;
          const op = () => parseFloat(getComputedStyle(document.querySelector('#main')).opacity);
          const x = [], o = [], t0 = performance.now();
          document.querySelector('nav.tabs button[data-tab="plan"]').click();
          await new Promise((res) => { const f = () => { x.push(pill()); o.push(op()); const busy = document.getAnimations({ subtree: true }).some((a) => a.playState === 'running' && (a.transitionProperty === 'transform' || ['slideR', 'slideL'].includes(a.animationName))); if (performance.now() - t0 < 3000 && (busy || performance.now() - t0 < 300)) requestAnimationFrame(f); else res(); }; f(); });
          return { x, o, names: [...new Set(document.getAnimations({ subtree: true }).map((a) => a.animationName || a.transitionProperty))] };
        }""")
        x, o = r["x"], r["o"]
        self.assertTrue(any(5 < v < x[-1] - 5 for v in x))                                    # есть промежуточные положения: это движение, а не прыжок (число кадров зависит от нагрузки на машину)
        self.assertLess(x[0], 5)                                                              # «таблетка» стартует у первой вкладки
        self.assertGreaterEqual(max(x), x[-1] - 0.01)                                        # доезжает до места (на быстром компьютере ещё и с лёгким перелётом)
        self.assertGreater(x[-1], 100)                                                        # и действительно переехала ко второй вкладке
        self.assertTrue(all(b >= a - 0.5 for a, b in zip(x[:len(x) // 3], x[1:len(x) // 3 + 1])))   # в начале движется только вперёд
        self.assertLess(min(o), 0.2)                                                          # страница появляется из прозрачности
        self.assertEqual(o[-1], 1)
        self.assertIn("slideR", r["names"])

    def test_numbers_count_up_on_the_subject_screen_too(self):
        page = self.open(tab="marks")
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")
        page.reload()
        page.wait_for_selector("nav.tabs")
        page.click('nav.tabs button[data-tab="marks"]')
        page.wait_for_selector(".scard")
        page.wait_for_timeout(900)
        page.click(".scard")
        page.wait_for_selector("#screen .atile strong")
        first = float(page.inner_text("#screen .atile strong").replace(",", "."))
        page.wait_for_timeout(1000)
        final = float(page.inner_text("#screen .atile strong").replace(",", "."))
        self.assertLess(first, final)
        self.assertGreater(final, 0)

    def test_tab_bar_pill_adapts_to_the_number_of_tabs(self):
        page = self.open(tab="hw")
        self.assertEqual(page.evaluate("() => document.querySelector('nav.tabs').style.getPropertyValue('--n')"), "3")
        w3 = page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('nav.tabs'), '::before').width)")
        page.evaluate("""() => { const nav = document.querySelector('nav.tabs'); const b = document.createElement('button'); b.textContent = 'Ещё'; nav.append(b); nav.style.setProperty('--n', 4); }""")
        w4 = page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('nav.tabs'), '::before').width)")
        self.assertLess(w4, w3)                                                              # четвёртая вкладка: «таблетка» уже, как раз по вкладке

    def test_no_motion_when_the_person_asked_for_less_of_it(self):
        page = self.browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce").new_page()
        page.goto(self.url)
        page.wait_for_selector("nav.tabs")
        dur = page.evaluate("() => parseFloat(getComputedStyle(document.querySelector('nav.tabs'), '::before').transitionDuration)")
        self.assertLess(dur, 0.01)

    def test_decor_never_changes_the_page_width(self):
        page = self.open(tab="hw")
        self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 390)
        page.click('nav.tabs button[data-tab="marks"]')
        page.wait_for_selector(".scard")
        self.assertLessEqual(page.evaluate("() => document.documentElement.scrollWidth"), 390)

    def test_decorative_pictures_are_hidden_from_screen_readers(self):
        page = self.open(tab="hw")
        bad = page.evaluate("""() => [...document.querySelectorAll('.scene, .em.art, .scard .wm')].filter((e) => !(e.getAttribute('aria-hidden') === 'true')).length""")
        self.assertEqual(bad, 0)


class StaticDesignTests(unittest.TestCase):
    def test_gradients_used_by_pictures_are_defined_once(self):
        from pathlib import Path
        html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")
        used = set(re.findall(r"sg-[a-z0-9]+", html))                                       # и url(#sg-…), и имена, подставляемые в код
        defined = re.findall(r'<linearGradient id="(sg-[a-z0-9]+)"', html)
        self.assertEqual(sorted(used), sorted(defined))                                  # ничего не пропущено и нет повторов
        self.assertEqual(len(defined), len(set(defined)))


if __name__ == "__main__":
    unittest.main()
