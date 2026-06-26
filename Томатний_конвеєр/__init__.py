import sys
import json

import pandas as pd
from datetime import datetime, timedelta
from genetic_optimizer import GeneticOptimizerTab
from detailed_harvest_plan_tab import DetailedHarvestPlanTab

from PySide6.QtCore import Qt, QPoint
from PySide6.QtWidgets import (
    QApplication, QWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel,
    QFileDialog, QTabWidget, QComboBox, QMenu,
    QInputDialog, QMessageBox
)

from tomato_conveyor.Томатний_конвеєр.graphs import GraphsTab

FILE_NAME = "data.json"
DIVISIONS_FILE = "divisions.json"

EXPECTED_COLUMNS = [
    "Номер поля",
    "Площа (га)",
    "Врожайність (т/га)",
    "Валовий збір (т)",
    "Дата висадки",
    "Вегетаційний період"
]

TABLE_HEADERS = [
    "Номер поля",          # 0
    "Площа (га)",          # 1
    "Врожайність (т/га)",  # 2
    "Валовий збір (т)",    # 3
    "Дата висадки",        # 4
    "Вегетаційний період", # 5
    "Дозрівання прогноз",  # 6
    "Початок збирання",    # 7
    "Закінчення збирання", # 8
    "Відхилення",          # 9
    "Відділення",          # 10
]
COL_DIVISION = len(TABLE_HEADERS) - 1  # 10


# =========================
# DIVISIONS MANAGER
# =========================
class DivisionsManager:
    def __init__(self):
        self._divisions: list[str] = []
        self._load()

    def _load(self):
        try:
            with open(DIVISIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._divisions = [str(d) for d in data if str(d).strip()]
        except FileNotFoundError:
            self._divisions = []

    def _save(self):
        with open(DIVISIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(self._divisions, f, ensure_ascii=False, indent=2)

    def get_all(self) -> list[str]:
        return list(self._divisions)

    def add(self, name: str) -> bool:
        name = name.strip()
        if not name or name in self._divisions:
            return False
        self._divisions.append(name)
        self._save()
        return True

    def remove(self, name: str):
        if name in self._divisions:
            self._divisions.remove(name)
            self._save()

    def rename(self, old: str, new: str) -> bool:
        new = new.strip()
        if not new or new == old:
            return False
        if old in self._divisions:
            idx = self._divisions.index(old)
            self._divisions[idx] = new
            self._save()
            return True
        return False


divisions_manager = DivisionsManager()


# =========================
# TABLE
# =========================
class TableWidget(QTableWidget):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app  # пряме посилання на FieldApp
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.ExtendedSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos: QPoint):
        selected_rows = sorted(set(idx.row() for idx in self.selectedIndexes()))
        if not selected_rows:
            return

        menu = QMenu(self)

        # --- Підменю: Призначити відділення ---
        submenu = menu.addMenu(f"📋  Призначити відділення ({len(selected_rows)} пол.)")

        all_divisions = divisions_manager.get_all()
        if all_divisions:
            for division in all_divisions:
                action = submenu.addAction(division)
                action.setData(("assign", division))
            submenu.addSeparator()

        new_action = submenu.addAction("➕  Нове відділення...")
        new_action.setData(("new", None))

        # --- Очистити відділення ---
        clear_action = menu.addAction("🗑️  Очистити відділення")

        # --- Управління відділеннями ---
        manage_menu = menu.addMenu("⚙️  Управління відділеннями")
        rename_action = manage_menu.addAction("✏️  Перейменувати відділення...")
        delete_div_action = manage_menu.addAction("❌  Видалити відділення зі списку...")

        # --- Видалити рядки ---
        menu.addSeparator()
        label = 'и' if len(selected_rows) > 1 else 'ок'
        delete_rows_action = menu.addAction(f"🗑️  Видалити рядк{label} ({len(selected_rows)})")

        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen == delete_rows_action:
            for row in sorted(selected_rows, reverse=True):
                self.removeRow(row)
            self.app.save_data()
            return

        if chosen == clear_action:
            for row in selected_rows:
                self.setItem(row, COL_DIVISION, QTableWidgetItem(""))
            self.app.save_data()
            return

        if chosen == rename_action:
            self._handle_rename_division()
            return

        if chosen == delete_div_action:
            self._handle_delete_division()
            return

        # Дії підменю (assign / new)
        data = chosen.data()
        if not data:
            return

        action_type, value = data

        if action_type == "new":
            text, ok = QInputDialog.getText(
                self, "Нове відділення", "Введіть назву нового відділення:"
            )
            if not ok or not text.strip():
                return
            value = text.strip()
            divisions_manager.add(value)
            self.app.refresh_division_combo()

        if value:
            for row in selected_rows:
                self.setItem(row, COL_DIVISION, QTableWidgetItem(value))
            self.app.save_data()

    def _handle_rename_division(self):
        divisions = divisions_manager.get_all()
        if not divisions:
            QMessageBox.information(self, "Відділення", "Список відділень порожній.")
            return

        old_name, ok = QInputDialog.getItem(
            self, "Перейменувати", "Виберіть відділення:", divisions, 0, False
        )
        if not ok:
            return

        new_name, ok2 = QInputDialog.getText(
            self, "Нова назва", f"Нова назва для «{old_name}»:"
        )
        if not ok2 or not new_name.strip():
            return

        new_name = new_name.strip()
        if divisions_manager.rename(old_name, new_name):
            for row in range(self.rowCount()):
                item = self.item(row, COL_DIVISION)
                if item and item.text() == old_name:
                    item.setText(new_name)
            self.app.save_data()
            self.app.refresh_division_combo()

    def _handle_delete_division(self):
        divisions = divisions_manager.get_all()
        if not divisions:
            QMessageBox.information(self, "Відділення", "Список відділень порожній.")
            return

        name, ok = QInputDialog.getItem(
            self, "Видалити відділення", "Виберіть відділення для видалення зі списку:", divisions, 0, False
        )
        if not ok:
            return

        reply = QMessageBox.question(
            self, "Підтвердження",
            f"Видалити «{name}» зі списку?\n(Рядки таблиці залишаться без змін)",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            divisions_manager.remove(name)
            self.app.refresh_division_combo()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            selected_rows = sorted(
                set(idx.row() for idx in self.selectedIndexes()),
                reverse=True
            )
            for row in selected_rows:
                self.removeRow(row)
            self.app.save_data()
        else:
            super().keyPressEvent(event)


# =========================
# MAIN APP
# =========================
class FieldApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Агро таблиця (Excel + автозбереження)")
        self.setGeometry(100, 100, 1300, 520)

        self.layout = QVBoxLayout()

        # ===== TABLE =====
        self.table = TableWidget(self)  # передаємо self як app
        self.table.setColumnCount(len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.layout.addWidget(self.table)

        # ===== INPUTS =====
        form = QHBoxLayout()

        self.division_input = QComboBox()
        self.division_input.setEditable(False)
        self.division_input.setFixedWidth(180)
        self._populate_division_combo()

        self.btn_new_division = QPushButton("+ Відділення")
        self.btn_new_division.setFixedWidth(110)
        self.btn_new_division.setToolTip("Створити нове відділення")
        self.btn_new_division.clicked.connect(self._create_new_division)

        self.field_number = QLineEdit()
        self.area = QLineEdit()
        self.yield_ha = QLineEdit()
        self.plant_date = QLineEdit()
        self.veg_period = QLineEdit()

        form.addWidget(QLabel("Відділення:"))
        form.addWidget(self.division_input)
        form.addWidget(self.btn_new_division)
        form.addWidget(QLabel("№"))
        form.addWidget(self.field_number)
        form.addWidget(QLabel("Площа"))
        form.addWidget(self.area)
        form.addWidget(QLabel("Врожайність"))
        form.addWidget(self.yield_ha)
        form.addWidget(QLabel("Дата (YYYY-MM-DD)"))
        form.addWidget(self.plant_date)
        form.addWidget(QLabel("Вегетація"))
        form.addWidget(self.veg_period)

        self.layout.addLayout(form)

        # ===== BUTTONS =====
        btns = QHBoxLayout()

        self.btn_add = QPushButton("Додати")
        self.btn_add.clicked.connect(self.add_row)

        self.btn_import = QPushButton("Імпорт Excel")
        self.btn_import.clicked.connect(self.import_excel)

        hint = QLabel("💡 Виділіть поля → права кнопка → Призначити відділення")
        hint.setStyleSheet("color: gray; font-size: 11px;")

        btns.addWidget(self.btn_add)
        btns.addWidget(self.btn_import)
        btns.addStretch()
        btns.addWidget(hint)

        self.layout.addLayout(btns)

        # ===== TABS =====
        self.main_tab = QWidget()
        self.main_tab.setLayout(self.layout)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.main_tab, "Основна таблиця")

        self.ga_tab = GeneticOptimizerTab(self)
        self.tabs.addTab(self.ga_tab, "Оптимізатор ТЗК")

        self.harvest_plan_tab = DetailedHarvestPlanTab(self)
        self.tabs.addTab(self.harvest_plan_tab, "Детальний план збирання")

        self.graphs_tab = GraphsTab(self)
        self.tabs.addTab(self.graphs_tab, "Графіки")

        outer = QVBoxLayout()
        outer.addWidget(self.tabs)
        self.setLayout(outer)

        self.load_data()

    # ----- Division ComboBox helpers -----

    def _populate_division_combo(self):
        self.division_input.clear()
        self.division_input.addItem("")
        for d in divisions_manager.get_all():
            self.division_input.addItem(d)

    def refresh_division_combo(self):
        current = self.division_input.currentText()
        self._populate_division_combo()
        idx = self.division_input.findText(current)
        if idx >= 0:
            self.division_input.setCurrentIndex(idx)

    def _create_new_division(self):
        text, ok = QInputDialog.getText(
            self, "Нове відділення", "Введіть назву нового відділення:"
        )
        if not ok or not text.strip():
            return
        name = text.strip()
        divisions_manager.add(name)
        self.refresh_division_combo()
        idx = self.division_input.findText(name)
        if idx >= 0:
            self.division_input.setCurrentIndex(idx)

    # =========================
    # ADD ROW
    # =========================
    def add_row(self):
        try:
            division = self.division_input.currentText().strip()
            field = self.field_number.text()
            area = float(self.area.text())
            yield_ha = float(self.yield_ha.text())
            plant_date = datetime.strptime(self.plant_date.text(), "%Y-%m-%d")
            veg = int(self.veg_period.text())

            gross = area * yield_ha
            ripening = plant_date + timedelta(days=veg)

            row = self.table.rowCount()
            self.table.insertRow(row)

            values = [
                field,                          # 0 - Номер поля
                area,                           # 1 - Площа
                yield_ha,                       # 2 - Врожайність
                gross,                          # 3 - Валовий збір
                plant_date.date().isoformat(),  # 4 - Дата висадки
                veg,                            # 5 - Вегетаційний період
                ripening.date().isoformat(),    # 6 - Дозрівання прогноз
                "", "", "",                     # 7,8,9 - Початок, Закінчення, Відхилення
                division                        # 10 - Відділення
            ]

            for col, val in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(str(val)))

            self.save_data()

        except Exception as e:
            print("Помилка:", e)

    # =========================
    # IMPORT EXCEL
    # =========================
    def import_excel(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Вибрати Excel файл", "", "Excel Files (*.xlsx *.xls)"
        )
        if not file_path:
            return

        df_raw = pd.read_excel(file_path, engine="openpyxl", header=None)

        header_row = None
        for i, row in df_raw.iterrows():
            row_values = row.astype(str).str.strip().tolist()
            matches = sum(1 for v in row_values if v in EXPECTED_COLUMNS)
            if matches >= 2:
                header_row = i
                break

        if header_row is None:
            print("❌ Не вдалось знайти рядок з заголовками!")
            return

        df = pd.read_excel(file_path, engine="openpyxl", header=header_row)
        df.columns = df.columns.astype(str).str.strip().str.replace(r'\s+', ' ', regex=True)

        column_mapping = {
            "Площа поля еф., га": "Площа (га)",
            "Врожайність, т/га": "Врожайність (т/га)",
            "Валовий збір, т": "Валовий збір (т)",
        }
        df = df.rename(columns=column_mapping)

        import_columns = [c for c in EXPECTED_COLUMNS]
        df = df.reindex(columns=import_columns)
        df = df.dropna(how='all').reset_index(drop=True)

        current_division = None
        rows_to_keep = []

        for _, row in df.iterrows():
            area = row.get("Площа (га)")
            try:
                float(area)
                is_field = True
            except:
                is_field = False

            if not is_field:
                current_division = str(row["Номер поля"]).strip()
                if current_division:
                    divisions_manager.add(current_division)
                continue

            row = row.copy()
            row["Відділення"] = current_division if current_division else ""
            rows_to_keep.append(row)

        self.refresh_division_combo()

        df = pd.DataFrame(rows_to_keep).reset_index(drop=True)

        stop_mask = df["Номер поля"].astype(str).str.contains("Панавіт", na=False)
        if stop_mask.any():
            stop_index = stop_mask.idxmax()
            df = df.iloc[:stop_index].reset_index(drop=True)

        df = df[df["Дата висадки"].notna() & df["Вегетаційний період"].notna()].reset_index(drop=True)

        def calc_ripening(row):
            try:
                date = pd.to_datetime(row["Дата висадки"])
                veg = int(float(str(row["Вегетаційний період"]).strip()))
                return (date + timedelta(days=veg)).strftime("%Y-%m-%d")
            except:
                return ""

        df["Дозрівання прогноз"] = df.apply(calc_ripening, axis=1)

        export_columns = [
            "Номер поля",          # 0
            "Площа (га)",          # 1
            "Врожайність (т/га)",  # 2
            "Валовий збір (т)",    # 3
            "Дата висадки",        # 4
            "Вегетаційний період", # 5
            "Дозрівання прогноз",  # 6
            "Початок збирання",    # 7
            "Закінчення збирання", # 8
            "Відхилення",          # 9
            "Відділення",          # 10
        ]
        df = df.reindex(columns=export_columns)

        self.table.setRowCount(0)

        for row in df.itertuples(index=False):
            row_index = self.table.rowCount()
            self.table.insertRow(row_index)

            for col, value in enumerate(row):
                text = "" if pd.isna(value) else str(value)
                if "00:00:00" in text:
                    text = text.replace(" 00:00:00", "")
                self.table.setItem(row_index, col, QTableWidgetItem(text))

        self.save_data()

    # =========================
    # SAVE / LOAD
    # =========================
    def save_data(self):
        data = []
        for row in range(self.table.rowCount()):
            row_data = []
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                row_data.append(item.text() if item else "")
            data.append(row_data)

        with open(FILE_NAME, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    def load_data(self):
        try:
            with open(FILE_NAME, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.table.setRowCount(0)
            for row_data in data:
                row = self.table.rowCount()
                self.table.insertRow(row)
                for col, value in enumerate(row_data):
                    self.table.setItem(row, col, QTableWidgetItem(str(value)))
        except FileNotFoundError:
            pass


# =========================
# RUN
# =========================
app = QApplication(sys.argv)
window = FieldApp()
window.show()
sys.exit(app.exec())
