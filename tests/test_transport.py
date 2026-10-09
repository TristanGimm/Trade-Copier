import asyncio
import struct
import unittest
from copier.transport import Connection,M,C,UncertainExecution

class Writer:
    def __init__(self):self.frames=[]
    def write(self,data):self.frames.append(data)
    async def drain(self):pass

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_official_length_prefix_and_envelope(self):
        connection=Connection('live',lambda *_:None);connection.alive=True;connection.writer=Writer()
        request=M.ProtoOAReconcileReq(ctidTraderAccountId=123)
        await connection._send(request,'correlation-id')
        frame=connection.writer.frames[0];self.assertEqual(struct.unpack('!I',frame[:4])[0],len(frame)-4)
        envelope=C.ProtoMessage.FromString(frame[4:]);self.assertEqual(envelope.clientMsgId,'correlation-id')
        self.assertEqual(M.ProtoOAReconcileReq.FromString(envelope.payload).ctidTraderAccountId,123)
    async def test_accepted_event_does_not_complete_trade(self):
        events=[];connection=Connection('live',lambda *args:events.append(args));connection.alive=True;connection.reader=asyncio.StreamReader()
        future=asyncio.get_running_loop().create_future();connection.pending['id']=(future,True)
        read=asyncio.create_task(connection._read())
        def send(kind):
            message=M.ProtoOAExecutionEvent(ctidTraderAccountId=1,executionType=kind)
            raw=C.ProtoMessage(payloadType=message.payloadType,payload=message.SerializeToString(),clientMsgId='id').SerializeToString()
            connection.reader.feed_data(struct.pack('!I',len(raw))+raw)
        send(2);await asyncio.sleep(.01);self.assertFalse(future.done())
        send(3);result=await asyncio.wait_for(future,1);self.assertEqual(result.executionType,3)
        read.cancel();await asyncio.gather(read,return_exceptions=True)
    async def test_timeout_is_uncertain_and_not_retried(self):
        connection=Connection('live',lambda *_:None);connection.alive=True;connection.writer=Writer()
        request=M.ProtoOAClosePositionReq(ctidTraderAccountId=1,positionId=1,volume=100)
        with self.assertRaises(UncertainExecution):await connection.request(request,trade=True,timeout=.01)
        self.assertEqual(len(connection.writer.frames),1)

    async def test_unique_open_label_correlates_fill_without_client_msg_id(self):
        connection=Connection('live',lambda *_:None);connection.alive=True;connection.writer=Writer();connection.reader=asyncio.StreamReader()
        request=M.ProtoOANewOrderReq(ctidTraderAccountId=1,symbolId=10,orderType=1,tradeSide=1,volume=100,label='gimm:unique',clientOrderId='unique')
        pending=asyncio.create_task(connection.request(request,trade=True,timeout=1))
        await asyncio.sleep(.01)
        message=M.ProtoOAExecutionEvent(ctidTraderAccountId=1,executionType=3)
        message.order.orderId=22;message.order.tradeData.symbolId=10;message.order.tradeData.volume=100;message.order.tradeData.tradeSide=1
        message.order.orderType=1;message.order.orderStatus=2;message.order.clientOrderId='unique'
        data=C.ProtoMessage(payloadType=message.payloadType,payload=message.SerializeToString()).SerializeToString()
        read=asyncio.create_task(connection._read());connection.reader.feed_data(struct.pack('!I',len(data))+data)
        result=await pending;self.assertEqual(result.order.orderId,22)
        read.cancel();await asyncio.gather(read,return_exceptions=True)

    async def test_entry_interlock_is_rechecked_inside_socket_lock(self):
        from copier.safety import EntryBlocked
        connection=Connection('live',lambda *_:None);connection.alive=True;connection.writer=Writer()
        await connection.send_lock.acquire()
        allowed=[True]
        def guard():
            if not allowed[0]:raise EntryBlocked('panic')
        task=asyncio.create_task(connection.request(M.ProtoOAReconcileReq(ctidTraderAccountId=1),trade=True,guard=guard))
        await asyncio.sleep(.01);allowed[0]=False;connection.send_lock.release()
        with self.assertRaises(EntryBlocked):await task
        self.assertEqual(connection.writer.frames,[])

    async def test_stalled_socket_drain_is_bounded_and_uncertain(self):
        class StalledWriter(Writer):
            async def drain(self):await asyncio.Event().wait()
        connection=Connection('live',lambda *_:None);connection.alive=True;connection.writer=StalledWriter()
        with self.assertRaises(UncertainExecution):
            await connection.request(M.ProtoOAClosePositionReq(ctidTraderAccountId=1,positionId=2,volume=100),trade=True,timeout=.01)
        self.assertEqual(len(connection.writer.frames),1)
        self.assertFalse(connection.send_lock.locked())

    async def test_amend_without_correlation_accepts_only_exact_broker_protection(self):
        from copier.protocol import OpenApiModelMessages_pb2 as Model
        connection=Connection('demo',lambda *_:None);connection.alive=True;connection.writer=Writer();connection.reader=asyncio.StreamReader()
        request=M.ProtoOAAmendPositionSLTPReq(ctidTraderAccountId=2,positionId=123,stopLoss=2990,takeProfit=3040,trailingStopLoss=False)
        task=asyncio.create_task(connection.request(request,trade=True,timeout=1));await asyncio.sleep(.01)
        read=asyncio.create_task(connection._read())
        def feed(account=2,pid=123,sl=2990,tp=3040):
            p=Model.ProtoOAPosition(positionId=pid,tradeData=Model.ProtoOATradeData(symbolId=10,volume=100,tradeSide=1),positionStatus=1,swap=0,stopLoss=sl,takeProfit=tp)
            event=M.ProtoOAExecutionEvent(ctidTraderAccountId=account,executionType=2,position=p)
            raw=C.ProtoMessage(payloadType=event.payloadType,payload=event.SerializeToString()).SerializeToString()
            connection.reader.feed_data(struct.pack('!I',len(raw))+raw)
        for params in ({'account':3},{'pid':124},{'sl':2980},{'tp':3050}):
            feed(**params);await asyncio.sleep(.01);self.assertFalse(task.done())
        feed();result=await task;self.assertEqual(result.position.stopLoss,2990)
        read.cancel();await asyncio.gather(read,return_exceptions=True)

    async def test_accepted_amend_without_position_is_not_confirmation(self):
        connection=Connection('demo',lambda *_:None);connection.alive=True;connection.writer=Writer();connection.reader=asyncio.StreamReader()
        request=M.ProtoOAAmendPositionSLTPReq(ctidTraderAccountId=2,positionId=123,stopLoss=2990)
        task=asyncio.create_task(connection.request(request,trade=True,timeout=.1));await asyncio.sleep(.01)
        correlation=next(iter(connection.pending))
        event=M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=2)
        raw=C.ProtoMessage(payloadType=event.payloadType,payload=event.SerializeToString(),clientMsgId=correlation).SerializeToString()
        read=asyncio.create_task(connection._read());connection.reader.feed_data(struct.pack('!I',len(raw))+raw)
        with self.assertRaises(UncertainExecution):await task
        read.cancel();await asyncio.gather(read,return_exceptions=True)

    async def test_full_close_without_correlation_is_confirmed_by_closed_position(self):
        from copier.protocol import OpenApiModelMessages_pb2 as Model
        connection=Connection('demo',lambda *_:None);connection.alive=True;connection.writer=Writer();connection.reader=asyncio.StreamReader()
        req=M.ProtoOAClosePositionReq(ctidTraderAccountId=2,positionId=123,volume=1000)
        task=asyncio.create_task(connection.request(req,trade=True,expected_remaining=0,timeout=1));await asyncio.sleep(.01)
        read=asyncio.create_task(connection._read())
        def feed(pid,status,volume):
            p=Model.ProtoOAPosition(positionId=pid,tradeData=Model.ProtoOATradeData(symbolId=10,volume=volume,tradeSide=1),positionStatus=status,swap=0)
            event=M.ProtoOAExecutionEvent(ctidTraderAccountId=2,executionType=11,position=p)
            raw=C.ProtoMessage(payloadType=event.payloadType,payload=event.SerializeToString()).SerializeToString()
            connection.reader.feed_data(struct.pack('!I',len(raw))+raw)
        feed(123,1,500);await asyncio.sleep(.01);self.assertFalse(task.done())
        feed(124,2,0);await asyncio.sleep(.01);self.assertFalse(task.done())
        feed(123,2,0);self.assertEqual((await task).position.positionStatus,2)
        read.cancel();await asyncio.gather(read,return_exceptions=True)
