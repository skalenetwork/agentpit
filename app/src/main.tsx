import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, Navigate, Outlet, RouterProvider, redirect } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { safeRedirect } from "./auth";
import { Account } from "./pages/account";
import { AgentPage, isTab } from "./pages/agent";
import { Agents } from "./pages/agents";
import { AuthCallback } from "./pages/auth-callback";
import { SignIn } from "./pages/sign-in";
import { ApiError, signedIn, subscribe } from "./session";
import { Shell } from "./shell";
import "./styles.css";

const root = createRootRoute({ component: Outlet, notFoundComponent: () => <Navigate to="/" /> });

const console = createRoute({
  getParentRoute: () => root,
  id: "console",
  component: Shell,
  beforeLoad: ({ location }) => {
    if (!signedIn()) throw redirect({ to: "/sign-in", search: { redirect: location.href === "/" ? undefined : location.href } });
  },
});

const routes = root.addChildren([
  console.addChildren([
    createRoute({ getParentRoute: () => console, path: "/", component: Agents }),
    createRoute({
      getParentRoute: () => console,
      path: "/agents/$address",
      component: AgentPage,
      validateSearch: (search: Record<string, unknown>) => ({ tab: isTab(search.tab) ? search.tab : undefined }),
    }),
    createRoute({ getParentRoute: () => console, path: "/account", component: Account }),
  ]),
  createRoute({
    getParentRoute: () => root,
    path: "/sign-in",
    component: SignIn,
    validateSearch: (search: Record<string, unknown>) => ({ redirect: safeRedirect(search.redirect) }),
    beforeLoad: () => {
      if (signedIn()) throw redirect({ to: "/" });
    },
  }),
  createRoute({ getParentRoute: () => root, path: "/auth/callback", component: AuthCallback }),
]);

const router = createRouter({ routeTree: routes });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}

const queries = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: (failures, error) => failures < 2 && !(error instanceof ApiError && error.status < 500) } },
});

subscribe(() => {
  if (!signedIn()) queries.clear();
  void router.invalidate();
});

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <QueryClientProvider client={queries}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
