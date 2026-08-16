// Load profile for the wallet API.
//
// The question this answers is not "how fast is it" — that number belongs to
// whatever hardware you ran it on and is worth very little. It is:
//
//   when the outbound dependency turns slow, does the damage stay on the
//   endpoints that call it, or does it spread to the ones that do not?
//
// So there are two scenarios running at once against the same process: writes,
// which go through the risk provider, and reads, which do not. The result to
// look at is `http_req_duration{scenario:reads}` while the provider is slow.
//
//   docker run --rm -i --network evenkeel_default \
//     -e BASE_URL=http://api:8000 grafana/k6 run - < tools/load/wallets.js
//
// Everything is configurable through env so a run can be repeated:
//   BASE_URL, WRITE_RPS, READ_RPS, DURATION, WALLETS
//
// Note the rate limit: the API allows 30 movements per owner per minute by
// default, so a write-heavy run against few wallets measures the limiter and
// nothing else. Raise it for the service under test (`RATE_LIMIT` in
// compose.yml) rather than quietly working around it here.

import http from 'k6/http'
import { check, fail } from 'k6'
import { Counter } from 'k6/metrics'

const BASE = __ENV.BASE_URL || 'http://localhost:8000'
const WRITE_RPS = Number(__ENV.WRITE_RPS || 200)
const READ_RPS = Number(__ENV.READ_RPS || 50)
const DURATION = __ENV.DURATION || '30s'
const WALLETS = Number(__ENV.WALLETS || 40)

// Counted by hand because the interesting outcomes are not failures. A 503 from
// a shed movement is the system working as designed, and lumping it in with
// http_req_failed would report a successful demonstration as a broken service.
const shed = new Counter('movements_shed_503')
const refused = new Counter('movements_refused_403')
const applied = new Counter('movements_applied')
const conflicted = new Counter('movements_conflicted_409')
const limited = new Counter('movements_rate_limited_429')
// Anything not on the list above. A run where this is non-zero is a run whose
// summary is lying to you about what it measured — the first attempt at this
// profile was 88% rate-limited and looked, at a glance, like a load result.
const unexpected = new Counter('movements_unexpected_status')

export const options = {
  // An open model: a fixed number of requests per second arrive whether or not
  // the previous ones have come back. `constant-vus` would be a closed model,
  // where each VU waits for its response before sending the next — so a server
  // that answers faster is immediately offered more load, and two runs are
  // never comparable. The first version of this file made that mistake, and it
  // produced a run where fast shedding looked *worse* than slow success purely
  // because the client sent 2.5x as many requests.
  scenarios: {
    writes: {
      executor: 'constant-arrival-rate',
      rate: WRITE_RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(50, WRITE_RPS),
      maxVUs: Math.max(200, WRITE_RPS * 4),
      exec: 'move',
    },
    reads: {
      executor: 'constant-arrival-rate',
      rate: READ_RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: Math.max(20, READ_RPS),
      maxVUs: Math.max(100, READ_RPS * 4),
      exec: 'read',
    },
  },
  thresholds: {
    // The claim being tested, as an assertion. Reads never touch the provider,
    // so their latency must not move when it does. If this fails, the bulkhead
    // is not doing its job and the slow dependency is spreading.
    'http_req_duration{scenario:reads}': ['p(95)<150'],
    // No 5xx that is not a deliberate 503 from the risk policy.
    'http_req_failed{scenario:reads}': ['rate<0.01'],
    // A refusal must be cheap. The whole argument for a bulkhead and a breaker
    // is that a caller who cannot be served finds out immediately instead of
    // paying the dependency's timeout to be told the same thing. Run the
    // no-guards scenario in tools/load/README.md and watch this one break: it
    // goes from single-digit milliseconds to the full retry budget.
    'http_req_duration{scenario:writes}': ['p(95)<250'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
}

function uuid() {
  // Enough of a v4 for the dev identity provider, which only parses it.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

function auth(owner) {
  return { Authorization: `Bearer ${owner}`, 'Content-Type': 'application/json' }
}

export function setup() {
  const wallets = []
  for (let i = 0; i < WALLETS; i++) {
    const owner = uuid()
    const created = http.post(
      `${BASE}/v1/wallets`,
      JSON.stringify({ currency: 'EUR' }),
      { headers: auth(owner) },
    )
    if (created.status !== 201) {
      fail(`could not open a wallet: ${created.status} ${created.body}`)
    }
    const id = created.json('id')
    // Funded generously so withdrawals under load fail for interesting reasons
    // rather than because the wallet is empty.
    http.post(
      `${BASE}/v1/wallets/${id}/deposits`,
      JSON.stringify({ amount: '1000000.00', currency: 'EUR' }),
      { headers: auth(owner) },
    )
    wallets.push({ owner, id })
  }
  return { wallets }
}

// Spread across wallets: concurrent writers on a single wallet serialise on the
// per-wallet lock, which measures the lock rather than the dependency this run
// is about. With an arrival-rate executor the VU pool churns, so the iteration
// counter is the stable thing to spread on.
function pick(data) {
  return data.wallets[__ITER % data.wallets.length]
}

export function move(data) {
  const wallet = pick(data)
  const response = http.post(
    `${BASE}/v1/wallets/${wallet.id}/withdrawals`,
    JSON.stringify({ amount: '1.00', currency: 'EUR' }),
    { headers: auth(wallet.owner) },
  )

  if (response.status === 200) applied.add(1)
  else if (response.status === 503) shed.add(1)
  else if (response.status === 403) refused.add(1)
  else if (response.status === 409) conflicted.add(1)
  else if (response.status === 429) limited.add(1)
  else unexpected.add(1)

  check(response, {
    'movement resolved, one way or another': (r) =>
      [200, 403, 409, 429, 503].includes(r.status),
    'never a 500': (r) => r.status !== 500,
  })
}

export function read(data) {
  const wallet = pick(data)
  const response = http.get(`${BASE}/v1/wallets/${wallet.id}`, {
    headers: auth(wallet.owner),
  })

  check(response, { 'read is 200': (r) => r.status === 200 })
}
