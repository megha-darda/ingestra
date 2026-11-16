from datetime import date
from .common import base


def validate(row):
    data, errors = base(row, ('expiryDate',))
    if 'expiryDate' in data:
        try:
            val = data['expiryDate']
            if len(val) != 10 or date.fromisoformat(val).isoformat() != val:
                raise ValueError()
        except (ValueError, TypeError):
            errors.append('expiryDate must be a valid YYYY-MM-DD date.')
    return data, errors
