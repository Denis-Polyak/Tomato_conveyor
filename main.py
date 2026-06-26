import sys
import json
import os
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QFrame, QPushButton, QListWidget, QListWidgetItem,
    QDialog, QLineEdit, QFormLayout, QDialogButtonBox, QMessageBox,
    QSplitter, QTreeWidget, QTreeWidgetItem, QComboBox, QInputDialog,
    QTabWidget, QAbstractItemView, QScrollArea
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont

DATA_FILE = os.path.join(os.path.dirname(__file__), "requisites_data.json")


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"requisites": [], "groups": [], "relations": []}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── Dialogs ────────────────────────────────────────────────────────────────

class RequisiteDialog(QDialog):
    """Create / edit a single requisite."""
    TYPES = ["Завод", "Відділення", "Поле", "Інше"]

    def __init__(self, parent=None, name="", rtype="Завод", description=""):
        super().__init__(parent)
        self.setWindowTitle("Реквізит")
        self.setMinimumWidth(360)

        form = QFormLayout(self)

        self.name_edit = QLineEdit(name)
        self.type_combo = QComboBox()
        self.type_combo.addItems(self.TYPES)
        if rtype in self.TYPES:
            self.type_combo.setCurrentText(rtype)
        self.desc_edit = QLineEdit(description)

        form.addRow("Назва:", self.name_edit)
        form.addRow("Тип:", self.type_combo)
        form.addRow("Опис:", self.desc_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return (
            self.name_edit.text().strip(),
            self.type_combo.currentText(),
            self.desc_edit.text().strip(),
        )


class GroupDialog(QDialog):
    """Create / edit a group."""
    def __init__(self, parent=None, name="", description=""):
        super().__init__(parent)
        self.setWindowTitle("Група")
        self.setMinimumWidth(360)

        form = QFormLayout(self)
        self.name_edit = QLineEdit(name)
        self.desc_edit = QLineEdit(description)
        form.addRow("Назва групи:", self.name_edit)
        form.addRow("Опис:", self.desc_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.name_edit.text().strip(), self.desc_edit.text().strip()


class RelationDialog(QDialog):
    """Link two requisites / groups together."""
    def __init__(self, parent, data):
        super().__init__(parent)
        self.setWindowTitle("Додати зв'язок")
        self.setMinimumWidth(400)
        self.data = data

        form = QFormLayout(self)

        self.from_combo = QComboBox()
        self.to_combo = QComboBox()
        self.label_edit = QLineEdit()
        self.label_edit.setPlaceholderText("напр. «постачає сировину»")

        all_items = (
            [("r:" + r["id"], f"[Реквізит] {r['name']} ({r['type']})") for r in data["requisites"]] +
            [("g:" + g["id"], f"[Група] {g['name']}") for g in data["groups"]]
        )
        for uid, label in all_items:
            self.from_combo.addItem(label, uid)
            self.to_combo.addItem(label, uid)

        form.addRow("Від:", self.from_combo)
        form.addRow("До:", self.to_combo)
        form.addRow("Тип зв'язку:", self.label_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return (
            self.from_combo.currentData(),
            self.to_combo.currentData(),
            self.label_edit.text().strip(),
        )


# ─── Main Requisites Window ──────────────────────────────────────────────────

class RequisitesWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Спільні реквізити")
        self.resize(1100, 680)
        self.data = load_data()

        tabs = QTabWidget()
        self.setCentralWidget(tabs)

        tabs.addTab(self._build_requisites_tab(), "Реквізити")
        tabs.addTab(self._build_groups_tab(), "Групи")
        tabs.addTab(self._build_relations_tab(), "Взаємозв'язки")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _next_id(self, collection):
        existing = {int(x["id"]) for x in collection if x["id"].isdigit()}
        i = 1
        while i in existing:
            i += 1
        return str(i)

    def _find(self, collection, uid):
        return next((x for x in collection if x["id"] == uid), None)

    def _req_label(self, uid):
        r = self._find(self.data["requisites"], uid)
        return f"{r['name']} ({r['type']})" if r else uid

    def _grp_label(self, uid):
        g = self._find(self.data["groups"], uid)
        return g["name"] if g else uid

    def _entity_label(self, uid):
        if uid.startswith("r:"):
            return "[Р] " + self._req_label(uid[2:])
        if uid.startswith("g:"):
            return "[Г] " + self._grp_label(uid[2:])
        return uid

    # ── Requisites tab ───────────────────────────────────────────────────────

    def _build_requisites_tab(self):
        w = QWidget()
        vbox = QVBoxLayout(w)

        # toolbar
        hbar = QHBoxLayout()
        btn_add  = QPushButton("＋ Додати")
        btn_edit = QPushButton("✎ Редагувати")
        btn_del  = QPushButton("✕ Видалити")
        for b in (btn_add, btn_edit, btn_del):
            b.setFixedHeight(30)
            hbar.addWidget(b)
        hbar.addStretch()
        vbox.addLayout(hbar)

        self.req_list = QListWidget()
        self.req_list.setAlternatingRowColors(True)
        vbox.addWidget(self.req_list)

        btn_add.clicked.connect(self._add_requisite)
        btn_edit.clicked.connect(self._edit_requisite)
        btn_del.clicked.connect(self._delete_requisite)

        self._refresh_req_list()
        return w

    def _refresh_req_list(self):
        self.req_list.clear()
        for r in self.data["requisites"]:
            item = QListWidgetItem(f"{r['name']}  [{r['type']}]  {r.get('description','')}")
            item.setData(Qt.UserRole, r["id"])
            self.req_list.addItem(item)

    def _add_requisite(self):
        dlg = RequisiteDialog(self)
        if dlg.exec() == QDialog.Accepted:
            name, rtype, desc = dlg.values()
            if not name:
                return
            self.data["requisites"].append({
                "id": self._next_id(self.data["requisites"]),
                "name": name, "type": rtype, "description": desc
            })
            save_data(self.data)
            self._refresh_req_list()
            self._refresh_groups_tree()
            self._refresh_relations_list()

    def _edit_requisite(self):
        item = self.req_list.currentItem()
        if not item:
            return
        uid = item.data(Qt.UserRole)
        r = self._find(self.data["requisites"], uid)
        dlg = RequisiteDialog(self, r["name"], r["type"], r.get("description",""))
        if dlg.exec() == QDialog.Accepted:
            name, rtype, desc = dlg.values()
            if not name:
                return
            r["name"], r["type"], r["description"] = name, rtype, desc
            save_data(self.data)
            self._refresh_req_list()
            self._refresh_groups_tree()
            self._refresh_relations_list()

    def _delete_requisite(self):
        item = self.req_list.currentItem()
        if not item:
            return
        uid = item.data(Qt.UserRole)
        r = self._find(self.data["requisites"], uid)
        if QMessageBox.question(
            self, "Видалення", f"Видалити «{r['name']}»?",
            QMessageBox.Yes | QMessageBox.No
        ) != QMessageBox.Yes:
            return
        # remove from groups
        for g in self.data["groups"]:
            g.setdefault("members", [])
            if uid in g["members"]:
                g["members"].remove(uid)
        # remove relations
        self.data["relations"] = [
            rel for rel in self.data["relations"]
            if rel["from"] != "r:"+uid and rel["to"] != "r:"+uid
        ]
        self.data["requisites"] = [x for x in self.data["requisites"] if x["id"] != uid]
        save_data(self.data)
        self._refresh_req_list()
        self._refresh_groups_tree()
        self._refresh_relations_list()

    # ── Groups tab ───────────────────────────────────────────────────────────

    def _build_groups_tab(self):
        w = QWidget()
        vbox = QVBoxLayout(w)

        hbar = QHBoxLayout()
        btn_add_grp   = QPushButton("＋ Нова група")
        btn_edit_grp  = QPushButton("✎ Перейменувати")
        btn_del_grp   = QPushButton("✕ Видалити групу")
        btn_add_mem   = QPushButton("＋ Додати реквізит")
        btn_rem_mem   = QPushButton("− Вилучити реквізит")
        for b in (btn_add_grp, btn_edit_grp, btn_del_grp, btn_add_mem, btn_rem_mem):
            b.setFixedHeight(30)
            hbar.addWidget(b)
        hbar.addStretch()
        vbox.addLayout(hbar)

        self.groups_tree = QTreeWidget()
        self.groups_tree.setHeaderLabels(["Назва", "Тип / Опис"])
        self.groups_tree.setColumnWidth(0, 300)
        vbox.addWidget(self.groups_tree)

        btn_add_grp.clicked.connect(self._add_group)
        btn_edit_grp.clicked.connect(self._edit_group)
        btn_del_grp.clicked.connect(self._delete_group)
        btn_add_mem.clicked.connect(self._add_member)
        btn_rem_mem.clicked.connect(self._remove_member)

        self._refresh_groups_tree()
        return w

    def _refresh_groups_tree(self):
        self.groups_tree.clear()
        for g in self.data["groups"]:
            root = QTreeWidgetItem([g["name"], g.get("description", "")])
            root.setData(0, Qt.UserRole, ("group", g["id"]))
            bold = QFont()
            bold.setBold(True)
            root.setFont(0, bold)
            for mid in g.get("members", []):
                r = self._find(self.data["requisites"], mid)
                if r:
                    child = QTreeWidgetItem([r["name"], f"{r['type']}  {r.get('description','')}"])
                    child.setData(0, Qt.UserRole, ("member", g["id"], mid))
                    root.addChild(child)
            self.groups_tree.addTopLevelItem(root)
            root.setExpanded(True)

    def _add_group(self):
        dlg = GroupDialog(self)
        if dlg.exec() == QDialog.Accepted:
            name, desc = dlg.values()
            if not name:
                return
            self.data["groups"].append({
                "id": self._next_id(self.data["groups"]),
                "name": name, "description": desc, "members": []
            })
            save_data(self.data)
            self._refresh_groups_tree()

    def _edit_group(self):
        item = self.groups_tree.currentItem()
        if not item:
            return
        d = item.data(0, Qt.UserRole)
        if not d or d[0] != "group":
            return
        g = self._find(self.data["groups"], d[1])
        dlg = GroupDialog(self, g["name"], g.get("description",""))
        if dlg.exec() == QDialog.Accepted:
            name, desc = dlg.values()
            if not name:
                return
            g["name"], g["description"] = name, desc
            save_data(self.data)
            self._refresh_groups_tree()
            self._refresh_relations_list()

    def _delete_group(self):
        item = self.groups_tree.currentItem()
        if not item:
            return
        d = item.data(0, Qt.UserRole)
        if not d or d[0] != "group":
            return
        g = self._find(self.data["groups"], d[1])
        if QMessageBox.question(
            self, "Видалення", f"Видалити групу «{g['name']}»?",
            QMessageBox.Yes | QMessageBox.No
        ) != QMessageBox.Yes:
            return
        self.data["relations"] = [
            rel for rel in self.data["relations"]
            if rel["from"] != "g:"+d[1] and rel["to"] != "g:"+d[1]
        ]
        self.data["groups"] = [x for x in self.data["groups"] if x["id"] != d[1]]
        save_data(self.data)
        self._refresh_groups_tree()
        self._refresh_relations_list()

    def _add_member(self):
        item = self.groups_tree.currentItem()
        if not item:
            return
        d = item.data(0, Qt.UserRole)
        gid = d[1] if d and d[0] in ("group", "member") else None
        if gid is None:
            return
        g = self._find(self.data["groups"], gid)
        available = [r for r in self.data["requisites"] if r["id"] not in g.get("members", [])]
        if not available:
            QMessageBox.information(self, "Інфо", "Немає вільних реквізитів.")
            return
        choices = [f"{r['name']} ({r['type']})" for r in available]
        choice, ok = QInputDialog.getItem(self, "Додати реквізит", "Оберіть реквізит:", choices, 0, False)
        if ok:
            idx = choices.index(choice)
            g.setdefault("members", []).append(available[idx]["id"])
            save_data(self.data)
            self._refresh_groups_tree()

    def _remove_member(self):
        item = self.groups_tree.currentItem()
        if not item:
            return
        d = item.data(0, Qt.UserRole)
        if not d or d[0] != "member":
            return
        _, gid, mid = d
        g = self._find(self.data["groups"], gid)
        g["members"].remove(mid)
        save_data(self.data)
        self._refresh_groups_tree()

    # ── Relations tab ────────────────────────────────────────────────────────

    def _build_relations_tab(self):
        w = QWidget()
        vbox = QVBoxLayout(w)

        hbar = QHBoxLayout()
        btn_add = QPushButton("＋ Додати зв'язок")
        btn_del = QPushButton("✕ Видалити зв'язок")
        for b in (btn_add, btn_del):
            b.setFixedHeight(30)
            hbar.addWidget(b)
        hbar.addStretch()
        vbox.addLayout(hbar)

        self.rel_list = QListWidget()
        self.rel_list.setAlternatingRowColors(True)
        vbox.addWidget(self.rel_list)

        btn_add.clicked.connect(self._add_relation)
        btn_del.clicked.connect(self._delete_relation)

        self._refresh_relations_list()
        return w

    def _refresh_relations_list(self):
        self.rel_list.clear()
        for rel in self.data["relations"]:
            label = rel.get("label", "")
            text = f"{self._entity_label(rel['from'])}  →  {self._entity_label(rel['to'])}"
            if label:
                text += f"  ({label})"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, rel["id"])
            self.rel_list.addItem(item)

    def _add_relation(self):
        if not self.data["requisites"] and not self.data["groups"]:
            QMessageBox.information(self, "Інфо", "Спочатку створіть реквізити або групи.")
            return
        dlg = RelationDialog(self, self.data)
        if dlg.exec() == QDialog.Accepted:
            frm, to, label = dlg.values()
            if frm == to:
                QMessageBox.warning(self, "Помилка", "Не можна пов'язати елемент із самим собою.")
                return
            self.data["relations"].append({
                "id": self._next_id(self.data["relations"]),
                "from": frm, "to": to, "label": label
            })
            save_data(self.data)
            self._refresh_relations_list()

    def _delete_relation(self):
        item = self.rel_list.currentItem()
        if not item:
            return
        uid = item.data(Qt.UserRole)
        self.data["relations"] = [r for r in self.data["relations"] if r["id"] != uid]
        save_data(self.data)
        self._refresh_relations_list()


# ─── Shared panel widget ─────────────────────────────────────────────────────

class ClickablePanelWidget(QFrame):
    def __init__(self, title: str, callback=None, parent=None):
        super().__init__(parent)
        self.callback = callback
        self.setFrameShape(QFrame.Box)
        self.setLineWidth(2)
        self.setCursor(Qt.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        label = QLabel(title)
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.callback:
            self.callback()
        super().mousePressEvent(event)


# ─── TZK Settings window (unchanged) ────────────────────────────────────────

class TZKSettingsWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
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

        left_panel  = ClickablePanelWidget("Додати новий\nТЗК")
        right_panel = ClickablePanelWidget("Перегляд та редагування\nвхідних ТТХ ТЗК")

        h_layout.addWidget(left_panel)
        h_layout.addWidget(right_panel)


# ─── Main Menu ───────────────────────────────────────────────────────────────

class MainMenu(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Головне меню")
        self.resize(960, 600)

        self.tzk_window = None
        self.req_window = None

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

        h_layout.addWidget(ClickablePanelWidget("Томатний\nконвеєр"))
        h_layout.addWidget(ClickablePanelWidget(
            "Налаштування\nпараметрів ТЗК",
            callback=self.open_tzk_settings
        ))
        h_layout.addWidget(ClickablePanelWidget(
            "Спільні\nреквізити",
            callback=self.open_requisites
        ))

    def open_tzk_settings(self):
        if self.tzk_window is None:
            self.tzk_window = TZKSettingsWindow()
        self.tzk_window.show()
        self.tzk_window.raise_()
        self.tzk_window.activateWindow()

    def open_requisites(self):
        if self.req_window is None:
            self.req_window = RequisitesWindow()
        self.req_window.show()
        self.req_window.raise_()
        self.req_window.activateWindow()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainMenu()
    window.show()
    sys.exit(app.exec())
