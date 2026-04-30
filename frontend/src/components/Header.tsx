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
  onStart: () => void;
  onStop: () => void;
  onDisconnect: () => void;
  starting: boolean;
  stopping: boolean;
};

export default function Header(p: Props) {
  const sinceLabel = p.botStartedAt ? ` • since ${formatTime(p.botStartedAt)}` : "";
  const loopLabel = p.lastLoopAt ? ` • loop ${formatTime(p.lastLoopAt)}` : "";
  return (
    <header className="card flex flex-col gap-4 p-4 lg:flex-row lg:items-center lg:justify-between">
      <div className="flex items-center gap-3">
        <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-sky-400 to-violet-500" />
        <div>
          <div className="text-sm font-semibold tracking-tight">Angel One — Auto Trader</div>
          <div className="text-xs text-slate-400">
            Client {p.clientCode || "—"} • {p.connected ? "connected" : "disconnected"}
            {sinceLabel}
            {loopLabel}
          </div>
          {p.lastError ? (
            <div className="text-xs text-rose-300">err: {p.lastError}</div>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        {p.autoMode ? (
          <span className="pill-blue" title="ANGEL_TOTP_SECRET is set — backend can auto-relogin daily.">
            AUTO MODE
          </span>
        ) : null}
        <span
          className={p.tradingEnabled ? "pill-amber" : "pill-slate"}
          title={p.tradingEnabled ? "TRADING_ENABLED=true: bot may place live orders" : "TRADING_ENABLED=false: dry-run only"}
        >
          {p.tradingEnabled ? "LIVE TRADING" : "DRY RUN"}
        </span>
        <span className={p.botRunning ? "pill-green" : "pill-slate"}>
          {p.botRunning ? "Bot running" : "Bot stopped"}
        </span>
        {p.botRunning ? (
          <button className="btn-danger" onClick={p.onStop} disabled={p.stopping}>
            {p.stopping ? "Stopping…" : "Stop bot"}
          </button>
        ) : (
          <button className="btn-primary" onClick={p.onStart} disabled={p.starting}>
            {p.starting ? "Starting…" : "Start bot"}
          </button>
        )}
        <button className="btn-ghost" onClick={p.onDisconnect}>Disconnect</button>
      </div>
    </header>
  );
}
