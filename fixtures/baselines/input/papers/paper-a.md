# Seasonal Naive Baselines in Retail Forecasting

We evaluate forecasting baselines on 400 weekly retail demand series.
Each baseline is scored with mean absolute scaled error.
The seasonal naive method repeats the value observed one season earlier.
Across all series, the seasonal naive baseline beat exponential smoothing on 71 percent of series.
We therefore recommend seasonal naive as the default baseline for weekly demand.
Limitation: all series in this study had at least two full years of history.
