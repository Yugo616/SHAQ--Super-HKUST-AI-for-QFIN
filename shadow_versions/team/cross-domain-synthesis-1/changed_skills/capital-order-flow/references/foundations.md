# Foundations

- Cont, Kukanov and Stoikov, “The Price Impact of Order Book Events,” Journal of Financial Econometrics 2014. Short-horizon price changes relate to order-flow imbalance scaled by market depth: <https://doi.org/10.1093/jjfinec/nbt003>
- Futu OpenAPI real-time ticker: `Session.ALL` is required when US premarket ticker observations are requested: <https://openapi.futunn.com/futu-api-doc/quote/get-ticker.html>

System rule: require signed event-level flow and liquidity context. Vendor size buckets are observations, not evidence of informed buying or selling.
