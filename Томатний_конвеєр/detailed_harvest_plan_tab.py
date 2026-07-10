import os
import csv
from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
  QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
  QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
  QScrollArea, QSizePolicy, QDateEdit, QFileDialog
)
from PySide6.QtGui import QColor, QFont

try:
    import openpyxl
    from openpyxl.styles import (
        PatternFill, Font as XFont, Alignment, Border, Side
    )
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False


class DetailedHarvestPlanTab(QWidget):
    """
    Детальний план збирання.

    Структура таблиці:
        Рядок 0  — «Плановий вал (т)»   — вал кожного поля
        Рядки 1+ — дати збирання        — скільки тонн зібрано з кожного поля в цей день

    Логіка заповнення:
        • Поля збираються ПОСЛІДОВНО (по порядку в основній таблиці).
        • Щодня відділення, до якого належить поле, має розраховану добову
          потужність (з GA-результату або за формулою).
        • Ця потужність «списується» з валу поточного поля.
        • Якщо вал поля вичерпується до кінця дня — залишок потужності
          переходить на наступне поле (показується в тому ж рядку, але в
          стовпці наступного поля).
        • Якщо GA ще не запускався — потужність обраховується безпосередньо
          з даних комбайнів відділення.
    """

    def __init__(self, main_app):
        super().__init__()
        self.main_app = main_app
        # Кешуємо останній розрахований розклад для експорту
        self._last_fields   = []
        self._last_days     = []
        self._last_schedule = []
        self._last_remaining = []
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        # — Заголовок + кнопки —
        top_row = QHBoxLayout()
        title = QLabel("Детальний план збирання")
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title.setFont(title_font)
        top_row.addWidget(title)
        top_row.addStretch()

        self.btn_refresh = QPushButton("🔄  Оновити план")
        self.btn_refresh.setFixedHeight(34)
        self.btn_refresh.clicked.connect(self.refresh)
        top_row.addWidget(self.btn_refresh)

        self.btn_export_xlsx = QPushButton("📊  Експорт Excel")
        self.btn_export_xlsx.setFixedHeight(34)
        self.btn_export_xlsx.setStyleSheet(
            "background:#1565c0;color:white;font-weight:bold;"
            "border-radius:4px;padding:0 12px;")
        self.btn_export_xlsx.clicked.connect(self._export_xlsx)
        self.btn_export_xlsx.setEnabled(False)
        top_row.addWidget(self.btn_export_xlsx)

        self.btn_export_csv = QPushButton("📄  Експорт CSV")
        self.btn_export_csv.setFixedHeight(34)
        self.btn_export_csv.setStyleSheet(
            "background:#37474f;color:white;font-weight:bold;"
            "border-radius:4px;padding:0 12px;")
        self.btn_export_csv.clicked.connect(self._export_csv)
        self.btn_export_csv.setEnabled(False)
        top_row.addWidget(self.btn_export_csv)

        layout.addLayout(top_row)

        # — Підказка —
        hint = QLabel(
            "Поля збираються послідовно. "
            "Потужність відділення щодня списується з валу поточного поля; "
            "при переході на наступне — обидві суми відображаються в одному рядку."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)

        # — Таблиця —
        self.table = QTableWidget()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setStyleSheet("""
            QTableWidget { gridline-color: #d0d0d0; font-size: 12px; }
            QHeaderView::section {
                background-color: #2e7d32;
                color: white;
                font-weight: bold;
                padding: 4px;
                border: 1px solid #1b5e20;
            }
        """)
        layout.addWidget(self.table)

        # — Підсумковий рядок —
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.setLayout(layout)

    # ------------------------------------------------------------------
    # Збір даних
    # ------------------------------------------------------------------
    def _get_fields(self):
        main_table = self.main_app.table
        fields = []
        for r in range(main_table.rowCount()):
            name_item  = main_table.item(r, 0)
            gross_item = main_table.item(r, 3)
            div_item   = main_table.item(r, 10)

            name = name_item.text().strip() if name_item else f"Поле {r+1}"
            if not name:
                name = f"Поле {r+1}"

            try:
                gross = float(gross_item.text()) if gross_item else 0.0
            except ValueError:
                gross = 0.0

            division = div_item.text().strip() if div_item else ""

            if gross <= 0:
                continue

            fields.append({
                "name":     name,
                "gross":    gross,
                "division": division,
                "row_idx":  r,
            })
        return fields

    def _get_division_start_dates(self):
        ga_tab    = self.main_app.ga_tab
        div_table = ga_tab.div_table
        result    = {}
        for r in range(div_table.rowCount()):
            n_item = div_table.item(r, 0)
            if not n_item:
                continue
            name = n_item.text().strip()
            if not name:
                continue
            widget = div_table.cellWidget(r, 4)
            if isinstance(widget, QDateEdit):
                result[name] = widget.date().toPython()
            else:
                result[name] = None
        return result

    def _get_plan_days(self):
        ga_tab     = self.main_app.ga_tab
        plan_table = ga_tab.plan_table
        div_caps   = self._calc_division_capacities(ga_tab)

        days = []
        for r in range(plan_table.rowCount()):
            date_item   = plan_table.item(r, 0)
            demand_item = plan_table.item(r, 1)
            date_str    = date_item.text() if date_item else f"День {r+1}"

            day_date = None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    day_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue

            try:
                demand = float(demand_item.text()) if demand_item else 0.0
            except ValueError:
                demand = 0.0

            days.append({
                "date_str": date_str,
                "date":     day_date,
                "demand":   demand,
                "div_caps": div_caps,
            })
        return days

    def _calc_division_capacities(self, ga_tab):
        div_caps = {}
        if ga_tab.result_data:
            cap_sum = {}; cap_cnt = {}
            for day_row in ga_tab.result_data:
                for div_name, info in day_row["divisions"].items():
                    cap_sum[div_name] = cap_sum.get(div_name, 0) + info.get("capacity", 0)
                    cap_cnt[div_name] = cap_cnt.get(div_name, 0) + 1
            for div_name in cap_sum:
                div_caps[div_name] = cap_sum[div_name] / cap_cnt[div_name]
            return div_caps

        div_table  = ga_tab.div_table
        harv_a_cap = ga_tab.harv_a_cap.value()
        harv_b_cap = ga_tab.harv_b_cap.value()
        hours      = ga_tab.hours_spin.value()

        for r in range(div_table.rowCount()):
            n_item = div_table.item(r, 0)
            a_item = div_table.item(r, 2)
            b_item = div_table.item(r, 3)
            if not n_item:
                continue
            name = n_item.text().strip()
            try:
                harv_a = int(a_item.text() or 0) if a_item else 0
            except ValueError:
                harv_a = 0
            try:
                harv_b = int(b_item.text() or 0) if b_item else 0
            except ValueError:
                harv_b = 0
            cap = (harv_a * harv_a_cap + harv_b * harv_b_cap) * hours
            if cap > 0:
                div_caps[name] = cap
        return div_caps

    # ------------------------------------------------------------------
    # Основна логіка розподілу
    # ------------------------------------------------------------------
    def _compute_schedule(self, fields, days, div_start_dates=None):
        if div_start_dates is None:
            div_start_dates = {}

        n_days   = len(days)
        n_fields = len(fields)

        remaining = [f["gross"] for f in fields]
        schedule  = [[0.0] * n_fields for _ in range(n_days)]

        div_queues = {}
        for f_idx, field in enumerate(fields):
            div = field.get("division", "") or "__no_div__"
            div_queues.setdefault(div, []).append(f_idx)

        div_queue_pos = {div: 0 for div in div_queues}

        ga_tab     = self.main_app.ga_tab
        harv_a_cap = ga_tab.harv_a_cap.value()
        harv_b_cap = ga_tab.harv_b_cap.value()
        hours      = ga_tab.hours_spin.value()

        div_harvesters = {}
        div_table = ga_tab.div_table
        for r in range(div_table.rowCount()):
            n_item = div_table.item(r, 0)
            a_item = div_table.item(r, 2)
            b_item = div_table.item(r, 3)
            if not n_item:
                continue
            name = n_item.text().strip()
            if not name:
                continue
            try:
                harv_a = int(a_item.text() or 0) if a_item else 0
            except ValueError:
                harv_a = 0
            try:
                harv_b = int(b_item.text() or 0) if b_item else 0
            except ValueError:
                harv_b = 0
            div_harvesters[name] = {"a": harv_a, "b": harv_b}

        def calc_cap(harvesters):
            return (harvesters["a"] * harv_a_cap + harvesters["b"] * harv_b_cap) * hours

        current_caps = {
            div: calc_cap(h) for div, h in div_harvesters.items()
        }

        for d_idx, day in enumerate(days):
            day_date = day.get("date")
            demand   = day["demand"]

            eligible = {}
            active   = {}

            for div in div_queues:
                cap = current_caps.get(div, 0)
                if cap <= 0:
                    continue
                start_date = div_start_dates.get(div)
                if start_date and day_date and day_date < start_date:
                    continue
                eligible[div] = cap
                has_remaining = any(
                    remaining[div_queues[div][i]] > 0.001
                    for i in range(div_queue_pos[div], len(div_queues[div]))
                )
                if has_remaining:
                    active[div] = cap

            if not active:
                continue

            total_eligible_cap = sum(eligible.values())
            total_active_cap   = sum(active.values())

            if total_eligible_cap <= demand:
                quotas = {div: cap for div, cap in active.items()}
            else:
                finished_share = sum(
                    demand * (cap / total_eligible_cap)
                    for div, cap in eligible.items()
                    if div not in active
                )
                active_demand = demand - finished_share
                quotas = {
                    div: active_demand * (cap / total_active_cap)
                    for div, cap in active.items()
                }

                for _ in range(len(active)):
                    leftover = 0.0
                    capped   = set()
                    for div, quota in quotas.items():
                        queue   = div_queues[div]
                        pos     = div_queue_pos[div]
                        div_rem = sum(remaining[queue[i]] for i in range(pos, len(queue)))
                        if div_rem < quota - 0.001:
                            leftover     += quota - div_rem
                            quotas[div]   = div_rem
                            capped.add(div)
                    if leftover < 0.001:
                        break
                    free     = {div: active[div] for div in active if div not in capped}
                    free_cap = sum(free.values())
                    if free_cap < 0.001:
                        break
                    for div, cap in free.items():
                        quotas[div] = quotas[div] + leftover * (cap / free_cap)

                total_quota = sum(quotas.values())
                if total_quota > demand + 0.001:
                    scale  = demand / total_quota
                    quotas = {div: q * scale for div, q in quotas.items()}

            for div, quota in quotas.items():
                pos          = div_queue_pos[div]
                capacity_left = quota
                queue         = div_queues[div]
                while capacity_left > 0.001 and pos < len(queue):
                    f_idx = queue[pos]
                    if remaining[f_idx] <= 0.001:
                        pos += 1
                        continue
                    take = min(remaining[f_idx], capacity_left)
                    schedule[d_idx][f_idx] += take
                    remaining[f_idx]       -= take
                    capacity_left          -= take
                    if remaining[f_idx] <= 0.001:
                        pos += 1
                div_queue_pos[div] = pos

            just_finished = []
            still_active  = []
            for div in div_queues:
                cap = current_caps.get(div, 0)
                if cap <= 0:
                    continue
                start_date = div_start_dates.get(div)
                if start_date and day_date and day_date < start_date:
                    continue
                queue   = div_queues[div]
                pos     = div_queue_pos[div]
                div_rem = sum(remaining[queue[i]] for i in range(pos, len(queue)))
                if div_rem <= 0.001:
                    just_finished.append(div)
                else:
                    still_active.append(div)

            if just_finished and still_active:
                freed_cap = sum(current_caps[div] for div in just_finished)
                active_remainders = {}
                for div in still_active:
                    queue = div_queues[div]
                    pos   = div_queue_pos[div]
                    active_remainders[div] = sum(
                        remaining[queue[i]] for i in range(pos, len(queue))
                    )
                total_rem = sum(active_remainders.values())
                if total_rem > 0.001:
                    for div in still_active:
                        share = active_remainders[div] / total_rem
                        current_caps[div] += freed_cap * share
                for div in just_finished:
                    current_caps[div] = 0.0

        # Діагностичний лог
        import os
        log_lines = ["=== ДІАГНОСТИКА ЗБИРАННЯ ==="]
        total_scheduled = sum(
            schedule[d][f] for d in range(n_days) for f in range(n_fields)
        )
        total_gross = sum(f["gross"] for f in fields)
        log_lines.append(f"Загальний вал: {total_gross:,.1f} т")
        log_lines.append(f"Заплановано:   {total_scheduled:,.1f} т")
        log_lines.append(f"Різниця:       {total_gross - total_scheduled:,.1f} т")
        log_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "harvest_debug.log"
        )
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write("\n".join(log_lines))

        return schedule, remaining

    # ------------------------------------------------------------------
    # Побудова таблиці
    # ------------------------------------------------------------------
    def refresh(self):
        fields = self._get_fields()
        if not fields:
            QMessageBox.warning(
                self, "Помилка",
                "Основна таблиця порожня або не містить полів з валовим збором.\n"
                "Заповніть основну таблицю та повторіть спробу."
            )
            return

        days = self._get_plan_days()
        if not days:
            QMessageBox.warning(
                self, "Помилка",
                "План заводу порожній.\n"
                "Додайте дні у вкладці «Оптимізатор ТЗК» та повторіть спробу."
            )
            return

        ga_tab    = self.main_app.ga_tab
        divisions = []
        for r in range(ga_tab.div_table.rowCount()):
            name_item   = ga_tab.div_table.item(r, 0)
            gross_item  = ga_tab.div_table.item(r, 1)
            harv_a_item = ga_tab.div_table.item(r, 2)
            harv_b_item = ga_tab.div_table.item(r, 3)
            if not name_item or not gross_item:
                continue
            try:
                divisions.append({
                    "name":   name_item.text().strip(),
                    "gross":  float(gross_item.text() or 0),
                    "harv_a": int(harv_a_item.text() or 0) if harv_a_item else 0,
                    "harv_b": int(harv_b_item.text() or 0) if harv_b_item else 0,
                })
            except ValueError:
                continue

        factory_plan = [day["demand"] for day in days]
        if divisions and factory_plan:
            if not ga_tab._check_division_capacity(divisions, factory_plan):
                return

        div_start_dates          = self._get_division_start_dates()
        schedule, remaining      = self._compute_schedule(fields, days, div_start_dates)
        self._render_table(fields, days, schedule, remaining)
        self._fill_main_table_dates(fields, days, schedule)

    def _render_table(self, fields, days, schedule, remaining):
        n_days   = len(days)
        n_fields = len(fields)

        # Кешуємо для експорту
        self._last_fields    = fields
        self._last_days      = days
        self._last_schedule  = schedule
        self._last_remaining = remaining

        col_labels = ["Дата", "Потреба\nзаводу (т)"] + [f["name"] for f in fields]
        n_cols     = len(col_labels)

        self.table.setColumnCount(n_cols)
        self.table.setRowCount(1 + n_days)
        self.table.setHorizontalHeaderLabels(col_labels)

        # Рядок 0: Плановий вал
        val_item = QTableWidgetItem("Плановий\nвал (т)")
        val_item.setFont(self._bold_font())
        val_item.setBackground(QColor("#1b5e20"))
        val_item.setForeground(QColor("white"))
        self.table.setItem(0, 0, val_item)

        empty_demand = QTableWidgetItem("—")
        empty_demand.setBackground(QColor("#1b5e20"))
        empty_demand.setForeground(QColor("white"))
        empty_demand.setTextAlignment(Qt.AlignCenter)
        self.table.setItem(0, 1, empty_demand)

        for f_idx, field in enumerate(fields):
            item = QTableWidgetItem(f"{field['gross']:,.0f}")
            item.setTextAlignment(Qt.AlignCenter)
            item.setFont(self._bold_font())
            item.setBackground(QColor("#c8e6c9"))
            item.setForeground(QColor("black"))
            self.table.setItem(0, 2 + f_idx, item)

        # Рядки днів
        for d_idx, day in enumerate(days):
            row = d_idx + 1

            date_item = QTableWidgetItem(day["date_str"])
            date_item.setTextAlignment(Qt.AlignCenter)
            date_item.setFont(self._bold_font())
            date_item.setBackground(QColor("#e8f5e9"))
            date_item.setForeground(QColor("black"))
            self.table.setItem(row, 0, date_item)

            demand_item = QTableWidgetItem(f"{day['demand']:,.0f}")
            demand_item.setTextAlignment(Qt.AlignCenter)
            demand_item.setBackground(QColor("#e8f5e9"))
            demand_item.setForeground(QColor("black"))
            self.table.setItem(row, 1, demand_item)

            for f_idx in range(n_fields):
                val = schedule[d_idx][f_idx]
                if val < 0.01:
                    item = QTableWidgetItem("")
                    item.setBackground(QColor("#f5f5f5"))
                else:
                    item = QTableWidgetItem(f"{val:,.0f}")
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setForeground(QColor("black"))
                    field_gross = fields[f_idx]["gross"]
                    if val >= field_gross * 0.99:
                        item.setBackground(QColor("#a5d6a7"))
                    elif val < field_gross and val > 0.01:
                        item.setBackground(QColor("#fff9c4"))
                    else:
                        item.setBackground(QColor("#c8e6c9"))
                self.table.setItem(row, 2 + f_idx, item)

        self.table.resizeRowsToContents()
        self.table.resizeColumnsToContents()

        total_gross          = sum(f["gross"] for f in fields)
        total_harvested      = sum(
            schedule[d][f] for d in range(n_days) for f in range(n_fields)
        )
        total_factory_demand = sum(day["demand"] for day in days)
        fields_done          = sum(
            1 for f_idx in range(n_fields) if remaining[f_idx] <= 0.001
        )

        summary_parts = [
            f"Полів всього: <b>{n_fields}</b>",
            f"Зібрано повністю: <b>{fields_done}</b>",
            f"Загальний вал: <b>{total_gross:,.0f} т</b>",
            f"Потреба заводу: <b>{total_factory_demand:,.0f} т</b>",
            f"Заплановано до збирання: <b>{total_harvested:,.0f} т</b>",
        ]
        if total_gross > 0:
            pct = total_harvested / total_gross * 100
            summary_parts.append(f"Охоплення: <b>{pct:.1f}%</b>")

        self.summary_label.setText("   |   ".join(summary_parts))

        # Вмикаємо кнопки експорту
        self.btn_export_xlsx.setEnabled(True)
        self.btn_export_csv.setEnabled(True)

    def _fill_main_table_dates(self, fields, days, schedule):
        main_table = self.main_app.table
        n_days     = len(days)

        for f_idx, field in enumerate(fields):
            row_idx   = field["row_idx"]
            first_day = None
            last_day  = None

            for d_idx in range(n_days):
                val = schedule[d_idx][f_idx]
                if val >= 0.01:
                    if first_day is None:
                        first_day = days[d_idx]["date"]
                    last_day = days[d_idx]["date"]

            start_str = first_day.strftime("%d.%m.%Y") if first_day else ""
            main_table.setItem(row_idx, 7, QTableWidgetItem(start_str))

            end_str = last_day.strftime("%d.%m.%Y") if last_day else ""
            main_table.setItem(row_idx, 8, QTableWidgetItem(end_str))

            deviation_str = ""
            if first_day:
                ripening_item = main_table.item(row_idx, 6)
                if ripening_item and ripening_item.text().strip():
                    ripening = None
                    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                        try:
                            ripening = datetime.strptime(
                                ripening_item.text().strip(), fmt
                            ).date()
                            break
                        except Exception:
                            continue
                    if ripening:
                        delta = (first_day - ripening).days
                        sign  = "+" if delta > 0 else ""
                        deviation_str = f"{sign}{delta} дн."

            main_table.setItem(row_idx, 9, QTableWidgetItem(deviation_str))

        self.main_app.save_data()

    # ------------------------------------------------------------------
    # ЕКСПОРТ
    # ------------------------------------------------------------------
    def _export_xlsx(self):
        if not self._last_fields:
            QMessageBox.warning(self, "Експорт", "Спочатку побудуйте план.")
            return
        if not _HAS_OPENPYXL:
            QMessageBox.warning(
                self, "Експорт",
                "Бібліотека openpyxl не встановлена.\n"
                "Встановіть: pip install openpyxl"
            )
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Зберегти Excel", "детальний_план.xlsx",
            "Excel файли (*.xlsx)"
        )
        if not path:
            return

        fields    = self._last_fields
        days      = self._last_days
        schedule  = self._last_schedule
        remaining = self._last_remaining
        n_days    = len(days)
        n_fields  = len(fields)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Детальний план"

        # ── Стилі ──
        hdr_fill    = PatternFill("solid", fgColor="2E7D32")   # темно-зелений
        gross_fill  = PatternFill("solid", fgColor="C8E6C9")   # світло-зелений
        date_fill   = PatternFill("solid", fgColor="E8F5E9")
        done_fill   = PatternFill("solid", fgColor="A5D6A7")   # зібрано повністю
        part_fill   = PatternFill("solid", fgColor="FFF9C4")   # частково
        empty_fill  = PatternFill("solid", fgColor="F5F5F5")

        hdr_font    = XFont(bold=True, color="FFFFFF", size=11)
        bold_font   = XFont(bold=True, size=11)
        reg_font    = XFont(size=11)
        center      = Alignment(horizontal="center", vertical="center", wrap_text=True)
        thin        = Side(style="thin", color="D0D0D0")
        border      = Border(left=thin, right=thin, top=thin, bottom=thin)

        # ── Рядок 1: заголовки ──
        headers = ["Дата", "Потреба заводу (т)"] + [f["name"] for f in fields]
        for c_idx, h in enumerate(headers, start=1):
            cell             = ws.cell(row=1, column=c_idx, value=h)
            cell.fill        = hdr_fill
            cell.font        = hdr_font
            cell.alignment   = center
            cell.border      = border

        # ── Рядок 2: плановий вал ──
        ws.cell(row=2, column=1, value="Плановий вал (т)").font = bold_font
        ws.cell(row=2, column=1).fill      = gross_fill
        ws.cell(row=2, column=1).border    = border
        ws.cell(row=2, column=1).alignment = center

        ws.cell(row=2, column=2, value="—").fill   = gross_fill
        ws.cell(row=2, column=2).border  = border
        ws.cell(row=2, column=2).alignment = center

        for f_idx, field in enumerate(fields):
            cell           = ws.cell(row=2, column=3 + f_idx, value=round(field["gross"]))
            cell.fill      = gross_fill
            cell.font      = bold_font
            cell.alignment = center
            cell.border    = border

        # ── Рядки днів ──
        for d_idx, day in enumerate(days):
            row = d_idx + 3

            # Дата
            c = ws.cell(row=row, column=1, value=day["date_str"])
            c.fill = date_fill; c.font = bold_font
            c.alignment = center; c.border = border

            # Потреба
            c = ws.cell(row=row, column=2, value=round(day["demand"]))
            c.fill = date_fill; c.font = reg_font
            c.alignment = center; c.border = border

            # Поля
            for f_idx in range(n_fields):
                val = schedule[d_idx][f_idx]
                c   = ws.cell(row=row, column=3 + f_idx)
                c.border    = border
                c.alignment = center
                c.font      = reg_font
                if val < 0.01:
                    c.value = ""
                    c.fill  = empty_fill
                else:
                    c.value = round(val)
                    if val >= fields[f_idx]["gross"] * 0.99:
                        c.fill = done_fill
                    elif val < fields[f_idx]["gross"]:
                        c.fill = part_fill
                    else:
                        c.fill = gross_fill

        # ── Рядок підсумку ──
        sum_row      = n_days + 3
        total_gross  = sum(f["gross"] for f in fields)
        total_harv   = sum(
            schedule[d][f] for d in range(n_days) for f in range(n_fields)
        )
        fields_done  = sum(1 for fi in range(n_fields) if remaining[fi] <= 0.001)

        ws.cell(row=sum_row, column=1, value="ПІДСУМОК").font = bold_font
        ws.cell(row=sum_row, column=1).fill = PatternFill("solid", fgColor="37474F")
        ws.cell(row=sum_row, column=1).font = XFont(bold=True, color="FFFFFF", size=11)
        ws.cell(row=sum_row, column=1).alignment = center
        ws.cell(row=sum_row, column=1).border = border

        summary_vals = [
            f"Полів: {n_fields}  |  Зібрано: {fields_done}  |  "
            f"Вал: {total_gross:,.0f} т  |  "
            f"Заплановано: {total_harv:,.0f} т  |  "
            f"Охоплення: {total_harv/total_gross*100:.1f}%" if total_gross > 0 else ""
        ]
        c = ws.cell(row=sum_row, column=2, value=summary_vals[0])
        c.font = bold_font; c.alignment = center; c.border = border
        ws.merge_cells(
            start_row=sum_row, start_column=2,
            end_row=sum_row,   end_column=2 + n_fields
        )

        # ── Ширина колонок ──
        ws.column_dimensions[
            openpyxl.utils.get_column_letter(1)
        ].width = 14
        ws.column_dimensions[
            openpyxl.utils.get_column_letter(2)
        ].width = 18
        for f_idx in range(n_fields):
            col_letter = openpyxl.utils.get_column_letter(3 + f_idx)
            name_len   = max(10, len(fields[f_idx]["name"]) + 2)
            ws.column_dimensions[col_letter].width = min(name_len, 24)

        # ── Закріплення першого рядка і колонки ──
        ws.freeze_panes = "C3"

        # ── Другий аркуш: зведення по відділеннях ──
        ws2 = wb.create_sheet("Зведення по відділеннях")
        div_data = {}
        for f_idx, field in enumerate(fields):
            div = field["division"] or "—"
            if div not in div_data:
                div_data[div] = {"gross": 0.0, "harvested": 0.0, "fields": 0}
            div_data[div]["gross"]     += field["gross"]
            div_data[div]["harvested"] += sum(
                schedule[d][f_idx] for d in range(n_days)
            )
            div_data[div]["fields"]    += 1

        ws2_headers = ["Відділення", "Полів", "Загальний вал (т)",
                       "Заплановано (т)", "Охоплення (%)"]
        for c_idx, h in enumerate(ws2_headers, start=1):
            cell           = ws2.cell(row=1, column=c_idx, value=h)
            cell.fill      = hdr_fill
            cell.font      = hdr_font
            cell.alignment = center
            cell.border    = border

        for r_idx, (div, data) in enumerate(div_data.items(), start=2):
            pct = data["harvested"] / data["gross"] * 100 if data["gross"] > 0 else 0
            row_vals = [
                div,
                data["fields"],
                round(data["gross"]),
                round(data["harvested"]),
                round(pct, 1),
            ]
            for c_idx, v in enumerate(row_vals, start=1):
                cell           = ws2.cell(row=r_idx, column=c_idx, value=v)
                cell.alignment = center
                cell.border    = border
                cell.font      = reg_font

        for c_idx in range(1, 6):
            ws2.column_dimensions[
                openpyxl.utils.get_column_letter(c_idx)
            ].width = 22

        wb.save(path)
        QMessageBox.information(
            self, "Експорт завершено",
            f"✅ Excel файл збережено:\n{path}\n\n"
            f"Аркуші:\n"
            f"  • «Детальний план» — повна таблиця збирання\n"
            f"  • «Зведення по відділеннях» — підсумки"
        )

    def _export_csv(self):
        if not self._last_fields:
            QMessageBox.warning(self, "Експорт", "Спочатку побудуйте план.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Зберегти CSV", "детальний_план.csv",
            "CSV файли (*.csv)"
        )
        if not path:
            return

        fields   = self._last_fields
        days     = self._last_days
        schedule = self._last_schedule
        n_days   = len(days)
        n_fields = len(fields)

        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")

            # Заголовок
            writer.writerow(
                ["Дата", "Потреба заводу (т)"] + [fi["name"] for fi in fields]
            )

            # Плановий вал
            writer.writerow(
                ["Плановий вал (т)", "—"] + [str(round(fi["gross"])) for fi in fields]
            )

            # Дні
            for d_idx, day in enumerate(days):
                row = [day["date_str"], round(day["demand"])]
                for f_idx in range(n_fields):
                    val = schedule[d_idx][f_idx]
                    row.append(round(val) if val >= 0.01 else "")
                writer.writerow(row)

        QMessageBox.information(
            self, "Експорт завершено",
            f"✅ CSV файл збережено:\n{path}\n\n"
            "Відкрийте в Excel або Google Sheets.\n"
            "Роздільник: крапка з комою (;)"
        )

    # ------------------------------------------------------------------
    # Допоміжні
    # ------------------------------------------------------------------
    @staticmethod
    def _bold_font():
        f = QFont()
        f.setBold(True)
        return f
