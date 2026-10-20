"""Frozen release workflows, executed against an extracted candidate, never the user home."""
from __future__ import annotations

import tests  # Install dotenv/network guard before project/tool imports.

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
import zipfile

from scripts._offline import copy_ignore, isolated_environment
from scripts import release_round7 as release

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "round7_release_cases.json"
FIXTURE_SHA256 = "7ad2304654181ac7ff04e528be3ca70455baba38e03bf47ff41acd7f6def01b5"
EVIDENCE = {}
LEGACY_SOURCE_OVERRIDE = None

# These snippets use only the selected source root. No TestCase from another
# module is imported or accidentally collected by unittest.
PRELUDE = '''
import asyncio, json, sys, time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from suwan.app import SuwanRuntime
from suwan.clock import ManualClock
from suwan.config import Settings
HOME = Path(sys.argv[1])
def settings():
    return Settings.from_env(env={}, load_env=False, home_dir=HOME)
def runtime():
    return SuwanRuntime(settings(), clock=ManualClock(datetime(2026,10,20,12)),
                        migrate_legacy_data=False, api_enabled=False, network_enabled=False)
def emit(value):
    print('__R7_RESULT__' + json.dumps(value, ensure_ascii=False), flush=True)
'''

SEED = PRELUDE + '''
async def main():
    rt=runtime(); await rt.start()
    try:
        assert (await rt.memory_command('/memory set user.name=七版验收'))['ok']
        assert (await rt.memory_command('/memory set user.city=合成北岸'))['ok']
        old = await rt.memory_command('/memory set user.drink=旧合成饮品')
        assert old['ok']
        assert (await rt.memory_command('/memory set user.drink=新合成饮品'))['ok']
        assert (await rt.set_interaction_preference('preference.response_style','brief'))['ok']
        assert (await rt.set_interaction_preference('preference.initiative','off'))['ok']
        assert (await rt.update_desktop_settings({'volume':37,'daily_calls':2,
          'daily_output_tokens':200,'foreground_reserved_calls':0,
          'foreground_reserved_output_tokens':0}))['ok']
        service=rt.autonomous_service
        service.llm=SimpleNamespace(available=True, chat=lambda *a, **kw:'R7_SYNTHETIC_REGISTERED_WORK')
        service.api_enabled=True
        work=service._new_activity('creation')['id']
        for _ in range(4): await service._advance_activity(work)
        await rt.bus.flush()
        assert service.journal.get(work)['status']=='completed'
        pending=service._new_activity('idle')['id']
        assert service.journal.get(pending)['status']=='active'
        assert rt.notification_ledger.claim('r7:already-delivered') is not None
        view=await rt.desktop_view()
        assert len(view['works'])==1
        assert rt.notification_ledger.snapshot()['claimed']==1
        emit({'work_id':work,'pending_id':pending,'work_sha':service.journal.get(work)['artifact_sha256'],
              'works':len(view['works']),'goals':rt.organs['goals'].state()})
    finally: await rt.stop()
asyncio.run(main())
(HOME/'empty-preserved').mkdir()
'''

CRASH = '''
import json, os, sys, time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from suwan.clock import ManualClock
from suwan.calls import CallManager, CallPolicy
from suwan.config import Settings
from suwan.instance_lock import InstanceLock
home=Path(sys.argv[1])
settings=Settings.from_env(env={},load_env=False,home_dir=home)
lock=InstanceLock(settings.data_dir); lock.acquire()
manager=CallManager(settings.call_ledger_file,CallPolicy(2,200,0,0),
                    ManualClock(datetime(2026,10,20,12)))
assert manager.start()
meta=dict(kind='llm',provider='fake',model='r7-fake',max_output_tokens=90)
manager.run(lambda:SimpleNamespace(usage=None),call_key='r7:first',**meta)
ready,release=Path(sys.argv[3]),Path(sys.argv[4])
ready.write_text(json.dumps({'instance_lock_path':str(lock.path)}),encoding='utf-8')
deadline=time.monotonic()+15
while not release.exists():
    assert time.monotonic()<deadline,'hard-exit handshake timed out'
    time.sleep(.01)
manager.run(lambda:os._exit(37),call_key='r7:second',**meta)
raise AssertionError('hard exit did not happen')
'''

CHECK = PRELUDE + '''
async def main():
    rt=runtime(); await rt.start()
    try:
        view=await rt.desktop_view()
        assert rt.organs['identity'].user_name=='七版验收'
        assert '合成北岸' in repr(view)
        assert '新合成饮品' in repr(view) and '旧合成饮品' not in repr(view)
        assert view['preferences']['response_style']=='brief'
        assert view['preferences']['initiative']=='off'
        assert view['settings']['volume']==37
        assert len(view['works'])==1
        budget=rt.call_manager.snapshot()
        assert budget['used_calls']==2 and budget['today_unknown_calls']==1, budget
        assert budget['allocated_output_tokens']==180, budget
        before=[r['id'] for r in rt.autonomous_service.journal.records]
        active=[r for r in rt.autonomous_service.journal.records if r['status']=='active']
        assert len(active)==1 and active[0]['action']=='idle'
        await rt.autonomous_service._advance_activity(active[0]['id'])
        await rt.bus.flush()
        assert rt.autonomous_service.journal.get(active[0]['id'])['status']=='completed'
        after=await rt.desktop_view()
        assert len(after['works'])==1
        assert [r['id'] for r in rt.autonomous_service.journal.records]==before
        from suwan.calls import CallDenied
        try:
            rt.call_manager.run(lambda:(_ for _ in ()).throw(AssertionError('budget bypass')),
                kind='llm',provider='fake',model='r7-fake',max_output_tokens=1)
        except CallDenied as exc: assert exc.reason=='budget_exhausted'
        else: raise AssertionError('unknown budget was refunded')
        assert rt.notification_ledger.snapshot()['claimed']==1
        emit({'works':len(after['works']),'work_id':after['works'][0]['id'].removeprefix('work:'),
              'instance_lock_path':str(rt._instance_lock.path),
              'used_calls':budget['used_calls'],'unknown':budget['today_unknown_calls'],
              'allocated':budget['allocated_output_tokens'],'activity_ids':before,
              'goals':rt.organs['goals'].state(),'pending_resumed':True,'notification_claims':1})
    finally: await rt.stop()
asyncio.run(main())
'''


def tree_manifest(folder, *, held_lock=None):
    # Windows deliberately denies reads of an actively held instance-lock file.
    # Only that exact file uses a presence marker; every other file is hashed.
    held_lock = Path(held_lock) if held_lock is not None else None
    files, empty = {}, []
    for path in sorted(Path(folder).rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise AssertionError('test tree contains a link')
        relative = path.relative_to(folder).as_posix()
        if path.is_file():
            files[relative] = ('held-instance-lock' if path == held_lock else
                               hashlib.sha256(path.read_bytes()).hexdigest())
        elif path.is_dir() and not any(path.iterdir()):
            empty.append(relative)
    return {'files':files, 'empty_directories':empty}


def legacy_evidence(source):
    """Only Python source under the explicitly selected src, never backup dotenv."""
    rows=[]
    for path in sorted(source.rglob('*.py')):
        if path.is_symlink() or path.is_junction():
            raise AssertionError('legacy source link refused')
        rows.append({'path':path.relative_to(source).as_posix(),
                     'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    payload=json.dumps(rows,sort_keys=True,separators=(',',':')).encode('utf-8')
    return {'src':str(source.resolve()),'python_files':len(rows),
            'source_manifest_sha256':hashlib.sha256(payload).hexdigest(),
            'app_sha256':next(row['sha256'] for row in rows if row['path']=='suwan/app.py')}


class ReleaseAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if hashlib.sha256(FIXTURE.read_bytes()).hexdigest() != FIXTURE_SHA256:
            raise AssertionError('seventh version frozen cases changed')
        cls.temporary = tempfile.TemporaryDirectory(prefix='suwan-r7-accept-')
        cls.base = Path(cls.temporary.name)
        cls.archive = cls.base/'candidate.zip'
        cls.build = release.build_release(ROOT, cls.archive)
        cls.package = cls.base/'candidate'
        cls.manifest = release.extract_release(cls.archive, cls.package)
        cls.source = cls.package/'src'
        cls.guard_dir = cls.base/'guard'
        cls.guard_dir.mkdir()
        shutil.copyfile(ROOT/'scripts'/'_offline.py',cls.guard_dir/'_offline.py')
        (cls.guard_dir/'sitecustomize.py').write_text(
            "import atexit,json\nfrom _offline import install_offline_guard\n"
            "_state=install_offline_guard()\n"
            "atexit.register(lambda:print('__R7_GUARD__'+json.dumps({'network_blocks':_state['network_blocks']}),flush=True))\n",
            encoding='utf-8')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.case = self._testMethodName.split('_')[1].upper()
        self.area = self.base/self.case
        self.area.mkdir()

    def child_invocation(self, code, *, source=None, home=None, arguments=()):
        source = Path(source or self.source).resolve()
        home = Path(home or self.area/'home').resolve()
        script = self.area/('child-'+str(len(list(self.area.glob('child-*.py'))))+'.py')
        script.write_text(
            "import sys,json\nfrom pathlib import Path\nimport suwan\n"
            "_origin=Path(suwan.__file__).resolve()\n"
            "assert _origin.is_relative_to(Path(sys.argv[2]).resolve()),str(_origin)\n"
            "print('__R7_SOURCE__'+str(_origin),flush=True)\n"+textwrap.dedent(code), encoding='utf-8')
        env=isolated_environment(home)
        env['PYTHONPATH']=os.pathsep.join((str(self.guard_dir),str(source)))
        command=[sys.executable,'-B','-X','utf8',str(script),str(home),str(source),
                 *(str(value) for value in arguments)]
        return command,env

    def child(self, code, *, source=None, home=None, input_text=None, expected=0, timeout=25):
        command,env=self.child_invocation(code,source=source,home=home)
        process=subprocess.run(command,
            cwd=self.package,env=env,input=input_text,text=True,encoding='utf-8',capture_output=True,timeout=timeout)
        self.assertEqual(process.returncode,expected,process.stdout+'\n'+process.stderr)
        if expected==0:
            self.assertIn('__R7_GUARD__{"network_blocks": 0}',process.stdout)
            self.assertNotIn('Exception in Tkinter callback',process.stderr)
            self.assertNotIn('exception was never retrieved',process.stderr)
        values=[json.loads(line[len('__R7_RESULT__'):]) for line in process.stdout.splitlines()
                if line.startswith('__R7_RESULT__')]
        return values[-1] if values else {},process

    def legacy_source(self):
        suffix=Path('backups/rf-pre-round6fix_20261020-01/suwan_project 重构版/src')
        options=([Path(LEGACY_SOURCE_OVERRIDE)] if LEGACY_SOURCE_OVERRIDE else
                 [ROOT.parent/suffix,Path(os.environ['USERPROFILE'])/'Desktop'/'suwan project'/suffix])
        for source in options:
            if (source/'suwan'/'app.py').is_file():
                return source
        self.fail('verified pre-round6fix source backup unavailable; supply acceptance_round7.py --legacy-source SRC; no legacy result can be claimed')

    def seed_crash(self, home, *, source=None):
        value,_=self.child(SEED,source=source,home=home)
        ready,release=self.area/'crash-ready.json',self.area/'crash-release'
        command,env=self.child_invocation(CRASH,source=source,home=home,
                                          arguments=(ready,release))
        holder=subprocess.Popen(command,cwd=self.package,env=env,text=True,encoding='utf-8',
                                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+10
            while not ready.exists() and holder.poll() is None and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists(),'hard-exit child did not acquire its runtime lock')
            self.assertIsNone(holder.poll(),'lock holder exited before contention was tested')
            held=json.loads(ready.read_text(encoding='utf-8'))
            held_path=Path(held['instance_lock_path'])
            self.assertEqual(held_path,home/'v2'/'.suwan.lock')
            before=tree_manifest(home,held_lock=held_path)
            self.assertEqual(before['files']['v2/.suwan.lock'],'held-instance-lock')
            blocked,_=self.child(PRELUDE+'''
from suwan.instance_lock import InstanceAlreadyRunningError
async def main():
    rt=runtime()
    try:
        await rt.start()
    except InstanceAlreadyRunningError:
        assert not rt._running and not rt.bus.running
        emit({'startup_blocked':True,'instance_lock_path':str(rt._instance_lock.path)})
    else:
        await rt.stop()
        raise AssertionError('candidate started while crash child held the actual runtime lock')
asyncio.run(main())
''',home=home)
            self.assertTrue(blocked['startup_blocked'])
            self.assertEqual(blocked['instance_lock_path'],held['instance_lock_path'])
            self.assertEqual(tree_manifest(home,held_lock=held_path),before,'rejected startup modified held data')
            self.assertIsNone(holder.poll(),'lock holder did not remain alive during contention')
            release.write_text('hard-exit now',encoding='utf-8')
            stdout,stderr=holder.communicate(timeout=10)
            self.assertEqual(holder.returncode,37,stdout+'\n'+stderr)
            self.assertIn('__R7_SOURCE__',stdout)
            value['crash_lock']={**held,'candidate_startup_blocked':True,
                                 'rejected_startup_unchanged':True,'hard_exit_code':37}
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=10)
        return value

    def record(self, **values):
        EVIDENCE[self.case]={'status':'PASS',**values}

    def test_r701_package_allowlist_integrity_and_rejection(self):
        manifest=release.validate_release(self.archive)
        self.assertEqual(manifest,self.manifest)
        with zipfile.ZipFile(self.archive) as archive:
            names=archive.namelist()
            self.assertEqual(len(names),len(set(names)))
            for row in manifest['files']:
                data=archive.read(row['path'])
                self.assertEqual(len(data),row['bytes'])
                self.assertEqual(hashlib.sha256(data).hexdigest(),row['sha256'])
            for name in names:
                self.assertFalse(Path(name).is_absolute())
                self.assertNotIn('..',Path(name).parts)
                self.assertFalse(any(x in name.lower() for x in ('.env','backups/','research/','docs/reference/','__pycache__')))
        synthetic=self.area/'source'
        shutil.copytree(ROOT,synthetic,ignore=copy_ignore)
        (synthetic/'.env').mkdir()
        (synthetic/'private-memory.json').write_text('SYNTHETIC_PRIVATE_DATA',encoding='utf-8')
        (synthetic/'unknown-root.txt').write_text('SYNTHETIC_UNKNOWN_ROOT',encoding='utf-8')
        (synthetic/'data').mkdir(exist_ok=True)
        (synthetic/'data'/'personal.json').write_text('SYNTHETIC_PRIVATE_DATA',encoding='utf-8')
        second=self.area/'filtered.zip'
        release.build_release(synthetic,second)
        with zipfile.ZipFile(second) as archive:
            self.assertFalse(any('private-memory' in name or 'unknown-root' in name or name.startswith('data/')
                                 or name.startswith('.env') for name in archive.namelist()))
        original=Path.is_symlink
        linked=synthetic/'src'/'suwan'/'__init__.py'
        with patch.object(Path,'is_symlink',lambda path: path==linked or original(path)):
            with self.assertRaises((ValueError,OSError)):
                release.build_release(synthetic,self.area/'link-rejected.zip')
        for member in ('../escape.py','.env','/absolute.py','src/suwan/__init__.py'):
            bad=self.area/('bad-'+hashlib.sha256(member.encode()).hexdigest()[:8]+'.zip')
            with zipfile.ZipFile(self.archive) as source,zipfile.ZipFile(bad,'w') as target:
                for info in source.infolist(): target.writestr(info,source.read(info.filename))
                target.writestr(member,b'SYNTHETIC_UNTRUSTED_MEMBER')
            with self.assertRaises((ValueError,OSError,zipfile.BadZipFile)):
                release.validate_release(bad)
        tampered=self.area/'bad-hash.zip'
        with zipfile.ZipFile(self.archive) as source,zipfile.ZipFile(tampered,'w') as target:
            for info in source.infolist():
                data=source.read(info.filename)
                if info.filename=='src/suwan/__init__.py': data+=b'\n# synthetic tamper\n'
                target.writestr(info,data)
        with self.assertRaises((ValueError,OSError,zipfile.BadZipFile)):
            release.validate_release(tampered)
        self.record(files=len(manifest['files']),archive_sha256=self.build['sha256'],malicious_archives_rejected=5,
                    link_injection='Path.is_symlink fault injection; no platform privilege bypass')

    def test_r702_candidate_repl_restart(self):
        launcher=self.package/'start_suwan.py'
        code="import runpy\nsys.path.insert(0,"+repr(str(self.package))+")\nsys.argv=["+repr(str(launcher))+",'--home',str(Path(sys.argv[1])),'--frontend','repl','--offline']\nrunpy.run_path("+repr(str(launcher))+",run_name='__main__')\n"
        _,first=self.child(code,input_text='我叫七版验收\n昨晚我们一起看了合成星空\n/quit\n')
        _,second=self.child(code,input_text='我叫什么？\n/quit\n')
        self.assertIn('七版验收',first.stdout)
        self.assertIn('七版验收',second.stdout)
        content=(self.area/'home'/'v2'/'memories.json').read_text(encoding='utf-8')
        self.assertIn('合成星空',content)
        self.record(processes=2,returncodes=[0,0],candidate_origin_verified=True,environment='current interpreter, installed dependencies; isolated extracted source and home')

    def test_r703_candidate_real_tk_settings_close_restart(self):
        code=PRELUDE+'''
import gc, tkinter as tk
from tkinter import ttk
from unittest.mock import patch
from suwan.frontends.tkapp import TkApp
gc.disable()
results=[]
for cycle in range(2):
    rt=runtime(); app=TkApp(rt); app.root.withdraw()
    errors=[]
    app.root.report_callback_exception=lambda *args:errors.append(str(args))
    deadline=time.monotonic()+8
    while app._desktop_pending or not app.data_info.get():
        assert time.monotonic()<deadline
        app._pump();app.root.update();time.sleep(.005)
    notebook=next(w for w in app.root.winfo_children() if isinstance(w,ttk.Notebook))
    tab_count=len(notebook.tabs())
    for tab in notebook.tabs(): notebook.select(tab);app.root.update()
    if cycle==0: assert app.server.update_desktop_settings({'volume':37}).result(5)['ok']
    else: assert app.server.desktop_view().result(5)['settings']['volume']==37
    with patch('suwan.frontends.tkapp.messagebox.showerror',side_effect=lambda *a,**kw:errors.append(str(a))):
        app.on_close()
        deadline=time.monotonic()+8
        while True:
            try:
                app.root.update()
                exists=bool(app.root.winfo_exists())
            except tk.TclError: exists=False
            if not exists: break
            assert time.monotonic()<deadline
            time.sleep(.005)
    assert not app.server._thread.is_alive()
    assert not errors,errors
    results.append({'cycle':cycle,'tabs':tab_count,'thread_exited':True})
    app._toggles.clear()
    for key,value in list(vars(app).items()):
        if isinstance(value,tk.Variable): delattr(app,key)
    del app;gc.collect()
emit({'real_tk_cycles':results,'volume_restored':37})
'''
        result,_=self.child(code,timeout=35)
        self.record(**result,candidate_origin_verified=True)

    def test_r704_stopped_backup_empty_restore(self):
        home=self.area/'home'
        legacy=self.legacy_source()
        seed=self.seed_crash(home,source=legacy)
        before=tree_manifest(home)
        backup=self.area/'backup';restored=self.area/'restored'
        shutil.copytree(home,backup);shutil.copytree(backup,restored)
        self.assertEqual(before,tree_manifest(backup))
        self.assertEqual(before,tree_manifest(restored))
        result,_=self.child(CHECK,home=restored)
        self.assertEqual(result['work_id'],seed['work_id'])
        self.assertEqual(before,tree_manifest(backup))
        self.record(files=len(before['files']),empty_directories=before['empty_directories'],sha_equal_before_start=True,
                    legacy_source=legacy_evidence(legacy),**result)

    def test_r705_upgrade_and_full_directory_rollback(self):
        legacy=self.legacy_source();home=self.area/'v6-data'
        seed=self.seed_crash(home,source=legacy)
        frozen=tree_manifest(home)
        upgrade=self.area/'upgrade';rollback=self.area/'rollback'
        shutil.copytree(home,upgrade);shutil.copytree(home,rollback)
        upgraded,_=self.child(CHECK,home=upgrade)
        rolled_back,_=self.child(CHECK,source=legacy,home=rollback)
        self.assertEqual(upgraded['work_id'],seed['work_id'])
        self.assertEqual(rolled_back['work_id'],seed['work_id'])
        self.assertEqual(upgraded['activity_ids'],rolled_back['activity_ids'])
        self.assertEqual(upgraded['goals'],rolled_back['goals'])
        self.assertEqual(tree_manifest(home),frozen)
        self.record(original_unchanged=True,upgrade=upgraded,rollback=rolled_back,
                    legacy_source=legacy_evidence(legacy),
                    boundary='whole pre-upgrade data directory restored; not arbitrary backwards schema compatibility')

    def test_r706_corruption_write_failure_and_restore(self):
        code=PRELUDE+'''
from unittest.mock import patch
from suwan.adapters.filestore import DataFileError
async def main():
    rt=runtime();await rt.start()
    assert (await rt.memory_command('/memory set user.name=七版验收'))['ok']
    assert (await rt.update_desktop_settings({'volume':37}))['ok']
    await rt.stop()
    state=settings().state_file;valid=state.read_bytes();broken=b'{R7 broken snapshot'
    state.write_bytes(broken)
    failed=runtime()
    try: await failed.start()
    except DataFileError: pass
    else: raise AssertionError('corrupt snapshot silently accepted')
    assert state.read_bytes()==broken
    state.write_bytes(valid)
    rt=runtime();await rt.start()
    try:
        path=rt.desktop_service.path;original=path.read_bytes()
        with patch('suwan.services.desktop.atomic_write_json',side_effect=OSError('synthetic disk write failure')):
            result=await rt.update_desktop_settings({'volume':12})
        assert not result['ok'] and result['settings']['volume']==37,result
        assert path.read_bytes()==original
        assert await rt.ask('你好')
    finally: await rt.stop()
    path=HOME/'v2'/'desktop-v6.json';saved=path.read_bytes();path.write_bytes(b'{R7 broken settings')
    rt=runtime();await rt.start()
    try:
        view=await rt.desktop_view()
        assert view['settings_readonly'] and view['warnings']
        assert path.read_bytes()==b'{R7 broken settings'
        assert await rt.ask('你好')
    finally: await rt.stop()
    path.write_bytes(saved)
    rt=runtime();await rt.start()
    try:
        assert rt.organs['identity'].user_name=='七版验收'
        assert (await rt.desktop_view())['settings']['volume']==37
    finally: await rt.stop()
    emit({'corrupt_snapshot_retained':True,'corrupt_settings_readonly':True,'write_failure_not_success':True,'restored':True})
asyncio.run(main())
'''
        result,_=self.child(code)
        self.record(**result)

    def test_r707_candidate_hard_exit_keeps_unknown_and_effects(self):
        home=self.area/'home';seed=self.seed_crash(home)
        result,_=self.child(CHECK,home=home)
        self.assertEqual(result['work_id'],seed['work_id'])
        self.assertEqual(result['instance_lock_path'],seed['crash_lock']['instance_lock_path'])
        # A second reboot must leave the now-completed idle step and work stable.
        code=PRELUDE+'''
async def main():
    rt=runtime();await rt.start()
    try:
        rows=rt.autonomous_service.journal.records
        assert len(rows)==2 and all(r['status']=='completed' for r in rows)
        view=await rt.desktop_view();assert len(view['works'])==1
        budget=rt.call_manager.snapshot()
        assert budget['used_calls']==2 and budget['today_unknown_calls']==1
        assert rt.notification_ledger.snapshot()['claimed']==1
        emit({'ids':[r['id'] for r in rows],'goals':rt.organs['goals'].state(),'unknown':budget['today_unknown_calls']})
    finally: await rt.stop()
asyncio.run(main())
'''
        reboot,_=self.child(code,home=home)
        self.assertEqual(reboot['ids'],result['activity_ids'])
        self.assertEqual(reboot['goals'],result['goals'])
        self.record(hard_exit_code=37,candidate_origin_verified=True,reboots=2,
                    crash_lock=seed['crash_lock'],same_runtime_lock_reacquired=True,**result)

    def test_r708_controlled_provider_timeout_cancel_tts_recovery(self):
        code=PRELUDE+'''
import base64, threading
from unittest.mock import patch
from suwan.services.llm_request import llm_chat
async def main():
    rt=runtime();await rt.start()
    entered=threading.Event();release=threading.Event()
    mode=['fail'];physical=[]
    def sdk(**kw):
        physical.append(mode[0])
        if mode[0]=='fail': raise OSError('synthetic provider unavailable')
        entered.set();assert release.wait(5)
        return SimpleNamespace(usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='R7_LATE_REPLY'))])
    rt.llm._keyless=True
    rt.llm._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=sdk)))
    rt.set_api_enabled(True)
    try:
        assert await rt.ask('请讨论一次合成旅程')
        mode[0]='timeout';entered.clear()
        with patch('suwan.services.chat.LLM_TIMEOUT_SECONDS',.05):
            reply=await rt.ask('请讨论另一次合成旅程')
        assert 'R7_LATE_REPLY' not in reply
        assert entered.is_set()
        release.set()
        for _ in range(100):
            if not rt.call_manager.snapshot()['in_flight']:break
            await asyncio.sleep(.01)
        release.clear();entered.clear();mode[0]='cancel'
        task=asyncio.create_task(rt.ask('请讨论可取消的合成旅程'))
        assert await asyncio.to_thread(entered.wait,3)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        else: raise AssertionError('cancel was ignored')
        release.set()
        for _ in range(100):
            if not rt.call_manager.snapshot()['in_flight']:break
            await asyncio.sleep(.01)
        rt.settings.api_keys['mimo']='R7_SYNTHETIC_NOT_A_REAL_CREDENTIAL'
        def tts_sdk(**kw): raise OSError('synthetic voice unavailable')
        rt.tts._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=tts_sdk)))
        path,error=await rt.speak('合成语音测试')
        assert path is None and error
        assert not list(rt.settings.audio_dir.glob('*.wav'))
        rt.set_api_enabled(False)
        assert await rt.ask('我叫七版验收')
        assert 'R7_LATE_REPLY' not in repr(rt.organs['sensory'].recent(100))
        budget=rt.call_manager.snapshot()
        assert budget['used_calls']>=3,budget
        assert budget['actual_usage_unknown_calls']>=3,budget
        emit({'physical_fake_calls':physical,'charged_calls':budget['used_calls'],
              'usage_unknown':budget['actual_usage_unknown_calls'],'tts_error':bool(error),'late_reply_absent':True})
    finally:
        release.set();await rt.stop()
    rt=runtime();await rt.start()
    try: assert rt.organs['identity'].user_name=='七版验收'
    finally: await rt.stop()
asyncio.run(main())
'''
        result,_=self.child(code,timeout=35)
        self.record(**result)

    def test_r709_real_elapsed_observer_short_and_stop(self):
        from scripts.observe_round7 import observe
        import inspect
        import time
        parameters=inspect.signature(observe).parameters
        self.assertIn('stop_file',parameters)
        start=time.monotonic()
        result=asyncio.run(observe(self.area/'short-home',self.area/'short-results',2,.1))
        actual=time.monotonic()-start
        summary=json.loads((self.area/'short-results'/'summary.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(actual,2)
        self.assertFalse(summary['24h_verified'])
        self.assertEqual(summary['status'],'PASS')
        self.assertGreaterEqual(summary['actual_elapsed_seconds'],2)
        self.assertGreaterEqual(summary['samples'],2)
        self.assertTrue(summary['continuous_samples'])
        self.assertTrue(summary['clean_stop'])
        self.assertEqual(summary['error_count'],0)
        self.assertLessEqual(summary['max_sample_gap_seconds'],summary['allowed_sample_gap_seconds'])
        self.assertEqual(summary['network_guard']['blocked_attempts'],0)
        packaged={row['path']:row['sha256'] for row in self.manifest['files']}
        for row in summary['source_manifest']['files']:
            self.assertEqual(packaged[row['path']],row['sha256'])
        stop=self.area/'STOP'
        async def stopped():
            async def trigger():
                deadline=time.monotonic()+5
                heartbeat=self.area/'stop-results'/'heartbeat.json'
                while time.monotonic()<deadline:
                    await asyncio.sleep(.05)
                    if heartbeat.exists():
                        value=json.loads(heartbeat.read_text(encoding='utf-8'))
                        if value.get('samples',0)>=2:
                            stop.write_text('STOP',encoding='utf-8')
                            return
                raise AssertionError('long-target observer did not produce two live samples')
            task=asyncio.create_task(trigger())
            try: return await observe(self.area/'stop-home',self.area/'stop-results',86400,.1,stop_file=stop)
            finally: await task
        asyncio.run(stopped())
        stopped_summary=json.loads((self.area/'stop-results'/'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(stopped_summary['status'],'INCOMPLETE')
        self.assertFalse(stopped_summary['24h_verified'])
        self.assertLess(stopped_summary['actual_elapsed_seconds'],86400)
        self.assertTrue(stopped_summary['stop_requested'])
        self.assertGreaterEqual(stopped_summary['samples'],2)
        def compact(value):
            value=dict(value)
            manifest=value['source_manifest']
            value['source_manifest']={'sha256':manifest['sha256'],'file_count':len(manifest['files'])}
            return value
        self.record(short=compact(summary),stopped=compact(stopped_summary),measured_wall_seconds=round(actual,3),
                    scope='real elapsed mechanism only; 24h and human 7-day acceptance pending')

    def test_r710_guide_template_and_truthful_pending_boundaries(self):
        guide=(self.package/'README.md').read_text(encoding='utf-8')
        template=json.loads((self.package/'seven-day-log.json').read_text(encoding='utf-8'))
        self.assertIn('PENDING',json.dumps(template))
        self.assertIn('NOT_RUN',json.dumps(template))
        self.assertIn('24',guide)
        self.assertIn('7',guide)
        for text in ('备份','恢复','回退'):
            self.assertIn(text,guide)
        self.assertNotIn(str(ROOT),guide)
        self.assertNotIn(os.environ['USERPROFILE'],guide)
        days=template['days']
        self.assertEqual(len(days),7)
        self.assertEqual([day['day'] for day in days],list(range(1,8)))
        for day in days:
            self.assertEqual(day['status'],'PENDING')
            self.assertIsNone(day['actual_date'])
            self.assertEqual(day['sessions'],[])
            self.assertIsNone(day['runtime_minutes'])
            self.assertEqual(day['learning_reuse'],[])
            self.assertEqual(day['failures'],[])
            self.assertIsNone(day['manual_restart_required'])
            self.assertTrue(all(value is None for value in day['external_calls'].values()))
        with zipfile.ZipFile(self.archive) as archive:
            names=archive.namelist()
            self.assertFalse(any(name.startswith(('docs/','research/','backups/','tests/')) for name in names))
        self.record(days=7,status='PASS',human_use='PENDING',real_providers='NOT_RUN',
                    real_audio_hardware='NOT_RUN',real_24h='NOT_RUN',current_dependency_environment_only=True)


if __name__=='__main__':
    unittest.main()
