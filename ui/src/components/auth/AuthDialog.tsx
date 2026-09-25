import { useEffect, useState, type FormEvent } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuth } from "@/auth/useAuth";
import {
  buildAuthorizeUrl,
  CALLBACK_PATH,
  createState,
  STATE_KEY,
  WORKOS_CLIENT_ID,
} from "@/lib/workosAuth";
import {
  CODE_LENGTH,
  canResend,
  isCompleteCode,
  normaliseCode,
  resendSecondsLeft,
  sendCodeErrorMessage,
  signInErrorMessage,
  statusOf,
} from "@/components/auth/codeFlow";

const EMAIL_RE = /.+@.+\..+/;

/** Which of the two mailed-code steps the dialog is showing. */
type Step = "email" | "code";

export function AuthDialog() {
  const { dialogOpen, closeDialog, sendCode, signInWithCode } = useAuth();

  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<Step>("email");
  const [lastSentAt, setLastSentAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Reset everything whenever the dialog re-opens.
  useEffect(() => {
    if (dialogOpen) {
      setEmail("");
      setCode("");
      setStep("email");
      setLastSentAt(null);
      setError(null);
      setSubmitting(false);
    }
  }, [dialogOpen]);

  // Drives the resend countdown. Only ticks on the code step, so the rest of
  // the app never re-renders on a timer it cannot see.
  useEffect(() => {
    if (step !== "code") return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [step]);

  // Shared by the first send and every resend. Only advances to the code step
  // when the send actually succeeded — otherwise the user is asked for a code
  // that was never mailed.
  const requestCode = async (address: string) => {
    setSubmitting(true);
    try {
      await sendCode(address);
      setLastSentAt(Date.now());
      setCode("");
      setStep("code");
    } catch (err) {
      setError(sendCodeErrorMessage(statusOf(err)));
    } finally {
      setSubmitting(false);
    }
  };

  const onSendCode = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);
    if (!EMAIL_RE.test(email)) {
      setError("Please enter a valid email address.");
      return;
    }
    await requestCode(email);
  };

  const onVerifyCode = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);
    if (!isCompleteCode(code)) {
      setError(`Enter the ${CODE_LENGTH}-digit code from the email.`);
      return;
    }
    setSubmitting(true);
    try {
      await signInWithCode(email, code);
    } catch (err) {
      setError(signInErrorMessage(statusOf(err)));
    } finally {
      setSubmitting(false);
    }
  };

  const onResend = async () => {
    setError(null);
    await requestCode(email);
  };

  const title = step === "email" ? "Sign in" : "Check your email";

  const secondsLeft = resendSecondsLeft(lastSentAt, now);
  const resendReady = canResend(lastSentAt, now) && !submitting;

  // The product's own label device — mono micro-caps, the same one the market
  // cards use for anything that labels data.
  const fieldLabelClass =
    "font-mono text-[10px] uppercase tracking-[0.22em] text-muted-foreground";
  const linkClass =
    "font-medium text-blue-600 underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:opacity-60 dark:text-blue-400";

  const errorBlock = error && (
    <p
      role="alert"
      className="text-sm text-destructive"
      data-testid="auth-error"
    >
      {error}
    </p>
  );

  const emailField = (
    <div className="space-y-1.5">
      <Label htmlFor="auth-email" className={fieldLabelClass}>
        Email
      </Label>
      <Input
        id="auth-email"
        type="email"
        autoComplete="email"
        autoFocus
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        disabled={submitting}
      />
    </div>
  );

  const onGoogleAuthorize = () => {
    // Narrowing an imported const doesn't survive into this closure (see
    // GoogleSignInButton), so re-check rather than trust the render guard
    // that decided whether this block exists at all.
    if (!WORKOS_CLIENT_ID) return;
    const state = createState();
    sessionStorage.setItem(STATE_KEY, state);
    window.location.href = buildAuthorizeUrl({
      clientId: WORKOS_CLIENT_ID,
      redirectUri: new URL(CALLBACK_PATH, location.origin).href,
      provider: "GoogleOAuth",
      state,
    });
  };

  const googleBlock = WORKOS_CLIENT_ID && (
    <div className="space-y-4">
      {/* Below the fields, not above them: the emailed code is the primary
          path, and the divider reads as "or, instead of the above". */}
      <div className="flex items-center gap-3">
        <span className="h-px flex-1 bg-border" />
        <span className={fieldLabelClass}>or</span>
        <span className="h-px flex-1 bg-border" />
      </div>
      <Button
        type="button"
        variant="outline"
        className="w-full"
        onClick={onGoogleAuthorize}
      >
        Continue with Google
      </Button>
    </div>
  );

  return (
    <Dialog
      open={dialogOpen}
      onOpenChange={(next) => {
        if (!next) closeDialog();
      }}
    >
      {/* A dialog that touches both edges of a phone screen reads as a page
          that failed to load, not as a panel. */}
      <DialogContent
        className="w-[calc(100%-2rem)] sm:max-w-md"
        aria-describedby={undefined}
      >
        {/* Centred, and the only thing at this size. With the explanatory copy
            gone the dialog is a title, a field and a button, so the title has
            to carry the weight the paragraph used to. `pt-1` because Radix
            pins its close button top-right and an optically centred line wants
            a little air above it. */}
        <DialogHeader className="pt-1 text-center sm:text-center">
          <DialogTitle className="text-2xl font-semibold tracking-tight">
            {title}
          </DialogTitle>
        </DialogHeader>

        {step === "email" ? (
          <form onSubmit={onSendCode} className="space-y-4">
            {emailField}
            {errorBlock}
            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? "Sending…" : "Send code"}
            </Button>
            {googleBlock}
          </form>
        ) : (
          <form onSubmit={onVerifyCode} className="space-y-4">
            {/* Kept, and centred under the title: this line is not an
                explanation, it names the address to go and look in. */}
            <p className="text-center text-sm text-muted-foreground">
              We sent a {CODE_LENGTH}-digit code to{" "}
              <span className="font-medium text-foreground">{email}</span>.
            </p>
            <div className="space-y-1.5">
              <Label htmlFor="auth-code" className={fieldLabelClass}>
                Code
              </Label>
              <Input
                id="auth-code"
                // one-time-code lets the OS offer the code straight from the
                // mail; inputMode gets the numeric keypad on a phone.
                autoComplete="one-time-code"
                inputMode="numeric"
                autoFocus
                required
                maxLength={CODE_LENGTH}
                className="font-mono text-lg tracking-[0.4em]"
                value={code}
                // Normalised on the way in, not on submit: a paste of
                // "515 627" should look right in the field immediately.
                onChange={(e) => setCode(normaliseCode(e.target.value))}
                disabled={submitting}
              />
            </div>
            {errorBlock}
            <Button
              type="submit"
              className="w-full"
              disabled={submitting || !isCompleteCode(code)}
            >
              {submitting ? "Signing in…" : "Sign in"}
            </Button>
            <p className="pt-1 text-center text-sm text-muted-foreground">
              {resendReady ? (
                <button
                  type="button"
                  className={linkClass}
                  onClick={() => void onResend()}
                >
                  Send a new code
                </button>
              ) : (
                <span>Send a new code in {secondsLeft}s</span>
              )}
            </p>
            <p className="text-center text-sm text-muted-foreground">
              <button
                type="button"
                className={linkClass}
                onClick={() => {
                  setStep("email");
                  setError(null);
                }}
                disabled={submitting}
              >
                Use a different email
              </button>
            </p>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
