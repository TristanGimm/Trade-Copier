import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from cryptography.fernet import Fernet
from copier import store,oauth
from copier.engine import Engine
from copier.transport import M
from copier.protocol import OpenApiModelMessages_pb2 as Model
from test_engine import FakeConnection,SYMBOL,add_account

class BrokerService(FakeConnection):
    async def connect(self,*args):self.alive=True
    async def close(self):self.alive=False
    async def request(self,request,**kwargs):
        bid=getattr(request,'ctidTraderAccountId',1)
        if isinstance(request,M.ProtoOAGetAccountListByAccessTokenReq):
            return M.ProtoOAGetAccountListByAccessTokenRes(accessToken=request.accessToken,permissionScope=1,ctidTraderAccount=[Model.ProtoOACtidTraderAccount(ctidTraderAccountId=1,isLive=True,traderLogin=1001,brokerTitleShort='Pepperstone')])
        if isinstance(request,M.ProtoOATraderReq):
            return M.ProtoOATraderRes(ctidTraderAccountId=bid,trader=Model.ProtoOATrader(ctidTraderAccountId=bid,balance=1000000,depositAssetId=2,accountType=0,moneyDigits=2))
        if isinstance(request,M.ProtoOAAssetListReq):
            return M.ProtoOAAssetListRes(ctidTraderAccountId=bid,asset=[Model.ProtoOAAsset(assetId=1,name='XAU'),Model.ProtoOAAsset(assetId=2,name='USD')])
        if isinstance(request,M.ProtoOASymbolsListReq):
            return M.ProtoOASymbolsListRes(ctidTraderAccountId=bid,symbol=[Model.ProtoOALightSymbol(symbolId=10,symbolName='XAUUSD',enabled=True,baseAssetId=1,quoteAssetId=2)])
        if isinstance(request,M.ProtoOASymbolByIdReq):
            return M.ProtoOASymbolByIdRes(ctidTraderAccountId=bid,symbol=[Model.ProtoOASymbol(symbolId=10,digits=2,pipPosition=1,lotSize=10000,minVolume=100,maxVolume=1000000,stepVolume=100)])
        if isinstance(request,M.ProtoOAGetPositionUnrealizedPnLReq):
            return M.ProtoOAGetPositionUnrealizedPnLRes(ctidTraderAccountId=bid,moneyDigits=2,positionUnrealizedPnL=[Model.ProtoOAPositionUnrealizedPnL(positionId=100,grossUnrealizedPnL=13000,netUnrealizedPnL=12500)])
        if isinstance(request,M.ProtoOADealListReq):
            detail=Model.ProtoOAClosePositionDetail(entryPrice=3000,grossProfit=10000,swap=-100,commission=-300,balance=1009600,moneyDigits=2,pnlConversionFee=50)
            deal=Model.ProtoOADeal(dealId=99,orderId=199,positionId=299,volume=1000,filledVolume=1000,symbolId=10,createTimestamp=int(time.time()*1000),executionTimestamp=int(time.time()*1000),tradeSide=2,dealStatus=2,closePositionDetail=detail,moneyDigits=2)
            return M.ProtoOADealListRes(ctidTraderAccountId=bid,deal=[deal],hasMore=False)
        return await super().request(request,**kwargs)

class BrokerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.old=store.DB;store.DB=self.temp.name+'/broker.db';store.init()
        self.env=patch.dict(os.environ,{'CTRADER_CLIENT_ID':'test','CTRADER_CLIENT_SECRET':'secret','TOKEN_ENCRYPTION_KEY':Fernet.generate_key().decode()});self.env.start()
        oauth.save_token({'accessToken':'access','refreshToken':'refresh','expiresIn':3600},'grant')
        self.services={}
        def factory(env,event):
            connection=BrokerService();self.services[env]=connection;return connection
        self.engine=Engine(factory)
    async def asyncTearDown(self):self.env.stop();store.DB=self.old;self.temp.cleanup()
    async def test_discover_auth_symbol_metadata_and_real_snapshot_flow(self):
        await self.engine.ensure_connections();discovered=store.one('SELECT * FROM discovered')
        self.assertEqual((discovered['broker_id'],discovered['broker']),(1,'Pepperstone'))
        add_account('live:1','master',1)
        await self.engine.initialize_account(store.account('live:1'))
        self.assertIn('live:1',self.engine.ready)
        account=store.account('live:1');self.assertEqual(account['currency'],'USD');self.assertEqual(account['symbol_id'],10)
        await self.engine.snapshot(account)
        account=store.account('live:1');self.assertEqual(account['balance'],10000);self.assertEqual(account['equity'],10125);self.assertEqual(account['unrealized'],125)
        self.assertEqual(len(store.rows('SELECT * FROM equity_samples')),1)
    async def test_refresh_token_rotates_encrypted_grant(self):
        store.execute('UPDATE oauth_tokens SET expires=?',(time.time()-1,))
        with patch('copier.oauth.token_request',return_value={'accessToken':'new-access','refreshToken':'new-refresh','expiresIn':3600}) as refresh:
            await self.engine.ensure_connections()
        refresh.assert_called_once_with(grant_type='refresh_token',refresh_token='refresh')
        self.assertEqual(oauth.load_token('grant')['refreshToken'],'new-refresh');self.assertEqual(self.engine.grants['grant'],'new-access')
    async def test_history_net_money_digits_and_deduplication(self):
        await self.engine.ensure_connections();add_account('live:1','master',1);await self.engine.initialize_account(store.account('live:1'))
        await self.engine.history(store.account('live:1'));self.engine.history_time.clear();await self.engine.history(store.account('live:1'))
        deals=store.rows('SELECT * FROM deals');self.assertEqual(len(deals),1);self.assertEqual(deals[0]['net'],95.5)
    async def test_negative_conversion_fee_is_a_cost(self):
        add_account('live:1','master',1)
        detail=Model.ProtoOAClosePositionDetail(entryPrice=3000,grossProfit=2192,swap=0,commission=-52,balance=997013,moneyDigits=2,pnlConversionFee=-16)
        deal=Model.ProtoOADeal(dealId=1,orderId=1,positionId=1,volume=1000,filledVolume=1000,symbolId=10,createTimestamp=1,executionTimestamp=1,tradeSide=2,dealStatus=2,closePositionDetail=detail,moneyDigits=2)
        self.engine.save_deal(store.account('live:1'),deal)
        self.assertAlmostEqual(store.one('SELECT net FROM deals')['net'],21.24)
