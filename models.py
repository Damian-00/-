"""Модели базы данных (ORM SQLAlchemy)."""
from datetime import datetime, date

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """Пользователь системы."""
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    currency = db.Column(db.String(8), default="₽", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    accounts = db.relationship("Account", backref="user", lazy=True, cascade="all, delete-orphan")
    categories = db.relationship("Category", backref="user", lazy=True, cascade="all, delete-orphan")
    transactions = db.relationship("Transaction", backref="user", lazy=True, cascade="all, delete-orphan")
    budgets = db.relationship("Budget", backref="user", lazy=True, cascade="all, delete-orphan")
    goals = db.relationship("Goal", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def total_balance(self) -> float:
        return sum(a.balance() for a in self.accounts)


class Account(db.Model):
    """Счёт (кошелёк): наличные, банковская карта, вклад и т.п."""
    __tablename__ = "accounts"

    TYPES = {"cash": "Наличные", "card": "Банковская карта", "deposit": "Вклад", "other": "Другое"}

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(64), nullable=False)
    type = db.Column(db.String(16), default="card", nullable=False)
    initial_balance = db.Column(db.Float, default=0.0, nullable=False)
    color = db.Column(db.String(7), default="#0d6efd")

    transactions = db.relationship("Transaction", backref="account", lazy=True, cascade="all, delete-orphan")

    def balance(self) -> float:
        income = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter_by(
            account_id=self.id, type="income").scalar()
        expense = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter_by(
            account_id=self.id, type="expense").scalar()
        return round(self.initial_balance + income - expense, 2)

    @property
    def type_label(self) -> str:
        return self.TYPES.get(self.type, self.type)


class Category(db.Model):
    """Категория дохода или расхода."""
    __tablename__ = "categories"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(64), nullable=False)
    type = db.Column(db.String(8), nullable=False)  # income | expense
    color = db.Column(db.String(7), default="#6c757d")
    icon = db.Column(db.String(32), default="tag")

    transactions = db.relationship("Transaction", backref="category", lazy=True)
    budgets = db.relationship("Budget", backref="category", lazy=True, cascade="all, delete-orphan")


class Transaction(db.Model):
    """Финансовая операция (доход или расход)."""
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    account_id = db.Column(db.Integer, db.ForeignKey("accounts.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=True)
    type = db.Column(db.String(8), nullable=False)  # income | expense
    amount = db.Column(db.Float, nullable=False)
    date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    description = db.Column(db.String(255), default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Budget(db.Model):
    """Месячный лимит расходов по категории."""
    __tablename__ = "budgets"
    __table_args__ = (db.UniqueConstraint("user_id", "category_id", "month"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"), nullable=False)
    month = db.Column(db.String(7), nullable=False)  # формат YYYY-MM
    limit_amount = db.Column(db.Float, nullable=False)

    def spent(self) -> float:
        year, month = map(int, self.month.split("-"))
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        total = db.session.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
            Transaction.user_id == self.user_id,
            Transaction.category_id == self.category_id,
            Transaction.type == "expense",
            Transaction.date >= start,
            Transaction.date < end,
        ).scalar()
        return round(total, 2)

    def percent(self) -> float:
        return round(self.spent() / self.limit_amount * 100, 1) if self.limit_amount else 0.0


class Goal(db.Model):
    """Финансовая цель (накопление)."""
    __tablename__ = "goals"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    target_amount = db.Column(db.Float, nullable=False)
    current_amount = db.Column(db.Float, default=0.0, nullable=False)
    deadline = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def percent(self) -> float:
        return min(100.0, round(self.current_amount / self.target_amount * 100, 1)) if self.target_amount else 0.0

    def monthly_needed(self) -> float | None:
        """Сколько нужно откладывать в месяц, чтобы успеть к сроку."""
        if not self.deadline:
            return None
        left = self.target_amount - self.current_amount
        if left <= 0:
            return 0.0
        months = (self.deadline.year - date.today().year) * 12 + self.deadline.month - date.today().month
        return round(left / max(months, 1), 2)


DEFAULT_CATEGORIES = [
    ("Зарплата", "income", "#198754", "briefcase"),
    ("Подработка", "income", "#20c997", "laptop"),
    ("Подарки", "income", "#0dcaf0", "gift"),
    ("Проценты по вкладам", "income", "#6610f2", "piggy-bank"),
    ("Продукты", "expense", "#fd7e14", "basket"),
    ("Кафе и рестораны", "expense", "#d63384", "cup-hot"),
    ("Транспорт", "expense", "#0d6efd", "bus-front"),
    ("Жильё и ЖКХ", "expense", "#6f42c1", "house"),
    ("Связь и интернет", "expense", "#0dcaf0", "wifi"),
    ("Здоровье", "expense", "#dc3545", "heart-pulse"),
    ("Одежда", "expense", "#ffc107", "bag"),
    ("Развлечения", "expense", "#20c997", "controller"),
    ("Образование", "expense", "#198754", "book"),
    ("Прочее", "expense", "#6c757d", "three-dots"),
]


def create_defaults(user: User) -> None:
    """Создаёт стандартные категории и счёт для нового пользователя."""
    for name, type_, color, icon in DEFAULT_CATEGORIES:
        db.session.add(Category(user=user, name=name, type=type_, color=color, icon=icon))
    db.session.add(Account(user=user, name="Основная карта", type="card", initial_balance=0, color="#0d6efd"))
    db.session.add(Account(user=user, name="Наличные", type="cash", initial_balance=0, color="#198754"))
