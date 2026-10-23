#!/usr/bin/env python3
"""BTC Paper Engine V17.1: public market data only; no live orders."""
import json, math, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
BUILD = 'BTC_PAPER_V17_1_2026-10-23'
API = 'https://api.hyperliquid.xyz/info'
COIN = 'BTC'
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / 'state.json'
DATA_FILE = ROOT / 'docs' / 'data.json'
INITIAL_EQUITY = 100.0
LEVERAGE = 40.0
RISK_PER_TRADE = 0.0075
MAX_MARGIN_FRACTION = 0.35
DAILY_LOSS_LIMIT = 0.03
MAX_DRAWDOWN_LIMIT = 0.1
MAX_CONSECUTIVE_LOSSES = 4
COOLDOWN_SECONDS = 300
MAX_HOLD_SECONDS = 86400
MIN_SIGNAL_SCORE = 6
MIN_SCORE_EDGE = 2
FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002
ATR_STOP_MULTIPLIER = 1.8
ATR_TARGET_MULTIPLIER = 3.0
MIN_REWARD_RISK = 1.5
TRAIL_ACTIVATION_R = 1.5
ATR_TRAIL_MULTIPLIER = 1.5
CANDLE_LIMIT = 200
HTTP_TIMEOUT = 20
MAX_TRADES_STORED = 1000
MAX_HISTORY_STORED = 1000
BB_PERIOD = 20
BB_STDDEV = 2.0
FIB_LOOKBACK = 80

def now_ts():
    return int(time.time())

def iso_now():
    return datetime.now(timezone.utc).isoformat()

def utc_date():
    return str(datetime.now(timezone.utc).date())

def safe_float(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except (TypeError, ValueError, OverflowError):
        return float(default)

def safe_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return int(default)

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
    tmp.replace(path)

def read_json(path, default):
    try:
        with path.open('r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default

def post(payload):
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'User-Agent': 'BTC-Paper-Engine-V17.1/1.0'}, method='POST')
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode())

def normalize_timestamp(v):
    ts = safe_int(v)
    if ts > 100000000000:
        ts //= 1000
    return 0 if ts < 0 or ts > now_ts() + 60 else ts

def default_state():
    return {'build': BUILD, 'initial_equity': INITIAL_EQUITY, 'equity': INITIAL_EQUITY, 'peak_equity': INITIAL_EQUITY, 'realized_pnl': 0.0, 'funding_total': 0.0, 'trades': [], 'equity_history': [], 'position': None, 'last_close_ts': 0, 'last_candle_ts': 0, 'consecutive_losses': 0, 'consecutive_loss_lock': False, 'daily_date': utc_date(), 'daily_start_equity': INITIAL_EQUITY, 'daily_loss_lock': False, 'drawdown_lock': False, 'last_error': None}

def load_state():
    s = read_json(STATE_FILE, {})
    if not isinstance(s, dict) or not s:
        return default_state()
    d = default_state()
    for k, v in d.items():
        s.setdefault(k, v)
    s['initial_equity'] = safe_float(s.get('initial_equity'), INITIAL_EQUITY)
    s['equity'] = safe_float(s.get('equity'), s['initial_equity'])
    s['peak_equity'] = max(safe_float(s.get('peak_equity'), s['equity']), s['equity'])
    s['realized_pnl'] = safe_float(s.get('realized_pnl'))
    s['funding_total'] = safe_float(s.get('funding_total'))
    tr = s.get('trades', s.get('paper_trades', []))
    s['trades'] = tr if isinstance(tr, list) else []
    h = s.get('equity_history', [])
    s['equity_history'] = h if isinstance(h, list) else []
    p = s.get('position') or s.get('open_position')
    s['position'] = p if isinstance(p, dict) else None
    for k in ('last_close_ts', 'last_candle_ts'):
        s[k] = normalize_timestamp(s.get(k))
    s['consecutive_losses'] = max(0, safe_int(s.get('consecutive_losses')))
    for k in ('consecutive_loss_lock', 'daily_loss_lock', 'drawdown_lock'):
        s[k] = bool(s.get(k))
    s['daily_start_equity'] = safe_float(s.get('daily_start_equity'), s['equity'])
    s['build'] = BUILD
    return s

def fetch_price_and_funding():
    mids = post({'type': 'allMids'})
    price = safe_float(mids.get(COIN)) if isinstance(mids, dict) else 0
    if price <= 0:
        raise RuntimeError('Could not retrieve BTC price')
    funding = 0.0
    try:
        r = post({'type': 'metaAndAssetCtxs'})
        if isinstance(r, list) and len(r) >= 2:
            for i, a in enumerate(r[0].get('universe', [])):
                if a.get('name') == COIN and i < len(r[1]):
                    funding = safe_float(r[1][i].get('funding'))
                    break
    except Exception as e:
        print(f'Funding unavailable; using zero: {e}')
    return (price, funding)

def fetch_candles(interval, limit=CANDLE_LIMIT):
    mins = {'15m': 15, '1h': 60, '4h': 240}
    ms = mins[interval] * 60 * 1000
    end = int(time.time() * 1000)
    start = end - ms * (limit + 5)
    raw = post({'type': 'candleSnapshot', 'req': {'coin': COIN, 'interval': interval, 'startTime': start, 'endTime': end}})
    if not isinstance(raw, list):
        raise RuntimeError(f'Invalid {interval} candle response')
    cs = []
    for x in raw:
        c = {'t': safe_int(x.get('t')), 'T': safe_int(x.get('T')), 'o': safe_float(x.get('o')), 'h': safe_float(x.get('h')), 'l': safe_float(x.get('l')), 'c': safe_float(x.get('c')), 'v': safe_float(x.get('v')), 'n': safe_int(x.get('n'))}
        if c['t'] > 0 and c['c'] > 0 and (c['h'] >= c['l'] > 0) and (c['h'] > 0):
            cs.append(c)
    cs.sort(key=lambda x: x['t'])
    now = int(time.time() * 1000)
    cs = [c for c in cs if c['t'] + ms <= now]
    if len(cs) < 35:
        raise RuntimeError(f'Not enough closed candles for {interval}')
    return cs[-limit:]

def ema(vals, period):
    if not vals:
        return []
    a = 2 / (period + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out

def calc_rsi(vals, period=14):
    if len(vals) < period + 1:
        return 50.0
    ch = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    gains = [max(x, 0) for x in ch]
    losses = [max(-x, 0) for x in ch]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(ch)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al <= 0:
        return 100.0 if ag > 0 else 50.0
    return 100 - 100 / (1 + ag / al)

def calc_atr(cs, period=14):
    if len(cs) < period + 1:
        return max(cs[-1]['c'] * 0.002, 1.0)
    tr = []
    for i in range(1, len(cs)):
        c = cs[i]
        pc = cs[i - 1]['c']
        tr.append(max(c['h'] - c['l'], abs(c['h'] - pc), abs(c['l'] - pc)))
    v = sum(tr[:period]) / period
    for x in tr[period:]:
        v = (v * (period - 1) + x) / period
    return max(v, 0.01)

def bollinger(vals, period=BB_PERIOD, mult=BB_STDDEV):
    if len(vals) < period:
        return {'upper': None, 'middle': None, 'lower': None, 'width_pct': None, 'percent_b': None, 'zscore': None}
    win = vals[-period:]
    mid = sum(win) / period
    variance = sum(((x - mid) ** 2 for x in win)) / period
    sd = math.sqrt(max(variance, 0))
    upper = mid + mult * sd
    lower = mid - mult * sd
    width = (upper - lower) / mid * 100 if mid else 0
    last = vals[-1]
    pb = (last - lower) / (upper - lower) if upper > lower else 0.5
    return {'upper': upper, 'middle': mid, 'lower': lower, 'width_pct': width, 'percent_b': pb, 'zscore': (last - mid) / sd if sd else 0.0}

def calc_fibonacci(cs, lookback=FIB_LOOKBACK):
    window = cs[-min(lookback, len(cs)):]
    hi = max(window, key=lambda c: c['h'])
    lo = min(window, key=lambda c: c['l'])
    high = hi['h']
    low = lo['l']
    if high <= low:
        return {'swing_high': high, 'swing_low': low, 'trend': 'UNKNOWN', 'levels': {}, 'nearest_level': None, 'distance_pct': None}
    if hi['t'] > lo['t']:
        trend_dir = 'UP'
        levels = {'0.236': high - (high - low) * 0.236, '0.382': high - (high - low) * 0.382, '0.500': high - (high - low) * 0.5, '0.618': high - (high - low) * 0.618, '0.786': high - (high - low) * 0.786}
    else:
        trend_dir = 'DOWN'
        levels = {'0.236': low + (high - low) * 0.236, '0.382': low + (high - low) * 0.382, '0.500': low + (high - low) * 0.5, '0.618': low + (high - low) * 0.618, '0.786': low + (high - low) * 0.786}
    price = cs[-1]['c']
    name, val = min(levels.items(), key=lambda kv: abs(price - kv[1]))
    return {'swing_high': high, 'swing_low': low, 'trend': trend_dir, 'levels': levels, 'nearest_level': name, 'nearest_price': val, 'distance_pct': abs(price - val) / price * 100 if price else None}

def trend(cs):
    vals = [c['c'] for c in cs]
    f = ema(vals, 9)[-1]
    s = ema(vals, 21)[-1]
    if f > s * 1.0005:
        return 'UP'
    if f < s * 0.9995:
        return 'DOWN'
    return 'SIDEWAYS'

def make_signal(c15, c1h, c4h, funding):
    vals = [c['c'] for c in c15]
    e9 = ema(vals, 9)
    e21 = ema(vals, 21)
    rsi = calc_rsi(vals)
    atr = calc_atr(c15)
    bb = bollinger(vals)
    fib = calc_fibonacci(c1h)
    t15, t1h, t4h = (trend(c15), trend(c1h), trend(c4h))
    long = short = 0
    reasons = []
    flags = []
    if e9[-1] > e21[-1]:
        long += 2
        reasons.append('15m EMA bullish')
    elif e9[-1] < e21[-1]:
        short += 2
        reasons.append('15m EMA bearish')
    for tf, d, w in (('15m', t15, 2), ('1h', t1h, 2), ('4h', t4h, 2)):
        if d == 'UP':
            long += w
            reasons.append(f'{tf} uptrend')
        elif d == 'DOWN':
            short += w
            reasons.append(f'{tf} downtrend')
    if 45 <= rsi <= 67:
        long += 1
    if 33 <= rsi <= 55:
        short += 1
    if rsi > 75:
        long -= 2
        flags.append('RSI overbought')
    if rsi < 25:
        short -= 2
        flags.append('RSI oversold')
    if len(vals) >= 5:
        mom = vals[-1] / vals[-5] - 1
        if mom > 0.001:
            long += 1
            reasons.append('15m positive momentum')
        elif mom < -0.001:
            short += 1
            reasons.append('15m negative momentum')
    if funding > 0.0003:
        short += 1
        reasons.append('positive funding')
    elif funding < -0.0003:
        long += 1
        reasons.append('negative funding')
    if bb['percent_b'] is not None and len(vals) >= 3:
        if bb['percent_b'] < 0.08 and vals[-1] > vals[-2] and (rsi < 45):
            long += 1
            reasons.append('BB lower-band recovery')
        elif bb['percent_b'] > 0.92 and vals[-1] < vals[-2] and (rsi > 55):
            short += 1
            reasons.append('BB upper-band rejection')
        if bb['width_pct'] is not None and bb['width_pct'] < 0.35:
            flags.append('BB squeeze: breakout unconfirmed')
    fib_near = fib.get('distance_pct') is not None and fib['distance_pct'] <= 0.2
    if fib_near:
        if fib['trend'] == 'UP' and t1h == 'UP' and (rsi >= 40):
            long += 1
            reasons.append(f"Fib support area {fib['nearest_level']}")
        elif fib['trend'] == 'DOWN' and t1h == 'DOWN' and (rsi <= 60):
            short += 1
            reasons.append(f"Fib resistance area {fib['nearest_level']}")
    direction = 'WAIT'
    edge = abs(long - short)
    if long >= MIN_SIGNAL_SCORE and long - short >= MIN_SCORE_EDGE:
        direction = 'LONG'
    elif short >= MIN_SIGNAL_SCORE and short - long >= MIN_SCORE_EDGE:
        direction = 'SHORT'
    return {'direction': direction, 'score': max(long, short), 'long_score': long, 'short_score': short, 'score_edge': edge, 'rsi': round(rsi, 4), 'atr': round(atr, 6), 'ema9': round(e9[-1], 6), 'ema21': round(e21[-1], 6), 'trend_15m': t15, 'trend_1h': t1h, 'trend_4h': t4h, 'bollinger': {'period': BB_PERIOD, 'stddev': BB_STDDEV, **{k: round(v, 6) if isinstance(v, (int, float)) else v for k, v in bb.items()}}, 'fibonacci': {'lookback_1h_candles': FIB_LOOKBACK, **fib}, 'timestamp': iso_now(), 'reference_price': vals[-1], 'flags': flags, 'reasons': reasons or ['No clear directional advantage']}

def unrealized(p, price):
    if not p:
        return 0.0
    q = safe_float(p.get('quantity'))
    e = safe_float(p.get('entry_price'))
    return (price - e) * q if p.get('side') == 'LONG' else (e - price) * q

def drawdown(s, equity=None):
    v = safe_float(s.get('equity')) if equity is None else safe_float(equity)
    peak = max(safe_float(s.get('peak_equity')), v, 0.01)
    return max(0, (peak - v) / peak)

def refresh_locks(s, marked=None):
    today = utc_date()
    if s.get('daily_date') != today:
        s['daily_date'] = today
        s['daily_start_equity'] = safe_float(s.get('equity'))
        s['daily_loss_lock'] = False
    eq = safe_float(s.get('equity'))
    marked = eq if marked is None else safe_float(marked, eq)
    start = max(safe_float(s.get('daily_start_equity'), eq), 0.01)
    daily = max(0, (start - marked) / start)
    dd = drawdown(s, marked)
    if daily >= DAILY_LOSS_LIMIT:
        s['daily_loss_lock'] = True
    if dd >= MAX_DRAWDOWN_LIMIT:
        s['drawdown_lock'] = True
    if safe_int(s.get('consecutive_losses')) >= MAX_CONSECUTIVE_LOSSES:
        s['consecutive_loss_lock'] = True
    return {'daily_loss_pct': round(daily * 100, 4), 'daily_loss_lock': bool(s.get('daily_loss_lock')), 'drawdown_pct': round(dd * 100, 4), 'drawdown_lock': bool(s.get('drawdown_lock')), 'consecutive_losses': safe_int(s.get('consecutive_losses')), 'consecutive_loss_lock': bool(s.get('consecutive_loss_lock')), 'risk_per_trade_pct': RISK_PER_TRADE * 100, 'max_margin_pct': MAX_MARGIN_FRACTION * 100, 'leverage': LEVERAGE, 'paper_only': True, 'live_trading_enabled': False}

def size_position(s, entry, stop):
    eq = safe_float(s.get('equity'))
    dist = abs(entry - stop)
    if eq <= 0 or entry <= 0 or dist <= 0:
        return (None, 'Invalid sizing inputs')
    budget = eq * RISK_PER_TRADE
    cost = dist + entry * (2 * FEE_RATE + 2 * SLIPPAGE_RATE)
    if cost <= 0:
        return (None, 'Invalid estimated cost')
    qty = min(budget / cost, eq * MAX_MARGIN_FRACTION * LEVERAGE / entry)
    if qty <= 0:
        return (None, 'Position quantity is zero')
    notional = qty * entry
    margin = notional / LEVERAGE
    planned = qty * cost
    if planned > budget * 1.000001:
        return (None, 'Risk budget check failed')
    if margin > eq * MAX_MARGIN_FRACTION * 1.000001:
        return (None, 'Maximum margin check failed')
    return ({'quantity': qty, 'notional': notional, 'margin_used': margin, 'risk_budget': budget, 'planned_risk': planned}, None)

def open_position(s, side, price, atr, signal):
    slip = price * SLIPPAGE_RATE
    entry = price + slip if side == 'LONG' else price - slip
    dist = max(atr * ATR_STOP_MULTIPLIER, price * 0.001)
    target_dist = max(atr * ATR_TARGET_MULTIPLIER, dist * MIN_REWARD_RISK)
    stop = entry - dist if side == 'LONG' else entry + dist
    target = entry + target_dist if side == 'LONG' else entry - target_dist
    sizing, err = size_position(s, entry, stop)
    if err:
        return (False, err)
    fee = sizing['notional'] * FEE_RATE
    eq = safe_float(s.get('equity'))
    if fee >= eq:
        return (False, 'Entry fee exceeds available equity')
    s['equity'] = eq - fee
    now = now_ts()
    s['position'] = {'side': side, 'entry_price': entry, 'quantity': sizing['quantity'], 'leverage': LEVERAGE, 'margin_used': sizing['margin_used'], 'notional': sizing['notional'], 'risk_distance': dist, 'risk_budget': sizing['risk_budget'], 'planned_risk': sizing['planned_risk'], 'stop_price': stop, 'target_price': target, 'initial_stop_price': stop, 'initial_target_price': target, 'opened_timestamp': now, 'entry_time': iso_now(), 'entry_fee': fee, 'funding_paid': 0.0, 'last_funding_timestamp': now, 'runner_mode': False, 'entry_signal_score': signal.get('score', 0), 'entry_rsi': signal.get('rsi', 50), 'entry_signal': signal.get('direction'), 'entry_bollinger': signal.get('bollinger', {}), 'entry_fibonacci': signal.get('fibonacci', {}), 'build': BUILD}
    s['peak_equity'] = max(safe_float(s.get('peak_equity')), safe_float(s.get('equity')))
    return (True, f'{side} opened')

def close_position(s, price, reason):
    p = s.get('position')
    if not p:
        return False
    side = p.get('side')
    qty = safe_float(p.get('quantity'))
    entry = safe_float(p.get('entry_price'))
    if qty <= 0 or entry <= 0 or price <= 0:
        s['last_error'] = 'Invalid position values at close'
        return False
    exitp = price * (1 - SLIPPAGE_RATE) if side == 'LONG' else price * (1 + SLIPPAGE_RATE)
    gross = (exitp - entry) * qty if side == 'LONG' else (entry - exitp) * qty
    entryfee = safe_float(p.get('entry_fee'))
    exitfee = abs(exitp * qty) * FEE_RATE
    funding = safe_float(p.get('funding_paid'))
    net = gross - entryfee - exitfee + funding
    s['equity'] = safe_float(s.get('equity')) + gross - exitfee
    s['realized_pnl'] = safe_float(s.get('realized_pnl')) + net
    margin = max(safe_float(p.get('margin_used')), 0.01)
    trade = {'side': side, 'entry_price': entry, 'exit_price': exitp, 'quantity': qty, 'leverage': safe_float(p.get('leverage'), LEVERAGE), 'margin_used': margin, 'notional': safe_float(p.get('notional')), 'opened_timestamp': safe_int(p.get('opened_timestamp')), 'entry_time': p.get('entry_time'), 'exit_timestamp': now_ts(), 'exit_time': iso_now(), 'gross_pnl': gross, 'entry_fee': entryfee, 'exit_fee': exitfee, 'fees': entryfee + exitfee, 'funding_paid': funding, 'net_pnl': net, 'net_return_pct': net / margin * 100, 'reason': reason, 'planned_risk': safe_float(p.get('planned_risk')), 'entry_signal_score': safe_int(p.get('entry_signal_score')), 'entry_rsi': safe_float(p.get('entry_rsi'), 50), 'entry_bollinger': p.get('entry_bollinger', {}), 'entry_fibonacci': p.get('entry_fibonacci', {}), 'build': BUILD}
    s.setdefault('trades', []).append(trade)
    s['trades'] = s['trades'][-MAX_TRADES_STORED:]
    s['consecutive_losses'] = safe_int(s.get('consecutive_losses')) + 1 if net < 0 else 0 if net > 0 else safe_int(s.get('consecutive_losses'))
    if s['consecutive_losses'] >= MAX_CONSECUTIVE_LOSSES:
        s['consecutive_loss_lock'] = True
    s['last_close_ts'] = now_ts()
    s['position'] = None
    s['peak_equity'] = max(safe_float(s.get('peak_equity')), safe_float(s.get('equity')))
    return True

def apply_funding(s, funding, price):
    p = s.get('position')
    if not p:
        return
    now = now_ts()
    last = normalize_timestamp(p.get('last_funding_timestamp', now))
    elapsed = max(0, min((now - last) / 3600, 24))
    if elapsed <= 0:
        p['last_funding_timestamp'] = now
        return
    notional = abs(safe_float(p.get('quantity')) * price)
    amount = notional * funding * elapsed / 8
    if p.get('side') == 'SHORT':
        amount *= -1
    p['funding_paid'] = safe_float(p.get('funding_paid')) - amount
    s['funding_total'] = safe_float(s.get('funding_total')) - amount
    s['equity'] = safe_float(s.get('equity')) - amount
    p['last_funding_timestamp'] = now

def manage_position(s, cs, price, funding, atr):
    p = s.get('position')
    if not p:
        return
    apply_funding(s, funding, price)
    p = s.get('position')
    side = p.get('side')
    entry = safe_float(p.get('entry_price'))
    stop = safe_float(p.get('stop_price'))
    target = safe_float(p.get('target_price'))
    rd = max(safe_float(p.get('risk_distance')), 0.01)
    cursor = safe_int(s.get('last_candle_ts'))
    if cursor <= 0:
        cursor = cs[-1]['t']
    for c in cs:
        if c['t'] <= cursor:
            continue
        hi, lo = (c['h'], c['l'])
        if side == 'LONG':
            if lo <= stop:
                close_position(s, stop, 'STOP_LOSS')
                return
            if hi >= target:
                close_position(s, target, 'TAKE_PROFIT')
                return
            favorable = hi - entry
        else:
            if hi >= stop:
                close_position(s, stop, 'STOP_LOSS')
                return
            if lo <= target:
                close_position(s, target, 'TAKE_PROFIT')
                return
            favorable = entry - lo
        if favorable >= rd * TRAIL_ACTIVATION_R:
            p['runner_mode'] = True
            trail = max(atr * ATR_TRAIL_MULTIPLIER, 0.01)
            if side == 'LONG':
                p['stop_price'] = max(stop, entry, hi - trail)
            else:
                p['stop_price'] = min(stop, entry, lo + trail)
            stop = safe_float(p.get('stop_price'))
    opened = normalize_timestamp(p.get('opened_timestamp'))
    if opened and now_ts() - opened >= MAX_HOLD_SECONDS:
        close_position(s, price, 'MAX_HOLD_TIME')

def metrics(s):
    ts = [t for t in s.get('trades', []) if isinstance(t, dict) and 'net_pnl' in t]
    wins = [t for t in ts if safe_float(t.get('net_pnl')) > 0]
    losses = [t for t in ts if safe_float(t.get('net_pnl')) < 0]
    total = sum((safe_float(t.get('net_pnl')) for t in ts))
    ws = sum((safe_float(t.get('net_pnl')) for t in wins))
    ls = abs(sum((safe_float(t.get('net_pnl')) for t in losses)))
    today = utc_date()
    td = [t for t in ts if str(t.get('exit_time', '')).startswith(today)]
    return {'closed_trades': len(ts), 'wins': len(wins), 'losses': len(losses), 'win_rate': len(wins) / len(ts) * 100 if ts else 0, 'total_net_pnl': total, 'average_trade': total / len(ts) if ts else 0, 'average_win': ws / len(wins) if wins else 0, 'average_loss': -ls / len(losses) if losses else 0, 'profit_factor': ws / ls if ls else 999 if ws else 0, 'expectancy_per_trade': total / len(ts) if ts else 0, 'best_trade': max((safe_float(t.get('net_pnl')) for t in ts), default=0), 'worst_trade': min((safe_float(t.get('net_pnl')) for t in ts), default=0), 'trades_today_utc': len(td), 'wins_today_utc': sum((safe_float(t.get('net_pnl')) > 0 for t in td)), 'losses_today_utc': sum((safe_float(t.get('net_pnl')) < 0 for t in td))}

def make_dashboard(s, price, funding, signal, market_status='OK'):
    p = s.get('position')
    un = unrealized(p, price) if p else 0
    marked = safe_float(s.get('equity')) + un
    initial = max(safe_float(s.get('initial_equity')), 0.01)
    s['peak_equity'] = max(safe_float(s.get('peak_equity')), marked)
    risk = refresh_locks(s, marked)
    stats = metrics(s)
    hist = s.setdefault('equity_history', [])
    hist.append({'timestamp': iso_now(), 'equity': safe_float(s.get('equity')), 'marked_equity': marked, 'price': price})
    s['equity_history'] = hist[-MAX_HISTORY_STORED:]
    pv = dict(p) if p else None
    if pv:
        pv['unrealized_pnl'] = un
    return {'build': BUILD, 'generated_at': iso_now(), 'market_status': market_status, 'price': price, 'funding_rate': funding, 'funding_rate_pct': funding * 100, 'initial_equity': initial, 'equity': safe_float(s.get('equity')), 'marked_equity': marked, 'realized_pnl': safe_float(s.get('realized_pnl')), 'unrealized_pnl': un, 'net_pnl': marked - initial, 'net_pnl_pct': (marked - initial) / initial * 100, 'position': pv, 'open_position': pv, 'has_position': bool(p), 'signal': signal, 'risk': risk, 'current_drawdown_pct': risk['drawdown_pct'], 'max_drawdown_pct': risk['drawdown_pct'], 'funding_total': safe_float(s.get('funding_total')), 'metrics': stats, 'closed_trades': stats['closed_trades'], 'win_rate': stats['win_rate'], 'trades': s.get('trades', [])[-200:], 'paper_trades': s.get('trades', [])[-200:], 'equity_history': s.get('equity_history', [])[-300:], 'timeframes': {'15m': signal.get('trend_15m'), '1h': signal.get('trend_1h'), '4h': signal.get('trend_4h'), 'rsi': signal.get('rsi'), 'atr': signal.get('atr'), 'ema9': signal.get('ema9'), 'ema21': signal.get('ema21'), 'bollinger': signal.get('bollinger'), 'fibonacci': signal.get('fibonacci')}, 'config': {'risk_per_trade_pct': RISK_PER_TRADE * 100, 'max_margin_pct': MAX_MARGIN_FRACTION * 100, 'leverage': LEVERAGE, 'daily_loss_limit_pct': DAILY_LOSS_LIMIT * 100, 'max_drawdown_limit_pct': MAX_DRAWDOWN_LIMIT * 100, 'max_consecutive_losses': MAX_CONSECUTIVE_LOSSES, 'cooldown_seconds': COOLDOWN_SECONDS, 'min_signal_score': MIN_SIGNAL_SCORE, 'min_score_edge': MIN_SCORE_EDGE, 'fee_rate_per_side': FEE_RATE, 'slippage_rate_per_side': SLIPPAGE_RATE, 'bollinger_period': BB_PERIOD, 'bollinger_stddev': BB_STDDEV, 'fibonacci_lookback_1h': FIB_LOOKBACK, 'paper_only': True, 'live_trading_enabled': False}, 'last_error': s.get('last_error')}

def main():
    s = load_state()
    s['last_error'] = None
    try:
        price, funding = fetch_price_and_funding()
        c15 = fetch_candles('15m')
        c1h = fetch_candles('1h')
        c4h = fetch_candles('4h')
        signal = make_signal(c15, c1h, c4h, funding)
        atr = max(safe_float(signal.get('atr'), price * 0.002), price * 0.0005)
        if s.get('position'):
            manage_position(s, c15, price, funding, atr)
        s['last_candle_ts'] = max(normalize_timestamp(s.get('last_candle_ts')), safe_int(c15[-1]['t']))
        current_un = unrealized(s.get('position'), price) if s.get('position') else 0
        marked = safe_float(s.get('equity')) + current_un
        locks = refresh_locks(s, marked)
        last = normalize_timestamp(s.get('last_close_ts'))
        now = now_ts()
        cooldown = max(0, COOLDOWN_SECONDS - (now - last)) if 0 < last <= now else 0
        if s.get('position'):
            status = 'Position already open'
        elif locks['daily_loss_lock']:
            status = 'Daily loss lock active'
        elif locks['drawdown_lock']:
            status = 'Drawdown lock active'
        elif locks['consecutive_loss_lock']:
            status = 'Consecutive-loss lock active'
        elif cooldown > 0:
            status = f'Cooldown: {cooldown}s remaining'
        elif signal['direction'] in ('LONG', 'SHORT'):
            ok, msg = open_position(s, signal['direction'], price, atr, signal)
            status = msg if ok else f'Entry skipped: {msg}'
        else:
            status = 'No entry: signal is WAIT'
        signal['entry_status'] = status
        signal['cooldown_remaining_seconds'] = cooldown
        data = make_dashboard(s, price, funding, signal)
        write_json(STATE_FILE, s)
        write_json(DATA_FILE, data)
        print(f"BUILD: {BUILD}\nPRICE: {price:.2f}\nSIGNAL: {signal['direction']} / {signal['score']} (long={signal['long_score']}, short={signal['short_score']})\nENTRY STATUS: {status}\nEQUITY: {s['equity']:.6f}\nMARKED EQUITY: {data['marked_equity']:.6f}\nCLOSED TRADES: {data['closed_trades']}\nWIN RATE: {data['win_rate']:.2f}%\nEXPECTANCY: {data['metrics']['expectancy_per_trade']:.6f}\nPROFIT FACTOR: {data['metrics']['profit_factor']:.4f}\nPAPER ONLY: True\nLIVE TRADING ENABLED: False")
    except Exception as e:
        s['last_error'] = f'{type(e).__name__}: {e}'
        s['build'] = BUILD
        try:
            old = read_json(DATA_FILE, {})
            if isinstance(old, dict) and old:
                old.update({'build': BUILD, 'generated_at': iso_now(), 'market_status': 'ERROR', 'last_error': s['last_error']})
                write_json(DATA_FILE, old)
        except Exception:
            pass
        write_json(STATE_FILE, s)
        print(f"ERROR: {s['last_error']}")
        raise
if __name__ == '__main__':
    main()
