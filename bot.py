#!/usr/bin/env python3
"""BTC Paper Engine V6 — paper trading only; no wallet or live orders."""
import json, math, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.hyperliquid.xyz/info"
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DASH_FILE = ROOT / "docs" / "data.json"
BUILD = "BTC_PAPER_V6_2026-10-21"

INITIAL_EQUITY = 100.0
LEVERAGE = 30.0                 # maximum virtual leverage; not a target
RISK_PER_TRADE = 0.005           # 0.5% of equity
MAX_MARGIN_FRACTION = 0.10       # at most 10% of equity as margin
MAX_DAILY_LOSS = 0.03
MAX_DRAWDOWN = 0.10
FEE_PER_SIDE = 0.0005
SLIPPAGE_PER_SIDE = 0.0002
MIN_NOTIONAL = 1.0
MAX_HOLD_HOURS = 24
MAX_TRADES = 500
MAX_HISTORY = 2000
MAX_ARCHIVE = 2000

def now():
    return datetime.now(timezone.utc)

def iso_now():
    return now().isoformat()

def now_ms():
    return int(time.time() * 1000)

def finite(x):
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False

def api(payload):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "BTC-Paper-Research/6.0"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())

def candles(interval, hours):
    end = now_ms()
    raw = api({"type":"candleSnapshot","req":{"coin":"BTC","interval":interval,
        "startTime":end-hours*3600000,"endTime":end}})
    if not isinstance(raw, list):
        raise RuntimeError("Invalid candle response")
    out = []
    for x in raw:
        try:
            c = {k: float(x[k]) for k in ("o","h","l","c","v")}
            c["t"], c["T"] = int(x["t"]), int(x["T"])
            if (all(math.isfinite(c[k]) and c[k] > 0 for k in ("o","h","l","c"))
                and math.isfinite(c["v"]) and c["v"] >= 0
                and c["h"] >= max(c["o"],c["c"],c["l"])
                and c["l"] <= min(c["o"],c["c"],c["h"])
                and c["T"] < now_ms()):
                out.append(c)
        except (KeyError, TypeError, ValueError):
            continue
    return sorted({c["t"]:c for c in out}.values(), key=lambda z:z["t"])

def ema(values, period):
    if len(values) < period: return None
    v = sum(values[:period])/period
    a = 2/(period+1)
    for x in values[period:]: v = a*x+(1-a)*v
    return v

def rsi(values, period=14):
    if len(values) < period+1: return None
    d = [values[i]-values[i-1] for i in range(1,len(values))]
    gains, losses = [max(x,0) for x in d], [max(-x,0) for x in d]
    ag, al = sum(gains[:period])/period, sum(losses[:period])/period
    for i in range(period,len(d)):
        ag = (ag*(period-1)+gains[i])/period
        al = (al*(period-1)+losses[i])/period
    if al == 0: return 100.0 if ag else 50.0
    return 100-100/(1+ag/al)

def atr(cs, period=14):
    if len(cs) < period+1: return None
    ranges=[]
    for i in range(1,len(cs)):
        cur, prev=cs[i],cs[i-1]
        ranges.append(max(cur["h"]-cur["l"],abs(cur["h"]-prev["c"]),abs(cur["l"]-prev["c"])))
    v=sum(ranges[:period])/period
    for x in ranges[period:]: v=(v*(period-1)+x)/period
    return v

def fib_levels(cs, lookback=96):
    chunk=cs[-lookback:]
    if len(chunk)<10: return {}
    hi,lo=max(x["h"] for x in chunk),min(x["l"] for x in chunk)
    span=hi-lo
    if span<=0: return {}
    return {"swing_high":round(hi,2),"swing_low":round(lo,2),
        "retracement_382":round(hi-span*.382,2),"retracement_500":round(hi-span*.5,2),
        "retracement_618":round(hi-span*.618,2)}

def find_fvg(cs, price):
    gaps=[]
    for i in range(2,len(cs)):
        a,c=cs[i-2],cs[i]
        if c["l"]>a["h"]:
            gaps.append({"type":"bullish","low":round(a["h"],2),"high":round(c["l"],2),"formed_at":c["t"]})
        if c["h"]<a["l"]:
            gaps.append({"type":"bearish","low":round(c["h"],2),"high":round(a["l"],2),"formed_at":c["t"]})
    active=[]
    for g in gaps[-30:]:
        if (g["type"]=="bullish" and price>=g["low"]) or (g["type"]=="bearish" and price<=g["high"]):
            active.append(g)
    return active[-6:]

def default_state():
    return {"build":BUILD,"created_at":iso_now(),"account_started_at":iso_now(),
        "cash":INITIAL_EQUITY,"peak_equity":INITIAL_EQUITY,"trades":[],"archived_trades":[],
        "open_trade":None,"daily":{"date":now().date().isoformat(),"start_equity":INITIAL_EQUITY},
        "last_signal_key":None,"halt_reason":None,"equity_history":[],"archived_equity_history":[]}

def load_state():
    if not STATE_FILE.exists(): return default_state()
    try: s=json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e: raise RuntimeError("state.json could not be read; refusing reset") from e
    if not isinstance(s,dict): raise RuntimeError("Invalid state.json")
    if s.get("open_trade") and s.get("build") != BUILD:
        raise RuntimeError("Open position detected during version change; review state.json before upgrading")
    for k,default in (("trades",[]),("archived_trades",[]),("equity_history",[]),("archived_equity_history",[])):
        s.setdefault(k,default)
    if s.get("build") != BUILD:
        if s.get("open_trade"): raise RuntimeError("Cannot reset account with open position")
        s["archived_trades"]=(s.get("archived_trades",[])+s.get("trades",[]))[-MAX_ARCHIVE:]
        s["archived_equity_history"]=(s.get("archived_equity_history",[])+s.get("equity_history",[]))[-MAX_ARCHIVE:]
        s.update({"cash":INITIAL_EQUITY,"peak_equity":INITIAL_EQUITY,"trades":[],"open_trade":None,
            "equity_history":[],"daily":{"date":now().date().isoformat(),"start_equity":INITIAL_EQUITY},
            "last_signal_key":None,"halt_reason":None,"account_started_at":iso_now()})
    s.setdefault("cash",INITIAL_EQUITY); s.setdefault("peak_equity",float(s["cash"]))
    s.setdefault("daily",{"date":now().date().isoformat(),"start_equity":float(s["cash"])})
    s.setdefault("last_signal_key",None); s.setdefault("halt_reason",None)
    if not all(isinstance(s[k],list) for k in ("trades","archived_trades","equity_history","archived_equity_history")):
        raise RuntimeError("Invalid state lists")
    s["build"]=BUILD
    return s

def save_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(data,indent=2,ensure_ascii=False,allow_nan=False),encoding="utf-8")
    tmp.replace(path)

def market_data():
    r=api({"type":"metaAndAssetCtxs"})
    if not isinstance(r,list) or len(r)<2: raise RuntimeError("Invalid market response")
    meta,ctxs=r[0],r[1]
    idx=next((i for i,a in enumerate(meta.get("universe",[])) if a.get("name")=="BTC"),None)
    if idx is None or idx>=len(ctxs): raise RuntimeError("BTC market unavailable")
    ctx=ctxs[idx]
    c15,c1,c4=candles("15m",240),candles("1h",1080),candles("4h",2880)
    if min(len(c15),len(c1),len(c4))<60: raise RuntimeError("Insufficient candle history")
    price=float(ctx.get("markPx") or c15[-1]["c"])
    vals={"funding":float(ctx.get("funding") or 0),"open_interest_btc":float(ctx.get("openInterest") or 0),
          "premium":float(ctx.get("premium") or 0),"volume_24h_usd":float(ctx.get("dayNtlVlm") or 0)}
    if not finite(price) or price<=0 or not all(finite(v) for v in vals.values()): raise RuntimeError("Invalid market data")
    return {"price":price,**vals,"candles_15m":c15,"candles_1h":c1,"candles_4h":c4}

def make_signal(m):
    c15,c1,c4=m["candles_15m"],m["candles_1h"],m["candles_4h"]
    p15,p1,p4=[x["c"] for x in c15],[x["c"] for x in c1],[x["c"] for x in c4]
    e20_1,e50_1,e20_4,e50_4=ema(p1,20),ema(p1,50),ema(p4,20),ema(p4,50)
    rv,av=rsi(p15),atr(c15); price=m["price"]
    sig={"direction":"WAIT","score":0,"reasons":[],"price":price,"rsi_15m":rv,"atr_15m":av,
        "ema20_1h":e20_1,"ema50_1h":e50_1,"ema20_4h":e20_4,"ema50_4h":e50_4,
        "fib":fib_levels(c1),"fvgs":find_fvg(c15,price)}
    if any(x is None or not finite(x) for x in (e20_1,e50_1,e20_4,e50_4,rv,av)) or av<=0:
        sig["reasons"]=["指標不足のため見送り"]; return sig
    t1=1 if e20_1>e50_1 else -1; t4=1 if e20_4>e50_4 else -1
    score=t1+t4; reasons=[
        "1時間足EMA20 > EMA50" if t1>0 else "1時間足EMA20 < EMA50",
        "4時間足EMA20 > EMA50" if t4>0 else "4時間足EMA20 < EMA50"]
    if price>e20_1: score+=1; reasons.append("価格は1時間足EMA20より上")
    else: score-=1; reasons.append("価格は1時間足EMA20より下")
    if 50<=rv<=67: score+=1; reasons.append(f"RSI {rv:.1f}: 上向き候補")
    elif 33<=rv<50: score-=1; reasons.append(f"RSI {rv:.1f}: 弱め")
    else: reasons.append(f"RSI {rv:.1f}: 中立条件外")
    if m["funding"]>.0003: score-=1; reasons.append("資金調達率は高め")
    elif m["funding"]<-.0003: score+=1; reasons.append("資金調達率は低め")
    else: reasons.append("資金調達率に極端な偏りなし")
    move=abs(p15[-1]-p15[-2])/p15[-2]
    direction="WAIT"
    if t1==t4==1 and score>=3:
        if 50<=rv<=68 and move<=.012: direction="LONG"
        else: reasons.append("LONG条件不一致")
    elif t1==t4==-1 and score<=-3:
        if 32<=rv<=50 and move<=.012: direction="SHORT"
        else: reasons.append("SHORT条件不一致")
    else: reasons.append("上位足の方向またはスコアが不一致")
    sig.update({"direction":direction,"score":score,"reasons":reasons,
        "candle_move_pct":round(move*100,4),"signal_candle_time":c15[-1]["t"]})
    return sig

def unrealized(t,price):
    sign=1 if t["direction"]=="LONG" else -1
    return float(t["notional"])*sign*(price-float(t["entry_price"]))/float(t["entry_price"])

def equity(s,price):
    t=s.get("open_trade")
    return float(s["cash"]) if not t else float(s["cash"])+float(t["margin"])+unrealized(t,price)

def fill_price(price,direction,entering):
    sign=1 if direction=="LONG" else -1
    return price*(1+(sign if entering else -sign)*SLIPPAGE_PER_SIDE)

def close_trade(s,price,reason):
    t=s.get("open_trade")
    if not t: return
    exitp=fill_price(price,t["direction"],False)
    gross=unrealized(t,exitp)
    fees=float(t["notional"])*FEE_PER_SIDE*2
    net=gross-fees
    s["cash"]=max(0.0,float(s["cash"])+float(t["margin"])+net)
    t.update({"mark_exit_price":price,"exit_price":round(exitp,2),"closed_at":iso_now(),
        "close_reason":reason,"gross_pnl_usd":round(gross,6),"fees_est_usd":round(fees,6),
        "net_pnl_usd":round(net,6),"return_on_margin_pct":round(net/max(float(t["margin"]),1e-9)*100,3)})
    s["trades"]=(s["trades"]+[t])[-MAX_TRADES:]
    s["open_trade"]=None

def halt_check(s,eq):
    peak=max(float(s["peak_equity"]),eq)
    dd=(peak-eq)/max(peak,1e-9)
    daily=float(s["daily"]["start_equity"])
    loss=(daily-eq)/max(daily,1e-9)
    if eq<=0: return "EQUITY_DEPLETED"
    if dd>=MAX_DRAWDOWN: return "MAX_DRAWDOWN_10_PERCENT"
    if loss>=MAX_DAILY_LOSS: return "DAILY_LOSS_3_PERCENT"
    return None

def calc_metrics(trades):
    pnls=[float(t.get("net_pnl_usd",0)) for t in trades if finite(t.get("net_pnl_usd",0))]
    wins=[x for x in pnls if x>0]; losses=[x for x in pnls if x<0]
    gp,gl=sum(wins),abs(sum(losses)); pf=gp/gl if gl else (None if gp==0 else 999.0); n=len(pnls)
    return {"closed_trades":n,"wins":len(wins),"losses":len(losses),"breakeven_trades":sum(x==0 for x in pnls),
        "win_rate_pct":round(len(wins)/n*100,2) if n else 0.0,"net_closed_pnl_usd":round(sum(pnls),6),
        "gross_profit_usd":round(gp,6),"gross_loss_usd":round(gl,6),"profit_factor":round(pf,4) if pf is not None else None,
        "average_win_usd":round(gp/len(wins),6) if wins else 0.0,
        "average_loss_usd":round(sum(losses)/len(losses),6) if losses else 0.0,
        "best_trade_usd":round(max(pnls),6) if pnls else 0.0,"worst_trade_usd":round(min(pnls),6) if pnls else 0.0,
        "expectancy_usd":round(sum(pnls)/n,6) if n else 0.0}

def run():
    s=load_state(); m=market_data(); sig=make_signal(m); price=m["price"]
    today=now().date().isoformat()
    if s["daily"].get("date")!=today:
        s["daily"]={"date":today,"start_equity":equity(s,price)}
        if s.get("halt_reason")=="DAILY_LOSS_3_PERCENT": s["halt_reason"]=None
    # Mark equity before managing the position; stops are evaluated on live mark price.
    eq=equity(s,price); s["peak_equity"]=max(float(s["peak_equity"]),eq)
    why=halt_check(s,eq)
    if why: s["halt_reason"]=why
    t=s.get("open_trade")
    if t:
        sign=1 if t["direction"]=="LONG" else -1
        move=sign*(price-float(t["entry_price"]))/float(t["entry_price"])
        if move<=-.08: close_trade(s,price,"EMERGENCY_LOSS_GUARD")
        elif (sign==1 and price<=float(t["stop_price"])) or (sign==-1 and price>=float(t["stop_price"])):
            close_trade(s,price,"STOP_LOSS")
        elif (sign==1 and price>=float(t["target_price"])) or (sign==-1 and price<=float(t["target_price"])):
            close_trade(s,price,"TAKE_PROFIT")
        elif now_ms()-int(t.get("opened_at_ms",now_ms()))>=MAX_HOLD_HOURS*3600000:
            close_trade(s,price,"MAX_HOLD_24H")
    eq=equity(s,price); s["peak_equity"]=max(float(s["peak_equity"]),eq)
    why=halt_check(s,eq)
    if why: s["halt_reason"]=why
    if not s.get("open_trade") and not s.get("halt_reason") and sig["direction"] in ("LONG","SHORT"):
        key=f'{sig["direction"]}:{sig.get("signal_candle_time",0)}'
        if s.get("last_signal_key")!=key:
            av=float(sig.get("atr_15m") or 0)
            stop_dist=max(1.5*av/price,.005)
            target_dist=stop_dist*1.8
            risk_usd=max(0,eq*RISK_PER_TRADE)
            notional=min(risk_usd/stop_dist,max(0,eq*MAX_MARGIN_FRACTION)*LEVERAGE)
            margin=notional/LEVERAGE
            if not finite(notional) or notional<MIN_NOTIONAL or margin>float(s["cash"]) or stop_dist>=.08:
                sig["reasons"].append("資金・サイズ制限で見送り"); sig["direction"]="WAIT"
            else:
                direction=sig["direction"]; sign=1 if direction=="LONG" else -1
                entry=fill_price(price,direction,True)
                s["cash"]=float(s["cash"])-margin
                s["open_trade"]={"id":key,"direction":direction,"mark_entry_price":price,
                    "entry_price":round(entry,2),"opened_at":iso_now(),"opened_at_ms":now_ms(),
                    "notional":round(notional,6),"margin":round(margin,6),"leverage_virtual":LEVERAGE,
                    "stop_price":round(entry*(1-sign*stop_dist),2),
                    "target_price":round(entry*(1+sign*target_dist),2),
                    "stop_distance_pct":round(stop_dist*100,3),"signal_score":sig["score"],"entry_reasons":sig["reasons"]}
                s["last_signal_key"]=key
    eq=equity(s,price); s["peak_equity"]=max(float(s["peak_equity"]),eq)
    s["last_run_at"]=iso_now(); s["last_price"]=price; s["last_signal"]=sig
    s["equity_history"]=(s.setdefault("equity_history",[])+[{"time":s["last_run_at"],"equity_usd":round(eq,6),"price":price}])[-MAX_HISTORY:]
    metrics=calc_metrics(s["trades"])
    metrics.update({"equity_usd":round(eq,6),"cash_usd":round(float(s["cash"]),6),
        "peak_equity_usd":round(float(s["peak_equity"]),6),
        "drawdown_pct":round((float(s["peak_equity"])-eq)/max(float(s["peak_equity"]),1e-9)*100,3),
        "open_unrealized_pnl_usd":round(unrealized(s["open_trade"],price),6) if s.get("open_trade") else 0.0})
    s["metrics"]=metrics
    dash={"build":BUILD,"generated_at":iso_now(),"mode":"PAPER ONLY — LIVE ORDERS DISABLED",
        "exchange_data":"Hyperliquid public API","symbol":"BTC perpetual","price":price,
        "funding_rate":m["funding"],"funding_rate_pct":m["funding"]*100,
        "open_interest_btc":m["open_interest_btc"],"premium":m["premium"],"volume_24h_usd":m["volume_24h_usd"],
        "signal":sig,"metrics":metrics,"risk":{"virtual_leverage":LEVERAGE,"risk_per_trade_pct":RISK_PER_TRADE*100,
        "max_margin_pct":MAX_MARGIN_FRACTION*100,"max_daily_loss_pct":MAX_DAILY_LOSS*100,
        "max_drawdown_pct":MAX_DRAWDOWN*100,"halt_reason":s.get("halt_reason"),
        "warning":"Paper simulation only. Fees, execution and liquidation are simplified; no live trading."},
        "open_trade":s.get("open_trade"),"recent_trades":s.get("archived_trades",[])[-5:]+s["trades"][-15:],
        "equity_history":s["equity_history"][-200:],"fib":sig.get("fib",{}),"fvgs":sig.get("fvgs",[]),
        "macro_note":"経済指標は自動取得していません。CPI、雇用統計、FOMCは別途確認してください。",
        "method_notes":["確定済みローソク足を使用","Wilder RSI・ATR","1h・4h EMAと15mモメンタム",
        "上位足の方向が一致しない場合は見送り","仮想手数料とスリッページを概算",
        "初期資金100 USD、仮想レバレッジ上限30倍","実注文・ウォレット接続なし"]}
    save_json(STATE_FILE,s); save_json(DASH_FILE,dash)
    print(json.dumps({"ok":True,"build":BUILD,"price":price,"signal":sig["direction"],"score":sig["score"],
        "equity":metrics["equity_usd"],"closed_trades":metrics["closed_trades"],
        "profit_factor":metrics["profit_factor"],"virtual_leverage":LEVERAGE,"halt":s.get("halt_reason")},ensure_ascii=False))

if __name__=="__main__":
    try: run()
    except Exception as e:
        print(json.dumps({"ok":False,"build":BUILD,"error":str(e),"time":iso_now()},ensure_ascii=False))
        raise
