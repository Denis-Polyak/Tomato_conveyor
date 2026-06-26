import matplotlib
import mplcursors
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

      # =========================
      # 1. ДАТИ З ПЛАНУ (вісь X)
      # =========================
      ga_tab = self.app.ga_tab
      plan_table = ga_tab.plan_table

      parsed_dates = []
      for r in range(plan_table.rowCount()):
        date_item = plan_table.item(r, 0)
        if date_item:
          d = self.parse_date(date_item.text())
          if d:
            parsed_dates.append(d)

      if not parsed_dates:
        ax.text(0.5, 0.5, "Немає дат у плані", ha="center", va="center")
        self.canvas.draw()
        return

      n_days = len(parsed_dates)

      # =========================
      # 2. ДОЗРІЛО (наростаючим)
      # =========================
      main_table = self.app.table
      fields = []
      for r in range(main_table.rowCount()):
        try:
          gross_item = main_table.item(r, 3)
          ripening_item = main_table.item(r, 6)
          if not gross_item or not ripening_item:
            continue
          gross = self.to_float(gross_item.text())
          ripening = self.parse_date(ripening_item.text())
          if gross > 0 and ripening:
            fields.append({"gross": gross, "ripening": ripening})
        except:
          continue

      available = []
      for day in parsed_dates:
        total = sum(f["gross"] for f in fields if f["ripening"] <= day)
        available.append(total)

      # =========================
      # 3. ЗІБРАНО (наростаючим)
      # =========================
      harvest_table = self.app.harvest_plan_tab.table
      n_cols = harvest_table.columnCount()

      harvested_per_day = []
      for r in range(1, harvest_table.rowCount()):
        date_item = harvest_table.item(r, 0)
        if not date_item:
          harvested_per_day.append(0.0)
          continue
        row_sum = 0.0
        for c in range(2, n_cols):
          item = harvest_table.item(r, c)
          if item and item.text().strip():
            row_sum += self.to_float(item.text())
        harvested_per_day.append(row_sum)

      while len(harvested_per_day) < n_days:
        harvested_per_day.append(0.0)
      harvested_per_day = harvested_per_day[:n_days]

      acc_harvested = []
      acc = 0.0
      for h in harvested_per_day:
        acc += h
        acc_harvested.append(acc)

      # =========================
      # 4. ЗАЛИШОК
      # =========================
      remain = [avail - acc_h for avail, acc_h in zip(available, acc_harvested)]

      # =========================
      # 5. PLOT + TOOLTIPS
      # =========================
      lines = {
        "Дозріло":
          ax.plot(parsed_dates, available, label="Дозріло (наростаючим)", color="gold", marker="o", linewidth=2)[0],
        "Зібрано":
          ax.plot(parsed_dates, acc_harvested, label="Зібрано (наростаючим)", color="green", marker="o", linewidth=2)[
            0],
        "Залишок": ax.plot(parsed_dates, remain, label="Залишок для збирання", color="red", marker="o", linewidth=2)[0],
      }

      cursor = mplcursors.cursor(list(lines.values()), hover=True)

      @cursor.connect("add")
      def on_add(sel):
        idx = int(round(sel.index))
        idx = max(0, min(idx, len(parsed_dates) - 1))

        date_str = parsed_dates[idx].strftime("%d.%m.%Y")

        label_map = {
          id(lines["Дозріло"]): ("Дозріло", available, "gold"),
          id(lines["Зібрано"]): ("Зібрано", acc_harvested, "green"),
          id(lines["Залишок"]): ("Залишок", remain, "tomato"),
        }

        name, data, color = label_map[id(sel.artist)]
        value = data[idx]

        sel.annotation.set_text(f"📅 {date_str}\n{name}: {value:,.0f} т")
        sel.annotation.get_bbox_patch().set(
          facecolor=color,
          alpha=0.85,
          edgecolor="white",
          linewidth=1.5,
          boxstyle="round,pad=0.4"
        )
        sel.annotation.set_fontsize(10)
        sel.annotation.set_color("white" if color != "gold" else "black")

      ax.set_title("Агро-план збирання (наростаючим підсумком)")
      ax.set_xlabel("Дата")
      ax.set_ylabel("Тонни")
      ax.grid(True)
      ax.legend()

      self.figure.autofmt_xdate(rotation=45)
      ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d.%m"))

      self.canvas.draw()
