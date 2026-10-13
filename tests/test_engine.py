import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from copier import store
from copier.engine import Engine,lots_to_volume,remaining_volume
from copier.transport import M,BrokerError,UncertainExecution,to_dict
from copier.protocol import OpenApiModelMessages_pb2 as Model

SYMBOL={'symbolId':'10','lotSize':'10000','minVolume':'100','maxVolume':'1000000','stepVolume':'100','digits':2,'pipPosition':1}

def position(pid=101,volume=10000,sl=None,tp=None,label='',stamp=None,status=1,symbol=10):
    p=Model.ProtoOAPosition(positionId=pid,tradeData=Model.ProtoOATradeData(symbolId=symbol,volume=volume,tradeSide=1,openTimestamp=int(time.time()*1000)-60000,label=label),positionStatus=status,swap=0,price=3000,utcLastUpdateTimestamp=stamp or int(time.time()*1000))
    if sl is not None:p.stopLoss=sl
    if tp is not None:p.takeProfit=tp
    return p

def add_account(aid,role,broker_id,env='live'):
    store.execute('''INSERT INTO accounts(id,broker_id,environment,token_id,name,provider,kind,role,lots,enabled,symbol_name,symbol_id,symbol_json,currency,status)
        VALUES(?,?,?,'grant',?,'Pepperstone','real',?,.2,1,'XAUUSD',10,?,'USD','connected')''',(aid,broker_id,env,aid,role,json.dumps(SYMBOL)))

class FakeConnection:
    alive=True
    def __init__(self):self.requests=[];self.positions={};self.open_hook=None;self.failure=None;self.clock=int(time.time()*1000)+100
    async def request(self,request,**kwargs):
        self.requests.append(request)
        if self.failure:raise self.failure
        self.clock+=100
        if isinstance(request,M.ProtoOANewOrderReq):
            p=position(201,request.volume,label=request.label,stamp=self.clock)
            self.positions[p.positionId]=p
            if self.open_hook:await self.open_hook()
        elif isinstance(request,M.ProtoOAAmendPositionSLTPReq):
            p=self.positions[request.positionId];p.utcLastUpdateTimestamp=self.clock
            p.ClearField('stopLoss');p.ClearField('takeProfit')
            if request.HasField('stopLoss'):p.stopLoss=request.stopLoss
            if request.HasField('takeProfit'):p.takeProfit=request.takeProfit
        elif isinstance(request,M.ProtoOAClosePositionReq):
            p=self.positions[request.positionId];p.utcLastUpdateTimestamp=self.clock
            p.tradeData.volume-=request.volume
            if not p.tradeData.volume:p.positionStatus=2
        elif isinstance(request,M.ProtoOAReconcileReq):
            return M.ProtoOAReconcileRes(ctidTraderAccountId=request.ctidTraderAccountId,position=[p for p in self.positions.values() if p.positionStatus==1])
        elif isinstance(request,M.ProtoOATraderReq):
            return M.ProtoOATraderRes(ctidTraderAccountId=request.ctidTraderAccountId,trader=Model.ProtoOATrader(ctidTraderAccountId=request.ctidTraderAccountId,balance=100000,depositAssetId=1,accountType=1))
        elif isinstance(request,M.ProtoOAAccountAuthReq):return M.ProtoOAAccountAuthRes(ctidTraderAccountId=request.ctidTraderAccountId)
        else:raise AssertionError(type(request))
        response=M.ProtoOAExecutionEvent(ctidTraderAccountId=request.ctidTraderAccountId,executionType=4 if isinstance(request,M.ProtoOAAmendPositionSLTPReq) else 3)
        response.position.CopyFrom(p)
        return response

class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.previous=store.DB;store.DB=self.temp.name+'/copier.db';store.init()
        self.env=patch.dict(os.environ,{'ENGINE_TRADING_ENABLED':'true'});self.env.start()
        add_account('master','master',1);add_account('slave','slave',2)
        store.execute("UPDATE settings SET value='false' WHERE key='paused'")
        self.engine=Engine();self.conn=FakeConnection();self.engine.connections={'live':self.conn};self.engine.ready={'master','slave'};self.engine.started={'master':int(time.time()*1000)-1000}
    async def test_initialization_retry_reuses_authenticated_session(self):
        self.engine.grants={'grant':'access'}
        self.engine.authenticated={'master':'access'}
        self.engine.ready.discard('master')
        with self.assertRaisesRegex(ValueError,'hedging'):
            await self.engine.initialize_account(store.account('master'))
        self.assertFalse(any(isinstance(r,M.ProtoOAAccountAuthReq) for r in self.conn.requests))
        self.assertNotIn('master',self.engine.ready)

    async def test_already_logged_in_still_validates_account(self):
        self.engine.grants={'grant':'access'}
        original=self.conn.request
        async def request(message,**kwargs):
            if isinstance(message,M.ProtoOAAccountAuthReq):raise BrokerError('ALREADY_LOGGED_IN')
            return await original(message,**kwargs)
        self.conn.request=request
        with self.assertRaisesRegex(ValueError,'hedging'):
            await self.engine.initialize_account(store.account('master'))
        self.assertEqual(self.engine.authenticated['master'],'access')
        self.assertNotIn('master',self.engine.ready)

    async def asyncTearDown(self):
        tasks=list(self.engine.sync_tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.env.stop();store.DB=self.previous;self.temp.cleanup()
    async def settle(self):
        for _ in range(10):
            tasks=[t for t in self.engine.sync_tasks.values() if not t.done()]
            if not tasks:return
            await asyncio.gather(*tasks)
        self.fail('Engine did not settle')
    def source(self,p,allow=True):self.engine.update_master(store.account('master'),p,allow_new=allow)
    def mapping(self):return store.one('SELECT * FROM mappings')
    async def test_open_is_deduplicated_and_uses_symbol_lot_size(self):
        p=position();self.source(p);self.source(p);await self.settle()
        self.assertEqual(sum(isinstance(r,M.ProtoOANewOrderReq) for r in self.conn.requests),1)
        self.assertEqual(self.conn.requests[0].volume,2000);self.assertEqual(self.mapping()['status'],'active')
    async def test_sl_tp_change_and_removal(self):
        self.source(position());await self.settle()
        self.source(position(sl=2990,tp=3040,stamp=int(time.time()*1000)+1000));await self.settle()
        p=store.one('SELECT * FROM positions');self.assertEqual((p['sl'],p['tp']),(2990,3040))
        self.source(position(stamp=int(time.time()*1000)+2000));await self.settle()
        amend=self.conn.requests[-1];self.assertIsInstance(amend,M.ProtoOAAmendPositionSLTPReq)
        self.assertFalse(amend.HasField('stopLoss'));self.assertFalse(amend.HasField('takeProfit'))
    async def test_stop_moved_while_slave_open_in_flight(self):
        async def hook():self.source(position(sl=2993,tp=3050,stamp=int(time.time()*1000)+5000))
        self.conn.open_hook=hook;self.source(position());await self.settle()
        p=store.one('SELECT * FROM positions');self.assertEqual((p['sl'],p['tp']),(2993,3050))
    async def test_partial_and_full_close_continue_while_paused(self):
        self.source(position());await self.settle();store.execute("UPDATE settings SET value='true' WHERE key='paused'")
        self.source(position(volume=5000,stamp=int(time.time()*1000)+1000));await self.settle()
        self.assertEqual(self.conn.positions[201].tradeData.volume,1000)
        self.source(position(volume=0,status=2,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual(self.mapping()['status'],'closed');self.assertEqual(self.conn.positions[201].tradeData.volume,0)
    async def test_master_closes_while_slave_opens(self):
        async def hook():self.source(position(volume=0,status=2,stamp=int(time.time()*1000)+5000))
        self.conn.open_hook=hook;self.source(position());await self.settle()
        self.assertEqual(self.mapping()['status'],'closed')
        self.assertEqual([type(r) for r in self.conn.requests],[M.ProtoOANewOrderReq,M.ProtoOAClosePositionReq])
    async def test_independent_slave_close_is_not_reopened(self):
        self.source(position());await self.settle()
        self.engine.save_position(store.account('slave'),position(201,0,status=2,stamp=int(time.time()*1000)+10000))
        self.source(position(sl=2990,stamp=int(time.time()*1000)+11000));await self.settle()
        self.assertEqual(len(self.conn.requests),1);self.assertEqual(self.mapping()['status'],'closed')
    async def test_paused_signal_is_not_copied_on_resume(self):
        store.execute("UPDATE settings SET value='true' WHERE key='paused'");self.source(position())
        store.execute("UPDATE settings SET value='false' WHERE key='paused'");self.source(position(sl=2990));await self.settle()
        self.assertFalse(self.conn.requests)
    async def test_reconnect_baseline_does_not_reopen_old_master(self):
        self.source(position(),allow=False);self.source(position(sl=2990));await self.settle()
        self.assertFalse(self.conn.requests)
    async def test_tiny_partial_close_keeps_minimum_until_master_closed(self):
        self.source(position());await self.settle()
        self.source(position(volume=1,stamp=int(time.time()*1000)+1000));await self.settle()
        self.assertEqual(self.conn.positions[201].tradeData.volume,100)
    async def test_uncertain_open_never_blindly_retried(self):
        self.conn.failure=UncertainExecution('timeout');self.source(position());await self.settle()
        self.assertEqual(self.mapping()['status'],'uncertain')
        self.conn.failure=None;self.source(position(sl=2990));await self.settle();self.assertEqual(len(self.conn.requests),1)
        self.conn.positions[201]=position(201,2000,label=self.mapping()['label'])
        await self.engine.reconcile(store.account('slave'));await self.settle()
        self.assertEqual(self.mapping()['slave_position'],201)
        self.assertEqual(sum(isinstance(r,M.ProtoOANewOrderReq) for r in self.conn.requests),1)
        self.assertEqual(self.mapping()['status'],'active')
    async def test_recovery_does_not_rescale_an_already_reduced_slave(self):
        self.conn.failure=UncertainExecution('timeout');self.source(position());await self.settle()
        self.conn.failure=None
        self.conn.positions[201]=position(201,1000,label=self.mapping()['label'])
        self.source(position(volume=5000,stamp=int(time.time()*1000)+1000));await self.settle()
        await self.engine.reconcile(store.account('slave'));await self.settle()
        self.assertEqual(self.mapping()['initial_slave'],2000)
        self.assertEqual(self.conn.positions[201].tradeData.volume,1000)
        self.assertFalse(any(isinstance(r,M.ProtoOAClosePositionReq) for r in self.conn.requests))
    async def test_broker_rejection_is_recorded_and_not_retried(self):
        self.conn.failure=BrokerError('NO_MONEY');self.source(position());await self.settle()
        self.assertEqual(self.mapping()['status'],'failed');self.assertEqual(store.one('SELECT * FROM commands')['status'],'rejected')
        self.source(position(sl=2990));await self.settle();self.assertEqual(len(self.conn.requests),1)
    async def test_pending_position_is_not_a_trade(self):
        self.source(position(status=3));await self.settle()
        self.assertFalse(store.rows('SELECT * FROM master_positions'))
        self.source(position());await self.settle();self.assertEqual(len(self.conn.requests),1)
    async def test_pending_fill_with_old_creation_time_is_copied(self):
        consumer=asyncio.create_task(self.engine.consume())
        p=position();p.tradeData.openTimestamp=1
        event=M.ProtoOAExecutionEvent(ctidTraderAccountId=1,executionType=3,position=p)
        self.engine.event('live',event);await self.engine.events.join();await self.settle()
        consumer.cancel();await asyncio.gather(consumer,return_exceptions=True)
        self.assertEqual(len(self.conn.requests),1)
    async def test_netting_account_is_rejected_before_trading(self):
        self.engine.ready.clear();self.engine.grants={'grant':'access'}
        with self.assertRaisesRegex(ValueError,'hedging'):await self.engine.initialize_account(store.account('master'))
        self.assertFalse(any(isinstance(r,M.ProtoOANewOrderReq) for r in self.conn.requests))
    async def test_other_symbols_are_ignored(self):
        self.source(position(symbol=11));await self.settle();self.assertFalse(self.conn.requests)
    async def test_order_contains_existing_master_protection_distances(self):
        self.source(position(sl=2990,tp=3020));await self.settle()
        req=self.conn.requests[0];self.assertEqual(req.relativeStopLoss,1000000);self.assertEqual(req.relativeTakeProfit,2000000)
    async def test_stale_master_events_cannot_move_stop_back(self):
        stamp=int(time.time()*1000)
        self.source(position(sl=2995,stamp=stamp+1000));await self.settle()
        self.source(position(sl=2990,stamp=stamp));await self.settle()
        self.assertEqual(store.one('SELECT * FROM master_positions')['sl'],2995)
    def test_volume_rounding_and_bounds(self):
        self.assertEqual(lots_to_volume(.239,SYMBOL),2300)
        with self.assertRaises(ValueError):lots_to_volume(.001,SYMBOL)
        self.assertEqual(remaining_volume(2000,5000,10000,100),1000)
        self.assertEqual(remaining_volume(2000,0,10000,100),0)
