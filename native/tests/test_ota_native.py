"""Native adapter I/O is replaced by a bounded in-memory POSIX/FFI fake.
It validates ARM constants/procfs sizing/private modes/ancestors/journal fsync.
No native FFI, filesystem, socket or command runs in these tests.
"""
from pathlib import Path
# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from lupa.luajit21 import LuaRuntime
HERE=Path(__file__).resolve().parents[1]
SETUP=r'''
local h={nodes={},log={},fds={},fd=10,digests={}}
__h=h
local function directory(path,members)
    h.nodes[path]={mode='directory',uid=0,permissions='rwx------',nlink=1,size=0,members=members or {}}
end
local function file(path,body,digest)
    h.nodes[path]={mode='file',uid=0,permissions='rw-------',nlink=1,size=#body,body=body,digest=digest}
    if digest then h.digests[body]=digest end
    local parent,name=path:match('^(.*)/([^/]+)$');local dir=h.nodes[parent]
    if dir and dir.members then
        for _,n in ipairs(dir.members) do if n==name then return end end
        dir.members[#dir.members+1]=name
    end
end
h.directory=directory;h.file=file
for _,path in ipairs({'/var/local/kindle-printer-dashboard','/var/local/kindle-printer-dashboard/ota',
    '/mnt','/mnt/us','/mnt/us/koreader','/mnt/us/koreader/plugins'}) do directory(path) end
local parent='/mnt/us/koreader/plugins'
h.active=parent..'/kindleprinter.koplugin';h.stage=parent..'/kindleprinter.ota-stage-0.5.36-dev-1792269999-123456'
h.backup=parent..'/kindleprinter.ota-backup-0.5.35-dev-1792269999-123456';h.journal='/var/local/kindle-printer-dashboard/ota-file-recovery.transaction';h.lock='/var/local/kindle-printer-dashboard/ota-file-recovery.lock'
local old={['_meta.lua']=string.rep('a',64),['main.lua']=string.rep('b',64),VERSION=string.rep('c',64)}
local new={['_meta.lua']=string.rep('a',64),['main.lua']=string.rep('e',64),VERSION=string.rep('f',64)}
for _,pack in ipairs({{h.active,'0.5.35-dev',old},{h.stage,'0.5.36-dev',new}}) do
    directory(pack[1],{'_meta.lua','main.lua','VERSION'})
    for _,name in ipairs({'_meta.lua','main.lua','VERSION'}) do
        file(pack[1]..'/'..name,name=='VERSION' and pack[2]..'\n' or name=='_meta.lua' and 'shared metadata' or pack[2]..name,pack[3][name])
    end
end
file('/proc/usid','TEST000000000001\n');h.nodes['/proc/usid'].size=0
h.record={serial='TEST000000000001',parent=parent,old_version='0.5.35-dev',new_version='0.5.36-dev',
    stage=h.stage:match('[^/]+$'),backup=h.backup:match('[^/]+$'),old=old,new=new}
local lfs={}
function lfs.symlinkattributes(path) return h.nodes[path] end
function lfs.mkdir(path) if h.nodes[path] then return nil end;directory(path);return true end
function lfs.rmdir(path)
    local node=h.nodes[path];if not node or node.mode~='directory' or #(node.members or {})>0 then return nil end
    for other in pairs(h.nodes) do if other:sub(1,#path+1)==path..'/' then return nil end end
    h.nodes[path]=nil;return true
end
function lfs.dir(path)
    local row=assert(h.nodes[path]);local i=0
    return function() i=i+1;return row.members[i] end
end
local ffi={os='Linux',arch='arm',C={}}
function ffi.cdef() end
function ffi.cast(_,value) return value end
function ffi.errno() return h.errno or 0 end
function ffi.C.open(path,flags,mode)
    if mode then
        assert(mode==384 and (flags==557249 or flags==557122),'Incorrect ARM create flags')
        if h.nodes[path] and flags==557249 then return -1 end
        if not h.nodes[path] then file(path,'') end
    else assert(flags==573440,'Incorrect ARM directory flags') end
    if not h.nodes[path] then return -1 end
    h.fd=h.fd+1;h.fds[h.fd]=path;h.log[#h.log+1]='open:'..path;return h.fd
end
function ffi.C.write(fd,body,count)
    local path=assert(h.fds[fd]);local node=h.nodes[path];node.body=node.body..body:sub(1,count);node.size=#node.body;return count
end
function ffi.C.fsync(fd)
    local path=assert(h.fds[fd]);h.log[#h.log+1]='fsync:'..path
    if h.fail_fsync then return -1 end;return 0
end
function ffi.C.close(fd) assert(h.fds[fd]);if h.lockfd==fd then h.lockfd=nil end;h.fds[fd]=nil;return 0 end
function ffi.C.flock(fd,operation)
    assert(h.fds[fd]==h.lock)
    if operation==6 then if h.lock_busy or h.lockfd then return -1 end;h.lockfd=fd;return 0 end
    assert(operation==8);h.lockfd=nil;return 0
end
function ffi.C.sync() h.log[#h.log+1]='sync' end
io={}
function io.open(path,mode)
    assert(mode=='rb','Read-only Lua IO expected');local node=h.nodes[path]
    if not node or node.mode~='file' then return nil end
    return {read=function(_,maximum) return node.body:sub(1,maximum) end,close=function() end}
end
os={time=function() return 1792269999 end}
function os.rename(from,to)
    if h.nodes[to] and (h.nodes[to].mode~='file' or not h.nodes[from] or h.nodes[from].mode~='file') then return nil end
    local selected={}
    for path,node in pairs(h.nodes) do
        if path==from or path:sub(1,#from+1)==from..'/' then selected[path]=node end
    end
    if not next(selected) then return nil end
    for path,node in pairs(selected) do h.nodes[to..path:sub(#from+1)]=node;h.nodes[path]=nil end
    local p,n=to:match('^(.*)/([^/]+)$');local d=h.nodes[p]
    if d and d.members then local found=false;for _,x in ipairs(d.members) do if x==n then found=true end end;if not found then d.members[#d.members+1]=n end end
    local p,n=from:match('^(.*)/([^/]+)$');local d=h.nodes[p]
    if d and d.members then for i=#d.members,1,-1 do if d.members[i]==n then table.remove(d.members,i) end end end
    h.log[#h.log+1]='rename:'..from..'>'..to;return true
end
function os.remove(path) if not h.nodes[path] then return nil end;h.nodes[path]=nil;h.log[#h.log+1]='remove:'..path;return true end
require=function(name)
    if name=='libs/libkoreader-lfs' then return lfs end
    if name=='ffi' then return ffi end
    if name=='ffi/sha2' then return {sha256=function(body)
        if h.digests[body] then return h.digests[body] end
        for _,node in pairs(h.nodes) do if node.body==body and node.digest then return node.digest end end
        error('Unexpected body to SHA fake')
    end} end
    error('Forbidden dependency')
end
'''
class NativeFileAtomic(unittest.TestCase):
    def context(self,setup=''):
        lua=LuaRuntime(unpack_returned_tuples=True);lua.execute(SETUP)
        if setup:lua.execute(setup)
        module=lua.execute((HERE/'ota-file-recovery.lua.in').read_text(encoding='utf-8').replace('__INKBOARD_SERIAL__','TEST000000000001'))
        machine=module.new_native()
        return lua,module,machine,lua.globals()['__h']
    def machine(self,value):
        return value[0] if isinstance(value,tuple) else value
    def test_native_full_install_keeps_active_meta_and_retained_backup(self):
        lua,module,raw,h=self.context();machine=self.machine(raw)
        self.assertEqual(machine.install(machine,h.record),(True,'installed'))
        self.assertEqual(h.nodes[h.active+'/VERSION'].body,'0.5.36-dev\n')
        self.assertEqual(h.nodes[h.active+'/_meta.lua'].body,'shared metadata')
        self.assertEqual(h.nodes[h.backup+'/VERSION'].body,'0.5.35-dev\n');self.assertIsNone(h.nodes[h.journal])
        self.assertIsNotNone(h.nodes[h.active]);self.assertIsNone(h.lockfd)
        replacements=[h.log[i] for i in range(1,len(h.log)+1) if h.log[i].startswith('rename:')]
        self.assertTrue(replacements[-2].endswith('>'+h.active+'/main.lua'));self.assertTrue(replacements[-1].endswith('>'+h.active+'/VERSION'))
    def test_native_actual_function_null_and_no_renameat2_dependency(self):
        lua,module,raw,h=self.context();machine=self.machine(raw)
        self.assertIsNotNone(machine);self.assertTrue(machine.install(machine,h.record)[0]);self.assertIsNone(lua.globals().require('ffi').C.syscall)
    def test_native_trailing_nul_serial_and_zero_stat_size(self):
        lua,module,raw,h=self.context("__h.nodes['/proc/usid'].body='TEST000000000001\\0\\r\\n'")
        machine=self.machine(raw);self.assertIsNotNone(machine);self.assertTrue(machine.install(machine,h.record)[0])
        _,_,raw,_=self.context("__h.nodes['/proc/usid'].body='TEST0000\\00000000001'")
        self.assertIsNone(raw[0]);self.assertEqual(raw[1],'private_storage_or_identity')
    def test_native_constructor_refuses_private_mode_uid_symlink_or_lock_busy(self):
        for field,value in [('permissions',"'rwxrwxrwx'"),('uid','1000'),('mode',"'link'")]:
            _,_,raw,h=self.context("__h.nodes['/var/local/kindle-printer-dashboard']."+field+'='+value)
            self.assertIsNone(raw[0]);self.assertEqual(raw[1],'private_storage_or_identity');self.assertIsNone(h.nodes[h.journal])
        _,_,raw,_=self.context('__h.lock_busy=true');self.assertIsNone(raw[0]);self.assertEqual(raw[1],'update_busy_or_lock_unavailable')
    def test_native_unknown_later_active_preserved_with_journal(self):
        lua,module,raw,h=self.context();machine=self.machine(raw)
        h.file(h.journal,module.encode(h.record));h.file(h.active+'/main.lua','later unknown main','9'*64)
        self.assertEqual(machine.recover(machine),(False,'unknown_active_preserved'));self.assertIsNotNone(h.nodes[h.journal])
        self.assertEqual(h.nodes[h.active+'/main.lua'].digest,'9'*64)
    def test_native_ancestor_symlink_or_extra_plugin_entry_refuses_before_backup(self):
        for setup in ["__h.nodes['/mnt/us/koreader'].mode='link'", "__h.nodes[__h.stage].members[4]='extra'"]:
            lua,module,raw,h=self.context(setup);machine=self.machine(raw)
            self.assertEqual(machine.install(machine,h.record),(False,'package_changed'));self.assertIsNone(h.nodes[h.backup]);self.assertIsNone(h.nodes[h.journal])
    def test_native_private_journal_hardlink_publicmode_or_wronguid_refused(self):
        for field,value in [('nlink',2),('uid',1000),('permissions','rw-r--r--')]:
            lua,module,raw,h=self.context();machine=self.machine(raw);h.file(h.journal,module.encode(h.record));h.nodes[h.journal][field]=value
            self.assertEqual(machine.recover(machine),(False,'journal_unreadable'));self.assertIsNotNone(h.nodes[h.journal])
    def test_native_private_journal_fsync_failure_never_touches_active(self):
        lua,module,raw,h=self.context();machine=self.machine(raw);h.fail_fsync=True
        self.assertEqual(machine.install(machine,h.record),(False,'journal_write_active_unchanged'))
        self.assertEqual(h.nodes[h.active+'/VERSION'].body,'0.5.35-dev\n');self.assertIsNone(h.nodes[h.journal]);self.assertIsNone(h.lockfd)
    def test_native_constructor_lock_file_permissions_and_hardlinks_refuse(self):
        for field,value in [('nlink','2'),('uid','1000'),('permissions',"'rw-r--r--'"),('mode',"'link'")]:
            setup="__h.file(__h.lock,'');__h.nodes[__h.lock]."+field+'='+value
            _,_,raw,_=self.context(setup);self.assertIsNone(raw[0]);self.assertEqual(raw[1],'update_busy_or_lock_unavailable')

    def test_native_mixed_state_process_cut_recovery_restores_old_bytes(self):
        lua,module,raw,h=self.context();machine=self.machine(raw)
        lua.execute("local rename=os.rename;os.rename=function(from,to) local ok=rename(from,to);if ok and to==__h.active..'/main.lua' and not __h.cut then __h.cut=true;error('PROCESS CUT') end;return ok end")
        self.assertEqual(machine.install(machine,h.record),(False,'unexpected_io_failure_journal_preserved'))
        self.assertIsNotNone(h.nodes[h.journal]);self.assertEqual(h.nodes[h.active+'/VERSION'].body,'0.5.35-dev\n')
        restored=self.machine(module.new_native());self.assertEqual(restored.recover(restored),(True,'predecessor_restored'))
        self.assertEqual(h.nodes[h.active+'/main.lua'].body,'0.5.35-devmain.lua');self.assertIsNone(h.nodes[h.journal]);self.assertIsNotNone(h.nodes[h.backup])
    def test_native_complete_new_cut_retains_new_and_verified_backup(self):
        lua,module,raw,h=self.context();machine=self.machine(raw)
        lua.execute("local rename=os.rename;os.rename=function(from,to) local ok=rename(from,to);if ok and to==__h.active..'/VERSION' and not __h.cut then __h.cut=true;error('PROCESS CUT') end;return ok end")
        self.assertFalse(machine.install(machine,h.record)[0]);self.assertEqual(h.nodes[h.active+'/VERSION'].body,'0.5.36-dev\n')
        restored=self.machine(module.new_native());self.assertEqual(restored.recover(restored),(True,'target_intact'))
        self.assertEqual(h.nodes[h.active+'/main.lua'].body,'0.5.36-devmain.lua');self.assertIsNone(h.nodes[h.journal]);self.assertIsNotNone(h.nodes[h.backup])

if __name__=='__main__':unittest.main(verbosity=2)
