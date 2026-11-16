from decimal import Decimal, InvalidOperation
from .common import base


def validate(row):
    data, errors = base(row, ('serialNumber', 'warrantyMonths'))
    if 'warrantyMonths' in data:
        try:
            value = data['warrantyMonths']
            num = Decimal(str(value))
            if isinstance(value, bool) or not num.is_finite() or num < 0 or num > 1200 or num != num.to_integral_value():
                raise ValueError()
            data['warrantyMonths'] = int(num)
        except (ValueError, InvalidOperation):
            errors.append('warrantyMonths must be a nonnegative integer, at most 1200.')
    return data, errors
