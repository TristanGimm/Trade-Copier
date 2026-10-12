"""Async TLS/TCP transport for official cTrader Protobuf messages (port 5035)."""
import asyncio
import collections
import secrets
import ssl
import struct
import time
from google.protobuf.json_format import MessageToDict
from .protocol import OpenApiMessages_pb2 as M
from .protocol import OpenApiCommonMessages_pb2 as C

class BrokerError(Exception):
    pass

class UncertainExecution(Exception):
    """The request may have reached the broker; repeating it could duplicate a trade."""

MESSAGES={}
for module in (M,C):
    for name in dir(module):
        cls=getattr(module,name)
        if name.startswith('Proto') and hasattr(cls,'DESCRIPTOR') and 'payloadType' in cls.DESCRIPTOR.fields_by_name:
            MESSAGES[cls().payloadType]=cls

def to_dict(message):
    return MessageToDict(message,preserving_proto_field_name=True,use_integers_for_enums=True)

def protection_confirmed(request,event):
    if not isinstance(request,M.ProtoOAAmendPositionSLTPReq) or not isinstance(event,M.ProtoOAExecutionEvent):return False
    if event.executionType not in (2,3,4,11) or event.errorCode or not event.HasField('position'):return False
    p=event.position
    if event.ctidTraderAccountId!=request.ctidTraderAccountId or p.positionId!=request.positionId or p.positionStatus!=1:return False
    for name in ('stopLoss','takeProfit'):
        if request.HasField(name)!=p.HasField(name):return False
        if request.HasField(name) and getattr(request,name)!=getattr(p,name):return False
    return not p.trailingStopLoss if p.HasField('trailingStopLoss') else True

def close_confirmed(request,event,target):
    if target is None or not isinstance(request,M.ProtoOAClosePositionReq) or not isinstance(event,M.ProtoOAExecutionEvent):return False
    if event.executionType not in (2,3,4,11) or event.errorCode or not event.HasField('position'):return False
    p=event.position
    if event.ctidTraderAccountId!=request.ctidTraderAccountId or p.positionId!=request.positionId:return False
    return p.positionStatus==2 or (target>0 and p.positionStatus==1 and p.tradeData.volume<=target)

def pending_confirmed(request,event):
    if not isinstance(event,M.ProtoOAExecutionEvent) or event.errorCode or not event.HasField('order'):return False
    if event.ctidTraderAccountId!=getattr(request,'ctidTraderAccountId',None):return False
    order=event.order
    if isinstance(request,M.ProtoOANewOrderReq) and request.orderType in (2,3,6):
        return order.orderStatus in (1,2) and order.clientOrderId==request.clientOrderId and bool(request.clientOrderId)
    if isinstance(request,M.ProtoOAAmendOrderReq):
        if order.orderId!=request.orderId or event.executionType!=4:return False
        return all(not request.HasField(k) or (order.HasField(k) and getattr(order,k)==getattr(request,k)) for k in ('limitPrice','stopPrice','stopLoss','takeProfit','relativeStopLoss','relativeTakeProfit','expirationTimestamp','slippageInPoints'))
    if isinstance(request,M.ProtoOACancelOrderReq):return order.orderId==request.orderId and order.orderStatus in (4,5)
    return False

class RateLimiter:
    def __init__(self,limit=45):
        self.limit=limit; self.times=collections.deque(); self.lock=asyncio.Lock()
    async def acquire(self):
        async with self.lock:
            while True:
                now=time.monotonic()
                while self.times and self.times[0]<=now-1: self.times.popleft()
                if len(self.times)<self.limit:
                    self.times.append(now); return
                await asyncio.sleep(max(.001,1-(now-self.times[0])))

class Connection:
    HOSTS={'live':'live.ctraderapi.com','demo':'demo.ctraderapi.com'}
    def __init__(self,environment,on_event):
        self.environment=environment; self.on_event=on_event
        self.reader=None; self.writer=None; self.pending={}; self.pending_requests={}; self.pending_targets={}; self.tasks=[]
        self.normal=RateLimiter(40); self.emergency=RateLimiter(5); self.history=RateLimiter(4)
        self.send_lock=asyncio.Lock(); self.alive=False; self.last_received=0
    async def connect(self,client_id,client_secret):
        self.reader,self.writer=await asyncio.wait_for(asyncio.open_connection(self.HOSTS[self.environment],5035,ssl=ssl.create_default_context()),20)
        self.alive=True; self.last_received=time.time()
        self.tasks=[asyncio.create_task(self._read()),asyncio.create_task(self._heartbeats())]
        await self.request(M.ProtoOAApplicationAuthReq(clientId=client_id,clientSecret=client_secret))
    async def _send(self,message,msg_id='',guard=None):
        envelope=C.ProtoMessage(payloadType=message.payloadType,payload=message.SerializeToString(),clientMsgId=msg_id)
        data=envelope.SerializeToString()
        async with self.send_lock:
            if not self.alive: raise ConnectionError('cTrader connection disconnected')
            if guard:guard()
            self.writer.write(struct.pack('!I',len(data))+data)
            await self.writer.drain()
    async def request(self,message,trade=False,historical=False,timeout=15,guard=None,emergency=False,expected_remaining=None):
        await (self.history if historical else self.emergency if emergency else self.normal).acquire()
        msg_id=secrets.token_hex(12); future=asyncio.get_running_loop().create_future()
        self.pending[msg_id]=(future,trade)
        self.pending_requests[msg_id]=message
        self.pending_targets[msg_id]=expected_remaining
        sent=False
        try:
            # Set before writing: a failed drain can still mean delivery.
            sent=True
            await asyncio.wait_for(self._send(message,msg_id,guard),timeout)
            return await asyncio.wait_for(future,timeout)
        except (asyncio.TimeoutError,ConnectionError,OSError) as exc:
            if trade and sent: raise UncertainExecution('Broker confirmation missing; no automatic resend.') from exc
            raise
        finally:
            self.pending.pop(msg_id,None)
            self.pending_requests.pop(msg_id,None)
            self.pending_targets.pop(msg_id,None)
    async def _read(self):
        try:
            while self.alive:
                header=await asyncio.wait_for(self.reader.readexactly(4),45)
                length=struct.unpack('!I',header)[0]
                if not 0<length<=15000000: raise ConnectionError('Invalid cTrader frame')
                envelope=C.ProtoMessage.FromString(await asyncio.wait_for(self.reader.readexactly(length),15))
                self.last_received=time.time()
                cls=MESSAGES.get(envelope.payloadType)
                if not cls: continue
                message=cls.FromString(envelope.payload)
                if isinstance(message,C.ProtoHeartbeatEvent): continue
                pending=self.pending.get(envelope.clientMsgId)
                matched_request=self.pending_requests.get(envelope.clientMsgId)
                matched_target=self.pending_targets.get(envelope.clientMsgId)
                if not pending and isinstance(message,M.ProtoOAExecutionEvent):
                    # Some server events omit clientMsgId; a unique opening label still proves correlation.
                    for correlation,request in list(self.pending_requests.items()):
                        if isinstance(request,M.ProtoOANewOrderReq) and request.ctidTraderAccountId==message.ctidTraderAccountId:
                            order_match=message.HasField('order') and message.order.clientOrderId==request.clientOrderId
                            label_match=message.HasField('position') and message.position.tradeData.label==request.label
                            if order_match or label_match:
                                pending=self.pending.get(correlation);matched_request=request;break
                # Protection updates may arrive without clientMsgId. A broker position
                # with the exact requested pair proves the desired state, unlike ORDER_ACCEPTED alone.
                if not pending and isinstance(message,M.ProtoOAExecutionEvent):
                    candidates=[(key,req) for key,req in self.pending_requests.items() if protection_confirmed(req,message) or close_confirmed(req,message,self.pending_targets.get(key)) or pending_confirmed(req,message)]
                    if len(candidates)==1:
                        correlation,matched_request=candidates[0];pending=self.pending.get(correlation);matched_target=self.pending_targets.get(correlation)
                is_error=isinstance(message,(M.ProtoOAErrorRes,M.ProtoOAOrderErrorEvent,C.ProtoErrorRes))
                if isinstance(message,M.ProtoOAExecutionEvent):
                    is_error=message.executionType in (7,8) or bool(message.errorCode)
                if pending:
                    future,trade=pending
                    # ORDER_ACCEPTED is not a confirmed fill. Wait for final execution.
                    terminal=not trade or is_error or not isinstance(message,M.ProtoOAExecutionEvent) or message.executionType in (3,4,5,6) or protection_confirmed(matched_request,message) or close_confirmed(matched_request,message,matched_target) or pending_confirmed(matched_request,message)
                    if terminal and not future.done():
                        if is_error: future.set_exception(BrokerError(getattr(message,'errorCode','BROKER_ERROR')))
                        else: future.set_result(message)
                # Never await a handler here: it may itself send requests.
                self.on_event(self.environment,message)
        except (Exception,asyncio.CancelledError):
            self.alive=False
            for future,trade in list(self.pending.values()):
                if not future.done(): future.set_exception(ConnectionError('cTrader connection interrupted'))
    async def _heartbeats(self):
        try:
            while self.alive:
                await asyncio.sleep(10)
                await self._send(C.ProtoHeartbeatEvent())
        except (Exception,asyncio.CancelledError):
            self.alive=False
    async def close(self):
        self.alive=False
        for task in self.tasks: task.cancel()
        if self.writer:
            self.writer.close()
            try: await self.writer.wait_closed()
            except Exception: pass
        await asyncio.gather(*self.tasks,return_exceptions=True)
