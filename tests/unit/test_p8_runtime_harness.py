import json, platform
from pathlib import Path

import pytest

from scripts.p8_runtime import harness as h


def test_fixture_generation_stable_hashes_and_depths():
    a=h.generate_fixture('8k', seed=1566)
    b=h.generate_fixture('8k', seed=1566)
    assert a == b
    m=a['manifest']
    assert m['actual_tokens'] >= m['requested_tokens']
    assert set(m['target_depths_tokens']) == {'VII','XIV','XXI'}
    assert m['target_depths_tokens']['VII'] < m['target_depths_tokens']['XIV'] < m['target_depths_tokens']['XXI']
    assert m['expected_answers']['canary'].startswith('lunar-maple-')


def test_fixture_generation_supported_sizes():
    for tier in ['8k','32k','55k']:
        m=h.generate_fixture(tier)['manifest']
        assert m['requested_tokens'] == h.TIERS[tier]
        assert m['actual_tokens'] >= h.TIERS[tier]


def test_authoritative_tokenizer_adapter_used():
    calls=[]
    def tok(text):
        calls.append(text)
        return len(text.split())
    fx=h.generate_fixture('8k', tokenizer=tok)
    assert calls
    assert fx['manifest']['actual_tokens'] == len(fx['prompt'].split())


def good_response(manifest):
    return json.dumps(manifest['expected_answers'], sort_keys=True)


def test_semantic_exact_success():
    m=h.generate_fixture('8k')['manifest']
    res=h.evaluate_semantic(good_response(m),m)
    assert res['semantic_pass']
    assert all(res['categories'].values())


@pytest.mark.parametrize('payload,code', [
    ({'VII':'They were obliged to camp out'}, 'word_count_VII'),
    ({'XIV':'The Winged Monkeys'}, 'heading_substitution_XIV'),
    ({'XXI':'The Lion Becomes the King'}, 'heading_substitution_XXI'),
    ({'canary':'wrong'}, 'canary_mismatch'),
])
def test_known_bad_semantic_failures(payload, code):
    m=h.generate_fixture('8k')['manifest']; obj=dict(m['expected_answers']); obj.update(payload)
    res=h.evaluate_semantic(json.dumps(obj, sort_keys=True),m)
    assert not res['semantic_pass']
    assert code in res['failure_codes']


@pytest.mark.parametrize('text,code', [
    ('```json\n{}\n```','json_not_only'),
    ('not json','invalid_json'),
    ('{}\ncommentary','json_not_only'),
])
def test_json_markdown_commentary_failures(text, code):
    m=h.generate_fixture('8k')['manifest']; res=h.evaluate_semantic(text,m)
    assert code in res['failure_codes']


def test_extra_missing_capitalization_punctuation_whitespace():
    m=h.generate_fixture('8k')['manifest']
    obj=dict(m['expected_answers']); obj['extra']='x'
    assert 'key_set_mismatch' in h.evaluate_semantic(json.dumps(obj),m)['failure_codes']
    obj=dict(m['expected_answers']); del obj['VII']
    assert 'key_set_mismatch' in h.evaluate_semantic(json.dumps(obj),m)['failure_codes']
    obj=dict(m['expected_answers']); obj['XIV']=obj['XIV'].lower()
    assert 'capitalization_XIV' in h.evaluate_semantic(json.dumps(obj),m)['failure_codes']
    obj=dict(m['expected_answers']); obj['XXI'] += '.'
    res=h.evaluate_semantic(json.dumps(obj),m)
    assert 'punctuation_XXI' in res['failure_codes'] and 'exact_mismatch_XXI' in res['failure_codes']


def test_repeated_trial_scoring():
    m=h.generate_fixture('8k')['manifest']
    score=h.score_trials([good_response(m), '{}'],m)
    assert score['trial_count']==2 and score['exact_match_count']==1 and score['pass_rate']==0.5


def test_progress_invariants():
    ok=[{'seq':0,'phase':'preparing','processed_tokens':0,'generated_tokens':0,'prompt_total_tokens':10},{'seq':1,'phase':'prefill','processed_tokens':10,'generated_tokens':0,'prompt_total_tokens':10},{'seq':2,'phase':'generation','processed_tokens':10,'generated_tokens':1,'prompt_total_tokens':10},{'seq':3,'phase':'complete','processed_tokens':10,'generated_tokens':2,'prompt_total_tokens':10}]
    assert h.validate_progress(ok)['pass']
    bad=ok+[{'seq':4,'phase':'generation','processed_tokens':10,'generated_tokens':3,'prompt_total_tokens':10}]
    assert 'progress_after_terminal' in h.validate_progress(bad)['failure_codes']
    bad=[{'seq':1,'phase':'prefill','processed_tokens':5,'generated_tokens':0,'prompt_total_tokens':10},{'seq':0,'phase':'prefill','processed_tokens':4,'generated_tokens':0,'prompt_total_tokens':11}]
    assert {'sequence_decreased','processed_decreased','prompt_total_changed'} <= set(h.validate_progress(bad)['failure_codes'])
    bad=[{'seq':0,'phase':'generation','processed_tokens':11,'generated_tokens':0,'prompt_total_tokens':10},{'seq':1,'phase':'prefill','processed_tokens':11,'generated_tokens':0,'prompt_total_tokens':10}]
    assert {'processed_exceeds_total','invalid_phase_transition'} <= set(h.validate_progress(bad)['failure_codes'])


def test_metrics_and_missing_telemetry():
    events=[{'seq':0,'phase':'preparing','processed_tokens':0,'generated_tokens':0,'prompt_total_tokens':10,'elapsed_seconds':0.1},{'seq':1,'phase':'prefill','processed_tokens':10,'generated_tokens':0,'prompt_total_tokens':10,'elapsed_seconds':1.1},{'seq':2,'phase':'generation','processed_tokens':10,'generated_tokens':1,'prompt_total_tokens':10,'elapsed_seconds':1.2},{'seq':3,'phase':'complete','processed_tokens':10,'generated_tokens':2,'prompt_total_tokens':10,'elapsed_seconds':2.2}]
    metrics=h.calculate_metrics(events, 5, 4)
    assert metrics['remaining_completion_margin_seconds']==pytest.approx(2.8)
    assert metrics['prompt_tokens_per_second'] is not None
    with pytest.raises(ValueError): h.calculate_metrics([],5,1)


def test_p7_memory_estimate_wrapper_consumes_existing_estimator(monkeypatch):
    seen={}
    def fake(path,n_ctx,kv,backend,batch):
        seen.update(path=path,n_ctx=n_ctx,kv=kv,backend=backend,batch=batch)
        return {'exact_kv_allocation_bytes': 42}
    monkeypatch.setattr(h, '_qwen_64k_memory_estimate', fake)
    assert h.p7_memory_estimate('model.gguf', 8192, 'q8', 'metal', 'balanced')['exact_kv_allocation_bytes'] == 42
    assert seen == {'path':'model.gguf','n_ctx':8192,'kv':'q8','backend':'metal','batch':'balanced'}


def test_kv_compare_boundaries():
    est={'exact_kv_allocation_bytes':1000,'conservative_fallback_used':False}
    assert h.compare_kv_estimate(est, {'kv_allocation_bytes':1000})['pass']
    assert not h.compare_kv_estimate(est, {'kv_allocation_bytes':1000+20*1024*1024})['pass']
    assert h.compare_kv_estimate({'conservative_fallback_used':True}, {'kv_allocation_bytes':1})['failure_code']=='exact_estimate_unavailable'
    assert h.compare_kv_estimate(est, {})['failure_code']=='runtime_kv_diagnostic_missing'


def test_cancellation_prefill_generation_and_recovery():
    events=[{'seq':0,'phase':'prefill','processed_tokens':1,'generated_tokens':0,'prompt_total_tokens':10},{'seq':1,'phase':'prefill','processed_tokens':5,'generated_tokens':0,'prompt_total_tokens':10}]
    run=h.FakeRuntimeAdapter('{}',events).run('p',{}, {'field':'processed_tokens','value':5})
    assert run['cancelled'] and run['response'] is None and run['recovery']['followup_success']
    events=[{'seq':0,'phase':'generation','processed_tokens':10,'generated_tokens':0,'prompt_total_tokens':10},{'seq':1,'phase':'generation','processed_tokens':10,'generated_tokens':3,'prompt_total_tokens':10}]
    assert h.FakeRuntimeAdapter('{}',events).run('p',{}, {'field':'generated_tokens','value':2})['cancelled']


def test_atomic_report_schema_and_redaction(tmp_path):
    data={'schema_version':h.SCHEMA_VERSION,'path':'/Users/daniel/secret/model.gguf','request_id':'abc','nested':['ciphertext payload']}
    p=tmp_path/'report.json'; h.atomic_write_json(p,data)
    loaded=json.loads(p.read_text())
    assert loaded['schema_version']==h.SCHEMA_VERSION
    text=p.read_text(); assert '/Users/daniel' not in text and 'ciphertext' not in text and 'request_id' not in text


def test_cli_input_validation_and_fake_report(tmp_path, capsys):
    assert h.main(['run','--mode','packaged','--output-dir',str(tmp_path)]) == 3
    assert h.main(['run','--mode','fake','--tier','8k','--output-dir',str(tmp_path),'--strict']) == 0
    assert (tmp_path/'p8-runtime-benchmark-report.json').exists()


def test_cli_generate_and_evaluate(tmp_path):
    assert h.main(['generate-fixture','--tier','8k','--output-dir',str(tmp_path)]) == 0
    manifest=tmp_path/'synthetic-8k.manifest.json'
    m=json.loads(manifest.read_text())
    response=tmp_path/'response.json'; response.write_text(good_response(m))
    assert h.main(['evaluate','--manifest',str(manifest),'--response',str(response),'--strict']) == 0
    bad=tmp_path/'bad.json'; bad.write_text('{}')
    assert h.main(['evaluate','--manifest',str(manifest),'--response',str(bad),'--strict']) == 2


def test_memory_probe_success_absence_timeout_malformed_sanitized(monkeypatch):
    class CP: returncode=0; stdout='/Users/daniel ok'; stderr=''
    monkeypatch.setattr(platform,'system',lambda:'Darwin')
    monkeypatch.setattr(h.subprocess,'run',lambda *a,**k: CP())
    assert '/Users/daniel' not in str(h.MemoryProbe().collect())
    def timeout(*a,**k): raise h.subprocess.TimeoutExpired('x',1)
    monkeypatch.setattr(h.subprocess,'run',timeout)
    assert h.MemoryProbe().collect()['reason']=='timeout'
    monkeypatch.setattr(platform,'system',lambda:'Linux')
    assert h.MemoryProbe().collect()['reason']=='unsupported_platform'


def test_platform_behavior_labels(monkeypatch):
    for name in ['Darwin','Windows','Plan9']:
        monkeypatch.setattr(platform,'system',lambda n=name:n)
        result=h.MemoryProbe().collect()
        assert result['platform'] in {'darwin','windows','plan9'}
