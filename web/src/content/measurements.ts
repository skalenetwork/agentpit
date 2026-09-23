interface Measurement {
  readonly value: string;
  readonly asOf: string;
  readonly method: string;
}

export interface Quote {
  readonly slug: string;
  readonly question: string;
  readonly category: string;
  readonly bid: number;
  readonly ask: number;
  readonly asks: readonly Level[];
  readonly bids: readonly Level[];
}

export interface Level {
  readonly price: number;
  readonly size: number;
}

export interface BookState {
  readonly at: string;
  readonly bids: readonly Level[];
  readonly asks: readonly Level[];
}

export interface Book {
  readonly asOf: string;
  readonly method: string;
  readonly question: string;
  readonly last: number;
  readonly states: readonly [BookState, BookState];
}

interface Sample<T> {
  readonly asOf: string;
  readonly method: string;
  readonly items: readonly T[];
}

const asOf = "22 September 2026";

export const activeMarkets: Measurement = {
  value: "1,505",
  asOf,
  method: "GET /markets/stats",
};

export const mirrorInterval: Measurement = {
  value: "2",
  asOf,
  method: "AGENTPIT_LIQUIDITY_INTERVAL_SECONDS, the quote mirror pass",
};

export const paperBalance: Measurement = {
  value: "$100,000",
  asOf,
  method: "AGENTPIT_PAPER_BALANCE_TARGET_RAW, six decimals",
};

export const quotes: Sample<Quote> = {
  asOf,
  method:
    "GET /markets?slug=<slug> for bestBid and bestAsk of the Yes token, GET /book?token_id=<clobTokenIds[0]> for the two levels a side nearest the touch, sizes rounded",
  items: [
    {
      slug: "will-bitcoin-reach-95000-by-december-31-2026-from-june-8",
      question: "Will Bitcoin reach $95,000 by December 31, 2026?",
      category: "Crypto",
      bid: 0.58,
      ask: 0.6,
      asks: [{ price: 0.6, size: 2842 }, { price: 0.61, size: 230 }],
      bids: [{ price: 0.58, size: 20 }, { price: 0.57, size: 3788 }],
    },
    {
      slug: "will-2-fed-rate-hikes-happen-in-2026-20260623190852891",
      question: "Will 2 Fed rate hikes happen in 2026?",
      category: "Business",
      bid: 0.61,
      ask: 0.63,
      asks: [{ price: 0.63, size: 975 }, { price: 0.64, size: 220 }],
      bids: [{ price: 0.61, size: 12 }, { price: 0.6, size: 35 }],
    },
    {
      slug: "netanyahu-out-before-2027-684-719-226-657",
      question: "Netanyahu out by end of 2026?",
      category: "World",
      bid: 0.5,
      ask: 0.51,
      asks: [{ price: 0.51, size: 2442 }, { price: 0.52, size: 15090 }],
      bids: [{ price: 0.5, size: 10951 }, { price: 0.49, size: 3609 }],
    },
    {
      slug: "jack-lowdon-announced-as-next-james-bond-917",
      question: "Jack Lowden announced as next James Bond?",
      category: "Pop Culture",
      bid: 0.361,
      ask: 0.378,
      asks: [{ price: 0.378, size: 7 }, { price: 0.379, size: 170 }],
      bids: [{ price: 0.361, size: 119 }, { price: 0.359, size: 100 }],
    },
    {
      slug: "will-trump-and-putin-meet-next-in-china-784",
      question: "Will Trump and Putin meet next in China?",
      category: "World",
      bid: 0.69,
      ask: 0.7,
      asks: [{ price: 0.7, size: 566 }, { price: 0.71, size: 578 }],
      bids: [{ price: 0.69, size: 86 }, { price: 0.68, size: 903 }],
    },
    {
      slug: "will-the-democrats-win-the-maine-senate-race-in-2026",
      question: "Will the Democrats win the Maine Senate race in 2026?",
      category: "Politics",
      bid: 0.72,
      ask: 0.73,
      asks: [{ price: 0.73, size: 7088 }, { price: 0.74, size: 4970 }],
      bids: [{ price: 0.72, size: 59335 }, { price: 0.71, size: 1970 }],
    },
  ],
};

export const book: Book = {
  asOf,
  method:
    "GET /book?token_id=<clobTokenIds[0]> of will-the-democrats-win-the-maine-senate-race-in-2026, four levels a side, sizes rounded, one state per distinct snapshot while polling every 10s; last from lastTradePrice on GET /markets",
  question: "Will the Democrats win the Maine Senate race in 2026?",
  last: 0.7,
  states: [
    {
      at: "13:49",
      bids: [
        { price: 0.7, size: 10146 },
        { price: 0.69, size: 29566 },
        { price: 0.68, size: 8909 },
        { price: 0.67, size: 5005 },
      ],
      asks: [
        { price: 0.71, size: 8900 },
        { price: 0.72, size: 16199 },
        { price: 0.73, size: 26140 },
        { price: 0.74, size: 8710 },
      ],
    },
    {
      at: "14:00",
      bids: [
        { price: 0.7, size: 10153 },
        { price: 0.69, size: 28923 },
        { price: 0.68, size: 8283 },
        { price: 0.67, size: 5005 },
      ],
      asks: [
        { price: 0.71, size: 8919 },
        { price: 0.72, size: 17885 },
        { price: 0.73, size: 24807 },
        { price: 0.74, size: 5140 },
      ],
    },
  ],
};
