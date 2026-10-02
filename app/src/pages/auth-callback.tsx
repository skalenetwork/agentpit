import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { callbackCode, callbackError, takeRedirect, takeState } from "../auth";
import { open, type Session, start } from "../session";
import { Button, Logo } from "../ui";

let exchange: Promise<Session> | undefined;

const complete = () => {
  const code = callbackCode(location.search, takeState());
  exchange ??= code ? open<Session>("/auth/callback", "POST", { code }) : Promise.reject();
  return exchange;
};

export const AuthCallback = () => {
  const navigate = useNavigate();
  const [fault, setFault] = useState<string>();

  useEffect(() => {
    complete().then(
      (session) => {
        start(session);
        return navigate({ href: takeRedirect(), replace: true });
      },
      (error) => setFault(callbackError(error)),
    );
  }, [navigate]);

  return (
    <main className="grid min-h-dvh place-items-center px-5 py-16">
      <div className="grid w-full max-w-form justify-items-center gap-6 text-center">
        <Logo height={22} />
        <p className="text-muted">{fault ?? "Signing you in"}</p>
        {fault && (
          <Button variant="primary" size="lg" onClick={() => navigate({ to: "/sign-in", search: { redirect: undefined }, replace: true })}>
            Back to sign in
          </Button>
        )}
      </div>
    </main>
  );
};
