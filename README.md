# Pump.fun top-five forecast

This entry reads the task settings and snapshot history, learns venue-level cap movement and downside frequency, and writes deterministic place and size-bin forecasts.

To run it from the repository root:

```sh
mkdir -p out
MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
```

It uses Python's standard library only. The output path is selected by the filename in `task.json`, usually `forecast.json`.
