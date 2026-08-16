# Load runs

Not a benchmark. The numbers below come from Docker Desktop on a laptop and say
nothing about what this code does on a server. They exist to answer one
question that cannot be answered by reading the source:

> when the outbound dependency degrades, what does it cost, and which guard
> actually fires?

Everything here is reproducible in about a minute per run.

## Running one

```bash
RISK_PROVIDER=http RATE_LIMIT=1000000 METRICS_ENABLED=true \
RISK_LATENCY_MS=700 RISK_BULKHEAD_LIMIT=32 \
docker compose --profile load up -d --build --wait
```

```bash
docker run --rm -i --network evenkeel_default -e BASE_URL=http://api:8000 \
  grafana/k6 run - < tools/load/wallets.js
```

```bash
curl -s localhost:8000/metrics | grep evenkeel_external_call_total
```

Or read it on a dashboard instead of in a grep — `make observe` brings the same
stack up with Prometheus and Grafana attached, and the run above fills it:

```bash
make observe && make load
```

`RATE_LIMIT` matters: the API allows 30 movements per owner per minute by
default, and the first attempt at this profile was 88% `429` and looked, at a
glance, like a load result. The stub provider's behaviour is a set of dials —
`RISK_LATENCY_MS`, `RISK_FAILURE_RATE`, `RISK_REFUSE_RATE` — and the service's
guards are `RISK_BULKHEAD_LIMIT` and `RISK_TIMEOUT_MS`.

The client profile is an **open model** (`constant-arrival-rate`): 200 write
requests and 50 read requests per second arrive whether or not the previous ones
have come back. A closed model — fixed VUs, each waiting for its response —
offers *more* load to a server that answers faster, which made an early run
report fast shedding as worse than slow success. Two runs are only comparable if
the offered load is the same.

## What the runs found

200 write rps + 50 read rps for 20s, 40 wallets. Reads never touch the provider;
writes always do.

| | provider | guards | movements applied | write p50 | write p95 | read p95 |
| --- | --- | --- | --- | --- | --- | --- |
| **0** | none (`allow-all`) | — | 4001 | 4.5ms | 7.5ms | 2.6ms |
| **A** | healthy, 5ms | bulkhead 32 | 4001 | 11.1ms | 17.5ms | 3.0ms |
| **B2** | slow, 700ms | bulkhead 32 | 88 | 0.7ms | 1.43s | 3.2ms |
| **C** | slow, 700ms | none | 109 | 449ms | 1.43s | 5.2ms |
| **D** | stopped | bulkhead 32 | — | 32.8ms | 56.5ms | 4.3ms |
| **F** | failing 55% | bulkhead 32 | 1950 | 22.4ms | 63.4ms | — |

Server-side, from `/metrics` — the reason that endpoint got built:

| | `success` | `timeout` | `server_error` | `bulkhead_full` | `connection` |
| --- | --- | --- | --- | --- | --- |
| A | 4041 | | | | |
| B2 | 213 | 329 | | 3498 | |
| C | 255 | 3786 | | | |
| D | | | | | 4040 |
| F | 2785 | | 1256 | | |

### The outbound hop costs 10ms, and it is the whole cost

Run 0 against run A: write p95 goes 7.5ms → 17.5ms with a provider answering in
5ms. Nothing else moves. Throughput is identical because the offered rate is
fixed and neither run is saturated.

### With the bulkhead, a refusal is ~600x cheaper than without

Run B2 against run C, same slow provider: with the bulkhead a shed write is
refused in **0.7ms at the median**, against **449ms** with no bulkhead, where
every caller pays the provider's latency before being told no. The p95 is the
same 1.43s in both, because that is the retry budget spent by the calls that did
get a slot — the bulkhead changes what happens to the majority, not to the few
it lets through.

It is not about protecting the database; it is about not making a client wait
for a no.

### The read endpoints were never in danger — and not for the reason claimed

The bulkhead's docstring says an unbounded outbound call ties up "a task and the
database connection of the request it sits inside", and reads suffer. Reads
here stayed between 2.2ms and 5.2ms in every run, including the one with no
guards at all.

The reason is that SQLAlchemy acquires a connection on the first query, not when
the session is created, and the risk check runs *before* the first query. So no
pool connection is held across the outbound call. That is a real property of the
design and it is worth stating — but it was an accident of ordering, not a
decision, until this run made it visible. Move the check after the wallet is
loaded and run C again: the guarantee disappears.

### Two 503s that mean opposite things look identical from outside

`bulkhead_full`, `timeout` and `budget_exhausted` all reach the client as
`503 DEPENDENCY_UNAVAILABLE`, which is correct — the distinction is internal —
and it makes a client-side load summary unreadable. The first two runs of this
profile were interpreted wrongly for exactly that reason.

`observe_external_call` had been an abstract method with a single no-op
implementation and no caller. The Prometheus adapter exists because of this run.

### An expected conflict was being logged as an error

`movements_conflicted_409` in runs B2 and C is the per-wallet lock doing its job
under contention. The interactor was recording every one of them as
`outcome="error"`, indistinguishable from a genuine fault. Fixed while reading
these numbers; the outcome is now the error code.
