# Native performance sweep harness

This directory is an isolated engineering harness. It does not modify the
frozen production launcher, does not rerun historical research cases, does not
delete containers, and does not retry failed replay requests.

## 1. Freeze the exact 68-request workload

Run on the GPU host against the actual historical wire log and its aligned
per-call metrics. Aggregates are not accepted as substitutes for request
payloads.

```bash
.venv/bin/python scripts/build_native_replay.py \
  --wire-jsonl /path/to/the-five-valid-reruns/wire.jsonl \
  --metrics /path/to/the-five-valid-reruns/call-metrics.json \
  --output-dir benchmarks/native-sweep-v1
```

`seed` is required. If the historical wire requests did not carry one, the
builder fails: the field must be disclosed as unavailable and the protocol
amended explicitly, never invented. Use `--align-by-order` only when an audit
has established that metrics and wire rows share exactly the same ordering.


## 2. Launch and verify one variant

Set and verify GPU 2's power cap before launching. The launcher refuses an
unexpected power value, active GPU 2 process, disabled persistence mode,
unexpected power range, existing container name, image drift, or server drift.

```bash
sudo nvidia-smi -i 2 -pl 240
bash deploy/native/benchmark/launch_variant.sh \
  --power 240 --concurrency 2 --container pilot-native-bench-p240-c2
```

The formal `pilot-native-seven-layer-v1` container should first be stopped (not
removed) by the operator. The benchmark launcher never stops it implicitly.

## 3. Run a cell

```bash
.venv/bin/python scripts/run_native_replay.py \
  --phase power --power 240 --concurrency 2 --repeat 1 \
  --container pilot-native-bench-p240-c2 \
  --output-dir outputs/native-performance-sweep-20261101/power/p240-c2-r01
```

For the 200 W smoke, add `--phase smoke --limit 8 --warmup 0`. For concurrency
safety, use `--phase concurrency-safety --limit $((2 * concurrency))`. A formal
cell uses the full corpus and five unmeasured warmups.

Stop without deleting the variant:

```bash
bash deploy/native/benchmark/stop_variant.sh \
  --container pilot-native-bench-p240-c2 \
  --output-dir outputs/native-performance-sweep-20261101/power/p240-c2-r01
```

## 4. Summarize

```bash
.venv/bin/python scripts/summarize_native_sweep.py \
  --root outputs/native-performance-sweep-20261101
```

The summarizer will not select a level until every candidate has three complete
passing repetitions. Follow the orders and thresholds in
`config/benchmark/native_sweep_v1.yaml`.

## Local telemetry charts

On Windows, drag a completed `gpu-telemetry.csv` onto
`生成GPU实验监测报告.cmd`. It creates a sibling `*-report` directory and opens
the standalone HTML report. The report includes power/raw median/cap and flagged
outliers, temperature, memory, utilization, clocks, sampling gaps, thermal
slowdown, ECC, and raw trapezoidal energy. If local Matplotlib is installed, it
also writes `gpu-monitor.png`; HTML, SVG, and JSON require only Python's standard
library.

Equivalent command:

```powershell
python scripts/generate_gpu_monitor_report.py `
  --input C:\path\to\gpu-telemetry.csv `
  --output C:\path\to\gpu-monitor-report `
  --png --open
```

