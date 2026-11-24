"""Веб-приложение для учёта, анализа и управления личными финансами.

Запуск:  python app.py   (или flask --app app run)
Демо-данные:  flask --app app seed
"""
import csv
import io
import os
import random
import secrets
from datetime import date, timedelta

from flask import (Flask, render_template, redirect, url_for, request, flash,
                   session, abort, Response)
from flask_login import (LoginManager, login_user, logout_user, login_required,
                         current_user)
from sqlalchemy import or_

import analytics as an
from models import (db, User, Account, Category, Transaction, Budget, Goal,
                    create_defaults)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "finance.db"))
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Войдите, чтобы продолжить."
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


# ---------------------------------------------------------------- безопасность
def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def csrf_protect():
    if request.method == "POST":
        if request.form.get("_csrf") != session.get("_csrf"):
            abort(400, "Неверный CSRF-токен")


@app.context_processor
def inject_globals():
    return {"csrf_token": csrf_token, "today": date.today()}


@app.template_filter("money")
def money(value):
    """Форматирование суммы: 12 345,67 ₽"""
    cur = current_user.currency if current_user.is_authenticated else "₽"
    s = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} {cur}"


def own_or_404(model, obj_id):
    """Получить объект текущего пользователя или вернуть 404."""
    obj = db.session.get(model, obj_id)
    if obj is None or obj.user_id != current_user.id:
        abort(404)
    return obj


def parse_amount(raw: str) -> float | None:
    try:
        value = round(float(raw.replace(",", ".").replace(" ", "")), 2)
        return value if value > 0 else None
    except (ValueError, AttributeError):
        return None


def parse_date(raw: str, default=None):
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- авторизация
@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        errors = []
        if len(username) < 3:
            errors.append("Логин должен содержать не менее 3 символов.")
        if "@" not in email:
            errors.append("Введите корректный e-mail.")
        if len(password) < 6:
            errors.append("Пароль должен содержать не менее 6 символов.")
        if password != password2:
            errors.append("Пароли не совпадают.")
        if User.query.filter((User.username == username) | (User.email == email)).first():
            errors.append("Пользователь с таким логином или e-mail уже существует.")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("register.html", form=request.form)
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        create_defaults(user)
        db.session.commit()
        login_user(user)
        flash("Регистрация прошла успешно! Добро пожаловать.", "success")
        return redirect(url_for("dashboard"))
    return render_template("register.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        login_ = request.form.get("username", "").strip()
        user = User.query.filter((User.username == login_) | (User.email == login_.lower())).first()
        if user and user.check_password(request.form.get("password", "")):
            login_user(user, remember=bool(request.form.get("remember")))
            next_url = request.args.get("next")
            if not next_url or not next_url.startswith("/"):
                next_url = url_for("dashboard")
            return redirect(next_url)
        flash("Неверный логин или пароль.", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Вы вышли из системы.", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------- главная панель
@app.route("/dashboard")
@login_required
def dashboard():
    uid = current_user.id
    start, end = an.month_bounds(date.today())
    month_key = date.today().strftime("%Y-%m")
    return render_template(
        "dashboard.html",
        summary=an.summary(uid, start, end),
        accounts=current_user.accounts,
        total=current_user.total_balance(),
        recent=Transaction.query.filter_by(user_id=uid)
        .order_by(Transaction.date.desc(), Transaction.id.desc()).limit(8).all(),
        expense_cats=an.by_category(uid, "expense", start, end),
        dynamics=an.monthly_dynamics(uid, 6),
        budgets=Budget.query.filter_by(user_id=uid, month=month_key).all(),
        goals=Goal.query.filter_by(user_id=uid).limit(3).all(),
        forecast=an.month_forecast(uid),
    )


# ---------------------------------------------------------------- операции
def filtered_transactions():
    """Запрос операций с учётом фильтров из строки запроса."""
    q = Transaction.query.filter_by(user_id=current_user.id)
    args = request.args
    period = args.get("period", "month")
    if period != "all":
        start, end = an.period_range(period, args.get("start"), args.get("end"))
        q = q.filter(Transaction.date >= start, Transaction.date <= end)
    if args.get("type") in ("income", "expense"):
        q = q.filter(Transaction.type == args["type"])
    if args.get("account", type=int):
        q = q.filter(Transaction.account_id == args.get("account", type=int))
    if args.get("category", type=int):
        q = q.filter(Transaction.category_id == args.get("category", type=int))
    if args.get("q"):
        q = q.filter(Transaction.description.ilike(f"%{args['q']}%"))
    return q.order_by(Transaction.date.desc(), Transaction.id.desc())


@app.route("/transactions")
@login_required
def transactions():
    page = request.args.get("page", 1, type=int)
    query = filtered_transactions()
    items = query.all()
    income = sum(t.amount for t in items if t.type == "income" and t.category_id)
    expense = sum(t.amount for t in items if t.type == "expense" and t.category_id)
    pagination = query.paginate(page=page, per_page=20, error_out=False)
    return render_template(
        "transactions.html", pagination=pagination, income=income, expense=expense,
        accounts=current_user.accounts,
        categories=Category.query.filter_by(user_id=current_user.id).order_by(Category.type, Category.name).all(),
        args=request.args,
    )


def save_transaction(t: Transaction) -> bool:
    f = request.form
    amount = parse_amount(f.get("amount", ""))
    type_ = f.get("type")
    account = db.session.get(Account, f.get("account_id", type=int) or 0)
    category = db.session.get(Category, f.get("category_id", type=int) or 0)
    if amount is None:
        flash("Введите корректную сумму (больше нуля).", "danger")
        return False
    if type_ not in ("income", "expense"):
        flash("Выберите тип операции.", "danger")
        return False
    if not account or account.user_id != current_user.id:
        flash("Выберите счёт.", "danger")
        return False
    if not category or category.user_id != current_user.id or category.type != type_:
        flash("Выберите категорию, соответствующую типу операции.", "danger")
        return False
    t.user_id = current_user.id
    t.type, t.amount, t.account_id, t.category_id = type_, amount, account.id, category.id
    t.date = parse_date(f.get("date"), date.today())
    t.description = f.get("description", "").strip()[:255]
    return True


@app.route("/transactions/new", methods=["GET", "POST"])
@app.route("/transactions/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def transaction_form(tid=None):
    t = own_or_404(Transaction, tid) if tid else Transaction(type=request.args.get("type", "expense"),
                                                              date=date.today())
    if request.method == "POST":
        if save_transaction(t):
            if not tid:
                db.session.add(t)
            db.session.commit()
            flash("Операция сохранена.", "success")
            return redirect(request.args.get("next") or url_for("transactions"))
    return render_template(
        "transaction_form.html", t=t, accounts=current_user.accounts,
        categories=Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all(),
    )


@app.route("/transactions/<int:tid>/delete", methods=["POST"])
@login_required
def transaction_delete(tid):
    db.session.delete(own_or_404(Transaction, tid))
    db.session.commit()
    flash("Операция удалена.", "info")
    return redirect(request.referrer or url_for("transactions"))


@app.route("/transactions/export")
@login_required
def transactions_export():
    """Экспорт отфильтрованных операций в CSV (открывается в Excel)."""
    buf = io.StringIO()
    buf.write("﻿")  # BOM для корректной кириллицы в Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Дата", "Тип", "Категория", "Счёт", "Сумма", "Описание"])
    for t in filtered_transactions().all():
        w.writerow([t.date.strftime("%d.%m.%Y"), "Доход" if t.type == "income" else "Расход",
                    t.category.name if t.category else "", t.account.name,
                    f"{t.amount:.2f}".replace(".", ","), t.description])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transactions.csv"})


@app.route("/transactions/import", methods=["POST"])
@login_required
def transactions_import():
    """Импорт операций из CSV в формате экспорта."""
    file = request.files.get("file")
    if not file:
        flash("Выберите файл.", "danger")
        return redirect(url_for("transactions"))
    text = file.read().decode("utf-8-sig", errors="ignore")
    reader = csv.reader(io.StringIO(text), delimiter=";")
    next(reader, None)
    cats = {(c.name, c.type): c for c in current_user.categories}
    # пустая категория в файле означает перевод между счетами
    accs = {a.name: a for a in current_user.accounts}
    added = 0
    for row in reader:
        if len(row) < 5:
            continue
        try:
            d = date(*reversed([int(x) for x in row[0].split(".")]))
        except (ValueError, TypeError):
            continue
        type_ = "income" if row[1].strip().lower().startswith("доход") else "expense"
        amount = parse_amount(row[4])
        if amount is None:
            continue
        cat = cats.get((row[2].strip(), type_))
        if not cat and row[2].strip():
            cat = Category(user_id=current_user.id, name=row[2].strip() or "Прочее", type=type_)
            db.session.add(cat)
            cats[(cat.name, type_)] = cat
        acc = accs.get(row[3].strip())
        if not acc:
            acc = Account(user_id=current_user.id, name=row[3].strip() or "Импорт", type="other")
            db.session.add(acc)
            accs[acc.name] = acc
        db.session.flush()
        db.session.add(Transaction(user_id=current_user.id, type=type_, amount=amount, date=d,
                                   account_id=acc.id, category_id=cat.id if cat else None,
                                   description=row[5] if len(row) > 5 else ""))
        added += 1
    db.session.commit()
    flash(f"Импортировано операций: {added}.", "success")
    return redirect(url_for("transactions", period="all"))


# ---------------------------------------------------------------- счета
@app.route("/accounts", methods=["GET", "POST"])
@login_required
def accounts():
    if request.method == "POST":
        aid = request.form.get("id", type=int)
        acc = own_or_404(Account, aid) if aid else Account(user_id=current_user.id)
        name = request.form.get("name", "").strip()
        if not name:
            flash("Введите название счёта.", "danger")
            return redirect(url_for("accounts"))
        acc.name = name[:64]
        acc.type = request.form.get("type") if request.form.get("type") in Account.TYPES else "other"
        try:
            acc.initial_balance = float(request.form.get("initial_balance", "0").replace(",", ".") or 0)
        except ValueError:
            acc.initial_balance = 0
        acc.color = request.form.get("color", "#0d6efd")[:7]
        db.session.add(acc)
        db.session.commit()
        flash("Счёт сохранён.", "success")
        return redirect(url_for("accounts"))
    return render_template("accounts.html", accounts=current_user.accounts, types=Account.TYPES,
                           total=current_user.total_balance())


@app.route("/accounts/<int:aid>/delete", methods=["POST"])
@login_required
def account_delete(aid):
    acc = own_or_404(Account, aid)
    if len(current_user.accounts) <= 1:
        flash("Нельзя удалить единственный счёт.", "warning")
    else:
        db.session.delete(acc)
        db.session.commit()
        flash("Счёт и его операции удалены.", "info")
    return redirect(url_for("accounts"))


@app.route("/accounts/transfer", methods=["POST"])
@login_required
def account_transfer():
    """Перевод между своими счетами (две связанные операции)."""
    src = own_or_404(Account, request.form.get("from_id", type=int) or 0)
    dst = own_or_404(Account, request.form.get("to_id", type=int) or 0)
    amount = parse_amount(request.form.get("amount", ""))
    if src.id == dst.id or amount is None:
        flash("Проверьте счета и сумму перевода.", "danger")
        return redirect(url_for("accounts"))
    note = f"Перевод: {src.name} → {dst.name}"
    d = date.today()
    db.session.add(Transaction(user_id=current_user.id, account_id=src.id, type="expense",
                               amount=amount, date=d, description=note))
    db.session.add(Transaction(user_id=current_user.id, account_id=dst.id, type="income",
                               amount=amount, date=d, description=note))
    db.session.commit()
    flash("Перевод выполнен.", "success")
    return redirect(url_for("accounts"))


# ---------------------------------------------------------------- категории
@app.route("/categories", methods=["GET", "POST"])
@login_required
def categories():
    if request.method == "POST":
        cid = request.form.get("id", type=int)
        cat = own_or_404(Category, cid) if cid else Category(user_id=current_user.id)
        name = request.form.get("name", "").strip()
        type_ = request.form.get("type")
        if not name or type_ not in ("income", "expense"):
            flash("Заполните название и тип категории.", "danger")
            return redirect(url_for("categories"))
        if cid and cat.type != type_ and cat.transactions:
            flash("Нельзя сменить тип категории, у которой есть операции.", "warning")
            return redirect(url_for("categories"))
        cat.name, cat.type = name[:64], type_
        cat.color = request.form.get("color", "#6c757d")[:7]
        cat.icon = request.form.get("icon", "tag").strip()[:32] or "tag"
        db.session.add(cat)
        db.session.commit()
        flash("Категория сохранена.", "success")
        return redirect(url_for("categories"))
    cats = Category.query.filter_by(user_id=current_user.id).order_by(Category.name).all()
    return render_template("categories.html",
                           income=[c for c in cats if c.type == "income"],
                           expense=[c for c in cats if c.type == "expense"])


@app.route("/categories/<int:cid>/delete", methods=["POST"])
@login_required
def category_delete(cid):
    cat = own_or_404(Category, cid)
    if cat.transactions:
        flash(f"Категория используется в {len(cat.transactions)} операциях — удаление невозможно.", "warning")
    else:
        db.session.delete(cat)
        db.session.commit()
        flash("Категория удалена.", "info")
    return redirect(url_for("categories"))


# ---------------------------------------------------------------- бюджеты
@app.route("/budgets", methods=["GET", "POST"])
@login_required
def budgets():
    month = request.args.get("month") or date.today().strftime("%Y-%m")
    if request.method == "POST":
        cat = own_or_404(Category, request.form.get("category_id", type=int) or 0)
        limit = parse_amount(request.form.get("limit_amount", ""))
        month = request.form.get("month", month)
        if limit is None or cat.type != "expense":
            flash("Укажите категорию расходов и положительный лимит.", "danger")
        else:
            b = Budget.query.filter_by(user_id=current_user.id, category_id=cat.id, month=month).first()
            if b:
                b.limit_amount = limit
            else:
                db.session.add(Budget(user_id=current_user.id, category_id=cat.id, month=month,
                                      limit_amount=limit))
            db.session.commit()
            flash("Бюджет сохранён.", "success")
        return redirect(url_for("budgets", month=month))

    items = Budget.query.filter_by(user_id=current_user.id, month=month).all()
    try:
        y, m = map(int, month.split("-"))
        cur = date(y, m, 1)
    except ValueError:
        cur = date.today().replace(day=1)
    return render_template(
        "budgets.html", budgets=items, month=month,
        prev_month=an.shift_month(cur, -1).strftime("%Y-%m"),
        next_month=an.shift_month(cur, 1).strftime("%Y-%m"),
        month_label=f"{an.MONTHS_RU[cur.month - 1]} {cur.year}",
        total_limit=sum(b.limit_amount for b in items),
        total_spent=sum(b.spent() for b in items),
        categories=Category.query.filter_by(user_id=current_user.id, type="expense").order_by(Category.name).all(),
    )


@app.route("/budgets/<int:bid>/delete", methods=["POST"])
@login_required
def budget_delete(bid):
    b = own_or_404(Budget, bid)
    month = b.month
    db.session.delete(b)
    db.session.commit()
    flash("Бюджет удалён.", "info")
    return redirect(url_for("budgets", month=month))


@app.route("/budgets/copy", methods=["POST"])
@login_required
def budgets_copy():
    """Скопировать бюджеты прошлого месяца в выбранный месяц."""
    month = request.form.get("month")
    y, m = map(int, month.split("-"))
    prev = an.shift_month(date(y, m, 1), -1).strftime("%Y-%m")
    existing = {b.category_id for b in Budget.query.filter_by(user_id=current_user.id, month=month)}
    n = 0
    for b in Budget.query.filter_by(user_id=current_user.id, month=prev):
        if b.category_id not in existing:
            db.session.add(Budget(user_id=current_user.id, category_id=b.category_id, month=month,
                                  limit_amount=b.limit_amount))
            n += 1
    db.session.commit()
    flash(f"Скопировано бюджетов: {n}.", "success" if n else "info")
    return redirect(url_for("budgets", month=month))


# ---------------------------------------------------------------- цели
@app.route("/goals", methods=["GET", "POST"])
@login_required
def goals():
    if request.method == "POST":
        gid = request.form.get("id", type=int)
        g = own_or_404(Goal, gid) if gid else Goal(user_id=current_user.id)
        target = parse_amount(request.form.get("target_amount", ""))
        name = request.form.get("name", "").strip()
        if not name or target is None:
            flash("Укажите название и сумму цели.", "danger")
            return redirect(url_for("goals"))
        g.name, g.target_amount = name[:100], target
        g.deadline = parse_date(request.form.get("deadline"))
        if not gid:
            g.current_amount = parse_amount(request.form.get("current_amount", "")) or 0
        db.session.add(g)
        db.session.commit()
        flash("Цель сохранена.", "success")
        return redirect(url_for("goals"))
    return render_template("goals.html", goals=Goal.query.filter_by(user_id=current_user.id)
                           .order_by(Goal.created_at).all())


@app.route("/goals/<int:gid>/deposit", methods=["POST"])
@login_required
def goal_deposit(gid):
    g = own_or_404(Goal, gid)
    amount = parse_amount(request.form.get("amount", ""))
    if amount is None:
        flash("Введите корректную сумму.", "danger")
    else:
        if request.form.get("action") == "withdraw":
            g.current_amount = max(0, g.current_amount - amount)
        else:
            g.current_amount += amount
        db.session.commit()
        flash("Цель обновлена." + (" 🎉 Цель достигнута!" if g.percent() >= 100 else ""), "success")
    return redirect(url_for("goals"))


@app.route("/goals/<int:gid>/delete", methods=["POST"])
@login_required
def goal_delete(gid):
    db.session.delete(own_or_404(Goal, gid))
    db.session.commit()
    flash("Цель удалена.", "info")
    return redirect(url_for("goals"))


# ---------------------------------------------------------------- аналитика
@app.route("/analytics")
@login_required
def analytics_page():
    uid = current_user.id
    period = request.args.get("period", "month")
    start, end = an.period_range(period, request.args.get("start"), request.args.get("end"))
    return render_template(
        "analytics.html", period=period, start=start, end=end,
        summary=an.summary(uid, start, end),
        expense_cats=an.by_category(uid, "expense", start, end),
        income_cats=an.by_category(uid, "income", start, end),
        dynamics=an.monthly_dynamics(uid, 12),
        daily=an.daily_expenses(uid, start, end),
        weekday=an.weekday_expenses(uid, start, end),
        forecast=an.month_forecast(uid),
        tips=an.recommendations(uid),
    )


# ---------------------------------------------------------------- профиль
@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "settings":
            current_user.currency = request.form.get("currency", "₽")[:8]
            db.session.commit()
            flash("Настройки сохранены.", "success")
        elif action == "password":
            if not current_user.check_password(request.form.get("old_password", "")):
                flash("Текущий пароль указан неверно.", "danger")
            elif len(request.form.get("new_password", "")) < 6:
                flash("Новый пароль должен содержать не менее 6 символов.", "danger")
            else:
                current_user.set_password(request.form["new_password"])
                db.session.commit()
                flash("Пароль изменён.", "success")
        elif action == "delete":
            if current_user.check_password(request.form.get("confirm_password", "")):
                user = db.session.get(User, current_user.id)
                logout_user()
                db.session.delete(user)
                db.session.commit()
                flash("Аккаунт удалён.", "info")
                return redirect(url_for("index"))
            flash("Неверный пароль.", "danger")
        return redirect(url_for("profile"))
    stats = {
        "transactions": Transaction.query.filter_by(user_id=current_user.id).count(),
        "accounts": len(current_user.accounts),
        "categories": len(current_user.categories),
        "goals": len(current_user.goals),
    }
    return render_template("profile.html", stats=stats)


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, message="Страница не найдена"), 404


@app.errorhandler(400)
def bad_request(e):
    return render_template("error.html", code=400, message=getattr(e, "description", "Неверный запрос")), 400


# ---------------------------------------------------------------- демо-данные
@app.cli.command("seed")
def seed():
    """Создаёт демо-пользователя demo / demo123 с операциями за 12 месяцев."""
    db.create_all()
    old = User.query.filter_by(username="demo").first()
    if old:
        db.session.delete(old)
        db.session.commit()
    user = User(username="demo", email="demo@example.com")
    user.set_password("demo123")
    db.session.add(user)
    create_defaults(user)
    db.session.flush()
    accs = {a.name: a for a in user.accounts}
    accs["Основная карта"].initial_balance = 25000
    accs["Наличные"].initial_balance = 3000
    savings = Account(user_id=user.id, name="Накопительный счёт", type="deposit",
                      initial_balance=80000, color="#6f42c1")
    db.session.add(savings)
    db.session.flush()
    cats = {c.name: c for c in user.categories}
    card, cash = accs["Основная карта"], accs["Наличные"]

    rnd = random.Random(42)
    plan = [  # категория, мин, макс, раз в месяц, описание
        ("Продукты", 400, 3500, 12, ["Пятёрочка", "Магнит", "Перекрёсток", "Рынок"]),
        ("Кафе и рестораны", 300, 2500, 5, ["Кофейня", "Обед", "Пиццерия", "Суши"]),
        ("Транспорт", 60, 900, 10, ["Метро", "Такси", "Автобус", "Бензин"]),
        ("Развлечения", 300, 3000, 3, ["Кино", "Концерт", "Подписка", "Боулинг"]),
        ("Одежда", 1500, 7000, 1, ["Кроссовки", "Куртка", "Футболки"]),
        ("Здоровье", 300, 3000, 1, ["Аптека", "Стоматолог", "Анализы"]),
        ("Прочее", 200, 2000, 2, ["Подарок другу", "Хозтовары", "Ремонт"]),
    ]
    today = date.today()
    for i in range(11, -1, -1):
        first = an.shift_month(today.replace(day=1), -i)
        last = an.month_bounds(first)[1]  # операции с будущими датами пропускаются в add()

        def add(cat, amount, day, desc, acc=card, type_="expense"):
            d = first.replace(day=min(day, last.day))
            if d <= today:
                db.session.add(Transaction(user_id=user.id, account_id=acc.id, category_id=cats[cat].id,
                                           type=type_, amount=round(amount, 2), date=d, description=desc))

        add("Зарплата", 45000, 5, "Аванс", type_="income")
        add("Зарплата", 40000 + rnd.randint(0, 8000), 20, "Зарплата", type_="income")
        if rnd.random() < 0.6:
            add("Подработка", rnd.randint(5000, 15000), rnd.randint(1, 28), "Фриланс-проект", type_="income")
        add("Проценты по вкладам", 650 + i * 5, 28, "Проценты", acc=savings, type_="income")
        add("Жильё и ЖКХ", 25000, 3, "Аренда квартиры")
        add("Жильё и ЖКХ", rnd.randint(3500, 5500), 15, "Коммунальные платежи")
        add("Связь и интернет", 650, 10, "Мобильная связь")
        add("Связь и интернет", 550, 12, "Домашний интернет")
        add("Образование", 2990, 7, "Онлайн-курс")
        for cat, lo, hi, times, descs in plan:
            for _ in range(times + rnd.randint(-1, 2)):
                add(cat, rnd.uniform(lo, hi), rnd.randint(1, last.day), rnd.choice(descs),
                    acc=cash if rnd.random() < 0.12 else card)
        # ежемесячное снятие наличных (перевод между счетами)
        wd = first.replace(day=6)
        if wd <= today:
            for acc, type_ in ((card, "expense"), (cash, "income")):
                db.session.add(Transaction(user_id=user.id, account_id=acc.id, type=type_, amount=9000,
                                           date=wd, description="Перевод: Основная карта → Наличные"))

    month_key = today.strftime("%Y-%m")
    for cat, limit in [("Продукты", 22000), ("Кафе и рестораны", 7000), ("Транспорт", 5000),
                       ("Развлечения", 5000), ("Одежда", 6000)]:
        db.session.add(Budget(user_id=user.id, category_id=cats[cat].id, month=month_key, limit_amount=limit))
    db.session.add(Goal(user_id=user.id, name="Отпуск на море", target_amount=150000,
                        current_amount=62000, deadline=date(today.year + 1, 7, 1)))
    db.session.add(Goal(user_id=user.id, name="Новый ноутбук", target_amount=90000,
                        current_amount=35000, deadline=date(today.year + 1, 3, 1)))
    db.session.add(Goal(user_id=user.id, name="Финансовая подушка", target_amount=300000,
                        current_amount=80000))
    db.session.commit()
    print("Демо-пользователь создан: логин demo, пароль demo123")


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)
