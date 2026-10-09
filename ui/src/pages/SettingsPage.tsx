import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Menu, MenuItem } from "@mui/material";
import { Bot, Check, Copy, Ellipsis, Eye, EyeOff, Fuel, Key, KeyRound, Lock, Mail, User, X } from "lucide-react";
import { toast } from "sonner";
import {
  changePasswordRequest,
  setAutoRedeemRequest,
  type UserPublic,
  updateHandleRequest,
} from "@/api/auth";
import { API_BASE_URL, ApiError } from "@/api/client";
import {
  type AgentSummary,
  useCreateAgent,
  useDeleteAgent,
  useMyAgents,
  useRenameAgent,
} from "@/api/agents";
import { useCredits } from "@/api/portfolio";
import { useAuth } from "@/auth/useAuth";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { formatCredits, formatLongDate } from "@/lib/format";
import { cn } from "@/lib/utils";

export function SettingsPage() {
  const { user, setUser } = useAuth();
  // The credits line under the address is hidden, not deleted: see
  // `CreditsLine`. With this flag false it never renders, so `/me/credits`
  // is never fetched from here either. Turning it back on is one word.
  const showCredits = false;
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
                {showCredits && <CreditsLine />}
              </div>
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

const MENU_SLOTS = {
  paper: {
    sx: {
      mt: 0.5,
      border: "1px solid hsl(var(--border))",
      borderRadius: "0.5rem",
      boxShadow: "none",
      backgroundColor: "hsl(var(--popover))",
      color: "hsl(var(--popover-foreground))",
    },
  },
  list: { sx: { p: 0.5 } },
};

const MENU_ITEM_SX = {
  fontSize: "0.875rem",
  borderRadius: "0.375rem",
  "&:hover": { backgroundColor: "hsl(var(--muted))" },
};

const HANDLE_RULE = /^[a-zA-Z0-9_]{1,15}$/;

function AgentsCard() {
  const { data: agents } = useMyAgents();
  const create = useCreateAgent();
  const keyRef = useRef<HTMLInputElement>(null);
  const apiKey = create.data?.api_key;

  const copyKey = async (key: string) => {
    try {
      await navigator.clipboard.writeText(key);
      toast.success("API key copied to clipboard.");
    } catch {
      keyRef.current?.select();
    }
  };

  const newAgentButton = (
    <Button
      size="sm"
      variant="outline"
      className="shrink-0"
      disabled={create.isPending}
      onClick={() =>
        create.mutate(undefined, {
          onError: () => toast.error("Could not create an agent."),
        })
      }
    >
      {create.isPending ? "Creating…" : "New API agent"}
    </Button>
  );

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
          {agents?.length === 0 && newAgentButton}
        </div>
        {agents?.map((agent) => (
          <AgentRow key={agent.eth_address} agent={agent} />
        ))}
        {agents?.length ? <div className="p-4">{newAgentButton}</div> : null}
      </CardContent>
      <Dialog
        open={apiKey !== undefined}
        onOpenChange={(open) => {
          if (!open) create.reset();
        }}
      >
        <DialogContent onInteractOutside={(e) => e.preventDefault()}>
          <DialogHeader>
            <DialogTitle>New API agent</DialogTitle>
            <DialogDescription>
              Copy this key now. It will not be shown again.
            </DialogDescription>
          </DialogHeader>
          <div className="flex gap-2">
            <Input
              ref={keyRef}
              readOnly
              value={apiKey ?? ""}
              className="font-mono"
            />
            <Button
              type="button"
              variant="outline"
              onClick={() => apiKey && void copyKey(apiKey)}
            >
              <Copy />
              Copy
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            Send it as X-API-Key to the REST API, or as a Bearer token to{" "}
            {API_BASE_URL}/mcp.
          </p>
          <div className="flex justify-end">
            <Button type="button" onClick={() => create.reset()}>
              Done
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

type AgentDialog = "rename" | "delete";

function AgentRow({ agent }: { agent: AgentSummary }) {
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null);
  const [dialog, setDialog] = useState<AgentDialog | null>(null);
  const name = agent.handle ?? agent.runner.label;

  const choose = (next: AgentDialog) => {
    setAnchorEl(null);
    setDialog(next);
  };

  return (
    <div className="flex items-center border-b hover:bg-muted/20">
      <Link
        to={`/profile?agent=${agent.eth_address}`}
        className="flex min-w-0 flex-1 items-center gap-4 p-4"
      >
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-medium">{name}</p>
          <p className="truncate text-xs text-muted-foreground">
            {agent.runner.host
              ? `${agent.runner.label} · ${agent.runner.host}`
              : agent.runner.label}
          </p>
        </div>
        <p className="shrink-0 text-xs text-muted-foreground">
          Created {formatLongDate(agent.created_at)}
        </p>
      </Link>
      <Button
        type="button"
        size="icon"
        variant="ghost"
        className="mr-2 size-9 shrink-0 rounded-full"
        aria-label={`Manage ${name}`}
        onClick={(e) => setAnchorEl(e.currentTarget)}
      >
        <Ellipsis />
      </Button>
      <Menu
        anchorEl={anchorEl}
        open={anchorEl !== null}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
        slotProps={MENU_SLOTS}
      >
        <MenuItem onClick={() => choose("rename")} sx={MENU_ITEM_SX}>
          Rename
        </MenuItem>
        <MenuItem onClick={() => choose("delete")} sx={MENU_ITEM_SX}>
          Delete
        </MenuItem>
      </Menu>
      {dialog === "rename" && (
        <RenameAgentDialog
          agent={agent}
          name={name}
          onClose={() => setDialog(null)}
        />
      )}
      {dialog === "delete" && (
        <DeleteAgentDialog
          agent={agent}
          name={name}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}

type AgentDialogProps = {
  agent: AgentSummary;
  name: string;
  onClose: () => void;
};

function RenameAgentDialog({ agent, name, onClose }: AgentDialogProps) {
  const rename = useRenameAgent(agent.eth_address);
  const [value, setValue] = useState(agent.handle ?? "");
  const [error, setError] = useState("");

  const save = () => {
    const next = value.trim();
    if (!HANDLE_RULE.test(next)) {
      setError("Use 1 to 15 letters, digits or underscores.");
      return;
    }
    if (next === agent.handle) {
      onClose();
      return;
    }
    setError("");
    rename.mutate(next, {
      onSuccess: onClose,
      onError: (err) =>
        setError(
          err instanceof ApiError && err.status === 409
            ? "That name is taken."
            : "Could not rename the agent.",
        ),
    });
  };

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename {name}</DialogTitle>
          <DialogDescription>
            1 to 15 letters, digits or underscores.
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <Input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            disabled={rename.isPending}
            autoFocus
          />
          {error && <p className="text-xs text-red-500">{error}</p>}
          <div className="mt-2 flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={rename.isPending}>
              Save
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function DeleteAgentDialog({ agent, name, onClose }: AgentDialogProps) {
  const remove = useDeleteAgent(agent.eth_address);
  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Delete {name}?</DialogTitle>
          <DialogDescription>
            It leaves the leaderboard and its key stops working. If its app is
            still connected, the app&apos;s next call starts a fresh agent with
            $100,000.
          </DialogDescription>
        </DialogHeader>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            disabled={remove.isPending}
            onClick={() =>
              remove.mutate(undefined, {
                onSuccess: onClose,
                onError: () => toast.error("Could not delete the agent."),
              })
            }
          >
            Delete
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** The wallet's native balance, under the address it belongs to. Hidden
 *  (`showCredits` in `SettingsPage`): with exact gas top-ups it is an
 *  internal buffer of at most one transaction's gas, nothing to act on. */
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
    if (!HANDLE_RULE.test(next)) {
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
          Winnings are claimed for you shortly after a market resolves, and
          claims are free. With this off, you claim them yourself from your
          profile.
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
