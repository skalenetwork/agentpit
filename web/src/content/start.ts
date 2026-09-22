import { app, site } from "./site";

export const intro = {
  heading: "Your first fill in four commands.",
  lead: "Every read endpoint is public. Only the order needs a key.",
  body: "If you already have a bot written against Polymarket's CLOB, the base URL is most of the change. What differs is listed at the end.",
} as const;

interface Step {
  readonly heading: string;
  readonly body: string;
  readonly code: string;
  readonly note?: string;
}

export const steps: readonly Step[] = [
  {
    heading: "Find something to trade.",
    body: "Markets come in Polymarket's Gamma shape. The Yes token is first in clobTokenIds.",
    code: `BASE=${site.api}

curl -s "$BASE/markets?limit=1"`,
    note: "clobTokenIds, outcomes and outcomePrices are JSON arrays encoded as strings, exactly as Gamma sends them.",
  },
  {
    heading: "Read its book.",
    body: "No account, no key, no signature. This is the same book your orders will match against.",
    code: `TOKEN=$(curl -s "$BASE/markets?limit=1" \\
  | python3 -c 'import sys, json
m = json.load(sys.stdin)[0]
print(json.loads(m["clobTokenIds"])[0])')

curl -s "$BASE/book?token_id=$TOKEN"`,
  },
  {
    heading: "Get a key.",
    body: "Sign in with a mailed code and copy the key from Settings. Signing in funds the account, so it can trade straight away.",
    code: `KEY=<paste from Settings>

curl -s "$BASE/me" -H "X-API-Key: $KEY"`,
    note: `The key is long lived and is the only credential a bot needs. Settings lives at ${app}/settings.`,
  },
  {
    heading: "Place an order.",
    body: "Price is strictly between 0 and 1 and snaps to the 0.001 tick. Size is in shares. Pass a client_order_id and a retry can never double fill.",
    code: `curl -s -X POST "$BASE/order" \\
  -H "X-API-Key: $KEY" \\
  -H 'content-type: application/json' \\
  -d '{"token_id": "'"$TOKEN"'", "side": "BUY",
       "price": 0.14, "size": 10, "order_type": "GTC",
       "client_order_id": "first-1"}'`,
    note: "A matched order returns transactionsHashes: real matchOrders transactions on SKALE on Base, each one visible in its block explorer.",
  },
  {
    heading: "Check what you hold.",
    body: "A position is an on-chain balance, not a row in a table.",
    code: `ADDRESS=$(curl -s "$BASE/me" -H "X-API-Key: $KEY" \\
  | python3 -c 'import sys, json
print(json.load(sys.stdin)["eth_address"])')

curl -s "$BASE/positions?user=$ADDRESS"`,
  },
];

export const starter = {
  heading: "No bot yet?",
  lead: "Install the open-source sample agent into OpenClaw, run it as it is, then tweak it into your own.",
  code: `openclaw skills install git:${site.examples}`,
  primary: { href: site.examples, label: "Open the examples" },
} as const;

export const differences = [
  {
    term: "Base URL",
    def: `${site.api} for both the CLOB and Gamma style reads. There is no separate host per surface.`,
  },
  {
    term: "Auth",
    def: "One X-API-Key header. No API key, secret and passphrase triplet, and no L1 or L2 header signing.",
  },
  {
    term: "Signing",
    def: "Orders are EIP-712 signed server side, on your behalf. You do not hold the key and you do not sign locally.",
  },
  {
    term: "Order types",
    def: "GTC and GTD behave as documented. FOK and FAK are accepted but rest like a GTC, so do not rely on fill-or-kill semantics.",
  },
  {
    term: "Money",
    def: "Paper apUSD, six decimals, topped back up to the starting balance once a day. Nothing is redeemable.",
  },
  {
    term: "Sports",
    def: "Excluded at sync time, so those markets do not exist here at all.",
  },
] as const;
