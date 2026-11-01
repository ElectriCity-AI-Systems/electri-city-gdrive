#!/usr/bin/env python3
"""Record exact candidate provenance and prepare a CI-gated draft; never publish."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import os
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'ElectriCity-AI-Systems/electri-city-gdrive'
REQUIRED_JOBS = {f'test ({v})' for v in ('3.10', '3.11', '3.12', '3.13', '3.14')} | {
    'debian-package (ubuntu-22.04)', 'debian-package (ubuntu-24.04)'}


def command(*args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, text=True, **kwargs).strip()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def source_snapshot():
    if command('git', 'status', '--porcelain', '--untracked-files=normal'):
        raise ValueError('Candidate source must be committed and clean')
    fingerprints = {}
    object_format = command('git','rev-parse','--show-object-format')
    for entry in command('git','ls-tree','-rz','--full-tree','HEAD').split('\0'):
        if not entry: continue
        metadata,name = entry.split('\t',1)
        mode,kind,expected = metadata.split()
        path = ROOT/name
        if kind != 'blob' or path.is_symlink() or not path.is_file():
            raise ValueError(f'Working source is not a regular HEAD file: {name}')
        payload = path.read_bytes()
        actual = hashlib.new(object_format,b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()
        executable = bool(path.stat().st_mode & 0o111)
        if actual != expected or executable != (mode == '100755'):
            raise ValueError(f'Working source differs from HEAD: {name}')
        fingerprints[name] = hashlib.sha256(payload).hexdigest()
    version = re.search(r'^version\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"',
                        (ROOT/'pyproject.toml').read_text(), re.M).group(1)
    return {'version':version, 'commit':command('git','rev-parse','HEAD'),
            'source_sha256':fingerprints}


def validate_tag_lines(output, tag, commit):
    refs = dict(line.split(None,1)[::-1] for line in output.splitlines() if line.strip())
    ref = 'refs/tags/'+tag
    actual = refs.get(ref+'^{}',refs.get(ref))
    if actual is not None and actual != commit:
        raise ValueError('Existing remote tag does not resolve to the candidate commit')
    return actual


def validate_remote_tag(tag, commit):
    output = command('git','ls-remote','--tags',f'https://github.com/{REPOSITORY}.git',
                     'refs/tags/'+tag,'refs/tags/'+tag+'^{}')
    return validate_tag_lines(output,tag,commit)


def stage_uploads(directory, manifest, manifest_bytes, destination):
    files,expected = [],{}
    for entry in manifest['assets']:
        name=entry['name']
        if name != Path(name).name:
            raise ValueError('Unsafe upload name')
        target=destination/name
        fd=os.open(Path(directory)/name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        h=hashlib.sha256();size=0
        with os.fdopen(fd,'rb') as source, target.open('xb') as out:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError('Upload source is not a regular file')
            for block in iter(lambda:source.read(1024*1024),b''):
                h.update(block);size+=len(block);out.write(block)
        if size!=entry['size'] or h.hexdigest()!=entry['sha256']:
            raise ValueError('Asset changed while preparing immutable upload snapshot')
        files.append(target);expected[name]=entry['sha256']
    target=destination/'candidate-manifest.json'
    target.write_bytes(manifest_bytes)
    files.append(target);expected[target.name]=hashlib.sha256(manifest_bytes).hexdigest()
    return files,expected


def validate_ci(run, jobs, commit):
    if (run.get('head_sha') != commit or run.get('name') != 'CI' or
            run.get('status') != 'completed' or run.get('conclusion') != 'success'):
        raise ValueError('CI must pass on the exact candidate commit')
    observed = {job['name']:job.get('conclusion') for job in jobs}
    if any(observed.get(name) != 'success' for name in REQUIRED_JOBS):
        raise ValueError('All seven required CI jobs must pass')


def validate_assets(directory, assets, version=None):
    if version is not None and {entry['name'] for entry in assets} != {
            f'electridrive_{version}_amd64.deb','ElectriDrive-x86_64.AppImage','SHA256SUMS.txt'}:
        raise ValueError('Candidate manifest must contain the complete asset set')
    names = set()
    for entry in assets:
        name = entry['name']
        if (name != Path(name).name or name in names or not re.fullmatch(
                r'(electridrive_\d+\.\d+\.\d+_amd64\.deb|ElectriDrive-x86_64\.AppImage|SHA256SUMS\.txt)',name)):
            raise ValueError('Unexpected candidate asset')
        names.add(name)
        path = Path(directory)/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
            raise ValueError(f'Candidate asset changed: {name}')


def verify_candidate(directory, version):
    package=Path(directory)/f'electridrive_{version}_amd64.deb'
    command('python3','scripts/verify_deb.py',str(package),'--expected-version',version,
            '--max-glibc','2.35','--require-baked-client')
    for path in (package,Path(directory)/'ElectriDrive-x86_64.AppImage'):
        command('python3','scripts/verify_customer_artifact.py',str(path),
                '--expected-channel','CUSTOMER-CANDIDATE','--max-glibc','2.35')


def record_candidate(path, before):
    after=source_snapshot()
    if after != before: raise ValueError('Source changed during the candidate build')
    directory=path.parent
    verify_candidate(directory,after['version'])
    names=['ElectriDrive-x86_64.AppImage',f"electridrive_{after['version']}_amd64.deb",'SHA256SUMS.txt']
    assets=[{'name':name,'sha256':digest(directory/name),'size':(directory/name).stat().st_size} for name in names]
    validate_assets(directory,assets,version=after['version'])
    checksums=(directory/'SHA256SUMS.txt').read_text().splitlines()
    expected={f"{item['sha256']}  {item['name']}" for item in assets if item['name']!='SHA256SUMS.txt'}
    if set(checksums)!=expected: raise ValueError('SHA256SUMS does not match candidate assets')
    path.write_text(json.dumps(dict(after,assets=assets,channel='CUSTOMER-CANDIDATE',max_glibc='2.35'),indent=2)+'\n')


def release_command(version, commit, paths, notes):
    return ['gh','release','create','v'+version,*map(str,paths),'--repo',REPOSITORY,
            '--draft','--target',commit,'--title',f'ElectriDrive {version} — release candidate',
            '--notes-file',str(notes)]


def api(path):
    return json.loads(command('gh','api',f'repos/{REPOSITORY}/{path}'))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--snapshot',type=Path)
    mode.add_argument('--record',type=Path)
    parser.add_argument('--source-record',type=Path)
    parser.add_argument('--manifest',type=Path,default=ROOT/'dist/candidate-manifest.json')
    parser.add_argument('--ci-run',type=int)
    parser.add_argument('--notes',type=Path,default=ROOT/'packaging/release-2.2.0.md')
    args=parser.parse_args(argv)
    if args.snapshot:
        args.snapshot.write_text(json.dumps(source_snapshot(),indent=2)+'\n');return
    if args.record:
        if not args.source_record: parser.error('--record requires --source-record')
        record_candidate(args.record,json.loads(args.source_record.read_text()));return
    if not args.ci_run: parser.error('--ci-run is required to create a draft')
    manifest_bytes=args.manifest.read_bytes()
    manifest=json.loads(manifest_bytes);current=source_snapshot()
    if any(manifest.get(key)!=value for key,value in current.items()):
        raise ValueError('Manifest does not match current committed source')
    if manifest.get('channel')!='CUSTOMER-CANDIDATE' or manifest.get('max_glibc')!='2.35':
        raise ValueError('Only verified customer candidates may be staged')
    directory=args.manifest.parent
    validate_assets(directory,manifest['assets'],version=current['version']);verify_candidate(directory,current['version'])
    validate_ci(api(f'actions/runs/{args.ci_run}'),api(f'actions/runs/{args.ci_run}/jobs?per_page=100')['jobs'],current['commit'])
    latest=api('releases/latest')['tag_name'].removeprefix('v')
    if tuple(map(int,current['version'].split('.'))) <= tuple(map(int,latest.split('.'))):
        raise ValueError('New candidate version must be newer than Latest')
    tag='v'+current['version']
    validate_remote_tag(tag,current['commit'])
    if any(item['tag_name']==tag for item in api('releases?per_page=100')):
        raise ValueError('Release already exists; inspect the existing draft instead of overwriting it')
    with tempfile.TemporaryDirectory(prefix='electridrive-verified-upload-') as temporary:
        assets,expected=stage_uploads(directory,manifest,manifest_bytes,Path(temporary))
        result=command(*release_command(current['version'],current['commit'],assets,args.notes))
    release=next(item for item in api('releases?per_page=100') if item['tag_name']==tag)
    if not release['draft'] or release['target_commitish']!=current['commit']:
        raise ValueError('Draft target verification failed')
    validate_remote_tag(tag,current['commit'])
    uploaded={item['name']:item.get('digest') for item in release['assets']}
    if uploaded!={name:'sha256:'+sha for name,sha in expected.items()}:
        raise ValueError('Uploaded draft assets do not match verified manifest bytes')
    print(result)



if __name__=='__main__': main()
