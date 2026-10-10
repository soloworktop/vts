"""Release boundary tests: complete assets, immutable public releases, fail closed."""
import json
from types import SimpleNamespace
import zipfile
import pytest
from scripts import publish_release as release
from scripts.prepare_version import release_version


@pytest.fixture
def bundle(tmp_path):
    info = release_version('v1.2.3-rc.1', 'a' * 40)
    for name in release.expected_assets(info):
        (tmp_path / name).write_bytes(b'fixture-build')
    with zipfile.ZipFile(tmp_path / 'vts-1.2.3rc1-py3-none-any.whl', 'w') as archive:
        archive.writestr('vts-1.2.3rc1.dist-info/METADATA', 'Metadata-Version: 2.4\nName: vts\nVersion: 1.2.3rc1\n')
    return tmp_path, info


def test_incomplete_or_unexpected_assets_fail(bundle):
    directory, info = bundle
    hashes = release.assemble(directory, info)
    assert len(hashes) == 5
    assert all(release.checksum(directory / name) == digest for name, digest in hashes.items())
    (directory / 'extra.exe').write_bytes(b'wrong')
    with pytest.raises(RuntimeError, match='unexpected'):
        release.assemble(directory, info)
    (directory / 'extra.exe').unlink()
    (directory / 'vts-v1.2.3-rc.1-macos-arm64.dmg').unlink()
    with pytest.raises(RuntimeError, match='missing'):
        release.assemble(directory, info)


def test_public_release_is_verified_without_mutation(bundle, monkeypatch):
    directory, info = bundle
    hashes = release.assemble(directory, info)
    monkeypatch.setattr(release.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({'isDraft': False, 'assets': [{'name': name} for name in hashes]})))
    calls = []
    def gh(*args):
        calls.append(args)
        assert args[0] == 'api'
        return info['sha']
    monkeypatch.setattr(release, 'gh', gh)
    checked = []
    monkeypatch.setattr(release, 'verify_remote', lambda *args: checked.append(args))
    release.publish(info['display'], directory, info, 'publish', hashes)
    assert len(calls) == 1 and len(checked) == 1
    monkeypatch.setattr(release, 'verify_remote', lambda *args: (_ for _ in ()).throw(RuntimeError('hash differs')))
    with pytest.raises(RuntimeError, match='hash differs'):
        release.publish(info['display'], directory, info, 'publish', hashes)


def test_tag_move_or_network_failure_never_creates_release(bundle, monkeypatch):
    directory, info = bundle
    hashes = release.assemble(directory, info)
    monkeypatch.setattr(release, 'gh', lambda *args: 'b' * 40)
    with pytest.raises(RuntimeError, match='Tag moved'):
        release.publish(info['display'], directory, info, 'draft', hashes)
    monkeypatch.setattr(release, 'gh', lambda *args: info['sha'])
    monkeypatch.setattr(release.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stderr='network timeout'))
    with pytest.raises(RuntimeError, match='Cannot inspect'):
        release.publish(info['display'], directory, info, 'draft', hashes)


def test_draft_upload_verified_before_publish(bundle, monkeypatch):
    directory, info = bundle
    hashes = release.assemble(directory, info)
    monkeypatch.setattr(release.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stderr='release not found'))
    calls = []
    def gh(*args):
        calls.append(args)
        if args[0] == 'api': return info['sha']
        if args[:2] == ('release', 'view'):
            return json.dumps({'isDraft': True, 'assets': [{'name': name} for name in hashes]})
        return ''
    monkeypatch.setattr(release, 'gh', gh)
    def verify(*args): calls.append(('verified',))
    monkeypatch.setattr(release, 'verify_remote', verify)
    release.publish(info['display'], directory, info, 'publish', hashes)
    assert calls[-2][0] == 'verified' and calls[-1][:2] == ('release', 'edit')
    assert '--draft' in calls[1] and '--prerelease' in calls[1]
    calls.clear()
    monkeypatch.setattr(release, 'verify_remote', lambda *a: (_ for _ in ()).throw(RuntimeError('attachment corrupt')))
    with pytest.raises(RuntimeError, match='corrupt'):
        release.publish(info['display'], directory, info, 'publish', hashes)
    assert not any(call[:2] == ('release', 'edit') for call in calls)
