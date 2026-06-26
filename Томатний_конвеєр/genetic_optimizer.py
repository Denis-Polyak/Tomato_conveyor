import json
import math
import random
import copy
from datetime import datetime, timedelta
from openpyxl import load_workbook

from PySide6.QtCore import Qt, QThread, Signal, QDate
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QSpinBox, QDoubleSpinBox,
    QGroupBox, QScrollArea, QProgressBar, QHeaderView,
    QSplitter, QTabWidget, QLineEdit, QMessageBox, QDateEdit, QFileDialog,
    QDialog, QTextEdit
)
from PySide6.QtGui import QColor, QFont

GA_FILE = "ga_data.json"


# =========================
# ГЕНЕТИЧНИЙ АЛГОРИТМ
# =========================
class GeneticScheduler:
    def __init__(self, divisions, harv_a_cap, harv_b_cap, factory_plan,
                 hours_per_day=16, max_load=0.8):
        self.divisions = divisions
        self.harv_a_cap = harv_a_cap
        self.harv_b_cap = harv_b_cap
        self.factory_plan = factory_plan
        self.hours = hours_per_day
        self.max_load = max_load
        self.n_days = len(factory_plan)
        self.n_div = len(divisions)
        self.total_gross = sum(d["gross"] for d in divisions)
        self.div_ratio = [
            d["gross"] / self.total_gross if self.total_gross > 0 else 1 / self.n_div
            for d in divisions
        ]

    def daily_capacity(self, div):
        return (div["harv_a"] * self.harv_a_cap + div["harv_b"] * self.harv_b_cap) * self.hours

    def _random_chromosome(self):
        chrom = []
        for day in range(self.n_days):
            assignment = {
                d["name"]: {"harv_a": d["harv_a"], "harv_b": d["harv_b"]}
                for d in self.divisions
            }
            chrom.append(assignment)
        return chrom

    def fitness(self, chrom):
        total_penalty = 0.0
        remaining = {d["name"]: d["gross"] for d in self.divisions}
        for day_idx, assignment in enumerate(chrom):
            demand = self.factory_plan[day_idx]
            total_supply = 0.0
            for div in self.divisions:
                name = div["name"]
                cap = self.daily_capacity(div)
                ratio = self.div_ratio[self.divisions.index(div)]
                supply = min(cap, demand * ratio, remaining[name])
                supply = max(0, supply)
                remaining[name] -= supply
                total_supply += supply
                if cap > 0:
                    load = supply / cap
                    if load > 1.0:
                        total_penalty += (load - 1.0) * 10000
                    elif load > self.max_load:
                        total_penalty += (load - self.max_load) * 1000
            total_penalty += abs(total_supply - demand) ** 1.5
        return total_penalty

    def run(self, pop_size=60, generations=200, callback=None):
        """Еволюційний цикл. Повертає (best_chromosome, best_score)."""
        # Оскільки хромосоми фіксовані (комбайни не перерозподіляємо),
        # всі хромосоми однакові — просто повертаємо одну і рахуємо фітнес.
        best = self._random_chromosome()
        best_score = self.fitness(best)

        population = [self._random_chromosome() for _ in range(pop_size)]
        scores = [self.fitness(c) for c in population]

        best_idx = scores.index(min(scores))
        best = copy.deepcopy(population[best_idx])
        best_score = scores[best_idx]

        for gen in range(1, generations + 1):
            # Турнірна селекція + мутація (для майбутніх розширень)
            new_population = [copy.deepcopy(best)]  # елітизм

            while len(new_population) < pop_size:
                # Турнір з 3 особин
                candidates = random.sample(range(pop_size), min(3, pop_size))
                parent = copy.deepcopy(population[min(candidates, key=lambda i: scores[i])])
                new_population.append(parent)

            population = new_population
            scores = [self.fitness(c) for c in population]

            gen_best_idx = scores.index(min(scores))
            if scores[gen_best_idx] < best_score:
                best = copy.deepcopy(population[gen_best_idx])
                best_score = scores[gen_best_idx]

            if callback:
                callback(gen, generations, best_score)

        return best, best_score

    def decode_result(self, chrom):
        rows = []
        remaining = {d["name"]: d["gross"] for d in self.divisions}
        for day_idx, assignment in enumerate(chrom):
            demand = self.factory_plan[day_idx]
            row = {"day": day_idx + 1, "demand": demand, "divisions": {}}
            total_supply = 0.0
            for div in self.divisions:
                name = div["name"]
                cap = self.daily_capacity(div)
                ratio = self.div_ratio[self.divisions.index(div)]
                supply = min(cap, demand * ratio, remaining[name])
                supply = max(0, supply)
                remaining[name] -= supply
                load_pct = (supply / cap * 100) if cap > 0 else 0
                row["divisions"][name] = {
                    "harv_a": div["harv_a"],
                    "harv_b": div["harv_b"],
                    "capacity": round(cap, 1),
                    "supply": round(supply, 1),
                    "load_pct": round(load_pct, 1),
                    "remaining": round(remaining[name], 1)
                }
                total_supply += supply
            row["total_supply"] = round(total_supply, 1)
            row["deviation"] = round(total_supply - demand, 1)
            rows.append(row)
        return rows


# =========================
# ПОТІК ДЛЯ GA
# =========================
class GAThread(QThread):
    progress = Signal(int, int, float)
    finished = Signal(list, float)

    def __init__(self, scheduler, pop_size, generations):
        super().__init__()
        self.scheduler = scheduler
        self.pop_size = pop_size
        self.generations = generations

    def run(self):
        def cb(gen, total, score):
            self.progress.emit(gen, total, score)

        best, score = self.scheduler.run(
            pop_size=self.pop_size,
            generations=self.generations,
            callback=cb
        )
        result = self.scheduler.decode_result(best)
        self.finished.emit(result, score)


# =========================
# ВКЛАДКА ОПТИМІЗАТОРА
# =========================
class GeneticOptimizerTab(QWidget):
    def __init__(self, main_app):
        super().__init__()
        self.main_app = main_app
        self.result_data = []
        self.ga_thread = None
        self._build_ui()
        self._load()

    # -------------------------------------------------------
    def _auto_calculate_harvesters(self):
        if self.plan_table.rowCount() == 0:
            QMessageBox.warning(self, "Помилка", "Немає плану заводу")
            return
        max_demand = 0.0
        for r in range(self.plan_table.rowCount()):
            item = self.plan_table.item(r, 1)
            if item:
                try:
                    val = float(item.text())
                    if val > max_demand:
                        max_demand = val
                except:
                    pass
        if max_demand == 0:
            QMessageBox.warning(self, "Помилка", "Потреба заводу = 0")
            return

        ratio_a = self.ratio_a_spin.value() / 100.0
        ratio_b = self.ratio_b_spin.value() / 100.0
        need_a = max_demand * ratio_a
        need_b = max_demand * ratio_b
        cap_a_per_hour = self.harv_a_cap.value()
        cap_b_per_hour = self.harv_b_cap.value()
        hours = self.hours_spin.value()

        pool_a_raw = need_a / (cap_a_per_hour * hours) if cap_a_per_hour * hours > 0 else 0
        pool_b_raw = need_b / (cap_b_per_hour * hours) if cap_b_per_hour * hours > 0 else 0
        pool_a = max(1, round(pool_a_raw))
        pool_b = max(1, round(pool_b_raw))

        divisions_data = []
        for r in range(self.div_table.rowCount()):
            name_item = self.div_table.item(r, 0)
            gross_item = self.div_table.item(r, 1)
            if not name_item or not gross_item:
                continue
            try:
                gross = float(gross_item.text())
            except:
                gross = 0.0
            divisions_data.append({"row": r, "name": name_item.text().strip(), "gross": gross})

        if not divisions_data:
            QMessageBox.warning(self, "Помилка", "Додайте хоча б одне відділення з валом!")
            return
        total_gross = sum(d["gross"] for d in divisions_data)
        if total_gross == 0:
            QMessageBox.warning(self, "Помилка", "Загальний вал = 0.")
            return

        for d in divisions_data:
            pct = d["gross"] / total_gross
            d["harv_a"] = int(pool_a * pct)
            d["harv_b"] = int(pool_b * pct)
            d["frac_a"] = (pool_a * pct) % 1
            d["frac_b"] = (pool_b * pct) % 1

        for d in divisions_data:
            if d["gross"] > 0 and d["harv_a"] == 0 and d["harv_b"] == 0:
                d["harv_b"] = 1

        for d in divisions_data:
            lost_per_hour = d["frac_a"] * cap_a_per_hour + d["frac_b"] * cap_b_per_hour
            if lost_per_hour <= 0:
                continue
            if lost_per_hour >= cap_a_per_hour:
                d["harv_a"] += 1
            elif lost_per_hour >= cap_b_per_hour / 2:
                d["harv_b"] += 1
            else:
                current_cap = (d["harv_a"] * cap_a_per_hour + d["harv_b"] * cap_b_per_hour) * hours
                if current_cap > 0:
                    extra_load_pct = (lost_per_hour * hours / current_cap) * 100
                    new_load = min(100, self.max_load_spin.value() + round(extra_load_pct))
                    self.max_load_spin.setValue(new_load)

        biggest = max(divisions_data, key=lambda x: x["gross"])
        diff_a = pool_a - sum(d["harv_a"] for d in divisions_data)
        diff_b = pool_b - sum(d["harv_b"] for d in divisions_data)
        if diff_a > 0:
            biggest["harv_a"] += diff_a
        if diff_b > 0:
            biggest["harv_b"] += diff_b

        for d in divisions_data:
            self.div_table.setItem(d["row"], 2, QTableWidgetItem(str(d["harv_a"])))
            self.div_table.setItem(d["row"], 3, QTableWidgetItem(str(d["harv_b"])))

    def _import_plan_excel(self):
        file_name, _ = QFileDialog.getOpenFileName(
            self, "Виберіть файл плану", "", "Excel files (*.xlsx *.xlsm)"
        )
        if not file_name:
            return
        try:
            wb = load_workbook(file_name, data_only=True)
            ws = wb.active
            self.plan_table.setRowCount(0)
            first_date = None
            for row in ws.iter_rows(min_row=2, values_only=True):
                if row[0] is None:
                    continue
                date_value = row[0]
                demand_value = row[1]
                if demand_value is None:
                    demand_value = 0
                else:
                    try:
                        demand_value = int(float(demand_value))
                    except:
                        demand_value = 0
                r = self.plan_table.rowCount()
                self.plan_table.insertRow(r)
                if isinstance(date_value, datetime):
                    if first_date is None:
                        first_date = date_value
                    date_text = date_value.strftime("%d.%m.%Y")
                else:
                    date_text = str(date_value)
                date_item = QTableWidgetItem(date_text)
                date_item.setFlags(Qt.ItemIsEnabled)
                self.plan_table.setItem(r, 0, date_item)
                self.plan_table.setItem(r, 1, QTableWidgetItem(str(demand_value)))
            if first_date:
                self.start_date_edit.setDate(QDate(first_date.year, first_date.month, first_date.day))
            QMessageBox.information(self, "Імпорт", f"Імпортовано {self.plan_table.rowCount()} днів.")
        except Exception as e:
            QMessageBox.critical(self, "Помилка", f"Не вдалося імпортувати файл:\n{e}")

    def _update_plan_dates(self):
        start_date = self.start_date_edit.date().toPython()
        for row in range(self.plan_table.rowCount()):
            current_date = start_date + timedelta(days=row)
            item = QTableWidgetItem(current_date.strftime("%d.%m.%Y"))
            item.setFlags(Qt.ItemIsEnabled)
            self.plan_table.setItem(row, 0, item)

    # -------------------------------------------------------
    def _build_ui(self):
        outer = QVBoxLayout()
        outer.setSpacing(8)
        splitter = QSplitter(Qt.Horizontal)

        # ===== ЛІВА ПАНЕЛЬ =====
        left = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setSpacing(8)

        grp_harv = QGroupBox("Комбайни")
        harv_layout = QVBoxLayout()
        harv_layout.addWidget(QLabel("Тип А (велика продуктивність)"))
        row_a = QHBoxLayout()
        row_a.addWidget(QLabel("т/год:"))
        self.harv_a_cap = QDoubleSpinBox()
        self.harv_a_cap.setRange(1, 500)
        self.harv_a_cap.setValue(33)
        row_a.addWidget(self.harv_a_cap)
        harv_layout.addLayout(row_a)

        harv_layout.addWidget(QLabel("Тип Б (менша продуктивність)"))
        row_b = QHBoxLayout()
        row_b.addWidget(QLabel("т/год:"))
        self.harv_b_cap = QDoubleSpinBox()
        self.harv_b_cap.setRange(1, 500)
        self.harv_b_cap.setValue(22)
        row_b.addWidget(self.harv_b_cap)
        harv_layout.addLayout(row_b)

        harv_layout.addWidget(QLabel("Співвідношення А/Б (%):"))
        ratio_row = QHBoxLayout()
        ratio_row.addWidget(QLabel("А:"))
        self.ratio_a_spin = QSpinBox()
        self.ratio_a_spin.setRange(0, 100)
        self.ratio_a_spin.setValue(60)
        self.ratio_a_spin.setSuffix("%")
        self.ratio_a_spin.valueChanged.connect(lambda v: self.ratio_b_spin.setValue(100 - v))
        ratio_row.addWidget(self.ratio_a_spin)
        ratio_row.addWidget(QLabel("Б:"))
        self.ratio_b_spin = QSpinBox()
        self.ratio_b_spin.setRange(0, 100)
        self.ratio_b_spin.setValue(40)
        self.ratio_b_spin.setSuffix("%")
        self.ratio_b_spin.valueChanged.connect(lambda v: self.ratio_a_spin.setValue(100 - v))
        ratio_row.addWidget(self.ratio_b_spin)
        harv_layout.addLayout(ratio_row)

        harv_layout.addWidget(QLabel("Годин роботи на день:"))
        self.hours_spin = QSpinBox()
        self.hours_spin.setRange(1, 24)
        self.hours_spin.setValue(16)
        harv_layout.addWidget(self.hours_spin)

        harv_layout.addWidget(QLabel("Макс. навантаження (%):"))
        self.max_load_spin = QSpinBox()
        self.max_load_spin.setRange(10, 100)
        self.max_load_spin.setValue(80)
        harv_layout.addWidget(self.max_load_spin)

        btn_auto_harv = QPushButton("⚙ Авто-розрахунок комбайнів")
        btn_auto_harv.setFixedHeight(32)
        btn_auto_harv.clicked.connect(self._auto_calculate_harvesters)
        harv_layout.addWidget(btn_auto_harv)
        grp_harv.setLayout(harv_layout)
        left_layout.addWidget(grp_harv)

        grp_div = QGroupBox("Відділення")
        div_layout = QVBoxLayout()
        div_btn_row = QHBoxLayout()
        btn_add_div = QPushButton("+ Додати")
        btn_add_div.clicked.connect(self._add_division_row)
        btn_rem_div = QPushButton("− Видалити")
        btn_rem_div.clicked.connect(self._remove_division_row)
        div_btn_row.addWidget(btn_add_div)
        div_btn_row.addWidget(btn_rem_div)
        div_layout.addLayout(div_btn_row)

        self.div_table = QTableWidget()
        self.div_table.setColumnCount(5)
        self.div_table.setHorizontalHeaderLabels(
            ["Назва відділення", "Валовий збір (т)", "Комбайнів А (шт)", "Комбайнів Б (шт)", "Дата старту"])
        self.div_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.div_table.setFixedHeight(160)
        div_layout.addWidget(self.div_table)

        btn_sync = QPushButton("Синхронізувати з основної таблиці")
        btn_sync.clicked.connect(self._sync_divisions)
        div_layout.addWidget(btn_sync)
        grp_div.setLayout(div_layout)
        left_layout.addWidget(grp_div)

        grp_ga = QGroupBox("Параметри алгоритму")
        ga_layout = QVBoxLayout()
        ga_layout.addWidget(QLabel("Розмір популяції:"))
        self.pop_spin = QSpinBox()
        self.pop_spin.setRange(10, 500)
        self.pop_spin.setValue(60)
        ga_layout.addWidget(self.pop_spin)
        ga_layout.addWidget(QLabel("Кількість поколінь:"))
        self.gen_spin = QSpinBox()
        self.gen_spin.setRange(10, 2000)
        self.gen_spin.setValue(200)
        ga_layout.addWidget(self.gen_spin)
        grp_ga.setLayout(ga_layout)
        left_layout.addWidget(grp_ga)

        left_layout.addStretch()
        left.setLayout(left_layout)

        # ===== ПРАВА ПАНЕЛЬ =====
        right = QWidget()
        right_layout = QVBoxLayout()
        right_layout.setSpacing(8)

        grp_plan = QGroupBox("План переробки заводу (т/день)")
        plan_layout = QVBoxLayout()
        plan_layout.addWidget(QLabel("Дата початку кампанії:"))
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setCalendarPopup(True)
        self.start_date_edit.setDisplayFormat("dd.MM.yyyy")
        self.start_date_edit.setDate(QDate.currentDate())
        self.start_date_edit.dateChanged.connect(self._update_plan_dates)
        plan_layout.addWidget(self.start_date_edit)

        plan_btn_row = QHBoxLayout()
        btn_add_day = QPushButton("+ День")
        btn_add_day.clicked.connect(self._add_plan_day)
        btn_rem_day = QPushButton("− День")
        btn_rem_day.clicked.connect(self._remove_plan_day)
        btn_import_plan = QPushButton("Імпорт Excel")
        btn_import_plan.clicked.connect(self._import_plan_excel)
        plan_btn_row.addWidget(btn_add_day)
        plan_btn_row.addWidget(btn_rem_day)
        plan_btn_row.addWidget(btn_import_plan)
        plan_layout.addLayout(plan_btn_row)

        self.plan_table = QTableWidget()
        self.plan_table.setColumnCount(2)
        self.plan_table.setHorizontalHeaderLabels(["Дата", "Потреба заводу (т)"])
        self.plan_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.plan_table.setFixedHeight(200)
        plan_layout.addWidget(self.plan_table)
        grp_plan.setLayout(plan_layout)
        right_layout.addWidget(grp_plan)

        self.btn_run = QPushButton("▶  Запустити генетичний алгоритм")
        self.btn_run.setFixedHeight(40)
        self.btn_run.clicked.connect(self._run_ga)
        right_layout.addWidget(self.btn_run)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        right_layout.addWidget(self.progress_bar)

        self.score_label = QLabel("")
        right_layout.addWidget(self.score_label)

        # --- Результат: план збирання ---
        grp_result = QGroupBox("Результат — план збирання")
        result_layout = QVBoxLayout()
        self.result_table = QTableWidget()
        self.result_table.setEditTriggers(QTableWidget.NoEditTriggers)
        result_layout.addWidget(self.result_table)
        grp_result.setLayout(result_layout)
        right_layout.addWidget(grp_result)

        # --- Результат: навантаження на комбайни ---
        grp_load = QGroupBox("Навантаження на комбайни по відділеннях (%)")
        load_layout = QVBoxLayout()

        load_hint = QLabel(
            "Показує % використання добової потужності кожного відділення щодня. "
            "🟢 ≤80% — норма,  🟡 81–100% — підвищене,  🔴 >100% — критичне."
        )
        load_hint.setWordWrap(True)
        load_hint.setStyleSheet("color: #555; font-size: 11px;")
        load_layout.addWidget(load_hint)

        self.load_table = QTableWidget()
        self.load_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.load_table.setAlternatingRowColors(False)
        self.load_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.load_table.verticalHeader().setVisible(False)
        self.load_table.setStyleSheet("""
            QTableWidget { gridline-color: #c0c0c0; font-size: 12px; }
            QHeaderView::section {
                background-color: #1565c0;
                color: white;
                font-weight: bold;
                padding: 4px;
                border: 1px solid #0d47a1;
            }
        """)
        load_layout.addWidget(self.load_table)

        self.load_summary_label = QLabel("")
        self.load_summary_label.setStyleSheet("font-size: 11px; color: #333;")
        load_layout.addWidget(self.load_summary_label)

        grp_load.setLayout(load_layout)
        right_layout.addWidget(grp_load)

        right.setLayout(right_layout)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([340, 860])
        outer.addWidget(splitter)
        self.setLayout(outer)

        self._add_division_row("Шевченкове", 0, 3, 2)
        self._add_division_row("Центральне", 0, 2, 2)
        self._add_division_row("Лимани", 0, 2, 1)
        for i in range(5):
            self._add_plan_day(demand=3000)

    # -------------------------------------------------------
    # ВІДДІЛЕННЯ
    # -------------------------------------------------------
    def _add_division_row(self, name="", gross=0, harv_a=0, harv_b=0, start_date=None):
        r = self.div_table.rowCount()
        self.div_table.insertRow(r)
        self.div_table.setItem(r, 0, QTableWidgetItem(str(name)))
        self.div_table.setItem(r, 1, QTableWidgetItem(str(gross)))
        self.div_table.setItem(r, 2, QTableWidgetItem(str(harv_a)))
        self.div_table.setItem(r, 3, QTableWidgetItem(str(harv_b)))
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        date_edit.setDisplayFormat("dd.MM.yyyy")
        if start_date:
            if isinstance(start_date, str):
                try:
                    dt = datetime.strptime(start_date, "%d.%m.%Y")
                    date_edit.setDate(QDate(dt.year, dt.month, dt.day))
                except:
                    date_edit.setDate(QDate.currentDate())
            else:
                date_edit.setDate(QDate.currentDate())
        else:
            date_edit.setDate(QDate.currentDate())
        self.div_table.setCellWidget(r, 4, date_edit)

    def _remove_division_row(self):
        row = self.div_table.currentRow()
        if row >= 0:
            self.div_table.removeRow(row)

    def _sync_divisions(self):
        main_table = self.main_app.table
        div_gross = {}
        for r in range(main_table.rowCount()):
            division_item = main_table.item(r, 10)
            gross_item = main_table.item(r, 3)
            if not division_item or not gross_item:
                continue
            division = division_item.text().strip()
            if not division:
                continue
            try:
                gross = float(gross_item.text())
            except:
                continue
            div_gross[division] = div_gross.get(division, 0) + gross
        if not div_gross:
            QMessageBox.information(self, "Синхронізація",
                                    "Не знайдено жодного поля з призначеним відділенням.")
            return
        self.div_table.setRowCount(0)
        for division, gross in div_gross.items():
            self._add_division_row(division, round(gross, 2), 0, 0)
        QMessageBox.information(self, "Синхронізація",
                                f"Знайдено {len(div_gross)} відділень:\n" +
                                "\n".join(f"  {k}: {round(v, 2)} т" for k, v in div_gross.items()))

    # -------------------------------------------------------
    # ПЛАН ЗАВОДУ
    # -------------------------------------------------------
    def _add_plan_day(self, demand=3000):
        r = self.plan_table.rowCount()
        self.plan_table.insertRow(r)
        start_date = self.start_date_edit.date().toPython()
        current_date = start_date + timedelta(days=r)
        date_item = QTableWidgetItem(current_date.strftime("%d.%m.%Y"))
        date_item.setFlags(Qt.ItemIsEnabled)
        self.plan_table.setItem(r, 0, date_item)
        self.plan_table.setItem(r, 1, QTableWidgetItem(str(demand)))

    def _remove_plan_day(self):
        row = self.plan_table.currentRow()
        if row >= 0:
            self.plan_table.removeRow(row)
            self._update_plan_dates()

    # -------------------------------------------------------
    # ЗАПУСК GA
    # -------------------------------------------------------
    def _run_ga(self):
        divisions = []
        for r in range(self.div_table.rowCount()):
            name_item = self.div_table.item(r, 0)
            gross_item = self.div_table.item(r, 1)
            harv_a_item = self.div_table.item(r, 2)
            harv_b_item = self.div_table.item(r, 3)
            date_widget = self.div_table.cellWidget(r, 4)
            if not name_item or not gross_item:
                continue
            try:
                start_date = ""
                if isinstance(date_widget, QDateEdit):
                    start_date = date_widget.date().toString("dd.MM.yyyy")
                divisions.append({
                    "name": name_item.text().strip(),
                    "gross": float(gross_item.text()),
                    "harv_a": int(harv_a_item.text() or 0) if harv_a_item else 0,
                    "harv_b": int(harv_b_item.text() or 0) if harv_b_item else 0,
                    "start_date": start_date
                })
            except:
                continue

        if not divisions:
            QMessageBox.warning(self, "Помилка", "Додайте хоча б одне відділення!")
            return

        no_harvesters = [d["name"] for d in divisions if d["harv_a"] == 0 and d["harv_b"] == 0]
        if len(no_harvesters) == len(divisions):
            QMessageBox.warning(self, "Помилка",
                                "Жодне відділення не має комбайнів!\n"
                                "Вкажіть кількість комбайнів типу А або Б.")
            return
        if no_harvesters:
            names = "\n".join(f"  • {n}" for n in no_harvesters)
            reply = QMessageBox.question(
                self, "Увага — відділення без комбайнів",
                f"Наступні відділення не мають комбайнів і їхні поля НЕ будуть зібрані:\n\n"
                f"{names}\n\nПродовжити все одно?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        factory_plan = []
        for r in range(self.plan_table.rowCount()):
            item = self.plan_table.item(r, 1)
            if item:
                try:
                    factory_plan.append(float(item.text()))
                except:
                    factory_plan.append(0.0)

        if not factory_plan:
            QMessageBox.warning(self, "Помилка", "Додайте хоча б один день плану!")
            return

        total_capacity = sum(
            (d["harv_a"] * self.harv_a_cap.value() + d["harv_b"] * self.harv_b_cap.value())
            * self.hours_spin.value()
            for d in divisions
        )
        max_demand = max(factory_plan)
        if total_capacity > max_demand:
            reply = QMessageBox.question(
                self, "Увага — перевищення потреби заводу",
                f"Сумарна добова потужність комбайнів: {total_capacity:,.0f} т\n"
                f"Максимальна добова потреба заводу: {max_demand:,.0f} т\n\n"
                f"Збирати більше ніж потрібно заводу не можна!\n"
                f"Зменшіть кількість комбайнів або потребу заводу.\n\n"
                f"Все одно продовжити?",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply == QMessageBox.No:
                return

        scheduler = GeneticScheduler(
            divisions=divisions,
            harv_a_cap=self.harv_a_cap.value(),
            harv_b_cap=self.harv_b_cap.value(),
            factory_plan=factory_plan,
            hours_per_day=self.hours_spin.value(),
            max_load=self.max_load_spin.value() / 100.0
        )

        self.btn_run.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, self.gen_spin.value())
        self.score_label.setText("Виконується оптимізація...")

        self.ga_thread = GAThread(scheduler, self.pop_spin.value(), self.gen_spin.value())
        self.ga_thread.progress.connect(self._on_progress)
        self.ga_thread.finished.connect(lambda res, score: self._on_finished(res, score, divisions))
        self.ga_thread.start()

    def _on_progress(self, gen, total, score):
        self.progress_bar.setValue(gen)
        self.score_label.setText(f"Покоління {gen}/{total} | Штраф: {score:.1f}")

    def _on_finished(self, result, score, divisions):
        self.btn_run.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.score_label.setText(f"✅ Готово! Фінальний штраф: {score:.1f}")
        self.result_data = result
        self._display_result(result, divisions)
        self._save()

    # -------------------------------------------------------
    # ВІДОБРАЖЕННЯ РЕЗУЛЬТАТУ
    # -------------------------------------------------------
    def _display_result(self, result, divisions):
        div_names = [d["name"] for d in divisions]

        # ── Основна таблиця плану ──
        base_cols = ["День", "Потреба (т)"]
        div_cols = []
        for dn in div_names:
            div_cols += [
                f"{dn}\nКомб. А", f"{dn}\nКомб. Б",
                f"{dn}\nПот-ть т", f"{dn}\nПост. т", f"{dn}\n% навант.",
            ]
        all_cols = base_cols + div_cols + ["Всього (т)", "Відхилення (т)"]
        self.result_table.setColumnCount(len(all_cols))
        self.result_table.setHorizontalHeaderLabels(all_cols)
        self.result_table.setRowCount(len(result))

        for r, row in enumerate(result):
            col = 0
            self.result_table.setItem(r, col, QTableWidgetItem(str(row["day"]))); col += 1
            self.result_table.setItem(r, col, QTableWidgetItem(str(row["demand"]))); col += 1
            for dn in div_names:
                d = row["divisions"].get(dn, {})
                self.result_table.setItem(r, col, QTableWidgetItem(str(d.get("harv_a", 0)))); col += 1
                self.result_table.setItem(r, col, QTableWidgetItem(str(d.get("harv_b", 0)))); col += 1
                self.result_table.setItem(r, col, QTableWidgetItem(str(d.get("capacity", 0)))); col += 1
                self.result_table.setItem(r, col, QTableWidgetItem(str(d.get("supply", 0)))); col += 1
                load = d.get("load_pct", 0)
                load_item = QTableWidgetItem(f"{load}%")
                if load > 100:
                    load_item.setBackground(QColor(255, 100, 100))
                elif load > 80:
                    load_item.setBackground(QColor(255, 200, 100))
                else:
                    load_item.setBackground(QColor(180, 230, 180))
                self.result_table.setItem(r, col, load_item); col += 1
            self.result_table.setItem(r, col, QTableWidgetItem(str(row["total_supply"]))); col += 1
            dev = row["deviation"]
            dev_item = QTableWidgetItem(f"{dev:+.1f}")
            if abs(dev) > row["demand"] * 0.1:
                dev_item.setBackground(QColor(255, 180, 180))
            else:
                dev_item.setBackground(QColor(200, 240, 200))
            self.result_table.setItem(r, col, dev_item)

        self.result_table.resizeColumnsToContents()

        # ── Таблиця навантаження ──
        self._display_load_table(result, div_names)

    def _display_load_table(self, result, div_names):
        """
        Рядки = відділення, стовпці = дати + Середнє + Мін + Макс.
        Заповнюється одразу після генерації плану.
        """
        if not result or not div_names:
            self.load_table.setRowCount(0)
            self.load_table.setColumnCount(0)
            return

        n_days = len(result)

        # Заголовки стовпців — дати з plan_table
        day_labels = []
        for d_idx in range(n_days):
            date_item = self.plan_table.item(d_idx, 0)
            day_labels.append(date_item.text() if date_item else f"День {d_idx + 1}")

        col_labels = ["Відділення"] + day_labels + ["Середнє\n%", "Мін\n%", "Макс\n%"]
        self.load_table.setColumnCount(len(col_labels))
        self.load_table.setRowCount(len(div_names))
        self.load_table.setHorizontalHeaderLabels(col_labels)

        bold_font = QFont()
        bold_font.setBold(True)

        overall_overloads = 0
        overall_warnings = 0

        for r_idx, dn in enumerate(div_names):
            # Назва відділення
            name_item = QTableWidgetItem(dn)
            name_item.setFont(bold_font)
            name_item.setBackground(QColor("#e3f2fd"))
            name_item.setForeground(QColor("#0d47a1"))
            self.load_table.setItem(r_idx, 0, name_item)

            loads = []

            for d_idx, day_row in enumerate(result):
                div_data = day_row["divisions"].get(dn, {})
                load = div_data.get("load_pct", 0.0)
                capacity = div_data.get("capacity", 0.0)
                supply = div_data.get("supply", 0.0)
                harv_a = div_data.get("harv_a", 0)
                harv_b = div_data.get("harv_b", 0)
                total_harv = harv_a + harv_b

                loads.append(load)

                if capacity <= 0:
                    cell_text = "—"
                    bg = QColor("#f5f5f5")
                    fg = QColor("#9e9e9e")
                else:
                    # Навантаження на 1 комбайн = load% відділення
                    # (усі комбайни завантажені рівномірно в моделі)
                    cell_text = f"{load:.1f}%"
                    if load > 100:
                        bg = QColor(255, 100, 100)
                        fg = QColor("white")
                        overall_overloads += 1
                    elif load > 80:
                        bg = QColor(255, 210, 80)
                        fg = QColor("#4a3000")
                        overall_warnings += 1
                    else:
                        bg = QColor(180, 230, 180)
                        fg = QColor("#1b3a1b")

                item = QTableWidgetItem(cell_text)
                item.setTextAlignment(Qt.AlignCenter)
                item.setBackground(bg)
                item.setForeground(fg)

                # Tooltip з деталями по кожному комбайну
                if capacity > 0 and total_harv > 0:
                    cap_per_harv_a = self.harv_a_cap.value() * self.hours_spin.value()
                    cap_per_harv_b = self.harv_b_cap.value() * self.hours_spin.value()
                    supply_per_harv = supply / total_harv if total_harv > 0 else 0
                    load_a = (supply_per_harv / cap_per_harv_a * 100) if cap_per_harv_a > 0 and harv_a > 0 else 0
                    load_b = (supply_per_harv / cap_per_harv_b * 100) if cap_per_harv_b > 0 and harv_b > 0 else 0
                    tooltip = (
                        f"Відділення: {dn}  |  {day_labels[d_idx]}\n"
                        f"Комбайнів А: {harv_a}  |  Б: {harv_b}\n"
                        f"Потужність відділення: {capacity:,.1f} т\n"
                        f"Постачання: {supply:,.1f} т\n"
                        f"─────────────────────\n"
                        f"Навантаження відділення: {load:.1f}%\n"
                        f"На 1 комбайн А: {load_a:.1f}%  ({supply_per_harv:,.1f} т)\n"
                        f"На 1 комбайн Б: {load_b:.1f}%  ({supply_per_harv:,.1f} т)"
                    )
                    item.setToolTip(tooltip)

                self.load_table.setItem(r_idx, 1 + d_idx, item)

            # Статистика
            active_loads = [l for l in loads if l > 0]
            avg_load = sum(active_loads) / len(active_loads) if active_loads else 0.0
            min_load = min(active_loads) if active_loads else 0.0
            max_load = max(active_loads) if active_loads else 0.0

            def stat_item(val):
                it = QTableWidgetItem(f"{val:.1f}%")
                it.setTextAlignment(Qt.AlignCenter)
                it.setFont(bold_font)
                if val > 100:
                    it.setBackground(QColor(255, 100, 100))
                    it.setForeground(QColor("white"))
                elif val > 80:
                    it.setBackground(QColor(255, 210, 80))
                    it.setForeground(QColor("#4a3000"))
                else:
                    it.setBackground(QColor(180, 230, 180))
                    it.setForeground(QColor("#1b3a1b"))
                return it

            self.load_table.setItem(r_idx, 1 + n_days,     stat_item(avg_load))
            self.load_table.setItem(r_idx, 1 + n_days + 1, stat_item(min_load))
            self.load_table.setItem(r_idx, 1 + n_days + 2, stat_item(max_load))

        self.load_table.resizeColumnsToContents()

        # Підсумок
        summary_parts = [f"Відділень: <b>{len(div_names)}</b>", f"Днів: <b>{n_days}</b>"]
        if overall_overloads:
            summary_parts.append(
                f"<span style='color:red;'>🔴 Критичних перевантажень: <b>{overall_overloads}</b></span>")
        if overall_warnings:
            summary_parts.append(
                f"<span style='color:#b8860b;'>🟡 Підвищене навантаження: <b>{overall_warnings}</b></span>")
        if not overall_overloads and not overall_warnings:
            summary_parts.append("<span style='color:green;'>✅ Всі відділення в межах норми</span>")
        self.load_summary_label.setText("   |   ".join(summary_parts))

    # -------------------------------------------------------
    # ЗБЕРЕЖЕННЯ / ЗАВАНТАЖЕННЯ
    # -------------------------------------------------------
    def _save(self):
        divisions = []
        for r in range(self.div_table.rowCount()):
            n = self.div_table.item(r, 0)
            g = self.div_table.item(r, 1)
            a = self.div_table.item(r, 2)
            b = self.div_table.item(r, 3)
            s = self.div_table.item(r, 4)
            divisions.append({
                "name": n.text() if n else "",
                "gross": g.text() if g else "0",
                "harv_a": a.text() if a else "0",
                "harv_b": b.text() if b else "0",
                "start_date": s.text() if s else "",
            })
        plan = []
        for r in range(self.plan_table.rowCount()):
            it = self.plan_table.item(r, 1)
            plan.append(it.text() if it else "0")
        data = {
            "harv_a_cap": self.harv_a_cap.value(),
            "harv_b_cap": self.harv_b_cap.value(),
            "hours": self.hours_spin.value(),
            "max_load": self.max_load_spin.value(),
            "pop_size": self.pop_spin.value(),
            "generations": self.gen_spin.value(),
            "divisions": divisions,
            "plan": plan,
            "result": self.result_data,
            "ratio_a": self.ratio_a_spin.value(),
            "ratio_b": self.ratio_b_spin.value(),
        }
        with open(GA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def _load(self):
        try:
            with open(GA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return

        self.harv_a_cap.setValue(data.get("harv_a_cap", 33))
        self.harv_b_cap.setValue(data.get("harv_b_cap", 22))
        self.hours_spin.setValue(data.get("hours", 16))
        self.max_load_spin.setValue(data.get("max_load", 80))
        self.ratio_a_spin.setValue(data.get("ratio_a", 60))
        self.ratio_b_spin.setValue(data.get("ratio_b", 40))
        self.pop_spin.setValue(data.get("pop_size", 60))
        self.gen_spin.setValue(data.get("generations", 200))

        self.div_table.setRowCount(0)
        for d in data.get("divisions", []):
            self._add_division_row(
                d.get("name", ""), d.get("gross", 0),
                d.get("harv_a", 0), d.get("harv_b", 0),
                d.get("start_date", "")
            )

        self.plan_table.setRowCount(0)
        for val in data.get("plan", []):
            self._add_plan_day(val)

        result = data.get("result", [])
        if result:
            self.result_data = result
            divisions = [{"name": d.get("name", "")} for d in data.get("divisions", [])]
            self._display_result(result, divisions)

    # -------------------------------------------------------
    # ПЕРЕВІРКА ПОТУЖНОСТЕЙ
    # -------------------------------------------------------
    def _check_division_capacity(self, divisions, factory_plan):
        hours = self.hours_spin.value()
        harv_a_cap = self.harv_a_cap.value()
        harv_b_cap = self.harv_b_cap.value()

        plan_dates = []
        for r in range(self.plan_table.rowCount()):
            date_item = self.plan_table.item(r, 0)
            date_str = date_item.text() if date_item else ""
            day_date = None
            for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                try:
                    day_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    continue
            plan_dates.append(day_date)

        div_start_dates = {}
        for r in range(self.div_table.rowCount()):
            n_item = self.div_table.item(r, 0)
            if not n_item:
                continue
            name = n_item.text().strip()
            widget = self.div_table.cellWidget(r, 4)
            if isinstance(widget, QDateEdit):
                div_start_dates[name] = widget.date().toPython()

        problems = []
        for div in divisions:
            name = div["name"]
            gross = div["gross"]
            harv_a = div["harv_a"]
            harv_b = div["harv_b"]
            if harv_a == 0 and harv_b == 0:
                continue
            daily_cap = (harv_a * harv_a_cap + harv_b * harv_b_cap) * hours
            if daily_cap <= 0:
                continue
            start_date = div_start_dates.get(name)
            active_days = sum(
                1 for d in plan_dates
                if d is None or start_date is None or d >= start_date
            )
            if active_days == 0:
                problems.append({"name": name, "gross": gross, "max_can_harvest": 0.0,
                                  "daily_cap": daily_cap, "active_days": 0, "deficit": gross,
                                  "extra_harvesters_a": 0, "extra_harvesters_b": 0,
                                  "start_after_plan": True})
                continue
            max_can_harvest = daily_cap * active_days
            if max_can_harvest >= gross * 0.999:
                continue
            deficit = gross - max_can_harvest
            needed_extra_daily = deficit / active_days
            extra_a = math.ceil(needed_extra_daily / (harv_a_cap * hours)) if harv_a_cap * hours > 0 else 0
            extra_b = math.ceil(needed_extra_daily / (harv_b_cap * hours)) if harv_b_cap * hours > 0 else 0
            problems.append({"name": name, "gross": gross, "max_can_harvest": max_can_harvest,
                              "daily_cap": daily_cap, "active_days": active_days, "deficit": deficit,
                              "extra_harvesters_a": extra_a, "extra_harvesters_b": extra_b,
                              "start_after_plan": False})

        if not problems:
            return True

        dlg = QDialog(self)
        dlg.setWindowTitle("⚠ Недостатня збиральна потужність")
        dlg.setMinimumWidth(600)
        dlg.setMinimumHeight(400)
        layout = QVBoxLayout()

        title_label = QLabel("⚠  Виявлено проблеми зі збиральними потужностями")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        title_label.setFont(title_font)
        title_label.setStyleSheet("color: #b71c1c;")
        layout.addWidget(title_label)

        text = QTextEdit()
        text.setReadOnly(True)
        text.setStyleSheet("font-size: 12px; font-family: monospace;")
        lines = []
        for p in problems:
            lines.append(f"{'─' * 55}")
            lines.append(f"Відділення:  {p['name']}")
            if p.get("start_after_plan"):
                lines.append(f"  ❌ Дата старту відділення виходить за межі плану!")
                lines.append(f"     Активних днів: 0  |  Вал: {p['gross']:,.0f} т")
            else:
                pct = p["max_can_harvest"] / p["gross"] * 100 if p["gross"] > 0 else 0
                lines.append(f"  Плановий вал:         {p['gross']:,.0f} т")
                lines.append(f"  Активних днів:        {p['active_days']}")
                lines.append(f"  Добова потужність:    {p['daily_cap']:,.0f} т/день")
                lines.append(f"  Макс. можна зібрати:  {p['max_can_harvest']:,.0f} т  ({pct:.1f}%)")
                lines.append(f"  ❌ Дефіцит:           {p['deficit']:,.0f} т  ({100 - pct:.1f}%)")
                lines.append(f"  Щоб покрити дефіцит:")
                lines.append(f"    • або +{p['extra_harvesters_a']} комбайн(ів) типу А")
                lines.append(f"    • або +{p['extra_harvesters_b']} комбайн(ів) типу Б")
            lines.append("")
        text.setPlainText("\n".join(lines))
        layout.addWidget(text)

        hint = QLabel("Ви можете продовжити — алгоритм зробить максимум можливого,\n"
                      "але частина врожаю залишиться незібраною в межах плану.")
        hint.setStyleSheet("color: #555; font-size: 11px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_continue = QPushButton("⚠ Все одно продовжити")
        btn_continue.setStyleSheet(
            "background-color: #ff8f00; color: white; font-weight: bold; padding: 6px 16px;")
        btn_cancel = QPushButton("✕ Скасувати і виправити")
        btn_cancel.setStyleSheet(
            "background-color: #c62828; color: white; font-weight: bold; padding: 6px 16px;")
        btn_cancel.setDefault(True)
        btn_continue.clicked.connect(dlg.accept)
        btn_cancel.clicked.connect(dlg.reject)
        btn_row.addStretch()
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_continue)
        layout.addLayout(btn_row)

        dlg.setLayout(layout)
        return dlg.exec() == QDialog.Accepted
