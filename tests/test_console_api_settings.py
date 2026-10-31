"""Local files and scripted clients only; no provider requests."""
import json
import os
from types import SimpleNamespace

import pytest

from pilot01.experiment.console import local_credentials, api_values
from test_experiment_console import make_app, start_data


@pytest.fixture
def app(tmp_path, monkeypatch):
    for slot in (1, 2):
        suffix = '' if slot == 1 else '_2'
        monkeypatch.setenv('PILOT01_API_KEY' + suffix, '')
        monkeypatch.setenv('PILOT01_BASE_URL' + suffix, '')
    instance = make_app(tmp_path)
    path = tmp_path/'config/api.local.env'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('PILOT01_BASE_URL=https://first.example/v1\nPILOT01_API_KEY=test-old-secret-one\nPILOT01_MODEL_ID=frozen-id\nPILOT01_BASE_URL_2=https://second.example/v1\nPILOT01_API_KEY_2=test-old-secret-two\n')
    instance.credential_loader = lambda: local_credentials(path)
    instance.configured_slots = instance.credential_loader()
    return instance


def payload(**change):
    result = {'apis': [dict(slot=1, base_url='https://first.example/v1', api_key=''),
                       dict(slot=2, base_url='https://second.example/v1', api_key='')]}
    result['apis'][0].update(change)
    return result


def test_public_settings_never_return_key_and_blank_keeps_current(app):
    before = app.config()
    result = app.save_apis(payload())
    assert result['apis'][0]['key_id'] == before['apis'][0]['key_id']
    assert result['apis'][0]['base_url'] == 'https://first.example/v1'
    assert result['apis'][0]['key_present']
    assert api_values(app.root/'config/api.local.env')['PILOT01_API_KEY'] == 'test-old-secret-one'
    assert 'test-old-secret' not in json.dumps(result)
    assert api_values(app.root/'config/api.local.env')['PILOT01_MODEL_ID'] == 'frozen-id'


def test_replace_key_and_endpoint_persists_and_redacts_old_and_new(app):
    old = app.config()['apis'][0]['key_id']
    result = app.save_apis(payload(base_url='https://replacement.example/v1/', api_key='test-new-secret'))
    assert result['apis'][0]['base_url'] == 'https://replacement.example/v1'
    assert result['apis'][0]['key_id'] != old
    assert os.environ['PILOT01_API_KEY'] == 'test-new-secret'
    assert app.redact('test-old-secret-one test-new-secret') == '[REDACTED] [REDACTED]'
    assert 'test-new-secret' not in json.dumps(app.config())
    if os.name != 'nt':
        assert (app.root/'config/api.local.env').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('changes', [
    {'base_url':'https://user:password@example.com/v1'},
    {'base_url':'https://example.com/v1?key=do-not-echo'},
    {'base_url':'https://example.com/v1/chat/completions'},
    {'base_url':'file:///etc/passwd'},
    {'base_url':'https://example.com/\nPILOT01_MODEL_ID=other'},
    {'base_url':'https://example.com:broken/v1'},
    {'api_key':'new\nPILOT01_MODEL_ID=other'},
    {'api_key':'replace-with-key'},
    {'slot':True}, {'slot':2},
])
def test_invalid_payload_leaves_original_bytes_unchanged(app, changes):
    path = app.root/'config/api.local.env'
    before = path.read_bytes()
    with pytest.raises(ValueError):
        app.save_apis(payload(**changes))
    assert path.read_bytes() == before
    assert not list(path.parent.glob('*.tmp'))


def test_busy_configuration_is_readable_but_not_editable(app):
    app.thread = SimpleNamespace(is_alive=lambda: True)
    assert app.config()['apis'][0]['ready']
    with pytest.raises(ValueError, match='正在运行'):
        app.save_apis(payload(api_key='new-secret'))


def test_model_edits_are_rejected(app):
    value = payload()
    value['apis'][0]['model'] = 'another-model'
    with pytest.raises(ValueError, match='模型'):
        app.save_apis(value)


def test_started_snapshot_contains_only_public_settings(app):
    draft = app.prepare({})
    app.start(start_data(draft))
    app.thread.join(timeout=15)
    snapshot = app.session['api_configuration']
    assert snapshot[0]['base_url'] == 'https://first.example/v1'
    assert 'test-old-secret' not in json.dumps(snapshot)
    saved = json.loads((app._paths().manifest_dir/'launch_selection.json').read_text())
    assert saved['api_configuration'] == snapshot


def test_resume_rejects_changed_credentials_before_constructing_client(app, monkeypatch):
    draft = app.prepare({})
    app.start(start_data(draft, count=1))
    app.thread.join(timeout=15)
    app.session['status'] = 'stopped'
    monkeypatch.setattr(app, '_pending_count', lambda: 1)
    app.save_apis(payload(api_key='a-replacement-secret'))
    with pytest.raises(ValueError, match='API地址或Key已改变'):
        app.resume()


@pytest.mark.parametrize('reload_fails', [False, True])
def test_interactive_login_preserves_other_accounts_and_rolls_back(tmp_path, monkeypatch, reload_fails, capsys):
    import importlib.util
    import subprocess
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('login_helper', Path(__file__).parents[1]/'deploy/cloud/change_access.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper.Path, 'home', lambda: tmp_path)
    original = {'owner':'unchanged-owner-secret','researcher':'previous-login-secret'}
    file = tmp_path/'.pilot01-access.json'
    file.write_text(json.dumps(original))
    answers = iter(['researcher','lab'])
    monkeypatch.setattr('builtins.input',lambda _:next(answers))
    monkeypatch.setattr(helper.getpass,'getpass',lambda _:'easier-test-password')
    installed = []
    reloads = []
    def command(args, **kwargs):
        if 'hash-password' in args:
            return SimpleNamespace(returncode=0,stdout='fake-bcrypt-hash')
        if 'cat' in args:
            return SimpleNamespace(stdout=b'old hashed entries')
        if 'install' in args:
            installed.append(Path(args[-2]).read_bytes())
        if 'reload' in args:
            reloads.append(True)
            if reload_fails and len(reloads)==1:
                raise subprocess.CalledProcessError(1,args)
        return SimpleNamespace(returncode=0,stdout='')
    monkeypatch.setattr(helper.subprocess,'run',command)
    if reload_fails:
        with pytest.raises(SystemExit,match='original login restored'):
            helper.main()
        assert json.loads(file.read_text()) == original
        assert installed[-1] == b'old hashed entries'
    else:
        helper.main()
        assert json.loads(file.read_text()) == {'owner':original['owner'],'lab':'easier-test-password'}
        assert b'owner fake-bcrypt-hash' in installed[0]
        assert b'lab fake-bcrypt-hash' in installed[0]
    out=capsys.readouterr().out
    assert 'easier-test-password' not in out and original['owner'] not in out
