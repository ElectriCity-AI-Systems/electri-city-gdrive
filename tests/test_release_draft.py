import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('prepare_release_draft', Path(__file__).resolve().parents[1] / 'scripts/prepare_release_draft.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def run(commit='a'*40, conclusion='success'):
    return {'head_sha':commit, 'name':'CI', 'status':'completed', 'conclusion':conclusion}


def jobs():
    return [{'name':name, 'conclusion':'success'} for name in module.REQUIRED_JOBS]


def test_ci_requires_exact_commit_and_all_jobs():
    module.validate_ci(run(), jobs(), 'a'*40)
    with pytest.raises(ValueError): module.validate_ci(run('b'*40), jobs(), 'a'*40)
    with pytest.raises(ValueError): module.validate_ci(run(), jobs()[:-1], 'a'*40)
    with pytest.raises(ValueError): module.validate_ci(run(conclusion='failure'), jobs(), 'a'*40)


def test_ci_rejects_failed_required_job():
    items=jobs();items[0]['conclusion']='skipped'
    with pytest.raises(ValueError): module.validate_ci(run(),items,'a'*40)


def test_asset_manifest_rejects_drift_and_unknown_files(tmp_path):
    package=tmp_path/'electridrive_2.2.0_amd64.deb';package.write_bytes(b'tested')
    entry={'name':package.name,'sha256':module.digest(package),'size':6}
    module.validate_assets(tmp_path,[entry])
    package.write_bytes(b'changed')
    with pytest.raises(ValueError):module.validate_assets(tmp_path,[entry])
    with pytest.raises(ValueError):module.validate_assets(tmp_path,[dict(entry,name='../private.pem')])


def test_release_command_is_always_draft_and_exact_sha():
    cmd=module.release_command('2.2.0','a'*40,[Path('example.deb')],Path('notes.md'))
    assert '--draft' in cmd
    assert cmd[cmd.index('--target')+1]=='a'*40
    assert '--latest' not in cmd


def test_versioned_manifest_requires_both_formats_and_checksums(tmp_path):
    with pytest.raises(ValueError, match='complete'):
        module.validate_assets(tmp_path,[],version='2.2.0')


def test_record_refuses_source_drift(monkeypatch, tmp_path):
    monkeypatch.setattr(module,'source_snapshot',lambda:{'commit':'new','version':'2.2.0'})
    with pytest.raises(ValueError,match='Source changed'):
        module.record_candidate(tmp_path/'manifest.json',{'commit':'old','version':'2.2.0'})
