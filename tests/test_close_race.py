import time
import test_engine as fixtures
from copier import store
from copier.transport import M,BrokerError

class CloseRaceTests(fixtures.EngineTests):
    async def race(self,kind,absent=True):
        self.source(fixtures.position(sl=2990,tp=3040));await self.settle()
        original=self.conn.request
        async def request(r,**kwargs):
            expected=M.ProtoOAClosePositionReq if kind=='close' else M.ProtoOAAmendPositionSLTPReq
            if isinstance(r,expected):
                if absent:self.conn.positions[201].positionStatus=2
                raise BrokerError('POSITION_NOT_FOUND')
            return await original(r,**kwargs)
        self.conn.request=request
        self.source(fixtures.position(status=2,volume=0,stamp=int(time.time()*1000)+1000) if kind=='close' else fixtures.position(sl=2995,tp=3040,stamp=int(time.time()*1000)+1000))
        await self.settle()
    async def test_tp_wins_race_with_close(self):
        await self.race('close')
        self.assertEqual(self.mapping()['status'],'closed')
        self.assertNotEqual(store.setting('safety_latched'),'true')
        self.assertEqual(store.one("SELECT status FROM commands WHERE kind='close'")['status'],'reconciled')
    async def test_tp_wins_race_with_amend(self):
        await self.race('amend')
        self.assertEqual(self.mapping()['status'],'closed')
        self.assertNotEqual(store.setting('safety_latched'),'true')
    async def test_not_found_with_still_open_position_is_failure(self):
        await self.race('close',absent=False)
        self.assertEqual(store.setting('safety_latched'),'true')
