import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
import pytest
from moto import mock_aws
from backend.ingestra.common.service import Service
from backend.ingestra.common.store import LocalStore, AwsStore


@pytest.fixture(params=['sqlite', 'dynamodb'])
def service(request, tmp_path, monkeypatch):
    if request.param == 'sqlite':
        yield Service(LocalStore(tmp_path / 'test.db'))
        return
    for name, value in {'AWS_ACCESS_KEY_ID':'testing','AWS_SECRET_ACCESS_KEY':'testing','AWS_DEFAULT_REGION':'eu-central-1','AWS_EC2_METADATA_DISABLED':'true','BUCKET':'ingestra-test-bucket','INVENTORY_TABLE':'inventory','JOBS_TABLE':'jobs','JOURNAL_TABLE':'journal'}.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        ddb = boto3.client('dynamodb', region_name='eu-central-1')
        for table in ('inventory', 'jobs', 'journal'):
            ddb.create_table(TableName=table, BillingMode='PAY_PER_REQUEST', AttributeDefinitions=[{'AttributeName':k,'AttributeType':'S'} for k in ('PK','SK')],KeySchema=[{'AttributeName':'PK','KeyType':'HASH'},{'AttributeName':'SK','KeyType':'RANGE'}])
        boto3.client('s3').create_bucket(Bucket='ingestra-test-bucket',CreateBucketConfiguration={'LocationConstraint':'eu-central-1'})
        yield Service(AwsStore())
