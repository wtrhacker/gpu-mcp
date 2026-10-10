"""Private library-temporary cleanup without broader delete/execute grants."""

from pathlib import Path
import json
import os
import subprocess
import sys
import textwrap

import pytest
import gpu_mcp_guard as guard


ROOT = Path(__file__).resolve().parents[1]


def run_worker(tmp_path, source, helper=None, *, environment=None):
    repo = tmp_path/'repo'
    jobs, results = repo/'jobs', repo/'results'
    jobs.mkdir(parents=True)
    results.mkdir()
    (jobs/'job.py').write_text(textwrap.dedent(source))
    if helper:
        (jobs/'library_helper.py').write_text(textwrap.dedent(helper))
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
        (root.parent/'result.json').write_text(json.dumps({'root':str(root),'ok':True}))
        print(json.dumps({'root':str(root)}))
    ''')
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
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
        assert root.parent.name == 'results'
    ''', environment={'GPU_MCP_TEMP_ROOT':str(outside),'TMPDIR':str(outside)})
    assert result.returncode == 0, result.stderr
