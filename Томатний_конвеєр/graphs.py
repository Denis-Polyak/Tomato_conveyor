import matplotlib
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QLabel, QMessageBox, QStackedLayout
)

matplotlib.use("QtAgg")

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure


class GraphsTab(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app

        main_layout = QVBoxLayout()
        self.stack = QStackedLayout()

        # =========================
        # МЕНЮ
        # =========================
        self.menu = QWidget()
        menu_layout = QVBoxLayout()

        title = QLabel("📊 Графіки плану збирання")
        menu_layout.addWidget(title)

        btn = QPushButton("📈 Динаміка збирання")
        btn.clicked.connect(self.open_graph)

        menu_layout.addWidget(btn)
        self.menu.setLayout(menu_layout)

        # =========================
        # ГРАФІК
        # =========================
        self.graph_page = QWidget()
        graph_layout = QVBoxLayout()

        self.figure = Figure()
        self.canvas = FigureCanvas(self.figure)
        graph_layout.addWidget(self.canvas)

        self.btn_back = QPushButton("⬅ Назад")
        self.btn_back.clicked.connect(self.go_back)
        graph_layout.addWidget(self.btn_back)

        self.graph_page.setLayout(graph_layout)

        self.stack.addWidget(self.menu)
        self.stack.addWidget(self.graph_page)

        main_layout.addLayout(self.stack)
        self.setLayout(main_layout)

    # =========================
    # NAVIGATION
    # =========================
    def open_graph(self):
        if self.app.harvest_plan_tab.table.rowCount() <= 1:
            QMessageBox.warning(
                self,
                "Немає даних",
                "Спочатку згенеруйте детальний план збирання"
            )
            return

        self.stack.setCurrentWidget(self.graph_page)
        self.draw()

    def go_back(self):
        self.stack.setCurrentWidget(self.menu)

    # =========================
    # HELPERS
    # =========================
    def to_float(self, text):
        if not text:
            return 0.0
        try:
            return float(text.replace(" ", "").replace(",", ""))
        except:
            return 0.0

    def parse_date(self, text):
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return datetime.strptime(text, fmt).date()
            except:
                continue
        return None

    # =========================
    # GRAPH
    # =========================
    def draw(self):
        self.figure.clear()
        ax = self.figure.add_subplot(111)

        plan_tab = self.app.harvest_plan_tab
        table = plan_tab.table

        n_rows = table.rowCount()
        n_cols = table.columnCount()

        # =========================
        # 1. ДАТИ + ЗБИРАННЯ
        # =========================
        dates = []
        harvested = []

        plan_table = self.app.genetic_optimizer_tab.plan_table  # або шлях до твого плану

        n_plan_rows = plan_table.rowCount()

        for r in range(n_plan_rows):
          date_item = plan_table.item(r, 0)
          if not date_item:
            continue

          dates.append(date_item.text())

          row_sum = 0.0
          for c in range(2, n_cols):
            item = table.item(r, c)
            if item and item.text().strip():
              row_sum += self.to_float(item.text())

          harvested.append(row_sum)

        # =========================
        # 2. ПОЛЯ (дозрівання)
        # =========================
        fields = []
        main_table = self.app.table

        for r in range(main_table.rowCount()):
            try:
                gross = self.to_float(main_table.item(r, 3).text())
                ripening_text = main_table.item(r, 6).text()

                ripening = self.parse_date(ripening_text)

                if gross > 0 and ripening:
                    fields.append({
                        "gross": gross,
                        "ripening": ripening
                    })
            except:
                continue

        # =========================
        # 3. ДОСТУПНЕ ДОЗРІЛЕ
        # =========================
        available = []

        for d in dates:
            day = self.parse_date(d)
            if not day:
                available.append(0)
                continue

            avail = sum(
                f["gross"]
                for f in fields
                if f["ripening"] <= day
            )
            available.append(avail)

        # =========================
        # 4. ЗАЛИШОК = ДОСТУПНЕ - ЗІБРАНЕ
        # =========================
        remain = []
        acc = 0.0

        for h, avail in zip(harvested, available):
            acc += h
            remain.append(avail - acc)

        # =========================
        # 5. PLOT
        # =========================
        ax.plot(dates, available, label="Дозріло (доступно)")
        ax.plot(dates, harvested, label="Збирання")
        ax.plot(dates, remain, label="Залишок")

        ax.set_title("Агро-план збирання (реальна модель)")
        ax.set_xlabel("Дата")
        ax.set_ylabel("Тонни")

        ax.grid(True)
        ax.legend()
        ax.tick_params(axis='x', rotation=45)

        self.canvas.draw()
