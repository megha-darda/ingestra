"""Identical conditional-write contract for local SQLite and DynamoDB.

Expected None means absence; a dict means every named attribute must match.
All inventory/journal pairs go through one atomic transaction.
"""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from ..common.model import Conflict, dumps, loads


class LocalStore:
    def __init__(self, path='.local/ingestra.db'):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (tbl TEXT, pk TEXT, sk TEXT, data TEXT, PRIMARY KEY(tbl,pk,sk))')
            db.execute('CREATE TABLE IF NOT EXISTS objects (key TEXT PRIMARY KEY, data BLOB)')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            yield db
        finally:
            db.close()

    def get(self, table, key):
        with self.connection() as db:
            row = db.execute('SELECT data FROM records WHERE tbl=? AND pk=? AND sk=?', (table, *key)).fetchone()
        return loads(row[0]) if row else None

    def query(self, table, pk):
        with self.connection() as db:
            rows = db.execute('SELECT data FROM records WHERE tbl=? AND pk=? ORDER BY sk', (table, pk)).fetchall()
        return [loads(r[0]) for r in rows]

    def transact(self, operations):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                for table, key, value, expected in operations:
                    check_only = table.startswith('check:')
                    table = table.removeprefix('check:')
                    row = db.execute('SELECT data FROM records WHERE tbl=? AND pk=? AND sk=?', (table, *key)).fetchone()
                    old = loads(row[0]) if row else None
                    if (expected is None and old is not None) or (expected is not None and (old is None or any(old.get(k) != v for k, v in expected.items()))):
                        raise Conflict()
                    if check_only:
                        continue
                    if value is None:
                        db.execute('DELETE FROM records WHERE tbl=? AND pk=? AND sk=?', (table, *key))
                    else:
                        db.execute('INSERT OR REPLACE INTO records VALUES (?,?,?,?)', (table, *key, dumps(value)))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise

    def put_object(self, key, content, immutable=False):
        with self.connection() as db:
            try:
                db.execute('INSERT INTO objects VALUES (?,?)' if immutable else 'INSERT OR REPLACE INTO objects VALUES (?,?)', (key, content))
            except sqlite3.IntegrityError as exc:
                old = db.execute('SELECT data FROM objects WHERE key=?', (key,)).fetchone()
                if not old or old[0] != content:
                    raise Conflict('Object already exists.') from exc

    def get_object(self, key):
        with self.connection() as db:
            row = db.execute('SELECT data FROM objects WHERE key=?', (key,)).fetchone()
        if not row:
            from ..common.model import Problem
            raise Problem('Upload not found. Upload the file first.', 404)
        return row[0]


class AwsStore:
    def __init__(self):
        import boto3
        from boto3.dynamodb.types import TypeSerializer, TypeDeserializer
        self.ddb = boto3.client('dynamodb')
        from botocore.config import Config
        region = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION', 'eu-central-1')
        self.s3 = boto3.client('s3', region_name=region, endpoint_url=f'https://s3.{region}.amazonaws.com',
                               config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()
        self.tables = {k: os.environ[k.upper() + '_TABLE'] for k in ('inventory', 'jobs', 'journal')}
        self.bucket = os.environ['BUCKET']

    def encode(self, value):
        return {k: self.serializer.serialize(v) for k, v in value.items()}

    def decode(self, value):
        return {k: self.deserializer.deserialize(v) for k, v in value.items() if k not in ('PK', 'SK')}

    def get(self, table, key):
        result = self.ddb.get_item(TableName=self.tables[table], Key=self.encode(dict(zip(('PK', 'SK'), key))), ConsistentRead=True)
        return self.decode(result['Item']) if 'Item' in result else None

    def query(self, table, pk):
        args = dict(TableName=self.tables[table], KeyConditionExpression='PK = :pk', ExpressionAttributeValues={':pk': {'S': pk}}, ConsistentRead=True)
        items = []
        while True:
            result = self.ddb.query(**args)
            items.extend(self.decode(v) for v in result['Items'])
            if 'LastEvaluatedKey' not in result:
                return items
            args['ExclusiveStartKey'] = result['LastEvaluatedKey']

    def transact(self, operations):
        from botocore.exceptions import ClientError
        actions = []
        for table, key, value, expected in operations:
            check_only = table.startswith('check:')
            table = table.removeprefix('check:')
            args = {'TableName': self.tables[table]}
            if value is None or check_only:
                action = 'ConditionCheck' if check_only else 'Delete'
                args['Key'] = self.encode(dict(zip(('PK', 'SK'), key)))
            else:
                action = 'Put'
                args['Item'] = self.encode(dict(value, PK=key[0], SK=key[1]))
            if expected is None:
                args['ConditionExpression'] = 'attribute_not_exists(PK)'
            else:
                args['ConditionExpression'] = ' AND '.join(f'#c{i} = :c{i}' for i in range(len(expected)))
                args['ExpressionAttributeNames'] = {f'#c{i}': k for i, k in enumerate(expected)}
                args['ExpressionAttributeValues'] = {f':c{i}': self.serializer.serialize(v) for i, v in enumerate(expected.values())}
            actions.append({action: args})
        try:
            self.ddb.transact_write_items(TransactItems=actions)
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'TransactionCanceledException':
                reasons = exc.response.get('CancellationReasons', [])
                if any(r.get('Code') == 'ConditionalCheckFailed' for r in reasons):
                    raise Conflict() from exc
            raise

    def put_object(self, key, content, immutable=False):
        args = dict(Bucket=self.bucket, Key=key, Body=content, ServerSideEncryption='AES256')
        if immutable:
            args['IfNoneMatch'] = '*'
        from botocore.exceptions import ClientError
        try:
            self.s3.put_object(**args)
        except ClientError as exc:
            if not immutable or exc.response['Error']['Code'] not in ('PreconditionFailed', 'ConditionalRequestConflict') or self.get_object(key) != content:
                raise

    def get_object(self, key):
        from ..common.model import MAX_BYTES, Problem
        from botocore.exceptions import ClientError
        try:
            result = self.s3.get_object(Bucket=self.bucket, Key=key)
            limit = MAX_BYTES if key.startswith('uploads/') else 10 * MAX_BYTES
            if result['ContentLength'] > limit:
                result['Body'].close()
                raise Problem('File exceeds the 2 MiB upload limit.', 413)
            return result['Body'].read(limit + 1)
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'NoSuchKey':
                raise Problem('Upload not found. Upload the file first.', 404) from exc
            raise


def make_store():
    return AwsStore() if os.environ.get('BUCKET') else LocalStore(os.environ.get('LOCAL_DB', '.local/ingestra.db'))
