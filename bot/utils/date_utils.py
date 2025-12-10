from datetime import datetime, timedelta


def add_months(base_dt: datetime, months_to_add: int) -> datetime:
    """
    Add calendar months to a datetime, clamping the day to the month's length.
    Preserves tzinfo from base_dt.
    """
    year = base_dt.year
    month = base_dt.month + months_to_add
    day = base_dt.day

    # Normalize year and month
    year += (month - 1) // 12
    month = ((month - 1) % 12) + 1

    # Determine last day of target month
    if month == 12:
        next_month_first = datetime(year + 1, 1, 1, tzinfo=base_dt.tzinfo)
    else:
        next_month_first = datetime(year, month + 1, 1, tzinfo=base_dt.tzinfo)

    last_day = (next_month_first - timedelta(days=1)).day
    clamped_day = min(day, last_day)

    return base_dt.replace(year=year, month=month, day=clamped_day)


def pluralize_months(n: int) -> str:
    """
    Возвращает правильную форму слова 'месяц':
    1 месяц, 2–4 месяца, 5+ месяцев.
    """
    n_abs = abs(n) % 100
    last_digit = n_abs % 10

    if 11 <= n_abs <= 19:
        return "месяцев"
    if last_digit == 1:
        return "месяц"
    if 2 <= last_digit <= 4:
        return "месяца"
    return "месяцев"