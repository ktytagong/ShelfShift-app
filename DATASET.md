# Dataset notice

ShelfShift runs on public sales history from FreshRetailNet-50K, with simulated stock and labelled assumptions. This notice says where the data comes from, under what license, which parts are public, assumed or simulated, what was changed and what the data cannot tell you. The app's About page carries the same paragraphs; this file adds the details and the figures.

The app marks every page "Public sales dataset · Simulated operational scenario · Scenario date: Sat 17 Oct 2026". The scenario date is a fixed reference value (`calendar.data_as_of`); it never advances with the real calendar.

## Source and license

The sales history is FreshRetailNet-50K by Dingdong-Inc (Wang, Gu, Long, Li, Shen, Fu, Zhou, Jiang, 2025), https://huggingface.co/datasets/Dingdong-Inc/FreshRetailNet-50K, described in arXiv:2505.16319. © Dingdong-Inc, licensed under CC BY 4.0: https://creativecommons.org/licenses/by/4.0/. The dataset is provided as is, without warranties. ShelfShift changed it as listed under Modifications. ShelfShift is not affiliated with or endorsed by Dingdong-Inc.

- **Dataset:** FreshRetailNet-50K, https://huggingface.co/datasets/Dingdong-Inc/FreshRetailNet-50K (the files `train.parquet` and `eval.parquet`).
- **Creator:** Dingdong-Inc. Authors: Wang, Gu, Long, Li, Shen, Fu, Zhou, Jiang (2025). Paper: arXiv:2505.16319, https://arxiv.org/abs/2505.16319.
- **License:** Creative Commons Attribution 4.0 International, CC BY 4.0, https://creativecommons.org/licenses/by/4.0/.
- **Copyright notice:** © Dingdong-Inc, licensed under CC BY 4.0. The dataset card names no other copyright holder.
- **Disclaimer:** the dataset is provided as is, without warranties of any kind (the license's disclaimer of warranties, section 5).
- **Modified:** yes. Every change is listed under Modifications below.
- **No endorsement:** ShelfShift is not affiliated with or endorsed by Dingdong-Inc.

No dataset file is stored in this repository. The pipeline reads the two files from a git-ignored folder and checks each file's sha256 against the published ids before it reads a row (`dataset.files` in `reference/public.json`).

## Three kinds of data

Public: the daily sales, hours out of stock, discounts, holiday and activity flags, weather, and the city, store, product and category codes, all from FreshRetailNet-50K. Assumption: prices, costs, shelf life per category, truck runs within a city, storage, the waste price and the units factor, each labelled Assumption with its source. Simulated: stock on hand, lots, expiry dates and deliveries, built from the public sales for this scenario and never shown as observed.

| Kind | Covers | How it is labelled |
|---|---|---|
| **Public** | `sale_amount` (daily sales), `stock_hour6_22_cnt` (hours out of stock, 06:00 to 22:00), `discount`, `holiday_flag`, `activity_flag`, weather (precipitation, temperature, humidity, wind level), and the city, store, product and category codes | The source is named on About and here |
| **Assumption** | Prices, costs, shelf life per category, truck runs within a city, storage capacity, the waste price, the units factor, the assumed store positions and travel times | Labelled Assumption, with its source (the table below) |
| **Simulated** | Stock on hand, lots, expiry dates, deliveries, record errors, truck schedules | Labelled simulated; never shown as observed |

The word "synthetic" names generated data only: the app's small generated test set and its own assumptions table (`data/reference/assumptions.md`). The public sales are not synthetic, and no screen describes the whole dataset as observed retail data.

## Assumptions

**Units factor.** FreshRetailNet-50K publishes `sale_amount`, the daily sales after a global normalisation by one undisclosed coefficient, not a count of units. ShelfShift uses a stated round factor of 10: `units = sale_amount × units.factor`. The profile of the public files found no single step that the values sit on (only 32% of the positive values fit the best candidate step), so the coefficient cannot be recovered from the data, and the rule set before the profile ran picks a round factor. The factor is a configurable scenario assumption, never a fact of the public dataset.

**Category level.** The dataset's product hierarchy is management group, then first, second and third category, then product, all as encoded codes with no names. The assumptions sit at the management-group level (`mgmt`, 7 codes in the profile; every code covers at least 560 series). There is one default row and no overrides: the codes carry no names, so giving different values to different codes would have no basis. Every product therefore gets the same price, costs, shelf life (3 days, chilled), tote size and markdown schedule, each a configurable scenario assumption.

The engine policy (markdown schedules and response, the waste price, the transport rules) is the same set of assumptions as the generated test set, read from `reference/policy.json` (`policy.source` in `reference/public.json`); its own table is `data/reference/assumptions.md`.

The table below lists every leaf typed Assumption in `reference/public.json`, `reference/categories.json` and `reference/geography.json`, with its value, unit and source. It is generated by `.venv/bin/python -I tools/frn.py assumptions --md`; never edit it by hand (a test regenerates it and compares).

<!-- BEGIN ASSUMPTIONS -->
| Path | Value | Unit | Source |
|---|---|---|---|
| categories.default.delivery_cycle_days | 2 | days | 05.2-06 D-33 (user): a configurable scenario assumption; the synthetic STRAW-500 delivery cycle |
| categories.default.disposal_cost | 0.08 | USD | 05.2-06 D-33 (user): a configurable scenario assumption; the synthetic STRAW-500 disposal cost (spec §5 table) |
| categories.default.kg_per_unit | 0.5 | kg | 05.2-06 D-33 (user): a configurable scenario assumption; the synthetic STRAW-500 weight (spec §5 table) |
| categories.default.order_case | 8 | units per case | 05.2-06 D-33 (user): a configurable scenario assumption; the synthetic STRAW-500 order case |
| categories.default.price | 2.64 | USD | 05.2-06 D-33 (user): a configurable scenario assumption; the synthetic STRAW-500 price (spec §5 table) |
| categories.default.schedule | B |  | 05.2-06 D-33 (user): a configurable scenario assumption; markdown schedule B of reference/policy.json (20% at D-1, 30% at D), the 3-day products' schedule (03-CONTEXT D-20) |
| categories.default.shelf_life_days | 3 | days | 05.2-06 D-33 (user): a configurable scenario assumption; the 3-day chilled default |
| categories.default.units_per_tote | 8 | units per tote | 05.2-06 D-33 (user): a configurable scenario assumption; the synthetic STRAW-500 tote |
| categories.default.zone | chilled |  | 05.2-06 D-33 (user): a configurable scenario assumption; the 3-day chilled default, the synthetic STRAW-500 row |
| geography.capacity.ambient | 25 | free totes per store per day | 05.2-06 D-34 (user): capacity 25 ambient totes |
| geography.capacity.chilled | 20 | free totes per store per day | 05.2-06 D-34 (user): capacity 20 chilled totes |
| geography.costs.cancel | null | USD | 05.2-06 D-34 (user): cancel = the full quote |
| geography.costs.fixed_base | 12 | USD per run | 05.2-06 D-34 (user): $12 + $0.50/km |
| geography.costs.per_km | 0.5 | USD per km | 05.2-06 D-34 (user): $12 + $0.50/km |
| geography.costs.per_unit | 0 | USD per unit | 05.2-06 D-34 (user): $0 per unit |
| geography.coverage.unmapped | [] | raw store codes | 05.2-06 D-34 (user): every store mapped |
| geography.mapping.note | Assumed positions and travel times; the dataset has no locations; proximity is never inferred from codes; city codes limit candidates to one city because the dataset card defines city_id as an encoded city ID |  | 05.2-06 D-34 (user); 05.2-CONTEXT D-30: an assumed location and route mapping, clearly labelled Assumption with its source |
| geography.positions.city_radius_km | 15 | km | 05.2-06 D-34 (user): positions uniform in a 15 km disc per city |
| geography.positions.rule | each mapped store gets a point uniform in a disc of positions.city_radius_km around its city, drawn from a stream keyed by positions.seed, the city code and the store code; only distances inside one city are used |  | 05.2-06 D-34 (user): positions uniform in a 15 km disc per city; data/runs.py positions |
| geography.positions.seed | 52052 |  | 05.2-06 D-34 (user): the 05.2-05 measurement seed, kept so the D-34 measured figures reproduce |
| geography.runs.cold_chain_share | 0.667 | share of runs | 05.2-06 D-34 (user): cold-chain share 0.667 (4 of the 6 synthetic runs); a simulated truck schedule |
| geography.runs.days | [0, 1, 2, 3, 4, 5, 6] | day index | 05.2-06 D-34 (user): runs every day 0..6; a simulated truck schedule |
| geography.runs.departs_min | 750 | minutes after midnight | 05.2-06 D-34 (user): departure 12:30; a simulated truck schedule |
| geography.runs.loading_cutoff_min | 720 | minutes after midnight | 05.2-06 D-34 (user): cutoff 12:00; a simulated truck schedule |
| geography.runs.spare_totes | 15 | totes per run | 05.2-06 D-34 (user): 15 spare totes; a simulated truck schedule |
| geography.service.filters | ["same_city", "max_travel_min", "runs.k", "runs.days"] |  | 05.2-06 D-34 (user); 05.2-CONTEXT D-29 stage 1: the lot-independent eligibility; arrival before expiry, cold chain and receiving capacity are checked per lot at case time |
| geography.service.same_city | true |  | 05.2-06 D-34 (user); 05.2-CONTEXT D-30: the dataset card defines city_id as an encoded city ID, so it may limit candidates to the same city; spec §5: transfers stay within a city |
| geography.travel.handling_min | 15 | minutes per trip | 05.2-06 D-34 (user): plus 15 min handling |
| geography.travel.source | assumed_positions |  | 05.2-06 D-34 (user); 05.2-CONTEXT D-29, D-30: the travel-time source is a setting; distance and travel time inside a city come only from the assumed mapping |
| geography.travel.speed_kmh | 30 | km/h | 05.2-06 D-34 (user): 30 km/h |
| stock.error_from_day | -15 | day index | 05.2-06 D-39 (user): from day -15; the declared record-error model (reference/world.json error_model) |
| stock.error_range | [0.6, 0.95] | physical ÷ record, rounded down | 05.2-06 D-39 (user): range [0.6, 0.95]; the declared record-error model (reference/world.json error_model) |
| stock.error_share | 0.1 | share of regular lots | 05.2-06 D-39 (user): error model share 0.1; the declared record-error model (reference/world.json error_model) |
| stock.expiry_estimated_share | 0.0 | share of lots | 05.2-06 D-39 (user): expiry-estimated share 0.0; the synthetic world's natural lots are all PRINTED |
| stock.lookback_days | 28 | days | 05.2-06 D-39 (user); the synthetic ordering rule (reference/world.json ordering, Seed calibration 2) |
| stock.multiplier | [0.9, 1.0] | × the in-stock estimate | 05.2-06 D-39 (user): delivery multiplier 0.9-1.0 with the minimal whole-case top-up; the synthetic release range (reference/world.json ordering) |
| stock.same_weekday_obs | 4 | observations | 05.2-06 D-39 (user); the synthetic ordering rule (reference/world.json ordering, Seed calibration 2) |
| stock.seed | 52052 |  | the 05.2-05 measurement seed, kept so the D-39 measured figures reproduce |
| stock.topup_on_eval_days | false |  | 05.2-06 D-39 (user): eval-day top-ups are reported, not delivered to the worlds |
| truth.price_response.m20 | 1.0 | × demand at 20% off | 05.2-06 D-36 (user): an unmeasured no-lift assumption for the warm-up worlds |
| truth.price_response.m30 | 1.0 | × demand at 30% off | 05.2-06 D-36 (user): an unmeasured no-lift assumption for the warm-up worlds |
| units.factor | 10 | units per 1.0 of sale_amount | 05.2-06 D-33 (user): a configurable scenario assumption; rule A10: the profile shows no single lattice (share 0.32), so a stated round factor is used |
| units.rule | units = sale_amount × units.factor |  | 05.2-06 D-33 (user): a configurable scenario assumption; 05.2-CONTEXT D-20 and spec §5 Units: sale_amount is rescaled, not units |
<!-- END ASSUMPTIONS -->

## Modifications

Dates are shifted forward by a whole number of weeks, so every date keeps its weekday; the scenario date falls inside the shifted held-out week. Units are the public sale_amount times an assumed units factor. Only the day-level fields are used, not the hourly arrays. Codes carry a short display prefix. Rows whose discount is outside the known range are tagged unknown price and counted. Stock, lots, expiry and deliveries are simulated; they are not part of the dataset.

1. **Dates** (`calendar.*`): every date is shifted by +840 days (120 weeks), so each date keeps its weekday. The training days 2024-03-28 to 2024-06-25 become Thu 16 Jul to Tue 13 Oct 2026 (days −90 to −1); the evaluation week 2024-06-26 to 2024-07-02 becomes Wed 14 to Tue 20 Oct 2026 (days 0 to 6). The scenario date is Sat 17 Oct 2026 (day 3); days 0 to 2 run before the app opens.
2. **Units** (`units.factor`, `units.rule`): units = `sale_amount` × the units factor, an Assumption (see Assumptions).
3. **Fields**: day-level fields only; the hourly arrays (`hours_sale`, `hours_stock_status`) are not used. The day's count of out-of-stock hours is used.
4. **Codes** (`codes.*`): each code gets a display prefix: `C{}` for a city, `S{}` for a store, `P{}` for a product and `G{}` for a category at the management-group level. The prefix is reversible. No name is invented for a product, store or city.
5. **Unknown price** (`unknown_price.*`): a row whose discount is at or below 0.0 or above 1.0 has an unknown discount or markdown status. It is tagged unknown price, counted, and converted at the full base price with no response applied. "Unknown price" never means a missing amount: the money price is the category row's Assumption.
6. **Forecast input** (`stockout.*`, the markdown response of `reference/policy.json`): a day with any out-of-stock hour between 06:00 and 22:00 is filled from the mean of the in-stock days on the same weekday, never below that day's own recorded units. A discounted in-stock day is converted to a full-price equivalent by the assumed markdown response, interpolated by discount depth and capped at 30%. The result is labelled "estimated full-price-equivalent sales" and is never called true demand.
7. **Held-out week**: the public sales of the evaluation week are the held-out demand of days 0 to 6, and the public sales of the last training weeks score the forecast.
8. **Simulated operations** (`stock.*`, `geography.*`): the stock, lots, expiry and deliveries are simulated from the public sales; they are not part of the dataset. The simulated units sold equal the public units on every history day of every series. A share of lots carries a simulated record error, the truck schedules are simulated, and the store positions and travel times are assumed. No designed example is added (the reference data names none); if one is added later it is listed here and labelled as designed.

## Limits

The sales come from an online grocer's front warehouses in China in 2024, so the holiday flags follow that calendar. On a day with hours out of stock, the recorded sales understate demand. The stock is simulated, so a stock-out in this app is a simulated one, and it seldom falls on a day the public data records as out of stock. Store locations and routes are assumed, with travel times derived from them: the dataset has no locations. Prices and costs are assumptions, not the grocer's.

- **Where and when:** 898 stores in 18 cities, 865 products, 50,000 store and product series over 97 days of 2024. The stores are small delivery warehouses of one online grocer, not shops of any other retailer.
- **Holidays:** the holiday and activity flags follow that grocer's calendar in China, not the shifted dates' calendar.
- **Out-of-stock days:** 44.0% of the public series-days record at least one out-of-stock hour. On those days the recorded sales understate demand; the forecast input fills them by a stated rule and never calls the result true demand.
- **Stock is simulated:** the dataset has no stock, lots, expiry or deliveries. The simulation reproduces the public units sold but not the public out-of-stock hours (see What the figures are).
- **Locations and routes are assumed:** positions are drawn inside a disc per city, travel times come from those positions, and transfers stay within one city. 0 stores are unmapped; an unmapped store would be excluded from transfers, never given an invented route.
- **Prices and costs** are one default category row for every product, a configurable scenario assumption.

## What the figures are

Every number the app computes is simulated or estimated, and it is labelled so where it appears. The figures below are copied from the committed reports in `data/reference/reports/`, as reported and never tuned; a test checks each one against its report.

- **Sold equals public** (`public-consistency.json`): on all 50,000 series (4,850,000 series-days), the simulated units sold equal the public units every day.
- **Stock-out agreement** (`public-consistency.json`): the public data records 2,135,444 stock-out days and the simulation 93,022; they agree on 49,839 days, which is 2.3% of the public stock-out days and 53.6% of the simulated ones. The simulation's top-ups keep its shelves stocked, so it seldom runs out where the public data records out-of-stock hours.
- **Evaluation-week shortage** (`public-consistency.json`): on 36,203 of 350,000 evaluation series-days (10.3%), the regular deliveries alone would not cover the public units. A simulated result, never observed in FreshRetailNet-50K.
- **Warm-up shortage** (the phase's Wave 1 measurement, before the units factor was fixed): with no action taken on days 0 to 2 and the evaluation-week top-ups withheld, the simulated shelves fell short of the public units on 16.0% of the series-days at a units factor of 16.667, and on 11.1% at the adopted factor of 10. A simulated result, never observed in FreshRetailNet-50K.
- **Unknown price** (`public-consistency.json`): 17,373 rows.
- **Network** (`public-network.json`): 898 stores in 18 cities, 0 unmapped; positions and runs are assumed, not observed.
- **Forecast accuracy** (`public-backtest.json`): reported as "accuracy on public sales, in-stock days": scored against held-out public sales on in-stock days only, with stock-out days excluded and never filled. The committed backtest scores the released forecast (TimesFM 2.5 through BigQuery `AI.FORECAST`, with the per-series ARIMA_PLUS fallback) against the same-weekday-last-week baseline; its numbers are quoted only with that label and the providers it names.
- **Money:** every money figure rests on the assumed prices and costs. Waste is compared with doing nothing only.
