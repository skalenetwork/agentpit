import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { AuthButtons } from "@/components/auth/AuthButtons";
import { useAuth } from "@/auth/useAuth";
import { avatarUrl } from "@/lib/avatar";
import { useSearch } from "@/lib/searchContext";
import { useTheme } from "@/lib/theme";
import { useState } from "react";
import { Menu, MenuItem, IconButton } from "@mui/material";
import { TrendingUp, LogOut, Moon, Settings, Sun, User, Bot } from "lucide-react";

export function TopNav() {
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  // The search moved into the markets page, so the header no longer carries
  // one. `SearchBar` below is kept rather than deleted, but be clear about
  // what that means: with this flag false it never renders, so it is dormant
  // code, not a live fallback. It is retained because turning the header
  // search back on is one word, and because the markets page's own search
  // reuses the same query context it reads.
  //
  // One page lost its search outright: the event detail page had the header
  // one and has no in-page replacement. That is a real gap, not a decision.
  const showSearch = false;

  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);
  const { theme, toggleTheme } = useTheme();
  const isDarkMode = theme === "dark";
  const open = Boolean(anchorEl);

  const handleMenuOpen = (event: React.MouseEvent<HTMLElement>) => {
    setAnchorEl(event.currentTarget);
  };

  const handleMenuClose = () => {
    setAnchorEl(null);
  };

  const handleLogout = () => {
    handleMenuClose();
    logout();
  };

  const handleProfile = () => {
    handleMenuClose();
    navigate("/profile");
  };

  const handleSettings = () => {
    handleMenuClose();
    navigate("/settings");
  };

  const menuPaperSx = {
    width: 220,
    borderRadius: "1rem", // match menu item rounded-xl
    mt: 1,
    border: "1px solid hsl(var(--border) / 0.7)",
    backgroundColor: "hsl(var(--popover))",
    color: "hsl(var(--popover-foreground))",
    boxShadow:
      "0 16px 40px -20px hsl(var(--foreground) / 0.45), 0 2px 12px -4px hsl(var(--foreground) / 0.25)",
    overflow: "hidden",
  };

  const menuItemSx = {
    minHeight: 38,
    px: 1.5,
    py: 0.75,
    fontSize: "0.875rem",
    color: "hsl(var(--popover-foreground))",
    borderRadius: "0.5rem",
    transition: "background-color 0.15s, border-radius 0.15s",
    "&:hover": {
      backgroundColor: "hsl(var(--muted))",
      borderRadius: "1rem",
    },
  };

  const logoutItemSx = {
    ...menuItemSx,
    color: "rgb(220 38 38)",
    "&:hover": {
      backgroundColor: "rgb(239 68 68 / 0.1)",
      borderRadius: "1rem",
    },
  };

  // z-40, not z-10: an app header sits above page content unconditionally. At
  // z-10 it merely tied with anything in the page using the same value, and a
  // tie is settled by DOM order -- which page content always wins. Stays below
  // ScrollToTop's z-50, which is meant to float over everything.
  return (
    <header className="sticky top-0 z-40 border-b bg-background/80 backdrop-blur">
      <div className="container flex h-14 items-center gap-6">
        <NavLink to="/" className="flex shrink-0 items-center gap-2">
          <AgentLogo />
          <span className="text-base font-semibold tracking-tight">
            AgentPit
          </span>
          <span className="rounded-full bg-orange-500 px-1.5 py-0.5 text-[10px] font-bold uppercase leading-none tracking-wide text-white">
            Beta
          </span>
        </NavLink>
        <div className="flex items-center gap-2">
          <NavLink
            to="/markets"
            className={({ isActive }) =>
              `hidden shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors sm:inline-flex ${isActive
                ? "border-blue-400/40 bg-blue-500/10 text-blue-700 dark:text-blue-400"
                : "border-border text-muted-foreground hover:text-foreground"
              }`
            }
          >
            <TrendingUp className="size-3.5" />
            Markets
          </NavLink>
          <NavLink
            to="/agents"
            className={({ isActive }) =>
              `hidden shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-semibold transition-colors sm:inline-flex ${isActive
                ? "border-blue-400/40 bg-blue-500/10 text-blue-700 dark:text-blue-400"
                : "border-border text-muted-foreground hover:text-foreground"
              }`
            }
          >
            <Bot className="size-3.5" />
            Arena
          </NavLink>
        </div>
        {showSearch && <SearchBar />}
        <div className="ml-auto flex items-center gap-2">
          <button
            type="button"
            aria-label={
              isDarkMode ? "Switch to light mode" : "Switch to dark mode"
            }
            onClick={toggleTheme}
            className="inline-flex h-9 items-center gap-2 rounded-full border border-border/70 bg-background px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
          >
            {isDarkMode ? (
              <Moon className="size-3.5" />
            ) : (
              <Sun className="size-3.5" />
            )}
            <span>{isDarkMode ? "Dark" : "Light"}</span>
            <span
              className={`inline-flex h-5 w-9 items-center rounded-full transition-colors ${isDarkMode ? "bg-primary" : "bg-muted-foreground/40"}`}
              aria-hidden
            >
              <span
                className={`inline-block size-3.5 rounded-full bg-white transition-transform ${isDarkMode ? "translate-x-[18px]" : "translate-x-[3px]"}`}
              />
            </span>
          </button>
          {user ? (
            <>
              <IconButton
                aria-label="Account"
                aria-controls={open ? "profile-menu" : undefined}
                aria-haspopup="true"
                aria-expanded={open ? "true" : undefined}
                onClick={handleMenuOpen}
              >
                <img
                  className="size-10 shrink-0 rounded-[28%] bg-muted"
                  src={avatarUrl(user.eth_address)}
                  alt=""
                />
              </IconButton>
              <Menu
                id="profile-menu"
                anchorEl={anchorEl}
                open={open}
                onClose={handleMenuClose}
                anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
                transformOrigin={{ vertical: "top", horizontal: "center" }}
                slotProps={{
                  paper: {
                    sx: menuPaperSx,
                  },
                  list: {
                    sx: { p: 0.75 },
                  },
                }}
              >
                <MenuItem onClick={handleProfile} sx={menuItemSx}>
                  <User className="mr-2 size-4" /> Profile
                </MenuItem>
                <MenuItem onClick={handleSettings} sx={menuItemSx}>
                  <Settings className="mr-2 size-4" /> Settings
                </MenuItem>
                <MenuItem onClick={handleLogout} sx={logoutItemSx}>
                  <LogOut className="mr-2 size-4" /> Logout
                </MenuItem>
              </Menu>
            </>
          ) : null}
          <AuthButtons />
        </div>
      </div>
    </header>
  );
}

function SearchBar() {
  const { query, setQuery } = useSearch();
  const { pathname } = useLocation();
  const navigate = useNavigate();

  const onChange = (next: string) => {
    setQuery(next);
    // The results grid lives on the markets route, so searching from an
    // event page jumps back there where matches can render.
    if (pathname !== "/markets") navigate("/markets");
  };

  return (
    <div className="group flex max-w-md flex-1 items-center gap-2.5 rounded-lg bg-muted px-3 py-2 text-muted-foreground transition-colors focus-within:bg-background focus-within:text-foreground focus-within:ring-1 focus-within:ring-border">
      <span aria-hidden className="text-muted-foreground/70">
        <SearchGlyph />
      </span>
      <input
        type="search"
        value={query}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Search markets"
        className="min-w-0 flex-1 bg-transparent text-sm text-foreground placeholder:text-muted-foreground/60 focus:outline-none"
      />
    </div>
  );
}

function AgentLogo() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 25 25"
      fill="currentColor"
      className="size-6 shrink-0 text-primary"
      aria-hidden
    >
      <ellipse cx="12.42" cy="22" rx="2.57" ry="2.73" />
      <path d="M13.18,1.04l-.19-.12c-.63-.39-1.44-.16-1.81.51L.6,20.9c-.37.67-.15,1.53.48,1.92l.19.12c.63.39,1.44.16,1.81-.51L13.67,2.95c.37-.67.15-1.53-.48-1.92Z" />
      <path d="M11.66,1.04l.19-.12c.63-.39,1.44-.16,1.81.51l10.59,19.46c.37.67.15,1.53-.48,1.92l-.19.12c-.63.39-1.44.16-1.81-.51L11.18,2.95c-.37-.67-.15-1.53.48-1.92Z" />
    </svg>
  );
}

function SearchGlyph() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="size-4"
      aria-hidden
    >
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path d="M20 20l-4.65-4.65" />
    </svg>
  );
}
