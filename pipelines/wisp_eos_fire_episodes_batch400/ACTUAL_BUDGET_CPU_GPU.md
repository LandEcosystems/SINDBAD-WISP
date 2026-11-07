# Actual input-storage budget and CPU/GPU prediction consumers

The campaign uses **5 TB = 5,000,000,000,000 bytes of actual stored input** as
the global input-cache limit. This is disk storage, not allocated RAM. The
earlier 5 TiB limit based on conservative per-region reservations is superseded.
Completed compressed input, incomplete input and in-flight writes are counted;
the admission guard retains bounded headroom for writes already in progress.
The filesystem free-space guard remains active independently of this budget.

## Production and consumption

```text
Raw sources → CPU input producers → stored, validated input queue
                                       ├─ A100: bfloat16, batch 8
                                       └─ CPU: float32, batch 1
                                                  ↓
                              verified prediction + provenance
                                                  ↓
                                delayed, locked input cleanup
```

Up to 210 CPU input producer jobs can serve at most 10 simultaneously building
regions, with at most 21 producer jobs per region. A region waiting for
prediction does not consume a building-region slot. The resident-region limit
permits all 400 regions; actual storage, outstanding work and scheduler resource
availability determine admission. Completed regions and regions with no
observed fire are not submitted again.

The prediction side supports one A100 consumer and initially up to eight CPU
consumers. Each CPU consumer requests eight CPUs and 32 GiB RAM and reuses its
loaded model across tasks and regions. CPU and GPU requests are separate from
the input producers. The scheduler may queue them rather than run every
requested job simultaneously. CPU inference is slower per allocated worker
than A100 inference; its purpose is to produce results while GPU availability
is limited, with separately recorded runtime and backend information.

## One prediction owner per input task

Both consumers use the **same prediction queue**, the same exclusive task
lease and the same per-task file lock. The historical queue stage name `gpu`
continues to mean prediction; it is not a statement about actual hardware.
The first successful claim pins a task to `cpu` or `cuda`. A second consumer
cannot claim an active task, and completed predictions are skipped.

If a task stops between dates, its partial NetCDF and per-date hashes remain.
It resumes on its pinned backend and computes only missing dates. CPU partial
tasks can resume on another CPU model, with the actual processor recorded per
date. Task leases are only recovered after scheduler-confirmed process exit,
and existing task-file locks continue to protect live readers and writers.

Predictors never delete their own input. After durable prediction validation,
the independent cleanup stage waits at least 600 seconds and checks leases and
reader locks before deleting only registered temporary inputs. The released
capacity allows new input production. Original datasets, SINDBAD cubes,
prediction files, source identities and validation journals are retained.

## Scientific identity and execution provenance

All consumers read the same seven preprocessed float32 tensors. The frozen
input protocol, normalization, two float16 preprocessing conversions, weights,
model graph, query order, softmax-before-cast order and Gaussian rendering are
unchanged. Both paths use evaluation and inference mode.

The CPU option is explicitly authorized as a different numerical execution
backend. It uses float32 without CPU autocast; CUDA retains bfloat16 and batch
8. Frozen weights **do not guarantee identical floating-point predictions**.
Earlier CPU tests observed numerical differences, including a confidence value
crossing 0.5. Accordingly, CPU predictions are not labeled as A100 results.

CPU chunk attributes and receipts record:

| Field | CPU value |
|---|---|
| `inference_backend` | `cpu` |
| `precision` | `float32` |
| `batch_size` | `1` |
| `hardware` | `CPU`; processor models are recorded separately |
| `inference_hardware_by_day` | Actual processor per date in the receipt |
| Per-date journal | Backend, precision, batch size, processor, host, threads and timing |

Final NetCDF products have daily backend/precision/batch/provenance codes and
a backend catalog. A region can therefore contain both CPU and CUDA dates
without a misleading uniform metadata label. Older validated A100 chunks are
not rewritten: their provenance is inferred from their existing audited
A100/bfloat16/batch-8 metadata and explicitly marked as legacy provenance.

## Validation

`test_cpu_consumer.py` checks the seven-input inference call and output shapes,
finiteness and ranges; honest CPU metadata; exact partial-day read-back hashes;
interrupted publication and resume; shared CPU/GPU claims; backend pinning;
and the absence of repeated prediction or consumer-side deletion. Product
tests additionally verify mixed-backend consolidation and daily provenance.

The runtime API is
`cpu_consumer.run_region(config, model=None, max_tasks=1, stop_requested=None)`.
It returns the reusable model and completed task count, and returns promptly
when no eligible prediction task is available. The campaign controller holds
the shared regional prediction-reader lock while invoking it.
