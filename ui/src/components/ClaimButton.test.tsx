// @vitest-environment jsdom
//
// Click-level tests for the Claim button: react-dom + `act` (the project has no
// @testing-library/react), a stubbed `fetch`, a real QueryClient, a mocked toast.
// What the server does with a claim is pinned in the Python tests.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { toast } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ClaimButton } from "./ClaimButton";

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

// React 18 only batches a test's updates into `act` when told it is in one.
Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });

const ADDRESS = "0xabc0000000000000000000000000000000000001";
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
/** The body FastAPI sends for every domain error: `{"detail": "<text>"}`. */
const detail = (text: string) => JSON.stringify({ detail: text });

let container: HTMLElement;
let root: Root;
let queryClient: QueryClient;
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  queryClient = new QueryClient();
  vi.spyOn(queryClient, "invalidateQueries"); // calls through
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

/** One Claim button per condition id under one QueryClient: a profile page's rows. */
function mount(...conditionIds: string[]): void {
  act(() =>
    root.render(
      <QueryClientProvider client={queryClient}>
        {conditionIds.map((id) => (
          <ClaimButton key={id} conditionId={id} userAddress={ADDRESS} />
        ))}
      </QueryClientProvider>,
    ),
  );
}

const buttons = () =>
  Array.from(container.querySelectorAll<HTMLButtonElement>("button"));
const toasts = () =>
  vi.mocked(toast.success).mock.calls.length +
  vi.mocked(toast.error).mock.calls.length;

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
  // often one a background pass already paid: the server answers 400 and the
  // dead button has to go with the next refetch, not stay for the 10 s the
  // positions query counts as fresh.
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

    for (const key of ["positions", "closed-positions"]) {
      expect(queryClient.invalidateQueries).toHaveBeenCalledWith(
        expect.objectContaining({ queryKey: [key, ADDRESS] }),
      );
    }
  });

  // The server holds one transaction lock per ACCOUNT but each row has its own
  // button: Claim on rows 2 and 3 clicked while row 1 is in flight must not go
  // out at once and meet the held lock ("A claim is already in progress.").
  it("T4: claims clicked on several rows queue instead of racing into a 409", async () => {
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
    await until(() => toasts() >= 3); // every claim ends in one toast

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(toast.success).toHaveBeenCalledTimes(3);
    expect(toast.error).not.toHaveBeenCalled();
  });
});
