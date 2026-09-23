import { site } from "../content/site";

export const activeMarkets = async (): Promise<number | undefined> => {
  try {
    const response = await fetch(`${site.api}/markets/stats`, { signal: AbortSignal.timeout(2000) });
    const { active }: { active?: unknown } = await response.json();
    return response.ok && typeof active === "number" ? active : undefined;
  } catch {
    return undefined;
  }
};
