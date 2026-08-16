# Recorded with VHS: https://github.com/charmbracelet/vhs
#
#   docker compose up -d --wait
#   docker build -t evenkeel-vhs tools/demo
#   docker run --rm -v "$PWD:/vhs" --network evenkeel_default \
#     evenkeel-vhs tools/demo/api.tape
#
# The container joins the compose network, so `api:8000` is the service under
# test — the same image CI boots in its smoke job. Nothing is staged: every
# response in the recording is what the API actually returned.
#
# Two things the lexer will not forgive: typed strings use backticks, because
# VHS cannot parse an escaped double quote inside a double-quoted `Type` and
# these commands are full of JSON; and typed text is ASCII only, because a
# multibyte character (an em-dash, in the first recording) desynchronises the
# cursor and the words around it come out reordered.

Output docs/demo.gif

Set Shell bash
Set FontSize 17
Set Width 1200
Set Height 520
Set Padding 18
Set TypingSpeed 18ms
Set Theme "Catppuccin Mocha"
Set WindowBar Colorful
Set BorderRadius 8

# Off camera: the base URL and two random owner ids. The bundled identity
# adapter treats the bearer token as the owner, which is why a demo of a money
# API has no signup step.
Hide
Type `export API=http://api:8000` Enter
Type `export OWNER=$(cat /proc/sys/kernel/random/uuid)` Enter
Type `export STRANGER=$(cat /proc/sys/kernel/random/uuid)` Enter
Type `clear` Enter
Show

Type `# one helper, so the demo is about the API and not about curl flags` Enter
Sleep 200ms
Type `api() { curl -s $API$1 -H 'content-type: application/json' -H "authorization: Bearer ${AS:-$OWNER}" "${@:2}"; }` Enter
Sleep 900ms

Type `` Enter
Type `W=$(api /v1/wallets -d '{"currency":"EUR"}' | jq -r .id)` Enter
Sleep 1.2s

Type `` Enter
Type `# money is decimal strings, never floats` Enter
Sleep 200ms
Type `api /v1/wallets/$W/deposits -d '{"amount":"100.00","currency":"EUR"}' | jq -c` Enter
Sleep 2s

Type `` Enter
Type `# overdraft: RFC 9457 problem+json, branch on code not on title` Enter
Sleep 200ms
Type `api /v1/wallets/$W/withdrawals -d '{"amount":"1000.00","currency":"EUR"}' | jq -c` Enter
Sleep 3s

Type `clear` Enter
Type `# a retry with the same Idempotency-Key does not move money twice` Enter
Sleep 200ms
Type `for i in 1 2; do api /v1/wallets/$W/deposits -H 'idempotency-key: k-42' -d '{"amount":"10.00","currency":"EUR"}' | jq -c '{balance,replayed}'; done` Enter
Sleep 3s

Type `` Enter
Type `# and another owner's wallet is absent, not forbidden` Enter
Sleep 200ms
Type `AS=$STRANGER api /v1/wallets/$W -o /dev/null -w '%{http_code}\n'` Enter
Sleep 2.5s
