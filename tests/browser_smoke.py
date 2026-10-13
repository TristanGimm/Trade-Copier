"""Real browser smoke test; isolated database, never connects to cTrader."""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
import server

with tempfile.TemporaryDirectory() as directory:
    server.DB=directory+'/browser.db';os.environ['SETUP_TOKEN']='browser-test-setup-secret';server.init()
    http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
    server.ORIGIN='http://localhost:'+str(http.server_address[1])
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    artifacts=Path('artifacts');artifacts.mkdir(exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser=getattr(playwright,os.environ.get('TEST_BROWSER','chromium')).launch(headless=os.environ.get('FIREFOX_HEADLESS','true')!='false',timeout=30000)
            context=browser.new_context(viewport={'width':1512,'height':1100})
            page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(server.ORIGIN);page.get_by_label('Email address').fill('firefox@example.com')
            page.get_by_label('Password',exact=True).fill('browser-test-password')
            page.get_by_label('Setup-Token').fill('browser-test-setup-secret');page.get_by_role('button',name='Create workspace').click()
            page.get_by_role('heading',name='Your trading. Connected.').wait_for()
            page.get_by_role('button',name='Emergency stop',exact=True).click()
            page.get_by_role('dialog').get_by_role('button',name='Close all slaves now').click()
            page.get_by_text('Safety stop active · new copies blocked',exact=True).wait_for()
            assert server.store.setting('safety_latched')=='true'
            page.locator('#connection-status').click();page.get_by_role('heading',name='Connection status',exact=True).wait_for()
            page.get_by_role('dialog').get_by_role('button',name='Close',exact=True).last.click()
            # Test-only account; no cTrader connection, no seeded production data.
            sys.path.insert(0,str(Path(__file__).parent))
            from test_engine import add_account
            add_account('test-slave','slave',999)
            server.store.execute("UPDATE accounts SET balance=10000,equity=9560,currency='EUR',updated=? WHERE id='test-slave'",(__import__('time').time(),))
            page.get_by_role('button',name='Refresh',exact=True).click()
            page.get_by_role('button',name='Loss limits',exact=True).first.click()
            dialog=page.get_by_role('dialog')
            dialog.locator('[name=initial_balance]').fill('10000')
            dialog.locator('[name=max_loss]').fill('1000')
            dialog.locator('[name=daily_loss]').fill('500')
            dialog.locator('[name=day_balance]').fill('10000')
            dialog.get_by_role('button',name='Save',exact=True).click()
            dialog.wait_for(state='detached')
            page.get_by_text('WARNING! Low headroom',exact=True).wait_for()
            assert server.store.one('SELECT * FROM risk_limits')['daily_loss']==500
            page.screenshot(path=str(artifacts/'chromium-overview.png'),full_page=True)
            for label,title in [('Trading accounts','Every account. One workspace.'),('Copy Engine','Your strategy. In sync.'),('Trade journal','Every execution tells a story.'),('Analytics','Understand your edge.'),('Activity log','Nothing gets lost.'),('Settings','Make it yours.')]:
                page.get_by_role('navigation').get_by_role('button',name=label,exact=True).click();page.get_by_role('heading',name=title).wait_for()
                if label=='Analytics':
                    page.get_by_role('button',name='30D',exact=True).click();page.get_by_role('button',name='All time',exact=True).click()
                    page.screenshot(path=str(artifacts/'chromium-analytics.png'),full_page=True)
            page.get_by_role('button',name='Change password',exact=True).click();page.get_by_label('Current password').fill('browser-test-password');page.get_by_label('New password').fill('browser-test-password-new');page.get_by_role('dialog').get_by_role('button',name='Save',exact=True).click();page.get_by_role('dialog').wait_for(state='detached')
            page.set_viewport_size({'width':390,'height':844});page.get_by_role('button',name='Open navigation').click();page.get_by_role('navigation').get_by_role('button',name='Analytics',exact=True).click();page.get_by_role('heading',name='Understand your edge.').wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),'Mobile layout overflows'
            page.screenshot(path=str(artifacts/'chromium-mobile.png'),full_page=True)
            page.get_by_role('button',name='Open navigation').click()
            page.get_by_role('navigation').get_by_role('button',name='Copy Engine',exact=True).click()
            page.get_by_role('heading',name='Your strategy. In sync.').wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'),'Mobile rules layout overflows'
            page.screenshot(path=str(artifacts/'chromium-rules-mobile.png'),full_page=True)
            page.get_by_role('button',name='Open navigation').click()
            page.get_by_role('navigation').get_by_role('button',name='Trading accounts',exact=True).click()
            page.get_by_role('heading',name='Every account. One workspace.').wait_for()
            assert page.get_by_text('SLAVE',exact=True).count()==1
            server.store.set_status('heartbeat',__import__('time').time())
            server.store.execute("INSERT INTO discovered VALUES('test-slave',999,'live','grant','999','Pepperstone')")
            page.get_by_role('button',name='Remove',exact=True).click()
            page.get_by_role('dialog').get_by_role('button',name='Remove account',exact=True).click()
            page.get_by_role('dialog').wait_for(state='detached')
            from copier.engine import Engine
            from test_engine import FakeConnection
            engine=Engine();engine.connections={'live':FakeConnection()};engine.ready={'test-slave'}
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(lambda:__import__('asyncio').run(engine.remove_accounts())).result()
            assert server.store.account('test-slave') is None
            page.get_by_role('button',name='Refresh',exact=True).click()
            page.get_by_text('Your accounts start here.',exact=True).wait_for()
            page.get_by_role('button',name='Add account',exact=True).click()
            dialog=page.get_by_role('dialog')
            dialog.get_by_label('Account name',exact=True).fill('Re-added slave')
            dialog.get_by_label('Role',exact=True).select_option(label='Slave')
            dialog.get_by_label('Capital type',exact=True).select_option('simulated')
            dialog.get_by_role('button',name='Connect account',exact=True).click()
            dialog.wait_for(state='detached')
            page.get_by_text('Re-added slave',exact=True).first.wait_for()
            assert server.store.account('test-slave')['role']=='slave'
            assert not __import__('re').search(r'Slave|Schutzstopp|Verlustgrenzen|Schließen|Konto zuordnen',page.locator('body').inner_text())
            page.get_by_role('button',name='Sign out',exact=True).last.click();page.get_by_role('heading',name='Welcome back.').wait_for()
            assert not errors,errors
            print(json.dumps({'browser':os.environ.get('TEST_BROWSER','chromium'),'routes':7,'mobileWidth':390,'javascriptErrors':errors,'screenshots':str(artifacts)}))
            browser.close()
    finally:
        http.shutdown();http.server_close()
