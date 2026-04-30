import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiGet,
  apiPost,
  getDashboardToken,
  setDashboardToken,
  type StatusResponse,
} from "../lib/api";

export default function LoginPage() {
  const [totp, setTotp] = useState("");
  const [token, setToken] = useState(getDashboardToken());
  const [showAdvanced, setShowAdvanced] = useState(Boolean(getDashboardToken()));
  const [forceManual, setForceManual] = useState(false);
  const navigate = useNavigate();
  const qc = useQueryClient();

  const status = useQuery({
    queryKey: ["status"],
    queryFn: () => apiGet<StatusResponse>("/api/status"),
    refetchInterval: 3000,
  });

  // If the backend has already auto-connected (ANGEL_TOTP_SECRET in .env), skip the form.
  useEffect(() => {
    if (status.data?.connected) {
      navigate("/dashboard", { replace: true });
    }
  }, [status.data?.connected, navigate]);

  const connect = useMutation({
    mutationFn: async () => {
      setDashboardToken(token);
      return apiPost<{ status: boolean; clientcode: string | null; profile_message?: string }>(
        "/api/connect",
        { totp }
      );
    },
    onSuccess: async (data) => {
      if (!data.status) throw new Error(data.profile_message || "Login failed");
      await qc.invalidateQueries({ queryKey: ["status"] });
      navigate("/dashboard", { replace: true });
    },
  });

  const valid = /^\d{6}$/.test(totp.trim());
  const errorMsg = connect.error instanceof Error ? connect.error.message : null;
  const autoMode = !!status.data?.auto_mode && !forceManual;

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-10">
      <div className="card w-full max-w-md p-8">
        <div className="mb-6 flex items-center gap-3">
          <div className="h-9 w-9 rounded-xl bg-gradient-to-br from-sky-400 to-violet-500" />
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Angel One Auto Trader</h1>
            <p className="text-xs text-slate-400">
              {autoMode
                ? "ANGEL_TOTP_SECRET detected — backend will auto-login."
                : "Connect with the current TOTP from your authenticator."}
            </p>
          </div>
        </div>

        {autoMode ? (
          <AutoModePanel
            connected={!!status.data?.connected}
            lastError={status.data?.last_error || null}
            onRetry={() => qc.invalidateQueries({ queryKey: ["status"] })}
            onUseManual={() => setForceManual(true)}
          />
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (valid && !connect.isPending) connect.mutate();
            }}
            className="space-y-4"
          >
            <div>
              <label className="stat-label">Authenticator code</label>
              <input
                className="input mt-2 text-center text-2xl tracking-[0.4em]"
                autoFocus
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={6}
                placeholder="••••••"
                value={totp}
                onChange={(e) => setTotp(e.target.value.replace(/\D/g, "").slice(0, 6))}
              />
              <p className="mt-1 text-xs text-slate-500">
                Open your authenticator app, copy the current 6-digit code.
              </p>
            </div>

            <div className="text-xs">
              <button
                type="button"
                onClick={() => setShowAdvanced((v) => !v)}
                className="text-slate-400 hover:text-slate-200"
              >
                {showAdvanced ? "Hide advanced" : "Advanced (optional)"}
              </button>
              {showAdvanced ? (
                <div className="mt-2 space-y-2 rounded-lg border border-white/5 bg-slate-900/40 p-3">
                  <label className="stat-label">Dashboard API token</label>
                  <input
                    className="input text-sm"
                    placeholder="Only needed if .env sets DASHBOARD_TOKEN"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                  />
                  <p className="text-[11px] leading-relaxed text-slate-500">
                    This is <strong>not</strong> your TOTP. It is an optional shared secret
                    that protects the dashboard&apos;s own API. Leave blank unless you set
                    <code className="ml-1">DASHBOARD_TOKEN</code> in the backend
                    <code className="ml-1">.env</code>.
                  </p>
                </div>
              ) : null}
            </div>

            {errorMsg ? (
              <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200">
                {errorMsg}
              </div>
            ) : null}

            <button
              type="submit"
              disabled={!valid || connect.isPending}
              className="btn-primary w-full text-base"
            >
              {connect.isPending ? "Connecting…" : "Connect"}
            </button>

            <p className="text-[11px] leading-relaxed text-slate-500">
              For full automation set <code>ANGEL_TOTP_SECRET</code> (the base32 from
              <a
                className="ml-1 text-slate-300 underline decoration-dotted"
                href="https://smartapi.angelone.in/enable-totp"
                target="_blank"
                rel="noreferrer"
              >
                Angel TOTP setup
              </a>
              ) in <code>.env</code>; the backend will then auto-login on startup and
              auto-relogin after midnight. The bot defaults to{" "}
              <span className="text-slate-300">dry-run</span>; no system can guarantee
              profits — review trades and keep risk caps tight.
            </p>
          </form>
        )}
      </div>
    </div>
  );
}

function AutoModePanel({
  connected,
  lastError,
  onRetry,
  onUseManual,
}: {
  connected: boolean;
  lastError: string | null;
  onRetry: () => void;
  onUseManual: () => void;
}) {
  return (
    <div className="space-y-4">
      <div
        className={
          connected
            ? "rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-3 text-sm text-emerald-100"
            : "rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-3 text-sm text-amber-100"
        }
      >
        {connected
          ? "Connected to Angel One. Redirecting to the dashboard…"
          : "Backend is auto-logging in using ANGEL_TOTP_SECRET. This usually takes a few seconds."}
      </div>
      {lastError ? (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {lastError}
        </div>
      ) : null}
      <div className="flex gap-2">
        <button type="button" className="btn-ghost flex-1" onClick={onRetry}>
          Refresh status
        </button>
        <button type="button" className="btn-ghost flex-1" onClick={onUseManual}>
          Enter TOTP manually
        </button>
      </div>
      <p className="text-[11px] leading-relaxed text-slate-500">
        To make this permanent, paste the base32 secret from{" "}
        <a
          className="text-slate-300 underline decoration-dotted"
          href="https://smartapi.angelone.in/enable-totp"
          target="_blank"
          rel="noreferrer"
        >
          Angel TOTP setup
        </a>{" "}
        into <code>ANGEL_TOTP_SECRET</code> and restart the backend.
      </p>
    </div>
  );
}
