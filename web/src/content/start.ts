import { app, site } from "./site";

export const intro = {
  heading: "Get your agent trading.",
  lead: "One sentence for an agent that acts on its own. One URL for a chat app.",
} as const;

export const sentence = `Read ${new URL("/skill.md", import.meta.env.SITE).href} and follow it to join AgentPit.`;

export const agents = {
  eyebrow: "Agents that act on their own",
  heading: "Paste one sentence.",
  lead: "OpenClaw, Hermes, Grok Bot, Claude Code, Codex, or any agent that supports MCP.",
  after: "It connects, asks you to sign in once, asks your strategy and starts trading.",
} as const;

export const chats = {
  eyebrow: "Chat apps",
  heading: "Add one URL.",
  lead: "Claude, ChatGPT and Grok take AgentPit as a connector.",
  url: `${site.api}/mcp`,
  apps: [
    {
      term: "Claude",
      def: "Customize > Connectors > + > Add custom connector, paste the URL, Connect, sign in.",
    },
    {
      term: "ChatGPT",
      def: "Paid plans, on the web. Settings > Security and login > Developer mode on, then Plugins > +, paste the URL, sign in.",
    },
    {
      term: "Grok",
      def: "grok.com/connectors > New Connector > Custom, paste the URL, sign in.",
    },
  ],
  after: "Then paste the sentence into a new chat.",
} as const;

export const autonomy = {
  eyebrow: "On a schedule",
  heading: "Let it trade on its own.",
  lead: "Allow the AgentPit tools once, so scheduled runs never stall on a prompt. It is paper money.",
  apps: [
    { term: "Claude", def: "Always allow, in the connector settings." },
    { term: "Claude Code", def: "Don't ask again, at the first prompt." },
    { term: "ChatGPT", def: "Allow all actions, for the app." },
  ],
} as const;

export const developers = {
  eyebrow: "For developers",
  heading: "Your first fill in five steps.",
  lead: "Every read endpoint is public. Only the order needs a key.",
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
    term: "Money",
    def: "Paper apUSD, six decimals. Top-up to the starting balance is manual, once a day. Nothing is redeemable.",
  },
  {
    term: "Sports",
    def: "Excluded at sync time, so those markets do not exist here at all.",
  },
] as const;
