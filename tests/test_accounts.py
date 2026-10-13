import time
import test_engine as fixtures
from copier import store,safety
from copier.transport import M,UncertainExecution
from copier.protocol import OpenApiModelMessages_pb2 as Model

class AccountTests(fixtures.EngineTests):
    def queue(self,aid):
        store.execute("INSERT OR REPLACE INTO account_removals VALUES(?,'queued',NULL,?)",(aid,time.time()))
        store.execute("UPDATE settings SET value='true' WHERE key='paused'")
        store.execute('UPDATE accounts SET enabled=0 WHERE id=?',(aid,))
    async def test_remove_slave_and_add_same_authorized_account_again(self):
        store.execute("INSERT INTO discovered VALUES('slave',2,'live','grant','2','Pepperstone')")
        self.queue('slave');await self.engine.remove_accounts()
        self.assertIsNone(store.account('slave'));self.assertNotIn('slave',self.engine.ready)
        self.assertIsNotNone(store.one("SELECT * FROM discovered WHERE id='slave'"))
        fixtures.add_account('slave','slave',2)
        self.assertEqual(store.account('slave')['role'],'slave')
    async def test_remove_master_allows_new_master(self):
        self.queue('master');await self.engine.remove_accounts()
        fixtures.add_account('new-master','master',99)
        self.assertIsNotNone(store.account('new-master'));self.assertIsNone(store.account('master'))
    async def test_open_broker_position_blocks_removal_then_retry_succeeds_when_flat(self):
        self.source(fixtures.position());await self.settle();self.queue('slave')
        await self.engine.remove_accounts()
        self.assertEqual(store.one('SELECT * FROM account_removals')['status'],'blocked')
        self.assertIsNotNone(store.account('slave'))
        self.source(fixtures.position(volume=0,status=2,stamp=int(time.time()*1000)+2000));await self.settle()
        self.queue('slave');await self.engine.remove_accounts()
        self.assertIsNone(store.account('slave'))
        self.assertIsNotNone(store.one('SELECT * FROM mappings'))
        self.assertIsNotNone(store.one('SELECT * FROM commands'))
    async def test_pending_order_blocks_removal(self):
        original=self.conn.request
        async def request(req,**kwargs):
            r=await original(req,**kwargs)
            if isinstance(req,M.ProtoOAReconcileReq):r.order.add(orderId=3,tradeData=Model.ProtoOATradeData(symbolId=10,volume=100,tradeSide=1),orderType=2,orderStatus=1)
            return r
        self.conn.request=request;self.queue('slave');await self.engine.remove_accounts()
        self.assertIsNotNone(store.account('slave'))
        self.assertIn('pending orders',store.one('SELECT * FROM account_removals')['error'])
    async def test_uncertain_open_blocks_removal_even_when_snapshot_is_empty(self):
        self.conn.failure=UncertainExecution('timeout');self.source(fixtures.position());await self.settle()
        self.conn.failure=None;self.queue('slave');await self.engine.remove_accounts()
        self.assertIsNotNone(store.account('slave'))
        self.assertIn('unconfirmed',store.one('SELECT * FROM account_removals')['error'])
    async def test_disconnected_account_cannot_be_silently_removed(self):
        self.conn.alive=False;self.queue('slave');await self.engine.remove_accounts()
        self.assertIsNotNone(store.account('slave'))
        self.assertEqual(store.one('SELECT * FROM account_removals')['status'],'blocked')
    async def test_queued_removal_prevents_new_copy(self):
        self.source(fixtures.position());self.queue('slave');await self.settle()
        self.assertFalse(any(isinstance(r,M.ProtoOANewOrderReq) for r in self.conn.requests))
    def test_legacy_activity_is_displayed_in_english(self):
        store.audit('cTrader verbunden: Old account')
        store.audit('Kopierregel gespeichert: Old account · aktiv')
        text=' '.join(r['event'] for r in store.public_state()['audit'])
        self.assertIn('cTrader connected:',text);self.assertIn('Copy rule saved:',text)
        self.assertNotIn('verbunden',text);self.assertNotIn('aktiv',text)

for name in fixtures.EngineTests.__dict__:
    if name.startswith('test_') and name not in AccountTests.__dict__:setattr(AccountTests,name,None)
