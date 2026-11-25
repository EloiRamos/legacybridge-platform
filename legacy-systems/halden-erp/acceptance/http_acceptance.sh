#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    printf 'Usage: %s SIM_ON_URL SIM_OFF_URL OBSERVATIONS_FILE\n' "$0" >&2
    exit 2
fi
sim_on="${1%/}"
sim_off="${2%/}"
observations=$3

printf '# HALDEN ERP HTTP acceptance observations\n' >> "$observations"
path_as_is=0

fail() {
    printf '\n**FAIL** %s\n' "$*" >> "$observations"
    printf 'HTTP acceptance failed: %s\n' "$*" >&2
    exit 1
}

section() {
    printf '\n## %s\n' "$1" >> "$observations"
}

pass() {
    printf '\n**PASS** %s\n' "$1" >> "$observations"
    printf '%s PASS\n' "$1"
}

request() {
    local base=$1 method=$2 path=$3 ticket=$4
    shift 4
    local response headers head line key_headers=''
    local -a flags
    flags=(--silent --show-error --include --max-time 10 --noproxy '*' --request "$method")
    if (( path_as_is )); then
        flags+=(--path-as-is)
    fi
    if [[ -n "$ticket" ]]; then
        flags+=(--header "X-Halden-Ticket: $ticket")
    fi
    if [[ "$method" == POST ]]; then
        flags+=(--header 'Content-Type: application/x-www-form-urlencoded; charset=UTF-8')
        if (( $# == 0 )); then
            flags+=(--data '')
        fi
        while (( $# )); do
            flags+=(--data-urlencode "$1")
            shift
        done
    fi

    if ! response=$(curl "${flags[@]}" "$base$path" && printf '.'); then
        fail "$method $base$path could not be completed by curl"
    fi
    response=${response%.}
    [[ "$response" == *$'\r\n\r\n'* ]] || fail "Malformed HTTP response for $method $path"
    headers=${response%%$'\r\n\r\n'*}
    body=${response#*$'\r\n\r\n'}
    head=${headers%%$'\r\n'*}
    read -r _ status _ <<< "$head"
    while IFS= read -r line; do
        line=${line%$'\r'}
        case "$line" in
            [Cc]ontent-[Tt]ype:*|[Xx]-[Hh]alden-[Ww]ait:*) key_headers+=" $line"$'\n' ;;
        esac
    done <<< "$headers"
    printf '\n### %s %s%s → %s\n\n\140\140\140text\n%s%s\n\140\140\140\n' \
        "$method" "$base" "$path" "$status" "$key_headers" "${body:0:720}" >> "$observations"
}

expect_status() {
    [[ "$status" == "$1" ]] || fail "Expected HTTP $1, observed $status ($body)"
}

expect_body() {
    [[ "$body" == "$1" ]] || fail "Expected body $1, observed $body"
}

contains() {
    [[ "$body" == *"$1"* ]] || fail "Missing $1 in response body"
}

login() {
    request "$1" POST /halden/session '' login=svc_bridge secret=halden-demo-secret
    expect_status 200
    local granted=$'^STATUS=GRANTED\r\nTICKET=([A-F0-9]{32})\r\nEXPIRES_IN=[0-9]+\r\n$'
    [[ "$body" =~ $granted ]] || fail 'Invalid E2 grant format'
    ticket=${BASH_REMATCH[1]}
}

reset() {
    request "$sim_on" POST /_sim/reset '' rate_limit=0
    expect_status 200
    expect_body '{"reset":true,"rate_limit":0,"ticket_ttl_sec":900,"rate_window_sec":10}'
}

count_orders() {
    local base=$1 ticket=$2
    local next='' page='' pages=0 next_pattern='"continue":"([A-Za-z0-9_=-]*)"'
    order_count=0
    while :; do
        request "$base" GET "/halden/orders?cursor=$next" "$ticket"
        expect_status 200
        page=$body
        while [[ "$page" == *'"OrderNo":'* ]]; do
            page=${page#*'"OrderNo":'}
            order_count=$((order_count + 1))
        done
        [[ "$body" =~ $next_pattern ]] || fail 'Missing E6 continuation'
        next=${BASH_REMATCH[1]}
        [[ -n "$next" ]] || break
        pages=$((pages + 1))
        (( pages < 25 )) || fail 'E6 pagination did not terminate'
    done
}

raw_request() {
    path_as_is=1
    request "$@"
    path_as_is=0
}

stock_line() {
    local base=$1 ticket=$2 expected=$3
    request "$base" GET '/halden/stock?item=BW-1001' "$ticket"
    expect_status 200
    contains "$expected"
}

order_aliases=(/_sim/../halden/orders /_sim/./../halden/orders /halden/ping/../orders
    /halden/session/../orders /halden//orders /halden/./orders)
read_aliases=('/_sim/../halden/customers?acct=417' /_sim/../halden/items '/_sim/../halden/stock?item=BW-1001'
    /_sim/../halden/orders /_sim/../halden/orders/SO-240101)

reject_aliases_without_ticket() {
    local base=$1 alias
    for alias in "${read_aliases[@]}"; do
        raw_request "$base" GET "$alias" ''
        expect_status 401
        expect_body 'NO TICKET'
    done
    for alias in "${order_aliases[@]}"; do
        raw_request "$base" POST "$alias" '' acct=00417 ref=H01-ALIAS item=BW-1001 qty=10
        expect_status 401
        expect_body 'NO TICKET'
    done
}

race_order() {
    local account=$1 ref=$2
    request "$sim_on" POST /halden/orders "$ticket" "acct=$account" "ref=$ref" item=BW-1010 qty=2
    printf '%s|%s|%s|%s\n' "$account" "$ref" "$status" "$body"
}

section 'GA-H1 · Container liveness'
for base in "$sim_on" "$sim_off"; do
    request "$base" GET /halden/ping ''
    expect_status 200
    expect_body 'HALDEN-ERP 3.1 READY'
done
pass 'GA-H1 HTTP liveness'

section 'GA-H2 · Product reads, booking and rejections'
reset
login "$sim_on"
request "$sim_on" GET '/halden/customers?acct=417' "$ticket"
expect_status 200
contains '"acct":417'
contains '"NORDKAP MARINE SERVICES AS"'
request "$sim_on" GET '/halden/items?page=1' "$ticket"
expect_status 200
contains '<ITEMS page="1" pages="3">'
contains '<ITEM id="1"><ITMNO>BW-1001</ITMNO>'
request "$sim_on" GET '/halden/stock?item=BW-1001' "$ticket"
expect_status 200
contains $'BW-1001;DUS;240;0\r\n'
request "$sim_on" GET /halden/orders "$ticket"
expect_status 200
contains '"OrderNo":"SO-240101"'
contains '"continue":"Mw=="'
request "$sim_on" GET /halden/orders/SO-240101 "$ticket"
expect_status 200
contains '"rc":0'
contains '"OrderNo":"SO-240101","Acct":"417"'
request "$sim_on" POST /halden/orders "$ticket" acct=00417 ref=HTTP-BOOK item=BW-1001 qty=10
expect_status 200
expect_body '{"rc":0,"order_no":"SO-240111","total":"48.50"}'
request "$sim_on" GET /halden/orders/SO-240111 "$ticket"
expect_status 200
contains '"OrderNo":"SO-240111","Acct":"417"'
contains '"Ref":"HTTP-BOOK"'
contains '"lines":[{"ln":1,"item":"BW-1001","qty":"10","price":"4.85"}]'
request "$sim_on" GET '/halden/stock?item=BW-1001' "$ticket"
expect_status 200
contains $'BW-1001;DUS;240;10\r\n'
count_orders "$sim_on" "$ticket"
[[ "$order_count" == 11 ]] || fail "Expected 11 orders after booking, observed $order_count"
request "$sim_on" POST /halden/orders "$ticket" acct=10871 ref=HTTP-HOLD item=BW-1001 qty=1
expect_status 200
expect_body '{"rc":12,"msg":"ACCOUNT_ON_HOLD"}'
count_orders "$sim_on" "$ticket"
[[ "$order_count" == 11 ]] || fail 'Account-hold rejection changed the order count'
request "$sim_on" POST /halden/orders "$ticket" acct=00417 ref=HTTP-SHORT item=BW-1010 qty=5
expect_status 409
expect_body 'SHORT:BW-1010:2'
count_orders "$sim_on" "$ticket"
[[ "$order_count" == 11 ]] || fail 'Shortage rejection changed the order count'
pass 'GA-H2'

section 'GA-H3 · Concurrent last-stock requests'
reset
login "$sim_on"
request "$sim_on" GET '/halden/stock?item=BW-1010' "$ticket"
expect_status 200
contains $'BW-1010;DUS;2;0\r\n'
if ! race_results=$(
    race_order 00417 HRACE-A &
    a=$!
    race_order 52290 HRACE-B &
    b=$!
    wait "$a" && wait "$b"
); then
    fail 'A concurrent curl booking did not complete'
fi
successes=0
shortages=0
while IFS='|' read -r account ref status body; do
    case "$status" in
        200)
            expect_body '{"rc":0,"order_no":"SO-240111","total":"237.00"}'
            successes=$((successes + 1))
            winner=${account#00}
            winner_ref=$ref
            ;;
        409)
            expect_body 'SHORT:BW-1010:0'
            shortages=$((shortages + 1))
            ;;
        *) fail "Unexpected concurrent response $status ($body)" ;;
    esac
done <<< "$race_results"
[[ "$successes" == 1 && "$shortages" == 1 ]] || fail 'Expected exactly one booking and one shortage'
request "$sim_on" GET /halden/orders/SO-240111 "$ticket"
expect_status 200
contains "\"Acct\":\"$winner\""
contains "\"Ref\":\"$winner_ref\""
contains '"lines":[{"ln":1,"item":"BW-1010","qty":"2","price":"118.50"}]'
request "$sim_on" GET '/halden/stock?item=BW-1010' "$ticket"
expect_status 200
expect_body $'ITEM;WH;ON_HAND;ALLOCATED\r\nBW-1010;DUS;2;2\r\n'
count_orders "$sim_on" "$ticket"
[[ "$order_count" == 11 ]] || fail "Expected 11 orders after race, observed $order_count"
printf '\nWinner %s (%s). One shortage, DUS allocated 2 and 11 total orders.\n' "$winner" "$winner_ref" >> "$observations"
pass 'GA-H3'

section 'GA-H4 · Ordinary product path'
for operation in 'POST /_sim/reset' 'POST /_sim/faults' 'GET /_sim/faults' 'DELETE /_sim/faults' 'POST /_sim/external-order'; do
    read -r verb path <<< "$operation"
    request "$sim_off" "$verb" "$path" ''
    expect_status 404
    expect_body 'NOT FOUND'
done
login "$sim_off"
request "$sim_off" GET '/halden/customers?acct=417' "$ticket"
expect_status 200
contains '"acct":417'
request "$sim_off" GET '/halden/stock?item=BW-1001' "$ticket"
expect_status 200
contains $'BW-1001;DUS;240;0\r\n'
request "$sim_off" POST /halden/orders "$ticket" acct=00417 ref=HTTP-NORMAL item=BW-1001 qty=10
expect_status 200
expect_body '{"rc":0,"order_no":"SO-240111","total":"48.50"}'
request "$sim_off" GET /halden/orders/SO-240111 "$ticket"
expect_status 200
contains '"OrderNo":"SO-240111","Acct":"417"'
contains '"Ref":"HTTP-NORMAL"'
contains '"lines":[{"ln":1,"item":"BW-1001","qty":"10","price":"4.85"}]'
pass 'GA-H4'

section 'GA-H4 · Non-canonical request paths (H-01 regression, curl --path-as-is)'
reset
login "$sim_on"
reject_aliases_without_ticket "$sim_on"
for alias in "${order_aliases[@]}"; do
    request "$sim_on" POST /_sim/faults '' method=POST path_prefix=/halden/orders mode=error_500
    expect_status 200
    expect_body '{"armed":1}'
    raw_request "$sim_on" POST "$alias" "$ticket" acct=00417 ref=H01-FAULT item=BW-1001 qty=10
    expect_status 500
    expect_body '<html><body><h1>HALDEN ERP - Internal Error</h1></body></html>'
    request "$sim_on" GET /_sim/faults ''
    expect_status 200
    expect_body '{"faults":[]}'
done
count_orders "$sim_on" "$ticket"
[[ "$order_count" == 10 ]] || fail "Non-canonical requests changed the sim-on order count to $order_count"
stock_line "$sim_on" "$ticket" $'BW-1001;DUS;240;0\r\n'
reject_aliases_without_ticket "$sim_off"
for alias in /halden/../_sim/reset /halden/ping/../../_sim/faults; do
    raw_request "$sim_off" POST "$alias" ''
    expect_status 404
    expect_body 'NOT FOUND'
done
login "$sim_off"
count_orders "$sim_off" "$ticket"
[[ "$order_count" == 11 ]] || fail "Non-canonical requests changed the sim-off order count to $order_count"
stock_line "$sim_off" "$ticket" $'BW-1001;DUS;240;10\r\n'
pass 'GA-H4 non-canonical path regression'
