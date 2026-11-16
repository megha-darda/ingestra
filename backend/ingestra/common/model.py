import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

MAX_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 500
TYPES = ('electronics', 'groceries')


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Conflict(Problem):
    def __init__(self, message='Resource changed; refresh and review again.'):
        super().__init__(message, 409)


def now():
    return datetime.now(timezone.utc).isoformat()


def token():
    return uuid4().hex


def dumps(value):
    return json.dumps(value, default=lambda x: (int(x) if x == x.to_integral_value() else str(x)) if isinstance(x, Decimal) else x,
                      sort_keys=True, separators=(',', ':'), allow_nan=False)


def loads(value):
    def numbers(obj):
        if 'price' in obj and isinstance(obj['price'], (str, int)) and not isinstance(obj['price'], bool):
            try:
                obj['price'] = Decimal(obj['price'])
            except Exception:
                pass
        return obj
    return json.loads(value, parse_float=Decimal, object_hook=numbers)


def resource_key(tenant, kind, sku):
    return f'{tenant}#{kind}', sku
