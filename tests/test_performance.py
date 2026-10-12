import tempfile
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from copier import store
from test_engine import add_account

class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=store.DB;store.DB=self.tmp.name+'/test.db';store.init();add_account('master','master',1)
        self.now=datetime(2026,10,12,14,tzinfo=ZoneInfo('Europe/Berlin')).timestamp()
        self.start=int((self.now-2*86400)*1000)
        store.execute('INSERT INTO equity_samples VALUES(?,?,?,?)',('master',self.start,10000,10000))
        store.execute('UPDATE accounts SET equity=11125,balance=11100,unrealized=25')
    def tearDown(self):store.DB=self.old;self.tmp.cleanup()
    def deal(self,stamp,net):
        store.execute('INSERT INTO deals(account_id,deal_id,timestamp,net,is_close) VALUES(?,?,?,?,1)',('master',stamp,stamp,net))
    def test_excludes_old_history_and_deposits_includes_open_change(self):
        self.deal(self.start-1000,999);self.deal(self.start+1000,100)
        result=store.performance(store.account('master'),self.now)['all']
        self.assertEqual(result['net'],125);self.assertEqual(result['capital'],10000)
    def test_today_uses_berlin_midnight_and_open_change(self):
        midnight=int(datetime(2026,10,12,tzinfo=ZoneInfo('Europe/Berlin')).timestamp()*1000)
        store.execute('INSERT INTO equity_samples VALUES(?,?,?,?)',('master',midnight-60000,10090,10050))
        self.deal(midnight-1,200);self.deal(midnight+1000,-20)
        result=store.performance(store.account('master'),self.now)['today']
        self.assertEqual(result['since'],midnight);self.assertEqual(result['net'],-35)
    def test_aggregates_beyond_browser_deal_limit(self):
        with store.db() as c:c.executemany('INSERT INTO deals(account_id,deal_id,timestamp,net,is_close) VALUES(?,?,?,?,1)',[('master',i,self.start+1000,1) for i in range(5100)])
        self.assertEqual(store.performance(store.account('master'),self.now)['all']['net'],5125)
