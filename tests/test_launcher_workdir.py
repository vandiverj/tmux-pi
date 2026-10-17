"""Exercise the real launcher with a stateful fake tmux; no models or live sessions."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent

class LauncherWorkdirTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.a = self.base / "project one's"
        self.b = self.base / "project two"
        self.a.mkdir(); self.b.mkdir()
        self.fake = self.base / "bin"
        self.fake.mkdir()
        self.state = self.base / "state.json"
        self.log = self.base / "log.jsonl"
        tmux = self.fake / "tmux"
        tmux.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p=Path(os.environ['TEST_TMUX_STATE'])
s=json.loads(p.read_text()) if p.exists() else {}
a=sys.argv[1:]
with open(os.environ['TEST_TMUX_LOG'],'a') as f: f.write(json.dumps(a)+'\\n')
def flag(k): return a[a.index(k)+1]
def target(): return flag('-t').lstrip('=').rstrip(':')
cmd=a[0]
if cmd=='has-session': sys.exit(0 if target() in s else 1)
if cmd=='list-sessions':
 for i,name in enumerate(s): print(str(i+1)+' '+name)
if cmd=='show-options': print(s.get(target(),{}).get(a[-1],''))
if cmd=='new-session':
 name=flag('-s'); s[name]={'cwd':flag('-c')}; print('%main')
if cmd=='set-option' and '-t' in a and a[-2].startswith('@'):
 s.setdefault(target(),{})[a[-2]]=a[-1]
if cmd=='split-window': print('%side')
if cmd=='list-panes': pass
p.write_text(json.dumps(s))
''')
        tmux.chmod(0o755)
        for name in ('pi', 'claude'):
            harness = self.fake / name
            harness.write_text('#!/bin/sh\npwd -P > "$TEST_HARNESS_CWD"\n')
            harness.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(self.fake)+os.pathsep+os.environ['PATH'],
                    'TEST_TMUX_STATE':str(self.state), 'TEST_TMUX_LOG':str(self.log),
                    'TEST_HARNESS_CWD':str(self.base/'cwd'), 'TP_NO_ATTACH':'1', 'TP_PI_NODE':''}
        for key in ('TP_SESSION','TMUX','TP_WORKDIR','TP_MAIN_HARNESS'):
            self.env.pop(key,None)
        self.alias = self.fake/'agent'
        self.alias.symlink_to(ROOT/'bin/tp')

    def launch(self, cwd, *args, **env):
        return subprocess.run([str(self.alias),*args],cwd=cwd,env={**self.env,**env},
                              text=True,capture_output=True,timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_current_directory_two_projects_and_reattach(self):
        for cwd,args in ((self.a,()),(self.b,()),(self.a,('--resume',))):
            run=self.launch(cwd,*args)
            self.assertEqual(run.returncode,0,run.stderr)
        sessions=json.loads(self.state.read_text())
        self.assertEqual(len(sessions),2)
        self.assertEqual({s['@tp_workdir'] for s in sessions.values()},{str(self.a),str(self.b)})
        created=[c for c in self.calls() if c[0]=='new-session']
        self.assertEqual(len(created),2)
        self.assertIn('TP_WORKDIR='+str(self.a),created[0])
        self.assertFalse(any(c[0] in ('attach','switch-client') for c in self.calls()))

    def test_explicit_directory_and_symlink_are_same_project(self):
        link=self.base/'alias'; link.symlink_to(self.b,target_is_directory=True)
        self.assertEqual(self.launch(self.a,str(link)).returncode,0)
        self.assertEqual(self.launch(self.b,'--resume').returncode,0)
        self.assertEqual(len(json.loads(self.state.read_text())),1)

    def test_home_legacy_not_reused_for_other_project(self):
        self.state.write_text(json.dumps({'tmux-pi':{'@tp_state_dir':str(self.base/'.tmux-pi')}}))
        run=self.launch(self.a)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(len(json.loads(self.state.read_text())),2)

    def test_same_project_legacy_reused(self):
        self.state.write_text(json.dumps({'tmux-pi':{'@tp_state_dir':str(self.a/'.tmux-pi')}}))
        run=self.launch(self.a,'--resume')
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertFalse(any(c[0]=='new-session' for c in self.calls()))

    def test_explicit_session_other_directory_refused(self):
        self.assertEqual(self.launch(self.a,TP_SESSION='custom').returncode,0)
        run=self.launch(self.b,'--resume',TP_SESSION='custom')
        self.assertNotEqual(run.returncode,0)
        self.assertIn('belongs to',run.stderr)
        self.assertEqual(len(json.loads(self.state.read_text())),1)

    def test_resume_respects_existing_harness(self):
        self.assertEqual(self.launch(self.a, '--main', 'claude', TP_SESSION='claude-run').returncode, 0)
        self.assertEqual(self.launch(self.a, '--resume', TP_SESSION='claude-run').returncode, 0)
        wrong = self.launch(self.a, '--resume', '--main', 'pi', TP_SESSION='claude-run')
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('already runs claude', wrong.stderr)

    def sent_main(self):
        return [c[-2] for c in self.calls() if c[0] == 'send-keys' and 'tp-main' in c[-2]]

    def test_bare_agent_defaults_to_claude(self):
        self.assertEqual(self.launch(self.a, TP_SESSION='bare').returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())['bare']['@tp_main_harness'], 'claude')
        self.assertIn("TP_MAIN_HARNESS='claude'", self.sent_main()[0])
        self.assertIn('TP_MAIN_HARNESS=claude', next(c for c in self.calls() if c[0] == 'new-session'))

    def test_explicit_main_pi_overrides_agent_default(self):
        self.assertEqual(self.launch(self.a, '--main', 'pi', TP_SESSION='pi-run').returncode, 0)
        self.assertEqual(json.loads(self.state.read_text())['pi-run']['@tp_main_harness'], 'pi')
        self.assertIn("TP_MAIN_HARNESS='pi'", self.sent_main()[0])

    def test_explicit_main_claude_launches_claude(self):
        tp = self.fake / 'tp'; tp.symlink_to(ROOT / 'bin/tp')
        run = subprocess.run([str(tp), '--main', 'claude', str(self.a)], env={**self.env, 'TP_SESSION': 'c'},
                             text=True, capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("TP_MAIN_HARNESS='claude'", self.sent_main()[0])

    def test_tp_name_still_defaults_to_pi(self):
        tp = self.fake / 'tp'; tp.symlink_to(ROOT / 'bin/tp')
        run = subprocess.run([str(tp), str(self.a)], env={**self.env, 'TP_SESSION': 'p'},
                             text=True, capture_output=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("TP_MAIN_HARNESS='pi'", self.sent_main()[0])

    def test_resume_bare_agent_keeps_pi_session(self):
        self.assertEqual(self.launch(self.a, '--main', 'pi', TP_SESSION='r').returncode, 0)
        (self.fake / 'claude').unlink()
        resume = self.launch(self.a, '--resume', TP_SESSION='r', PATH=str(self.fake) + ':/usr/bin:/bin')
        self.assertEqual(resume.returncode, 0, resume.stderr)
        self.assertEqual(json.loads(self.state.read_text())['r']['@tp_main_harness'], 'pi')
        self.assertEqual(sum(c[0] == 'new-session' for c in self.calls()), 1)

    def test_fresh_same_project_preserves_history_and_resume_keeps_tasks(self):
        self.assertEqual(self.launch(self.a).returncode,0)
        first=json.loads(self.state.read_text())
        first_name=next(iter(first))
        old=Path(first[first_name]['@tp_state_dir'])
        (old/'tasks'/'previous').mkdir()
        (old/'tasks'/'previous'/'meta.json').write_text('{"id":"previous"}')
        self.assertEqual(self.launch(self.a).returncode,0)
        sessions=json.loads(self.state.read_text())
        self.assertEqual(len(sessions),2)
        fresh=next(v for k,v in sessions.items() if k!=first_name)
        self.assertNotEqual(fresh['@tp_state_dir'],str(old))
        self.assertEqual(list((Path(fresh['@tp_state_dir'])/'tasks').iterdir()),[])
        self.assertTrue((old/'tasks'/'previous'/'meta.json').exists())
        self.assertEqual(self.launch(self.a,'--resume').returncode,0)
        self.assertEqual(len(json.loads(self.state.read_text())),2)
        self.assertEqual(self.launch(self.a,TP_SESSION=first_name).returncode,1)

    def test_resume_without_live_workspace_does_not_create_one(self):
        run=self.launch(self.a,'--resume')
        self.assertNotEqual(run.returncode,0)
        self.assertIn('no running workspace',run.stderr)
        self.assertFalse(any(c[0]=='new-session' for c in self.calls()))

    def test_status_rows_handle_project_spaces(self):
        self.assertEqual(self.launch(self.a).returncode,0)
        sessions=json.loads(self.state.read_text())
        name=next(iter(sessions))
        task=Path(sessions[name]['@tp_state_dir'])/'tasks'/'current-worker'
        task.mkdir()
        (task/'meta.json').write_text(json.dumps({'id':'current-worker','worker':'pi',
                                               'pane':'%worker','model':'test','thinking':'low'}))
        run=subprocess.run(['bash','-c','source "$1/lib/tp.sh"; tp_rows "$2"',
                            'fixture',str(ROOT),name],env=self.env,text=True,capture_output=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertIn('current-worker',run.stdout)

    def test_main_restores_launch_directory_and_missing_path_fails(self):
        env={**self.env,'TP_WORKDIR':str(self.a)}
        run=subprocess.run([str(ROOT/'bin/tp-main')],cwd=self.b,env=env,capture_output=True,text=True)
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual((self.base/'cwd').read_text().strip(),str(self.a))
        (self.base/'cwd').unlink()
        env['TP_WORKDIR']=str(self.base/'missing')
        run=subprocess.run([str(ROOT/'bin/tp-main')],cwd=self.b,env=env,capture_output=True,text=True)
        self.assertNotEqual(run.returncode,0)
        self.assertFalse((self.base/'cwd').exists())

if __name__=='__main__': unittest.main()
