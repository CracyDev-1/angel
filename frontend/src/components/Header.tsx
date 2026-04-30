import { formatTime } from "../lib/format";

type Props = {
  connected: boolean;
  botRunning: boolean;
  tradingEnabled: boolean;
  autoMode: boolean;
  clientCode: string | null;
  lastError: string | null;
  lastLoopAt: string | null;
  botStartedAt: string | null;
  onStartTrading: () => void;
  onStopTrading: () => void;
  onDisconnect: () => void;
  onRequestGoLive: () => void;
  onGoDryRun: () => void;
  starting: boolean;
  stopping: boolean;
  switchingMode: boolean;
};

export default function Header(p: Props) {
  const sinceLabel = p.botStartedAt ? ` • since ${formatTime(p.botStartedAt)}` : "";
  const loopLabel = p.lastLoopAt ? ` • loop ${formatTime(p.lastLoopAt)}` : "";

  return (
    <header className="card flex flex-col gap-4 p-4 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex items-center gap-3">
        <div>
          <div className="text-base font-semibold tracking-tight">Auto Trader</div>
          <div className="text-xs text-slate-400">
            Client {p.clientCode || "—"} • {p.connected ? "connected" : "disconnected"}
            {sinceLabel}
            {loopLabel}
          </div>
          {p.lastError ? (
            <div className="mt-1 text-xs text-rose-300">err: {p.lastError}</div>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {p.autoMode ? (
          <span className="pill-blue" title="ANGEL_TOTP_SECRET set — backend can auto-relogin daily.">
            AUTO MODE
          </span>
        ) : null}

        {p.tradingEnabled ? (
          <button
            className="pill-red hover:opacity-80"
            onClick={p.onGoDryRun}
            disabled={p.switchingMode}
            title="Switch back to dry-run (no real orders)."
          >
            LIVE — switch to dry-run?
          </button>
        ) : (
          <button
            className="pill-blue hover:opacity-80"
            onClick={p.onRequestGoLive}
            disabled={p.switchingMode}
            title="Switch from dry-run paper trading to live (sends real orders)."
          >
            DRY RUN — go live?
          </button>
        )}

        <span className={p.botRunning ? "pill-green" : "pill-slate"}>
          {p.botRunning ? "Bot running" : "Bot stopped"}
        </span>

        {p.botRunning ? (
          <button className="btn-danger" onClick={p.onStopTrading} disabled={p.stopping}>
            {p.stopping ? "Stopping…" : "Stop trading"}
          </button>
        ) : (
          <button className="btn-primary" onClick={p.onStartTrading} disabled={p.starting}>
            {p.starting ? "Starting…" : "Start trading"}
          </button>
        )}

        <button className="btn-ghost" onClick={p.onDisconnect}>Disconnect</button>
      </div>
    </header>
  );
}
