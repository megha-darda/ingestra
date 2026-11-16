"""Regenerate static SAM/ASL files. No AWS API calls."""
import json
from pathlib import Path
import yaml

root = Path(__file__).resolve().parents[1]
def ref(name): return {'Ref': name}
def arn(name): return {'Fn::GetAtt': [name, 'Arn']}
def sub(value): return {'Fn::Sub': value}
retry = [{'ErrorEquals':['States.TaskFailed'], 'IntervalSeconds':2, 'MaxAttempts':3, 'BackoffRate':2}]
def task(resource, parameters=None, next_=None):
    value = dict(Type='Task', Resource=resource, Retry=retry)
    if parameters is not None: value['Parameters'] = parameters
    if next_: value['Next'] = next_
    else: value['End'] = True
    return value

Path(root/'workflows').mkdir(exist_ok=True)
preview = {'Comment':'Small job references only; validators store full plan in private S3.', 'StartAt':'InventoryType', 'States':{
    'InventoryType':{'Type':'Choice','Choices':[{'Variable':'$.inventoryType','StringEquals':'electronics','Next':'ValidateElectronics'}, {'Variable':'$.inventoryType','StringEquals':'groceries','Next':'ValidateGroceries'}],'Default':'WorkflowFailed'},
    'ValidateElectronics':task('${ElectronicsArn}'), 'ValidateGroceries':task('${GroceriesArn}'),
    'WorkflowFailed':task('${WorkerArn}', {'stage':'fail','action':'preview','jobId.$':'$.jobId','attempt.$':'$.attempt'})}}
for state in ['ValidateElectronics','ValidateGroceries']:
    preview['States'][state]['Catch']=[{'ErrorEquals':['States.ALL'],'ResultPath':'$.failure','Next':'WorkflowFailed'}]
(root/'workflows/preview.asl.json').write_text(json.dumps(preview,indent=2)+'\n')
for name, action in [('import','execute'),('rollback','rollback')]:
    base={'jobId.$':'$.jobId','attempt.$':'$.attempt','action':action}
    process=task('${WorkerArn}',dict(base,stage='process',**{'ref.$':'$.ref'}))
    process['Catch']=[{'ErrorEquals':['States.ALL'],'ResultPath':None,'Next':'RecordFailure'}]
    prepare=task('${WorkerArn}',dict(base,stage='prepare'),'ProcessRecords')
    prepare['Catch']=[{'ErrorEquals':['States.ALL'],'ResultPath':None,'Next':'WorkflowFailed'}]
    finish=task('${WorkerArn}',dict(base,stage='finish',**{'refs.$':'$.refs'}))
    finish['Catch']=[{'ErrorEquals':['States.ALL'],'ResultPath':None,'Next':'WorkflowFailed'}]
    mapping={'Type':'Map','ItemsPath':'$.refs','MaxConcurrency':5,'ItemSelector':dict(base,**{'ref.$':'$$.Map.Item.Value'}),
             'ItemProcessor':{'ProcessorConfig':{'Mode':'INLINE'},'StartAt':'ProcessRecord','States':{'ProcessRecord':process,'RecordFailure':task('${WorkerArn}',dict(base,stage='recordFailure',**{'ref.$':'$.ref'}))}},
             'ResultPath':None,'Next':'Finalize','Catch':[{'ErrorEquals':['States.ALL'],'ResultPath':None,'Next':'WorkflowFailed'}]}
    workflow={'Comment':'Bounded record references; plans and per-record reports stay in S3.','StartAt':'Prepare','States':{
        'Prepare':prepare,'ProcessRecords':mapping,'Finalize':finish,'WorkflowFailed':task('${WorkerArn}',dict(base,stage='fail'))}}
    (root/f'workflows/{name}.asl.json').write_text(json.dumps(workflow,indent=2)+'\n')

resources={}
for table in ('Inventory','Jobs','Journal'):
    resources[table+'Table']={'Type':'AWS::DynamoDB::Table','Properties':{'BillingMode':'PAY_PER_REQUEST','AttributeDefinitions':[{'AttributeName':k,'AttributeType':'S'} for k in ('PK','SK')],
        'KeySchema':[{'AttributeName':'PK','KeyType':'HASH'},{'AttributeName':'SK','KeyType':'RANGE'}], 'SSESpecification':{'SSEEnabled':True}, 'PointInTimeRecoverySpecification':{'PointInTimeRecoveryEnabled':True}}}
resources['UploadBucket']={'Type':'AWS::S3::Bucket','Properties':{
    'PublicAccessBlockConfiguration':{'BlockPublicAcls':True,'BlockPublicPolicy':True,'IgnorePublicAcls':True,'RestrictPublicBuckets':True},
    'BucketEncryption':{'ServerSideEncryptionConfiguration':[{'ServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]},
    'CorsConfiguration':{'CorsRules':[{'AllowedOrigins':[ref('FrontendOrigin')],'AllowedMethods':['PUT'],'AllowedHeaders':['content-type','x-amz-server-side-encryption'],'MaxAge':300}]},
    'LifecycleConfiguration':{'Rules':[{'Id':'ExpireUnprocessedUploads','Status':'Enabled','Prefix':'uploads/','ExpirationInDays':7}]}}}
resources['BucketPolicy']={'Type':'AWS::S3::BucketPolicy','Properties':{'Bucket':ref('UploadBucket'),'PolicyDocument':{'Version':'2012-10-17','Statement':[{'Effect':'Deny','Principal':'*','Action':'s3:*','Resource':[arn('UploadBucket'),sub('${UploadBucket.Arn}/*')],'Condition':{'Bool':{'aws:SecureTransport':'false'}}}]}}}
resources['HttpApi']={'Type':'AWS::Serverless::HttpApi','Properties':{'CorsConfiguration':{'AllowOrigins':[ref('FrontendOrigin')],'AllowHeaders':['content-type'],'AllowMethods':['GET','POST','PATCH','OPTIONS'],'MaxAge':300},'DefaultRouteSettings':{'ThrottlingBurstLimit':20,'ThrottlingRateLimit':10}}}
def statement(actions, targets):return {'Effect':'Allow','Action':actions,'Resource':targets}
def transaction(actions, targets):
    return dict(statement(actions,targets), Condition={'ForAnyValue:StringEquals':{'dynamodb:EnclosingOperation':['TransactWriteItems']}})
table_arns=[arn(n+'Table') for n in ('Inventory','Jobs','Journal')]
object_arns=[sub('${UploadBucket.Arn}/'+p+'/*') for p in ('uploads','plans','outcomes','reports')]
worker_policy=[statement(['dynamodb:GetItem','dynamodb:Query'],table_arns),transaction(['dynamodb:PutItem'],table_arns),transaction(['dynamodb:DeleteItem'],[arn('InventoryTable')]),transaction(['dynamodb:ConditionCheckItem'],[arn('JobsTable')]),statement(['s3:GetObject','s3:PutObject'],object_arns)]
validator_policy=[statement(['dynamodb:GetItem','dynamodb:Query'],[arn('InventoryTable'),arn('JobsTable')]),transaction(['dynamodb:PutItem'],[arn('JobsTable')]),statement(['s3:GetObject'],object_arns[:2]),statement(['s3:PutObject'],[object_arns[1]])]
api_policy=[statement(['dynamodb:GetItem','dynamodb:Query'],table_arns),transaction(['dynamodb:PutItem'],[arn('InventoryTable'),arn('JobsTable')]),statement(['s3:GetObject'],object_arns[1:]),statement(['s3:PutObject'],[object_arns[0]]),statement(['states:StartExecution'],[arn(n+'Workflow') for n in ('Preview','Import','Rollback')])]
functions=[('Api','ingestra.handler.api',api_policy),('Worker','ingestra.handler.worker',worker_policy),('Electronics','ingestra.handler.electronics_validator',validator_policy),('Groceries','ingestra.handler.groceries_validator',validator_policy)]
for name, handler, policy in functions:
    props={'CodeUri':'backend/','Handler':handler,'Policies':[{'Version':'2012-10-17','Statement':policy}]}
    if name=='Api':
        props['Events']={'Proxy':{'Type':'HttpApi','Properties':{'ApiId':ref('HttpApi'),'Path':'/{proxy+}','Method':'ANY'}}}
        props['Environment']={'Variables':{n.upper()+'_WORKFLOW':arn(n+'Workflow') for n in ('Preview','Import','Rollback')}}
    resources[name+'Function']={'Type':'AWS::Serverless::Function','Properties':props}
    resources[name+'LogGroup']={'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':sub('/aws/lambda/${'+name+'Function}'),'RetentionInDays':14}}
for name in ('Preview','Import','Rollback'):
    resources[name+'WorkflowLogs']={'Type':'AWS::Logs::LogGroup','Properties':{'LogGroupName':sub('/aws/vendedlogs/states/${AWS::StackName}-'+name.lower()),'RetentionInDays':14}}
    substitutions={'WorkerArn':arn('WorkerFunction')}
    invokes=[arn('WorkerFunction')]
    if name=='Preview':
        substitutions.update(ElectronicsArn=arn('ElectronicsFunction'),GroceriesArn=arn('GroceriesFunction'))
        invokes += [arn('ElectronicsFunction'),arn('GroceriesFunction')]
    resources[name+'Workflow']={'Type':'AWS::Serverless::StateMachine','Properties':{
        'Type':'STANDARD','DefinitionUri':'workflows/'+name.lower()+'.asl.json','DefinitionSubstitutions':substitutions,
        'Logging':{'Level':'ERROR','IncludeExecutionData':False,'Destinations':[{'CloudWatchLogsLogGroup':{'LogGroupArn':arn(name+'WorkflowLogs')}}]},
        'Policies':[{'Version':'2012-10-17','Statement':[statement(['lambda:InvokeFunction'],invokes),statement(['logs:CreateLogDelivery','logs:GetLogDelivery','logs:UpdateLogDelivery','logs:DeleteLogDelivery','logs:ListLogDeliveries','logs:PutResourcePolicy','logs:DescribeResourcePolicies','logs:DescribeLogGroups'],['*'])]}]}}
template={'AWSTemplateFormatVersion':'2010-09-09','Transform':'AWS::Serverless-2016-10-31','Description':'Ingestra - previewed inventory ingestion and conflict-aware recovery (public demo API).',
    'Parameters':{'FrontendOrigin':{'Type':'String','Default':'http://localhost:5173','Description':'Exact browser origin allowed by API and S3 CORS.'},'DemoTenant':{'Type':'String','Default':'demo-retail','AllowedPattern':'[a-zA-Z0-9-]{1,50}'}},
    'Globals':{'Function':{'Runtime':'python3.12','Timeout':60,'MemorySize':512,'Architectures':['x86_64'],'Environment':{'Variables':{'DEMO_TENANT':ref('DemoTenant'),'BUCKET':ref('UploadBucket'),'INVENTORY_TABLE':ref('InventoryTable'),'JOBS_TABLE':ref('JobsTable'),'JOURNAL_TABLE':ref('JournalTable')}}}},
    'Resources':resources,'Outputs':{'ApiUrl':{'Value':sub('https://${HttpApi}.execute-api.${AWS::Region}.${AWS::URLSuffix}')},'UploadBucketName':{'Value':ref('UploadBucket')},
        **{n+'TableName':{'Value':ref(n+'Table')} for n in ('Inventory','Jobs','Journal')},**{n+'WorkflowArn':{'Value':arn(n+'Workflow')} for n in ('Preview','Import','Rollback')}}}
(root/'template.yaml').write_text(yaml.safe_dump(template,sort_keys=False))
