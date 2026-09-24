import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bot, Check, Copy, Eye, EyeOff, Fuel, Key, KeyRound, Lock, Mail, User, X } from "lucide-react";
import { toast } from "sonner";
import {
  changePasswordRequest,
  exportPrivateKeyRequest,
  sendExportCodeRequest,
  setAutoRedeemRequest,
  type UserPublic,
  updateHandleRequest,
} from "@/api/auth";
import { ApiError } from "@/api/client";
import { useMyAgents } from "@/api/agents";
import { useCredits } from "@/api/portfolio";
import { useAuth } from "@/auth/useAuth";
import {
  CODE_LENGTH,
  canResend,
  isCompleteCode,
  normaliseCode,
  resendSecondsLeft,
} from "@/components/auth/codeFlow";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import {
  exportErrorMessage,
  sendExportCodeErrorMessage,
} from "@/lib/exportKeyError";
import { formatCredits, formatLongDate } from "@/lib/format";
import { cn } from "@/lib/utils";

export function SettingsPage() {
  const { user, setUser } = useAuth();
  if (!user) return null;
  return (
    <section className="mx-auto max-w-5xl space-y-6">
      <div className="space-y-6">
        <h1 className="text-3xl font-semibold tracking-tight">Settings</h1>
        <Card className="rounded-xl">
          <CardContent className="p-0">
            <UsernameRow user={user} onUpdated={setUser} />
            <div className="flex items-center gap-4 border-b p-4">
              <Mail className="size-5 text-muted-foreground" />
              <div className="flex-1">
                <p className="text-sm font-medium">Email</p>
                <p className="truncate text-sm text-muted-foreground">
                  {user.email}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-4 border-b p-4">
              <Lock className="size-5 shrink-0 text-muted-foreground" />
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium">Address</p>
                <p className="break-all font-mono text-sm text-muted-foreground">
                  {user.eth_address}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  Import it into MetaMask to fund this wallet.
                </p>
                <CreditsLine />
              </div>
              <ExportKeyButton user={user} />
            </div>
            <AutoRedeemRow user={user} onUpdated={setUser} />
            <ApiKeyRow apiKey={user.api_key} />
            {/* Only the accounts that predate the cutover have a password.
                `link_workos_identity` preserves their PASSWORD_HASH on
                purpose (it is what the rollback rests on), so `has_password`
                stays true for them and the form keeps working. Every account
                created since signs in by mailed code with a null hash, and
                for those `change_password` raises "this account signs in with
                Google" -> 400, which the handler below renders as "New
                password must be different from current password" -- three
                fields and a falsehood about a password they never had. */}
            {user.has_password && <ChangePasswordRow />}
          </CardContent>
        </Card>
        <AgentsCard />
      </div>
    </section>
  );
}

function AgentsCard() {
  const { data: agents } = useMyAgents();
  return (
    <Card className="rounded-xl">
      <CardContent className="p-0">
        <div className="flex items-center gap-4 border-b p-4 last:border-b-0">
          <Bot className="size-5 shrink-0 text-muted-foreground" aria-hidden />
          <div className="flex-1">
            <p className="text-sm font-medium">Agents</p>
            {agents?.length === 0 && (
              <p className="text-xs text-muted-foreground">
                No agents yet.{" "}
                <a
                  href="https://agentpit.dev/start"
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium text-blue-600 underline-offset-4 hover:underline dark:text-blue-400"
                >
                  Connect one
                </a>
              </p>
            )}
          </div>
        </div>
        {agents?.map((agent) => (
          <Link
            key={agent.eth_address}
            to={`/profile?agent=${agent.eth_address}`}
            className="flex items-center gap-4 border-b p-4 last:border-b-0 hover:bg-muted/20"
          >
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium">
                {agent.handle ?? agent.app}
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {agent.app}
              </p>
            </div>
            <p className="shrink-0 text-xs text-muted-foreground">
              Created {formatLongDate(agent.created_at)}
            </p>
          </Link>
        ))}
      </CardContent>
    </Card>
  );
}

/** What pays for a claim, in the same muted style as the MetaMask hint
 *  above it — the address row is the wallet, and this is what's in it to
 *  spend on gas. */
function CreditsLine() {
  const { data: credits } = useCredits();
  return (
    <p className="mt-1 text-xs text-muted-foreground">
      {credits != null ? `${formatCredits(credits)} credits` : "— credits"}
    </p>
  );
}

type UsernameRowProps = {
  user: UserPublic;
  onUpdated: (user: UserPublic) => void;
};

function UsernameRow({ user, onUpdated }: UsernameRowProps) {
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const displayName = user.handle || user.user_id;
  const [value, setValue] = useState(displayName);

  const startEdit = () => {
    setValue(displayName);
    setError("");
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setValue(displayName);
    setError("");
  };

  const save = async () => {
    const next = value.trim();
    if (!/^[a-zA-Z0-9_]{1,15}$/.test(next)) {
      setError(
        "Username must be 1-15 chars using letters, numbers, or underscores.",
      );
      return;
    }
    if (next === displayName) {
      setEditing(false);
      setError("");
      return;
    }

    setSaving(true);
    setError("");
    try {
      const updated = await updateHandleRequest(next);
      onUpdated(updated);
      setEditing(false);
      toast.success("Username updated.");
    } catch (err) {
      let message = "Failed to update username.";
      if (err instanceof ApiError) {
        if (err.status === 409) {
          message = "That username is already taken.";
        } else if (err.status === 400 || err.status === 422) {
          message =
            "Username must be 1-15 chars using letters, numbers, or underscores.";
        }
      }
      setError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="border-b p-4">
      <div className="flex items-center gap-4">
        <User className="size-5 text-muted-foreground" aria-hidden />
        <div className="flex flex-1 items-center justify-between gap-4">
          <div>
            <p className="text-sm font-medium">Username</p>
            {!editing && (
              <p className="truncate text-sm text-muted-foreground">
                {displayName}
              </p>
            )}
          </div>

          {editing ? (
            <div className="flex items-center gap-2">
              <Input
                value={value}
                onChange={(e) => setValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void save();
                  }
                  if (e.key === "Escape") {
                    e.preventDefault();
                    cancelEdit();
                  }
                }}
                disabled={saving}
                autoFocus
                className="w-64 sm:w-72"
              />
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="size-9 rounded-full"
                onClick={cancelEdit}
                disabled={saving}
                aria-label="Cancel username edit"
              >
                <X className="size-4" />
              </Button>
              <Button
                type="button"
                size="icon"
                className="size-9 rounded-full"
                onClick={() => void save()}
                disabled={saving}
                aria-label="Save username"
              >
                <Check className="size-4" />
              </Button>
            </div>
          ) : (
            <Button size="sm" variant="outline" onClick={startEdit}>
              Edit
            </Button>
          )}
        </div>
      </div>
      {error && <p className="mt-2 text-xs text-red-500">{error}</p>}
    </div>
  );
}

function ApiKeyRow({ apiKey }: { apiKey: string }) {
  const [revealed, setRevealed] = useState(false);
  const masked = apiKey
    ? `${apiKey.slice(0, 6)}${"•".repeat(10)}${apiKey.slice(-4)}`
    : "—";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(apiKey);
      toast.success("API key copied to clipboard.");
    } catch {
      toast.error("Could not copy API key.");
    }
  };

  return (
    <div className="flex items-center gap-4 border-b p-4">
      <Key className="size-5 shrink-0 text-muted-foreground" />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">Agentpit API Key</p>
        <p
          className="truncate font-mono text-sm text-muted-foreground"
          title={revealed ? apiKey : undefined}
        >
          {revealed ? apiKey : masked}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-9 rounded-full"
          onClick={() => setRevealed((r) => !r)}
          aria-label={revealed ? "Hide API key" : "Reveal API key"}
        >
          {revealed ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
        </Button>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-9 rounded-full"
          onClick={() => void copy()}
          aria-label="Copy API key"
        >
          <Copy className="size-4" />
        </Button>
      </div>
    </div>
  );
}

type ExportedKey = { private_key: string; eth_address: string };

/** Which of the three export-dialog screens is showing: the warning, the
 *  mailed code, or the key itself. */
type ExportStep = "warning" | "code" | "result";

function ExportKeyButton({ user }: { user: UserPublic }) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<ExportStep>("warning");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [sendingCode, setSendingCode] = useState(false);
  const [loading, setLoading] = useState(false);
  const [lastSentAt, setLastSentAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [result, setResult] = useState<ExportedKey | null>(null);

  const reset = () => {
    setStep("warning");
    setCode("");
    setError("");
    setSendingCode(false);
    setLoading(false);
    setLastSentAt(null);
    setResult(null);
  };

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (!next) reset();
  };

  // Drives the resend countdown, same as AuthDialog: only while the code
  // field is showing, so the rest of the page never re-renders on a timer it
  // cannot see.
  useEffect(() => {
    if (step !== "code") return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [step]);

  const requestCode = async () => {
    setError("");
    setSendingCode(true);
    try {
      await sendExportCodeRequest();
      setLastSentAt(Date.now());
      setCode("");
      setStep("code");
    } catch (err) {
      // Not `exportErrorMessage`: no code has been typed at this point, so its
      // 401 wording ("that code is wrong or expired") describes something that
      // has not happened yet.
      const message =
        err instanceof ApiError
          ? sendExportCodeErrorMessage(err.status)
          : "Could not send the code. Try again in a moment.";
      setError(message);
    } finally {
      setSendingCode(false);
    }
  };

  const submit = async () => {
    if (!isCompleteCode(code)) return;
    setError("");
    setLoading(true);
    try {
      const exported = await exportPrivateKeyRequest(code);
      setResult(exported);
      setStep("result");
      // Clear it now rather than leaving it to `reset()` on close — the
      // code otherwise sits in state, a live credential, for as long as the
      // dialog stays open showing the key beside it.
      setCode("");
    } catch (err) {
      const message =
        err instanceof ApiError
          ? exportErrorMessage(err.status, err.body)
          : "Failed to export private key.";
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  };

  const copy = async () => {
    if (!result) return;
    try {
      await navigator.clipboard.writeText(result.private_key);
      toast.success("Private key copied to clipboard.");
    } catch {
      toast.error("Could not copy private key.");
    }
  };

  const secondsLeft = resendSecondsLeft(lastSentAt, now);
  const resendReady = canResend(lastSentAt, now) && !sendingCode;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          Export private key
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Export private key</DialogTitle>
          <DialogDescription>
            Anyone with this key controls the wallet and everything in it. We
            cannot undo an export or move the funds back.
          </DialogDescription>
        </DialogHeader>
        {step === "result" && result ? (
          <div className="flex flex-col gap-3">
            <p className="break-all rounded-md border bg-muted p-3 font-mono text-sm">
              {result.private_key}
            </p>
            <Button type="button" variant="outline" onClick={() => void copy()}>
              <Copy className="mr-2 size-4" />
              Copy private key
            </Button>
          </div>
        ) : step === "code" ? (
          <div className="flex flex-col gap-2">
            <p className="text-sm text-muted-foreground">
              We sent a {CODE_LENGTH}-digit code to{" "}
              <span className="font-medium text-foreground">{user.email}</span>.
            </p>
            <Input
              // one-time-code lets the OS offer the code straight from the
              // mail; inputMode gets the numeric keypad on a phone.
              autoComplete="one-time-code"
              inputMode="numeric"
              maxLength={CODE_LENGTH}
              placeholder="Code"
              className="font-mono text-lg tracking-[0.4em]"
              value={code}
              // Normalised on the way in, not on submit: a paste of
              // "515 627" should look right in the field immediately.
              onChange={(e) => setCode(normaliseCode(e.target.value))}
              autoFocus
              disabled={loading}
              onKeyDown={(e) => {
                if (e.key === "Enter" && isCompleteCode(code) && !loading) {
                  e.preventDefault();
                  void submit();
                }
              }}
            />
            {error && <p className="text-xs text-red-500">{error}</p>}
            <Button
              type="button"
              onClick={() => void submit()}
              disabled={loading || !isCompleteCode(code)}
            >
              Confirm
            </Button>
            <p className="text-center text-xs text-muted-foreground">
              {resendReady ? (
                <button
                  type="button"
                  className="font-medium underline-offset-4 hover:underline"
                  onClick={() => void requestCode()}
                >
                  Send a new code
                </button>
              ) : (
                <span>Send a new code in {secondsLeft}s</span>
              )}
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {error && <p className="text-xs text-red-500">{error}</p>}
            <Button
              type="button"
              onClick={() => void requestCode()}
              disabled={sendingCode}
            >
              {sendingCode ? "Sending…" : "Email me a code"}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

type AutoRedeemRowProps = {
  user: UserPublic;
  onUpdated: (user: UserPublic) => void;
};

function AutoRedeemRow({ user, onUpdated }: AutoRedeemRowProps) {
  const [saving, setSaving] = useState(false);

  const toggle = async () => {
    setSaving(true);
    try {
      const updated = await setAutoRedeemRequest(!user.auto_redeem);
      onUpdated(updated);
      toast.success(
        updated.auto_redeem
          ? "You'll now claim winnings automatically."
          : "You'll claim your own winnings from now on.",
      );
    } catch {
      toast.error("Failed to update claim setting.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex items-center gap-4 border-b p-4">
      <Fuel className="size-5 shrink-0 text-muted-foreground" aria-hidden />
      <div className="flex-1">
        <p className="text-sm font-medium">Claim winnings automatically</p>
        <p className="text-xs text-muted-foreground">
          Claiming costs a small amount of gas from this wallet. With this
          off, you claim your winnings yourself.
        </p>
      </div>
      <button
        type="button"
        role="switch"
        aria-checked={user.auto_redeem}
        aria-label="Claim winnings automatically"
        onClick={() => void toggle()}
        disabled={saving}
        className={cn(
          "inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50",
          user.auto_redeem ? "bg-primary" : "bg-input",
        )}
      >
        <span
          className={cn(
            "pointer-events-none block size-5 rounded-full bg-background shadow-lg transition-transform",
            user.auto_redeem ? "translate-x-5" : "translate-x-0",
          )}
        />
      </button>
    </div>
  );
}

function ChangePasswordRow() {
  const [showForm, setShowForm] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSave = async () => {
    setError("");
    if (!current || !next || !confirm) {
      setError("All fields are required.");
      return;
    }
    if (next !== confirm) {
      setError("Passwords do not match.");
      return;
    }
    if (current === next) {
      setError("New password must be different from current password.");
      return;
    }
    setLoading(true);
    try {
      await changePasswordRequest(current, next);
      toast.success("Password changed successfully.");
      setShowForm(false);
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      let message = "Failed to change password.";
      if (err instanceof ApiError) {
        if (err.status === 401) {
          message = "Current password is incorrect.";
        } else if (err.status === 422) {
          message = "New password must be at least 8 characters.";
        } else if (err.status === 400) {
          message = "New password must be different from current password.";
        }
      }
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = () => {
    setShowForm(false);
    setCurrent("");
    setNext("");
    setConfirm("");
    setError("");
  };

  if (!showForm) {
    return (
      <div className="flex items-center gap-4 p-4">
        <KeyRound className="size-5 text-muted-foreground" />
        <div className="flex flex-1 items-center justify-between">
          <div>
            <p className="text-sm font-medium">Password</p>
            <p className="text-xs text-muted-foreground">
              Change your account password
            </p>
          </div>
          <Button size="sm" variant="outline" onClick={() => setShowForm(true)}>
            Change
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2 border-t p-4">
      <div className="mb-2 flex items-center gap-4">
        <KeyRound className="size-5 text-muted-foreground" />
        <span className="text-sm font-medium">Change Password</span>
      </div>
      <Input
        type="password"
        placeholder="Current password"
        value={current}
        onChange={(e) => setCurrent(e.target.value)}
        autoComplete="current-password"
        className="mb-1"
        autoFocus
      />
      <Input
        type="password"
        placeholder="New password"
        value={next}
        onChange={(e) => setNext(e.target.value)}
        autoComplete="new-password"
        className="mb-1"
      />
      <Input
        type="password"
        placeholder="Confirm new password"
        value={confirm}
        onChange={(e) => setConfirm(e.target.value)}
        autoComplete="new-password"
        className="mb-2"
      />
      {error && <p className="mb-1 text-xs text-red-500">{error}</p>}
      <div className="flex justify-end gap-2">
        <Button
          size="sm"
          variant="ghost"
          onClick={handleCancel}
          disabled={loading}
        >
          Cancel
        </Button>
        <Button size="sm" onClick={handleSave} disabled={loading}>
          Save
        </Button>
      </div>
    </div>
  );
}
