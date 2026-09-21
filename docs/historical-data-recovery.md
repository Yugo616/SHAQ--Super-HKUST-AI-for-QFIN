# Historical data recovery

Daily history is cached by market-data source, adjustment semantics and interval.
Changing a candidate limit, an intraday interval or a request timeout does not
discard the daily history. Each refresh still downloads the configured overlap
to detect corrections. A failed or empty refresh never presents stale rows as
new observations.

Successful Yahoo requests are checkpointed individually. If the surrounding
worker fails, verified successes are promoted into the incremental cache before
the original error is raised. HTTP 429 stops the remaining ticker requests in
that group after bounded retries. This is not proof of upstream availability.

Legacy daily caches without a verifiable coverage interval are not silently
migrated. They remain on disk; a fresh acquisition creates the checked cache.
Existing frozen research evidence and account records are never rewritten.

## Optional historical backup adapter

`history_fallback.py` contains an Alpaca SIP historical adapter and a
collection-scoped failover wrapper. These are **not enabled in the desktop
settings or automatic workflow**. Account authorization and live feed-validation
are required before integration. No API account is created and no subscription
is purchased by the application.

The adapter requests raw prices, follows every response page, retains original
response bytes and checksums, and keeps minute-start timestamps. Missing minutes
stay missing. It refuses current-session requests, IEX substitution, options,
redirected credential requests and malformed responses. It is not an official
auction-price verifier and does not change label or settlement providers.

The wrapper can recover completed Yahoo daily requests and request only the
missing symbols from the backup after a transport failure. Authentication and
schema errors do not trigger provider switching. Premarket and option calls
remain on their original provider; delayed historical data is never presented
as a current premarket snapshot.

## References and decisions

- [yfinance project](https://github.com/ranaroussi/yfinance): third-party access to
  Yahoo public interfaces; recovery cannot guarantee the upstream service.
- [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars):
  explicit `raw`, `sip`, inclusive API end, total-page limit and continuation
  tokens. The adapter converts this to the application's exclusive-end contract.
- [Alpaca data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq): IEX and SIP
  are different coverage, and free historical SIP excludes the most recent
  15 minutes. Entitlement must be checked against the actual account.
- [Alpaca minute construction](https://alpaca.markets/learn/stock-minute-bars):
  minute timestamps identify the start of the interval; do not substitute a
  neighbouring minute when the target is absent.

Contract tests are independent of live account validation. They cover pagination,
raw feed requests, timestamps, missing bars, malformed prices, credential-safe
errors, partial recovery and non-historical isolation.
