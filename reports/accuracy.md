# TrustLane accuracy simulation

1500 trips (1356 honest, 144 thieves); 15 shoppers in the store at once (max 25), 16 aisles with shelf stations; 62.2% of shelf picks had more than one possible taker.
Shopper behaviour is assumed, not measured: replace the mix with pilot data.

| Thresholds (green / amber) | Honest green | Honest amber (one tap) | Honest red (staff stop) | Forgetful green | Overcharged | Theft caught | Theft loss |
|---|---|---|---|---|---|---|---|
| 25 / 60 (current) | 90.0% | 6.7% | 3.2% | 82.2% | 11 | 60.4% | Rs 20,929 |

*Caught* = the stolen item ended up paid, or the trip was stopped for a staff check. Forgetful shoppers who fixed a nudge in time still go green; the rest answer one amber prompt.

## Crowding

The shelf knows *what* was taken, and Bluetooth/aisle reports know *who was nearby*. When several shoppers stand at the same shelf, a pocketed item can't be pinned on one of them without tracking people on camera, which this design avoids. Catch rate therefore depends on how crowded the monitored shelves are:

| Aisles with shelf stations | Picks shared | Honest red | Theft caught |
|---|---|---|---|
| 4 | 91.5% | 2.8% | 31.9% |
| 8 | 80.2% | 3.4% | 42.4% |
| 16 | 62.2% | 3.2% | 60.4% |
