const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const net=require('node:net');
const {spawn,spawnSync}=require('node:child_process');
const {JSDOM,VirtualConsole,CookieJar}=require('jsdom');
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(test,label){for(let i=0;i<200;i++){if(test())return;await delay(25)}throw Error('Timed out: '+label)}
(async()=>{
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'gimm-dom-'));
 const socket=net.createServer();await new Promise(resolve=>socket.listen(0,'127.0.0.1',resolve));const port=socket.address().port;await new Promise(resolve=>socket.close(resolve));
 const origin=`http://127.0.0.1:${port}`;
 const env={...process.env,DATABASE_PATH:path.join(dir,'ui.db'),SETUP_TOKEN:'dom-test-setup-secret',APP_ORIGIN:origin,PORT:String(port)};
 const server=spawn('.venv/bin/python',['-B','server.py'],{env,stdio:['ignore','ignore','pipe']});
 const serverErrors=[];server.stderr.on('data',b=>serverErrors.push(b.toString()));let dom;
 try{
  for(let i=0;i<100;i++){try{if((await fetch(origin+'/health')).ok)break}catch{}await delay(50)}
  const jar=new CookieJar();const errors=[];const downloads=[];const virtual=new VirtualConsole();virtual.on('jsdomError',error=>errors.push(error.message));
  dom=await JSDOM.fromURL(origin,{runScripts:'dangerously',resources:'usable',pretendToBeVisual:true,cookieJar:jar,virtualConsole:virtual,beforeParse(window){
   window.fetch=async(url,options={})=>{const headers={...options.headers};headers.Cookie=jar.getCookieStringSync(origin);if(options.method==='POST')headers.Origin=origin;const response=await fetch(new URL(url,origin),{...options,headers});for(const cookie of response.headers.getSetCookie())jar.setCookieSync(cookie,origin);return response};
   window.URL.createObjectURL=()=> 'blob:dom-test';window.URL.revokeObjectURL=()=>{};window.HTMLAnchorElement.prototype.click=function(){downloads.push(this.download)};window.scrollTo=()=>{};
   window.addEventListener('error',event=>errors.push(event.message));
  }});
  const w=dom.window,document=w.document;
  const click=selector=>{const node=document.querySelector(selector);assert.ok(node,selector);node.click()};
  const submit=selector=>document.querySelector(selector).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
  await until(()=>document.querySelector('#auth-form'),'setup form');
  document.querySelector('#email').value='dom@example.com';document.querySelector('#password').value='dom-test-password';document.querySelector('#setupToken').value='dom-test-setup-secret';submit('#auth-form');
  await until(()=>document.querySelector('h1')?.textContent==='Your trading. Connected.','dashboard');
  assert.ok(document.body.textContent.includes('Your accounts start here.'));
  assert.ok(!document.body.textContent.includes('25.218'));
  for(const route of ['accounts','rules','trades','analytics','activity','settings','overview']){
   click(`.nav[data-page="${route}"]`);assert.equal(document.querySelector('.nav.active').dataset.page,route);
  }
  click('.nav[data-page="analytics"]');click('[data-period="30"]');assert.equal(document.querySelector('[data-period="30"]').getAttribute('aria-pressed'),'true');
  click('.nav[data-page="trades"]');click('#export');assert.deepEqual(downloads,['gimm-trades.csv']);
  click('.nav[data-page="settings"]');click('#change-password');document.querySelector('#current').value='dom-test-password';document.querySelector('#new-password').value='dom-test-password-new';submit('.modal');await until(()=>!document.querySelector('.modal-bg'),'password saved');
  // Fixture accounts are inserted only in the isolated test DB, never in application startup.
  const seed=spawnSync('.venv/bin/python',['-B','-c',"import sys,time;sys.path.insert(0,'tests');from test_engine import add_account;from copier import store;store.init();add_account('master','master',1);add_account('follower','follower',2);store.execute(\"UPDATE accounts SET balance=10000,equity=10100,unrealized=100,margin=100\");store.execute('UPDATE accounts SET updated=?',(time.time(),));store.set_status('safety_heartbeat',time.time());store.set_status('connection:live',True);store.set_status('reconciled:master',time.time());store.set_status('reconciled:follower',time.time());store.set_status('heartbeat',time.time());store.set_status('trading_enabled',True);store.execute('INSERT INTO equity_samples SELECT id,?,10000,10000 FROM accounts',(int((time.time()-86400)*1000),))"],{env,encoding:'utf8'});
  assert.equal(seed.status,0,seed.stderr);
  click('#refresh');await until(()=>document.querySelector('#toast').textContent==='Workspace refreshed.','settings refresh');
  click('.nav[data-page="rules"]');await until(()=>document.querySelector('[data-rule="follower"]'),'follower form');
  document.querySelector('[name="lots"]').value='0.35';submit('[data-rule="follower"]');await until(()=>document.querySelector('#toast').textContent==='Copy rule saved.','rule saved');
  click('#pause');await until(()=>document.body.textContent.includes('NEW COPIES ACTIVE'),'copier activated');
  click('.nav[data-page="overview"]');assert.ok(document.body.textContent.includes('10,100.00'));
  assert.ok(document.querySelector('.stats').textContent.includes('Total portfolio equity'));
  assert.ok(document.querySelector('.stats').textContent.includes('20,200.00'));
  assert.ok(document.querySelector('.stats').textContent.includes('+US$200.00'));
  assert.ok(document.querySelector('.stats').textContent.includes('+1.0%'));
  assert.ok(!document.querySelector('.stats').textContent.includes('Real portfolio equity'));
  click('[data-period="today"]');assert.ok(document.querySelector('.stats').textContent.includes("Today's profit"));
  click('[data-period="all"]');assert.ok(document.querySelector('.stats').textContent.includes('Profit since connection'));
  click('#mobile-menu');assert.ok(document.querySelector('.sidebar').classList.contains('open'));
  click('.nav[data-page="analytics"]');assert.ok(document.querySelector('.sidebar')&&!document.querySelector('.sidebar').classList.contains('open'));
  click('.topbar [data-logout]');try{await until(()=>document.querySelector('.auth-form h1')?.textContent==='Welcome back.','logout')}catch(error){console.error({heading:document.querySelector('h1')?.textContent,toast:document.querySelector('#toast')?.textContent,errors,serverErrors});throw error}
  assert.deepEqual(errors,[]);assert.deepEqual(serverErrors,[]);
  console.log('DOM smoke: setup, 7 routes, empty states, filters, CSV, password change, persisted rules, activation, navigation and logout passed.');
 }finally{if(dom)dom.window.close();server.kill('SIGTERM');await new Promise(resolve=>server.once('exit',resolve));fs.rmSync(dir,{recursive:true,force:true})}
})().catch(error=>{console.error(error);process.exitCode=1});
