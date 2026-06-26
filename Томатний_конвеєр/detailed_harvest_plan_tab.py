import os
from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
  QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
  QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
  QScrollArea, QSizePolicy, QDateEdit
)
from PySide6.QtGui import QColor, QFont


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
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(10, 10, 10, 10)

        # — Заголовок + кнопка оновлення —
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
        """
        Повертає список полів з основної таблиці.
        Кожне поле: {name, gross, division, area}
        Стовпці основної таблиці (0-based):
          0  — назва поля / номер
          3  — валовий збір (т)
          10 — відділення
        """
        main_table = self.main_app.table
        fields = []
        for r in range(main_table.rowCount()):
            name_item = main_table.item(r, 0)
            gross_item = main_table.item(r, 3)
            div_item = main_table.item(r, 10)

            name = name_item.text().strip() if name_item else f"Поле {r+1}"
            if not name:
                name = f"Поле {r+1}"

            try:
                gross = float(gross_item.text()) if gross_item else 0.0
            except ValueError:
                gross = 0.0

            division = div_item.text().strip() if div_item else ""

            if gross <= 0:
                continue  # пропускаємо порожні рядки

            fields.append({
                "name": name,
                "gross": gross,
                "division": division,
                "row_idx": r,
            })
        return fields

    def _get_division_start_dates(self):
      """
      Повертає {відділення: date} з QDateEdit у таблиці GA.
      """
      ga_tab = self.main_app.ga_tab
      div_table = ga_tab.div_table
      result = {}

      for r in range(div_table.rowCount()):
        n_item = div_table.item(r, 0)
        if not n_item:
          continue

        name = n_item.text().strip()
        if not name:
          continue

        widget = div_table.cellWidget(r, 4)  # <-- ВАЖЛИВО: QDateEdit тут

        if isinstance(widget, QDateEdit):
          result[name] = widget.date().toPython()
        else:
          result[name] = None

      return result

    def _get_plan_days(self):
        """
        Повертає список днів плану заводу з вкладки GA-оптимізатора.
        Кожен день: {date_str, date, demand, div_caps}
        """
        ga_tab = self.main_app.ga_tab
        plan_table = ga_tab.plan_table
        div_caps = self._calc_division_capacities(ga_tab)

        days = []
        for r in range(plan_table.rowCount()):
            date_item = plan_table.item(r, 0)
            demand_item = plan_table.item(r, 1)
            date_str = date_item.text() if date_item else f"День {r+1}"

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
                "date": day_date,
                "demand": demand,
                "div_caps": div_caps,
            })
        return days

    def _calc_division_capacities(self, ga_tab):
        """
        Будуємо словник {назва_відділення: добова_потужність_т}.
        Пріоритет: GA-результат → власний розрахунок.
        """
        div_caps = {}

        # 1) Якщо GA вже відпрацював — беремо середню добову потужність
        if ga_tab.result_data:
            # result_data[day]["divisions"][div_name]["capacity"]
            cap_sum = {}
            cap_cnt = {}
            for day_row in ga_tab.result_data:
                for div_name, info in day_row["divisions"].items():
                    cap_sum[div_name] = cap_sum.get(div_name, 0) + info.get("capacity", 0)
                    cap_cnt[div_name] = cap_cnt.get(div_name, 0) + 1
            for div_name in cap_sum:
                div_caps[div_name] = cap_sum[div_name] / cap_cnt[div_name]
            return div_caps

        # 2) Fallback — рахуємо з таблиці відділень оптимізатора
        div_table = ga_tab.div_table
        harv_a_cap = ga_tab.harv_a_cap.value()
        harv_b_cap = ga_tab.harv_b_cap.value()
        hours = ga_tab.hours_spin.value()

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

      n_days = len(days)
      n_fields = len(fields)

      remaining = [f["gross"] for f in fields]
      schedule = [[0.0] * n_fields for _ in range(n_days)]

      div_queues = {}
      for f_idx, field in enumerate(fields):
        div = field.get("division", "") or "__no_div__"
        div_queues.setdefault(div, []).append(f_idx)

      div_queue_pos = {div: 0 for div in div_queues}

      # --- Початкова кількість комбайнів кожного відділення ---
      ga_tab = self.main_app.ga_tab
      harv_a_cap = ga_tab.harv_a_cap.value()
      harv_b_cap = ga_tab.harv_b_cap.value()
      hours = ga_tab.hours_spin.value()

      # div_harvesters[div] = {"a": int, "b": int}
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
        """Добова потужність по кількості комбайнів."""
        return (harvesters["a"] * harv_a_cap + harvesters["b"] * harv_b_cap) * hours

      # Поточна кількість комбайнів (змінюється при перерозподілі)
      current_harvesters = {div: dict(h) for div, h in div_harvesters.items()}

      # Множник для дробових комбайнів (зберігаємо як float окремо)
      # Використовуємо float-потужність замість цілих комбайнів для точності
      current_caps = {div: calc_cap(h) for div, h in current_harvesters.items()}

      for d_idx, day in enumerate(days):
        day_date = day.get("date")
        demand = day["demand"]

        # 1) Визначаємо які відділення активні сьогодні
        eligible = {}  # відділення що стартували і мають потужність
        active = {}  # з eligible — ті що мають залишок врожаю

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
        total_active_cap = sum(active.values())

        # 2) Розраховуємо квоти з урахуванням завершених відділень
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

          # 3) Ітеративний перерозподіл якщо залишок < квоти
          for _ in range(len(active)):
            leftover = 0.0
            capped = set()
            for div, quota in quotas.items():
              queue = div_queues[div]
              pos = div_queue_pos[div]
              div_rem = sum(remaining[queue[i]] for i in range(pos, len(queue)))
              if div_rem < quota - 0.001:
                leftover += quota - div_rem
                quotas[div] = div_rem
                capped.add(div)
            if leftover < 0.001:
              break
            free = {div: active[div] for div in active if div not in capped}
            free_cap = sum(free.values())
            if free_cap < 0.001:
              break
            for div, cap in free.items():
              # ВАЖЛИВО: обмежуємо по поточній потужності (active[div]),
              # але після циклу нормуємо всю суму по demand
              quotas[div] = quotas[div] + leftover * (cap / free_cap)

          # +++ НОРМУВАННЯ: сума квот не повинна перевищувати demand +++
          total_quota = sum(quotas.values())
          if total_quota > demand + 0.001:
            scale = demand / total_quota
            quotas = {div: q * scale for div, q in quotas.items()}

        # 4) Збираємо
        for div, quota in quotas.items():
          pos = div_queue_pos[div]
          capacity_left = quota
          queue = div_queues[div]
          while capacity_left > 0.001 and pos < len(queue):
            f_idx = queue[pos]
            if remaining[f_idx] <= 0.001:
              pos += 1
              continue
            take = min(remaining[f_idx], capacity_left)
            schedule[d_idx][f_idx] += take
            remaining[f_idx] -= take
            capacity_left -= take
            if remaining[f_idx] <= 0.001:
              pos += 1
          div_queue_pos[div] = pos

        # 5) Після збору — перевіряємо чи якесь відділення щойно завершило вал.
        #    Якщо так — розподіляємо його комбайни між тими що ще мають залишок,
        #    пропорційно до їхнього залишку врожаю. З НАСТУПНОГО дня.
        just_finished = []
        still_active = []
        for div in div_queues:
          cap = current_caps.get(div, 0)
          if cap <= 0:
            continue
          start_date = div_start_dates.get(div)
          if start_date and day_date and day_date < start_date:
            continue
          queue = div_queues[div]
          pos = div_queue_pos[div]
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
            pos = div_queue_pos[div]
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

      # === ДІАГНОСТИКА ===
      import os
      log_lines = ["=== ДІАГНОСТИКА ЗБИРАННЯ ==="]
      total_scheduled = sum(schedule[d][f] for d in range(n_days) for f in range(n_fields))
      total_gross = sum(f["gross"] for f in fields)
      log_lines.append(f"Загальний вал: {total_gross:,.1f} т")
      log_lines.append(f"Заплановано:   {total_scheduled:,.1f} т")
      log_lines.append(f"Різниця:       {total_gross - total_scheduled:,.1f} т")
      log_lines.append(f"Кількість днів: {n_days}")
      log_lines.append(f"Кількість полів: {n_fields}")

      log_lines.append("\nЗалишки по відділеннях:")
      for dname, dqueue in div_queues.items():
        rem = sum(remaining[dqueue[i]] for i in range(len(dqueue)))
        log_lines.append(f"  {dname}: залишок={rem:,.1f} т | поточна потужність={current_caps.get(dname, 0):,.1f}")

      log_lines.append("\nДетально по відділеннях — скільки зібрано щодня:")
      for dname, dqueue in div_queues.items():
        log_lines.append(f"\n  [{dname}]")
        for di, d in enumerate(days):
          day_total = sum(schedule[di][dqueue[i]] for i in range(len(dqueue)))
          if day_total > 0:
            log_lines.append(f"    День {di + 1} ({d['date_str']}): {day_total:,.1f} т")

      log_lines.append("\nЗалишки по полях (тільки > 0):")
      for fi, field in enumerate(fields):
        if remaining[fi] > 0.001:
          log_lines.append(
            f"  {field['name']} ({field['division']}): залишок={remaining[fi]:,.1f} / вал={field['gross']:,.1f}")

      log_lines.append("\nПочаткові потужності відділень:")
      for dname in div_queues:
        log_lines.append(f"  {dname}: {calc_cap(div_harvesters.get(dname, {'a': 0, 'b': 0})):,.1f} т/день")

      log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvest_debug.log")
      with open(log_path, "w", encoding="utf-8") as lf:
        lf.write("\n".join(log_lines))
      print(f"[ДІАГНОСТИКА] Лог збережено: {log_path}")

      return schedule, remaining

    def _get_field_day_capacity(self, field, div_caps):
        """Добова потужність для поля, виходячи з його відділення."""
        division = field.get("division", "")
        if division and division in div_caps:
            return div_caps[division]
        # Якщо відділення не вказане або не знайдене — шукаємо будь-яку
        if div_caps:
            # Ділимо сумарну потужність порівну між невідомими полями
            return sum(div_caps.values()) / len(div_caps)
        return 0.0

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

      # ── Перевірка потужностей ──
      ga_tab = self.main_app.ga_tab
      divisions = []
      for r in range(ga_tab.div_table.rowCount()):
        name_item = ga_tab.div_table.item(r, 0)
        gross_item = ga_tab.div_table.item(r, 1)
        harv_a_item = ga_tab.div_table.item(r, 2)
        harv_b_item = ga_tab.div_table.item(r, 3)
        if not name_item or not gross_item:
          continue
        try:
          divisions.append({
            "name": name_item.text().strip(),
            "gross": float(gross_item.text() or 0),
            "harv_a": int(harv_a_item.text() or 0) if harv_a_item else 0,
            "harv_b": int(harv_b_item.text() or 0) if harv_b_item else 0,
          })
        except ValueError:
          continue

      factory_plan = [day["demand"] for day in days]

      if divisions and factory_plan:
        if not ga_tab._check_division_capacity(divisions, factory_plan):
          return
      # ──────────────────────────────

      div_start_dates = self._get_division_start_dates()
      schedule, remaining = self._compute_schedule(fields, days, div_start_dates)
      self._render_table(fields, days, schedule, remaining)
      self._fill_main_table_dates(fields, days, schedule)

    def _render_table(self, fields, days, schedule, remaining):
        n_days = len(days)
        n_fields = len(fields)

        # Колонки: «Дата» + «Потреба (т)» + поля
        col_labels = ["Дата", "Потреба\nзаводу (т)"] + [f["name"] for f in fields]
        n_cols = len(col_labels)

        # Рядки: «Плановий вал» + дні
        self.table.setColumnCount(n_cols)
        self.table.setRowCount(1 + n_days)
        self.table.setHorizontalHeaderLabels(col_labels)

        # --- Рядок 0: Плановий вал ---
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

        # --- Рядки днів ---
        for d_idx, day in enumerate(days):
            row = d_idx + 1

            # Дата
            date_item = QTableWidgetItem(day["date_str"])
            date_item.setTextAlignment(Qt.AlignCenter)
            date_item.setFont(self._bold_font())
            date_item.setBackground(QColor("#e8f5e9"))
            date_item.setForeground(QColor("black"))
            self.table.setItem(row, 0, date_item)

            # Потреба
            demand_item = QTableWidgetItem(f"{day['demand']:,.0f}")
            demand_item.setTextAlignment(Qt.AlignCenter)
            demand_item.setBackground(QColor("#e8f5e9"))
            demand_item.setForeground(QColor("black"))
            self.table.setItem(row, 1, demand_item)

            # Поля
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
                    is_partial = val < field_gross and val > 0.01

                    if val >= field_gross * 0.99:
                        item.setBackground(QColor("#a5d6a7"))
                    elif is_partial:
                        item.setBackground(QColor("#fff9c4"))
                    else:
                        item.setBackground(QColor("#c8e6c9"))

                self.table.setItem(row, 2 + f_idx, item)

        # Авторозмір
        self.table.resizeRowsToContents()
        self.table.resizeColumnsToContents()

        # --- Підсумок ---
        total_gross = sum(f["gross"] for f in fields)
        total_harvested = sum(
            schedule[d][f] for d in range(n_days) for f in range(n_fields)
        )
        total_factory_demand = sum(day["demand"] for day in days)
        fields_done = sum(1 for f_idx in range(n_fields) if remaining[f_idx] <= 0.001)

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

    def _fill_main_table_dates(self, fields, days, schedule):
      """
      Заповнює в основній таблиці колонки:
        7 — Початок збирання (перший день коли щось зібрано з поля)
        8 — Закінчення збирання (останній день)
        9 — Відхилення (Дозрівання прогноз - Початок збирання)
      """
      main_table = self.main_app.table
      n_days = len(days)
      n_fields = len(fields)

      for f_idx, field in enumerate(fields):
        row_idx = field["row_idx"]

        # Знаходимо перший і останній день збору
        first_day = None
        last_day = None

        for d_idx in range(n_days):
          val = schedule[d_idx][f_idx]
          if val >= 0.01:
            if first_day is None:
              first_day = days[d_idx]["date"]
            last_day = days[d_idx]["date"]

        # Початок збирання
        start_str = first_day.strftime("%d.%m.%Y") if first_day else ""
        main_table.setItem(row_idx, 7, QTableWidgetItem(start_str))

        # Закінчення збирання
        end_str = last_day.strftime("%d.%m.%Y") if last_day else ""
        main_table.setItem(row_idx, 8, QTableWidgetItem(end_str))

        # Відхилення = Дозрівання прогноз - Початок збирання
        deviation_str = ""
        if first_day:
          ripening_item = main_table.item(row_idx, 6)
          if ripening_item and ripening_item.text().strip():
            ripening = None
            for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
              try:
                ripening = datetime.strptime(ripening_item.text().strip(), fmt).date()
                break
              except:
                continue
            if ripening:
              delta = (first_day - ripening).days
              sign = "+" if delta > 0 else ""
              deviation_str = f"{sign}{delta} дн."

        main_table.setItem(row_idx, 9, QTableWidgetItem(deviation_str))

      self.main_app.save_data()
    # ------------------------------------------------------------------
    # Допоміжні
    # ------------------------------------------------------------------
    @staticmethod
    def _bold_font():
        f = QFont()
        f.setBold(True)
        return f
