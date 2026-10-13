import asyncio
import time
import unittest
import test_engine as fixtures
from copier import store,risk,safety
from copier.transport import M

class SlaveTests(fixtures.EngineTests):
    async def event(self,event):
        task=asyncio.create_task(self.engine.consume());self.engine.event('live',event)
        await self.engine.events.join();task.cancel();await asyncio.gather(task,return_exceptions=True)
        await self.settle()
    async def test_manual_slave_sl_stays_independent_and_tp_keeps_following(self):
        self.source(fixtures.position(sl=2990,tp=3040));await self.settle()
        p=self.conn.positions[201];p.stopLoss=2995;p.utcLastUpdateTimestamp+=100
        await self.event(M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=4,position=p))
        self.assertTrue(self.mapping()['sl_override']);self.assertFalse(self.mapping()['tp_override'])
        self.source(fixtures.position(sl=2980,tp=3050,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual((p.stopLoss,p.takeProfit),(2995,3050))
        self.assertEqual(store.one('SELECT sl FROM master_positions')['sl'],2980)
    async def test_manual_slave_edit_does_not_trip_protection_mismatch(self):
        self.source(fixtures.position(sl=2990,tp=3040));await self.settle()
        p=self.conn.positions[201];p.stopLoss=2995;p.utcLastUpdateTimestamp+=100
        await self.event(M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=2,position=p))
        w=safety.Watchdog(self.engine);w.since[(self.mapping()['id'],'protection')]=time.monotonic()-20
        await w.tick();await asyncio.gather(*w.tasks.values())
        self.assertNotEqual(store.setting('safety_latched'),'true')
    async def test_master_close_still_closes_manually_overridden_slave(self):
        self.source(fixtures.position(sl=2990));await self.settle()
        store.execute('UPDATE mappings SET sl_override=1,tp_override=1')
        self.source(fixtures.position(status=2,volume=0,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_missing_master_close_event_recovered_by_reconcile(self):
        self.source(fixtures.position());await self.settle()
        # Broker master snapshot is empty; slave snapshot still contains its trade.
        original=self.conn.request
        async def request(req,**kwargs):
            if isinstance(req,M.ProtoOAReconcileReq) and req.ctidTraderAccountId==1:return M.ProtoOAReconcileRes(ctidTraderAccountId=1)
            return await original(req,**kwargs)
        self.conn.request=request
        await self.engine.reconcile(store.account('master'));await self.settle()
        self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_master_closed_position_in_accepted_event_is_not_ignored(self):
        self.source(fixtures.position());await self.settle()
        await self.event(M.ProtoOAExecutionEvent(ctidTraderAccountId=1,executionType=2,position=fixtures.position(status=2,volume=0,stamp=int(time.time()*1000)+2000)))
        self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_partial_fill_of_full_close_closes_remaining_instead_of_marking_flat(self):
        self.source(fixtures.position());await self.settle();original=self.conn.request;once=[True]
        async def request(req,**kwargs):
            if isinstance(req,M.ProtoOAClosePositionReq) and once[0]:
                once[0]=False;req.volume//=2
            return await original(req,**kwargs)
        self.conn.request=request
        self.source(fixtures.position(status=2,volume=0,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual(self.conn.positions[201].positionStatus,2)
        self.assertEqual(sum(isinstance(r,M.ProtoOAClosePositionReq) for r in self.conn.requests),2)
    async def test_resync_restores_master_prices(self):
        self.source(fixtures.position(sl=2990,tp=3040));await self.settle()
        p=self.conn.positions[201];p.stopLoss=2995;p.utcLastUpdateTimestamp+=100
        await self.event(M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=4,position=p))
        store.execute('UPDATE mappings SET sl_override=0,tp_override=0,protection_initialized=0')
        self.engine.schedule(self.mapping()['id']);await self.settle()
        self.assertEqual(p.stopLoss,2990)
    def test_loss_budgets_only_warn_and_include_floating_losses(self):
        store.execute('INSERT INTO risk_limits VALUES(?,?,?,?,?)',('slave',10000,1000,500,20))
        risk.record_day('slave',10200)
        store.execute('UPDATE accounts SET equity=9760,updated=? WHERE id=?',(time.time(),'slave'))
        view=risk.view(store.account('slave'))
        self.assertEqual(view['total']['remaining'],760)
        self.assertEqual(view['daily']['remaining'],60)
        self.assertTrue(view['daily']['warning']);self.assertFalse(view['total']['warning'])
        self.assertEqual(store.setting('paused'),'false');self.assertFalse(store.rows('SELECT * FROM liquidations'))
    def test_daily_reference_is_stable_and_rolls_over_in_berlin(self):
        risk.record_day('slave',10000,0);risk.record_day('slave',9000,0)
        self.assertEqual(store.one('SELECT * FROM risk_days')['balance'],10000)
        risk.record_day('slave',11000,86400)
        self.assertEqual(len(store.rows('SELECT * FROM risk_days')),2)
    def test_loss_breach_does_not_pause_or_close(self):
        store.execute('INSERT INTO risk_limits VALUES(?,?,?,?,?)',('slave',10000,1000,500,20))
        risk.record_day('slave',10000);store.execute("UPDATE accounts SET equity=8900 WHERE id='slave'")
        view=risk.view(store.account('slave'))
        self.assertTrue(view['total']['breached']);self.assertEqual(view['total']['remaining'],-100)
        self.assertEqual(store.setting('paused'),'false');self.assertFalse(store.rows('SELECT * FROM liquidations'))

    async def test_confirmed_master_close_executes_even_if_master_disconnects(self):
        self.source(fixtures.position(sl=2990));await self.settle()
        self.engine.ready.discard('master')
        self.source(fixtures.position(status=2,volume=0,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_old_close_timestamp_is_not_ignored_or_resurrected(self):
        stamp=int(time.time()*1000)
        self.source(fixtures.position(sl=2990,stamp=stamp));await self.settle()
        self.source(fixtures.position(status=2,volume=0,stamp=stamp-1000));await self.settle()
        self.assertEqual(self.conn.positions[201].positionStatus,2)
        self.source(fixtures.position(stamp=stamp+1000));await self.settle()
        self.assertEqual(store.one('SELECT * FROM master_positions')['closed'],1)
    async def test_uncertain_amend_does_not_block_confirmed_master_close(self):
        self.source(fixtures.position(sl=2990));await self.settle()
        m=self.mapping();store.execute("UPDATE mappings SET status='uncertain' WHERE id=?",(m['id'],))
        store.execute("INSERT INTO commands VALUES('old-amend',?,'amend','{}','uncertain',NULL,?,NULL)",(m['id'],time.time()))
        self.source(fixtures.position(status=2,volume=0,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual(self.conn.positions[201].positionStatus,2)
        self.assertEqual(store.one("SELECT status FROM commands WHERE id='old-amend'")['status'],'reconciled')

for name in fixtures.EngineTests.__dict__:
    if name.startswith('test_') and name not in SlaveTests.__dict__:setattr(SlaveTests,name,None)
