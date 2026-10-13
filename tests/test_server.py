import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
import server
from copier import store,oauth

class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();server.DB=cls.temp.name+'/test.db';server.init()
        cls.env=patch.dict(os.environ,{'SETUP_TOKEN':'integration-test-secret','TOKEN_ENCRYPTION_KEY':Fernet.generate_key().decode(),'CTRADER_CLIENT_ID':'test-client','CTRADER_CLIENT_SECRET':'test-secret'});cls.env.start()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler);cls.port=cls.http.server_address[1];server.ORIGIN='http://localhost:'+str(cls.port)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls):cls.http.shutdown();cls.http.server_close();cls.temp.cleanup();cls.env.stop()
    def request(self,path,body=None,cookie='',csrf='',origin=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.port)
        headers={'Cookie':cookie,'X-CSRF-Token':csrf,'Origin':origin or server.ORIGIN}
        if body is not None:headers['Content-Type']='application/json'
        connection.request('POST' if body is not None else 'GET',path,body=json.dumps(body) if body is not None else None,headers=headers)
        response=connection.getresponse();raw=response.read();result=(response.status,json.loads(raw),response.getheader('Set-Cookie'));connection.close();return result
    def test_full_private_workflow_without_demo_data(self):
        self.assertEqual(self.request('/api/state')[0],401)
        self.assertTrue(self.request('/api/session')[1]['setupRequired'])
        credentials={'email':'trader@example.com','password':'a-long-test-password','setupToken':'integration-test-secret'}
        self.assertEqual(self.request('/api/setup',{**credentials,'setupToken':'wrong'})[0],403)
        self.assertEqual(self.request('/api/setup',credentials,origin='https://evil.example')[0],403)
        status,data,cookie=self.request('/api/setup',credentials);self.assertEqual(status,200);self.assertIn('HttpOnly',cookie)
        cookie=cookie.split(';')[0];csrf=data['csrf']
        self.assertEqual(self.request('/api/setup',credentials)[0],409)
        self.assertEqual(self.request('/api/state',cookie=cookie)[1]['accounts'],[])
        self.assertEqual(self.request('/api/state',cookie=cookie)[1]['deals'],[])
        self.assertEqual(self.request('/api/rules',{'id':'slave','lots':.3,'enabled':True},cookie)[0],403)
        self.assertEqual(self.request('/api/pause',{'paused':False},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/accounts',{'name':'fake account'},cookie,csrf)[0],400)
        store.execute("INSERT INTO discovered VALUES('live:1',1,'live','grant','1001','Pepperstone')")
        store.execute("INSERT INTO discovered VALUES('live:2',2,'live','grant','1002','FTMO')")
        body={'id':'live:1','name':'Master','provider':'Pepperstone','kind':'real','role':'master','lots':1}
        self.assertEqual(self.request('/api/accounts',body,cookie,csrf)[0],200)
        slave={**body,'id':'live:2','name':'Challenge','provider':'FTMO','kind':'simulated','role':'slave','lots':.2}
        self.assertEqual(self.request('/api/accounts',{**slave,'kind':'real'},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/accounts',slave,cookie,csrf)[0],200)
        self.assertEqual(self.request('/api/rules',{'id':'live:2','lots':.3,'enabled':True},cookie,csrf)[0],400)
        store.execute("UPDATE accounts SET status='connected',updated=?",(time.time(),));store.set_status('safety_heartbeat',time.time());store.set_status('connection:live',True);store.set_status('reconciled:live:1',time.time());store.set_status('reconciled:live:2',time.time());store.set_status('heartbeat',time.time());store.set_status('trading_enabled',True)
        self.assertEqual(self.request('/api/rules',{'id':'live:2','lots':float('nan'),'enabled':True},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/rules',{'id':'live:2','lots':.3,'enabled':True},cookie,csrf)[0],200)
        self.assertEqual(self.request('/api/pause',{'paused':False},cookie,csrf)[0],200)
        state=self.request('/api/state',cookie=cookie)[1];self.assertFalse(state['paused']);self.assertTrue(all(a['equity'] is None for a in state['accounts']))
        self.assertTrue(state['brokerConnected'])
        self.assertEqual(self.request('/api/emergency',{},cookie,'wrong')[0],403)
        self.assertEqual(self.request('/api/emergency',{},cookie,csrf)[0],200)
        self.assertEqual(self.request('/api/emergency',{},cookie,csrf)[0],200)
        self.assertEqual(len(store.rows('SELECT * FROM liquidations')),1)
        self.assertFalse(self.request('/api/state',cookie=cookie)[1]['brokerConnected'])
        self.assertEqual(self.request('/api/pause',{'paused':False},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/safety/reset',{},cookie,csrf)[0],400)
        store.execute("UPDATE liquidations SET status='flat',verified=?",(time.time(),))
        self.assertEqual(self.request('/api/safety/reset',{},cookie,csrf)[0],200)
        self.assertEqual(store.setting('paused'),'true')
        self.assertEqual(self.request('/api/safety/policy',{'max_entries':0},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/safety/policy',{'max_entries':2},cookie,csrf)[0],200)
        loss={'id':'live:2','initial_balance':10000,'max_loss':1000,'daily_loss':500,'warning_pct':20,'day_balance':10000}
        self.assertEqual(self.request('/api/risk',loss,cookie,'bad')[0],403)
        self.assertEqual(self.request('/api/risk',{**loss,'id':'live:1'},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/risk',{**loss,'daily_loss':-1},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/risk',loss,cookie,csrf)[0],200)
        store.execute("UPDATE accounts SET equity=9560 WHERE id='live:2'")
        slave=next(a for a in self.request('/api/state',cookie=cookie)[1]['accounts'] if a['role']=='slave')
        self.assertEqual(slave['risk']['daily']['remaining'],60)
        self.assertTrue(slave['risk']['daily']['warning'])
        self.assertEqual(self.request('/api/mapping/resync',{'id':'missing'},cookie,csrf)[0],400)
        token_id=oauth.save_token({'accessToken':'sensitive-access','refreshToken':'sensitive-refresh','expiresIn':3600})
        stored=store.one('SELECT * FROM oauth_tokens WHERE id=?',(token_id,));self.assertNotIn('sensitive-access',stored['secret']);self.assertEqual(oauth.load_token(token_id)['accessToken'],'sensitive-access')
        public=json.dumps(self.request('/api/state',cookie=cookie)[1]);self.assertNotIn('sensitive-access',public);self.assertNotIn('test-secret',public)
        grant=self.request('/api/ctrader/connect',{},cookie,csrf)[1];self.assertIn('scope=trading',grant['url'])
        self.assertEqual(self.request('/api/ctrader/exchange',{'code':'code','state':'wrong'},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/account/remove',{'id':'live:2'},cookie,'wrong')[0],403)
        self.assertEqual(self.request('/api/account/remove',{'id':'missing'},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/account/remove',{'id':'live:2'},cookie,csrf)[0],200)
        self.assertEqual(store.setting('paused'),'true')
        self.assertEqual(store.account('live:2')['enabled'],0)
        self.assertEqual(store.one('SELECT status FROM account_removals')['status'],'queued')
        self.assertEqual(self.request('/api/rules',{'id':'live:2','lots':.3,'enabled':True},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/pause',{'paused':False},cookie,csrf)[0],400)
        self.assertEqual(self.request('/api/logout',{},cookie,csrf)[0],200)
        self.assertEqual(self.request('/api/state',cookie=cookie)[0],401)
        self.assertEqual(self.request('/api/login',{'email':credentials['email'],'password':'wrong'})[0],401)
        self.assertEqual(self.request('/api/login',credentials)[0],200)

if __name__=='__main__':unittest.main()
