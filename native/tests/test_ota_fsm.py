"""Exercise api2's full install/recovery FSM with in-memory I/O only."""
from pathlib import Path
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from lupa.luajit21 import LuaRuntime
HERE=Path(__file__).resolve().parents[1]
SETUP=r'''
return function(M)
    local function clone(v) if type(v)~='table' then return v end;local o={};for k,x in pairs(v) do o[k]=clone(x) end;return o end
    local old={['_meta.lua']=string.rep('a',64),['main.lua']=string.rep('b',64),VERSION=string.rep('c',64)}
    local new={['_meta.lua']=string.rep('a',64),['main.lua']=string.rep('e',64),VERSION=string.rep('f',64)}
    local r={serial='TEST000000000001',parent='/mnt/us/koreader/plugins',old_version='0.5.36-dev',new_version='0.5.37-dev',
        stage='kindleprinter.ota-stage-0.5.37-dev-1792269999-123456',backup='kindleprinter.ota-backup-0.5.36-dev-1792269999-123456',old=old,new=new}
    local h={packages={},log={},private=true,serial=r.serial,replacements=0,locks=0,unlocks=0}
    h.active=r.parent..'/kindleprinter.koplugin';h.stage=r.parent..'/'..r.stage;h.backup=r.parent..'/'..r.backup
    h.oldpack={version=r.old_version,hashes=clone(old)};h.newpack={version=r.new_version,hashes=clone(new)}
    h.packages[h.active]=clone(h.oldpack);h.packages[h.stage]=clone(h.newpack)
    local io={}
    io.private_ready=function() return h.private end
    io.serial=function() return h.serial end
    io.exists=function(path) return h.packages[path]~=nil end
    io.package=function(path) return h.packages[path] end
    io.acquire_lock=function() if h.locked or h.lock_fail then return nil end;h.locked=true;h.locks=h.locks+1;return 42 end
    io.release_lock=function(fd) assert(fd==42);h.locked=false;h.unlocks=h.unlocks+1;return not h.unlock_fail end
    io.read_journal=function() return h.journal,h.unreadable and 'unreadable' or h.journal and nil or 'absent' end
    io.write_journal_exclusive=function(raw)
        if h.journal or h.write_fail then return false end;h.journal=raw;h.log[#h.log+1]='journal';return true
    end
    io.clear_journal_if_equal=function(raw)
        if h.clear_fail or raw~=h.journal then return false end;h.journal=nil;h.log[#h.log+1]='clear';return true
    end
    io.sync_packages=function() h.log[#h.log+1]='sync';return not h.sync_fail end
    io.copy_package=function(from,to,manifest)
        h.log[#h.log+1]='backup';if h.packages[to] then return false end
        if h.backup_fail then h.packages[to]={version='partial',hashes={}};return false end
        h.packages[to]=clone(h.packages[from]);return true
    end
    io.replace_from=function(from,to,wanted,allowed)
        h.replacements=h.replacements+1
        if h.fail_replace_at==h.replacements then return false end
        local src,name=from:match('^(.*)/([^/]+)$');local dst,dname=to:match('^(.*)/([^/]+)$')
        assert(name==dname);assert(name=='main.lua' or name=='VERSION','Metadata must never be written')
        local source,target=h.packages[src],h.packages[dst]
        if not source or not target or source.hashes[name]~=wanted or not allowed[target.hashes[name]] then return false end
        h.log[#h.log+1]='replace:'..name;target.hashes[name]=wanted
        if name=='VERSION' then target.version=source.version end
        if h.cut_after_replace==h.replacements then error('SIMULATED PROCESS LOSS') end
        return true
    end
    return M.new(io),r,h,io,clone
end
'''
class Context:
    def __init__(self):
        self.lua=LuaRuntime(unpack_returned_tuples=True)
        self.lua.execute("require=function() error('forbidden native operation') end;io={};os={}")
        self.module=self.lua.execute((HERE/'ota-file-recovery.lua.in').read_text(encoding='utf-8').replace('__INKBOARD_SERIAL__','TEST000000000001'))
        self.machine,self.r,self.h,self.io,self.clone=self.lua.execute(SETUP)(self.module)
    def install(self):return self.machine.install(self.machine,self.r)
    def recover(self):return self.machine.recover(self.machine)
    def journal(self):self.h.journal=self.module.encode(self.r);self.h.packages[self.h.backup]=self.clone(self.h.oldpack)
    def changes(self):return [self.h.log[i] for i in range(1,len(self.h.log)+1) if self.h.log[i].startswith('replace:')]
    def old(self):self.assert_pack(self.r.old,self.r.old_version)
    def assert_pack(self,hashes,version):
        active=self.h.packages[self.h.active]
        assert active.version==version
        for name in ('_meta.lua','main.lua','VERSION'):assert active.hashes[name]==hashes[name]

class FileAtomic(unittest.TestCase):
    def test_install_verifies_backup_before_journal_then_main_then_version(self):
        c=Context();self.assertEqual(c.install(),(True,'installed'));c.assert_pack(c.r.new,c.r.new_version)
        log=[c.h.log[i] for i in range(1,len(c.h.log)+1)]
        self.assertLess(log.index('backup'),log.index('journal'));self.assertEqual(c.changes(),['replace:main.lua','replace:VERSION'])
        self.assertIsNone(c.h.journal);self.assertEqual(c.h.packages[c.h.backup].hashes['main.lua'],c.r.old['main.lua'])
        self.assertEqual(c.h.packages[c.h.active].hashes['_meta.lua'],c.r.old['_meta.lua']);self.assertFalse(c.h.locked)
    def test_metadata_change_and_invalid_lineage_never_create_backup(self):
        for key,value in [('serial','WRONG'),('parent','/tmp'),('new_version','0.5.35-dev'),('backup','../backup'),('stage','../stage'),('extra','bad')]:
            c=Context();c.r[key]=value;self.assertEqual(c.install(),(False,'invalid_transaction'));self.assertEqual(c.changes(),[]);self.assertEqual(len(c.h.log),0)
        c=Context();c.r.new['_meta.lua']='d'*64;self.assertEqual(c.install(),(False,'invalid_transaction'));self.assertEqual(len(c.h.log),0)
    def test_private_or_identity_failure_happens_before_lock_or_io(self):
        for key,value in [('private',False),('serial','WRONG')]:
            c=Context();c.h[key]=value;self.assertFalse(c.install()[0]);self.assertFalse(c.recover()[0]);self.assertEqual(c.h.locks,0);self.assertEqual(len(c.h.log),0)
    def test_live_lock_busy_blocks_install_and_startup_recovery(self):
        c=Context();c.h.locked=True;self.assertEqual(c.install(),(False,'update_busy_or_lock_unavailable'));self.assertFalse(c.recover()[0]);self.assertEqual(len(c.h.log),0)
    def test_partial_backup_failure_never_touches_active_and_next_attempt_can_use_unique_name(self):
        c=Context();c.h.backup_fail=True;self.assertEqual(c.install(),(False,'backup_failed_active_unchanged'));c.old();self.assertIsNone(c.h.journal);self.assertEqual(c.changes(),[])
        self.assertEqual(c.h.packages[c.h.backup].version,'partial')
    def test_journal_write_failure_leaves_complete_backup_and_old_active(self):
        c=Context();c.h.write_fail=True;self.assertEqual(c.install(),(False,'journal_write_active_unchanged'));c.old();self.assertEqual(c.changes(),[])
        self.assertIsNotNone(c.h.packages[c.h.backup]);self.assertIsNone(c.h.journal)
    def test_failed_second_replacement_rolls_back_recognized_mixture(self):
        c=Context();c.h.fail_replace_at=2;ok,reason=c.install();self.assertFalse(ok);self.assertIn('predecessor_restored',reason);c.old()
        self.assertEqual(c.changes(),['replace:main.lua','replace:main.lua','replace:VERSION']);self.assertIsNone(c.h.journal);self.assertIsNotNone(c.h.packages[c.h.backup])
    def test_process_loss_after_main_keeps_active_present_and_recovers_old(self):
        c=Context();c.h.cut_after_replace=1
        self.assertEqual(c.install(),(False,'unexpected_io_failure_journal_preserved'));self.assertIsNotNone(c.h.packages[c.h.active]);self.assertIsNotNone(c.h.journal)
        c.h.cut_after_replace=None;self.assertEqual(c.recover(),(True,'predecessor_restored'));c.old();self.assertFalse(c.h.locked)
    def test_process_loss_after_version_keeps_complete_new_and_cleans_without_rollback(self):
        c=Context();c.h.cut_after_replace=2;self.assertFalse(c.install()[0]);c.assert_pack(c.r.new,c.r.new_version)
        before=len(c.changes());c.h.cut_after_replace=None;self.assertEqual(c.recover(),(True,'target_intact'));self.assertEqual(len(c.changes()),before)
    def test_both_recognized_mixtures_recover_using_verified_old_backup(self):
        for oldmain in [True,False]:
            c=Context();c.journal();active=c.h.packages[c.h.active]
            active.hashes['main.lua']=c.r.old['main.lua'] if oldmain else c.r.new['main.lua']
            active.hashes.VERSION=c.r.new.VERSION if oldmain else c.r.old.VERSION
            active.version=c.r.new_version if oldmain else c.r.old_version
            self.assertEqual(c.recover(),(True,'predecessor_restored'));c.old();self.assertIsNotNone(c.h.packages[c.h.backup])
    def test_unknown_later_main_version_or_metadata_are_preserved(self):
        for name in ['main.lua','VERSION','_meta.lua']:
            c=Context();c.journal();c.h.packages[c.h.active].hashes[name]='9'*64;raw=c.h.journal
            self.assertEqual(c.recover(),(False,'unknown_active_preserved'));self.assertEqual(c.changes(),[]);self.assertEqual(c.h.journal,raw)
            self.assertEqual(c.h.packages[c.h.active].hashes[name],'9'*64)
    def test_missing_or_unreadable_active_is_never_created_or_deleted(self):
        c=Context();c.journal();c.h.packages[c.h.active]=None
        self.assertEqual(c.recover(),(False,'unknown_active_preserved'));self.assertIsNone(c.h.packages[c.h.active]);self.assertEqual(c.changes(),[])
    def test_corrupt_backup_blocks_rollback_and_keeps_mixture(self):
        c=Context();c.journal();c.h.packages[c.h.active].hashes['main.lua']=c.r.new['main.lua'];c.h.packages[c.h.backup].hashes['main.lua']='9'*64
        self.assertEqual(c.recover(),(False,'predecessor_unverified'));self.assertEqual(c.changes(),[]);self.assertIsNotNone(c.h.journal)
    def test_full_new_also_requires_retained_verified_predecessor_before_clearing(self):
        c=Context();c.journal();c.h.packages[c.h.active]=c.clone(c.h.newpack);c.h.packages[c.h.backup]=None
        self.assertEqual(c.recover(),(False,'predecessor_unverified'));self.assertIsNotNone(c.h.journal);self.assertEqual(c.changes(),[])
    def test_full_old_also_requires_verified_retained_backup(self):
        c=Context();c.journal();c.h.packages[c.h.backup]=None
        self.assertEqual(c.recover(),(False,'predecessor_unverified'));self.assertIsNotNone(c.h.journal);self.assertEqual(c.changes(),[])
    def test_complete_old_at_pre_activation_cut_only_cleans_journal(self):
        c=Context();c.journal();self.assertEqual(c.recover(),(True,'predecessor_intact'));c.old();self.assertEqual(c.changes(),[])
    def test_rollback_cleanup_failure_is_truthful_and_idempotent(self):
        c=Context();c.journal();c.h.packages[c.h.active].hashes['main.lua']=c.r.new['main.lua'];c.h.clear_fail=True
        self.assertEqual(c.recover(),(True,'predecessor_restored_journal_retained'));c.old();self.assertIsNotNone(c.h.journal)
        c.h.clear_fail=False;before=len(c.changes());self.assertEqual(c.recover(),(True,'predecessor_intact'));self.assertEqual(len(c.changes()),before);self.assertIsNone(c.h.journal)
    def test_successful_install_reports_retained_journal_without_lying_about_failure(self):
        c=Context();c.h.clear_fail=True;self.assertEqual(c.install(),(True,'installed_journal_retained'));c.assert_pack(c.r.new,c.r.new_version);self.assertIsNotNone(c.h.journal)
        self.assertEqual(c.install(),(False,'journal_exists_or_unreadable'))
    def test_malformed_unreadable_or_unbound_journal_never_mutates_or_deletes(self):
        for raw in ['bad','KOTA1\n','a'*2049]:
            c=Context();c.h.journal=raw;self.assertEqual(c.recover(),(False,'invalid_journal'));self.assertEqual(c.h.journal,raw);self.assertEqual(c.changes(),[])
        c=Context();c.h.unreadable=True;self.assertFalse(c.recover()[0]);self.assertFalse(c.install()[0]);self.assertEqual(c.changes(),[])
    def test_no_journal_does_not_scan_or_select_historical_backup(self):
        c=Context();c.h.packages[c.h.backup]=c.clone(c.h.oldpack);self.assertEqual(c.recover(),(True,'absent'));self.assertEqual(c.changes(),[])
    def test_unexpected_exception_releases_lock_and_keeps_journal_for_startup(self):
        c=Context();c.h.cut_after_replace=1;self.assertFalse(c.install()[0]);self.assertEqual(c.h.locks,c.h.unlocks);self.assertFalse(c.h.locked);self.assertIsNotNone(c.h.journal)
    def test_record_round_trip_binds_every_file_and_unique_attempt(self):
        c=Context();raw=c.module.encode(c.r);self.assertIsNotNone(c.module.decode(raw));self.assertIsNone(c.module.decode(raw+'\n'))
        self.assertIsNone(c.module.decode(raw.replace('KOTA2','KOTA1')));self.assertIsNone(c.module.decode(raw.replace('\n','\r\n')))
        c.r.backup='kindleprinter.ota-backup-0.5.36-dev-1792269999-999999';self.assertIsNone(c.module.encode(c.r))

if __name__=='__main__':unittest.main(verbosity=2)
