"""
season_optimizer.py
===================
Сезонний оптимізатор ДЗВ (Дозволеного Збирального Вікна).

Математична модель
------------------
Змінна рішення:
    schedule[d][f]  — тонн поля f, зібраних у день d

Цільова функція (мінімізація):
    TOTAL_COST =
        FACTORY_ERROR_WEIGHT  * factory_error        (пріоритет 1)
      + UNHARVESTED_WEIGHT    * unharvested_tons      (пріоритет 2)
      + DZV_WEIGHT            * dzv_penalty           (пріоритет 3)
      + SPLIT_WEIGHT          * fragmentation_penalty (пріоритет 4)
      + SWITCH_WEIGHT         * switch_penalty        (пріоритет 5)

Алгоритм:
    1. build_initial_schedule()  — EDF-стартовий план (Earliest Deadline First)
    2. optimize()                — Local Search до сходження або max_iterations
    3. get_schedule()            — повертає (schedule, remaining) у форматі
                                   [[float]*n_fields]*n_days,
                                   сумісному з _render_table / _fill_main_table_dates
"""

from __future__ import annotations

import copy
import logging
import math
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# Ваги цільової функції
# ──────────────────────────────────────────────────────────────────────────────
FACTORY_ERROR_WEIGHT = 1_000_000   # відхилення від потреби заводу (квадрат)
UNHARVESTED_WEIGHT   = 500_000     # незібраний вал (тонна)
DZV_WEIGHT           = 1_000       # ДЗВ-штраф × тонни
SPLIT_WEIGHT         = 50          # фрагментація збирання поля (розрив-день)
SWITCH_WEIGHT        = 10          # перемикання між полями в відділенні

EARLY_WEIGHT = 100     # штраф за ранній збір (за добу поза вікном)
LATE_WEIGHT  = 1_000   # штраф за запізнілий збір (за добу поза вікном)

MIN_MOVE     = 0.5     # мінімальна порція переміщення (т) при Local Search
MOVE_FRACTION = 0.15   # частка денного обсягу при одному кроці зміщення

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Клас оптимізатора
# ──────────────────────────────────────────────────────────────────────────────

class SeasonDZVOptimizer:
    """
    Параметри конструктора
    ----------------------
    fields : list[dict]
        Обов'язкові ключі:
            "gross"     : float       — валовий збір (т)
            "division"  : str         — назва відділення
            "ripening"  : date | None — прогнозована дата дозрівання
        Опційно:
            "name"      : str
    days : list[dict]
        Обов'язкові ключі:
            "date"      : date
            "demand"    : float       — добова потреба заводу (т)
    div_caps : dict[str, float]
        Добова продуктивність кожного відділення (т/день).
    early_days : int
        Дозволений ранній старт відносно дозрівання (днів до).
    late_days : int
        Дозволений пізній фініш відносно дозрівання (днів після).
    div_start_dates : dict[str, date] | None
        Мінімальна дата старту збирання для кожного відділення.
    max_iterations : int
        Максимальна кількість ітерацій Local Search.
    """

    def __init__(
        self,
        fields: List[dict],
        days: List[dict],
        div_caps: Dict[str, float],
        early_days: int = 1,
        late_days: int = 7,
        div_start_dates: Optional[Dict[str, date]] = None,
        max_iterations: int = 500,
        # Зворотна сумісність зі старим інтерфейсом
        factory_plan=None,
    ):
        self.fields = fields
        self.days = days
        self.div_caps = dict(div_caps)
        self.early_days = early_days
        self.late_days = late_days
        self.div_start_dates = div_start_dates or {}
        self.max_iterations = max_iterations

        self.n_fields = len(fields)
        self.n_days   = len(days)

        # ── Основна матриця рішення: schedule[d][f] ──
        self.schedule: List[List[float]] = [
            [0.0] * self.n_fields for _ in range(self.n_days)
        ]

        # ── Залишок врожаю (для зворотної сумісності) ──
        self.remaining: List[float] = [f["gross"] for f in fields]

        # ── Попередньо обчислені константи ──
        self._window_start: List[Optional[date]] = []
        self._window_end:   List[Optional[date]] = []
        self._deadlines:    List[Optional[date]] = []
        self._div_index:    Dict[str, List[int]] = {}   # відділення → [field_idx]

        for fi, f in enumerate(fields):
            rip = f.get("ripening")
            if rip:
                self._window_start.append(rip - timedelta(days=early_days))
                self._window_end.append(rip + timedelta(days=late_days))
                self._deadlines.append(rip + timedelta(days=late_days))
            else:
                self._window_start.append(None)
                self._window_end.append(None)
                self._deadlines.append(None)

            div = f.get("division") or "__no_div__"
            self._div_index.setdefault(div, []).append(fi)

    # ══════════════════════════════════════════════════════════════════════════
    # PUBLIC API
    # ══════════════════════════════════════════════════════════════════════════

    def build_initial_schedule(self) -> None:
        """
        Будує стартовий план методом EDF (Earliest Deadline First).
        Для кожного дня і кожного відділення: вся квота іде на поле
        з найближчим дедлайном до його закриття, потім залишок —
        на наступне поле.
        """
        remaining = [f["gross"] for f in self.fields]

        for d_idx, day in enumerate(self.days):
            day_date = day["date"]
            demand   = float(day["demand"])

            # Збираємо активні відділення та їх квоти
            div_active: Dict[str, float] = {}
            for div, f_indices in self._div_index.items():
                cap = self.div_caps.get(div, 0.0)
                if cap <= 0:
                    continue
                start_date = self.div_start_dates.get(div)
                if start_date and day_date < start_date:
                    continue
                has_rem = any(remaining[fi] > 1e-6 for fi in f_indices)
                if has_rem:
                    div_active[div] = cap

            if not div_active:
                continue

            total_cap = sum(div_active.values())
            # Масштабуємо під потребу заводу
            scale = min(1.0, demand / total_cap) if total_cap > 1e-9 else 1.0

            for div, cap in div_active.items():
                quota = cap * scale
                f_indices = self._div_index[div]

                # EDF: сортуємо за дедлайном
                candidates = []
                for fi in f_indices:
                    if remaining[fi] <= 1e-6:
                        continue
                    rip = self.fields[fi].get("ripening")
                    # Поле доступне, якщо дозріло або скоро дозріє
                    if rip is None or day_date >= rip - timedelta(days=self.early_days):
                        dl = self._deadlines[fi]
                        sort_key = (dl - day_date).days if dl else 99999
                        candidates.append((sort_key, fi))
                candidates.sort()

                for _, fi in candidates:
                    if quota <= 1e-6:
                        break
                    take = min(remaining[fi], quota)
                    self.schedule[d_idx][fi] += take
                    remaining[fi]            -= take
                    quota                    -= take

        # Синхронізуємо self.remaining
        self.remaining = remaining[:]

    def calculate_penalty(self) -> float:
        """
        Повна цільова функція (менше — краще):

            TOTAL_COST =
                FACTORY_ERROR_WEIGHT  * Σ(supply_d - demand_d)²
              + UNHARVESTED_WEIGHT    * незібрано_т
              + DZV_WEIGHT            * dzv_штраф
              + SPLIT_WEIGHT          * розриви_збирання
              + SWITCH_WEIGHT         * перемикання_техніки
        """
        return (
            FACTORY_ERROR_WEIGHT * self._factory_error()
            + UNHARVESTED_WEIGHT * self._unharvested_tons()
            + DZV_WEIGHT         * self._dzv_penalty_total()
            + SPLIT_WEIGHT       * self._fragmentation_penalty()
            + SWITCH_WEIGHT      * self._switch_penalty()
        )

    # Псевдонім для зворотної сумісності зі старим кодом
    def calculate_objective(self) -> float:
        return self.calculate_penalty()

    def optimize(self, max_iterations: Optional[int] = None) -> None:
        """
        Local Search по матриці schedule[d][f].

        На кожній ітерації:
          - перебираємо всі поля;
          - для кожного поля знаходимо день з максимальним ДЗВ-штрафом;
          - намагаємось перенести частину тонн у день ближче до вікна;
          - приймаємо зміну, якщо TOTAL_COST зменшився.

        Зупинка: немає покращень або досягнуто max_iterations.
        """
        iterations = max_iterations if max_iterations is not None else self.max_iterations
        best_cost  = self.calculate_penalty()

        for iteration in range(iterations):
            improved = False

            for fi in range(self.n_fields):
                # Дні, де поле збирається
                active = [d for d in range(self.n_days)
                          if self.schedule[d][fi] > MIN_MOVE]
                if not active:
                    continue

                # День з найбільшим ДЗВ-штрафом
                worst_d = max(active, key=lambda d: self._cell_dzv_penalty(fi, d))
                if self._cell_dzv_penalty(fi, worst_d) < 1e-9:
                    continue

                day_date = self.days[worst_d]["date"]
                ws = self._window_start[fi]
                we = self._window_end[fi]
                if ws is None or we is None:
                    continue

                # Визначаємо напрямок зміщення
                if day_date < ws:
                    # Занадто рано → зсуваємо вперед, у вікно або ближче до нього
                    target_days = [
                        d for d in range(worst_d + 1, self.n_days)
                        if self.days[d]["date"] >= ws
                    ]
                elif day_date > we:
                    # Занадто пізно → зсуваємо назад, у вікно або ближче до нього
                    target_days = [
                        d for d in range(0, worst_d)
                        if self.days[d]["date"] <= we
                    ][::-1]
                else:
                    continue

                if not target_days:
                    continue

                target_d = target_days[0]

                # Перевіряємо обмеження потужності відділення в цільовий день
                div = self.fields[fi].get("division") or "__no_div__"
                div_cap = self.div_caps.get(div, math.inf)
                current_load = sum(
                    self.schedule[target_d][fj]
                    for fj in self._div_index.get(div, [])
                )
                allowed = max(0.0, div_cap - current_load)
                if allowed < MIN_MOVE:
                    continue

                move = max(MIN_MOVE, self.schedule[worst_d][fi] * MOVE_FRACTION)
                move = min(move, self.schedule[worst_d][fi], allowed)

                # Пробуємо перемістити
                self.schedule[worst_d][fi]  -= move
                self.schedule[target_d][fi] += move
                new_cost = self.calculate_penalty()

                if new_cost < best_cost - 1e-6:
                    best_cost = new_cost
                    improved  = True
                else:
                    # Відкат
                    self.schedule[worst_d][fi]  += move
                    self.schedule[target_d][fi] -= move

            if not improved:
                log.debug("[SeasonDZV] Сходження на ітерації %d", iteration + 1)
                break

        # Синхронізуємо remaining після оптимізації
        for fi in range(self.n_fields):
            harvested = sum(self.schedule[d][fi] for d in range(self.n_days))
            self.remaining[fi] = max(0.0, self.fields[fi]["gross"] - harvested)

    def validate_constraints(self) -> dict:
        """
        Перевіряє всі обмеження та повертає звіт у вигляді словника.
        Корисно для логування та відображення в UI.
        """
        total_gross = sum(f["gross"] for f in self.fields)
        total_sched = sum(
            self.schedule[d][f]
            for d in range(self.n_days)
            for f in range(self.n_fields)
        )

        factory_errors = []
        for d_idx, day in enumerate(self.days):
            supply = sum(self.schedule[d_idx][f] for f in range(self.n_fields))
            err    = abs(supply - day["demand"])
            if err > 1.0:
                factory_errors.append({
                    "day":    d_idx + 1,
                    "date":   day["date"],
                    "demand": day["demand"],
                    "supply": supply,
                    "error":  err,
                })

        return {
            "total_gross":     total_gross,
            "total_scheduled": total_sched,
            "unharvested":     max(0.0, total_gross - total_sched),
            "factory_errors":  factory_errors,
            "dzv_violations":  self._count_dzv_violations(),
            "total_penalty":   self.calculate_penalty(),
        }

    def get_schedule(self) -> Tuple[List[List[float]], List[float]]:
        """
        Повертає (schedule, remaining) у форматі, повністю сумісному з:
            _render_table(fields, days, schedule, remaining)
            _fill_main_table_dates(fields, days, schedule)

            schedule[d_idx][f_idx] — тонн поля f у день d
            remaining[f_idx]       — незібраний залишок поля f
        """
        remaining = [
            max(0.0, self.fields[fi]["gross"] - sum(
                self.schedule[d][fi] for d in range(self.n_days)
            ))
            for fi in range(self.n_fields)
        ]
        return self.schedule, remaining

    # ══════════════════════════════════════════════════════════════════════════
    # КОМПОНЕНТИ ЦІЛЬОВОЇ ФУНКЦІЇ
    # ══════════════════════════════════════════════════════════════════════════

    def _factory_error(self) -> float:
        """Сума квадратів відхилень від добової потреби заводу."""
        total = 0.0
        for d_idx, day in enumerate(self.days):
            supply = sum(self.schedule[d_idx][f] for f in range(self.n_fields))
            diff   = supply - day["demand"]
            total += diff * diff
        return total

    def _unharvested_tons(self) -> float:
        """Сумарний незібраний вал (т)."""
        total = 0.0
        for fi in range(self.n_fields):
            harvested = sum(self.schedule[d][fi] for d in range(self.n_days))
            leftover  = self.fields[fi]["gross"] - harvested
            if leftover > 1e-6:
                total += leftover
        return total

    def _dzv_penalty_total(self) -> float:
        """
        Зважений ДЗВ-штраф: для кожної клітинки (поле, день) множимо
        питомий штраф (за тонну) на кількість тонн у цій клітинці.
        """
        total = 0.0
        for fi in range(self.n_fields):
            ws = self._window_start[fi]
            we = self._window_end[fi]
            if ws is None or we is None:
                continue
            for d in range(self.n_days):
                amount = self.schedule[d][fi]
                if amount < 1e-6:
                    continue
                total += self._cell_dzv_penalty(fi, d) * amount
        return total

    def _cell_dzv_penalty(self, fi: int, d: int) -> float:
        """
        Питомий ДЗВ-штраф (за тонну) для поля fi у день d.
          Ранній збір: EARLY_WEIGHT × кількість днів до початку вікна.
          Пізній збір: LATE_WEIGHT  × кількість днів після кінця вікна.
          В межах вікна: 0.
        """
        ws = self._window_start[fi]
        we = self._window_end[fi]
        if ws is None or we is None:
            return 0.0
        day_date = self.days[d]["date"]
        if day_date < ws:
            return (ws - day_date).days * EARLY_WEIGHT
        if day_date > we:
            return (day_date - we).days * LATE_WEIGHT
        return 0.0

    def _fragmentation_penalty(self) -> float:
        """
        Штраф за переривання збирання поля:
        кожен нульовий день між першим і останнім ненульовим днем
        збирання поля збільшує штраф на 1.
        """
        total = 0.0
        for fi in range(self.n_fields):
            first = next(
                (d for d in range(self.n_days) if self.schedule[d][fi] > 1e-6),
                None
            )
            last = next(
                (d for d in range(self.n_days - 1, -1, -1)
                 if self.schedule[d][fi] > 1e-6),
                None
            )
            if first is None or first == last:
                continue
            gaps = sum(
                1 for d in range(first + 1, last)
                if self.schedule[d][fi] < 1e-6
            )
            total += gaps
        return total

    def _switch_penalty(self) -> float:
        """
        Штраф за перемикання техніки між полями в одному відділенні:
        рахуємо, скільки полів змінили активність (0→+, +→0) між сусідніми днями.
        """
        total = 0.0
        for div, f_indices in self._div_index.items():
            if len(f_indices) <= 1:
                continue
            for d in range(self.n_days - 1):
                switches = sum(
                    1
                    for fi in f_indices
                    if (self.schedule[d][fi] > 1e-6) != (self.schedule[d + 1][fi] > 1e-6)
                )
                total += switches
        return total

    # ══════════════════════════════════════════════════════════════════════════
    # ЛОГУВАННЯ
    # ══════════════════════════════════════════════════════════════════════════

    def _count_dzv_violations(self) -> int:
        """Кількість пар (поле, день) зі збиранням поза ДЗВ."""
        count = 0
        for fi in range(self.n_fields):
            for d in range(self.n_days):
                if self.schedule[d][fi] > 1e-6 and self._cell_dzv_penalty(fi, d) > 0:
                    count += 1
        return count

    def log_before(self) -> float:
        """Логує стан ДО оптимізації. Повертає поточний штраф."""
        v = self.validate_constraints()
        log.info(
            "[SeasonDZV] ДО оптимізації | штраф=%.0f | порушень ДЗВ=%d | "
            "незібрано=%.1f т | помилок заводу=%d",
            v["total_penalty"],
            v["dzv_violations"],
            v["unharvested"],
            len(v["factory_errors"]),
        )
        return v["total_penalty"]

    def log_after(self, before_penalty: float) -> None:
        """Логує стан ПІСЛЯ оптимізації з відсотком покращення."""
        v = self.validate_constraints()
        improvement = (
            (before_penalty - v["total_penalty"]) / before_penalty * 100
            if before_penalty > 1e-9 else 0.0
        )
        log.info(
            "[SeasonDZV] ПІСЛЯ оптимізації | штраф=%.0f | порушень ДЗВ=%d | "
            "незібрано=%.1f т | покращення=%.1f%%",
            v["total_penalty"],
            v["dzv_violations"],
            v["unharvested"],
            improvement,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # ЗВОРОТНА СУМІСНІСТЬ зі старим інтерфейсом season_optimizer.py
    # ══════════════════════════════════════════════════════════════════════════

    def field_priority(self, field_index: int, current_date) -> float:
        """Пріоритет поля для EDF (зворотна сумісність)."""
        if self.remaining[field_index] <= 0:
            return -999.0
        rip = self.fields[field_index].get("ripening")
        if not rip:
            return -999.0
        deadline  = rip + timedelta(days=self.late_days)
        days_left = (deadline - current_date).days
        urgency   = 10000.0 if days_left <= 0 else 1.0 / days_left
        return self.remaining[field_index] * urgency

    def dzv_penalty(self, field_index: int, harvest_date) -> float:
        """Штраф ДЗВ для конкретної дати (зворотна сумісність)."""
        rip = self.fields[field_index].get("ripening")
        if not rip:
            return 0.0
        ws = rip - timedelta(days=self.early_days)
        we = rip + timedelta(days=self.late_days)
        if harvest_date < ws:
            return (ws - harvest_date).days * EARLY_WEIGHT
        if harvest_date > we:
            return (harvest_date - we).days * LATE_WEIGHT
        return 0.0

    def allocate_day(self, day_index: int, current_date, factory_need: float) -> None:
        """EDF-розподіл одного дня (зворотна сумісність)."""
        candidates = [
            (i, self.field_priority(i, current_date))
            for i in range(self.n_fields)
            if self.field_priority(i, current_date) > 0
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)

        capacity     = sum(self.div_caps.values())
        daily_volume = min(capacity, factory_need)

        for field_index, _ in candidates:
            if daily_volume <= 0:
                break
            amount = min(self.remaining[field_index], daily_volume)
            self.schedule[day_index][field_index] += amount
            self.remaining[field_index]           -= amount
            daily_volume                          -= amount

    def improve_plan(self) -> None:
        """Покращення плану (зворотна сумісність, делегує до optimize())."""
        self.optimize()
