"""Private library-temporary cleanup without broader delete/execute grants."""

from pathlib import Path
import json
import fcntl
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess
import sys
import socket
import time
import textwrap

import pytest
import gpu_mcp_guard as guard


ROOT = Path(__file__).resolve().parents[1]


def run_worker(tmp_path, source, helper=None, *, environment=None, prepare=None):
    repo = tmp_path/'repo'
    jobs, results = repo/'jobs', repo/'results'
    jobs.mkdir(parents=True)
    results.mkdir()
    (jobs/'job.py').write_text(textwrap.dedent(source))
    if helper:
        (jobs/'library_helper.py').write_text(textwrap.dedent(helper))
    if prepare:
        prepare(repo)
    command = [sys.executable, '-B', str(ROOT/'gpu_mcp_safe_runner.py'),
               '--job', str(jobs/'job.py'), '--repo-root', str(repo),
               '--script-roots', json.dumps([str(jobs)]), '--write-roots', json.dumps([str(results)])]
    env = {**os.environ, 'PYTHONPATH': str(ROOT), **(environment or {})}
    result = subprocess.run(command, text=True, capture_output=True, env=env, timeout=20)
    return repo, result


def test_default_tempfile_probe_and_cleanup_work(tmp_path):
    repo, result = run_worker(tmp_path, '''
        import json, os, tempfile
        from pathlib import Path
        tempfile.tempdir = None
        root = Path(tempfile.gettempdir())
        assert str(root) == os.environ['GPU_MCP_TEMP_ROOT']
        assert root.stat().st_mode & 0o777 == 0o700
        with tempfile.TemporaryDirectory() as folder:
            nested = Path(folder)/'nested'
            nested.mkdir()
            (nested/'value.txt').write_text('temporary')
        assert not Path(folder).exists()
        with tempfile.NamedTemporaryFile() as handle:
            name = handle.name
            handle.write(b'probe')
        assert not Path(name).exists()
        (root.parents[2]/'result.json').write_text(json.dumps({'root':str(root),'ok':True}))
        print(json.dumps({'root':str(root)}))
    ''')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert Path(data['root']).parent == repo/'results/.gpu_mcp_tmp'/socket.gethostname()
    assert not Path(data['root']).exists()
    assert json.loads((repo/'results/result.json').read_text())['ok']


def test_library_atomic_replace_and_descriptor_cleanup(tmp_path):
    _, result = run_worker(tmp_path, '''
        import library_helper
        library_helper.exercise()
    ''', '''
        import os, tempfile
        from pathlib import Path
        def exercise():
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root/'new').write_text('new')
                (root/'old').write_text('old')
                fd = os.open(root, os.O_RDONLY)
                try:
                    os.replace('new','old',src_dir_fd=fd,dst_dir_fd=fd)
                    assert (root/'old').read_text() == 'new'
                    os.unlink('old',dir_fd=fd)
                finally:
                    os.close(fd)
    ''')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('event', ['os.remove','os.rmdir','shutil.rmtree'])
def test_non_temporary_project_paths_stay_protected(tmp_path, event):
    scratch, data = tmp_path/'private', tmp_path/'project-data'
    scratch.mkdir()
    data.mkdir()
    hook = guard.make_safe_run_audit_hook([tmp_path], temporary_root=scratch)
    hook(event, (str(scratch/'item'), -1))
    with pytest.raises(PermissionError):
        hook(event, (str(data/'item'), -1))


@pytest.mark.parametrize('source_inside,dest_inside', [(True,False),(False,True),(False,False)])
def test_rename_requires_both_ends_inside(tmp_path, source_inside, dest_inside):
    scratch, outside = tmp_path/'private', tmp_path/'outside'
    scratch.mkdir()
    outside.mkdir()
    hook = guard.make_safe_run_audit_hook([tmp_path], temporary_root=scratch)
    source = (scratch if source_inside else outside)/'source'
    destination = (scratch if dest_inside else outside)/'destination'
    with pytest.raises(PermissionError):
        hook('os.rename', (str(source),str(destination),-1,-1))


def test_descriptor_and_symlink_escape_denied(tmp_path):
    scratch, outside = tmp_path/'private', tmp_path/'outside'
    scratch.mkdir()
    outside.mkdir()
    (outside/'victim').write_text('keep')
    (scratch/'link').symlink_to(outside, target_is_directory=True)
    hook = guard.make_safe_run_audit_hook([tmp_path], temporary_root=scratch)
    fd = os.open(outside, os.O_RDONLY)
    try:
        for path, directory in [('victim',fd),(scratch/'link/victim',-1),(scratch/'../outside/victim',-1),
                                (scratch/'link/../outside/victim',-1),(scratch/'..',-1)]:
            with pytest.raises(PermissionError):
                hook('os.remove',(path,directory))
    finally:
        os.close(fd)
    # Removing the link itself cannot remove its target.
    hook('os.remove',(scratch/'link',-1))
    assert (outside/'victim').read_text() == 'keep'


def test_environment_does_not_extend_permission(tmp_path, monkeypatch):
    scratch, outside = tmp_path/'private', tmp_path/'outside'
    scratch.mkdir()
    outside.mkdir()
    hook = guard.make_safe_run_audit_hook([tmp_path], temporary_root=scratch)
    monkeypatch.setenv('GPU_MCP_TEMP_ROOT',str(outside))
    with pytest.raises(PermissionError):
        hook('os.remove',(outside/'file',-1))


def test_unrelated_restrictions_unchanged(tmp_path):
    scratch = tmp_path/'private'
    scratch.mkdir()
    hook = guard.make_safe_run_audit_hook([tmp_path], temporary_root=scratch)
    for event, args in [('subprocess.Popen',('/bin/true',[],None,None)),
                        ('os.system',('true',)),('os.chmod',(scratch/'x',0o777,-1)),
                        ('os.symlink',('source',str(scratch/'link'),-1))]:
        with pytest.raises(PermissionError):
            hook(event,args)
    assert guard.scan_python_source_safety(Path('bad.py'),'import os\nos.unlink("x")\n')


def test_no_temporary_grant_keeps_old_restriction(tmp_path):
    hook = guard.make_safe_run_audit_hook([tmp_path])
    with pytest.raises(PermissionError):
        hook('os.remove',(tmp_path/'file',-1))


@pytest.mark.parametrize('outside', [False,True])
def test_temporary_grant_cannot_cover_an_entire_write_root_or_escape_it(tmp_path, outside):
    approved = tmp_path/'approved'
    approved.mkdir()
    root = tmp_path if outside else approved
    with pytest.raises(ValueError, match='strict child'):
        guard.make_safe_run_audit_hook([approved], temporary_root=root)


def test_global_temporary_directory_finalizer_runs_before_job_cleanup(tmp_path):
    _, result = run_worker(tmp_path, '''
        import json, os, tempfile
        from pathlib import Path
        holder = tempfile.TemporaryDirectory()
        (Path(holder.name)/'module.py').write_text('value = 1')
        print(json.dumps({'root':os.environ['GPU_MCP_TEMP_ROOT']}))
    ''')
    assert result.returncode == 0, result.stderr
    assert 'Exception ignored' not in result.stderr
    assert not Path(json.loads(result.stdout)['root']).exists()


def test_imported_helper_cannot_delete_project_output(tmp_path):
    repo, result = run_worker(tmp_path, '''
        from pathlib import Path
        import library_helper
        path = Path(__file__).parent.parent/'results/keep.txt'
        path.write_text('keep')
        library_helper.delete(path)
    ''', '''
        import os
        def delete(path):
            os.unlink(path)
    ''')
    assert result.returncode != 0
    assert 'GPU MCP blocked runtime event: os.remove' in result.stderr
    assert (repo/'results/keep.txt').read_text() == 'keep'
    assert 'retained temporary files' in result.stderr


def test_caller_cannot_choose_its_temporary_permission(tmp_path):
    outside = tmp_path/'caller-chosen'
    outside.mkdir()
    _, result = run_worker(tmp_path, '''
        import os
        from pathlib import Path
        root = Path(os.environ['GPU_MCP_TEMP_ROOT'])
        assert root.name.startswith('.gpu-mcp-tmp-')
        assert root.parent.parent.name == '.gpu_mcp_tmp'
        assert root.parents[2].name == 'results'
    ''', environment={'GPU_MCP_TEMP_ROOT':str(outside),'TMPDIR':str(outside)})
    assert result.returncode == 0, result.stderr


def scratch_record(container, label, *, pid=99999999, **changes):
    folder = container / f'.gpu-mcp-tmp-{pid}-{label}'
    folder.mkdir()
    (folder / 'cache').write_text('temporary cache')
    owner = {'schema_version': 1, 'kind': 'job', 'host': socket.gethostname(), 'pid': pid,
             'process_start': '1', 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
             'created_at': time.time()-guard.TEMPORARY_RETENTION_SEC-10}
    owner.update(changes)
    (folder / guard.TEMPORARY_OWNER_FILE).write_text(json.dumps(owner))
    return folder


def test_expired_failed_and_crashed_scratch_is_removed_but_results_remain(tmp_path):
    container = guard._temporary_container(tmp_path)
    expired = scratch_record(container, 'expired', finished_at=time.time()-guard.TEMPORARY_RETENTION_SEC-1)
    crashed = scratch_record(container, 'crashed')
    recent = scratch_record(container, 'recent', finished_at=time.time()-60)
    output = tmp_path / 'result.json'
    output.write_text('scientific result')
    assert set(guard._prune_temporary_roots(container)) == {expired.name, crashed.name}
    assert recent.is_dir() and output.read_text() == 'scientific result'


def test_live_owner_and_locked_scratch_survive_even_after_retention(tmp_path):
    container = guard._temporary_container(tmp_path)
    live = scratch_record(container, 'live', pid=os.getpid(), process_start=guard._temporary_process_start(os.getpid()))
    locked = scratch_record(container, 'locked')
    with (locked / guard.TEMPORARY_OWNER_FILE).open('r+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert guard._prune_temporary_roots(container) == []
        assert live.is_dir() and locked.is_dir()
    assert guard._prune_temporary_roots(container) == [locked.name]
    assert live.is_dir()


def test_other_hosts_unknown_owners_and_symlinks_are_not_cleaned(tmp_path):
    container = guard._temporary_container(tmp_path)
    foreign = scratch_record(container, 'foreign', host='another-execution-host')
    unknown = scratch_record(container, 'unknown', boot_id=None)
    unmanaged = container / '.gpu-mcp-tmp-123-unmanaged'
    unmanaged.mkdir()
    outside = tmp_path / 'outputs'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    (container / '.gpu-mcp-tmp-124-link').symlink_to(outside, target_is_directory=True)
    assert guard._prune_temporary_roots(container) == []
    assert all(p.is_dir() for p in (foreign, unknown, unmanaged))
    assert (outside / 'keep').read_text() == 'keep'


def test_unknown_process_inspection_keeps_scratch(tmp_path, monkeypatch):
    container = guard._temporary_container(tmp_path)
    unknown = scratch_record(container, 'unknown')
    monkeypatch.setattr(guard, '_temporary_process_start', lambda pid: (_ for _ in ()).throw(PermissionError('unreadable process')))
    assert guard._prune_temporary_roots(container) == [] and unknown.is_dir()


def test_cleaner_respects_directory_replacement_before_delete(tmp_path, monkeypatch):
    container = guard._temporary_container(tmp_path)
    retired = scratch_record(container, 'replaced')
    original = container / 'retired-original'
    check = guard._temporary_owner_gone
    def swap(owner):
        result = check(owner)
        retired.rename(original)
        retired.mkdir()
        (retired / 'keep').write_text('replacement')
        return result
    monkeypatch.setattr(guard, '_temporary_owner_gone', swap)
    assert guard._prune_temporary_roots(container) == []
    assert (retired / 'keep').read_text() == 'replacement' and (original / 'cache').is_file()


def test_temporary_container_symlink_is_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    approved = tmp_path / 'approved'
    approved.mkdir()
    (approved / guard.TEMPORARY_CONTAINER).symlink_to(outside, target_is_directory=True)
    with pytest.raises(PermissionError, match='real directory'):
        guard._temporary_container(approved)
    assert list(outside.iterdir()) == []


def test_verified_migrations_get_seven_days_from_the_move(tmp_path):
    container = guard._temporary_container(tmp_path)
    now = time.time()
    old = scratch_record(container, 'migrated', kind='migrated', created_at=now, finished_at=now)
    assert guard._prune_temporary_roots(container, now=now+guard.TEMPORARY_RETENTION_SEC-1) == []
    assert guard._prune_temporary_roots(container, now=now+guard.TEMPORARY_RETENTION_SEC) == [old.name]


def test_two_collectors_do_not_remove_a_folder_twice(tmp_path):
    container = guard._temporary_container(tmp_path)
    expired = scratch_record(container, 'concurrent')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: guard._prune_temporary_roots(container), range(2)))
    assert [item for result in results for item in result] == [expired.name]


def test_malformed_and_nonregular_owner_records_stay_untouched(tmp_path):
    container = guard._temporary_container(tmp_path)
    malformed = scratch_record(container, 'malformed')
    (malformed / guard.TEMPORARY_OWNER_FILE).write_text('{broken JSON')
    giant = scratch_record(container, 'giant', created_at=10**1000)
    fifo = scratch_record(container, 'fifo')
    (fifo / guard.TEMPORARY_OWNER_FILE).unlink()
    os.mkfifo(fifo / guard.TEMPORARY_OWNER_FILE)
    assert guard._prune_temporary_roots(container) == []
    assert all((p / 'cache').is_file() for p in (malformed, giant, fifo))


def test_actual_worker_collects_expired_scratch_before_running_its_guard(tmp_path):
    saved = {}
    def prepare(repo):
        container = guard._temporary_container(repo / 'results')
        saved['expired'] = scratch_record(container, 'expired')
        saved['live'] = scratch_record(container, 'live', pid=os.getpid(),
                                      process_start=guard._temporary_process_start(os.getpid()))
        (repo / 'results/keep.txt').write_text('ordinary output')
    repo, result = run_worker(tmp_path, '''
        import json, os
        print(json.dumps({'tmp':os.environ['GPU_MCP_TEMP_ROOT']}))
    ''', prepare=prepare)
    assert result.returncode == 0, result.stderr
    assert not saved['expired'].exists() and saved['live'].is_dir()
    assert not Path(json.loads(result.stdout)['tmp']).exists()
    assert (repo / 'results/keep.txt').read_text() == 'ordinary output'


def test_migration_flags_and_forged_identities_never_override_a_live_pid(tmp_path):
    container = guard._temporary_container(tmp_path)
    live = scratch_record(container, 'forged', pid=os.getpid(), kind='migrated', process_start='0',
                          boot_id='00000000-0000-0000-0000-000000000000')
    assert guard._prune_temporary_roots(container) == [] and live.is_dir()


def test_foreign_host_container_cannot_be_cleaned_by_forging_the_marker(tmp_path):
    container = tmp_path / guard.TEMPORARY_CONTAINER / 'other-host'
    container.mkdir(parents=True)
    foreign = scratch_record(container, 'forged-host')
    assert guard._prune_temporary_roots(container) == [] and foreign.is_dir()


def test_swap_after_the_final_stat_never_recursively_deletes_the_replacement(tmp_path, monkeypatch):
    container = guard._temporary_container(tmp_path)
    retired = scratch_record(container, 'late-swap')
    original = container / 'original-cache'
    walk = guard._remove_temporary_contents
    changed = [False]
    def swap(fd):
        if not changed[0]:
            changed[0] = True
            retired.rename(original)
            retired.mkdir()
            (retired / 'ordinary-output').write_text('keep')
        return walk(fd)
    monkeypatch.setattr(guard, '_remove_temporary_contents', swap)
    assert guard._prune_temporary_roots(container) == []
    assert (retired / 'ordinary-output').read_text() == 'keep'


def test_container_swap_before_creation_does_not_write_through_a_symlink(tmp_path, monkeypatch):
    approved, outside = tmp_path / 'approved', tmp_path / 'outside'
    approved.mkdir(); outside.mkdir()
    def swap(container, **kw):
        container.rename(container.with_name('retired-container'))
        container.symlink_to(outside, target_is_directory=True)
        return []
    monkeypatch.setattr(guard, '_prune_temporary_roots', swap)
    with pytest.raises(PermissionError, match='identity changed'):
        guard._prepare_temporary_root([approved])
    assert list(outside.iterdir()) == []


def test_success_cleanup_keeps_a_replacement_directory(tmp_path, monkeypatch):
    jobs, results = tmp_path / 'jobs', tmp_path / 'results'
    jobs.mkdir(); results.mkdir()
    script = jobs / 'job.py'
    script.write_text('pass\n')
    callbacks = []
    monkeypatch.setattr(guard.atexit, 'register', lambda fn: callbacks.append(fn))
    monkeypatch.setattr(guard.sys, 'addaudithook', lambda hook: None)
    monkeypatch.setattr(guard, 'install_preopen_write_guards', lambda roots: None)
    monkeypatch.setattr(guard.sys, 'argv', guard.sys.argv)
    monkeypatch.setattr(guard.tempfile, 'tempdir', guard.tempfile.tempdir)
    monkeypatch.setenv('GPU_MCP_TEMP_ROOT', os.environ.get('GPU_MCP_TEMP_ROOT', ''))
    monkeypatch.setenv('TMPDIR', os.environ.get('TMPDIR', ''))
    assert guard.run_job(str(script), [], repo_root=tmp_path, script_roots=[jobs], write_roots=[results]) == 0
    scratch = Path(os.environ['GPU_MCP_TEMP_ROOT'])
    original = scratch.with_name('original-scratch')
    scratch.rename(original)
    scratch.mkdir()
    (scratch / 'ordinary-output').write_text('keep')
    callbacks[0]()
    assert (scratch / 'ordinary-output').read_text() == 'keep'
