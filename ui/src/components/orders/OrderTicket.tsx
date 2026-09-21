import { useMemo, useRef, useState } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { getBook, placeMarketOrder, placeOrder, useBook } from "@/api/orders";
import { usePositions } from "@/api/portfolio";
import { useAuth } from "@/auth/useAuth";
import { useRequireAuth } from "@/auth/useRequireAuth";
import {
  MAX_PROB,
  MIN_PROB,
  bestAsk,
  bestBid,
  centsToPrice,
  computeMarketBuy,
  computeMarketSell,
  dollarsFromShares,
  pickSellOutcome,
  sharesFromDollars,
} from "@/components/orders/orderMath";
import type { Erc1155Token } from "@/types/market";
import type { OrderBookSummary, OrderSide } from "@/types/order";
import { ApiError } from "@/api/client";
import { normaliseQuantity } from "@/components/orders/quantity";
import {
  STEP_CENTS,
  normaliseLimitCents,
  stepLimitCents,
} from "@/components/orders/limitPrice";
import { EXPIRY_OPTIONS, expiryForLabel, isExpiryDisabled } from "@/lib/orderExpiry";
import type { ExpiryLabel } from "@/lib/orderExpiry";

type Mode = "Limit" | "Market";

interface OrderTicketProps {
  marketId: number;
  tokens: Erc1155Token[];
  outcome: string;
  question: string;
  iconUrl?: string | null;
  endDate: number | null;
  isTradingDisabled: boolean;
  disabledReason?: string;
  onOutcomeChange?: (outcome: string) => void;
}

const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function formatDollars(n: number): string {
  return USD.format(n);
}

function endsInLabel(endDate: number | null): string | null {
  if (endDate === null) return null;
  const msLeft = endDate * 1000 - Date.now();
  if (msLeft <= 0) return "Closed";
  const days = Math.ceil(msLeft / 86_400_000);
  if (days <= 1) {
    const hours = Math.max(1, Math.ceil(msLeft / 3_600_000));
    return `Ends in ${hours} hour${hours === 1 ? "" : "s"}`;
  }
  return `Ends in ${days} days`;
}

/** Permissive decimal parser — strips locale commas and grouping. */
function parseDecimal(input: string): number {
  const trimmed = input.trim().replace(/,/g, ".");
  if (trimmed === "") return NaN;
  return Number(trimmed);
}

export function OrderTicket({
  marketId: _marketId,
  tokens,
  outcome,
  question,
  iconUrl,
  endDate,
  isTradingDisabled,
  disabledReason,
  onOutcomeChange,
}: OrderTicketProps) {
  const [side, setSide] = useState<OrderSide>("BUY");
  const [mode, setMode] = useState<Mode>("Limit");
  // Held in CENTS, which is what the field shows. The API takes a
  // probability in [0,1], so the single conversion below is the only place
  // the two units meet — everything downstream reads limitPriceUsd.
  const [limitCents, setLimitCents] = useState<string>("50");
  // NaN for empty or malformed input, exactly as parseDecimal returned before,
  // so every downstream Number.isFinite guard keeps working unchanged.
  const limitPriceUsd = centsToPrice(limitCents);
  const [limitShares, setLimitShares] = useState<string>("");
  const [marketAmount, setMarketAmount] = useState<string>("");
  const [expiresOpen, setExpiresOpen] = useState(false);
  const [expiresLabel, setExpiresLabel] = useState<ExpiryLabel>("Never");

  const requireAuth = useRequireAuth();
  const queryClient = useQueryClient();
  const tokenId = tokens.find(([, label]) => label === outcome)?.[0] ?? "";
  const { data: book } = useBook(tokenId);
  const { user } = useAuth();
  const { data: positionsList } = usePositions(user?.eth_address);

  // Build a set of token_ids that belong to this market for fast membership check.
  const marketTokenIds = useMemo(
    () => new Set(tokens.map(([id]) => id)),
    [tokens],
  );

  const heldByOutcome = useMemo(() => {
    const map = new Map<string, number>();
    if (!positionsList) return map;
    for (const p of positionsList) {
      if (!marketTokenIds.has(p.asset)) continue;
      map.set(p.outcome, p.size);
    }
    return map;
  }, [positionsList, marketTokenIds]);

  function selectSide(next: OrderSide) {
    setSide(next);
    if (next === "SELL" && onOutcomeChange) {
      const target = pickSellOutcome(outcome, heldByOutcome);
      if (target !== outcome) onOutcomeChange(target);
    }
  }

  const bestAskPrice =
    book && book.asks.length > 0 ? bestAsk(book.asks) : null;
  const bestBidPrice =
    book && book.bids.length > 0 ? bestBid(book.bids) : null;

  const invalidate = () => {
    void queryClient.invalidateQueries({
      queryKey: ["book", tokenId],
    });
    void queryClient.invalidateQueries({ queryKey: ["positions", user?.eth_address] });
  };

  const limitMutation = useMutation({
    mutationFn: async () => {
      const price = limitPriceUsd;
      const shares = parseDecimal(limitShares);
      const { order_type, expiration } = expiryForLabel(expiresLabel, Date.now());
      return placeOrder({
        token_id: tokenId,
        side,
        price,
        size: shares,
        order_type,
        expiration,
      });
    },
    onSuccess: (res) => {
      if (!res.success) {
        toast.error(`Order failed: ${res.errorMsg || "unknown"}`);
        return;
      }
      // postOrder amounts are taker-perspective: BUY receives shares
      // (takingAmount), SELL gives shares (makingAmount).
      const filled =
        side === "BUY"
          ? Number(res.takingAmount || "0")
          : Number(res.makingAmount || "0");
      const isLive = res.status === "live";
      toast.success(
        isLive
          ? `Order placed: ${filled.toFixed(2)} filled, resting`
          : `Order filled: ${filled.toFixed(2)} shares`,
      );
      setLimitShares("");
      invalidate();
    },
    onError: (err) => {
      toast.error(err instanceof ApiError ? err.message : String(err));
    },
  });

  const marketMutation = useMutation({
    mutationFn: async () => {
      if (!book) throw new Error("Orderbook not loaded yet");
      const amount = parseDecimal(side === "BUY" ? marketAmount : limitShares);
      return placeMarketOrder({
        tokenId,
        side,
        amount,
        book,
      });
    },
    onSuccess: (res) => {
      const tail = res.cancelledRemainder
        ? ` (${res.remainingShares.toFixed(2)} unfilled, cancelled)`
        : "";
      const avg = res.avgPrice !== null ? ` @ avg $${res.avgPrice.toFixed(2)}` : "";
      if (res.filledShares <= 0) {
        toast.error(`No fills${tail || ""}`);
      } else {
        toast.success(
          `${side === "BUY" ? "Bought" : "Sold"} ${res.filledShares.toFixed(2)}${avg}${tail}`,
        );
      }
      if (res.cancelError) {
        toast.warning(`Auto-cancel failed: ${res.cancelError}`);
      }
      if (side === "BUY") {
        setMarketAmount("");
      } else {
        setLimitShares("");
      }
      invalidate();
    },
    onError: (err) => {
      toast.error(err instanceof Error ? err.message : String(err));
    },
  });

  const preview = useMemo(() => {
    if (mode === "Limit") {
      const price = limitPriceUsd;
      const shares = parseDecimal(limitShares);
      if (!Number.isFinite(price) || !Number.isFinite(shares)) return null;
      if (price <= 0 || price >= 1 || shares <= 0) return null;
      const cost = dollarsFromShares(shares, price);
      return {
        price,
        shares,
        cost,
        capWarning: null as string | null,
      };
    }
    const amount = parseDecimal(side === "BUY" ? marketAmount : limitShares);
    if (!book) return null;
    if (!Number.isFinite(amount) || amount <= 0) return null;
    if (side === "BUY") {
      const comp = computeMarketBuy(book.asks, amount);
      if (!comp) return null;
      const shares = sharesFromDollars(amount, comp.priceCap);
      return {
        price: comp.priceCap,
        shares,
        cost: amount,
        capWarning:
          comp.priceCap >= MAX_PROB
            ? `Cap clamped to ${MAX_PROB.toFixed(2)}`
            : null,
      };
    }
    const comp = computeMarketSell(book.bids, amount);
    if (!comp) return null;
    const total = dollarsFromShares(amount, comp.priceCap);
    return {
      price: comp.priceCap,
      shares: amount,
      cost: total,
      capWarning:
        comp.priceCap <= MIN_PROB
          ? `Cap clamped to ${MIN_PROB.toFixed(2)}`
          : null,
    };
  }, [mode, side, limitPriceUsd, limitShares, marketAmount, book]);

  const canSubmit =
    !isTradingDisabled &&
    preview !== null &&
    !limitMutation.isPending &&
    !marketMutation.isPending &&
    (mode === "Limit" ||
      (side === "BUY" ? bestAskPrice !== null : bestBidPrice !== null));

  const onSubmit = requireAuth(() => {
    if (mode === "Limit") {
      limitMutation.mutate();
    } else {
      marketMutation.mutate();
    }
  });

  const isBuy = side === "BUY";
  const endsLabel = endsInLabel(endDate);
  const selectedOutcomeIndex = tokens.findIndex(([, label]) => label === outcome);
  const selectedOutcomeTone =
    selectedOutcomeIndex === 0
      ? "text-emerald-600 dark:text-emerald-400"
      : "text-rose-600 dark:text-rose-400";
  // Buying and selling must not look identical. The restyle left this as a
  // ternary with two identical branches, which reads as intent while doing
  // nothing — on the one control where the colour carries the meaning.
  const ctaTone = isBuy
    ? "bg-emerald-700 hover:bg-emerald-700/90 text-white"
    : "bg-rose-700 hover:bg-rose-700/90 text-white";

  const ctaLabel = (() => {
    if (isTradingDisabled) return disabledReason ?? "Trading disabled";
    if (limitMutation.isPending || marketMutation.isPending) return "Placing…";
    // Just the verb. The button used to restate the size, outcome and price,
    // which reads as a confirmation until you notice every one of those
    // numbers is already on screen directly above it — the outcome in the
    // header and the Yes/No pair, the size and price in the fields, the
    // payout in the row above. Repeating them made the ticket noisy without
    // telling the trader anything the ticket had not already said.
    return isBuy ? "Buy" : "Sell";
  })();

  // What comes back if the outcome is right. Every share of a winning outcome
  // redeems for exactly $1, so the payout IS the share count in dollars.
  //
  // This row used to show the PROFIT, which made a $55 buy read "To win
  // $22.46" — a number smaller than the stake, so a winning trade looked like
  // a losing one. The profit is still worth knowing, so it rides along beside
  // the payout instead of standing in for it.
  const toWin = preview ? (isBuy ? preview.shares : preview.cost) : 0;

  const total = preview?.cost ?? 0;

  // A market buy's total is the amount just typed into the field above it
  // (`preview.cost` is that very number), so printing it back is a row that
  // tells the trader something they wrote themselves. A limit order's total
  // is derived from shares x price and does carry news.
  const showTotal = isBuy && mode === "Limit";

  const adjustLimitCents = (delta: number) => {
    setLimitCents((prev) => stepLimitCents(prev, delta));
  };

  const adjustShares = (delta: number) => {
    const source = mode === "Limit" || !isBuy ? limitShares : marketAmount;
    const raw = parseDecimal(source);
    const next = Number.isFinite(raw) ? raw + delta : delta;
    const clamped = Math.max(0, next);
    const rendered = clamped === 0 ? "" : String(Number(clamped.toFixed(2)));
    if (mode === "Limit" || !isBuy) {
      setLimitShares(rendered);
      return;
    }
    setMarketAmount(rendered);
  };

  return (
    <section className="sticky top-20 w-full max-w-[340px] self-start overflow-visible rounded-3xl border border-border bg-card shadow-[0_1px_0_0_hsl(var(--border)),0_24px_55px_-24px_hsl(var(--foreground)/0.18)]">
      <header className="space-y-1.5 px-4 py-4">
        <div className="flex items-start gap-2.5">
          {iconUrl ? (
            <img src={iconUrl} alt="" className="size-12 rounded-xl object-cover" />
          ) : (
            <div className="size-12 rounded-xl bg-muted" />
          )}
          <div className="min-w-0">
            <h2 className="truncate text-[13px] font-medium leading-[1.2] text-muted-foreground">
              {question}
            </h2>
            <p className={cn("mt-1 text-[14px] font-semibold leading-none", selectedOutcomeTone)}>
              {outcome}
            </p>
            {/* How long there is left to trade. The restyle dropped this call
                while keeping the function and the prop, so the ticket stopped
                saying when its market closes — restored here rather than
                deleting the two, because a trader deciding on a price needs to
                know whether the thing settles tonight or in March. */}
            {endsLabel ? (
              <p className="mt-1 text-[11px] text-muted-foreground">{endsLabel}</p>
            ) : null}
          </div>
        </div>
      </header>

      <div className="flex items-center justify-between border-y border-border px-4 py-2.5">
        <SegmentedSide side={side} onSelect={selectSide} />
        <button
          type="button"
          onClick={() => {
            // Leaving Limit hides the expiry row, so drop the open flag with
            // it — otherwise the menu springs back the moment the user
            // returns to Limit.
            setExpiresOpen(false);
            setMode((prev) => (prev === "Limit" ? "Market" : "Limit"));
          }}
          className="inline-flex items-center gap-1 text-[13px] font-medium text-foreground"
        >
          {mode}
          <ChevronDown aria-hidden className="size-4 text-muted-foreground" />
        </button>
      </div>

      <div className="space-y-0">
        <div className="px-4 py-4">
          <OutcomePicker
            tokens={tokens}
            selected={outcome}
            side={side}
            onSelect={(label) => onOutcomeChange?.(label)}
          />
        </div>

        {mode === "Limit" ? (
          <>
            <div className="flex items-center justify-between border-t border-border px-4 py-4">
              <span className="text-[14px] font-medium text-foreground">Limit price</span>
              <CentsStepper
                cents={limitCents}
                onChange={(typed) =>
                  setLimitCents((prev) => normaliseLimitCents(typed, prev))
                }
                onStep={adjustLimitCents}
                disabled={isTradingDisabled}
              />
            </div>

            <div className="space-y-3 border-t border-border px-4 py-4">
              <div className="flex items-center justify-between gap-4">
                <span className="text-[14px] font-medium text-foreground">Shares</span>
                <SharesInput
                  id="limit-shares"
                  value={limitShares}
                  onChange={(typed) =>
                    setLimitShares((prev) => normaliseQuantity(typed, prev))
                  }
                  disabled={isTradingDisabled}
                />
              </div>
              <QuickShareChips onPick={adjustShares} />
            </div>
          </>
        ) : isBuy ? (
          <div className="space-y-3 border-t border-border px-4 py-4">
            <Field label="Amount">
              <ValueInput
                id="market-amount"
                value={marketAmount}
                onChange={(typed) =>
                  setMarketAmount((prev) => normaliseQuantity(typed, prev))
                }
                disabled={isTradingDisabled}
              />
            </Field>
          </div>
        ) : (
          <div className="space-y-3 border-t border-border px-4 py-4">
            <div className="flex items-center justify-between gap-4">
              <span className="text-[14px] font-medium text-foreground">Shares</span>
              <SharesInput
                id="market-shares"
                value={limitShares}
                onChange={setLimitShares}
                disabled={isTradingDisabled}
              />
            </div>
            <QuickShareChips onPick={adjustShares} />
          </div>
        )}

        <div className="relative space-y-2 border-t border-border px-4 py-4">
          {/* Limit only. A market order takes whatever the book offers the
              instant it is sent, so it has no resting life for an expiry to
              cut short; offering the choice there would promise a control
              that cannot do anything. */}
          {mode === "Limit" ? (
            <>
          <div className="flex items-center justify-between text-[13px] text-muted-foreground">
            <span>Expires</span>
            <button
              type="button"
              onClick={() => setExpiresOpen((prev) => !prev)}
              className="inline-flex items-center gap-1.5 rounded-md px-1 py-0.5 hover:bg-muted"
              aria-expanded={expiresOpen}
              aria-haspopup="menu"
            >
              {expiresLabel}
              <ChevronDown
                aria-hidden
                className={cn("size-4 transition-transform", expiresOpen ? "rotate-180" : "")}
              />
            </button>
          </div>
          {expiresOpen ? (
            <div
              role="menu"
              className="absolute right-4 top-11 z-30 w-44 rounded-2xl border border-border bg-card p-2 shadow-lg"
            >
              {EXPIRY_OPTIONS.map((option) => (
                <button
                  key={option}
                  type="button"
                  role="menuitem"
                  disabled={isExpiryDisabled(option, Date.now())}
                  onClick={() => {
                    setExpiresLabel(option);
                    setExpiresOpen(false);
                  }}
                  className={cn(
                    "w-full rounded-xl px-3 py-2 text-left text-[14px] hover:bg-muted",
                    "disabled:opacity-40 disabled:cursor-not-allowed",
                    option === expiresLabel ? "font-semibold text-foreground" : "text-foreground",
                  )}
                >
                  {option}
                </button>
              ))}
            </div>
          ) : null}
            </>
          ) : null}
          {showTotal ? (
            <div className="flex items-center justify-between text-[14px]">
              <span className="text-foreground">Total</span>
              <span className="font-medium tabular-nums text-primary">{formatDollars(total)}</span>
            </div>
          ) : null}
          <div className="flex items-center justify-between text-[14px]">
            <span className="text-foreground">{isBuy ? "To win" : "You'll receive"}</span>
            <span
              className={cn(
                // Green either way: this row is what the position returns if
                // it comes good, and that is a gain on both sides. Was a
                // ternary with two identical branches.
                "font-medium tabular-nums text-emerald-600 dark:text-emerald-400",
              )}
            >
              {formatDollars(toWin)}
            </span>
          </div>

          {preview?.capWarning ? (
            <p className="text-[11px] text-amber-700 dark:text-amber-400">
              {preview.capWarning}
            </p>
          ) : null}

          <Button
            type="button"
            disabled={!canSubmit}
            className={cn(
              "mt-2 h-10 w-full rounded-xl text-[15px] font-semibold transition-all",
              !isTradingDisabled && ctaTone,
              !canSubmit && "opacity-60",
            )}
            onClick={onSubmit}
          >
            {ctaLabel}
          </Button>
        </div>
      </div>
    </section>
  );
}

/* ---------- Outcome picker (cents, sans, strong selected state) ---------- */

function OutcomePicker({
  tokens,
  selected,
  side,
  onSelect,
}: {
  tokens: Erc1155Token[];
  selected: string;
  side: OrderSide;
  onSelect: (label: string) => void;
}) {
  const results = useQueries({
    queries: tokens.map(([tokenId]) => ({
      queryKey: ["book", tokenId],
      queryFn: () => getBook(tokenId),
      refetchInterval: 5000,
      refetchIntervalInBackground: false,
    })),
  });

  return (
    <div
      role="tablist"
      aria-label="Outcomes"
      className="grid gap-2.5"
      style={{
        gridTemplateColumns: `repeat(${tokens.length}, minmax(0, 1fr))`,
      }}
    >
      {tokens.map(([id, label], i) => {
        const bookData = results[i]?.data as OrderBookSummary | undefined;
        // Show the price you'd actually trade at for the current side: the
        // best ask when buying, the best bid when selling — not the mid.
        const price =
          side === "BUY"
            ? bestAsk(bookData?.asks ?? [])
            : bestBid(bookData?.bids ?? []);
        // price is dollars in [0,1] → convert to cents for display
        const cents = price !== null ? price * 100 : null;
        const isActive = label === selected;
        const isPositive = i === 0;
        const tone = isPositive ? "emerald" : "rose";
        return (
          <button
            key={id}
            role="tab"
            type="button"
            aria-selected={isActive}
            onClick={() => onSelect(label)}
            className={cn(
              "flex items-center justify-center rounded-xl border px-2.5 py-2.5 text-center transition-all",
              "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
              isActive
                ? tone === "emerald"
                  ? "border-emerald-500/70 bg-emerald-500/25 text-emerald-700 dark:text-emerald-200"
                  : "border-rose-500/70 bg-rose-500/25 text-rose-700 dark:text-rose-200"
                : "border-border bg-muted text-muted-foreground hover:border-foreground/30 hover:text-foreground",
            )}
          >
            <span className="text-[15px] font-semibold leading-none">
              {label} {cents !== null ? Math.round(cents) : "—"}¢
            </span>
          </button>
        );
      })}
    </div>
  );
}

/* ---------- Buy / Sell (neutral segmented) ---------- */

function SegmentedSide({
  side,
  onSelect,
}: {
  side: OrderSide;
  onSelect: (next: OrderSide) => void;
}) {
  return (
    <div className="flex items-end gap-4">
      {(["BUY", "SELL"] as const).map((s) => {
        const active = side === s;
        return (
          <button
            key={s}
            type="button"
            onClick={() => onSelect(s)}
            className={cn(
              "relative pb-1.5 text-[16px] font-semibold leading-none transition-all",
              active
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            {s === "BUY" ? "Buy" : "Sell"}
            <span
              aria-hidden
              className={cn(
                "absolute -bottom-0.5 left-0 right-0 h-[2px] rounded-full",
                active ? "bg-foreground" : "bg-transparent",
              )}
            />
          </button>
        );
      })}
    </div>
  );
}

/* ---------- Compact field shell ---------- */

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-background px-3 py-2.5 transition-colors focus-within:border-foreground/45">
      <span className="text-[12px] text-muted-foreground">{label}</span>
      {children}
    </div>
  );
}

function CentsStepper({
  cents,
  onChange,
  onStep,
  disabled,
}: {
  cents: string;
  onChange: (next: string) => void;
  onStep: (delta: number) => void;
  disabled?: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  // Put the cursor back where the typing left it. The mask can hand back a
  // string of a different length than the one the field held a moment ago —
  // shorter when it refuses a keystroke, longer when the third digit becomes
  // a decimal — and React writes that string into the input, which parks the
  // cursor at the end. Left alone, a refused keystroke would quietly move the
  // cursor out from under the trader: their next digit would append to the
  // price they were editing rather than land where they were pointing, so
  // clicking into "50" to make it 49 would arrive at 50.9.
  const restoreCaret = (typed: string, at: number) => {
    queueMicrotask(() => {
      const node = inputRef.current;
      if (!node || document.activeElement !== node) return;
      const shifted = at + (node.value.length - typed.length);
      const pos = Math.max(0, Math.min(node.value.length, shifted));
      node.setSelectionRange(pos, pos);
    });
  };

  return (
    <div className="flex items-center gap-3 rounded-xl border border-border px-3 py-2">
      <button
        type="button"
        onClick={() => onStep(-STEP_CENTS)}
        disabled={disabled}
        className="text-[16px] text-foreground disabled:opacity-50"
      >
        -
      </button>
      <span className="inline-flex items-center gap-0.5">
        <input
          ref={inputRef}
          type="text"
          // Decimal, not numeric: tenths of a cent are enterable, so the
          // phone keyboard has to offer the point that types them.
          inputMode="decimal"
          value={cents}
          onChange={(e) => {
            const typed = e.target.value;
            const at = e.target.selectionStart ?? typed.length;
            onChange(typed);
            restoreCaret(typed, at);
          }}
          disabled={disabled}
          // A fixed width, not one that follows the text. This box sits at the
          // left of a right-pinned row, so a width that grew with "49.9" and
          // shrank with "49" would drag the "-" button out from under the
          // cursor that had just pressed it — on every whole-cent crossing,
          // which a 0.1¢ step reaches on the first click.
          className={`w-11 bg-transparent text-center text-[14px] font-semibold tabular-nums outline-none ${cents && cents !== "0" ? "text-foreground" : "text-muted-foreground"}`}
        />
        <span className={`text-[14px] font-semibold ${cents && cents !== "0" ? "text-foreground" : "text-muted-foreground"}`}>¢</span>
      </span>
      <button
        type="button"
        onClick={() => onStep(STEP_CENTS)}
        disabled={disabled}
        className="text-[16px] text-foreground disabled:opacity-50"
      >
        +
      </button>
    </div>
  );
}

function SharesInput({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
}) {
  return (
    <input
      id={id}
      type="text"
      inputMode="decimal"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled}
      placeholder="0"
      className={`h-10 w-[148px] rounded-xl border border-border bg-background px-4 text-right text-[14px] font-semibold tabular-nums outline-none placeholder:text-muted-foreground/50 disabled:opacity-60 ${value && value !== "0" ? "text-foreground" : "text-muted-foreground"}`}
    />
  );
}

function QuickShareChips({ onPick }: { onPick: (delta: number) => void }) {
  const chips = [-100, -10, 10, 20, 100];
  return (
    <div className="flex flex-wrap justify-end gap-1.5">
      {chips.map((chip) => (
        <button
          key={chip}
          type="button"
          onClick={() => onPick(chip)}
          className={cn(
            "rounded-xl border border-border px-2.5 py-1 text-[12px] font-semibold text-muted-foreground transition-colors hover:bg-muted",
            chip > 0 && "text-primary",
          )}
        >
          {chip > 0 ? `+${chip}` : chip}
        </button>
      ))}
    </div>
  );
}

/* ---------- Locale-stable decimal input ---------- */

function ValueInput({
  id,
  value,
  onChange,
  disabled,
}: {
  id: string;
  value: string;
  onChange: (next: string) => void;
  disabled?: boolean;
}) {
  return (
    // The dollar sign says what the number is, so the field needs no "apUSD"
    // over it and no "to spend" beside it — two labels for a fact one glyph
    // carries. It sits in front of the input rather than inside the value, so
    // what the field holds stays a plain number to parse.
    <div className="flex items-baseline gap-1">
      <span
        className={`text-[28px] font-semibold ${
          value ? "text-foreground" : "text-muted-foreground/60"
        }`}
      >
        $
      </span>
      <input
        id={id}
        type="text"
        inputMode="decimal"
        autoComplete="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        className="min-w-0 flex-1 bg-transparent text-[28px] font-semibold tabular-nums text-foreground outline-none placeholder:text-muted-foreground/60 disabled:opacity-60"
        placeholder="0"
      />
    </div>
  );
}


