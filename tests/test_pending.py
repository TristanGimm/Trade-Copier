import time
import test_engine as fixtures
from copier import store,pending
from copier.transport import M,BrokerError
from copier.protocol import OpenApiModelMessages_pb2 as Model


def order(oid=501,kind=2,status=1,sl=2990,tp=3040,stamp=None):
    o=Model.ProtoOAOrder(orderId=oid,tradeData=Model.ProtoOATradeData(symbolId=10,volume=10000,tradeSide=1,openTimestamp=int(time.time()*1000)),orderType=kind,orderStatus=status,utcLastUpdateTimestamp=stamp or int(time.time()*1000),timeInForce=2)
    if kind==2:o.limitPrice=3000
    else:o.stopPrice=3000
    if kind==6:o.slippageInPoints=10
    if sl is not None:o.stopLoss=sl
    if tp is not None:o.takeProfit=tp
    return o

class PendingTests(fixtures.EngineTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.orders={};self.original=self.conn.request
        async def request(r,**kwargs):
            if isinstance(r,M.ProtoOANewOrderReq) and r.orderType in (2,3,6):
                self.conn.requests.append(r)
                o=order(601,r.orderType);o.tradeData.volume=r.volume;o.tradeData.label=r.label;o.clientOrderId=r.clientOrderId
                o.ClearField('stopLoss');o.ClearField('takeProfit')
                for field in pending.FIELDS:
                    if r.HasField(field):setattr(o,field,getattr(r,field))
                self.orders[601]=o
                return M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=2,order=o)
            if isinstance(r,M.ProtoOAOrderDetailsReq):
                self.conn.requests.append(r)
                return M.ProtoOAOrderDetailsRes(ctidTraderAccountId=r.ctidTraderAccountId,order=self.orders[r.orderId])
            if isinstance(r,M.ProtoOAAmendOrderReq):
                self.conn.requests.append(r);o=self.orders[r.orderId]
                for field in pending.FIELDS:
                    o.ClearField(field)
                    if r.HasField(field):setattr(o,field,getattr(r,field))
                return M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=4,order=o)
            if isinstance(r,M.ProtoOACancelOrderReq):
                self.conn.requests.append(r);o=self.orders[r.orderId];o.orderStatus=5
                return M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=5,order=o)
            return await self.original(r,**kwargs)
        self.conn.request=request
    def master_order(self,o):pending.master_event(self.engine,store.account('master'),o,allow_new=True)
    async def test_pending_entry_protection_amend_cancel(self):
        self.master_order(order());await self.settle()
        self.assertEqual(self.mapping()['follower_order'],601)
        req=next(r for r in self.conn.requests if isinstance(r,M.ProtoOANewOrderReq))
        self.assertEqual((req.orderType,req.limitPrice,req.stopLoss,req.takeProfit,req.volume),(2,3000,2990,3040,2000))
        self.master_order(order(sl=2995,tp=3050,stamp=int(time.time()*1000)+1000));await self.settle()
        self.assertEqual((self.orders[601].stopLoss,self.orders[601].takeProfit),(2995,3050))
        self.master_order(order(status=5,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual(self.mapping()['status'],'closed');self.assertEqual(self.orders[601].orderStatus,5)
        self.assertNotEqual(store.setting('safety_latched'),'true')
    async def test_master_fill_adopts_mapping_without_market_duplicate(self):
        self.master_order(order());await self.settle()
        o=order(status=2,stamp=int(time.time()*1000)+1000);o.positionId=101;o.executedVolume=10000
        p=fixtures.position(sl=2990,tp=3040,stamp=o.utcLastUpdateTimestamp)
        pending.master_event(self.engine,store.account('master'),o,p)
        self.engine.update_master(store.account('master'),p,allow_new=True)
        f=fixtures.position(201,2000,sl=2990,tp=3040,label=self.mapping()['label'],stamp=o.utcLastUpdateTimestamp)
        self.orders[601].orderStatus=2;self.orders[601].positionId=201;self.orders[601].executedVolume=2000
        self.conn.positions[201]=f
        pending.follower_event(self.engine,store.account('follower'),self.orders[601],f)
        await self.settle()
        self.assertEqual(len(store.rows('SELECT * FROM mappings')),1)
        self.assertEqual((self.mapping()['master_position'],self.mapping()['follower_position']),(101,201))
        self.assertEqual(len([r for r in self.conn.requests if isinstance(r,M.ProtoOANewOrderReq)]),1)
        self.source(fixtures.position(volume=0,status=2,stamp=o.utcLastUpdateTimestamp+1000));await self.settle()
        self.assertEqual(self.mapping()['status'],'closed')
    async def test_follower_fills_first_then_master_cancels(self):
        self.master_order(order());await self.settle()
        f=fixtures.position(201,2000,sl=2990,tp=3040,label=self.mapping()['label'])
        self.conn.positions[201]=f;self.orders[601].orderStatus=2;self.orders[601].executedVolume=2000
        pending.follower_event(self.engine,store.account('follower'),self.orders[601],f);await self.settle()
        self.master_order(order(status=5,stamp=int(time.time()*1000)+2000));await self.settle()
        self.assertEqual(self.conn.positions[201].positionStatus,2)
        self.assertNotEqual(store.setting('safety_latched'),'true')
    async def test_old_pending_order_not_copied_on_startup(self):
        pending.master_event(self.engine,store.account('master'),order())
        self.master_order(order(stamp=int(time.time()*1000)+1000));await self.settle()
        self.assertIsNone(self.mapping())
    async def test_stop_and_stop_limit_types(self):
        for kind in (3,6):
            self.master_order(order(oid=500+kind,kind=kind));await self.settle()
        self.assertEqual([r.orderType for r in self.conn.requests if isinstance(r,M.ProtoOANewOrderReq)],[3,6])
    async def test_reconciliation_preserves_unfilled_master_order(self):
        self.master_order(order());await self.settle()
        original=self.conn.request
        async def request(r,**kwargs):
            if isinstance(r,M.ProtoOAReconcileReq) and r.ctidTraderAccountId==1:
                return M.ProtoOAReconcileRes(ctidTraderAccountId=1,order=[order()])
            return await original(r,**kwargs)
        self.conn.request=request
        await self.engine.reconcile(store.account('master'));await self.settle()
        self.assertEqual(self.mapping()['status'],'active')
        self.assertEqual(self.orders[601].orderStatus,1)
        self.assertEqual(store.one('SELECT closed FROM master_positions')['closed'],0)
    async def test_uncertain_cancel_is_not_resent(self):
        self.master_order(order());await self.settle()
        from copier.transport import UncertainExecution
        original=self.conn.request;sent=[]
        async def request(r,**kwargs):
            if isinstance(r,M.ProtoOACancelOrderReq):sent.append(r);raise UncertainExecution('timeout')
            return await original(r,**kwargs)
        self.conn.request=request
        self.master_order(order(status=5,stamp=int(time.time()*1000)+1000));await self.settle()
        self.engine.schedule(self.mapping()['id']);await self.settle()
        self.assertEqual(len(sent),1);self.assertEqual(self.mapping()['status'],'uncertain')
    async def test_follower_fill_found_after_restart_uses_existing_mapping(self):
        self.master_order(order());await self.settle()
        label=self.mapping()['label'];f=fixtures.position(201,2000,label=label,sl=2990,tp=3040)
        self.orders[601].orderStatus=2;self.orders[601].positionId=201;self.orders[601].executedVolume=2000
        self.conn.positions[201]=f
        await self.engine.reconcile(store.account('follower'));await self.settle()
        self.assertEqual(self.mapping()['follower_position'],201)
        self.assertEqual(len([r for r in self.conn.requests if isinstance(r,M.ProtoOANewOrderReq)]),1)
