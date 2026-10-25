#!/usr/bin/env python3
"""BTC Paper Engine V18.0 — public market data only; paper trading only."""
import json, math, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BUILD = "BTC_PAPER_V18_0_2026-10-25"
API = "https://api.hyperliquid.xyz/info"
COIN = "BTC"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DATA_FILE = ROOT / "docs" / "data.json"

INITIAL_EQUITY = 100.0
LEVERAGE = 10.0
RISK_PER_TRADE = 0.005
MAX_MARGIN_FRACTION = 0.20
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
ATR_STOP = 1.8
ATR_TARGET = 2.7
MIN_SIGNAL_SCORE = 5
MIN_SCORE_EDGE = 1
COOLDOWN_SECONDS = 300
MAX_HOLD_SECONDS = 86400
DAILY_LOSS_LIMIT = 0.08
MAX_DRAWDOWN_LIMIT = 0.25
DRAWDOWN_PAUSE_SECONDS = 21600
MAX_CONSECUTIVE_LOSSES = 6
CONSECUTIVE_PAUSE_SECONDS = 3600
HTTP_TIMEOUT = 20
HISTORY_LIMIT = 1000
TRADE_LIMIT = 1000

def now_ts(): return int(time.time())
def iso_now(): return datetime.now(timezone.utc).isoformat()
def utc_date(): return datetime.now(timezone.utc).date().isoformat()

def f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except (TypeError, ValueError, OverflowError):
        return float(default)

def i(v, default=0):
    try: return int(float(v))
    except (TypeError, ValueError, OverflowError): return default

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.replace(path)

def read_json(path, default):
    try:
        with path.open(encoding="utf-8") as src: return json.load(src)
    except (OSError, json.JSONDecodeError): return default

def post(payload):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type":"application/json","User-Agent":"BTC-Paper-Engine-V18/1.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as res:
        return json.loads(res.read().decode())

def default_state():
    return {
        "build": BUILD, "initial_equity": INITIAL_EQUITY, "equity": INITIAL_EQUITY,
        "peak_equity": INITIAL_EQUITY, "realized_pnl": 0.0, "trades": [],
        "equity_history": [], "position": None, "last_close_ts": 0,
        "last_entry_candle": 0, "consecutive_losses": 0, "consecutive_lock_until": 0,
        "daily_date": utc_date(), "daily_start_equity": INITIAL_EQUITY,
        "daily_loss_lock": False, "drawdown_pause_until": 0,
        "last_error": None, "events": []
    }

def event(s, kind, **extra):
    s.setdefault("events", []).append({"timestamp":iso_now(),"type":kind,**extra})
    s["events"] = s["events"][-200:]

def load_state():
    s = read_json(STATE_FILE, {})
    if not isinstance(s, dict) or not s: return default_state()
    d = default_state()
    for k,v in d.items(): s.setdefault(k,v)
    s["initial_equity"] = f(s.get("initial_equity"), INITIAL_EQUITY)
    s["equity"] = f(s.get("equity"), s["initial_equity"])
    s["peak_equity"] = max(f(s.get("peak_equity"),s["equity"]),s["equity"])
    s["realized_pnl"] = f(s.get("realized_pnl"))
    s["trades"] = s.get("trades",s.get("paper_trades",[]))
    if not isinstance(s["trades"],list): s["trades"]=[]
    s["equity_history"] = s.get("equity_history",[])
    if not isinstance(s["equity_history"],list): s["equity_history"]=[]
    pos = s.get("position") or s.get("open_position")
    s["position"] = pos if isinstance(pos,dict) else None
    for k in ("last_close_ts","last_entry_candle","consecutive_lock_until","drawdown_pause_until"):
        s[k]=max(0,i(s.get(k)))
    s["consecutive_losses"]=max(0,i(s.get("consecutive_losses")))
    s["daily_start_equity"]=f(s.get("daily_start_equity"),s["equity"])
    s["daily_loss_lock"]=bool(s.get("daily_loss_lock"))
    if not isinstance(s.get("events"),list): s["events"]=[]
    # Older builds used a permanent boolean drawdown lock. Convert it to a timed pause
    # so a paper test cannot remain frozen forever after a code upgrade.
    if s.pop("drawdown_lock",False) and not s.get("drawdown_pause_until"):
        s["drawdown_pause_until"]=now_ts()+DRAWDOWN_PAUSE_SECONDS
        event(s,"LEGACY_DRAWDOWN_LOCK_MIGRATED",pause_seconds=DRAWDOWN_PAUSE_SECONDS)
    s["build"]=BUILD
    return s

def fetch_price_funding():
    mids=post({"type":"allMids"})
    price=f(mids.get(COIN)) if isinstance(mids,dict) else 0
    if price<=0: raise RuntimeError("BTC price unavailable from Hyperliquid")
    funding=0.0
    try:
        r=post({"type":"metaAndAssetCtxs"})
        if isinstance(r,list) and len(r)>1:
            for n,a in enumerate(r[0].get("universe",[])):
                if a.get("name")==COIN and n<len(r[1]):
                    funding=f(r[1][n].get("funding")); break
    except Exception as e: print("Funding unavailable:",e)
    return price,funding

def fetch_candles(interval, limit=200):
    mins={"15m":15,"1h":60,"4h":240}[interval]
    end=int(time.time()*1000); start=end-mins*60_000*(limit+5)
    r=post({"type":"candleSnapshot","req":{"coin":COIN,"interval":interval,"startTime":start,"endTime":end}})
    if not isinstance(r,list): raise RuntimeError("Invalid candle response: "+interval)
    out=[]
    for x in r:
        c={"t":i(x.get("t")),"T":i(x.get("T")),"o":f(x.get("o")),"h":f(x.get("h")),"l":f(x.get("l")),"c":f(x.get("c")),"v":f(x.get("v")),"n":i(x.get("n"))}
        if c["t"]>0 and c["c"]>0 and c["h"]>=c["l"]>0 and c["t"]+mins*60_000<=end:
            out.append(c)
    out.sort(key=lambda x:x["t"])
    if len(out)<35: raise RuntimeError("Not enough closed candles: "+interval)
    return out[-limit:]

def ema(values, period):
    if not values:return []
    a=2/(period+1); out=[values[0]]
    for x in values[1:]: out.append(a*x+(1-a)*out[-1])
    return out

def rsi(values, period=14):
    if len(values)<period+1:return 50.0
    changes=[values[n]-values[n-1] for n in range(1,len(values))]
    gains=[max(x,0) for x in changes]; losses=[max(-x,0) for x in changes]
    ag=sum(gains[:period])/period; al=sum(losses[:period])/period
    for n in range(period,len(changes)):
        ag=(ag*(period-1)+gains[n])/period; al=(al*(period-1)+losses[n])/period
    if al<=0:return 100.0 if ag>0 else 50.0
    return 100-100/(1+ag/al)

def atr(candles, period=14):
    tr=[]
    for n in range(1,len(candles)):
        c,p=candles[n],candles[n-1]["c"]
        tr.append(max(c["h"]-c["l"],abs(c["h"]-p),abs(c["l"]-p)))
    if not tr:return candles[-1]["c"]*.002
    v=sum(tr[:period])/min(period,len(tr))
    for x in tr[period:]:v=(v*(period-1)+x)/period
    return max(v,.01)

def trend(candles):
    vals=[x["c"] for x in candles]; a=ema(vals,9)[-1]; b=ema(vals,21)[-1]
    if a>b*1.0003:return "UP"
    if a<b*.9997:return "DOWN"
    return "SIDEWAYS"

def make_signal(c15,c1,c4,funding):
    values=[x["c"] for x in c15]; e9=ema(values,9); e21=ema(values,21)
    rv=rsi(values); av=atr(c15)
    ts={"15m":trend(c15),"1h":trend(c1),"4h":trend(c4)}
    long=short=0; reasons=[]; flags=[]
    if e9[-1]>e21[-1]:long+=2; reasons.append("15m EMA上向き")
    elif e9[-1]<e21[-1]:short+=2; reasons.append("15m EMA下向き")
    for tf,d in ts.items():
        if d=="UP":long+=2 if tf!="15m" else 1; reasons.append(tf+"上昇")
        elif d=="DOWN":short+=2 if tf!="15m" else 1; reasons.append(tf+"下降")
    if 43<=rv<=68:long+=1
    if 32<=rv<=57:short+=1
    if rv>78:long-=1;flags.append("RSI買われ過ぎ")
    if rv<22:short-=1;flags.append("RSI売られ過ぎ")
    if len(values)>=5:
        mom=values[-1]/values[-5]-1
        if mom>.0008:long+=1;reasons.append("短期モメンタム上")
        elif mom<-.0008:short+=1;reasons.append("短期モメンタム下")
    if funding>.0003:short+=1;reasons.append("プラス資金調達率")
    elif funding<-.0003:long+=1;reasons.append("マイナス資金調達率")
    edge=abs(long-short); direction="WAIT"
    fib_window=c1[-min(80,len(c1)):]
    fib_high=max(x["h"] for x in fib_window)
    fib_low=min(x["l"] for x in fib_window)
    fib_levels={k:fib_high-(fib_high-fib_low)*pct for k,pct in {
        "0.236":0.236,"0.382":0.382,"0.500":0.500,"0.618":0.618,"0.786":0.786
    }.items()}
    fib_data={"swing_high":fib_high,"swing_low":fib_low,"levels":fib_levels,"trend":ts["1h"]}
    if long>=MIN_SIGNAL_SCORE and long-short>=MIN_SCORE_EDGE:direction="LONG"
    elif short>=MIN_SIGNAL_SCORE and short-long>=MIN_SCORE_EDGE:direction="SHORT"
    return {"direction":direction,"score":max(long,short),"long_score":long,"short_score":short,
        "score_edge":edge,"rsi":round(rv,3),"atr":round(av,5),"ema9":e9[-1],"ema21":e21[-1],
        "trend_15m":ts["15m"],"trend_1h":ts["1h"],"trend_4h":ts["4h"],
        "timestamp":iso_now(),"reference_price":values[-1],"reasons":reasons or ["方向優位性なし"],
        "flags":flags,"fibonacci":fib_data}

def unrealized(pos, price):
    if not pos:return 0.0
    q=f(pos.get("quantity")); e=f(pos.get("entry_price"))
    return (price-e)*q if pos.get("side")=="LONG" else (e-price)*q

def close_position(s, price, reason):
    p=s.get("position")
    if not p:return False
    side=p["side"]; q=f(p["quantity"]); entry=f(p["entry_price"])
    exitp=price*(1-SLIPPAGE_RATE if side=="LONG" else 1+SLIPPAGE_RATE)
    gross=(exitp-entry)*q if side=="LONG" else (entry-exitp)*q
    exit_fee=abs(exitp*q)*FEE_RATE; entry_fee=f(p.get("entry_fee"))
    net=gross-entry_fee-exit_fee
    s["equity"]+=gross-exit_fee
    s["realized_pnl"]+=net
    tr={"side":side,"direction":side,"entry_price":entry,"exit_price":exitp,"quantity":q,
        "leverage":p.get("leverage",LEVERAGE),"margin_used":p.get("margin_used",0),
        "entry_time":p.get("entry_time"),"exit_time":iso_now(),"exit_timestamp":now_ts(),
        "gross_pnl":gross,"entry_fee":entry_fee,"exit_fee":exit_fee,"fees":entry_fee+exit_fee,
        "net_pnl":net,"pnl":net,"reason":reason,"exit_reason":reason,"build":BUILD}
    s["trades"].append(tr);s["trades"]=s["trades"][-TRADE_LIMIT:]
    s["consecutive_losses"]=s["consecutive_losses"]+1 if net<0 else 0
    if s["consecutive_losses"]>=MAX_CONSECUTIVE_LOSSES:
        s["consecutive_lock_until"]=now_ts()+CONSECUTIVE_PAUSE_SECONDS
    s["last_close_ts"]=now_ts();s["position"]=None
    s["peak_equity"]=max(s["peak_equity"],s["equity"])
    event(s,"POSITION_CLOSED",side=side,reason=reason,net_pnl=net)
    return True

def size_position(equity, entry, stop):
    distance=abs(entry-stop)
    risk=equity*RISK_PER_TRADE
    cost=distance+entry*(2*FEE_RATE+2*SLIPPAGE_RATE)
    if equity<=0 or distance<=0 or cost<=0:return None
    q=min(risk/cost,equity*MAX_MARGIN_FRACTION*LEVERAGE/entry)
    if q<=0:return None
    return {"quantity":q,"margin_used":q*entry/LEVERAGE,"notional":q*entry,"planned_risk":q*cost}

def open_position(s, side, price, av, sig, candle_ts):
    entry=price*(1+SLIPPAGE_RATE if side=="LONG" else 1-SLIPPAGE_RATE)
    dist=max(av*ATR_STOP,price*.0012); target_dist=max(av*ATR_TARGET,dist*1.5)
    stop=entry-dist if side=="LONG" else entry+dist
    target=entry+target_dist if side=="LONG" else entry-target_dist
    sz=size_position(s["equity"],entry,stop)
    if not sz:return False,"数量計算失敗"
    fee=sz["notional"]*FEE_RATE
    if fee>=s["equity"]:return False,"手数料が資金を超過"
    s["equity"]-=fee
    s["position"]={"side":side,"entry_price":entry,"quantity":sz["quantity"],"leverage":LEVERAGE,
        "margin_used":sz["margin_used"],"notional":sz["notional"],"stop_price":stop,"target_price":target,
        "risk_distance":dist,"entry_fee":fee,"entry_time":iso_now(),"opened_timestamp":now_ts(),
        "last_checked_candle_ts":candle_ts,"entry_signal_score":sig["score"],"entry_rsi":sig["rsi"]}
    s["last_entry_candle"]=candle_ts
    event(s,"POSITION_OPENED",side=side,entry_price=entry,stop_price=stop,target_price=target)
    return True,side+" opened"

def manage_position(s,candles,price):
    p=s.get("position")
    if not p:return
    side=p["side"]; stop=f(p.get("stop_price")); target=f(p.get("target_price"))
    cursor=i(p.get("last_checked_candle_ts"))
    for c in candles:
        if c["t"]<=cursor:continue
        if side=="LONG":
            if c["l"]<=stop:close_position(s,stop,"STOP_LOSS");return
            if c["h"]>=target:close_position(s,target,"TAKE_PROFIT");return
        else:
            if c["h"]>=stop:close_position(s,stop,"STOP_LOSS");return
            if c["l"]<=target:close_position(s,target,"TAKE_PROFIT");return
        p["last_checked_candle_ts"]=c["t"];cursor=c["t"]
    if now_ts()-i(p.get("opened_timestamp"))>=MAX_HOLD_SECONDS:
        close_position(s,price,"MAX_HOLD_TIME")

def make_dashboard(s,price,funding,sig,status):
    p=s.get("position"); upnl=unrealized(p,price) if p else 0.0
    marked=s["equity"]+upnl; initial=max(f(s["initial_equity"]),.01)
    s["peak_equity"]=max(s["peak_equity"],marked)
    dd=max(0,(s["peak_equity"]-marked)/max(s["peak_equity"],.01))*100
    if s["daily_date"]!=utc_date():
        s["daily_date"]=utc_date();s["daily_start_equity"]=s["equity"];s["daily_loss_lock"]=False
        event(s,"DAILY_RESET")
    dayloss=max(0,(s["daily_start_equity"]-marked)/max(s["daily_start_equity"],.01))*100
    if dayloss>=DAILY_LOSS_LIMIT*100:s["daily_loss_lock"]=True
    if dd>=MAX_DRAWDOWN_LIMIT*100 and s["drawdown_pause_until"]<now_ts():
        s["drawdown_pause_until"]=now_ts()+DRAWDOWN_PAUSE_SECONDS
        event(s,"DRAWDOWN_PAUSE_STARTED",drawdown_pct=dd)
    if s["drawdown_pause_until"] and s["drawdown_pause_until"]<=now_ts():
        s["drawdown_pause_until"]=0;event(s,"DRAWDOWN_PAUSE_ENDED")
    trades=s["trades"]; pnls=[f(t.get("net_pnl",t.get("pnl"))) for t in trades]
    wins=sum(x>0 for x in pnls); losses=sum(x<0 for x in pnls)
    history=s["equity_history"];history.append({"timestamp":iso_now(),"equity":s["equity"],"marked_equity":marked,"price":price})
    s["equity_history"]=history[-HISTORY_LIMIT:]
    pos=dict(p) if p else None
    if pos:pos["unrealized_pnl"]=upnl
    return {"build":BUILD,"generated_at":iso_now(),"market_status":"OK","price":price,
        "funding_rate":funding,"funding_rate_pct":funding*100,"initial_equity":initial,
        "equity":s["equity"],"marked_equity":marked,"realized_pnl":s["realized_pnl"],
        "unrealized_pnl":upnl,"net_pnl":marked-initial,"net_pnl_pct":(marked-initial)/initial*100,
        "position":pos,"open_position":pos,"has_position":bool(pos),"signal":{**sig,"entry_status":status,
        "paper_only":True,"live_trading_enabled":False},"risk":{"daily_loss_pct":dayloss,
        "daily_loss_limit_pct":DAILY_LOSS_LIMIT*100,"daily_loss_lock":s["daily_loss_lock"],
        "drawdown_pct":dd,"drawdown_limit_pct":MAX_DRAWDOWN_LIMIT*100,
        "drawdown_pause_until":s["drawdown_pause_until"],"drawdown_lock":s["drawdown_pause_until"]>now_ts(),
        "consecutive_losses":s["consecutive_losses"],"consecutive_lock_until":s["consecutive_lock_until"],
        "consecutive_loss_lock":s["consecutive_lock_until"]>now_ts(),"leverage":LEVERAGE,
        "risk_per_trade_pct":RISK_PER_TRADE*100,"max_margin_pct":MAX_MARGIN_FRACTION*100,
        "paper_only":True,"live_trading_enabled":False},
        "metrics":{"closed_trades":len(trades),"wins":wins,"losses":losses,
        "win_rate":wins/len(trades)*100 if trades else 0,"total_net_pnl":sum(pnls),
        "average_trade":sum(pnls)/len(pnls) if pnls else 0,
        "profit_factor":sum(x for x in pnls if x>0)/abs(sum(x for x in pnls if x<0)) if any(x<0 for x in pnls) else (999 if wins else 0),
        "expectancy_per_trade":sum(pnls)/len(pnls) if pnls else 0},
        "closed_trades":len(trades),"win_rate":wins/len(trades)*100 if trades else 0,
        "trades":trades[-200:],"paper_trades":trades[-200:],"recent_trades":trades[-200:],
        "equity_history":s["equity_history"][-300:],"events":s["events"][-100:],
        "timeframes":{"15m":{"direction":sig["trend_15m"]},"1h":{"direction":sig["trend_1h"]},"4h":{"direction":sig["trend_4h"]}},
        "indicators":{"rsi_15m":sig["rsi"],"atr":sig["atr"]},
        "fibonacci":sig.get("fibonacci",{}),"config":{"leverage":LEVERAGE,"paper_only":True,
        "live_trading_enabled":False,"min_signal_score":MIN_SIGNAL_SCORE,"min_score_edge":MIN_SCORE_EDGE},
        "last_error":s.get("last_error")}

def main():
    s=load_state();s["last_error"]=None
    try:
        price,funding=fetch_price_funding()
        c15,c1,c4=fetch_candles("15m"),fetch_candles("1h"),fetch_candles("4h")
        sig=make_signal(c15,c1,c4,funding);latest=c15[-1]["t"]
        had_position=bool(s.get("position"))
        if had_position:manage_position(s,c15,price)
        if had_position and not s.get("position"):
            s["last_entry_candle"]=c15[-1]["t"]
        status="WAIT: シグナル条件未達"
        now=now_ts()
        if s.get("position"):
            status="保有中"
        elif s["daily_loss_lock"]:
            status="日次損失停止中（UTC日付更新で解除）"
        elif s["drawdown_pause_until"]>now:
            status=f"ドローダウン休止中（残り{(s['drawdown_pause_until']-now)//60}分）"
        elif s["consecutive_lock_until"]>now:
            status=f"連敗休止中（残り{(s['consecutive_lock_until']-now)//60}分）"
        elif now-s["last_close_ts"]<COOLDOWN_SECONDS:
            status="クールダウン中"
        elif latest==s["last_entry_candle"]:
            status="同じ15分足での再エントリー防止"
        elif sig["direction"] in ("LONG","SHORT"):
            ok,msg=open_position(s,sig["direction"],price,max(sig["atr"],price*.0005),sig,latest)
            status=msg if ok else "エントリー見送り: "+msg
        else:
            status=f"WAIT（LONG {sig['long_score']} / SHORT {sig['short_score']}、必要スコア {MIN_SIGNAL_SCORE}）"
        sig["entry_status"]=status
        dash=make_dashboard(s,price,funding,sig,status)
        write_json(STATE_FILE,s);write_json(DATA_FILE,dash)
        print(f"BUILD: {BUILD}\nPRICE: {price:.2f}\nSIGNAL: {sig['direction']} score={sig['score']} long={sig['long_score']} short={sig['short_score']}\nENTRY: {status}\nEQUITY: {s['equity']:.4f}\nCLOSED TRADES: {len(s['trades'])}\nPAPER ONLY: True\nLIVE TRADING ENABLED: False")
    except Exception as e:
        s["last_error"]=f"{type(e).__name__}: {e}"
        old=read_json(DATA_FILE,{})
        if isinstance(old,dict) and old:
            old.update({"build":BUILD,"generated_at":iso_now(),"market_status":"ERROR","last_error":s["last_error"]})
            write_json(DATA_FILE,old)
        write_json(STATE_FILE,s)
        print("ERROR:",s["last_error"])
        raise

if __name__=="__main__":main()
