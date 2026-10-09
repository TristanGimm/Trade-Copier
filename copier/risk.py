"""Display-only fixed loss budgets. Never issues orders or pauses the copier."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from . import store

def day_key(now=None):
    return datetime.fromtimestamp(time.time() if now is None else now,ZoneInfo('Europe/Berlin')).date().isoformat()

def record_day(account_id,balance,now=None):
    store.execute("INSERT OR IGNORE INTO risk_days VALUES(?,?,?,'first_snapshot')",(account_id,day_key(now),balance))

def view(account,now=None):
    config=store.one('SELECT * FROM risk_limits WHERE account_id=?',(account['id'],))
    if not config:return None
    day=store.one('SELECT * FROM risk_days WHERE account_id=? AND day=?',(account['id'],day_key(now)))
    def budget(reference,limit):
        if reference is None or limit is None or account['equity'] is None:return None
        floor=reference-limit;remaining=account['equity']-floor
        return dict(limit=limit,floor=floor,remaining=remaining,used=reference-account['equity'],
                    warning=remaining<=limit*config['warning_pct']/100,breached=remaining<=0,
                    used_pct=max(0,min(100,(reference-account['equity'])/limit*100)))
    return dict(config=config,total=budget(config['initial_balance'],config['max_loss']),
                daily=budget(day['balance'] if day else None,config['daily_loss']),day=day,
                stale=not account['updated'] or time.time()-account['updated']>30)
