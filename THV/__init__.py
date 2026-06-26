import sys
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QHBoxLayout, QVBoxLayout, QLabel, QFrame
)
from PySide6.QtCore import Qt


class PanelWidget(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Box)
        self.setLineWidth(2)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        label = QLabel(title)
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)


class MainMenu(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Управління ТЗК")
        self.resize(960, 600)

        central = QWidget()
        self.setCentralWidget(central)

        outer_frame = QFrame(central)
        outer_frame.setFrameShape(QFrame.Box)
        outer_frame.setLineWidth(2)

        outer_layout = QVBoxLayout(central)
        outer_layout.addWidget(outer_frame)

        h_layout = QHBoxLayout(outer_frame)
        h_layout.setSpacing(0)
        h_layout.setContentsMargins(0, 0, 0, 0)

        left_panel = PanelWidget("Додати новий\nТЗК")
        right_panel = PanelWidget("Перегляд та редагування\nвхідних ТТХ ТЗК")

        h_layout.addWidget(left_panel)
        h_layout.addWidget(right_panel)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainMenu()
    window.show()
    sys.exit(app.exec())
