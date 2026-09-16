"""Actual sends carry safe model provenance; reuse and rejected work do not."""
import json
from types import SimpleNamespace
import pytest
from ai_run_policy import run_context
from test_ai_local_activity import local_job


def events(store):
    with store._db.cursor() as cur:
        store._db.execute(cur, "SELECT action,detail FROM decision_log WHERE action LIKE 'remediate.ai_request_%' ORDER BY ts,id")
        return [(r['action'],json.loads(r['detail'])) for r in store._db.fetchall(cur)]


def test_real_transport_records_exact_sent_model_and_http_result(isolated_store,monkeypatch):
    import core
    from ai_request_activity import send
    store=isolated_store;job=local_job(store);monkeypatch.setattr(core,'store',store)
    observed=[]
    def post(endpoint,**kwargs):
        observed.extend(events(store));return SimpleNamespace(status_code=200)
    with run_context(store,job['payload'],job):
        response=send(post,'https://private-endpoint/secret',provider='ollama',model='moondream:latest',zone='local',surface='vision',json={'prompt':'secret document text'})
    rows=events(store)
    assert response.status_code==200 and len(observed)==1 and len(rows)==2
    assert [d['status'] for _,d in rows]==['dispatched','response_received']
    assert rows[0][1]['request_id']==rows[1][1]['request_id']
    assert rows[0][1]['model']=='moondream:latest' and rows[0][1]['processing_zone']=='local'
    assert rows[0][1]['run_id']==job['batch_id']
    assert 'secret' not in json.dumps(rows)


def test_cloud_image_deep_transport_uses_actual_payload_not_configured_candidate(isolated_store,monkeypatch):
    import core
    from ai_request_activity import managed_transport
    store=isolated_store;job=local_job(store);monkeypatch.setattr(core,'store',store)
    post=managed_transport(lambda *a,**k:SimpleNamespace(status_code=200),{'actual-model':SimpleNamespace(provider='anthropic'),'unused-model':SimpleNamespace(provider='openai')},SimpleNamespace(zone_for_url=lambda _: 'cloud'))
    with run_context(store,job['payload'],job):
        post('https://cloud/secret',json={'model':'actual-model','messages':[{'content':[{'type':'image','source':{'data':'secret-image'}}]}]})
    assert [(d['provider'],d['model'],d['surface'],d['processing_zone'])for _,d in events(store)]==[('anthropic','actual-model','vision','cloud')]*2


@pytest.mark.parametrize('status',[401,429,500])
def test_http_rejection_is_recorded_as_failed_not_caption_verified(isolated_store,monkeypatch,status):
    import core
    from ai_request_activity import send
    store=isolated_store;job=local_job(store);monkeypatch.setattr(core,'store',store)
    with run_context(store,job['payload'],job):
        send(lambda *a,**k:SimpleNamespace(status_code=status),'endpoint',provider='ollama',model='text:8b',zone='local',surface='text')
    assert events(store)[-1][1]['status']=='failed'
    assert events(store)[-1][1]['http_status']==status


def test_exception_is_preserved_without_sensitive_error_text(isolated_store,monkeypatch):
    import core
    from ai_request_activity import send
    store=isolated_store;job=local_job(store);monkeypatch.setattr(core,'store',store)
    def post(*a,**k):raise RuntimeError('secret credential endpoint')
    with run_context(store,job['payload'],job):
        with pytest.raises(RuntimeError,match='secret'):
            send(post,'endpoint',provider='ollama',model='text',zone='local',surface='text')
    assert events(store)[-1][1]['status']=='failed'
    assert 'secret' not in json.dumps(events(store))


def test_assessment_without_remediation_context_emits_nothing(isolated_store,monkeypatch):
    import core
    from ai_request_activity import send
    monkeypatch.setattr(core,'store',isolated_store)
    send(lambda *a,**k:SimpleNamespace(status_code=200),'endpoint',provider='ollama',model='text',zone='local',surface='text')
    assert events(isolated_store)==[]


def test_erased_scan_cannot_emit_actionable_ai_activity(isolated_store,monkeypatch):
    import core
    from ai_request_activity import send
    store=isolated_store;job=local_job(store);monkeypatch.setattr(core,'store',store)
    with run_context(store,job['payload'],job):
        store.delete_scan('scan','owner')
        send(lambda *a,**k:SimpleNamespace(status_code=200),'endpoint',provider='ollama',model='text',zone='local',surface='text')
    assert events(store)==[]


def test_narration_failure_does_not_change_transport(isolated_store,monkeypatch):
    import core
    from ai_request_activity import send
    store=isolated_store;job=local_job(store);monkeypatch.setattr(core,'store',store)
    monkeypatch.setattr(store,'append_scan_event',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('secret')))
    with run_context(store,job['payload'],job):
        assert send(lambda *a,**k:SimpleNamespace(status_code=200),'endpoint',provider='ollama',model='text',zone='local',surface='text').status_code==200
    assert events(store)==[]


def test_local_text_real_fallback_transport_records_both_actual_models(isolated_store,monkeypatch):
    import core,httpx
    from local_text_waterfall import generate
    store=isolated_store;job=local_job(store);monkeypatch.setattr(core,'store',store)
    monkeypatch.setenv('OLLAMA_FALLBACK_MODEL','fallback:8b')
    class Response:
        status_code=200
        def __init__(self,model):self.model=model
        def raise_for_status(self):pass
        def json(self):return {'response':'' if self.model=='primary:8b' else 'Actual replacement description','done':True}
    monkeypatch.setattr(httpx,'post',lambda endpoint,**kwargs:Response(kwargs['json']['model']))
    with run_context(store,job['payload'],job):
        result=generate('secret','http://localhost:11434','primary:8b',local_only=True)
    assert result['model']=='fallback:8b'
    rows=events(store)
    assert [d['model']for _,d in rows]==['primary:8b','primary:8b','fallback:8b','fallback:8b']
    assert all('file' not in d for _,d in rows)
    assert not store.is_material_event('remediate.ai_request_started')
    assert not store.is_material_event('remediate.ai_request_finished')


@pytest.mark.parametrize('fails', [False, True])
def test_actual_http_duration_uses_monotonic_clock_and_records_failed_sends(isolated_store, monkeypatch, fails):
    import core
    import ai_request_activity as activity
    store = isolated_store
    job = local_job(store)
    monkeypatch.setattr(core, 'store', store)
    ticks = iter([100.0, 102.25])
    monkeypatch.setattr(activity, 'perf_counter', lambda: next(ticks), raising=False)
    def post(*_args, **_kwargs):
        if fails:
            raise RuntimeError('private request text')
        return SimpleNamespace(status_code=200)
    with run_context(store, job['payload'], job):
        if fails:
            with pytest.raises(RuntimeError):
                activity.send(post, 'https://private/secret', provider='ollama', model='moondream:latest', zone='local', surface='vision')
        else:
            activity.send(post, 'https://private/secret', provider='ollama', model='moondream:latest', zone='local', surface='vision')
    rows = events(store)
    assert 'transport_elapsed_ms' not in rows[0][1]
    assert rows[1][1]['transport_elapsed_ms'] == 2250
    assert rows[1][1]['timing_basis'] == 'http_transport_round_trip'
    assert 'private request text' not in json.dumps(rows)
