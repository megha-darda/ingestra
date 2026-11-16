import json
from pathlib import Path
import yaml


def test_workflow_wiring_and_bounded_payloads():
    template=yaml.safe_load(Path('template.yaml').read_text())
    resources=template['Resources']
    for name in ('Preview','Import','Rollback'):
        definition=json.loads(Path(f'workflows/{name.lower()}.asl.json').read_text())
        substitutions=resources[name+'Workflow']['Properties']['DefinitionSubstitutions']
        assert resources[name+'Workflow']['Properties']['Type']=='STANDARD'
        for key in ('WorkerArn',):assert key in substitutions
        if name=='Preview':
            choices=definition['States']['InventoryType']['Choices']
            assert {c['StringEquals'] for c in choices}=={'electronics','groceries'}
        else:
            mapping=definition['States']['ProcessRecords']
            assert mapping['MaxConcurrency']==5
            assert mapping['ItemsPath']=='$.refs' and mapping['ResultPath'] is None
        for state in definition['States'].values():
            if state['Type']=='Task':assert state.get('Catch') or state.get('End')
    assert resources['UploadBucket']['Properties']['PublicAccessBlockConfiguration']['BlockPublicPolicy']
    assert all(resources[n+'Function']['Properties']['CodeUri']=='backend/' for n in ('Api','Worker','Electronics','Groceries'))
    for name in ('Api','Worker','Electronics','Groceries'):
        for policy in resources[name+'Function']['Properties']['Policies']:
            for statement in policy['Statement']:
                assert 'dynamodb:TransactWriteItems' not in statement['Action']
                if 'dynamodb:PutItem' in statement['Action']:
                    assert statement['Condition']['ForAnyValue:StringEquals']['dynamodb:EnclosingOperation']==['TransactWriteItems']
