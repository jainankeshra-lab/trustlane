# TrustLane load test

Simulated phones and shelf stations over HTTP and WebSockets against one server process (one store). Each shopper: start, 3-8 aisle reports, shelf takes and scans, cart, checkout, gate tap.

| Shoppers (arriving over) | Requests | Requests/s | p95, all calls | Scan p95 | Shelf event p95 | Gate tap p95 | Errors |
|---|---|---|---|---|---|---|---|
| 50 (20 s) | 858 | 28.6 | 92.2 ms | 89.1 ms | 74.7 ms | 71.4 ms | 0 |
| 100 (20 s) | 1758 | 41.2 | 2269.3 ms | 2382.9 ms | 1983.0 ms | 1716.1 ms | 0 |
| 200 (20 s) | 3607 | 38.1 | 10571.7 ms | 10816.0 ms | 10706.4 ms | 7899.1 ms | 0 |

The deck's alert threshold for shelf-to-server latency is a p95 of 2 s.
