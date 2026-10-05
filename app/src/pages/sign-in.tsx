import { useMutation } from "@tanstack/react-query";
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useEffect, useState } from "react";
import { CODE_LENGTH, normaliseCode, sendCodeError, signInError, startGoogle } from "../auth";
import { open, type Session, start } from "../session";
import { Button, Fault, Input, Logo } from "../ui";

const RESEND_MS = 60_000;
const AGENT_PATH = /^\/agents\/(0x[0-9a-fA-F]{40})/;
const route = getRouteApi("/sign-in");
const google = startGoogle;

const Google = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48z" />
  </svg>
);

export const SignIn = () => {
  const { redirect } = route.useSearch();
  const agent = redirect?.match(AGENT_PATH)?.[1];
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [sentAt, setSentAt] = useState<number>();
  const [resendable, setResendable] = useState(false);
  const send = useMutation({ mutationFn: () => open("/auth/code", "POST", { email }), onSuccess: () => setSentAt(Date.now()) });
  const signIn = useMutation({
    mutationFn: () => open<Session>("/auth/session", "POST", { email, code }),
    onSuccess: (session) => {
      start(session);
      return navigate({ href: redirect ?? "/" });
    },
  });
  const busy = send.isPending || signIn.isPending;

  useEffect(() => {
    setResendable(false);
    if (!sentAt) return;
    const timer = setTimeout(() => setResendable(true), RESEND_MS);
    return () => clearTimeout(timer);
  }, [sentAt]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    (sentAt ? signIn : send).mutate();
  };

  const restart = () => {
    setSentAt(undefined);
    setCode("");
    signIn.reset();
  };

  return (
    <main className="grid min-h-dvh place-items-center px-4 py-16">
      <form onSubmit={submit} className="grid w-full max-w-form gap-3">
        <div className="mb-7 flex justify-center">
          <Logo height={22} />
        </div>
        {sentAt ? (
          <>
            <p className="text-center text-muted">Enter the code sent to {email}</p>
            <Input
              key="code"
              value={code}
              onChange={(event) => setCode(normaliseCode(event.target.value))}
              placeholder="000000"
              inputMode="numeric"
              autoComplete="one-time-code"
              aria-label="6-digit code"
              autoFocus
              className="text-center tracking-[0.4em] tabular-nums"
            />
            <Button type="submit" variant="primary" size="lg" disabled={busy || code.length < CODE_LENGTH}>
              Continue
            </Button>
            <div className="grid grid-cols-2 gap-1">
              <Button variant="ghost" size="lg" disabled={busy || !resendable} onClick={() => send.mutate()}>
                Send a new code
              </Button>
              <Button variant="ghost" size="lg" onClick={restart}>
                Use another email
              </Button>
            </div>
          </>
        ) : (
          <>
            <Input key="email" type="email" required value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email" autoComplete="email" aria-label="Email" autoFocus />
            <Button type="submit" variant="primary" size="lg" disabled={busy}>
              Continue
            </Button>
            {google && (
              <Button size="lg" onClick={() => google(redirect)}>
                <Google />
                Continue with Google
              </Button>
            )}
          </>
        )}
        {signIn.isError ? <Fault>{signInError(signIn.error)}</Fault> : send.isError && <Fault>{sendCodeError(send.error)}</Fault>}
        {agent && (
          <a
            href={`https://agentpit.dev/agents/${agent}`}
            className="mt-4 justify-self-center text-ink underline decoration-line underline-offset-4 transition-colors duration-150 hover:decoration-ink"
          >
            View public page
          </a>
        )}
      </form>
    </main>
  );
};
