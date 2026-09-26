from __future__ import annotations
import shutil, subprocess
from pathlib import Path
from typing import Any
import pytest, yaml
CHART=Path('charts/tokenplace')
def helm(*args:str,check:bool=True)->subprocess.CompletedProcess[str]:
    exe=shutil.which('helm')
    if not exe: pytest.skip('helm is not installed')
    return subprocess.run([exe,'template','tokenplace',str(CHART),*args],text=True,capture_output=True,check=check)
def render(*args:str)->list[dict[str,Any]]: return [d for d in yaml.safe_load_all(helm(*args).stdout) if isinstance(d,dict)]
def v(discovery='direct'):
    a=['--set','stateBackend.type=valkey','--set','stateBackend.valkey.environment=staging','--set','stateBackend.valkey.cluster=relay-a','--set','stateBackend.valkey.acknowledgementKey.existingSecret=relay-state','--set','rateLimit.storage.existingSecret=relay-rate-limit','--set','stateBackend.valkey.auth.existingSecret=relay-valkey']
    return a+(['--set','stateBackend.valkey.direct.host=valkey.internal'] if discovery=='direct' else ['--set','stateBackend.valkey.discovery=sentinel','--set-string',r'stateBackend.valkey.sentinel.endpointsJson=[["sentinel-a"\,26379]]','--set','stateBackend.valkey.sentinel.service=tokenplace','--set','stateBackend.valkey.sentinel.auth.existingSecret=relay-sentinel'])
def dep(ds): return next(d for d in ds if d.get('kind')=='Deployment')
def env(d): return {e['name']:e for e in d['spec']['template']['spec']['containers'][0]['env']}
def test_memory_default_is_single_process_recreate():
 d=dep(render()); e=env(d); assert d['spec']['replicas']==1 and d['spec']['strategy']=={'type':'Recreate'}; assert e['RELAY_WORKERS']['value']=='1' and e['TOKENPLACE_RELAY_STATE_BACKEND']['value']=='memory'
@pytest.mark.parametrize('discovery',['direct','sentinel'])
def test_valkey_renders_exact_contract_with_secret_refs(discovery):
 ds=render(*v(discovery)); e=env(dep(ds)); assert e['TOKENPLACE_RELAY_VALKEY_DISCOVERY']['value']==discovery; assert e['TOKENPLACE_RELAY_VALKEY_SCHEMA_MAJOR']['value']=='1'; assert e['TOKENPLACE_RELAY_VALKEY_MIGRATION_EPOCH']['value']=='1'; assert e['TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64']['valueFrom']['secretKeyRef']['name']=='relay-state'; assert e['TOKENPLACE_RATE_LIMIT_STORAGE_URI']['valueFrom']['secretKeyRef']['name']=='relay-rate-limit'; assert not any(x.get('kind')=='StatefulSet' for x in ds)
def test_ha_renders_rollout_distribution_pdb_probes_and_drain():
 ds=render(*v(),'--set','replicaCount=3','--set','strategy.type=RollingUpdate','--set','podDisruptionBudget.enabled=true'); d=dep(ds); pod=d['spec']['template']['spec']; assert d['spec']['strategy']['rollingUpdate']=={'maxUnavailable':0,'maxSurge':1}; assert pod['topologySpreadConstraints'] and pod['affinity']['podAntiAffinity']; c=pod['containers'][0]; assert c['livenessProbe']['httpGet']['path']=='/livez' and c['readinessProbe']['httpGet']['path']=='/healthz'; assert c['lifecycle']['preStop']; assert next(x for x in ds if x.get('kind')=='PodDisruptionBudget')['spec']['maxUnavailable']==1
@pytest.mark.parametrize('args,msg',[(['--set','stateBackend.type=bogus'],'/stateBackend/type'),(['--set','replicaCount=2'],'require stateBackend.type=valkey'),(['--set','relay.workers=2'],'require stateBackend.type=valkey'),(['--set','stateBackend.type=valkey'],'environment is required'),(v()+['--set','relay.legacyRoutes.enabled=true'],'legacy relay routes'),(v()+['--set','stateBackend.valkey.acknowledgementKey.existingSecret='],'acknowledgementKey.existingSecret'),(v()+['--set','replicaCount=2'],'strategy.type=RollingUpdate')])
def test_invalid_combinations_fail(args,msg):
 r=helm(*args,check=False); assert r.returncode and msg in r.stderr
def test_managed_secret_env_cannot_be_overridden():
 e=env(dep(render(*v(),'--set','extraEnv[0].name=TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64','--set','extraEnv[0].value=plaintext'))); item=e['TOKENPLACE_RELAY_VALKEY_ACKNOWLEDGEMENT_KEY_BASE64']; assert 'value' not in item and item['valueFrom']['secretKeyRef']['name']=='relay-state'
