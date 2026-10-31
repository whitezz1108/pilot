import io
import json
from types import SimpleNamespace

from pilot01.experiment.console import make_handler
from test_experiment_console import make_app, start_data


def test_start_user_is_from_gateway_header_and_persisted(tmp_path):
    app = make_app(tmp_path)
    draft = app.prepare({})
    body = {**start_data(draft), 'initiated_by':'forged-body-user'}
    handler = make_handler(app).__new__(make_handler(app))
    handler.server = SimpleNamespace(server_port=8766)
    raw = json.dumps(body).encode()
    handler.headers={'Host':'127.0.0.1:8766','Origin':'http://127.0.0.1:8766',
                     'X-Console-Token':app.token,'X-Pilot01-User':'researcher','Content-Length':str(len(raw))}
    handler.path='/api/start'
    handler.rfile=io.BytesIO(raw)
    sent=[]
    handler.send=lambda status, value:sent.append(status)
    handler.do_POST()
    app.thread.join(timeout=15)
    assert sent==[200]
    assert app.session['initiated_by']=='researcher'
    saved=json.loads((app._paths().manifest_dir/'launch_selection.json').read_text())
    assert saved['initiated_by']=='researcher'
    item=app.records.list()[0]
    assert item['initiated_by']=='researcher'
    assert item['count']==2
    plan=json.loads(app._paths().run_plan_json.read_text())
    selected=[j for j in plan['jobs'] if j['run_id'] in app.session['run_ids']]
    assert item['condition_ids']==sorted({j['condition_id'] for j in selected})
    assert item['actual_contracts']==1
    assert item['started_at']==app.session['created_at']
    exported=json.loads(app.records.export(item['id']))
    assert exported['batch']['initiated_by']=='researcher'
    imported=app.records.import_bytes('records.json',json.dumps(exported).encode())
    assert app.records.batch(imported['id'])['batch']['initiated_by']=='researcher'


def test_legacy_user_remains_unknown_and_read_does_not_change_files(tmp_path):
    app=make_app(tmp_path)
    draft=app.prepare({})
    app.start(start_data(draft))
    app.thread.join(timeout=15)
    # Remove only test-generated metadata to emulate an older session.
    app.session.pop('initiated_by')
    app.session.pop('identity_source')
    app._save()
    # The rebuildable query index is allowed to change during browsing;
    # experiment evidence, frozen metadata and imported originals are not.
    before={p:p.read_bytes() for p in app.output.rglob('*') if p.is_file() and 'index' not in p.relative_to(app.output).parts}
    assert app.records.list()[0]['initiated_by'] is None
    assert all(p.read_bytes()==content for p,content in before.items())


def test_uploaded_csv_does_not_claim_upload_time_as_experiment_start(tmp_path):
    app=make_app(tmp_path)
    uploaded=app.records.import_bytes('summary.csv',b'run_id,case_id,condition,memo,status\nr1,CASE-0001,A1V0D2,E1,completed\n')
    item=app.records.list()[0]
    assert item['time_source']=='imported_at'
    assert item['condition_values']=={'a':['A1'],'v':['V0'],'e':['E1'],'d':['D2']}


def test_gateway_overwrites_client_user_header():
    from pathlib import Path
    import importlib.util
    path=Path(__file__).parents[1]/'deploy/cloud/render_config.py'
    spec=importlib.util.spec_from_file_location('cloud_renderer',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config=module.gateway('1.13.161.246')
    assert 'header_up X-Pilot01-User {http.auth.user.id}' in config
    assert 'header_up +X-Pilot01-User' not in config
