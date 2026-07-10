"""
dzv_optimizer.py  —  Вкладка «ДЗВ (Дозволене Збиральне Вікно)»

Алгоритм: RCPSP (Resource Constrained Project Scheduling Problem)
=========

Модель:
  • Кожне поле — активність j з:
      - обсягом  p_j  (валовий збір, т)
      - вікном   [ES_j, DL_j]  (earliest start / deadline в днях від початку горизонту)
      - потребою в ресурсі  r_j = 1 «одиниця потужності» (масштабується до фіз. capacity)

  • Ресурс відділення d: денна пропускна здатність  R_d  (т/день).
    Активності одного відділення КОНКУРУЮТЬ за цей ресурс.

  • Задача: знайти для кожної активності j вектор виконання x_{j,t} ≥ 0
    (скільки тонн збирається в день t) такий, що:
      (1) Σ_t x_{j,t} = p_j              — повне виконання
      (2) x_{j,t} = 0  якщо t < ES_j     — не раніше вікна
      (3) x_{j,t} = 0  якщо t > DL_j     — не пізніше дедлайну (м'яка умова, штраф)
      (4) Σ_{j∈d} x_{j,t} ≤ R_d,t       — обмеження ресурсу відділення
      (5) Σ_d R_d,t ≤ FACTORY_t          — обмеження заводу

  Розв'язок будується жадібно з пріоритетами:
    Priority(j, t) = (slack_j(t), urgency_j(t))
    де:
      slack_j(t)   = DL_j − t  (менший slack → вищий пріоритет)
      urgency_j(t) = p_j_remaining / max(1, DL_j − t)  (т/день що потрібно)

  Повна версія (LP-relax) для відділень з дефіцитом:
    Мінімізуємо суму штрафних змінних s_j (порушення дедлайну)
    через пропорційний перерозподіл ресурсу між активностями з однаковим slack.

Перерозподіл ресурсу (Variant 3):
  Якщо після базового розкладу залишаються порушення:
    - Рахуємо «тиск» відділення: Σ_j (remaining_j / slack_j)
    - Перерозподіляємо заводський ліміт пропорційно тиску
    - Перезапускаємо розклад з новими квотами
    - Загальний ліміт заводу зберігається точно

«Зарано» — перевіряється за датою ПОЧАТКУ збирання.
«Запізно» — перевіряється за датою ЗАВЕРШЕННЯ збирання.

Логування:
  Усі події пишуться у файл harvest_dzv.log поряд з модулем.
  Рівні: INFO (підсумки), DEBUG (кожна активність, urgency, розподіл ресурсу).
  Події: аналіз ДЗВ, порушення, побудова розкладу, дефіцит, квоти, перебудова.
"""

import math
import logging
import os
from datetime import datetime, date, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
    QGroupBox, QMessageBox, QDialog, QTextEdit,
    QSplitter, QFrame, QRadioButton, QButtonGroup
)
from PySide6.QtGui import QColor, QFont


# ─────────────────────────────────────────────
# Налаштування логера
# ─────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvest_dzv.log")

_logger = logging.getLogger("DZV")
if not _logger.handlers:
    _logger.setLevel(logging.DEBUG)
    _fh = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))
    _logger.addHandler(_fh)

log = _logger          # коротке ім'я для використання всередині модуля
SEP = "─" * 72        # роздільник секцій у лозі


# ─────────────────────────────────────────────
def _parse_date(text: str):
    if not text or not text.strip():
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _fmt_date(d) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


# ─────────────────────────────────────────────
class DZVOptimizerTab(QWidget):

    def __init__(self, main_app):
        super().__init__()
        self.main_app = main_app
        self._analysis_result = []
        self._build_ui()
        log.info("DZVOptimizerTab ініціалізовано. Лог: %s", _LOG_PATH)

    # ══════════════════════════════════════════
    # UI
    # ══════════════════════════════════════════
    def _build_ui(self):
        root = QVBoxLayout()
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        title = QLabel("ДЗВ — Дозволене Збиральне Вікно")
        tf = QFont(); tf.setPointSize(13); tf.setBold(True)
        title.setFont(tf)
        root.addWidget(title)

        hint = QLabel(
            "Алгоритм RCPSP (Resource Constrained Project Scheduling Problem): "
            "кожне поле — активність з вікном [ES, DL] та обсягом p. "
            "Щодня ресурс відділення розподіляється між активними полями пропорційно "
            "до їх терміновості (залишок / слак). Поля з мінімальним слаком отримують "
            "ресурс першими. Якщо ресурс обмежений — розраховується точний дефіцит "
            "і пропонуються варіанти усунення."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555; font-size:11px;")
        root.addWidget(hint)

        # ── Налаштування ──
        box = QGroupBox("Параметри ДЗВ")
        b = QHBoxLayout(); b.setSpacing(16)

        b.addWidget(QLabel("Збирати не раніше ніж за"))
        self.early_spin = QSpinBox()
        self.early_spin.setRange(0, 30); self.early_spin.setValue(1)
        self.early_spin.setSuffix(" дн. до дозрівання")
        self.early_spin.setFixedWidth(210)
        b.addWidget(self.early_spin)

        b.addWidget(QLabel("і не пізніше ніж через"))
        self.late_spin = QSpinBox()
        self.late_spin.setRange(0, 60); self.late_spin.setValue(7)
        self.late_spin.setSuffix(" дн. після дозрівання")
        self.late_spin.setFixedWidth(220)
        b.addWidget(self.late_spin)

        b.addStretch()

        btn_analyze = QPushButton("🔍  Аналіз поточного плану")
        btn_analyze.setFixedHeight(36)
        btn_analyze.setStyleSheet(
            "background:#1565c0;color:white;font-weight:bold;border-radius:4px;padding:0 14px;")
        btn_analyze.clicked.connect(self._run_analysis)
        b.addWidget(btn_analyze)

        btn_rebuild = QPushButton("⚙  Перебудувати план за ДЗВ (RCPSP)")
        btn_rebuild.setFixedHeight(36)
        btn_rebuild.setStyleSheet(
            "background:#2e7d32;color:white;font-weight:bold;border-radius:4px;padding:0 14px;")
        btn_rebuild.clicked.connect(self._run_rebuild)
        b.addWidget(btn_rebuild)

        box.setLayout(b)
        root.addWidget(box)

        # ── Сплітер ──
        splitter = QSplitter(Qt.Horizontal)

        # Ліва: таблиця аналізу
        lw = QWidget(); ll = QVBoxLayout(); ll.setContentsMargins(0, 0, 0, 0)
        grp_t = QGroupBox("Аналіз полів за ДЗВ")
        tl = QVBoxLayout()
        self.analysis_table = QTableWidget()
        self.analysis_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.analysis_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.analysis_table.verticalHeader().setVisible(False)
        self.analysis_table.setStyleSheet("""
            QTableWidget{gridline-color:#d0d0d0;font-size:12px;}
            QHeaderView::section{background:#37474f;color:white;font-weight:bold;
                padding:4px;border:1px solid #263238;}
        """)
        tl.addWidget(self.analysis_table)
        grp_t.setLayout(tl)
        ll.addWidget(grp_t)
        self.analysis_summary = QLabel("")
        self.analysis_summary.setWordWrap(True)
        self.analysis_summary.setStyleSheet("font-size:11px;color:#333;padding:4px;")
        ll.addWidget(self.analysis_summary)
        lw.setLayout(ll)
        splitter.addWidget(lw)

        # Права: порушники + рекомендації
        rw = QWidget(); rl = QVBoxLayout(); rl.setContentsMargins(0, 0, 0, 0)
        grp_v = QGroupBox("Поля з порушенням ДЗВ")
        vl = QVBoxLayout()
        self.violation_table = QTableWidget()
        self.violation_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.violation_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.violation_table.verticalHeader().setVisible(False)
        self.violation_table.setStyleSheet("""
            QTableWidget{gridline-color:#d0d0d0;font-size:12px;}
            QHeaderView::section{background:#b71c1c;color:white;font-weight:bold;
                padding:4px;border:1px solid #7f0000;}
        """)
        vl.addWidget(self.violation_table)
        grp_v.setLayout(vl)
        rl.addWidget(grp_v)

        grp_r = QGroupBox("Рекомендації")
        rl2 = QVBoxLayout()
        self.rec_label = QLabel("Виконайте аналіз для отримання рекомендацій.")
        self.rec_label.setWordWrap(True)
        self.rec_label.setStyleSheet("font-size:12px;padding:4px;")
        rl2.addWidget(self.rec_label)
        grp_r.setLayout(rl2)
        rl.addWidget(grp_r)
        rw.setLayout(rl)
        splitter.addWidget(rw)
        splitter.setSizes([700, 400])
        root.addWidget(splitter)
        self.setLayout(root)

    # ══════════════════════════════════════════
    # Збір даних з основної таблиці
    # ══════════════════════════════════════════
    def _collect_fields(self):
        main_table = self.main_app.table
        fields = []
        for r in range(main_table.rowCount()):
            def cell(c, _r=r):
                it = main_table.item(_r, c)
                return it.text().strip() if it else ""
            try:
                gross = float(cell(3))
            except ValueError:
                gross = 0.0
            if gross <= 0:
                continue
            fields.append({
                "name":          cell(0) or f"Поле {r+1}",
                "gross":         gross,
                "division":      cell(10),
                "ripening":      _parse_date(cell(6)),
                "harvest_start": _parse_date(cell(7)),
                "harvest_end":   _parse_date(cell(8)),
                "deviation_str": cell(9),
                "row_idx":       r,
            })
        log.debug("_collect_fields: зібрано %d полів", len(fields))
        return fields

    # ══════════════════════════════════════════
    # Перевірка вікна для одного поля
    # ══════════════════════════════════════════
    def _check_window(self, field, early_days, late_days):
        """
        «Зарано» — за датою ПОЧАТКУ збирання.
        «Запізно» — за датою ЗАВЕРШЕННЯ збирання.
        """
        rip   = field["ripening"]
        start = field["harvest_start"]
        end   = field["harvest_end"] or start
        if not rip or not start:
            return {"status": "no_data", "deviation_days": None,
                    "window_start": None, "window_end": None}
        window_start = rip - timedelta(days=early_days)
        window_end   = rip + timedelta(days=late_days)
        start_delta  = (start - rip).days
        end_delta    = (end - rip).days if end else start_delta

        if start < window_start:
            status    = "too_early"
            deviation = start_delta
        elif end and end > window_end:
            status    = "too_late"
            deviation = end_delta
        else:
            status    = "ok"
            deviation = end_delta
        return {"status": status, "deviation_days": deviation,
                "window_start": window_start, "window_end": window_end}

    # ══════════════════════════════════════════
    # АНАЛІЗ
    # ══════════════════════════════════════════
    def _run_analysis(self):
        early = self.early_spin.value()
        late  = self.late_spin.value()

        log.info(SEP)
        log.info("АНАЛІЗ ДЗВ  вікно: -%d / +%d дн.", early, late)
        log.info(SEP)

        fields = self._collect_fields()
        if not fields:
            log.warning("Аналіз: основна таблиця порожня — скасовано")
            QMessageBox.warning(self, "ДЗВ", "Основна таблиця порожня.")
            return
        if all(not f["harvest_start"] for f in fields):
            log.warning("Аналіз: жодне поле не має дати початку збирання — скасовано")
            QMessageBox.warning(self, "ДЗВ",
                "Жодне поле не має дати початку збирання.\n"
                "Спочатку побудуйте Детальний план збирання.")
            return

        self._analysis_result = [{**f, **self._check_window(f, early, late)} for f in fields]

        # ── Лог кожного поля ──
        log.info("Результати перевірки ДЗВ (%d полів):", len(self._analysis_result))
        counts = {"ok": 0, "too_early": 0, "too_late": 0, "no_data": 0}
        for rec in self._analysis_result:
            st  = rec["status"]
            dev = rec.get("deviation_days")
            counts[st] = counts.get(st, 0) + 1
            log.info(
                "  %-30s  [%-20s]  вал=%7.0f т  дозр=%s  старт=%s  фін=%s  "
                "відхил=%s дн.  статус=%s",
                rec["name"], rec["division"], rec["gross"],
                _fmt_date(rec["ripening"]),
                _fmt_date(rec["harvest_start"]),
                _fmt_date(rec.get("harvest_end")),
                (f"{dev:+d}" if dev is not None else "—"),
                st.upper(),
            )

        log.info(
            "Підсумок: всього=%d  ok=%d  too_early=%d  too_late=%d  no_data=%d",
            len(self._analysis_result),
            counts["ok"], counts["too_early"], counts["too_late"], counts["no_data"],
        )

        self._render_analysis_table(self._analysis_result)
        self._render_violation_table(self._analysis_result)
        self._render_recommendations(self._analysis_result, early, late)

    def _render_analysis_table(self, result):
        COLS = ["Поле", "Відділення", "Вал (т)", "Дозрівання",
                "Вікно від", "Вікно до", "Початок збирання", "Відхил. (дн.)", "Статус ДЗВ"]
        STATUS = {
            "ok":        (QColor("#c8e6c9"), QColor("#1b5e20"), "✅ В межах"),
            "too_early": (QColor("#fff9c4"), QColor("#e65100"), "⚠ Зарано"),
            "too_late":  (QColor("#ffcdd2"), QColor("#b71c1c"), "❌ Запізно"),
            "no_data":   (QColor("#f5f5f5"), QColor("#757575"), "— Немає даних"),
        }
        self.analysis_table.setColumnCount(len(COLS))
        self.analysis_table.setHorizontalHeaderLabels(COLS)
        self.analysis_table.setRowCount(len(result))
        for r, rec in enumerate(result):
            bg, fg, stxt = STATUS.get(rec["status"], STATUS["no_data"])
            dev = rec["deviation_days"]
            vals = [
                rec["name"], rec["division"], f"{rec['gross']:,.0f}",
                _fmt_date(rec["ripening"]),
                _fmt_date(rec.get("window_start")),
                _fmt_date(rec.get("window_end")),
                _fmt_date(rec["harvest_start"]),
                (f"+{dev}" if dev and dev > 0 else str(dev)) if dev is not None else "—",
                stxt,
            ]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignCenter)
                it.setBackground(bg); it.setForeground(fg)
                self.analysis_table.setItem(r, c, it)
        self.analysis_table.resizeColumnsToContents()

        ok  = sum(1 for r in result if r["status"] == "ok")
        ear = sum(1 for r in result if r["status"] == "too_early")
        lat = sum(1 for r in result if r["status"] == "too_late")
        nd  = sum(1 for r in result if r["status"] == "no_data")
        parts = [f"Всього: <b>{len(result)}</b>",
                 f"<span style='color:#2e7d32'>✅ Норма: <b>{ok}</b></span>"]
        if ear: parts.append(f"<span style='color:#e65100'>⚠ Зарано: <b>{ear}</b></span>")
        if lat: parts.append(f"<span style='color:#b71c1c'>❌ Запізно: <b>{lat}</b></span>")
        if nd:  parts.append(f"<span style='color:#757575'>— Без даних: <b>{nd}</b></span>")
        self.analysis_summary.setText("   |   ".join(parts))

    def _render_violation_table(self, result):
        viol = [r for r in result if r["status"] in ("too_early", "too_late")]
        COLS = ["Поле", "Відділення", "Вал (т)", "Дозрівання",
                "Початок збирання", "Відхил. (дн.)", "Тип"]
        self.violation_table.setColumnCount(len(COLS))
        self.violation_table.setHorizontalHeaderLabels(COLS)
        self.violation_table.setRowCount(len(viol))

        if viol:
            log.info("ПОРУШЕННЯ ДЗВ (%d):", len(viol))
        for r, rec in enumerate(viol):
            is_late = rec["status"] == "too_late"
            bg = QColor("#ffcdd2") if is_late else QColor("#fff9c4")
            fg = QColor("#b71c1c") if is_late else QColor("#e65100")
            dev = rec["deviation_days"]
            dev_s = (f"+{dev}" if dev > 0 else str(dev)) if dev is not None else "—"
            vals = [rec["name"], rec["division"], f"{rec['gross']:,.0f}",
                    _fmt_date(rec["ripening"]), _fmt_date(rec["harvest_start"]),
                    dev_s, "❌ Запізно" if is_late else "⚠ Зарано"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignCenter)
                it.setBackground(bg); it.setForeground(fg)
                self.violation_table.setItem(r, c, it)
            log.info(
                "  [%s] %-30s  [%-20s]  вал=%7.0f т  дозр=%s  старт=%s  відхил=%s дн.",
                "ЗАПІЗНО" if is_late else "ЗАРАНО ",
                rec["name"], rec["division"], rec["gross"],
                _fmt_date(rec["ripening"]), _fmt_date(rec["harvest_start"]), dev_s,
            )
        self.violation_table.resizeColumnsToContents()

    def _render_recommendations(self, result, early, late):
        viol = [r for r in result if r["status"] in ("too_early", "too_late")]
        if not viol:
            log.info("Рекомендації: всі поля в межах ДЗВ — коригувань не потрібно")
            self.rec_label.setText(
                "✅ <b>Всі поля вкладаються в ДЗВ.</b><br>Жодних коригувань не потрібно.")
            return
        lat = [r for r in viol if r["status"] == "too_late"]
        ear = [r for r in viol if r["status"] == "too_early"]
        log.info("Рекомендації: %d порушень (запізно=%d, зарано=%d)", len(viol), len(lat), len(ear))
        lines = [f"<b>Виявлено {len(viol)} порушень ДЗВ</b> (вікно: -{early} / +{late} дн.):<br>"]
        if lat:
            lines.append(f"❌ <b>Запізно ({len(lat)} пол.)</b> — збирання ЗАВЕРШУЄТЬСЯ "
                         f"пізніше ніж через {late} дн. після дозрівання.<br>")
        if ear:
            lines.append(f"⚠ <b>Зарано ({len(ear)} пол.)</b> — збирання починається "
                         f"більш ніж за {early} дн. до дозрівання.<br>")
        lines.append("<br>Натисніть <b>«Перебудувати план за ДЗВ (RCPSP)»</b> — алгоритм "
                     "розподілить ресурс відділення між полями пропорційно до терміновості "
                     "(залишок ÷ слак). Якщо ресурсу не вистачає — буде розраховано дефіцит "
                     "і запропоновано варіанти усунення.")
        self.rec_label.setText("".join(lines))

    # ══════════════════════════════════════════
    # RCPSP — допоміжні методи
    # ══════════════════════════════════════════
    def _build_rcpsp_activities(self, fields, days, early_days, late_days):
        """
        Перетворює поля у RCPSP-активності.

        Повертає список активностей:
          {
            "fi":       індекс у fields,
            "division": str,
            "gross":    float,
            "es":       int  (earliest start, день-індекс),
            "dl":       int  (deadline, день-індекс включно),
          }

        ES_j  = перший день горизонту, коли day_date >= ripening - early_days
        DL_j  = останній день горизонту, коли day_date <= ripening + late_days
                (якщо дозрівання невідоме — ES=0, DL=len(days)-1)
        """
        n = len(days)
        activities = []

        log.debug("_build_rcpsp_activities: %d полів, горизонт %d дн., вікно -%d/+%d",
                  len(fields), n, early_days, late_days)

        for fi, f in enumerate(fields):
            rip = f["ripening"]
            if rip is None:
                es, dl = 0, n - 1
            else:
                window_start = rip - timedelta(days=early_days)
                window_end   = rip + timedelta(days=late_days)
                es = next(
                    (i for i, d in enumerate(days)
                     if d.get("date") and d["date"] >= window_start),
                    0
                )
                dl = next(
                    (i for i in range(n - 1, -1, -1)
                     if days[i].get("date") and days[i]["date"] <= window_end),
                    n - 1
                )

            activities.append({
                "fi":       fi,
                "division": f["division"] or "__no_div__",
                "gross":    f["gross"],
                "es":       es,
                "dl":       dl,
            })

            log.debug(
                "  Акт[%03d] %-30s  [%-20s]  вал=%7.0f т  "
                "дозр=%s  ES=день%d(%s)  DL=день%d(%s)  вікно=%d дн.",
                fi, f["name"], f["division"], f["gross"],
                _fmt_date(rip),
                es, _fmt_date(days[es].get("date") if es < n else None),
                dl, _fmt_date(days[dl].get("date") if dl < n else None),
                max(0, dl - es + 1),
            )

        return activities

    def _read_div_caps(self, hours_override=None):
        """
        Зчитує денні потужності відділень (т/день) з таблиці GA-tab.
        Якщо GA вже запускався — усереднює його результати.
        """
        ga_tab     = self.main_app.ga_tab
        harv_a_cap = ga_tab.harv_a_cap.value()
        harv_b_cap = ga_tab.harv_b_cap.value()
        hours      = hours_override if hours_override is not None else ga_tab.hours_spin.value()

        log.debug("_read_div_caps: комбайн А=%s т/год, Б=%s т/год, годин=%s%s",
                  harv_a_cap, harv_b_cap, hours,
                  f" (override)" if hours_override is not None else "")

        div_caps = {}
        for r in range(ga_tab.div_table.rowCount()):
            n = ga_tab.div_table.item(r, 0)
            a = ga_tab.div_table.item(r, 2)
            b = ga_tab.div_table.item(r, 3)
            if not n: continue
            name = n.text().strip()
            if not name: continue
            try: ha = int(a.text() or 0) if a else 0
            except: ha = 0
            try: hb = int(b.text() or 0) if b else 0
            except: hb = 0
            cap = (ha * harv_a_cap + hb * harv_b_cap) * hours
            if cap > 0:
                div_caps[name] = cap
                log.debug("  Відділення %-20s  А=%d  Б=%d  cap=%.0f т/день",
                          name, ha, hb, cap)

        # Якщо GA вже запускався — використовуємо його середню потужність
        if ga_tab.result_data:
            cap_sum = {}; cap_cnt = {}
            for day_row in ga_tab.result_data:
                for dname, info in day_row["divisions"].items():
                    cap_sum[dname] = cap_sum.get(dname, 0) + info.get("capacity", 0)
                    cap_cnt[dname] = cap_cnt.get(dname, 0) + 1
            for dname in cap_sum:
                avg = cap_sum[dname] / cap_cnt[dname]
                log.debug("  GA-усереднення %-20s: %.0f т/день (замість таблиці)", dname, avg)
                div_caps[dname] = avg

        return div_caps

    # ══════════════════════════════════════════
    # RCPSP РОЗКЛАД
    # ══════════════════════════════════════════
    def _compute_rcpsp_schedule(self, fields, days,
                                 div_start_dates=None,
                                 early_days=0, late_days=0,
                                 div_quotas=None,
                                 hours_override=None):
        """
        RCPSP-розклад: жадібний з пріоритетом за слаком і терміновістю.

        Щодня для кожного відділення:
          1. Формуємо множину активних активностей:
               - es_j ≤ t ≤ dl_j  (у вікні)
               - remaining_j > 0   (не завершено)
               - day_date >= div_start_date (відділення вже активне)

          2. Для кожної активності рахуємо:
               slack_j    = dl_j − t               (днів до дедлайну)
               urgency_j  = remaining_j / max(1, slack_j)  (т/день що мінімально потрібно)

          3. Ресурс відділення R_d,t розподіляємо пропорційно до urgency_j:
               x_{j,t} = R_d,t * (urgency_j / Σ urgency_k)

             Але якщо активність досягає свого обсягу — її надлишок повертається
             в пул і перерозподіляється між рештою (ітеративно, до стабілізації).

          4. Обмеження заводу: R_d,t ≤ FACTORY_t * (quota_d / Σ quota)
             або пропорційне масштабування якщо квоти не задані.

        Складність: O(n_days * n_fields * iter) де iter ≤ n_fields.
        """
        if div_start_dates is None:
            div_start_dates = {}

        n_days   = len(days)
        n_fields = len(fields)

        log.info(SEP)
        log.info("RCPSP-РОЗКЛАД: %d полів, %d днів, вікно -%d/+%d%s",
                 n_fields, n_days, early_days, late_days,
                 "  [КВОТИ АКТИВНІ]" if div_quotas else "")
        if div_quotas:
            for div, q in sorted(div_quotas.items()):
                log.info("  Квота %-20s: %.0f т/день", div, q)
        log.info(SEP)

        div_caps  = self._read_div_caps(hours_override=hours_override)
        remaining = [f["gross"] for f in fields]
        schedule  = [[0.0] * n_fields for _ in range(n_days)]

        activities = self._build_rcpsp_activities(fields, days, early_days, late_days)

        # Індекси активностей по відділеннях
        div_act_indices = {}
        for ai, act in enumerate(activities):
            div_act_indices.setdefault(act["division"], []).append(ai)

        # ── Головний цикл по днях ──
        for t, day in enumerate(days):
            day_date   = day.get("date")
            day_demand = day.get("demand")

            log.debug("День t=%03d  %s  попит=%.0f т",
                      t, _fmt_date(day_date), day_demand or 0)

            # Збираємо активні відділення і їх потужності на поточний день
            div_active = {}
            for div, a_indices in div_act_indices.items():
                cap = div_caps.get(div, 0)
                if cap <= 0:
                    continue
                start_date = div_start_dates.get(div)
                if start_date and day_date and day_date < start_date:
                    log.debug("  [%s] пропущено — старт відділення %s", div, _fmt_date(start_date))
                    continue
                active_now = [
                    ai for ai in a_indices
                    if (remaining[activities[ai]["fi"]] > 1e-6
                        and activities[ai]["es"] <= t <= activities[ai]["dl"])
                ]
                if active_now:
                    div_active[div] = {"cap": cap, "active": active_now}
                    log.debug("  [%-20s]  фіз.cap=%.0f т/день  активних акт.=%d",
                              div, cap, len(active_now))
                else:
                    idle_reason = (
                        "всі закриті" if all(remaining[activities[ai]["fi"]] <= 1e-6
                                             for ai in a_indices)
                        else "поза вікном"
                    )
                    log.debug("  [%-20s]  простій (%s)", div, idle_reason)

            if not div_active:
                log.debug("  → жодне відділення не активне — день пропущено")
                continue

            # ── Обмеження заводу ──
            if div_quotas:
                factory_total = sum(div_quotas.values())
                for div_name, info in div_active.items():
                    quota = div_quotas.get(div_name)
                    if quota is not None and factory_total > 0 and day_demand:
                        share       = quota / factory_total
                        day_cap     = day_demand * share
                        old_cap     = info["cap"]
                        info["cap"] = min(info["cap"], day_cap)
                        log.debug(
                            "  Завод-квота [%-20s]: частка=%.3f  денний_ліміт=%.0f т  "
                            "ефект.cap=%.0f т (було %.0f)",
                            div_name, share, day_cap, info["cap"], old_cap)
            else:
                total_avail = sum(info["cap"] for info in div_active.values())
                if day_demand and day_demand > 0 and total_avail > day_demand:
                    scale = day_demand / total_avail
                    for div_name, info in div_active.items():
                        old_cap = info["cap"]
                        info["cap"] *= scale
                        log.debug(
                            "  Завод-масштаб [%-20s]: scale=%.4f  cap %.0f→%.0f т",
                            div_name, scale, old_cap, info["cap"])

            # ── RCPSP розподіл по кожному відділенню (Варіант Б: дискретний жадібний) ──
            #
            # Логіка:
            #   1. Рахуємо urgency_j = remaining_j / slack_j для кожної активності.
            #   2. Сортуємо активності за urgency спадно.
            #   3. Призначаємо кожній активності min(remaining_j, cap_per_field, pool)
            #      починаючи від найтерміновішої.
            #   4. Якщо після призначення залишок на день < MIN_DAILY — поле пропускається
            #      (воно отримає ресурс наступного дня коли urgency зросте).
            #   5. Пул вичерпується послідовно — жодних дробових залишків між полями.
            #
            # cap_per_field = денна продуктивність одного комбайна А (фізичний максимум
            # для одного поля за день). Якщо відділення має 3 комбайни і 5 полів —
            # щодня активні лише 3 поля, решта чекають своєї черги.

            for div_name, info in div_active.items():
                pool          = info["cap"]
                act_indices   = info["active"]

                # Фізичний мінімум виходу на поле і максимум на одне поле за день.
                # Беремо продуктивність комбайна А як одиницю ресурсу.
                ga_tab        = self.main_app.ga_tab
                harv_a_cap    = ga_tab.harv_a_cap.value()
                hours         = (hours_override if hours_override is not None
                                 else ga_tab.hours_spin.value())
                cap_per_field = harv_a_cap * hours          # т/день на 1 комбайн
                min_daily     = max(1.0, cap_per_field * 0.25)  # 25% від комбайна — мінімальний вихід

                log.debug("  RCPSP-Б [%-20s]  пул=%.0f т  акт.=%d  "
                          "cap_per_field=%.0f т  min_daily=%.0f т",
                          div_name, pool, len(act_indices), cap_per_field, min_daily)

                # ── Крок 1: urgency і сортування ──
                urgencies = {}
                for ai in act_indices:
                    act   = activities[ai]
                    fi    = act["fi"]
                    slack = max(1, act["dl"] - t)
                    urgencies[ai] = remaining[fi] / slack

                sorted_acts = sorted(act_indices, key=lambda ai: -urgencies[ai])

                log.debug("  Черга (urgency ↓):")
                for ai in sorted_acts:
                    fi    = activities[ai]["fi"]
                    slack = max(1, activities[ai]["dl"] - t)
                    log.debug(
                        "    Акт[%03d] %-28s  залишок=%7.0f т  "
                        "slack=%2d дн.  urgency=%7.1f",
                        fi, fields[fi]["name"],
                        remaining[fi], slack, urgencies[ai],
                    )

                # ── Крок 2: жадібне призначення ──
                skipped = []
                for ai in sorted_acts:
                    if pool < min_daily:
                        # Пул вичерпано — решта полів не виходять сьогодні
                        log.debug("    Акт[%03d] %-28s  ПРОПУЩЕНО (пул=%.0f < min=%.0f)",
                                  activities[ai]["fi"],
                                  fields[activities[ai]["fi"]]["name"],
                                  pool, min_daily)
                        skipped.append(ai)
                        continue

                    fi   = activities[ai]["fi"]
                    # Скільки може отримати це поле: не більше cap_per_field,
                    # не більше залишку, не більше поточного пулу.
                    take = min(cap_per_field, remaining[fi], pool)

                    if take < min_daily:
                        # Поле майже зібране — беремо залишок повністю (фінальний день)
                        if remaining[fi] <= min_daily:
                            take = remaining[fi]
                            log.debug(
                                "    Акт[%03d] %-28s  фінальний день  take=%.0f т",
                                fi, fields[fi]["name"], take)
                        else:
                            # Некратний залишок — пропускаємо до наступного дня
                            log.debug(
                                "    Акт[%03d] %-28s  ПРОПУЩЕНО (take=%.0f < min=%.0f)",
                                fi, fields[fi]["name"], take, min_daily)
                            skipped.append(ai)
                            continue

                    schedule[t][fi] += take
                    remaining[fi]   -= take
                    pool            -= take

                    closed = remaining[fi] < 1e-6
                    log.debug(
                        "    → Акт[%03d] %-28s  take=%7.0f т  "
                        "remaining=%7.0f т  пул_після=%7.0f т%s",
                        fi, fields[fi]["name"], take, remaining[fi], pool,
                        "  ✅ЗАКРИТО" if closed else "",
                    )

                if skipped:
                    log.debug("  Пропущено сьогодні (%d пол.) — вийдуть завтра: %s",
                              len(skipped),
                              ", ".join(fields[activities[ai]["fi"]]["name"]
                                        for ai in skipped[:5])
                              + ("…" if len(skipped) > 5 else ""))

        # ── Підсумок розкладу ──
        log.info("RCPSP завершено. Залишки після розкладу:")
        total_gross     = sum(f["gross"] for f in fields)
        total_remaining = sum(remaining)
        total_scheduled = total_gross - total_remaining
        for fi, f in enumerate(fields):
            rem = remaining[fi]
            pct = 100.0 * (f["gross"] - rem) / f["gross"] if f["gross"] > 0 else 0
            status = "✅" if rem < 1.0 else "⚠ ЗАЛИШОК"
            log.info(
                "  %s Акт[%03d] %-30s  [%-20s]  вал=%7.0f  "
                "заплановано=%7.0f  залишок=%7.1f  виконання=%.1f%%",
                status, fi, f["name"], f["division"],
                f["gross"], f["gross"] - rem, rem, pct,
            )
        log.info(
            "Всього: вал=%.0f т  заплановано=%.0f т  залишок=%.0f т  виконання=%.1f%%",
            total_gross, total_scheduled, total_remaining,
            100.0 * total_scheduled / total_gross if total_gross > 0 else 0,
        )

        return schedule, remaining

    # ══════════════════════════════════════════
    # ПЕРЕБУДОВА
    # ══════════════════════════════════════════
    def _run_rebuild(self):
        early = self.early_spin.value()
        late  = self.late_spin.value()

        log.info(SEP)
        log.info("ПЕРЕБУДОВА ПЛАНУ (RCPSP)  вікно: -%d / +%d дн.", early, late)
        log.info(SEP)

        fields = self._collect_fields()
        if not fields:
            log.warning("Перебудова: немає полів — скасовано")
            QMessageBox.warning(self, "ДЗВ", "Немає полів для перебудови.")
            return

        no_rip = [f for f in fields if not f["ripening"]]
        if len(no_rip) == len(fields):
            log.warning("Перебудова: жодне поле без дати дозрівання — скасовано")
            QMessageBox.warning(self, "ДЗВ",
                "Жодне поле не має дати дозрівання (колонка «Дозрівання прогноз»).\n"
                "Заповніть основну таблицю.")
            return
        if no_rip:
            log.warning("Перебудова: %d полів без дати дозрівання (буде ES=0, DL=кінець)",
                        len(no_rip))

        days = self.main_app.harvest_plan_tab._get_plan_days()
        if not days:
            log.warning("Перебудова: немає плану заводу — скасовано")
            QMessageBox.warning(self, "ДЗВ",
                "Немає плану заводу. Заповніть вкладку «Оптимізатор ТЗК».")
            return

        log.info("Горизонт: %d днів (%s — %s)",
                 len(days),
                 _fmt_date(days[0].get("date")),
                 _fmt_date(days[-1].get("date")))

        # Перевірка потужностей
        ga_tab = self.main_app.ga_tab
        divisions = []
        for r in range(ga_tab.div_table.rowCount()):
            ni = ga_tab.div_table.item(r, 0)
            gi = ga_tab.div_table.item(r, 1)
            ai = ga_tab.div_table.item(r, 2)
            bi = ga_tab.div_table.item(r, 3)
            if not ni or not gi: continue
            try:
                divisions.append({
                    "name":   ni.text().strip(),
                    "gross":  float(gi.text() or 0),
                    "harv_a": int(ai.text() or 0) if ai else 0,
                    "harv_b": int(bi.text() or 0) if bi else 0,
                })
            except ValueError:
                continue

        factory_plan = [day["demand"] for day in days]
        if divisions and factory_plan:
            if not ga_tab._check_division_capacity(divisions, factory_plan):
                log.warning("Перебудова: перевірка потужностей не пройдена — скасовано")
                return

        div_start_dates = self.main_app.harvest_plan_tab._get_division_start_dates()
        if div_start_dates:
            log.info("Дати старту відділень:")
            for div, sd in div_start_dates.items():
                log.info("  %-20s → %s", div, _fmt_date(sd))

        # RCPSP-розклад
        schedule, remaining = self._compute_rcpsp_schedule(
            fields, days, div_start_dates, early, late)

        # Оновлюємо детальний план і основну таблицю
        self.main_app.harvest_plan_tab._render_table(fields, days, schedule, remaining)
        self.main_app.harvest_plan_tab._fill_main_table_dates(fields, days, schedule)

        # Аналіз після перебудови
        fields_after = self._collect_fields()
        after_result = [{**f, **self._check_window(f, early, late)} for f in fields_after]
        self._analysis_result = after_result

        self._render_analysis_table(after_result)
        self._render_violation_table(after_result)

        viol = [r for r in after_result if r["status"] in ("too_early", "too_late")]

        if not viol:
            log.info("ПЕРЕБУДОВА УСПІШНА — всі поля в межах ДЗВ")
            self._render_recommendations(after_result, early, late)
            QMessageBox.information(self, "ДЗВ — Готово",
                "✅ Plan перебудовано за RCPSP.\n"
                "Всі поля вкладаються в Дозволене Збиральне Вікно!")
        else:
            log.warning("ПЕРЕБУДОВА: залишилось %d порушень", len(viol))
            self._render_recommendations(after_result, early, late)
            plan_start = next((d.get("date") for d in days if d.get("date")), None)
            self._show_remaining_dialog(viol, early, late, days, plan_start)

    # ══════════════════════════════════════════
    # Діалог залишкових порушень
    # ══════════════════════════════════════════
    def _show_remaining_dialog(self, violations, early, late, days=None, plan_start=None):
        log.info(SEP)
        log.info("ДІАЛОГ ЗАЛИШКОВИХ ПОРУШЕНЬ: %d полів", len(violations))
        for rec in violations:
            dev = rec.get("deviation_days")
            log.info("  [%s] %-30s [%-20s]  відхил=%s дн.",
                     rec["status"].upper(), rec["name"], rec["division"],
                     f"{dev:+d}" if dev is not None else "?")

        dlg = QDialog(self)
        dlg.setWindowTitle("⚠ Залишились порушення ДЗВ")
        dlg.setMinimumWidth(640); dlg.setMinimumHeight(500)
        lay = QVBoxLayout()

        lbl = QLabel(f"⚠  Після перебудови {len(violations)} пол(я) все ще порушують ДЗВ")
        f = QFont(); f.setPointSize(11); f.setBold(True)
        lbl.setFont(f); lbl.setStyleSheet("color:#b71c1c;")
        lay.addWidget(lbl)

        txt = QTextEdit(); txt.setReadOnly(True)
        txt.setStyleSheet("font-size:12px;font-family:monospace;")
        lines = [f"ДЗВ: -{early} / +{late} дн. від дозрівання", "─" * 52, ""]
        for rec in violations:
            dev   = rec.get("deviation_days")
            dev_s = (f"+{dev}" if dev and dev > 0 else str(dev)) if dev is not None else "?"
            slbl  = "❌ Запізно" if rec["status"] == "too_late" else "⚠ Зарано"
            lines += [
                f"{slbl}  |  {rec['name']}  [{rec['division']}]",
                f"   Дозрівання:          {_fmt_date(rec['ripening'])}",
                f"   Початок збирання:    {_fmt_date(rec['harvest_start'])}",
                f"   Завершення збирання: {_fmt_date(rec.get('harvest_end'))}",
                f"   Відхилення:          {dev_s} дн.",
                "",
            ]
        txt.setPlainText("\n".join(lines))
        lay.addWidget(txt)

        needs = self._calc_rcpsp_deficit(violations, late, today=plan_start)
        if needs:
            extra_html = self._format_deficit_html(needs)
            el = QLabel(extra_html); el.setWordWrap(True)
            el.setStyleSheet(
                "background:#fff8e1;border:1px solid #ffe082;"
                "padding:8px;font-size:12px;border-radius:4px;")
            lay.addWidget(el)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine); lay.addWidget(sep)

        opt_lbl = QLabel("<b>Оберіть варіант усунення дефіциту:</b>")
        opt_lbl.setStyleSheet("font-size:12px;")
        lay.addWidget(opt_lbl)

        btn_group = QButtonGroup(dlg)
        r1 = QRadioButton("🚜  Варіант 1: Додати техніку (авторозрахунок комбайнів)")
        r2 = QRadioButton("⏱  Варіант 2: Збільшити години роботи техніки")
        r3 = QRadioButton("📊  Варіант 3: Перерозподілити квоту заводу між відділеннями")
        r4 = QRadioButton("✅  Варіант 4: Прийняти залишкові порушення")
        r1.setChecked(True)
        for rb in (r1, r2, r3, r4):
            btn_group.addButton(rb)
            lay.addWidget(rb)

        r3_hint = QLabel(
            "   ↳ Відділення з найбільшим RCPSP-тиском (залишок ÷ слак) "
            "отримують більшу частку від ліміту заводу. "
            "Загальний ліміт заводу не змінюється."
        )
        r3_hint.setWordWrap(True)
        r3_hint.setStyleSheet("color:#555; font-size:11px; margin-left:24px;")
        lay.addWidget(r3_hint)

        hours_row = QHBoxLayout()
        hours_lbl = QLabel("Нові години роботи на день:")
        self._extra_hours_spin = QSpinBox()
        self._extra_hours_spin.setRange(1, 24)
        ga_tab = self.main_app.ga_tab
        self._extra_hours_spin.setValue(min(24, ga_tab.hours_spin.value() + 2))
        self._extra_hours_spin.setSuffix(" год/день")
        self._extra_hours_spin.setFixedWidth(130)
        self._extra_hours_spin.setEnabled(False)
        hours_row.addSpacing(24)
        hours_row.addWidget(hours_lbl)
        hours_row.addWidget(self._extra_hours_spin)
        hours_row.addStretch()
        lay.addLayout(hours_row)

        r2.toggled.connect(self._extra_hours_spin.setEnabled)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.HLine); lay.addWidget(sep2)

        br = QHBoxLayout()
        btn_ok = QPushButton("Застосувати")
        btn_ok.setFixedHeight(36)
        btn_ok.setStyleSheet(
            "background:#2e7d32;color:white;font-weight:bold;"
            "border-radius:4px;padding:0 20px;")
        btn_cancel = QPushButton("Скасувати")
        btn_cancel.setFixedHeight(36)
        btn_cancel.setStyleSheet(
            "background:#757575;color:white;font-weight:bold;"
            "border-radius:4px;padding:0 20px;")

        def on_apply():
            chosen = (
                "Варіант 1: Додати техніку" if r1.isChecked() else
                f"Варіант 2: Збільшити години до {self._extra_hours_spin.value()}" if r2.isChecked() else
                "Варіант 3: Перерозподіл квот" if r3.isChecked() else
                "Варіант 4: Прийняти порушення"
            )
            log.info("Обрано: %s", chosen)
            if r1.isChecked():
                dlg.accept()
                self._apply_extra_machines(violations, late, needs, plan_start)
            elif r2.isChecked():
                dlg.accept()
                self._apply_extra_hours(self._extra_hours_spin.value())
            elif r3.isChecked():
                dlg.accept()
                self._apply_quota_redistribution(violations, days, late)
            else:
                log.info("Залишкові порушення прийнято без змін")
                dlg.accept()

        btn_ok.clicked.connect(on_apply)
        btn_cancel.clicked.connect(dlg.reject)
        br.addWidget(btn_ok); br.addWidget(btn_cancel)
        lay.addLayout(br)

        dlg.setLayout(lay); dlg.exec()

    # ══════════════════════════════════════════
    # RCPSP-дефіцит потужності
    # ══════════════════════════════════════════
    def _calc_rcpsp_deficit(self, violations, late_days, today=None):
        """
        Мінімально необхідний приріст денної потужності по відділеннях.

        RCPSP-формулювання:
          Для кожного відділення d з полями «Запізно»:
            slack_j  = (deadline_j - today).days  (залишок часового вікна)
            needed_j = remaining_j / max(1, slack_j)  (мінімальна швидкість)
            R_needed = Σ_j needed_j               (пропускна здатність для паралельного виконання)
            gap      = max(0, R_needed - R_d)     (дефіцит ресурсу)
        """
        late_v = [v for v in violations if v["status"] == "too_late"]
        if not late_v:
            log.info("_calc_rcpsp_deficit: запізнень немає — дефіцит не розраховується")
            return []

        if today is None:
            days = self.main_app.harvest_plan_tab._get_plan_days()
            if days:
                today = next((d.get("date") for d in days if d.get("date")), date.today())
            else:
                today = date.today()

        log.info(SEP)
        log.info("RCPSP-ДЕФІЦИТ: точка відліку %s, +%d дн. від дозрівання", _fmt_date(today), late_days)

        ga     = self.main_app.ga_tab
        ha_cap = ga.harv_a_cap.value()
        hb_cap = ga.harv_b_cap.value()
        hours  = ga.hours_spin.value()
        one_A  = ha_cap * hours
        one_B  = hb_cap * hours

        log.debug("  Продуктивність: комбайн А=%.0f т/день, Б=%.0f т/день", one_A, one_B)

        div_caps = self._read_div_caps()

        by_div = {}
        for v in late_v:
            by_div.setdefault(v["division"], []).append(v)

        result = []
        for div, vs in by_div.items():
            if not div:
                continue

            log.info("  Відділення [%s]:", div)
            r_needed = 0.0
            nearest_deadline = None

            for v in vs:
                rip = v.get("ripening")
                if not rip:
                    log.debug("    Поле %-28s: немає дати дозрівання — пропущено", v["name"])
                    continue
                deadline = rip + timedelta(days=late_days)
                slack    = max(1, (deadline - today).days)
                speed    = v["gross"] / slack
                r_needed += speed
                log.info(
                    "    %-30s  вал=%7.0f т  дедлайн=%s  slack=%d дн.  "
                    "мін.швидкість=%.1f т/день",
                    v["name"], v["gross"], _fmt_date(deadline), slack, speed,
                )
                if nearest_deadline is None or deadline < nearest_deadline:
                    nearest_deadline = deadline

            if nearest_deadline is None:
                log.info("    → немає полів з датою дозрівання — пропущено")
                continue

            horizon  = max(1, (nearest_deadline - today).days)
            curr_cap = div_caps.get(div, 0)
            gap      = r_needed - curr_cap

            log.info(
                "    Σ(needed)=%.0f т/день  поточна cap=%.0f т/день  "
                "gap=%.0f т/день  горизонт=%d дн.",
                r_needed, curr_cap, gap, horizon,
            )

            if gap <= 0:
                log.info("    → дефіцит відсутній (cap достатня)")
                continue

            extra_A = math.ceil(gap / one_A) if one_A > 0 else None
            extra_B = math.ceil(gap / one_B) if one_B > 0 else None

            log.info(
                "    → ДЕФІЦИТ %.0f т/день  потрібно: +%s комб. А  або  +%s комб. Б",
                gap,
                str(extra_A) if extra_A is not None else "?",
                str(extra_B) if extra_B is not None else "?",
            )

            result.append({
                "division":     div,
                "horizon_days": horizon,
                "needed_daily": r_needed,
                "current_cap":  curr_cap,
                "gap":          gap,
                "extra_A":      extra_A,
                "extra_B":      extra_B,
            })

        if not result:
            log.info("RCPSP-дефіцит: всі відділення мають достатню потужність")
        return result

    def _format_deficit_html(self, needs):
        lines = ["<b>Розрахунок дефіциту (RCPSP):</b><br>"]
        for rec in needs:
            lines.append(
                f"<b>{rec['division']}</b>: горизонт {rec['horizon_days']} дн., "
                f"Σ(залишок÷слак) = {rec['needed_daily']:.0f} т/день, "
                f"є {rec['current_cap']:.0f} т/день, "
                f"дефіцит <b>{rec['gap']:.0f} т/день</b><br>"
                f"&nbsp;&nbsp;→ додати "
                f"<b>+{rec['extra_A']} комб. А</b>"
                + (f" або <b>+{rec['extra_B']} комб. Б</b>" if rec.get('extra_B') else "")
                + "<br>"
            )
        return "".join(lines)

    # ══════════════════════════════════════════
    # Перерозподіл заводської квоти (RCPSP-pressure)
    # ══════════════════════════════════════════
    def _calc_quota_redistribution(self, violations, days, late_days):
        """
        Перерозподіляє денну квоту заводу між відділеннями на основі
        RCPSP-тиску (pressure):

          pressure_d = Σ_{j∈d, порушники} (gross_j / max(1, slack_j))
        """
        log.info(SEP)
        log.info("RCPSP ПЕРЕРОЗПОДІЛ КВОТ (late_days=%d)", late_days)

        if not days:
            log.warning("_calc_quota_redistribution: немає днів плану")
            return {}

        ga_tab     = self.main_app.ga_tab
        ha_cap     = ga_tab.harv_a_cap.value()
        hb_cap     = ga_tab.harv_b_cap.value()
        hours      = ga_tab.hours_spin.value()
        plan_table = ga_tab.plan_table

        raw_demands = []
        for r in range(plan_table.rowCount()):
            d_item = plan_table.item(r, 1)
            try:
                val = float(d_item.text()) if d_item else 0.0
                if val > 0:
                    raw_demands.append(val)
            except (ValueError, AttributeError):
                continue

        demands = raw_demands if raw_demands else [
            d.get("demand", 0) for d in days if d.get("demand", 0) > 0
        ]
        if not demands:
            log.warning("_calc_quota_redistribution: немає даних про попит заводу")
            return {}

        factory_daily = sum(demands) / len(demands)
        log.info("  Середня денна потреба заводу: %.0f т/день (%d днів)", factory_daily, len(demands))

        div_caps = self._read_div_caps()
        if not div_caps:
            log.warning("_calc_quota_redistribution: потужності відділень не знайдено")
            return {}

        late_divs = set(
            v["division"] for v in violations
            if v["status"] == "too_late" and v["division"]
        )
        log.info("  Гарячі відділення (too_late): %s", list(late_divs))

        plan_start    = next((d.get("date") for d in days if d.get("date")), date.today())
        plan_days_cnt = max(1, len(days))

        # RCPSP-тиск для гарячих відділень
        div_pressure = {}
        for div in late_divs:
            vs = [v for v in violations
                  if v["division"] == div and v["status"] == "too_late"]
            pressure = 0.0
            log.info("  Тиск [%s]:", div)
            for v in vs:
                rip      = v.get("ripening")
                deadline = (rip + timedelta(days=late_days)) if rip else None
                slack    = max(1, (deadline - plan_start).days) if deadline else 1
                contrib  = v["gross"] / slack
                pressure += contrib
                log.info("    %-28s  вал=%7.0f т  дедлайн=%s  slack=%d дн.  внесок=%.1f",
                         v["name"], v["gross"], _fmt_date(deadline), slack, contrib)
            div_pressure[div] = max(pressure, 1e-9)
            log.info("  → pressure[%s] = %.1f", div, div_pressure[div])

        # Мінімальні квоти для спокійних відділень
        all_fields = self._collect_fields()
        min_quotas = {}
        reserved   = 0.0

        log.info("  Мінімальні квоти (спокійні відділення):")
        for div, cap in div_caps.items():
            if div in late_divs:
                continue
            div_gross  = sum(f["gross"] for f in all_fields
                             if f["division"] == div and f["gross"] > 0)
            min_q      = min(cap, div_gross / plan_days_cnt)
            min_quotas[div] = min_q
            reserved       += min_q
            log.info("    %-20s  gross=%.0f т  min_q=%.0f т/день  cap=%.0f т/день",
                     div, div_gross, min_q, cap)

        pool = max(0.0, factory_daily - reserved)
        log.info("  Пул для гарячих: factory=%.0f - reserved=%.0f = %.0f т/день",
                 factory_daily, reserved, pool)

        # Розподіл пулу пропорційно RCPSP-тиску
        total_pressure = sum(div_pressure.values())
        hot_quotas     = {}
        log.info("  Розподіл пулу (total_pressure=%.1f):", total_pressure)
        for div in late_divs:
            if div not in div_caps:
                log.warning("    [%s] відсутній у div_caps — пропущено", div)
                continue
            share           = pool * (div_pressure[div] / total_pressure)
            hot_quotas[div] = min(div_caps[div], share)
            log.info("    %-20s  частка=%.1f т  cap=%.0f т  квота=%.0f т/день",
                     div, share, div_caps[div], hot_quotas[div])

        # Залишок пулу → спокійним рівномірно
        used_by_hot   = sum(hot_quotas.values())
        leftover_pool = pool - used_by_hot
        if leftover_pool > 0.1 and min_quotas:
            per_ok = leftover_pool / len(min_quotas)
            log.info("  Залишок пулу %.0f т → +%.0f т/день кожному спокійному відділенню",
                     leftover_pool, per_ok)
            for div in min_quotas:
                old = min_quotas[div]
                min_quotas[div] = min(div_caps[div], min_quotas[div] + per_ok)
                log.info("    %-20s  %.0f → %.0f т/день", div, old, min_quotas[div])

        quotas  = {**min_quotas, **hot_quotas}
        total_q = sum(quotas.values())
        log.info("  До нормування: Σквот=%.0f т/день (ціль=%.0f)", total_q, factory_daily)
        if total_q > 1e-9 and abs(total_q - factory_daily) > 0.5:
            scale  = factory_daily / total_q
            quotas = {div: q * scale for div, q in quotas.items()}
            log.info("  Нормування: scale=%.4f", scale)

        log.info("  ФІНАЛЬНІ КВОТИ:")
        for div, q in sorted(quotas.items()):
            marker = "🔴" if div in late_divs else "✅"
            log.info("    %s %-20s: %.0f т/день", marker, div, q)
        log.info("  Σ квот = %.0f т/день", sum(quotas.values()))

        return quotas

    def _apply_quota_redistribution(self, violations, days, late_days):
        """
        Застосовує RCPSP-перерозподіл квот і перезапускає розклад.
        """
        quotas = self._calc_quota_redistribution(violations, days, late_days)
        if not quotas:
            log.warning("_apply_quota_redistribution: квоти не розраховані")
            QMessageBox.warning(self, "ДЗВ",
                "Не вдалося розрахувати перерозподіл квот.\n"
                "Перевірте потужності відділень і план заводу.")
            return

        late_div_names = {v["division"] for v in violations if v["status"] == "too_late"}
        lines = ["Нові денні квоти (RCPSP-тиск):\n"]
        for div, q in sorted(quotas.items()):
            marker = "🔴" if div in late_div_names else "✅"
            lines.append(f"  {marker}  {div}: {q:.0f} т/день")
        lines.append(
            f"\n  Сума: {sum(quotas.values()):.0f} т/день  "
            f"(ліміт заводу зберігається)")
        QMessageBox.information(self, "ДЗВ — Квоти перерозподілено",
            "\n".join(lines) + "\n\nЗараз план буде перебудовано.")

        early = self.early_spin.value()
        late  = self.late_spin.value()
        fields = self._collect_fields()
        div_start_dates = self.main_app.harvest_plan_tab._get_division_start_dates()

        log.info("Перезапуск RCPSP з квотами...")
        schedule, remaining = self._compute_rcpsp_schedule(
            fields, days, div_start_dates, early, late,
            div_quotas=quotas)

        self.main_app.harvest_plan_tab._render_table(fields, days, schedule, remaining)
        self.main_app.harvest_plan_tab._fill_main_table_dates(fields, days, schedule)

        fields_after = self._collect_fields()
        after_result = [{**f, **self._check_window(f, early, late)} for f in fields_after]
        self._analysis_result = after_result
        self._render_analysis_table(after_result)
        self._render_violation_table(after_result)

        viol = [r for r in after_result if r["status"] in ("too_early", "too_late")]
        plan_start = next((d.get("date") for d in days if d.get("date")), None)
        if not viol:
            log.info("Після перерозподілу квот: всі поля в ДЗВ ✅")
            self._render_recommendations(after_result, early, late)
            QMessageBox.information(self, "ДЗВ — Готово",
                "✅ Після перерозподілу квот (RCPSP) всі поля вкладаються в ДЗВ!")
        else:
            log.warning("Після перерозподілу квот: залишилось %d порушень", len(viol))
            self._render_recommendations(after_result, early, late)
            self._show_remaining_dialog(viol, early, late, days, plan_start)

    # ══════════════════════════════════════════
    # Варіант 1 — додати комбайни
    # ══════════════════════════════════════════
    def _apply_extra_machines(self, violations, late_days, needs, plan_start=None):
        log.info(SEP)
        log.info("ВАРІАНТ 1: додати техніку")
        if not needs:
            log.info("  RCPSP-дефіцит = 0 — доданої техніки не потрібно")
            QMessageBox.information(self, "ДЗВ — Техніки достатньо",
                "RCPSP-дефіцит відсутній — доданої техніки вистачає.\n\n"
                "Залишкові порушення пов'язані з тим, що частина полів дозріває пізніше "
                "за дату старту відділення або вікно занадто вузьке. Спробуйте:\n"
                "  • Зменшити параметр «не пізніше ніж через N днів»\n"
                "  • Або прийняти ці відхилення як неминучі.")
            return

        added = self._auto_add_capacity(needs)
        lines = []
        for div, rec in added.items():
            lines.append(
                f"  • {div}:  +{rec['extra_A']} комбайн(ів) типу А  "
                f"(RCPSP-дефіцит {rec['gap']:.0f} т/день, горизонт {rec['horizon_days']} дн.)"
            )
            log.info("  [%s] +%d комб. А  дефіцит=%.0f т/день  горизонт=%d дн.",
                     div, rec["extra_A"], rec["gap"], rec["horizon_days"])

        QMessageBox.information(self, "ДЗВ — Потужності збільшено",
            "Автоматично додано в таблицю відділень:\n\n"
            + "\n".join(lines) +
            "\n\nЗараз план буде перебудовано.")
        self._run_rebuild()

    def _auto_add_capacity(self, needs):
        """Дописує розраховану кількість комбайнів А у таблицю відділень."""
        ga    = self.main_app.ga_tab
        added = {}
        for rec in needs:
            div     = rec["division"]
            extra_A = rec["extra_A"]
            if extra_A is None or extra_A <= 0:
                continue
            row = None
            for r in range(ga.div_table.rowCount()):
                n = ga.div_table.item(r, 0)
                if n and n.text().strip() == div:
                    row = r; break
            if row is None:
                log.warning("_auto_add_capacity: відділення [%s] не знайдено в таблиці", div)
                continue
            a_item = ga.div_table.item(row, 2)
            try:
                cur_a = int(a_item.text() or 0) if a_item else 0
            except ValueError:
                cur_a = 0
            new_a = cur_a + extra_A
            if a_item:
                a_item.setText(str(new_a))
            else:
                ga.div_table.setItem(row, 2, QTableWidgetItem(str(new_a)))
            log.info("  Таблиця відділень [%-20s]: комб. А %d → %d (+%d)", div, cur_a, new_a, extra_A)
            added[div] = rec
        return added

    # ══════════════════════════════════════════
    # Варіант 2 — збільшити години роботи
    # ══════════════════════════════════════════
    def _apply_extra_hours(self, new_hours: int):
        ga_tab    = self.main_app.ga_tab
        old_hours = ga_tab.hours_spin.value()
        log.info(SEP)
        log.info("ВАРІАНТ 2: зміна годин роботи %d → %d год/день", old_hours, new_hours)
        if new_hours <= old_hours:
            log.warning("  Нове значення (%d) не більше поточного (%d) — скасовано",
                        new_hours, old_hours)
            QMessageBox.warning(self, "ДЗВ",
                f"Нове значення ({new_hours} год) не більше поточного ({old_hours} год).\n"
                "Збільшіть кількість годин.")
            return
        ga_tab.hours_spin.setValue(new_hours)
        log.info("  Встановлено %d год/день, запускаємо перебудову", new_hours)
        QMessageBox.information(self, "ДЗВ — Години оновлено",
            f"Робочий день змінено: {old_hours} → {new_hours} год/день.\n"
            "Зараз план буде перебудовано.")
        self._run_rebuild()

    # Залишаємо для зворотної сумісності якщо викликається ззовні
    def _estimate_extra(self, violations):
        needs = self._calc_rcpsp_deficit(violations, self.late_spin.value())
        if not needs:
            return None
        return self._format_deficit_html(needs)
