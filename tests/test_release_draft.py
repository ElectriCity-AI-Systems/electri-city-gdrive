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


def test_existing_tag_must_resolve_to_candidate_commit():
    tag='v2.2.0';ref='refs/tags/'+tag
    with pytest.raises(ValueError):module.validate_tag_lines('b'*40+'\t'+ref+'\n',tag,'a'*40)
    assert module.validate_tag_lines('',tag,'a'*40) is None
    assert module.validate_tag_lines('b'*40+'\t'+ref+'\n'+'a'*40+'\t'+ref+'^{}\n',tag,'a'*40)=='a'*40


@pytest.mark.parametrize('hidden_flag',['--assume-unchanged','--skip-worktree'])
def test_source_rejects_hidden_tracked_modification(tmp_path,monkeypatch,hidden_flag):
    import subprocess
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=tmp_path,text=True)
    git('init','-q');git('config','user.email','test@example.invalid');git('config','user.name','Test')
    path=tmp_path/'pyproject.toml';path.write_text('version = "2.2.0"\n')
    git('add','.');git('commit','-qm','baseline')
    git('update-index',hidden_flag,'pyproject.toml')
    path.write_text('version = "9.9.9"\n')
    assert not git('status','--porcelain').strip()
    monkeypatch.setattr(module,'ROOT',tmp_path)
    with pytest.raises(ValueError,match='HEAD'):
        module.source_snapshot()


def test_upload_snapshot_uses_verified_manifest_hashes(tmp_path):
    import json,hashlib
    directory=tmp_path/'source';directory.mkdir();stage=tmp_path/'upload';stage.mkdir()
    path=directory/'electridrive_2.2.0_amd64.deb';path.write_bytes(b'verified')
    manifest={'assets':[{'name':path.name,'size':8,'sha256':module.digest(path)}]}
    raw=json.dumps(manifest).encode()
    files,expected=module.stage_uploads(directory,manifest,raw,stage)
    path.write_bytes(b'replaced after validation')
    assert files[0].read_bytes()==b'verified'
    assert expected[path.name]==hashlib.sha256(b'verified').hexdigest()
    assert expected['candidate-manifest.json']==hashlib.sha256(raw).hexdigest()
