from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
import uuid
import zipfile

import conversation_bundle as b
import conversation_restore as r
import history_export as e
import history_import as i
import migrate as m


SCHEMA = '''
create table threads(id text primary key, rollout_path text not null, title text not null, archived integer default 0,
model_provider text not null, cwd text not null, created_at integer not null, updated_at integer not null,
source text not null, sandbox_policy text not null, approval_mode text not null, is_pinned integer default 0,
thread_section_id text, project_id text, history_mode text default 'legacy');
create table projects(id text primary key, name text, metadata text default '{}', position integer, created_at_ms integer, updated_at_ms integer);
create table project_roots(project_id text, position integer, path text, primary key(project_id,position));
create table thread_sections(id text primary key, name text, appearance text);
create table thread_attachments(id text primary key, thread_id text, attachment_type text, identity_key text, payload text, created_at integer);
'''


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source, self.target = self.base / 'source', self.base / 'target'
        self.project, self.destination = self.base / '项目 with spaces', self.base / '导入目录'
        for folder in (self.source, self.target, self.project, self.destination):
            folder.mkdir()
        for root in (self.source, self.target):
            (root / 'account-manager').mkdir()
            with closing(sqlite3.connect(root / 'state_5.sqlite')) as conn:
                conn.executescript(SCHEMA)
        with closing(sqlite3.connect(self.target / 'thread_history_1.sqlite')) as conn:
            for table, schema in r.HISTORY_SCHEMA.items():
                conn.execute(f'create table {table} ({schema})')
        self.tid, self.other = str(uuid.uuid4()), str(uuid.uuid4())
        self.pid, self.section = str(uuid.uuid4()), str(uuid.uuid4())
        (self.project / 'images').mkdir()
        (self.project / 'report.html').write_text('<img src="images/chart.svg">', encoding='utf-8')
        (self.project / 'images/chart.svg').write_text('<svg/>')
        (self.project / 'unreferenced.docx').write_bytes(b'fixture docx')
        self.session = self.source / 'archived_sessions' / 'fixture.jsonl'
        self.session.parent.mkdir()
        events = [
            {'type':'session_meta','payload':{'id':self.tid,'cwd':str(self.project),'model_provider':'source-provider'}},
            {'type':'event_msg','payload':{'message':f'[报告](<{self.project / "report.html"}>)'}},
        ]
        self.raw = b''.join(json.dumps(x,ensure_ascii=False).encode() + b'\n' for x in events)
        self.session.write_bytes(self.raw)
        with closing(sqlite3.connect(self.source / 'state_5.sqlite')) as conn:
            conn.execute('insert into threads values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (self.tid,str(self.session),'保留标题',1,'source-provider',str(self.project),111,222,'vscode','{}','never',1,self.section,self.pid,'paginated'))
            conn.execute('insert into projects values(?,?,?,?,?,?)',(self.pid,'原项目','{}',3,111000,222000))
            conn.execute('insert into project_roots values(?,?,?)',(self.pid,0,str(self.project)))
            conn.execute('insert into thread_sections values(?,?,?)',(self.section,'我的分组','{}'))
            conn.commit()
        history = self.source / 'thread_history_1.sqlite'
        with closing(sqlite3.connect(history)) as conn:
            for table,schema in r.HISTORY_SCHEMA.items():
                conn.execute(f'create table {table} ({schema})')
            conn.execute('insert into thread_items values(?,?,?,?,?,?,?,?)',
                         (self.tid,'turn','item',1,111000,json.dumps(events[1]),'message',1))
            conn.execute('insert into thread_history_projection_state values(?,?,?)',(self.tid,len(self.raw),2))
            conn.commit()
        m.save(self.source / '.codex-global-state.json', {'local-projects':{self.pid:{'id':self.pid,'name':'原项目','rootPaths':[str(self.project)]}},
               'thread-project-assignments':{self.tid:{'projectKind':'local','projectId':self.pid}},
               'project-order':[self.pid],'unrelated-setting':'must not copy'})
        m.save(self.target / '.codex-global-state.json', {'keep-existing':True})

    def export(self, project=False):
        records = e.catalog(self.source)['records']
        with e.archive_history(self.source, self.source/'account-manager', [records[0]['id']], [self.pid] if project else None) as (path, manifest):
            output = self.base / ('project.zip' if project else 'conversation.zip')
            shutil.copyfile(path,output)
        return output

    def test_conversation_roundtrip_preserves_history_files_and_organization(self):
        package = self.export()
        with zipfile.ZipFile(package) as archive:
            migration = json.loads(archive.read('migration.json'))
            sources = {Path(a['source']).name for a in migration['assets']}
            self.assertEqual(sources, {'report.html','chart.svg'})
        preview = i.preview(self.target,package)
        self.assertEqual(preview[0]['files'],2)
        result = i.merge(self.target,self.target/'account-manager',package,[self.tid],destination=str(self.destination),guard=lambda:None)
        self.assertEqual(result['files'],2)
        with closing(m.connect(self.target/'state_5.sqlite',True)) as conn:
            thread = dict(conn.execute('select * from threads').fetchone())
            self.assertEqual((thread['title'],thread['archived'],thread['is_pinned'],thread['created_at'],thread['updated_at']),('保留标题',1,1,111,222))
            self.assertEqual(thread['approval_mode'],'on-request')
            self.assertEqual(conn.execute('select name from projects where id=?',(thread['project_id'],)).fetchone()[0],'原项目')
            self.assertEqual(conn.execute('select name from thread_sections where id=?',(thread['thread_section_id'],)).fetchone()[0],'我的分组')
        folder=Path(thread['cwd'])
        self.assertTrue((folder/'report.html').exists())
        self.assertTrue((folder/'images/chart.svg').exists())
        self.assertFalse((folder/'unreferenced.docx').exists())
        imported=Path(thread['rollout_path']).read_text(encoding='utf-8')
        self.assertIn(str(folder).replace('\\','/'),imported)
        with closing(m.connect(self.target/'thread_history_1.sqlite',True)) as conn:
            self.assertIn(str(folder).replace('\\','/'),conn.execute('select item_json from thread_items').fetchone()[0])
            self.assertEqual(conn.execute('select next_rollout_byte_offset from thread_history_projection_state').fetchone()[0],Path(thread['rollout_path']).stat().st_size)
        state=m.load(self.target/'.codex-global-state.json')
        self.assertTrue(state['keep-existing'])
        self.assertNotIn('unrelated-setting',state)
        self.assertEqual(state['thread-project-assignments'][self.tid]['projectId'],thread['project_id'])
        self.assertEqual(self.session.read_bytes(),self.raw)
        self.assertEqual(i.merge(self.target,self.target/'account-manager',package,[self.tid],destination=str(self.destination),guard=lambda:None)['skipped'],1)

    def test_project_export_includes_unreferenced_document(self):
        package=self.export(project=True)
        with zipfile.ZipFile(package) as archive:
            migration=json.loads(archive.read('migration.json'))
            self.assertIn('unreferenced.docx',{Path(a['source']).name for a in migration['assets']})
        result=i.merge(self.target,self.target/'account-manager',package,[self.tid],destination=str(self.destination),guard=lambda:None)
        self.assertEqual(result['files'],3)
        self.assertEqual(len(list(self.destination.rglob('unreferenced.docx'))),1)

    def test_tampered_asset_rejected_before_any_writes(self):
        package=self.export()
        tampered=self.base/'tampered.zip'
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered,'w') as target:
            for name in source.namelist():
                raw=source.read(name)
                if name.endswith('chart.svg'):
                    raw=b'X'*len(raw)
                target.writestr(name,raw)
        with self.assertRaises(RuntimeError):
            i.merge(self.target,self.target/'account-manager',tampered,[self.tid],destination=str(self.destination),guard=lambda:None)
        self.assertFalse(list(self.destination.iterdir()))

    def test_portable_failure_rolls_back_state_history_files_and_ui(self):
        package=self.export()
        calls=0
        def guard():
            nonlocal calls
            calls+=1
            if calls==5:
                raise RuntimeError('fixture abort')
        with self.assertRaises(RuntimeError):
            i.merge(self.target,self.target/'account-manager',package,[self.tid],destination=str(self.destination),guard=guard)
        with closing(sqlite3.connect(self.target/'state_5.sqlite')) as conn:
            self.assertEqual(conn.execute('select count(*) from threads').fetchone()[0],0)
        self.assertFalse(list(self.destination.rglob('*.html')))
        self.assertEqual(m.load(self.target/'.codex-global-state.json'),{'keep-existing':True})

    def test_foreign_machine_paths_are_mapped_without_source_directory(self):
        package=self.export(project=True)
        foreign=self.base/'foreign.zip'
        original=str(self.project)
        replacements={original:'Z:\\Foreign Machine\\Project'}
        with zipfile.ZipFile(package) as source, zipfile.ZipFile(foreign,'w') as target:
            for name in source.namelist():
                raw=source.read(name)
                if name in ('migration.json','conversations.json'):
                    value=b.remap(json.loads(raw),replacements)
                    raw=json.dumps(value,ensure_ascii=False).encode()
                elif name.endswith('.jsonl') and name.startswith(('sessions/','archived_sessions/')):
                    raw=b''.join(json.dumps(b.remap(json.loads(line),replacements),ensure_ascii=False).encode()+b'\n' for line in raw.splitlines())
                target.writestr(name,raw)
        i.merge(self.target,self.target/'account-manager',foreign,[self.tid],destination=str(self.destination),guard=lambda:None)
        with closing(m.connect(self.target/'state_5.sqlite',True)) as conn:
            row=dict(conn.execute('select * from threads').fetchone())
        self.assertTrue((Path(row['cwd'])/'unreferenced.docx').exists())
        self.assertNotIn('Foreign Machine',Path(row['rollout_path']).read_text(encoding='utf-8'))

    def test_completed_import_journal_can_be_reconciled_after_restart(self):
        package=self.export()
        result=i.merge(self.target,self.target/'account-manager',package,[self.tid],destination=str(self.destination),guard=lambda:None)
        manifest=Path(result['backup'])/'manifest.json'
        data=m.load(manifest); data['state']='prepared'; m.save(manifest,data)
        self.assertEqual(len(i.pending(self.target/'account-manager')),1)
        i.recover(self.target,self.target/'account-manager',guard=lambda:None)
        self.assertEqual(m.load(manifest)['state'],'applied')
        self.assertFalse(i.pending(self.target/'account-manager'))

    def test_selected_thread_does_not_leak_other_history(self):
        with closing(sqlite3.connect(self.source/'thread_history_1.sqlite')) as conn:
            conn.execute('insert into thread_items values(?,?,?,?,?,?,?,?)',(self.other,'other-turn','other-item',1,1,'PRIVATE-OTHER-THREAD','message',1)); conn.commit()
        package=self.export()
        with zipfile.ZipFile(package) as archive:
            self.assertNotIn(b'PRIVATE-OTHER-THREAD',archive.read('migration.json'))

    def test_existing_different_file_is_not_overwritten(self):
        package=self.export()
        records=i.inspect_archive(package)
        with zipfile.ZipFile(package) as archive:
            migration=r.migration_data(archive,{self.tid})
            plan=r.plan(self.target,archive,migration,records,str(self.destination),m.sha(archive.read('conversations.json')))
            asset,target=plan['files'][0]
        target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(b'different')
        with self.assertRaises(RuntimeError):
            i.merge(self.target,self.target/'account-manager',package,[self.tid],destination=str(self.destination),guard=lambda:None)
        self.assertEqual(target.read_bytes(),b'different')

    def test_http_upload_preview_and_import_job(self):
        from http.server import ThreadingHTTPServer
        import threading
        import time
        import urllib.request
        from unittest.mock import patch
        import webui
        package=self.export()
        with patch.object(m,'private_folder',lambda p:p.mkdir(parents=True,exist_ok=True)):
            manager=webui.Manager(self.target)
        server=ThreadingHTTPServer(('127.0.0.1',0),webui.Handler)
        server.manager=manager; server.token='fixture-token'; server.instance='fixture'
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def post(route,body,mime='application/json'):
            request=urllib.request.Request(base+'/api/'+route,data=body,headers={'Content-Type':mime,'Origin':base,'X-CSRF-Token':'fixture-token'})
            with urllib.request.urlopen(request,timeout=10) as response:
                return json.load(response)
        try:
            preview=post('conversations/upload',package.read_bytes(),'application/zip')
            self.assertEqual(preview['records'][0]['files'],2)
            with patch.object(m,'busy',return_value=False),patch.object(m,'idle',return_value=None):
                post('conversations/import',json.dumps({'token':preview['token'],'ids':[self.tid],'destination':str(self.destination),'restart':False}).encode())
                deadline=time.monotonic()+5
                while manager.running_job() and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertEqual(manager.job['state'],'done',manager.job['message'])
            self.assertTrue(list(self.destination.rglob('report.html')))
            self.assertFalse((manager.store/'conversation-import-staging'/(preview['token']+'.zip')).exists())
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_missing_target_history_schema_stops_before_writes(self):
        package=self.export()
        (self.target/'thread_history_1.sqlite').unlink()
        with self.assertRaises(RuntimeError):
            i.merge(self.target,self.target/'account-manager',package,[self.tid],destination=str(self.destination),dry_run=True)
        self.assertFalse(list(self.destination.iterdir()))

    def test_file_changes_included_but_runtime_commands_excluded(self):
        changed=self.project/'generated.txt'; changed.write_text('fixture')
        runtime=self.base/'runtime.exe'; runtime.write_bytes(b'not a deliverable')
        with closing(sqlite3.connect(self.source/'thread_history_1.sqlite')) as conn:
            for kind, payload in [('fileChange',{'changes':[{'path':str(changed),'kind':'add','diff':''}]}),('commandExecution',{'command':str(runtime)})]:
                conn.execute('insert into thread_items values(?,?,?,?,?,?,?,?)',(self.tid,'turn',kind,2,1,json.dumps(payload),kind,2))
            conn.commit()
        result=b.collect(self.source,e.catalog(self.source)['records'])
        names={Path(a['source']).name for a in result['assets']}
        self.assertIn('generated.txt',names)
        self.assertNotIn('runtime.exe',names)
