"""Load the complete portable client against inert KOReader-shaped seams.

No real filesystem, FFI, credentials, network or hardware API is exposed to Lua.
These are independent fixture objects, not copies of KOReader implementation.
SPDX-License-Identifier: GPL-3.0-or-later
"""
from lupa.luajit21 import LuaRuntime

BOOTSTRAP = r'''
local function forbidden()error('fixture blocked host I/O')end
io={open=forbidden,popen=forbidden}
os={time=function()return 1800000000 end,date=function()return '2027-01-15T00:00:00Z'end,
execute=forbidden,remove=forbidden,rename=forbidden,getenv=function()return nil end}
local h={shown={},notices={},requests={},now=1800000000};__h=h
local widget={}
function widget:extend(t)return setmetatable(t or{},{__index=self})end
function widget:new(t)return setmetatable(t or{},{__index=self})end
function widget:onCloseWidget()end
function widget:getSize()return {w=self.width or 100,h=self.height or 40}end
local power={getCapacity=function()return 87 end,isCharging=function()return false end}
local device={screen={getWidth=function()return 1264 end,getHeight=function()return 1680 end,
isColorEnabled=function()return false end},getPowerDevice=function()return power end}
local network={isConnected=function()return not h.offline end,isWifiOn=function()return true end}
local ui={}
function ui:show(v)h.shown[#h.shown+1]=v end
function ui:close()end
function ui:setDirty()end
function ui:scheduleIn()end
function ui:unschedule()end
local modules={['device']=device,['gettext']=function(v)return v end,
['datastorage']={getFullDataDir=function()return '/inert-fixture'end},
['ui/widget/container/widgetcontainer']=widget,['ui/uimanager']=ui,
['luasettings']=widget,['pluginshare']=widget,
['ui/font']={getFace=function(_,_,size)return {size=size}end},
['ui/network/manager']=network,['libs/libkoreader-lfs']={symlinkattributes=forbidden,currentdir=forbidden},
['ffi/blitbuffer']={COLOR_WHITE=0,COLOR_BLACK=15},
['logger']={dbg=function()end,info=function()end,warn=function()end,err=function()end},
['random']={uuid=function()return '10000000-0000-4000-8000-000000000001'end},
['socket.url']={parse=forbidden},
['json']={decode=function()return h.payload end,encode=function(v)h.encoded=v;return '{}'end}}
function require(name)
if modules[name]then return modules[name]end
if name:match('^ui/')then return widget end
return forbidden()
end
function h.instance(class)
local t=setmetatable({settings={proxy_mode='https',refresh_seconds=60,traffic_window='5m',traffic_view='pages',token_file='/inert-reader',control_token_file='/inert-controller'},
active_tab=1,running=true,quick_menu_count=0,tab_cache={},stable_polls={},screen_changes=0}, {__index=class})
function t:requestRemote(path,method,token,payload,accept,maximum)
h.requests[#h.requests+1]={path=path,method=method,token=token,payload=payload}
return h.body or 'fixture',h.code or 200
end
function t:notice(text)h.notices[#h.notices+1]=text end
function t:saveSettings()return true end
return t
end
'''


class Client:
    def __init__(self, source: str):
        self.lua = LuaRuntime(unpack_returned_tuples=True, register_eval=False, register_builtins=False)
        self.lua.execute(BOOTSTRAP)
        self.kind = self.lua.execute(source, name="@portable-main.lua")
        self.h = self.lua.globals()["__h"]
        self.app = self.h.instance(self.kind)

    def table(self, value):
        if isinstance(value, dict):
            return self.lua.table_from({key: self.table(v) for key, v in value.items()})
        if isinstance(value, list):
            return self.lua.table_from([self.table(v) for v in value])
        return value

    def respond(self, value, code=200):
        self.h.payload = self.table(value)
        self.h.code = code

    def call(self, name, *args):
        return self.app[name](self.app, *args)
