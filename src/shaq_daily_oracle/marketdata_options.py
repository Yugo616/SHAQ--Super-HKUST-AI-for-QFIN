"""Free historical option snapshots, never relabelled as live quotes.

https://www.marketdata.app/docs/account/data-freshness/
https://www.marketdata.app/docs/api/options/chain/
"""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math
import time
import httpx

from .market_calendar import previous_market_session, market_session
from .alpaca_bars import SYMBOL

ET = ZoneInfo('America/New_York')


def free_snapshot_session(cutoff):
    previous = previous_market_session(cutoff.date())
    today = market_session(cutoff.date())
    # Options only roll to Historical at the NEXT session open + one second.
    if today and cutoff >= today.market_open + timedelta(seconds=1):
        return previous
    return previous_market_session(previous.session_date)


def collect_historical_chain(*, symbol, token, cutoff, timeout_seconds,
                             maximum_strikes, maximum_expiries, client=None, clock=None,
                             research_replay=False):
    if (not SYMBOL.fullmatch(symbol) or not token.strip() or cutoff.tzinfo is None
            or maximum_strikes < 1 or maximum_expiries < 1):
        raise ValueError('请填写有效的 Market Data Token 和股票代码。')
    cutoff = cutoff.astimezone(ET)
    snapshot = free_snapshot_session(cutoff)
    endpoint = 'https://api.marketdata.app/v1/options/chain/' + symbol + '/'
    params = {'date':snapshot.session_date.isoformat(),'expiration':'all','strikeLimit':maximum_strikes}
    clock = clock or (lambda: datetime.now(ET))
    with nullcontext(client) if client is not None else httpx.Client(timeout=timeout_seconds) as session:
        for attempt in range(2):
            try:
                response = session.get(endpoint, params=params, headers={'Authorization':'Bearer '+token},
                                       timeout=timeout_seconds, follow_redirects=False)
            except httpx.RequestError as exc:
                if attempt:
                    raise ValueError('期权历史报价连接失败，请稍后重试。') from exc
            else:
                if response.status_code in (200, 203):
                    break
                if not (response.status_code == 429 or 500 <= response.status_code < 600) or attempt:
                    reason = {401:'Token 未通过验证',403:'当前账号没有此报价权限',
                              429:'免费请求次数暂时用完'}.get(response.status_code,'服务未返回合格报价')
                    raise ValueError('Market Data：'+reason+'。')
            time.sleep(.25)
    captured = clock().astimezone(ET)
    eligible = captured <= cutoff
    if not eligible and not (research_replay and captured.date() == cutoff.date()):
        raise ValueError('期权报价采集晚于证据截止时间。')
    try:
        body = response.json()
        if body.get('s') == 'no_data':
            return {'symbol':symbol,'status':'no_data','raw':response.content,'source_uri':endpoint,
                    'captured_at':captured.isoformat(),'snapshot_session':snapshot.session_date.isoformat(),
                    'formal_cutoff_eligible':eligible,'post_cutoff_replay':not eligible}
        if body.get('s') != 'ok':
            raise ValueError('invalid response status')
        fields = ('optionSymbol','underlying','side','strike','expiration','bid','ask',
                  'updated','underlyingPrice','volume','openInterest')
        count = len(body['optionSymbol'])
        if not count or any(not isinstance(body[f],list) or len(body[f]) != count for f in fields):
            raise ValueError('incomplete arrays')
        contracts = []
        for index in range(count):
            row = {f:body[f][index] for f in fields}
            if row['underlying'] != symbol or row['side'] not in ('call','put'):
                raise ValueError('wrong contract identity')
            stamp = datetime.fromtimestamp(row['updated'],timezone.utc).astimezone(ET)
            expiry = datetime.fromtimestamp(row['expiration'],timezone.utc).astimezone(ET).date()
            if stamp.date() != snapshot.session_date or stamp > cutoff:
                raise ValueError('wrong snapshot timestamp')
            if expiry < cutoff.date():
                continue
            bid, ask = row['bid'], row['ask']
            # Retain only actual two-sided quotes, not last-trade substitutes.
            if bid is None or ask is None or bid == 0 or ask == 0:
                continue
            values = [float(v) for v in (bid,ask,row['strike'],row['underlyingPrice'])]
            if not all(math.isfinite(v) and v > 0 for v in values) or values[0] > values[1]:
                raise ValueError('invalid quote')
            for field in ('volume','openInterest'):
                v=row[field]
                if v is not None and (not isinstance(v,(float,int)) or not math.isfinite(v) or v < 0 or int(v) != v):
                    raise ValueError('invalid activity')
            contracts.append(dict(contract=row['optionSymbol'],side=row['side'],strike=values[2],
                expiration=expiry.isoformat(),bid=values[0],ask=values[1],underlying_price=values[3],
                volume=row['volume'],open_interest=row['openInterest'],quote_as_of=stamp.isoformat(),
                open_interest_as_of='before_open_on_'+snapshot.session_date.isoformat(),
                implied_volatility=None))
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        raise ValueError('期权历史报价字段或时间不合格，未使用。') from exc
    expiries = sorted({c['expiration'] for c in contracts})[:maximum_expiries]
    contracts = [c for c in contracts if c['expiration'] in expiries]
    straddles = []
    for expiry in expiries:
        pairs = {}
        for c in contracts:
            if c['expiration'] == expiry:
                pairs.setdefault((c['strike'],c['quote_as_of'],c['underlying_price']),{})[c['side']] = c
        matched = [(key,pair) for key,pair in pairs.items() if set(pair)=={'call','put'}]
        if matched:
            key,pair = min(matched,key=lambda item:abs(item[0][0]-item[0][2]))
            premium = sum((c['bid']+c['ask'])/2 for c in pair.values())
            straddles.append(dict(expiration=expiry,strike=key[0],quote_as_of=key[1],underlying_price=key[2],
                premium_mid=premium,premium_fraction=premium/key[2],
                interpretation='Historical ATM straddle premium, not a probability or forecast confidence interval.'))
    return dict(symbol=symbol,status='collected' if contracts else 'no_data',source_uri=endpoint,
        captured_at=captured.isoformat(),snapshot_session=snapshot.session_date.isoformat(),
        formal_cutoff_eligible=eligible,post_cutoff_replay=not eligible,
        quote_timestamp_status='available',evidence_scope='historical_option_pricing_context',
        current_quote=False,directional_flow_semantics=False,contracts=contracts,atm_straddles=straddles,
        source_contract_count=count,raw=response.content,
        interpretation_boundary='Free historical snapshot, not premarket/live pricing. Do not combine with a newer '
            'underlying to infer current skew or implied move. OI is from before snapshot-day open; '
            'no native aggressor, open/close classification or historical IV supplied.')
