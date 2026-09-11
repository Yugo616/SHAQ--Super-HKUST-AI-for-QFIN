"""Brokerless minute execution through genuine Zipline blotter and Ledger.

The only engine extension is an open-reference SlippageModel. A local sequential
driver calls Zipline's real order blotter and Ledger; it is deliberately not a
TradingAlgorithm/bundle integration. Inputs are unadjusted, bar-start-labelled.
"""
from dataclasses import asdict, dataclass, replace
from datetime import time
import hashlib
import json
import math
from typing import Mapping

import exchange_calendars
import pandas as pd
import zipline
from zipline.assets import Equity, ExchangeInfo
from zipline.finance.blotter.simulation_blotter import SimulationBlotter
from zipline.finance.commission import PerDollar
from zipline.finance.execution import MarketOrder
from zipline.finance.ledger import Ledger
from zipline.finance.slippage import SlippageModel
from zipline.finance.transaction import create_transaction


@dataclass(frozen=True)
class Rules:
    initial_cash: float = 10000.
    ticket_budget: float = 1000.
    commission: float = .0005
    slippage: float = .0005
    entry_time: time = time(9, 31)
    exit_minutes_before_close: int = 5
    calendar: str = "XNYS"


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: int


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class SessionInput:
    session: str
    signals: tuple[Signal, ...]
    bars: Mapping[tuple[str, pd.Timestamp], Bar]
    ticket_budgets: Mapping[str, float] | None = None


def execution_schedule(session, rules):
    if (not isinstance(rules.entry_time, time) or rules.entry_time.tzinfo is not None
            or rules.entry_time.second or rules.entry_time.microsecond):
        raise ValueError("entry_time must be an exchange-local whole-minute time")
    offset = rules.exit_minutes_before_close
    if (not isinstance(offset, (int, float)) or isinstance(offset, bool)
            or not math.isfinite(offset) or offset <= 0 or offset != int(offset)):
        raise ValueError("exit_minutes_before_close must be positive whole minutes")
    calendar = exchange_calendars.get_calendar(rules.calendar)
    opening = calendar.session_open(session)
    closing = calendar.session_close(session)
    entry = pd.Timestamp.combine(opening.tz_convert(calendar.tz).date(), rules.entry_time)
    entry = entry.tz_localize(calendar.tz).tz_convert("UTC")
    exit_ = closing - pd.Timedelta(minutes=rules.exit_minutes_before_close)
    if not opening <= entry < exit_ < closing:
        raise ValueError("Entry and exit must be ordered inside this exchange session")
    minutes = pd.date_range(opening, closing, freq="min", inclusive="left")
    if entry not in minutes or exit_ not in minutes:
        raise ValueError("Execution targets must belong to the session minute grid")
    return minutes, entry, exit_


class _ExecutionData:
    """Small in-memory data adapter, restricted to the current completed bar.

    `current_dt` is the bar-end label. No driver path exposes this object to the
    sizing decision. Missing bars remain absent; no forward-fill or daily data.
    """
    def __init__(self, bars, bar_start):
        self._bars = bars
        self.bar_start = bar_start
        self.current_dt = bar_start + pd.Timedelta(minutes=1)

    def current(self, asset, field):
        bar = self._bars.get((asset.symbol, self.bar_start))
        return math.nan if bar is None else getattr(bar, field)

    def get_scalar_asset_spot_value(self, asset, field, dt, data_frequency):
        if field != "price" or dt != self.current_dt or data_frequency != "minute":
            raise ValueError("Execution adapter only supplies this completed minute")
        return self.current(asset, "close")


class OpenReferenceSlippage(SlippageModel):
    """Market-only execution at known open, with adverse proportional slippage.

    Base SlippageModel.simulate explicitly reads close and checks triggers there.
    This narrow override avoids that close reference; Zipline still creates real
    transactions and the blotter owns fills/order state/commission generation.
    Positive volume is an eligibility gate, not a liquidity/impact model.
    """
    allowed_asset_types = (Equity,)

    def __init__(self, rate):
        super().__init__()
        self.rate = rate

    def process_order(self, data, order):
        opening = data.current(order.asset, "open")
        return opening * (1 + self.rate * order.direction), order.open_amount

    def simulate(self, data, asset, orders_for_asset):
        volume = data.current(asset, "volume")
        opening = data.current(asset, "open")
        if not math.isfinite(volume) or volume <= 0 or not math.isfinite(opening) or opening <= 0:
            return
        self._volume_for_bar = 0
        for order in orders_for_asset:
            if not order.open_amount:
                continue
            if order.stop is not None or order.limit is not None:
                raise ValueError("Execution accepts market orders only")
            order.check_triggers(opening, data.current_dt)
            price, amount = self.process_order(data, order)
            transaction = create_transaction(order, data.current_dt, price, amount)
            self._volume_for_bar += abs(transaction.amount)
            yield order, transaction


def _direction_hash(signals):
    return hashlib.sha256(json.dumps([asdict(s) for s in signals], sort_keys=True).encode()).hexdigest()


def _execute(fixture, rules, fixed_shares=None):
    minutes, entry, exit_ = execution_schedule(fixture.session, rules)
    for _, timestamp in fixture.bars:
        if (not isinstance(timestamp, pd.Timestamp) or timestamp.tzinfo is None
                or timestamp not in minutes):
            raise ValueError("Bar timestamps must be timezone-aware and on the session minute grid")
    signals = sorted(fixture.signals, key=lambda signal: signal.symbol)
    exchange = ExchangeInfo(rules.calendar, rules.calendar, "US")
    assets = {signal.symbol: Equity(index, symbol=signal.symbol, exchange_info=exchange)
              for index, signal in enumerate(signals, 1)}
    blotter = SimulationBlotter(equity_slippage=OpenReferenceSlippage(rules.slippage),
                                equity_commission=PerDollar(cost=rules.commission))
    ledger = Ledger(pd.DatetimeIndex([pd.Timestamp(fixture.session)]), rules.initial_cash, "minute")
    ledger.start_of_session(pd.Timestamp(fixture.session))
    budgets = min(rules.ticket_budget, rules.initial_cash / len(signals)) if signals else 0.
    quantities = {signal.symbol: 0 for signal in signals}
    collateral = 0.
    fills = []
    unfilled = []
    for minute in minutes:
        # Decision phase receives only current open; not this bar's later fields.
        blotter.set_date(minute)
        submitted = []
        if minute == entry:
            opens = {signal.symbol: (fixture.bars[signal.symbol, minute].open
                                     if (signal.symbol, minute) in fixture.bars else math.nan)
                     for signal in signals}
            for signal in signals:
                opening = opens[signal.symbol]
                if not math.isfinite(opening) or opening <= 0:
                    unfilled.append({"symbol": signal.symbol, "phase": "entry", "reason": "missing_or_invalid_open"})
                    continue
                # Same conservative buy-side reserve for both directions prevents
                # either input order or short-sale cash inflows financing tickets.
                reserve_per_share = opening * (1 + rules.slippage) * (1 + rules.commission)
                budget = (fixture.ticket_budgets or {}).get(signal.symbol, budgets)
                shares = fixed_shares[signal.symbol] if fixed_shares is not None else math.floor(budget / reserve_per_share)
                if shares:
                    order_id = blotter.order(assets[signal.symbol], shares * signal.direction,
                                             MarketOrder(), order_id=f"entry:{signal.symbol}")
                    submitted.append(order_id)
        elif minute == exit_:
            for symbol, asset in assets.items():
                position = ledger.position_tracker.positions.get(asset)
                if position is not None and position.amount:
                    submitted.append(blotter.order(asset, -position.amount, MarketOrder(),
                                                    order_id=f"exit:{symbol}"))
        for order_id in submitted:
            ledger.process_order(blotter.orders[order_id])
        # Completion phase: volume/close can now be read by execution/accounting,
        # never retroactively by sizing. Engine transactions get bar-end labels.
        data = _ExecutionData(fixture.bars, minute)
        transactions, commissions, closed = blotter.get_transactions(data)
        commission_by_order = {item["order"].id: item["cost"] for item in commissions}
        for transaction in transactions:
            ledger.process_transaction(transaction)
            if transaction.order_id.startswith('entry:'):
                quantities[transaction.asset.symbol] = abs(int(transaction.amount))
                collateral += abs(transaction.amount) * data.current(transaction.asset, 'open') * (1 + rules.slippage) * (1 + rules.commission)
            fills.append({"symbol": transaction.asset.symbol, "order_id": transaction.order_id,
                          "bar_start": minute.isoformat(), "engine_bar_end": transaction.dt.isoformat(),
                          "shares": int(transaction.amount), "price": float(transaction.price),
                          "reference_open": float(data.current(transaction.asset, "open")),
                          "commission": float(commission_by_order.get(transaction.order_id, 0.))})
        for commission in commissions:
            ledger.process_commission(commission)
        for order in closed:
            ledger.process_order(order)
        blotter.prune_orders(closed)
        # Orders are target-minute only: no accidental next-minute recovery fill.
        for order_id in submitted:
            if blotter.orders[order_id].open:
                unfilled.append({"symbol": blotter.orders[order_id].asset.symbol,
                                 "phase": "entry" if minute == entry else "exit",
                                 "reason": "missing_or_nonpositive_target_bar"})
                blotter.cancel(order_id)
                ledger.process_order(blotter.orders[order_id])
        ledger.sync_last_sale_prices(data.current_dt, data)
        ledger.end_of_bar(0)
    ledger.end_of_session(0)
    positions = {asset.symbol: int(position.amount) for asset, position in ledger.position_tracker.positions.items()
                 if position.amount}
    fees = math.fsum(fill["commission"] for fill in fills)
    slippage_cost = math.fsum(abs(fill["shares"]) * fill["reference_open"] * rules.slippage for fill in fills)
    # Independent cash equation uses input opens and cost parameters, not the
    # ledger's computed prices/cash changes. Also check each engine fill price.
    reference_cash_flow = -math.fsum(fill["shares"] * fill["reference_open"] for fill in fills)
    expected_fees = math.fsum(abs(fill["shares"]) * fill["reference_open"] *
                             (1 + math.copysign(rules.slippage, fill["shares"])) * rules.commission
                             for fill in fills)
    expected_cash = rules.initial_cash + reference_cash_flow - slippage_cost - expected_fees
    cash = float(ledger.portfolio.cash)
    reconciled = (math.isclose(cash, expected_cash, abs_tol=1e-8, rel_tol=1e-12) and
                  math.isclose(fees, expected_fees, abs_tol=1e-8, rel_tol=1e-12) and
                  all(math.isclose(fill["price"], fill["reference_open"] *
                                   (1 + math.copysign(rules.slippage, fill["shares"])),
                                   abs_tol=1e-10) for fill in fills))
    return {"shares": quantities, "fills": fills, "unfilled": unfilled,
            "reserved_collateral": collateral, "positions": positions,
            "settled": not positions, "final_cash": cash,
            "engine_portfolio_value": float(ledger.portfolio.portfolio_value),
            "gross_pnl": reference_cash_flow if not positions else None,
            "commissions": fees, "slippage_cost": slippage_cost,
            "net_pnl": cash - rules.initial_cash if not positions else None,
            "reconciled": reconciled, "mathematical_expected_cash": expected_cash,
            "minutes_processed": len(minutes)}


def run_session(fixture, rules=Rules()):
    """Run actual Zipline orders/accounting and a same-share zero-cost engine run."""
    if zipline.__version__ != "3.1.1":
        raise RuntimeError("Minute execution requires zipline-reloaded==3.1.1")
    for name in ("initial_cash", "ticket_budget", "commission", "slippage"):
        value = getattr(rules, name)
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and nonnegative")
    if rules.slippage >= 1:
        raise ValueError("slippage must be below one")
    symbols = [signal.symbol for signal in fixture.signals]
    if len(set(symbols)) != len(symbols) or any(signal.direction not in (-1, 1) for signal in fixture.signals):
        raise ValueError("Signals require unique symbols and direction +1 or -1")
    if fixture.ticket_budgets is not None:
        if set(fixture.ticket_budgets) != set(symbols) or any(
            isinstance(value, bool) or not math.isfinite(value) or value < 0
            for value in fixture.ticket_budgets.values()
        ):
            raise ValueError("Ticket budgets require one finite nonnegative value per signal")
    before = _direction_hash(fixture.signals)
    result = _execute(fixture, rules)
    zero = _execute(fixture, replace(rules, commission=0., slippage=0.), fixed_shares=result["shares"])
    result.update({"engine": {"name": "zipline-reloaded", "version": zipline.__version__,
                              "blotter": "zipline.finance.blotter.simulation_blotter.SimulationBlotter",
                              "ledger": "zipline.finance.ledger.Ledger",
                              "scope": "component integration; not TradingAlgorithm/bundle"},
                   "evidence": "unadjusted RTH minute execution; separate from official prediction labels",
                   "session": fixture.session, "directions_sha256": before,
                   "directions_unchanged": before == _direction_hash(fixture.signals),
                   "zero_cost": zero})
    return result
