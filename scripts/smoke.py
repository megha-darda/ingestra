"""Read-only HTTP checks; does not seed data or create cloud resources."""
import argparse
import json
from urllib.request import urlopen

parser=argparse.ArgumentParser()
parser.add_argument('--api',default='http://127.0.0.1:3001')
args=parser.parse_args()
for path in ('/health','/imports','/inventory'):
    with urlopen(args.api.rstrip('/')+path,timeout=15) as response:
        body=json.load(response)
        assert response.status==200
        print(path, response.status, body if path=='/health' else f'{len(body["items"])} records')
