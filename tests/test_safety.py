import asyncio
import time
from unittest.mock import patch
import test_engine as fixtures
position=fixtures.position
from copier import store,safety
from copier.transport import M,UncertainExecution

class SafetyTests(fixtures.EngineTests):
    # Reuse isolated broker fixtures, not the parent test cases.
    async def open_trade(self):self.source(position(sl=2990));await self.settle()
    async def watchdog_tick(self,w):
        await w.tick()
        await asyncio.gather(*w.tasks.values())
    async def test_panic_closes_slave_while_master_unavailable(self):
        await self.open_trade();self.engine.ready.discard('master')
        safety.trip('manual');w=safety.Watchdog(self.engine)
        await self.watchdog_tick(w);await self.watchdog_tick(w)
        self.assertEqual(self.conn.positions[201].positionStatus,2)
        self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'flat')
        self.assertTrue(all(r.ctidTraderAccountId==2 for r in self.conn.requests if isinstance(r,M.ProtoOAClosePositionReq)))
        self.assertEqual(store.setting('paused'),'true')
    async def test_disconnected_panic_is_durable_and_reconnect_executes(self):
        await self.open_trade();safety.trip('manual');self.conn.alive=False
        w=safety.Watchdog(self.engine);await self.watchdog_tick(w)
        self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'waiting')
        self.conn.alive=True;await self.watchdog_tick(w);await self.watchdog_tick(w)
        self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'flat')
    async def test_panic_catches_late_fill_after_flat(self):
        safety.trip('manual');w=safety.Watchdog(self.engine);await self.watchdog_tick(w)
        self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'flat')
        self.conn.positions[999]=position(999,1000,symbol=99)
        await self.watchdog_tick(w);self.assertEqual(self.conn.positions[999].positionStatus,2)
    async def test_missing_sl_exceeds_grace_then_closes(self):
        self.source(position());await self.settle();w=safety.Watchdog(self.engine)
        await self.watchdog_tick(w)
        w.since[(self.mapping()['id'],'unprotected')]=time.monotonic()-31
        await self.watchdog_tick(w)
        self.assertEqual(self.conn.positions[201].positionStatus,2)
        self.assertEqual(store.setting('safety_latched'),'true')
    async def test_rejected_amend_trips_and_closes(self):
        await self.open_trade();original=self.conn.request
        from copier.transport import BrokerError
        async def request(r,**kwargs):
            if isinstance(r,M.ProtoOAAmendPositionSLTPReq):raise BrokerError('BAD_STOPS')
            return await original(r,**kwargs)
        self.conn.request=request;self.source(position(sl=2995,stamp=int(time.time()*1000)+1000));await self.settle()
        self.assertEqual(store.setting('safety_latched'),'true')
        await self.watchdog_tick(safety.Watchdog(self.engine));self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_unconfirmed_close_is_not_blindly_retried(self):
        await self.open_trade();safety.trip('manual');original=self.conn.request;count=[]
        async def request(r,**kwargs):
            if isinstance(r,M.ProtoOAClosePositionReq):count.append(r);raise UncertainExecution('timeout')
            return await original(r,**kwargs)
        self.conn.request=request;w=safety.Watchdog(self.engine)
        await self.watchdog_tick(w);await self.watchdog_tick(w);await self.watchdog_tick(w)
        self.assertEqual(len(count),1)
        self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'uncertain')
        self.conn.positions[201].positionStatus=2
        await self.watchdog_tick(w);self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'flat')
    async def test_storm_blocks_entries_and_latches(self):
        with patch.dict(safety.DEFAULTS,{'max_entries':1}):
            await self.open_trade();self.source(position(pid=102));await self.settle()
        self.assertEqual(sum(isinstance(r,M.ProtoOANewOrderReq) for r in self.conn.requests),1)
        self.assertEqual(store.setting('safety_latched'),'true')
    async def test_expired_signal_never_sent(self):
        self.source(position());m=self.mapping()
        store.execute('UPDATE mappings SET created=?',(time.time()-10,))
        await self.settle();self.assertFalse(self.conn.requests)
    async def test_loss_of_master_triggers_close(self):
        await self.open_trade();self.engine.ready.discard('master');w=safety.Watchdog(self.engine)
        await self.watchdog_tick(w);w.since[(self.mapping()['id'],'connection')]=time.monotonic()-11
        await self.watchdog_tick(w);self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_missing_protection_confirmation_triggers_close(self):
        await self.open_trade();store.execute('UPDATE master_positions SET sl=2995')
        w=safety.Watchdog(self.engine);await self.watchdog_tick(w)
        w.since[(self.mapping()['id'],'protection')]=time.monotonic()-6
        await self.watchdog_tick(w);self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_repeated_panic_is_idempotent(self):
        safety.trip('a');safety.trip('b');self.assertEqual(len(store.rows('SELECT * FROM liquidations')),1)
    async def test_restart_pending_safety_action_is_not_repeated(self):
        await self.open_trade();safety.trip('manual')
        store.execute("INSERT INTO safety_actions VALUES('crashed','slave','close',201,'sending',NULL,?,NULL)",(time.time(),))
        before=len(self.conn.requests);await self.watchdog_tick(safety.Watchdog(self.engine))
        self.assertFalse(any(isinstance(r,M.ProtoOAClosePositionReq) for r in self.conn.requests[before:]))

    async def test_persisted_grace_survives_watchdog_restart(self):
        self.source(position());await self.settle()
        w=safety.Watchdog(self.engine);await self.watchdog_tick(w)
        store.execute('UPDATE safety_timers SET started=?',(time.time()-40,))
        await self.watchdog_tick(safety.Watchdog(self.engine))
        self.assertEqual(self.conn.positions[201].positionStatus,2)
    async def test_disabled_execution_never_sends_emergency_order(self):
        await self.open_trade();safety.trip('manual')
        with patch.dict('os.environ',{'ENGINE_TRADING_ENABLED':'false'}):
            await self.watchdog_tick(safety.Watchdog(self.engine))
        self.assertEqual(self.conn.positions[201].positionStatus,1)
        self.assertIn('disabled',store.one('SELECT * FROM liquidations')['error'])
    async def test_pending_orders_cancel_before_position_close(self):
        await self.open_trade();original=self.conn.request;pending=[777];actions=[]
        from copier.protocol import OpenApiModelMessages_pb2 as model
        async def request(r,**kwargs):
            if isinstance(r,M.ProtoOAReconcileReq):
                result=await original(r,**kwargs)
                if pending:result.order.add(orderId=777,tradeData=model.ProtoOATradeData(symbolId=10,volume=100,tradeSide=1),orderType=2,orderStatus=1)
                return result
            if isinstance(r,M.ProtoOACancelOrderReq):
                pending.clear();actions.append('cancel')
                return M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=5)
            if isinstance(r,M.ProtoOAClosePositionReq):actions.append('close')
            return await original(r,**kwargs)
        self.conn.request=request;safety.trip('manual');w=safety.Watchdog(self.engine)
        await self.watchdog_tick(w);await self.watchdog_tick(w)
        self.assertEqual(actions,['cancel','close'])
        self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'flat')
    async def test_repeated_broker_rejections_stop_retrying(self):
        await self.open_trade();original=self.conn.request;attempts=[]
        from copier.transport import BrokerError
        async def request(r,**kwargs):
            if isinstance(r,M.ProtoOAClosePositionReq):attempts.append(r);raise BrokerError('MARKET_CLOSED')
            return await original(r,**kwargs)
        self.conn.request=request;safety.trip('manual');w=safety.Watchdog(self.engine)
        for _ in range(5):await self.watchdog_tick(w)
        self.assertEqual(len(attempts),3)
        self.assertEqual(store.one('SELECT * FROM liquidations')['status'],'blocked')

    async def test_emergency_closes_every_slave_including_disabled_and_manual_other_symbol(self):
        await self.open_trade()
        fixtures.add_account('disabled','slave',3)
        store.execute("UPDATE accounts SET enabled=0 WHERE id='disabled'")
        self.engine.ready.add('disabled')
        book={1:[position(900,10000)],2:[self.conn.positions[201],position(902,500,symbol=99)],3:[position(903,1500)]}
        calls=[]
        async def request(req,**kwargs):
            aid=req.ctidTraderAccountId
            if isinstance(req,M.ProtoOAReconcileReq):return M.ProtoOAReconcileRes(ctidTraderAccountId=aid,position=[p for p in book[aid] if p.positionStatus==1])
            if isinstance(req,M.ProtoOAClosePositionReq):
                calls.append(aid);p=next(p for p in book[aid] if p.positionId==req.positionId)
                self.assertEqual(req.volume,p.tradeData.volume)
                p.tradeData.volume=0;p.positionStatus=2;p.utcLastUpdateTimestamp+=100
                return M.ProtoOAExecutionEvent(ctidTraderAccountId=aid,executionType=3,position=p)
            raise AssertionError(type(req))
        self.conn.request=request;safety.trip('manual');w=safety.Watchdog(self.engine)
        await self.watchdog_tick(w);await self.watchdog_tick(w)
        self.assertEqual(sorted(calls),[2,2,3]);self.assertEqual(book[1][0].positionStatus,1)
        self.assertTrue(all(x['status']=='flat' for x in store.rows('SELECT * FROM liquidations')))

# Import fixtures without running inherited tests a second time.
for name in list(fixtures.EngineTests.__dict__):
    if name.startswith('test_') and name not in SafetyTests.__dict__:setattr(SafetyTests,name,None)
