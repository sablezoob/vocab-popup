# -*- coding: utf-8 -*-
"""Точка входа: иконка в трее, планировщик показов, локальный дашборд."""
import ctypes
import logging
import os
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import ai
import aiworker
import backup
import db
import hotkeys
import srs
import webapp
from popup import Popup

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "data", "app.log")
DASHBOARD_URL = "http://127.0.0.1:8777/"
REVIEW_URL = "http://127.0.0.1:8777/train?only=unreviewed"
TRAIN_URL = "http://127.0.0.1:8777/train"
MUTEX_NAME = "VocabPopup_SingleInstance_Mutex"

log = logging.getLogger("vocab")


def setup_logging():
    """Без лога упавшее приложение молча исчезает из трея — причину не найти."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    h = RotatingFileHandler(LOG_PATH, maxBytes=512 * 1024, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    # Клиент нейросети пишет по строке на каждый запрос и повтор — в журнале
    # это заслоняет собственные сообщения приложения.
    for noisy in ("httpx", "httpcore", "openai", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def hook(exc_type, exc, tb):
        log.critical("Необработанная ошибка", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def already_running():
    """Второй экземпляр занял бы порт дашборда и показывал бы двойные карточки."""
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() == 183      # ERROR_ALREADY_EXISTS


def make_icon(letter="A", bg="#3a86ff"):
    """Иконка трея рисуется в коде — не тащим бинарник в репозиторий."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(bg))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QColor("#ffffff"))
    p.setFont(QFont("Segoe UI", 34, QFont.Bold))
    p.drawText(pm.rect(), Qt.AlignCenter, letter)
    p.end()
    return QIcon(pm)


# Состояния SHQueryUserNotificationState, при которых Windows сам прячет
# уведомления: полноэкранное приложение, игра D3D, режим презентации, Store-приложение.
BUSY_STATES = {2, 3, 4, 7}


def is_fullscreen_active():
    """True, если идёт полноэкранное приложение (игра, презентация, звонок).

    Сравнивать размер окна с размером экрана нельзя: у развёрнутого окна
    Windows рисует невидимые рамки, и оно оказывается на 16 px больше экрана —
    обычный максимизированный браузер выглядел бы полноэкранным. Спрашиваем
    систему тем же способом, каким она решает судьбу собственных уведомлений.
    """
    if sys.platform != "win32":
        return False
    try:
        state = ctypes.c_int()
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
            return state.value in BUSY_STATES
    except Exception:
        log.exception("Не удалось определить полноэкранное окно")
    return False


def pause_left():
    """Сколько осталось до конца временной паузы. None — паузы нет.

    Хранится момент окончания, а не остаток: приложение может быть закрыто
    и снова открыто, а «тишина до шести вечера» должна пережить перезапуск.
    """
    raw = (db.get("pause_until", "") or "").strip()
    if not raw:
        return None
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        db.put("pause_until", "")
        return None
    if datetime.now() >= until:
        db.put("pause_until", "")
        return None
    return until


def set_pause(minutes=None, until=None):
    """Пауза на N минут или до указанного момента."""
    if until is None and minutes:
        until = datetime.now() + timedelta(minutes=minutes)
    db.put("pause_until", until.isoformat(timespec="seconds") if until else "")
    return until


def next_morning():
    """Ближайшее утро — момент, когда заканчивается «выключить до завтра»."""
    try:
        h, m = [int(x) for x in (db.get("quiet_to", "08:00") or "08:00").split(":")]
    except ValueError:
        h, m = 8, 0
    now = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def in_quiet_hours():
    if db.get("quiet_enabled", "1") != "1":
        return False
    try:
        h1, m1 = [int(x) for x in db.get("quiet_from", "23:00").split(":")]
        h2, m2 = [int(x) for x in db.get("quiet_to", "08:00").split(":")]
    except (ValueError, AttributeError):
        return False
    now = datetime.now()
    start, end = h1 * 60 + m1, h2 * 60 + m2
    cur = now.hour * 60 + now.minute
    if start == end:
        return False
    if start > end:                     # интервал переходит через полночь
        return cur >= start or cur < end
    return start <= cur < end


class App:
    def __init__(self):
        db.init()
        self.qt = QApplication(sys.argv)
        self.qt.setQuitOnLastWindowClosed(False)

        self.popup = Popup()
        self.popup.answered.connect(self.on_answer)
        self.popup.unknown_word.connect(self.on_unknown_word)
        self._message_url = DASHBOARD_URL
        self._skip_reason = None
        self._interval_min = 3
        self._due_at = 0.0
        self._backup_check_at = 0.0
        self._review_offered_on = None      # дата последнего предложения разобрать

        self.tray = QSystemTrayIcon(make_icon("A"))
        self.tray.activated.connect(self.on_tray_click)
        self.tray.messageClicked.connect(self.on_message_clicked)

        self.hk_labels = hotkeys.register(self.qt, self.on_hotkey)
        self.qt.aboutToQuit.connect(hotkeys.unregister)
        log.info("Горячие клавиши заняты: %s", ", ".join(self.hk_labels) or "нет")

        self.build_menu()
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.on_timer)
        self.restart_timer()

        threading.Thread(target=self.serve, daemon=True).start()
        aiworker.start()
        log.info("Нейросеть: %s", "включена" if ai.is_enabled() else "выключена")

    def serve(self):
        try:
            webapp.run()
        except OSError:
            log.exception("Дашборд не поднялся — порт 8777 занят")

    # ---------- меню ----------
    def build_menu(self):
        """Меню несёт состояние — сколько осталось паузы, идут ли показы, —
        поэтому перед показом обновляется в refresh_menu."""
        m = QMenu()
        m.aboutToShow.connect(self.refresh_menu)

        hk = dict((lbl.split("+")[-1], lbl) for lbl in self.hk_labels)
        a_now = QAction("Показать слово сейчас" + (f"\t{hk['0']}" if "0" in hk else ""), m)
        a_now.triggered.connect(lambda: self.show_next(force=True))
        m.addAction(a_now)

        self.a_state = QAction("", m)
        self.a_state.setEnabled(False)
        m.addAction(self.a_state)

        m.addSeparator()

        sub_pause = QMenu("Приостановить", m)
        for title, minutes in (("на 30 минут", 30), ("на 1 час", 60),
                               ("на 2 часа", 120), ("на 4 часа", 240)):
            a = QAction(title, sub_pause)
            a.triggered.connect(lambda _, v=minutes: self.pause_for(v))
            sub_pause.addAction(a)
        sub_pause.addSeparator()
        a_tomorrow = QAction("Выключить до завтра", sub_pause)
        a_tomorrow.triggered.connect(self.pause_until_morning)
        sub_pause.addAction(a_tomorrow)
        m.addMenu(sub_pause)

        self.a_resume = QAction("Возобновить показы", m)
        self.a_resume.triggered.connect(self.resume)
        m.addAction(self.a_resume)

        self.a_pause = QAction("Пауза без срока", m, checkable=True)
        self.a_pause.setChecked(db.get("paused", "0") == "1")
        self.a_pause.toggled.connect(self.on_pause)
        m.addAction(self.a_pause)

        m.addSeparator()

        sub_time = QMenu("Когда не показывать", m)
        self.a_quiet = QAction("Тихие часы включены", sub_time, checkable=True)
        self.a_quiet.toggled.connect(self.on_quiet_toggled)
        sub_time.addAction(self.a_quiet)

        sub_from = QMenu("Начало тишины", sub_time)
        grp_from = QActionGroup(sub_from)
        sub_to = QMenu("Конец тишины", sub_time)
        grp_to = QActionGroup(sub_to)
        self.quiet_from_actions, self.quiet_to_actions = {}, {}
        for hour in range(24):
            label = "%02d:00" % hour
            a1 = QAction(label, sub_from, checkable=True)
            a1.triggered.connect(lambda _, v=label: self.set_quiet("quiet_from", v))
            grp_from.addAction(a1)
            sub_from.addAction(a1)
            self.quiet_from_actions[label] = a1

            a2 = QAction(label, sub_to, checkable=True)
            a2.triggered.connect(lambda _, v=label: self.set_quiet("quiet_to", v))
            grp_to.addAction(a2)
            sub_to.addAction(a2)
            self.quiet_to_actions[label] = a2
        sub_time.addMenu(sub_from)
        sub_time.addMenu(sub_to)
        m.addMenu(sub_time)

        sub_int = QMenu("Как часто показывать", m)
        grp = QActionGroup(sub_int)
        self.interval_actions = {}
        for n in (1, 2, 3, 5, 10, 15, 30, 60):
            a = QAction("каждые %d мин" % n, sub_int, checkable=True)
            a.triggered.connect(lambda _, v=n: self.set_interval(v))
            grp.addAction(a)
            sub_int.addAction(a)
            self.interval_actions[n] = a
        m.addMenu(sub_int)

        sub_pos = QMenu("Где показывать", m)
        grp2 = QActionGroup(sub_pos)
        positions = [("top-center", "Сверху по центру"), ("top-left", "Сверху слева"),
                     ("top-right", "Сверху справа"), ("center", "По центру экрана"),
                     ("bottom-left", "Снизу слева"), ("bottom-center", "Снизу по центру"),
                     ("bottom-right", "Снизу справа")]
        self.pos_actions = {}
        for key, title in positions:
            a = QAction(title, sub_pos, checkable=True)
            a.triggered.connect(lambda _, v=key: self.set_corner(v))
            grp2.addAction(a)
            sub_pos.addAction(a)
            self.pos_actions[key] = a
        m.addMenu(sub_pos)

        m.addSeparator()

        a_review = QAction("Разобрать накопленное…", m)
        a_review.triggered.connect(lambda: webbrowser.open(REVIEW_URL))
        m.addAction(a_review)

        a_train = QAction("Тренировка…", m)
        a_train.triggered.connect(lambda: webbrowser.open(TRAIN_URL))
        m.addAction(a_train)

        a_dash = QAction("Дашборд и слова…", m)
        a_dash.triggered.connect(lambda: webbrowser.open(DASHBOARD_URL))
        m.addAction(a_dash)

        st = backup.status()
        a_backup = QAction("Резервная копия сейчас", m)
        a_backup.setToolTip("Копий: %d, последняя %s" % (st["count"], st["last_at"] or "не делалась"))
        a_backup.triggered.connect(self.backup_now)
        m.addAction(a_backup)

        if self.hk_labels:
            info = QAction("Клавиши: Ctrl+Alt+1 знаю · 2 ещё раз · 3 пропустить", m)
            info.setEnabled(False)
            m.addAction(info)

        m.addSeparator()
        a_quit = QAction("Выход", m)
        a_quit.triggered.connect(self.qt.quit)
        m.addAction(a_quit)

        self.menu = m
        self.tray.setContextMenu(m)
        self.refresh_menu()

    def refresh_menu(self):
        """Состояние могли поменять из дашборда — подтягиваем перед показом."""
        try:
            until = pause_left()
            paused = db.get("paused", "0") == "1"
            if paused:
                state = "Показы остановлены"
            elif until:
                left = int((until - datetime.now()).total_seconds() // 60) + 1
                state = "Пауза до %s (осталось %d мин)" % (until.strftime("%H:%M"), left)
            elif in_quiet_hours():
                state = "Тихие часы до %s" % db.get("quiet_to", "08:00")
            else:
                state = "Показы идут, каждые %d мин" % db.get_int("interval_min", 3)
            self.a_state.setText(state)
            self.a_resume.setVisible(bool(until) or paused)

            cur = db.get_int("interval_min", 3)
            for n, a in self.interval_actions.items():
                a.setChecked(n == cur)
            pos = db.get("corner", "top-center")
            for key, a in self.pos_actions.items():
                a.setChecked(key == pos)
            self.a_quiet.setChecked(db.get("quiet_enabled", "1") == "1")
            self.a_pause.setChecked(paused)
            for label, a in self.quiet_from_actions.items():
                a.setChecked(label == db.get("quiet_from", "23:00"))
            for label, a in self.quiet_to_actions.items():
                a.setChecked(label == db.get("quiet_to", "08:00"))
        except Exception:
            log.exception("Не удалось обновить меню")

    # ---------- действия меню ----------
    def pause_for(self, minutes):
        until = set_pause(minutes=minutes)
        db.put("paused", "0")
        self.update_icon()
        self.tray.showMessage(
            "Vocab Popup",
            "Карточки не появятся до %s." % until.strftime("%H:%M"),
            QSystemTrayIcon.Information, 4000)
        log.info("Пауза на %s мин, до %s", minutes, until.strftime("%H:%M"))

    def pause_until_morning(self):
        until = set_pause(until=next_morning())
        db.put("paused", "0")
        self.update_icon()
        self.tray.showMessage(
            "Vocab Popup",
            "Выключено до завтра, до %s." % until.strftime("%H:%M"),
            QSystemTrayIcon.Information, 4000)
        log.info("Выключено до %s", until.strftime("%d.%m %H:%M"))

    def resume(self):
        db.put("pause_until", "")
        db.put("paused", "0")
        self._due_at = time.monotonic() + 5       # первая карточка почти сразу
        self.update_icon()
        self.tray.showMessage("Vocab Popup", "Показы возобновлены.",
                              QSystemTrayIcon.Information, 3000)
        log.info("Показы возобновлены вручную")

    def set_quiet(self, key, value):
        db.put(key, value)
        db.put("quiet_enabled", "1")
        log.info("Тихие часы: %s = %s", key, value)

    def on_quiet_toggled(self, checked):
        db.put("quiet_enabled", "1" if checked else "0")

    def set_corner(self, value):
        db.put("corner", value)

    def update_icon(self):
        """Серая иконка означает, что сейчас показов не будет."""
        quiet = db.get("paused", "0") == "1" or pause_left() is not None
        self.tray.setIcon(make_icon("A", "#5b6272" if quiet else "#3a86ff"))

    def on_message_clicked(self):
        """Клик по уведомлению открывает то, ради чего оно показано."""
        webbrowser.open(self._message_url or DASHBOARD_URL)

    def maybe_offer_review(self):
        """Показы без ответа — главная утечка: слово мелькнуло и не сдвинулось.

        Когда таких накопилось много, один раз в день предлагаем разобрать
        их пачкой — это ровно та тренировка, которой не хватало входа.
        """
        if db.get("review_prompt_enabled", "1") != "1":
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if self._review_offered_on == today:
            return
        try:
            n = srs.unreviewed_count()
        except Exception:
            log.exception("Не удалось посчитать непроверенные слова")
            return
        if n < max(1, db.get_int("review_threshold", 25)):
            return
        self._review_offered_on = today
        self._message_url = REVIEW_URL
        self.tray.showMessage(
            "Пора закрепить",
            f"{n} слов показались, но остались неотмеченными. "
            "Нажмите, чтобы разобрать их за пару минут.",
            QSystemTrayIcon.Information, 9000)
        log.info("Предложен разбор накопленного: %s слов", n)

    def on_tray_click(self, reason):
        if reason == QSystemTrayIcon.Trigger:      # левый клик
            self.show_next(force=True)
        elif reason == QSystemTrayIcon.DoubleClick:
            webbrowser.open(DASHBOARD_URL)

    def on_hotkey(self, hid):
        if hid == 4:
            self.show_next(force=True)
        elif self.popup.isVisible():
            self.popup.answer({1: "know", 2: "again", 3: "skip"}[hid])

    # ---------- планировщик ----------
    # Тикаем часто, а показываем по накопленному времени. Если ждать полного
    # интервала, смена настройки из дашборда вступала бы в силу только на
    # следующем тике старого интервала: поставил 1 минуту вместо часа —
    # и ждёшь час.
    TICK_MS = 10_000

    def restart_timer(self):
        minutes = max(1, db.get_int("interval_min", 3))
        self._interval_min = minutes
        self._due_at = time.monotonic() + minutes * 60
        self.timer.start(self.TICK_MS)
        self.tray.setToolTip(f"Vocab Popup — каждые {minutes} мин")

    def set_interval(self, minutes):
        db.put("interval_min", minutes)
        self.restart_timer()

    def on_pause(self, checked):
        db.put("paused", "1" if checked else "0")
        if not checked:
            db.put("pause_until", "")
        self.update_icon()

    def on_timer(self):
        self.maybe_backup()
        want = max(1, db.get_int("interval_min", 3))
        if want != self._interval_min:
            # интервал изменили из дашборда — пересчитываем срок показа
            log.info("Интервал изменён: %s -> %s мин", self._interval_min, want)
            self._due_at -= (self._interval_min - want) * 60
            self._interval_min = want
            self.tray.setToolTip(f"Vocab Popup — каждые {want} мин")
        if time.monotonic() >= self._due_at:
            self._due_at = time.monotonic() + want * 60
            self.show_next()
            self.maybe_offer_review()

    BACKUP_CHECK_S = 3600       # чаще проверять незачем: копия раз в сутки

    def maybe_backup(self):
        """Резервная копия по расписанию.

        Делается молча и в рабочем потоке: снимок базы размером в пару
        мегабайт занимает доли секунды, отдельный поток тут только добавил бы
        гонку за соединением к базе.
        """
        now = time.monotonic()
        if now < self._backup_check_at:
            return
        self._backup_check_at = now + self.BACKUP_CHECK_S
        try:
            path = backup.tick()
            if path:
                log.info("Резервная копия готова: %s", os.path.basename(path))
        except Exception:
            log.exception("Резервная копия не удалась")

    def backup_now(self):
        """Копия по кнопке. Результат показываем всплывающим сообщением:
        молчаливая кнопка «сделал или нет?» хуже, чем её отсутствие."""
        try:
            path = backup.make()
        except Exception:
            log.exception("Резервная копия по кнопке не удалась")
            path = None
        if not path:
            self.tray.showMessage("Vocab Popup",
                                  "Не удалось сделать копию — смотрите data/app.log",
                                  QSystemTrayIcon.Warning, 6000)
            return
        ok, info = backup.verify(path)
        text = (f"Копия готова: {os.path.basename(path)} — "
                f"слов {info['words']}, показов {info['events']}" if ok
                else f"Копия записана, но проверка не прошла: {info}")
        self.tray.showMessage("Vocab Popup", text, QSystemTrayIcon.Information, 6000)
        self.refresh_menu()

    def note_skip(self, reason):
        """Пишет в лог смену причины молчания — иначе непонятно, почему нет карточек."""
        if reason == self._skip_reason:
            return
        if reason:
            log.info("Показы приостановлены: %s", reason)
        else:
            log.info("Показы возобновлены")
        self._skip_reason = reason

    def show_next(self, force=False):
        try:
            if force:
                # закрываем текущую карточку по-честному, с записью события
                if self.popup.isVisible():
                    self.popup.answer("skip")
            else:
                if self.popup.isVisible():
                    return
                until = pause_left()
                reason = ("пауза" if db.get("paused", "0") == "1" else
                          f"пауза до {until:%H:%M}" if until else
                          "тихие часы" if in_quiet_hours() else
                          "полноэкранное приложение" if is_fullscreen_active() else None)
                if reason:
                    self.note_skip(reason)
                    return
                self.note_skip(None)
            row = srs.next_word()
            if row is None:
                if force:
                    self.tray.showMessage("Vocab Popup",
                                          "Словарь пуст. Откройте дашборд и добавьте слова.",
                                          QSystemTrayIcon.Information, 4000)
                return
            self.popup.show_word(row)
        except Exception:
            log.exception("Ошибка при показе слова")

    def on_unknown_word(self, word, context):
        """Слово, отмеченное в примере. Разбор идёт фоном — интерфейс не ждёт."""
        try:
            wid, res = aiworker.queue_unknown(word, context)
            log.info("Незнакомое слово «%s»: %s", word, res)
            if res == "exists":
                self.tray.showMessage("Vocab Popup", f"«{word}» уже есть в словаре",
                                      QSystemTrayIcon.Information, 3000)
            elif not ai.is_enabled():
                self.tray.showMessage(
                    "Vocab Popup",
                    f"«{word}» добавлено, но перевод не подтянуть: "
                    "нейросеть выключена в настройках дашборда.",
                    QSystemTrayIcon.Warning, 5000)
        except Exception:
            log.exception("Не удалось поставить слово в очередь")

    def on_answer(self, word_id, action, ms):
        try:
            db.log_event(word_id, action, ms)
            srs.grade(word_id, action)
        except sqlite3.IntegrityError:
            # Слово удалили из дашборда, пока карточка висела на экране.
            log.info("Ответ по удалённому слову id=%s пропущен", word_id)
        except Exception:
            log.exception("Не удалось записать ответ word_id=%s", word_id)

    def run(self):
        self.update_icon()
        QTimer.singleShot(4000, lambda: self.show_next())
        log.info("Запущено. Интервал %s мин", db.get_int("interval_min", 3))
        sys.exit(self.qt.exec())


if __name__ == "__main__":
    setup_logging()
    if already_running():
        log.warning("Экземпляр уже запущен — выходим")
        ctypes.windll.user32.MessageBoxW(
            None, "Vocab Popup уже запущен — ищите иконку в трее.",
            "Vocab Popup", 0x40)
        sys.exit(0)
    try:
        App().run()
    except SystemExit:
        raise
    except Exception:
        log.critical("Не удалось запустить", exc_info=True)
        raise
