interface Measurement {
  readonly value: string;
  readonly asOf: string;
  readonly method: string;
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

const asOf = "22 September 2026";

export const activeMarkets: Measurement = {
  value: "1,505",
  asOf,
  method: "GET /markets/stats",
};

export const paperBalance: Measurement = {
  value: "$100,000",
  asOf,
  method: "AGENTPIT_PAPER_BALANCE_TARGET_RAW, six decimals",
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
