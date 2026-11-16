import csv
import io
import json
from collections import Counter
from decimal import Decimal, InvalidOperation
from ..common.model import MAX_BYTES, MAX_RECORDS, Problem


def parse_file(content, filename):
    if len(content) > MAX_BYTES:
        raise Problem('File exceeds the 2 MiB upload limit.', 413)
    if not content:
        raise Problem('File must contain data.')
    try:
        text = content.decode('utf-8-sig')
        if filename.lower().endswith('.csv'):
            reader = csv.DictReader(io.StringIO(text), strict=True)
            if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                raise Problem('CSV needs unique column headers.')
            rows = []
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise Problem('CSV row has a different number of columns than the header.')
                rows.append(row)
                if len(rows) > MAX_RECORDS:
                    raise Problem('Maximum 500 records per import.')
        elif filename.lower().endswith('.json'):
            rows = json.loads(text, parse_float=Decimal,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        else:
            raise Problem('Choose a .csv or .json file.')
    except (UnicodeError, csv.Error, json.JSONDecodeError, ValueError) as exc:
        raise Problem('Malformed file. Use UTF-8 CSV or a JSON array of objects.') from exc
    if not isinstance(rows, list) or not rows or len(rows) > MAX_RECORDS:
        raise Problem('Provide an array/table with 1 to 500 records.')
    if any(not isinstance(r, dict) for r in rows):
        raise Problem('Every record must be an object.')
    return rows


def base(row, optional):
    data, errors = {}, []
    allowed = {'sku', 'name', 'quantity', 'price', 'category'} | set(optional)
    extra = set(row) - allowed
    if extra:
        errors.append('Unknown fields: ' + ', '.join(sorted(extra)))
    for key in ('sku', 'name'):
        val = row.get(key)
        if not isinstance(val, str) or not val.strip() or len(val.strip()) > (128 if key == 'sku' else 256):
            errors.append(f'{key} is required (maximum {128 if key == "sku" else 256} characters).')
        else:
            data[key] = val.strip()
    for key in ('quantity', 'price'):
        try:
            val = row.get(key)
            if isinstance(val, bool) or val is None:
                raise ValueError()
            num = Decimal(str(val))
            if not num.is_finite() or num < 0 or num > Decimal('999999999999'):
                raise ValueError()
            if key == 'quantity' and num != num.to_integral_value():
                raise ValueError()
            if key == 'price' and num != num.quantize(Decimal('.01')):
                raise ValueError()
            data[key] = int(num) if key == 'quantity' else num.quantize(Decimal('.01'))
        except (ValueError, InvalidOperation):
            errors.append(f'{key} must be a nonnegative {"integer" if key == "quantity" else "decimal with at most two decimal places"} (maximum 999999999999).')
    for key in ('category', *optional):
        val = row.get(key)
        if val is not None and val != '':
            if key != 'warrantyMonths' and (not isinstance(val, str) or len(val) > 256):
                errors.append(f'{key} must be text of at most 256 characters.')
            else:
                data[key] = val.strip() if isinstance(val, str) else val
    return data, errors


def duplicate_skus(rows):
    counts = Counter(r.get('sku', '').strip() for r in rows if isinstance(r.get('sku'), str))
    return {sku for sku, count in counts.items() if count > 1}
