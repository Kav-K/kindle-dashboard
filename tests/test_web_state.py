"""Exercise the shipped browser script against a controlled DOM, without providers."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from inkboard.cli import main
from inkboard.config import initialize, load


DOM_TEST = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
let source='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>source+=s);process.stdin.on('end',async()=>{
 class Node {
  constructor(tag){this.tagName=tag;this.children=[];this.listeners={};this.dataset={};this._text='';this.value='';this.checked=false;this.disabled=false;this.parentNode=null}
  set textContent(value){this._text=String(value);this.replaceChildren()}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('')}
  append(...nodes){for(let n of nodes){if(typeof n==='string'){const t=new Node('#text');t._text=n;n=t}if(n.parentNode){n.parentNode.children=n.parentNode.children.filter(c=>c!==n)}n.parentNode=this;this.children.push(n)}}
  contains(other){return this===other||this.children.some(n=>n.contains(other))}
  replaceChildren(...nodes){if(this.contains(document.activeElement))document.activeElement=null;for(const n of this.children)n.parentNode=null;this.children=[];this.append(...nodes)}
  addEventListener(type,fn){this.listeners[type]=fn}
  setAttribute(name,value){this[name]=value}
  focus(){document.activeElement=this}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(n=>n!==this);if(this.contains(document.activeElement))document.activeElement=null;this.parentNode=null}
  querySelectorAll(selector){let result=[];for(const n of this.children){if(selector==='button[data-action]'&&n.tagName==='button'&&n.dataset.action)result.push(n);result.push(...n.querySelectorAll(selector))}return result}
 }
 const elements=new Map();const document={activeElement:null,getElementById(id){if(!elements.has(id))elements.set(id,new Node('div'));return elements.get(id)},createElement(tag){return new Node(tag)},createTextNode(text){const n=new Node('#text');n._text=text;return n}};
 const module={id:'media',type:'plex',name:'Fixture media',available:true,stale:false,observed_at:'2026-10-18T00:00:00Z',summary:'Current fixture',capabilities:['seerr_search','seerr_tv','seerr_request','refresh_library'],details:{seerr_available:true,streams:[],libraries:[],requests:[],destinations:['Fixture SSD One','Fixture SSD Two']}};
 let current={name:'Fixture',version:'0.1.0',modules:[module]},lastCommand,posts=0,failRefresh=false,pendingPost=null,releasePost=null,serial=0;
 const context=vm.createContext({document,location:{pathname:'/kindle-dashboard/'},window:{addEventListener(){}},AbortSignal:{timeout(){}},setInterval(){return 1},clearInterval(){},setTimeout(fn){queueMicrotask(fn)},crypto:{randomUUID(){return '00000000-0000-4000-8000-'+String(++serial).padStart(12,'0')}},confirm:()=>{throw Error('Browser modal must not be used for media requests')},prompt:()=>{throw Error('Browser prompt must not be used for destinations')},URL,console,
  fetch:async(path,options)=>{if(path.endsWith('/api/v1/modules')){if(failRefresh)throw Error('Fixture offline');return {ok:true,json:async()=>JSON.parse(JSON.stringify(current))}}
   if(options.method==='POST'){posts++;lastCommand=JSON.parse(options.body);if(pendingPost)await pendingPost;return {ok:true,json:async()=>({id:lastCommand.request_id,state:'queued'})}}
   const result=lastCommand.action==='seerr_search'?{results:[{id:42,title:'Fixture Show',media_type:'tv',overview:'Synthetic result'}]}:lastCommand.action==='seerr_tv'?{seasons:[{number:1,name:'Season 1'},{number:2,name:'Season 2'}]}:{};
   return {ok:true,json:async()=>({id:lastCommand.request_id,state:'accepted',detail:'state_confirmed',result})}
  }});
 try{
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  await run("reader='fixture-reader';controller='fixture-controller';refresh()");
  const workspace=run("mediaWorkspaces.get('media')"),input=workspace.input;
  input.value='A show being typed';input.focus();
  await run('refresh({background:true})');
  assert.equal(run("mediaWorkspaces.get('media').input"),input);assert.equal(input.value,'A show being typed');assert.equal(document.activeElement,input);
  const search=workspace.node.querySelectorAll('button[data-action]').find(b=>b.textContent==='Search Seerr');await search.listeners.click();
  function descendants(node){return node.children.flatMap(n=>[n,...descendants(n)])}
  const results=workspace.results,request=descendants(results).find(n=>n.tagName==='button'&&n.textContent==='Request');assert.ok(request);await request.listeners.click();
  const season=descendants(results).find(n=>n.tagName==='input'&&n.type==='checkbox');assert.ok(season);season.checked=true;season.focus();
  await run('refresh({background:true})');
  assert.equal(workspace.results,results);assert.equal(descendants(results).find(n=>n===season),season);assert.equal(season.checked,true);assert.equal(document.activeElement,season);
  const submitSeasons=descendants(results).find(n=>n.tagName==='button'&&n.textContent==='Submit selected seasons');await submitSeasons.listeners.click();
  let confirmation=workspace.confirmation;assert.ok(confirmation);const beforeCancel=posts;const cancel=descendants(confirmation).find(n=>n.tagName==='button'&&n.textContent==='Cancel');cancel.listeners.click();assert.equal(workspace.confirmation,null);assert.equal(posts,beforeCancel);assert.equal(season.checked,true);
  await submitSeasons.listeners.click();confirmation=workspace.confirmation;const destination=descendants(confirmation).find(n=>n.tagName==='select');destination.value='Fixture SSD Two';
  await run('refresh({background:true})');assert.equal(workspace.confirmation,confirmation);assert.equal(destination.value,'Fixture SSD Two');assert.equal(season.checked,true);
  const confirmRequest=descendants(confirmation).find(n=>n.tagName==='button'&&n.textContent==='Confirm request');await confirmRequest.listeners.click();assert.equal(lastCommand.action,'seerr_request');assert.equal(lastCommand.parameters.destination,'Fixture SSD Two');assert.deepEqual(lastCommand.parameters.seasons,[1]);assert.equal(lastCommand.parameters.media_id,42);assert.equal(confirmRequest.disabled,true);
  await run('refresh({background:true})');assert.equal(confirmRequest.disabled,true);assert.equal(workspace.confirmation,confirmation);
  pendingPost=new Promise(resolve=>releasePost=resolve);const operation=run("command(currentModule('media'),'refresh_library',{library_id:'1'})");await Promise.resolve();
  assert.match(elements.get('message').textContent,/^Sending refresh_library/);await run('refresh({background:true})');assert.match(elements.get('message').textContent,/^Sending refresh_library/);
  releasePost();await operation;pendingPost=null;assert.equal(elements.get('message').textContent,'Confirmed by source.');await run('refresh({background:true})');assert.equal(elements.get('message').textContent,'Confirmed by source.');
  current.modules[0].stale=true;current.modules[0].capabilities=[];await run('refresh({background:true})');assert.ok(workspace.node.querySelectorAll('button[data-action]').every(b=>b.disabled));assert.equal(season.checked,true);
  const before=posts;await run("command(currentModule('media'),'seerr_request',{media_id:42,media_type:'tv',seasons:[1]})");assert.equal(posts,before);
  failRefresh=true;await run('refresh({background:true})');assert.match(elements.get('health').textContent,/refresh unavailable/);assert.equal(season.checked,true);assert.ok(workspace.node.querySelectorAll('button[data-action]').every(b=>b.disabled));
  elements.get('disconnect').onclick();assert.equal(run('mediaWorkspaces.size'),0);assert.equal(elements.get('workspaces').children.length,0);assert.equal(run('reader'), '');assert.equal(run('controller'),'');
  process.stdout.write('Persistent media DOM, checked seasons, inline destination confirmation/cancellation, live capability gates, command feedback and disconnect privacy verified\n');
 }catch(error){console.error(error.stack);process.exitCode=1}
});
"""


class BrowserStateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the controlled DOM execution")
    def test_media_forms_survive_live_refresh_with_safe_commands(self):
        html = Path(__file__).parents[1] / "src" / "inkboard" / "web.html"
        script = html.read_text(encoding="utf-8").split("<script>", 1)[1].split("</script>", 1)[0]
        result = subprocess.run([shutil.which("node"), "-e", DOM_TEST], input=script, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Persistent media DOM", result.stdout)

    def test_role_secret_setter_rejects_out_of_loader_bounds_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "private"
            initialize(home)
            before = (home / "secrets.json").read_bytes()
            for length in (31, 513):
                with patch("builtins.input", return_value="REPLACE"), patch("getpass.getpass", return_value="x" * length), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["set-secret", "--directory", str(home), "--name", "reader_token"]), 2)
                self.assertEqual((home / "secrets.json").read_bytes(), before)
            with patch("builtins.input", return_value="REPLACE"), patch("getpass.getpass", return_value="x" * 512), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["set-secret", "--directory", str(home), "--name", "reader_token"]), 0)
            self.assertEqual(len(load(home)[1]["reader_token"]), 512)


if __name__ == "__main__":
    unittest.main()
