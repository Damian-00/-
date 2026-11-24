"""Модуль анализа личных финансов: сводки, динамика, прогноз и рекомендации."""
import calendar
from collections import OrderedDict
from datetime import date, timedelta

from sqlalchemy import func

from models import db, Transaction, Category, Budget

# Переводы между своими счетами хранятся как пара операций без категории
NOT_TRANSFER = Transaction.category_id.isnot(None)

MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]


def month_bounds(d: date) -> tuple[date, date]:
    """Первый и последний день месяца."""
    last = calendar.monthrange(d.year, d.month)[1]
    return d.replace(day=1), d.replace(day=last)


def shift_month(d: date, n: int) -> date:
    """Сдвиг даты (первого числа) на n месяцев."""
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_range(period: str, start: str | None = None, end: str | None = None) -> tuple[date, date]:
    """Преобразует название периода в диапазон дат."""
    today = date.today()
    if period == "week":
        return today - timedelta(days=today.weekday()), today
    if period == "prev_month":
        first = shift_month(today.replace(day=1), -1)
        return month_bounds(first)
    if period == "3months":
        return shift_month(today.replace(day=1), -2), today
    if period == "year":
        return date(today.year, 1, 1), today
    if period == "custom" and start and end:
        try:
            return date.fromisoformat(start), date.fromisoformat(end)
        except ValueError:
            pass
    return month_bounds(today)  # по умолчанию — текущий месяц


def _sum(user_id: int, type_: str, start: date, end: date) -> float:
    return db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.user_id == user_id, Transaction.type == type_, NOT_TRANSFER,
        Transaction.date >= start, Transaction.date <= end).scalar()


def summary(user_id: int, start: date, end: date) -> dict:
    """Доходы, расходы, сальдо и норма сбережений за период."""
    income = _sum(user_id, "income", start, end)
    expense = _sum(user_id, "expense", start, end)
    days = max((min(end, date.today()) - start).days + 1, 1)
    return {
        "income": round(income, 2),
        "expense": round(expense, 2),
        "balance": round(income - expense, 2),
        "savings_rate": round((income - expense) / income * 100, 1) if income else 0.0,
        "avg_daily_expense": round(expense / days, 2),
    }


def by_category(user_id: int, type_: str, start: date, end: date) -> list[dict]:
    """Суммы по категориям (для круговой диаграммы и таблицы)."""
    rows = db.session.query(
        Category.name, Category.color, func.sum(Transaction.amount), func.count(Transaction.id)
    ).join(Category, Transaction.category_id == Category.id).filter(
        Transaction.user_id == user_id, Transaction.type == type_,
        Transaction.date >= start, Transaction.date <= end,
    ).group_by(Category.id).order_by(func.sum(Transaction.amount).desc()).all()
    total = sum(r[2] for r in rows) or 1
    return [{"name": n, "color": c, "amount": round(s, 2), "count": cnt,
             "share": round(s / total * 100, 1)} for n, c, s, cnt in rows]


def monthly_dynamics(user_id: int, months: int = 12) -> dict:
    """Доходы и расходы по месяцам за последние N месяцев."""
    first = shift_month(date.today().replace(day=1), -(months - 1))
    data = OrderedDict()
    for i in range(months):
        m = shift_month(first, i)
        data[(m.year, m.month)] = {"income": 0.0, "expense": 0.0}

    ym = func.strftime("%Y-%m", Transaction.date)
    rows = db.session.query(ym, Transaction.type, func.sum(Transaction.amount)).filter(
        Transaction.user_id == user_id, Transaction.date >= first, NOT_TRANSFER
    ).group_by(ym, Transaction.type).all()
    for key, type_, total in rows:
        y, m = map(int, key.split("-"))
        if (y, m) in data:
            data[(y, m)][type_] = round(total, 2)

    return {
        "labels": [f"{MONTHS_RU[m - 1]} {str(y)[2:]}" for (y, m) in data],
        "income": [v["income"] for v in data.values()],
        "expense": [v["expense"] for v in data.values()],
        "balance": [round(v["income"] - v["expense"], 2) for v in data.values()],
    }


def daily_expenses(user_id: int, start: date, end: date) -> dict:
    """Расходы по дням периода."""
    rows = dict(db.session.query(Transaction.date, func.sum(Transaction.amount)).filter(
        Transaction.user_id == user_id, Transaction.type == "expense", NOT_TRANSFER,
        Transaction.date >= start, Transaction.date <= end,
    ).group_by(Transaction.date).all())
    labels, values = [], []
    d = start
    while d <= end and (end - start).days <= 92:
        labels.append(d.strftime("%d.%m"))
        values.append(round(rows.get(d, 0), 2))
        d += timedelta(days=1)
    return {"labels": labels, "values": values}


def weekday_expenses(user_id: int, start: date, end: date) -> list[float]:
    """Средние расходы по дням недели (Пн..Вс)."""
    rows = db.session.query(Transaction.date, Transaction.amount).filter(
        Transaction.user_id == user_id, Transaction.type == "expense", NOT_TRANSFER,
        Transaction.date >= start, Transaction.date <= end).all()
    totals = [0.0] * 7
    for d, a in rows:
        totals[d.weekday()] += a
    weeks = max(((end - start).days + 1) / 7, 1)
    return [round(t / weeks, 2) for t in totals]


def month_forecast(user_id: int) -> dict:
    """Прогноз расходов до конца текущего месяца.

    Методика: прогноз = уже потрачено + средний дневной темп текущих расходов × оставшиеся дни.
    Темп считается по последним 90 дням без крупных разовых платежей (≥ 20 % среднемесячных
    расходов: аренда, крупные покупки), чтобы они не «размазывались» на каждый день.
    Если истории нет, используется темп текущего месяца.
    """
    today = date.today()
    start, end = month_bounds(today)
    spent = _sum(user_id, "expense", start, today)
    income = _sum(user_id, "income", start, end)
    days_left = end.day - today.day

    hist_start = today - timedelta(days=90)
    hist = [a for (a,) in db.session.query(Transaction.amount).filter(
        Transaction.user_id == user_id, Transaction.type == "expense", NOT_TRANSFER,
        Transaction.date >= hist_start, Transaction.date < start)]
    hist_days = (start - hist_start).days
    if hist:
        big = sum(hist) / (hist_days / 30) * 0.2
        avg = sum(a for a in hist if a < big) / hist_days
    else:
        avg = spent / today.day
    projected = spent + avg * days_left
    return {
        "spent": round(spent, 2),
        "projected": round(projected, 2),
        "avg_daily": round(avg, 2),
        "days_left": days_left,
        "income": round(income, 2),
        "projected_balance": round(income - projected, 2),
    }


def recommendations(user_id: int) -> list[dict]:
    """Простые правила-рекомендации на основе данных пользователя."""
    tips = []
    today = date.today()
    cur_start, cur_end = month_bounds(today)
    prev_start, prev_end = month_bounds(shift_month(cur_start, -1))
    cur = summary(user_id, cur_start, today)
    prev = summary(user_id, prev_start, prev_end)

    # 1. Норма сбережений
    three = summary(user_id, shift_month(cur_start, -3), prev_end)
    if three["income"]:
        if three["savings_rate"] < 10:
            tips.append({"level": "danger", "icon": "exclamation-triangle",
                         "text": f"За последние 3 месяца вы откладывали лишь {three['savings_rate']}% дохода. "
                                 "Финансисты рекомендуют сберегать не менее 10–20%."})
        elif three["savings_rate"] >= 20:
            tips.append({"level": "success", "icon": "trophy",
                         "text": f"Отлично! Норма сбережений за 3 месяца — {three['savings_rate']}%. "
                                 "Это соответствует правилу 50/30/20."})
        else:
            tips.append({"level": "info", "icon": "info-circle",
                         "text": f"Норма сбережений — {three['savings_rate']}%. Попробуйте довести её до 20%."})

    # 2. Сравнение категорий с прошлым месяцем (пропорционально прошедшим дням)
    ratio = today.day / cur_end.day
    prev_cats = {c["name"]: c["amount"] for c in by_category(user_id, "expense", prev_start, prev_end)}
    growing = []
    if today.day >= 7:  # в первые дни месяца сравнение темпа малоинформативно
        for c in by_category(user_id, "expense", cur_start, today):
            p = prev_cats.get(c["name"])
            if p and c["amount"] > p * ratio * 1.3 and c["amount"] > 3000:
                growing.append((round((c["amount"] / (p * ratio) - 1) * 100), c["name"]))
    for growth, name in sorted(growing, reverse=True)[:3]:
        tips.append({"level": "warning", "icon": "graph-up-arrow",
                     "text": f"Расходы на «{name}» идут на {growth}% выше темпа прошлого месяца."})

    # 3. Превышение бюджетов
    month_key = today.strftime("%Y-%m")
    for b in Budget.query.filter_by(user_id=user_id, month=month_key).all():
        pct = b.percent()
        if pct >= 100:
            tips.append({"level": "danger", "icon": "x-octagon",
                         "text": f"Бюджет «{b.category.name}» превышен ({pct}%)."})
        elif pct >= 80:
            tips.append({"level": "warning", "icon": "exclamation-circle",
                         "text": f"Бюджет «{b.category.name}» израсходован на {pct}%."})

    # 4. Прогноз
    fc = month_forecast(user_id)
    if fc["income"] and fc["projected"] > fc["income"]:
        tips.append({"level": "danger", "icon": "calendar-x",
                     "text": f"При текущем темпе расходы за месяц составят ~{fc['projected']:,.0f} — "
                             f"больше, чем уже полученные доходы ({fc['income']:,.0f}).".replace(",", " ")})

    # 5. Крупнейшая категория
    top = by_category(user_id, "expense", prev_start, prev_end)
    if top and top[0]["share"] > 40:
        tips.append({"level": "info", "icon": "pie-chart",
                     "text": f"В прошлом месяце «{top[0]['name']}» заняла {top[0]['share']}% всех расходов."})

    if not tips:
        tips.append({"level": "secondary", "icon": "lightbulb",
                     "text": "Добавьте больше операций, чтобы получить персональные рекомендации."})
    return tips
