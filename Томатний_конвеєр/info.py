from PySide6.QtWidgets import QMessageBox
from datetime import datetime, timedelta


def show_deviation_info(tab):
    early = -1
    late = 7
    table = getattr(tab, "table", None)

    if table is None:
        QMessageBox.warning(tab, "Помилка", "Таблиця не знайдена в вкладці")
        return

    result = []

    for row in range(table.rowCount()):
        field_item = table.item(row, 0)
        ripening_item = table.item(row, 6)

        if not field_item or not ripening_item:
            continue

        try:
            ripening = datetime.strptime(ripening_item.text(), "%Y-%m-%d")

            start = ripening + timedelta(days=early)
            end = ripening + timedelta(days=late)

            start_item = table.item(row, 7)
            end_item = table.item(row, 8)

            deviation = 0

            if start_item and start_item.text():
                actual = datetime.strptime(start_item.text(), "%Y-%m-%d")
                if actual < start:
                    deviation = max(deviation, (start - actual).days)
                elif actual > end:
                    deviation = max(deviation, (actual - end).days)

            if end_item and end_item.text():
                actual = datetime.strptime(end_item.text(), "%Y-%m-%d")
                if actual < start:
                    deviation = max(deviation, (start - actual).days)
                elif actual > end:
                    deviation = max(deviation, (actual - end).days)

            result.append((deviation, field_item.text()))

        except:
            continue

    # сортування від найбільшого до найменшого
    result.sort(key=lambda x: x[0], reverse=True)

    text = "\n".join(
        f"{i+1}. Поле {field}: {dev} дн."
        for i, (dev, field) in enumerate(result)
    )

    QMessageBox.information(
        tab,
        "Відхилення полів",
        text
    )
