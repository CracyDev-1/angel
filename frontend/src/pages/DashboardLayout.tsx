import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiGet, apiPost, type Snapshot } from "../lib/api";
import Header from "../components/Header";
import GoLiveModal from "../components/GoLiveModal";

export default function DashboardLayout() {
  const qc = useQueryClient();
  const [showGoLive, setShowGoLive] = useState(false);

  const snap = useQuery({
    queryKey: ["snapshot"],
    queryFn: () => apiGet<Snapshot>("/api/snapshot"),
    refetchInterval: 3000,
  });

  const startBot = useMutation({
    mutationFn: () => apiPost("/api/bot/start"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });
  const stopBot = useMutation({
    mutationFn: () => apiPost("/api/bot/stop"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });
  const disconnect = useMutation({
    mutationFn: () => apiPost("/api/disconnect"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["status"] }),
  });
  const enableLive = useMutation({
    mutationFn: () => apiPost("/api/trading/enable", { confirm: "I_UNDERSTAND_LIVE_TRADING" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });
  const disableLive = useMutation({
    mutationFn: () => apiPost("/api/trading/disable"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["snapshot"] }),
  });

  const data = snap.data;
  const tradingLive = !!data?.trading_enabled;

  // Re-tint the entire app whenever the trading mode flips. CSS variables in
  // index.css read these classes and animate the background gradient + accent
  // color so the user can never miss which mode is active.
  useEffect(() => {
    const cls = tradingLive ? "mode-live" : "mode-dryrun";
    document.body.classList.remove("mode-live", "mode-dryrun");
    document.body.classList.add(cls);
    return () => document.body.classList.remove("mode-live", "mode-dryrun");
  }, [tradingLive]);

  return (
    <div className="flex min-h-screen">
      <Sidebar live={tradingLive} />
      <main className="flex-1 px-4 py-6 lg:px-6">
        <ModeBanner live={tradingLive} />
        <Header
          connected={!!data?.connected}
          botRunning={!!data?.bot_running}
          tradingEnabled={tradingLive}
          autoMode={!!data?.auto_mode}
          clientCode={data?.clientcode || null}
          lastError={data?.last_error || null}
          lastLoopAt={data?.last_loop_at || null}
          botStartedAt={data?.bot_started_at || null}
          onStartTrading={() => startBot.mutate()}
          onStopTrading={() => stopBot.mutate()}
          onDisconnect={() => disconnect.mutate()}
          onRequestGoLive={() => setShowGoLive(true)}
          onGoDryRun={() => disableLive.mutate()}
          starting={startBot.isPending}
          stopping={stopBot.isPending}
          switchingMode={enableLive.isPending || disableLive.isPending}
        />

        <div className="mt-6">
          <Outlet />
        </div>
      </main>

      {showGoLive ? (
        <GoLiveModal
          onCancel={() => setShowGoLive(false)}
          onConfirm={() => {
            enableLive.mutate();
            setShowGoLive(false);
          }}
          submitting={enableLive.isPending}
        />
      ) : null}
    </div>
  );
}

function ModeBanner({ live }: { live: boolean }) {
  return (
    <div
      className={`mode-banner mb-3 flex items-center justify-between rounded-xl border px-4 py-2 text-xs font-semibold uppercase tracking-wider`}
    >
      <span className="flex items-center gap-2">
        <span
          className={`h-2.5 w-2.5 rounded-full ${live ? "bg-rose-400 animate-pulse" : "bg-sky-400"}`}
        />
        {live ? (
          <>
            LIVE TRADING — real money is on the line
          </>
        ) : (
          <>
            DRY RUN — paper trades only, real cash is safe
          </>
        )}
      </span>
      <span className="text-[10px] font-normal normal-case text-slate-400">
        Mode pill in the header switches between dry-run and live.
      </span>
    </div>
  );
}

function Sidebar({ live }: { live: boolean }) {
  const link =
    "flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium transition";
  const active = "bg-sky-500/15 text-sky-200";
  const inactive = "text-slate-400 hover:bg-white/5 hover:text-slate-100";
  return (
    <aside className="hidden w-56 shrink-0 border-r border-white/5 bg-slate-950/40 p-4 lg:block">
      <div className="mb-6 flex items-center gap-2">
        <div
          className={`h-8 w-8 rounded-lg bg-gradient-to-br ${
            live ? "from-orange-400 to-rose-500" : "from-sky-400 to-violet-500"
          }`}
        />
        <div>
          <div className="text-sm font-semibold tracking-tight">Auto Trader</div>
          <div className="text-[11px] text-slate-500">Angel One SmartAPI</div>
        </div>
      </div>
      <nav className="space-y-1">
        <NavLink
          to="/dashboard/live"
          className={({ isActive }) => `${link} ${isActive ? active : inactive}`}
        >
          <Dot className="bg-emerald-400" /> Live
        </NavLink>
        <NavLink
          to="/dashboard/history"
          className={({ isActive }) => `${link} ${isActive ? active : inactive}`}
        >
          <Dot className="bg-slate-400" /> History
        </NavLink>
        <NavLink
          to="/dashboard/universe"
          className={({ isActive }) => `${link} ${isActive ? active : inactive}`}
        >
          <Dot className="bg-violet-400" /> Universe
        </NavLink>
      </nav>
      <div className="mt-8 rounded-xl border border-white/5 bg-slate-900/50 p-3 text-[11px] leading-relaxed text-slate-400">
        Bot reasoning, scans, candidates and orders are streamed on the Live tab.
        Past orders, daily P&amp;L and capital flow live on History — switch
        between Live and Dry-run there.
      </div>
    </aside>
  );
}

function Dot({ className }: { className: string }) {
  return <span className={`h-2 w-2 rounded-full ${className}`} />;
}
