// @vitest-environment jsdom
//
// Click-level tests for the Claim button, mounted with react-dom and `act`
// (the project has no @testing-library/react), against a stubbed `fetch`, a
// real QueryClient and a mocked toast. What the server does with a claim is
// pinned in the Python tests; this is what the button does with the answer.
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ClaimButton } from "./ClaimButton";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// React 18 only batches a test's updates into `act` when told it is in one.
(
  globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
).IS_REACT_ACT_ENVIRONMENT = true;

const ADDRESS = "0xabc0000000000000000000000000000000000001";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** The body FastAPI sends for every domain error: `{"detail": "<text>"}`. */
const detail = (text: string) => JSON.stringify({ detail: text });

let container: HTMLElement;
let root: Root;
let queryClient: QueryClient;
let fetchMock: ReturnType<typeof vi.fn>;
let invalidated: unknown[][];

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient();
  invalidated = [];
  const invalidate = queryClient.invalidateQueries.bind(queryClient);
  vi.spyOn(queryClient, "invalidateQueries").mockImplementation((filters) => {
    invalidated.push([...(filters?.queryKey ?? [])]);
    return invalidate(filters);
  });
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

/** Mounts one Claim button per condition id, all under one QueryClient: the
 *  rows of one profile page. */
function mount(...conditionIds: string[]): void {
  const tree: ReactNode = (
    <QueryClientProvider client={queryClient}>
      {conditionIds.map((conditionId) => (
        <ClaimButton
          key={conditionId}
          conditionId={conditionId}
          userAddress={ADDRESS}
        />
      ))}
    </QueryClientProvider>
  );
  act(() => root.render(tree));
}

const buttons = () =>
  Array.from(container.querySelectorAll<HTMLButtonElement>("button"));

/** Lets pending timers and promise chains run inside `act`, until `done`. */
async function until(done: () => boolean): Promise<void> {
  for (let i = 0; i < 200 && !done(); i++) {
    await act(async () => {
      await sleep(5);
    });
  }
  expect(done()).toBe(true);
}

describe("ClaimButton", () => {
  // Auto-redeem is on by default, so a Claim button the page still shows is
  // often one a background pass has already paid. The server then answers
  // 400; the dead button has to go with the next refetch, not stay for the
  // 10 s the positions query is considered fresh.
  it.each([
    "nothing to claim",
    "market is not resolved on chain yet",
    "the claim reverted on chain",
  ])("T3: a 400 (%s) refreshes the positions lists", async (reason) => {
    fetchMock.mockResolvedValueOnce(
      new Response(detail(reason), { status: 400 }),
    );
    mount("0xcond");

    await act(async () => {
      buttons()[0]?.click();
    });
    await until(() => vi.mocked(toast.error).mock.calls.length > 0);

    expect(invalidated).toContainEqual(["positions", ADDRESS]);
    expect(invalidated).toContainEqual(["closed-positions", ADDRESS]);
  });

  it("T4: claims clicked on several rows queue instead of racing into a 409", async () => {
    // The server holds one transaction lock per ACCOUNT, but each row has its
    // own button. Clicking Claim on rows 2 and 3 while row 1 is in flight must
    // not send them at once: they would meet the held lock and toast "A claim
    // is already in progress." for a claim that is about to be paid.
    let inFlight = 0;
    fetchMock.mockImplementation(async () => {
      if (inFlight > 0) {
        return new Response(
          detail("another transaction for this account is in progress"),
          { status: 409 },
        );
      }
      inFlight += 1;
      await sleep(30);
      inFlight -= 1;
      return new Response("{}", { status: 200 });
    });
    mount("0xone", "0xtwo", "0xthree");

    await act(async () => {
      for (const button of buttons()) button.click();
    });
    // Every claim ends in one toast, whichever way it went.
    await until(
      () =>
        vi.mocked(toast.success).mock.calls.length +
          vi.mocked(toast.error).mock.calls.length >=
        3,
    );

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(toast.success).toHaveBeenCalledTimes(3);
    expect(toast.error).not.toHaveBeenCalled();
  });
});
