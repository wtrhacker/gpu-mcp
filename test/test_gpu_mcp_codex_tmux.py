"""Old tmux panes keep Ctrl+G without VISUAL, EDITOR or a Codex wrapper."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest

import gpu_mcp_codex_tmux as popup
from gpu_mcp_codex_terminal_setup import install, plan, tmux_binding
from gpu_mcp_codex_launcher import request
from gpu_mcp_codex_state import Origin
from test_gpu_mcp_codex_transport import backend, record_for

pytestmark = pytest.mark.contract
SOURCE = Path(__file__).resolve().parents[1]


def test_setup_preserves_byobu_and_tmux_settings_and_is_idempotent(tmp_path):
    (tmp_path / '.byobu').mkdir()
    normal, byobu = tmp_path / '.tmux.conf', tmp_path / '.byobu/.tmux.conf'
    normal.write_text('set -g prefix C-a\n')
    byobu.write_text('set -g mouse on\n')
    first = install(tmp_path, SOURCE, sys.executable, 'bash', with_tmux=True)
    assert len(first['changed_files']) == 5
    assert normal.read_text().startswith('set -g prefix C-a\n')
    assert byobu.read_text().startswith('set -g mouse on\n')
    assert 'codex-terminal-tmux.conf' in normal.read_text() and 'codex-terminal-tmux.conf' in byobu.read_text()
    assert install(tmp_path, SOURCE, sys.executable, 'bash', with_tmux=True)['changed_files'] == []


def test_existing_ctrl_g_binding_is_not_overwritten(tmp_path):
    path = tmp_path / '.tmux.conf'
    original = 'bind-key -n C-g display-message "my shortcut"\n'
    path.write_text(original)
    with pytest.raises(ValueError, match='existing tmux Ctrl'):
        install(tmp_path, SOURCE, sys.executable, 'bash', with_tmux=True)
    assert path.read_text() == original
    assert not (tmp_path / '.config').exists()


def test_popup_refuses_an_agent_style_caller_without_reading_jobs(monkeypatch):
    monkeypatch.setattr(popup.sys.stdin, 'isatty', lambda:False)
    monkeypatch.setattr(popup, 'pane_info', lambda *a:pytest.fail('should not inspect a pane'))
    assert popup.main(['--popup','--pane','%1','--frontend','123','--start','42']) == 1


def test_other_foreground_applications_receive_ctrl_g(monkeypatch):
    calls=[]
    monkeypatch.setattr(popup,'pane_info',lambda pane:(123,'/fixture'))
    monkeypatch.setattr(popup,'frontend',lambda pid:None)
    monkeypatch.setattr(popup,'tmux',lambda *args,**kwargs:calls.append(args))
    assert popup.dispatch('%4')['action'] == 'passthrough'
    assert calls == [('send-keys','-t','%4','C-g')]


def test_configured_native_editor_keeps_the_original_pane_handoff(monkeypatch):
    calls = []
    monkeypatch.setattr(popup, 'pane_info', lambda pane: (123, '/fixture'))
    monkeypatch.setattr(popup, 'frontend', lambda pid: {'pid': 456, 'pgid': 456, 'start': '1'})
    monkeypatch.setattr(popup, 'native_monitor', lambda pid: True)
    monkeypatch.setattr(popup, 'tmux', lambda *args, **kw: calls.append(args))
    assert popup.dispatch('%4')['action'] == 'native_editor'
    assert calls == [('send-keys', '-t', '%4', 'C-g')]


def make_proc(root, pid, *, exe, group, foreground, children=(), argv=()):
    directory = root / str(pid)
    (directory / 'task' / str(pid)).mkdir(parents=True)
    fields = ['S','1',str(group),str(group),'1',str(foreground),*(['0']*13),'12345']
    (directory / 'stat').write_text(f'{pid} (fixture) ' + ' '.join(fields))
    (directory / 'exe').symlink_to('/fixture/' + exe)
    (directory / 'cmdline').write_bytes(b'\0'.join([exe.encode(),*argv]))
    (directory / 'task' / str(pid) / 'children').write_text(' '.join(map(str,children)))


def test_npm_foreground_child_is_recognized_but_other_node_programs_are_not(tmp_path):
    make_proc(tmp_path,10,exe='bash',group=10,foreground=20)
    make_proc(tmp_path,20,exe='node',group=20,foreground=20,children=[30])
    make_proc(tmp_path,30,exe='codex',group=20,foreground=20,argv=[b'resume'])
    assert popup.frontend(10,tmp_path)['pid'] == 30
    (tmp_path/'30/cmdline').write_bytes(b'codex\0exec-server\0')
    assert popup.frontend(10,tmp_path) is None
    (tmp_path/'20/task/20/children').write_text('')
    assert popup.frontend(10,tmp_path) is None


@pytest.mark.skipif(not shutil.which('tmux') or not shutil.which('cc') or not shutil.which('node'),
                    reason='tmux, a C compiler and node are required for the real terminal fixture')
@pytest.mark.parametrize('npm', [False, True, pytest.param('native', marks=pytest.mark.skipif(
    os.environ.get('GPU_MCP_RUN_NATIVE_TMUX_TEST') != '1', reason='explicit native Codex UI opt-in required'))])
def test_existing_pane_without_editor_opens_real_popup_and_preserves_input(backend, tmp_path, monkeypatch, npm):
    policy, base, info = backend
    record_for(policy, dict(info,state_base=str(base)), Origin(str(uuid.uuid4())), label='popup-private-task')
    before = request(info,'/human/snapshot',human=True)
    prefix = ['tmux','-L','gpu-mcp-popup-' + uuid.uuid4().hex]
    env = {k:v for k,v in os.environ.items() if k not in {'VISUAL','EDITOR','GPU_MCP_CODEX_EDITOR_ENABLED','GPU_MCP_PROMPT_EDITOR','TMUX','TMUX_PANE'}}
    env['TERM'] = 'xterm-256color'
    source, exe, received = tmp_path/'frontend.c',tmp_path/'codex',tmp_path/'input.bin'
    source.write_text('''#include <unistd.h>
#include <stdio.h>
#include <fcntl.h>
#include <termios.h>
int main(int argc,char**argv){struct termios t;tcgetattr(0,&t);cfmakeraw(&t);tcsetattr(0,TCSANOW,&t);
int fd=open(argv[1],O_WRONLY|O_CREAT|O_APPEND,0600);write(1,"UNCHANGED_DRAFT\\r\\n",17);
char b[256];int n;while((n=read(0,b,sizeof b))>0){write(fd,b,n);write(1,b,n);}return 0;}
''')
    subprocess.run(['cc',str(source),'-o',str(exe)],check=True,capture_output=True)
    argv = [str(exe),str(received)]
    if npm == 'native':
        env.pop('CODEX_HOME',None)
        argv = [shutil.which('codex'),'--no-daemon','--no-alt-screen','--cd',str(policy.parent),
                '--model','gpt-5.5','-c','model_reasoning_effort="medium"','-c','features.hooks=false',
                '-c',f'projects.{json.dumps(str(policy.parent))}.trust_level="trusted"']
    elif npm:
        wrapper = tmp_path/'launcher.js'
        wrapper.write_text('const {spawn}=require("child_process");const p=spawn(process.argv[2],process.argv.slice(3),{stdio:"inherit"});p.on("exit",c=>process.exit(c||0));')
        argv = [shutil.which('node'),str(wrapper),*argv]
    def mux(*args):
        return subprocess.run([*prefix,*args],env=env,text=True,capture_output=True,check=True,timeout=5).stdout.strip()
    master = slave = None
    attached = None
    capture = bytearray()
    try:
        mux('-f','/dev/null','new-session','-d','-s','fixture','-c',str(policy.parent),'exec '+shlex.join(argv))
        pane = mux('display-message','-p','#{pane_id}')
        socket_path = mux('display-message','-p','#{socket_path}')
        server_pid = mux('display-message','-p','#{pid}')
        monkeypatch.setenv('TMUX',f'{socket_path},{server_pid},0')
        # Install only after the frontend is already running without editor vars.
        root = tmp_path / "runtime with ' quote"
        root.mkdir()
        for name in ('gpu_mcp_codex_tmux.py','gpu_mcp_codex_editor.py'):
            (root/name).symlink_to(SOURCE/name)
        home = tmp_path/'shell-home'
        install(home, root, sys.executable, 'bash', with_tmux=True, activate_tmux=True)
        config = tmp_path/'binding.conf'
        config.write_text(tmux_binding(root,sys.executable,state_base=base))
        mux('source-file',str(config))
        proof = popup.dispatch(pane,probe=True)
        deadline = time.monotonic() + 5
        while proof['action'] != 'popup' and time.monotonic() < deadline:
            time.sleep(.05)
            proof = popup.dispatch(pane,probe=True)
        assert proof['action'] == 'popup'
        fields = dict(v.split(b'=',1) for v in (Path('/proc')/str(proof['frontend']['pid'])/'environ').read_bytes().split(b'\0') if b'=' in v)
        assert b'VISUAL' not in fields and b'EDITOR' not in fields
        master, slave = pty.openpty()
        fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,150,0,0))
        attached = subprocess.Popen([*prefix,'attach-session','-t','fixture'],stdin=slave,stdout=slave,stderr=slave,env=env)
        os.close(slave)
        slave = None
        def await_text(needle,start=0):
            deadline=time.monotonic()+8
            while time.monotonic()<deadline:
                if select.select([master],[],[],.1)[0]:
                    chunk=os.read(master,65536)
                    capture.extend(chunk)
                if needle in capture[start:]: return
            raise AssertionError(capture[start:].decode(errors='replace')[-6000:])
        await_text(b'GPT-5.5 medium' if npm == 'native' else b'UNCHANGED_DRAFT')
        # Put Codex in the bottom-right of three splits, with a top status bar
        # as in Byobu. The old 95%-of-client popup wrongly covered both neighbors.
        neighbor = shlex.join([sys.executable, '-c', "import time; print('NEIGHBOR_SENTINEL',flush=True); time.sleep(3600)"])
        mux('set-option', '-g', 'status-position', 'top')
        mux('split-window', '-h', '-b', '-d', '-t', pane, neighbor)
        mux('split-window', '-v', '-b', '-d', '-t', pane, neighbor)
        mux('select-pane', '-t', pane)
        width, height = map(int, mux('display-message', '-p', '-t', pane, '#{pane_width} #{pane_height}').split())
        if npm == 'native':
            os.write(master,b'DRAFT_SENTINEL')
            await_text(b'DRAFT_SENTINEL')
        os.write(master,b'\x07')
        await_text(b'GPU MCP')
        viewers = []
        for pid in (Path('/proc') / server_pid / 'task' / server_pid / 'children').read_text().split():
            try:
                if b'--popup' in (Path('/proc') / pid / 'cmdline').read_bytes().split(b'\0'):
                    viewers.append(pid)
            except OSError:
                pass
        assert len(viewers) == 1
        with (Path('/proc') / viewers[0] / 'fd/0').open('rb', buffering=0) as terminal:
            rows, columns, _, _ = struct.unpack('HHHH', fcntl.ioctl(terminal, termios.TIOCGWINSZ, bytes(8)))
        assert (columns, rows) == (width - 2, height - 2), 'popup escaped its selected split'
        os.write(master,b'\r')
        await_text(b'GPU MCP Monitor')
        await_text(b'Jobs: 1/1')
        os.write(master,b'\x1bOC')
        await_text(b'popup-private-task')
        os.write(master,b'\x1bOC')
        await_text(b'popup-private-task private log')
        if npm == 'native':
            underlying=mux('capture-pane','-p','-t',pane)
            assert 'DRAFT_SENTINEL' in underlying and 'popup-private-task' not in underlying
        closed_at = len(capture)
        os.write(master,b'\x1b')
        await_text(b'DRAFT_SENTINEL' if npm == 'native' else b'UNCHANGED_DRAFT', closed_at)
        os.write(master,b'KEPT_INPUT')
        await_text(b'KEPT_INPUT')
        if npm == 'native':
            underlying=mux('capture-pane','-p','-t',pane)
            assert 'DRAFT_SENTINELKEPT_INPUT' in underlying and 'popup-private-task' not in underlying
        else:
            assert received.read_bytes() == b'KEPT_INPUT', 'popup input/data reached the underlying frontend'
        after=request(info,'/human/snapshot',human=True)
        assert after['jobs'] == before['jobs'] and after['events'] == before['events']
    finally:
        if capture:
            (tmp_path/'popup.ansi').write_bytes(capture)
        subprocess.run([*prefix,'kill-server'],env=env,capture_output=True,timeout=5)
        if attached is not None:
            attached.wait(timeout=5)
        for fd in (master,slave):
            if fd is not None: os.close(fd)
