"""Server-only cTrader credentials and encrypted rotating account grants."""
import json
import os
import secrets
import time
import urllib.request
from urllib.parse import urlencode
from cryptography.fernet import Fernet
from . import store

TOKEN_ENDPOINT='https://openapi.ctrader.com/apps/token'

def cipher():
    key=os.environ.get('TOKEN_ENCRYPTION_KEY','')
    if not key: raise ValueError('TOKEN_ENCRYPTION_KEY is missing from the server configuration.')
    try: return Fernet(key.encode())
    except Exception: raise ValueError('TOKEN_ENCRYPTION_KEY is not a valid Fernet key.') from None

def credentials():
    client=os.environ.get('CTRADER_CLIENT_ID',''); secret=os.environ.get('CTRADER_CLIENT_SECRET','')
    if not client or not secret: raise ValueError('cTrader client ID and client secret are missing from the server configuration.')
    cipher()
    return client,secret

def token_request(**params):
    client,secret=credentials()
    query=urlencode(dict(params,client_id=client,client_secret=secret))
    request=urllib.request.Request(TOKEN_ENDPOINT+'?'+query,headers={'Accept':'application/json'})
    try:
        with urllib.request.urlopen(request,timeout=20) as response:
            data=json.loads(response.read(65536))
    except Exception:
        # Do not expose exception URLs: they contain the client secret and auth code.
        raise ValueError('cTrader token exchange failed. Restart authorization.') from None
    if data.get('errorCode') or not data.get('accessToken') or not data.get('refreshToken'):
        raise ValueError('cTrader rejected authorization. Check permissions and redirect URI.')
    return data

def save_token(data,token_id=None):
    token_id=token_id or secrets.token_hex(12)
    encrypted=cipher().encrypt(json.dumps({'accessToken':data['accessToken'],'refreshToken':data['refreshToken']}).encode()).decode()
    store.execute('INSERT OR REPLACE INTO oauth_tokens VALUES(?,?,?)',(token_id,encrypted,time.time()+int(data.get('expiresIn',2628000))))
    return token_id

def load_token(token_id):
    row=store.one('SELECT * FROM oauth_tokens WHERE id=?',(token_id,))
    if not row: raise ValueError('Account authorization not found.')
    try: return json.loads(cipher().decrypt(row['secret'].encode()))
    except Exception: raise ValueError('Account authorization cannot be decrypted with this key.') from None

def begin(session_token,origin):
    client,_=credentials(); state=secrets.token_urlsafe(32)
    store.execute('DELETE FROM oauth_pending WHERE expires<?',(time.time(),))
    store.execute('INSERT INTO oauth_pending VALUES(?,?,?)',(state,session_token,time.time()+600))
    redirect=origin+'/ctrader/callback'
    url='https://id.ctrader.com/my/settings/openapi/grantingaccess/?'+urlencode(dict(client_id=client,redirect_uri=redirect,scope='trading',product='web',state=state))
    return {'url':url,'state':state,'redirectUri':redirect}

def exchange(code,state,session_token,origin):
    if not isinstance(code,str) or not 1<=len(code)<=2048: raise ValueError('Invalid authorization code.')
    with store.db() as c:
        pending=c.execute('SELECT * FROM oauth_pending WHERE state=? AND session_token=? AND expires>?',(state,session_token,time.time())).fetchone()
        if not pending: raise ValueError('Authorization expired or belongs to another session.')
        c.execute('DELETE FROM oauth_pending WHERE state=?',(state,))
    token=token_request(grant_type='authorization_code',code=code,redirect_uri=origin+'/ctrader/callback')
    save_token(token)
    store.audit('cTrader account authorization saved with encryption.')
    return {'ok':True}
