-- Forecast SQL run by data/bq.py on the user's machine (spec §5 Forecast; DATA-03; 03-CONTEXT D-07, D-08).
-- data/bq.py checks every project, dataset, table and model identifier against a regex before it fills one in;
-- otherwise only an int horizon, the configured confidence level and the configured TimesFM model name are filled.
-- Every series_id is "<run>|<branch>|<sku>", and each series stops at its run's cutoff, so one query forecasts
-- every run from its own last day. confidence_level is passed explicitly (both functions default to 0.95), and the
-- rows come back raw: data/forecast.py checks each one before anything else.

-- name: ai_forecast
SELECT series_id, forecast_timestamp, forecast_value,
       prediction_interval_lower_bound, prediction_interval_upper_bound,
       confidence_level, ai_forecast_status
FROM AI.FORECAST(
  TABLE `{table}`,
  data_col => 'value',
  timestamp_col => 'ts',
  model => '{model_name}',
  id_cols => ['series_id'],
  horizon => {horizon},
  confidence_level => {confidence_level});

-- name: arima_plus_model
CREATE OR REPLACE MODEL `{model}`
OPTIONS (model_type = 'ARIMA_PLUS', time_series_timestamp_col = 'ts', time_series_data_col = 'value',
         time_series_id_col = 'series_id', data_frequency = 'DAILY', horizon = {horizon})
AS SELECT series_id, ts, value FROM `{fallback_table}`;

-- name: arima_plus
SELECT series_id, forecast_timestamp, forecast_value,
       prediction_interval_lower_bound, prediction_interval_upper_bound, confidence_level
FROM ML.FORECAST(MODEL `{model}`, STRUCT({horizon} AS horizon, {confidence_level} AS confidence_level));
