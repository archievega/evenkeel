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

`RATE_LIMIT` matters: the API allows 30 movements per owner per minute by
default, and the first attempt at this profile was 88% `429` and looked, at a
glance, like a load result. The stub provider's behaviour is a set of dials —
`RISK_LATENCY_MS`, `RISK_FAILURE_RATE`, `RISK_REFUSE_RATE` — and the service's
guards are `RISK_BULKHEAD_LIMIT`, `RISK_TIMEOUT_MS`, `RISK_CIRCUIT_THRESHOLD`.

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
| **A** | healthy, 5ms | bulkhead 32, breaker 5 | 4001 | 11.1ms | 17.5ms | 3.0ms |
| **B** | slow, 700ms | bulkhead 32, breaker 5 | 0 | <1ms | <1ms | 2.6ms |
| **B2** | slow, 700ms | bulkhead 32, no breaker | 88 | 0.7ms | 1.43s | 3.2ms |
| **C** | slow, 700ms | none | 109 | 449ms | 1.43s | 5.2ms |
| **D** | stopped | bulkhead 32, breaker 5 | 0 | 0.6ms | 1.0ms | 2.2ms |

Server-side, from `/metrics` — the reason that endpoint got built:

| | `success` | `timeout` | `bulkhead_full` | `circuit_open` | `connection` |
| --- | --- | --- | --- | --- | --- |
| A | 4041 | | | | |
| B | 10 | 9 | 0 | 4022 | |
| B2 | 213 | 329 | 3498 | | |
| C | 255 | 3786 | | | |
| D | | | | 4035 | 5 |

### The outbound hop costs 10ms, and it is the whole cost

Run 0 against run A: write p95 goes 7.5ms → 17.5ms with a provider answering in
5ms. Nothing else moves. Throughput is identical because the offered rate is
fixed and neither run is saturated.

### With the guards, a refusal is 1400x cheaper than without

Run D is the clean case: the provider is stopped, five requests fail to connect,
the circuit opens, and the remaining 4035 are refused in **1ms at p95**. Run C
is the same situation without guards, and every refused write pays the full
retry budget — **1.43s at p95** — to be told exactly the same thing.

That is the argument for both guards in one number. It is not about protecting
the database; it is about not making a client wait a second and a half for a no.

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

### The circuit breaker overreacts to partial degradation

The finding that changed an opinion. In run B the provider was **alive** — it
answered 213 of 542 calls in run B2, under identical conditions. With the
breaker enabled, run B applied **zero** movements: nine timeouts in a row were
enough to open the circuit, and it stayed effectively open for the rest of the
run, refusing 4022 requests on behalf of a dependency that was working 39% of
the time.

Consecutive-failure thresholds are cheap, fast to react, and blunt. A
degraded-but-useful dependency gets treated exactly like a dead one. The
alternative — a failure *rate* over a rolling window, with a minimum request
volume before the rate is trusted — costs more state and reacts more slowly, and
would have kept those 213 successful calls.

`CircuitPolicy.failure_threshold` is configuration, so raising it is a deploy
rather than a release. That is the mitigation, not a fix, and it is written down
in `docs/adr/0006-outbound-calls.md` as an accepted limitation rather than
quietly left for someone to find during an incident.

### Two 503s that mean opposite things look identical from outside

`bulkhead_full`, `timeout` and `circuit_open` all reach the client as
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
