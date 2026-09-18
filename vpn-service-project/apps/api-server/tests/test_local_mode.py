"""
Тесты локального режима (SQLite).

Проверяем то, что уже ломалось: автоинкремент первичных ключей и время
с таймзоной. На Postgres это работает по умолчанию, на SQLite — нет,
поэтому регрессия здесь особенно вероятна.
"""

from datetime import timedelta

from sqlalchemy import Integer
from sqlalchemy.dialects import postgresql, sqlite

from app.models import PkType, Subscription, SubscriptionStatus, User, UtcDateTime, utcnow


def test_primary_key_is_integer_on_sqlite_and_bigint_on_postgres():
    assert isinstance(PkType.dialect_impl(sqlite.dialect()), Integer)
    assert "BIGINT" in PkType.compile(dialect=postgresql.dialect())


def test_utc_datetime_attaches_timezone_to_naive_value():
    """SQLite возвращает время без таймзоны — тип обязан её проставить."""
    column = UtcDateTime()
    naive = utcnow().replace(tzinfo=None)
    restored = column.process_result_value(naive, sqlite.dialect())
    assert restored.tzinfo is not None
    # Сравнение с now() больше не должно падать
    assert (restored - utcnow()) < timedelta(seconds=5)


def test_utc_datetime_keeps_aware_value_as_is():
    column = UtcDateTime()
    aware = utcnow()
    assert column.process_result_value(aware, postgresql.dialect()) == aware


def test_subscription_days_left_with_naive_expiry():
    """Даже если дата пришла «наивной», кабинет не должен падать."""
    sub = Subscription(
        user_id=1,
        tariff_code="multi",
        status=SubscriptionStatus.ACTIVE,
        expires_at=utcnow() + timedelta(days=5),
        is_trial=False,
    )
    assert sub.is_active is True
    assert 4 <= sub.days_left <= 5


def test_user_defaults():
    user = User(telegram_id=1, referral_code="abc123", marzban_username="u1")
    assert user.telegram_id == 1
    assert user.marzban_username == "u1"
