"""Exercise the real CLI in a fresh virtual environment with simulated sources.

No printer, media account, paid API, production server or privileged service is
used. An owned loopback server is started, stopped and restarted. Credentials
and the paired test Kindle package remain inside a temporary directory.
"""
from __future__ import annotations
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    wheel=parser.add_mutually_exclusive_group()
    wheel.add_argument('--wheel',type=Path,help='Install this wheel; otherwise install the source checkout')
    wheel.add_argument('--wheel-dir',type=Path,help='Directory containing exactly one built wheel')
    args=parser.parse_args()
    if args.wheel_dir:
        wheels=list(args.wheel_dir.resolve().glob('*.whl'))
        if len(wheels)!=1:raise RuntimeError('Choose a directory containing exactly one wheel')
        package=wheels[0]
    else:package=args.wheel.resolve() if args.wheel else ROOT
    env=dict(os.environ)
    env.pop('PYTHONPATH',None)
    env['PYTHONUTF8']='1'
    checks=[]
    with tempfile.TemporaryDirectory(prefix='inkboard-clean-') as temporary:
        work=Path(temporary);venv=work/'venv';setup=work/'private'
        def run(command,expected=0,timeout=120):
            p=subprocess.run(list(map(str,command)),cwd=work,env=env,capture_output=True,timeout=timeout)
            if p.returncode!=expected:
                raise RuntimeError('Setup subprocess failed: '+str(command[0])+' (no private output disclosed)')
            return p
        run([sys.executable,'-m','venv',venv])
        runtime=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        run([runtime,'-m','pip','install',str(package)],timeout=180)
        run([runtime,'-m','inkboard','init','--directory',setup,'--demo'])
        credentials=json.loads((setup/'secrets.json').read_bytes())
        initial_secret_hash=hashlib.sha256((setup/'secrets.json').read_bytes()).hexdigest()
        run([runtime,'-m','inkboard','check','--directory',setup])
        run([runtime,'-m','inkboard','init','--directory',setup,'--demo'],expected=2)
        assert hashlib.sha256((setup/'secrets.json').read_bytes()).hexdigest()==initial_secret_hash
        checks.append('fresh install, offline check and non-overwriting repeat setup')
        with socket.socket() as allocated:
            allocated.bind(('127.0.0.1',0));port=allocated.getsockname()[1]
        process=None
        log=(work/'owned-server.log').open('wb')
        def stop():
            nonlocal process
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=8)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
                process=None
        def get(path,token=None,method='GET',payload=None):
            c=http.client.HTTPConnection('127.0.0.1',port,timeout=2)
            headers={} if token is None else {'Authorization':'Bearer '+token}
            body=None
            if payload is not None:body=json.dumps(payload);headers['Content-Type']='application/json'
            try:
                c.request(method,'/kindle-dashboard'+path,body,headers)
                r=c.getresponse();raw=r.read(4_000_001)
                if len(raw)>4_000_000:raise RuntimeError('Bounded setup response exceeded')
                return r.status,raw
            finally:c.close()
        def start():
            nonlocal process
            process=subprocess.Popen([str(runtime),'-m','inkboard','serve','--directory',str(setup),'--port',str(port)],cwd=work,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            deadline=time.monotonic()+20
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('Owned demo server exited before readiness')
                try:
                    if get('/healthz')[0]==200:return
                except (OSError,http.client.HTTPException):pass
                time.sleep(.1)
            raise RuntimeError('Owned demo server did not become ready')
        try:
            start()
            status,page=get('/')
            assert status==200 and b'Inkboard' in page
            assert all(value.encode() not in page for value in credentials.values())
            assert get('/api/v1/modules')[0]==401
            status,raw=get('/api/v1/modules',credentials['reader_token'])
            assert status==200 and json.loads(raw)['modules']
            assert all(value.encode() not in raw for value in credentials.values())
            assert get('/api/v1/commands',credentials['reader_token'],'POST',{'request_id':str(uuid.uuid4()),'printer_id':'demo-one','action':'pause'})[0] in (401,403)
            status,png=get('/dashboard.png',credentials['reader_token'])
            assert status==200 and png[:8]==b'\x89PNG\r\n\x1a\n' and struct.unpack('>II',png[16:24])==(1264,1680)
            checks.append('real authenticated HTTP, role separation and rendered dashboard PNG')
            stop();start()
            assert get('/api/v1/printers',credentials['reader_token'])[0]==200
            assert hashlib.sha256((setup/'secrets.json').read_bytes()).hexdigest()==initial_secret_hash
            checks.append('server restart preserves configuration and authentication')
            config=json.loads((setup/'config.json').read_bytes())
            config.update(public_url='https://dashboard.example.net/kindle-dashboard',
                kindle={'serial':'DEMO123456789012','printer_ids':['demo-one','demo-two'],'timezone':'UTC'})
            # This private synthetic profile is never installed on a physical device.
            (setup/'config.json').write_text(json.dumps(config)+'\n',encoding='utf-8')
            run([runtime,'-m','inkboard','check','--directory',setup])
            output=work/'paired-build'
            run([runtime,ROOT/'tools/build_kindle.py','--config',setup/'config.json','--secrets',setup/'secrets.json','--out',output])
            manifest=json.loads((output/'client_updates/manifest.json').read_bytes())
            assert manifest['device_serial']=='DEMO123456789012'
            assert {row['name'] for row in manifest['files']}=={'main.lua','VERSION','_meta.lua'}
            shutil.copytree(output/'client_updates',setup/'client_updates')
            status,raw=get('/api/client-update/manifest',credentials['reader_token'])
            assert status==200 and json.loads(raw)==manifest
            for row in manifest['files']:
                status,raw=get('/api/client-update/file/'+manifest['version']+'/'+row['name'],credentials['reader_token'])
                assert status==200 and len(raw)==row['size'] and hashlib.sha256(raw).hexdigest()==row['sha256']
                assert all(value.encode() not in raw for value in credentials.values())
            checks.append('private per-device USB build and all authenticated OTA assets')
        finally:
            stop();log.close()
    print(json.dumps({'result':'passed','checks':checks,'real_devices_contacted':0,'service_registrations':0,'credentials_printed':False},indent=2))
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (AssertionError,RuntimeError,OSError,subprocess.SubprocessError) as error:
        print('Clean setup verification failed ('+type(error).__name__+'). No private subprocess output disclosed.',file=sys.stderr)
        raise SystemExit(1)
