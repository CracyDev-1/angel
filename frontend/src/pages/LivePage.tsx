import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiGet,
  apiPost,
  type KillSwitchReport,
  type RateLimitSummary,
  type Snapshot,
  type UniverseBlock,
  type WarmupBlock,
} from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";
import PositionsPanel from "../components/PositionsPanel";
import DecisionsTable from "../components/DecisionsTable";
import KillSwitchModal from "../components/KillSwitchModal";
import CandidateCard from "../components/CandidateCard";
import DryrunCapital from "../components/DryrunCapital";
import PaperTradesPanel from "../components/PaperTradesPanel";
import WatchlistPanel from "../components/WatchlistPanel";
import StocksPanel from "../components/StocksPanel";
import SkipReasonsPanel from "../components/SkipReasonsPanel";
import ClosedTradesPanel from "../components/ClosedTradesPanel";

export default function LivePage() {
  const qc = useQueryClient();
  const [showKill, setShowKill] = useState(false);
  const [killReport, setKillReport] = useState<KillSwitchReport | null>(null);

  const snap = useQuery({
    queryKey: ["snapshot"],
    queryFn: () => apiGet<Snapshot>("/api/snapshot"),
    refetchInterval: 3000,
  });

  const killSwitch = useMutation({
    mutationFn: (opts: { cancel_pending: boolean; square_off: boolean }) =>
      apiPost<KillSwitchReport>("/api/kill-switch", { confirm: "STOP_EVERYTHING", ...opts }),
    onSuccess: (rep) => {
      setKillReport(rep);
      setShowKill(false);
      qc.invalidateQueries({ queryKey: ["snapshot"] });
      qc.invalidateQueries({ queryKey: ["status"] });
    },
  });

  const toggleKind = useMutation({
    mutationFn: (payload: Record<string, boolean>) =>
      apiPost<{ kind_enabled: Record<string, boolean> }>("/api/universe/kinds", payload),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["snapshot"] });
      qc.invalidateQueries({ queryKey: ["universe"] });
    },
  });

  const data = snap.data;
  const funds = data?.funds;
  const today = data?.bot_today;
  const cepe = data?.ce_pe_summary;
  const positions = data?.positions;
  const scan = data?.last_scan_summary;
  const rate = data?.rate_limit;
  const isLive = !!data?.trading_enabled;
  const paper = data?.paper;
  const dryrun = data?.dryrun;
  const scannerHits = data?.scanner ?? [];
  const availableCash = funds?.available_cash ?? 0;
  const optionEnabled = data?.universe?.kind_enabled?.OPTION ?? true;
  const equityEnabled = data?.universe?.kind_enabled?.EQUITY ?? true;
  const indexMarket = data?.market_hours?.OPTION ?? data?.market_hours?.INDEX;
  const equityMarket = data?.market_hours?.EQUITY;

  const isAlive = !!data?.bot_running && !!data?.last_loop_at;
  const totalPnl = today?.net_pnl ?? 0;
  const pnlSourceLabel = isLive ? "broker positions" : "paper book";

  return (
    <div className="space-y-6">
      {/* Top strip — what is happening RIGHT NOW + kill switch */}
      <section className="card flex flex-col gap-3 p-5 md:flex-row md:items-center md:justify-between">
        <div className="flex items-center gap-3">
          <span
            className={`h-3 w-3 rounded-full ${
              isAlive ? "bg-emerald-400 animate-pulse" : "bg-slate-500"
            }`}
          />
          <div>
            <div className="text-base font-semibold">
              {isAlive
                ? "Bot is watching the market"
                : data?.connected
                ? "Bot is paused"
                : "Disconnected"}
            </div>
            <div className="text-xs text-slate-400">
              {isAlive ? plainCycleReason(scan?.reason) : "Hit Start trading in the header to begin."}
              {data?.last_loop_at ? ` • last update ${formatTime(data.last_loop_at)}` : ""}
            </div>
            {(scan?.hidden_unaffordable ?? 0) > 0 || (scan?.index_unaffordable ?? 0) > 0 ? (
              <div className="mt-0.5 text-[11px] text-slate-500">
                {(scan?.hidden_unaffordable ?? 0) > 0 ? (
                  <>
                    Hidden {scan?.hidden_unaffordable} option contract
                    {scan?.hidden_unaffordable === 1 ? "" : "s"} — 1-lot premium above your cash
                  </>
                ) : null}
                {(scan?.hidden_unaffordable ?? 0) > 0 && (scan?.index_unaffordable ?? 0) > 0 ? " · " : ""}
                {(scan?.index_unaffordable ?? 0) > 0 ? (
                  <>
                    Filtered {scan?.index_unaffordable} index{(scan?.index_unaffordable ?? 0) === 1 ? "" : "es"}
                    {" "}whose ATM lot is above your cash
                  </>
                ) : null}
                .
              </div>
            ) : null}
            <RateLimitChip rate={rate} />
            <UniverseChip universe={data?.universe} />
            <WarmupChip warmup={data?.warmup} botRunning={!!data?.bot_running} />
          </div>
        </div>

        <div className="flex items-center gap-2">
          <button
            className="btn-danger text-base"
            onClick={() => setShowKill(true)}
            title="Stops bot, switches to dry-run, optionally cancels orders and closes positions."
          >
            🛑 STOP EVERYTHING
          </button>
        </div>
      </section>

      {killReport ? <KillReportBanner report={killReport} onDismiss={() => setKillReport(null)} /> : null}

      {/* Dry-run capital control (only in dry-run) */}
      {!isLive && dryrun ? (
        <DryrunCapital
          liveAvailableCash={dryrun.live_available_cash}
          override={dryrun.capital_override}
          deployable={dryrun.deployable_cash}
        />
      ) : null}

      {/* Money / P&L strip — compact, three numbers in one card */}
      <section className="card overflow-hidden">
        <div className="grid grid-cols-1 divide-y divide-white/5 sm:grid-cols-3 sm:divide-x sm:divide-y-0">
          <StripStat
            label="Cash in account"
            value={formatINR(funds?.available_cash ?? 0)}
            hint={
              isLive
                ? "Real broker cash."
                : dryrun && dryrun.capital_override > 0
                ? `Sizing uses dry-run override ${formatINR(dryrun.capital_override, { compact: true })}.`
                : "Real broker cash; dry-run sim sizes against this."
            }
            tone="neutral"
          />
          <StripStat
            label={isLive ? "P&L today (live)" : "P&L today (dry-run)"}
            value={formatINR(totalPnl)}
            hint={`Realized ${formatINR(today?.realized_pnl ?? 0, { compact: true })} · Unrealized ${formatINR(today?.unrealized_pnl ?? 0, { compact: true })} · ${pnlSourceLabel}`}
            tone={totalPnl > 0 ? "good" : totalPnl < 0 ? "bad" : "neutral"}
          />
          <StripStat
            label={isLive ? "Trades placed today" : "Paper trades today"}
            value={`${today?.trades_placed ?? 0}`}
            hint={
              isLive
                ? `${today?.filled ?? 0} filled · ${today?.pending ?? 0} pending · ${today?.rejected ?? 0} rejected`
                : `${today?.filled ?? 0} closed · ${paper?.open?.open_positions ?? 0} still open`
            }
            tone="neutral"
          />
        </div>
      </section>

      {/* OPEN + CLOSED POSITIONS — pinned to the top of the page so the user
          always sees what they're holding and what just closed. Live mode
          shows the broker book; dry-run shows the paper book. */}
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2 space-y-4">
          {isLive ? (
            <PositionsPanel positions={positions ?? null} liveExits={data?.live_exits ?? null} />
          ) : (
            <PaperTradesPanel paper={paper} />
          )}
        </div>
        <div className="space-y-4">
          <ClosedTradesPanel
            decisions={data?.decisions ?? []}
            isLive={isLive}
            liveClosed={data?.live_closed_today ?? []}
          />
          <CePeMiniSummary
            ceCount={cepe?.ce_open ?? 0}
            peCount={cepe?.pe_open ?? 0}
            ceCap={cepe?.capital_ce ?? 0}
            peCap={cepe?.capital_pe ?? 0}
            cePnl={cepe?.pnl_ce ?? 0}
            pePnl={cepe?.pnl_pe ?? 0}
          />
        </div>
      </section>

      {/* Compact watchlist — every instrument the bot watches in dense rows. */}
      <section className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <WatchlistPanel
            hits={scannerHits}
            availableCash={availableCash}
            market={indexMarket}
            enabled={optionEnabled}
            onToggle={(v) => toggleKind.mutate({ OPTION: v })}
            togglePending={toggleKind.isPending}
          />
        </div>
        <div>
          <StocksPanel
            hits={scannerHits}
            availableCash={availableCash}
            market={equityMarket}
            enabled={equityEnabled}
            onToggle={(v) => toggleKind.mutate({ EQUITY: v })}
            togglePending={toggleKind.isPending}
          />
        </div>
      </section>

      {/* Brain analysis — collapsed by default, click any card to expand. */}
      {(() => {
        const allHits = data?.scanner ?? [];
        const visibleHits = allHits.filter((row) => {
          if ((row.kind || "").toUpperCase() === "INDEX") return true;
          if (row.is_affordable === false) return false;
          if ((row.affordable_lots ?? 0) >= 1) return true;
          return false;
        });
        const sorted = visibleHits
          .slice()
          .sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
        const hiddenCount = allHits.length - visibleHits.length;
        return (
          <section className="card overflow-hidden">
            <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-white/5 px-4 py-2.5">
              <div className="flex items-baseline gap-2">
                <h2 className="text-sm font-semibold tracking-wide text-slate-200">
                  Brain analysis
                </h2>
                <span className="text-[11px] text-slate-500">
                  {sorted.length} ranked
                  {scan?.min_score ? (
                    <> · pass ≥ {Math.round(scan.min_score * 100)} score</>
                  ) : null}
                  {hiddenCount > 0 ? (
                    <> · {hiddenCount} hidden (1-lot &gt; cash)</>
                  ) : null}
                </span>
              </div>
            </div>
            {sorted.length === 0 ? (
              <div className="p-5 text-center text-xs text-slate-500">
                {allHits.length > 0
                  ? `All ${allHits.length} scanned instruments need more cash than you have for one lot.`
                  : data?.bot_running
                  ? data?.warmup?.warmed_tokens && data.warmup.warmed_tokens > 0
                    ? "Brain seeded from broker history — waiting for the next scan cycle to grade signals."
                    : "Brain is warming up — fetching candles from the broker and waiting for ≥ 5 five-minute and ≥ 2 fifteen-minute bars."
                  : "Click Start trading in the header to begin scanning."}
              </div>
            ) : (
              <div className="grid grid-cols-1 gap-2 p-3 lg:grid-cols-2 2xl:grid-cols-3">
                {sorted.slice(0, 9).map((row, i) => (
                  <CandidateCard key={`${row.exchange}:${row.token}`} row={row} isTop={i === 0} />
                ))}
              </div>
            )}
          </section>
        );
      })()}

      {/* Aggregated skip reasons — quickly tells the user why no trades fired */}
      <SkipReasonsPanel decisions={data?.decisions ?? []} />

      {/* Bot reasoning stream — full transparency */}
      <section className="card overflow-hidden">
        <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-white/5 px-4 py-2.5">
          <div className="flex items-baseline gap-2">
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">
              Bot decisions
            </h2>
            <span className="text-[11px] text-slate-500">
              one row per scan cycle / placement attempt
            </span>
          </div>
        </div>
        <div className="p-3">
          <DecisionsTable rows={data?.decisions ?? []} />
        </div>
      </section>

      {showKill ? (
        <KillSwitchModal
          onCancel={() => setShowKill(false)}
          onConfirm={(opts) => killSwitch.mutate(opts)}
          submitting={killSwitch.isPending}
        />
      ) : null}
    </div>
  );
}

function plainCycleReason(reason: string | undefined): string {
  if (!reason) return "Waiting for the first scan cycle…";
  if (reason === "candidates_available")
    return "Found instruments worth a closer look this cycle.";
  if (reason === "max_positions_open")
    return "Already at the max number of open positions — holding.";
  if (reason === "no_affordable_lots_for_capital")
    return "Nothing in the watchlist fits your available cash for one full lot.";
  if (reason === "watchlist_empty_or_ltp_failed")
    return "Watchlist is empty or the broker LTP call failed this cycle.";
  if (reason === "no_brain_entry_signal_yet")
    return "No instrument has passed the multi-timeframe entry checks yet — staying out.";
  if (reason.startsWith("all_scores_below_min"))
    return "Market is quiet — no instrument cleared the minimum brain score.";
  return `Cycle reason: ${reason}`;
}

function StripStat({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint: string;
  tone: "good" | "bad" | "neutral";
}) {
  const valueClass =
    tone === "good" ? "text-emerald-300" : tone === "bad" ? "text-rose-300" : "text-slate-100";
  return (
    <div className="px-4 py-3">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums tracking-tight ${valueClass}`}>
        {value}
      </div>
      <div className="mt-1 text-[10px] text-slate-500">{hint}</div>
    </div>
  );
}

function CePeMiniSummary({
  ceCount,
  peCount,
  ceCap,
  peCap,
  cePnl,
  pePnl,
}: {
  ceCount: number;
  peCount: number;
  ceCap: number;
  peCap: number;
  cePnl: number;
  pePnl: number;
}) {
  return (
    <div className="card overflow-hidden">
      <div className="border-b border-white/5 px-4 py-2.5">
        <h2 className="text-sm font-semibold tracking-wide text-slate-200">
          Calls vs Puts
        </h2>
        <div className="text-[11px] text-slate-500">
          Direction breakdown of currently held positions.
        </div>
      </div>
      <div className="grid grid-cols-2 divide-x divide-white/5">
        <SideMini label="Calls (CE)" tone="good" count={ceCount} capital={ceCap} pnl={cePnl} />
        <SideMini label="Puts (PE)" tone="bad" count={peCount} capital={peCap} pnl={pePnl} />
      </div>
    </div>
  );
}

function SideMini({
  label,
  tone,
  count,
  capital,
  pnl,
}: {
  label: string;
  tone: "good" | "bad";
  count: number;
  capital: number;
  pnl: number;
}) {
  const headClass = tone === "good" ? "text-emerald-300" : "text-rose-300";
  return (
    <div className="px-4 py-3">
      <div className={`text-[10px] uppercase tracking-wider ${headClass}`}>{label}</div>
      <div className="mt-1 flex items-baseline gap-1.5">
        <div className="text-xl font-semibold tabular-nums text-slate-100">{count}</div>
        <div className="text-[10px] text-slate-500">open</div>
      </div>
      <div className="mt-1 text-[11px] text-slate-400">
        Capital <span className="tabular-nums text-slate-200">{formatINR(capital, { compact: true })}</span>
      </div>
      <div className={`text-[11px] ${classOf(pnl)}`}>
        P&amp;L <span className="font-semibold tabular-nums">{formatINR(pnl)}</span>
      </div>
    </div>
  );
}

function RateLimitChip({ rate }: { rate: RateLimitSummary | undefined }) {
  if (!rate) return null;
  const near = rate.near_cap || [];
  if (rate.enabled && near.length === 0 && (rate.last_wait_s || 0) === 0) {
    return (
      <div className="mt-1 text-[10px] uppercase tracking-wider text-slate-500">
        Broker API: healthy ({rate.calls_total} calls)
      </div>
    );
  }
  if (!rate.enabled) {
    return (
      <div className="mt-1 text-[10px] uppercase tracking-wider text-amber-300">
        Broker rate-limit guard DISABLED — re-enable RATE_LIMIT_ENABLED in .env
      </div>
    );
  }
  if (near.length > 0) {
    const top = near[0];
    return (
      <div className="mt-1 text-[10px] uppercase tracking-wider text-amber-300">
        Throttling: {top.path} {top.used}/{top.limit} per {top.window_s}s
        {near.length > 1 ? ` (+${near.length - 1} more)` : ""}
      </div>
    );
  }
  return (
    <div className="mt-1 text-[10px] uppercase tracking-wider text-slate-500">
      Broker API: paced ({rate.waits_total} pauses, last {rate.last_wait_s?.toFixed(2) ?? "0"}s)
    </div>
  );
}

function UniverseChip({ universe }: { universe: UniverseBlock | undefined }) {
  if (!universe) return null;
  const m = universe.master;
  if (!m) {
    return (
      <div className="mt-1 text-[10px] uppercase tracking-wider text-amber-300">
        Instrument master not loaded — open the Universe tab to refresh
      </div>
    );
  }
  const r = universe.report;
  const total =
    (r?.indices_resolved ?? 0) +
    (r?.stocks_resolved ?? 0) +
    (r?.commodities_resolved ?? 0) +
    (r?.atm_resolved ?? 0);
  const ageHrs = m.age_seconds != null ? (m.age_seconds / 3600).toFixed(1) : "?";
  return (
    <div className="mt-1 text-[10px] uppercase tracking-wider text-slate-500">
      Universe: {total} resolved ({m.instruments?.toLocaleString() ?? "?"} master, {ageHrs}h old)
      {r && (r.indices_missing.length || r.stocks_missing.length || r.atm_missing.length) ? (
        <span className="ml-2 text-amber-300">
          ⚠ {(r.indices_missing.length + r.stocks_missing.length + r.atm_missing.length)} unresolved
        </span>
      ) : null}
    </div>
  );
}

function WarmupChip({
  warmup,
  botRunning,
}: {
  warmup: WarmupBlock | undefined;
  botRunning: boolean;
}) {
  if (!warmup || !botRunning) return null;

  const pct = warmup.regime_data_pct ?? 0;
  const ready = warmup.regime_data_ready ?? 0;
  const total = warmup.regime_data_total ?? 0;
  const selective = warmup.regime_data_selective !== false;
  const barColor =
    pct >= 90 ? "bg-emerald-400/90" : pct >= 50 ? "bg-amber-400/90" : "bg-rose-400/80";

  const historyNote =
    warmup.from_history && warmup.warmed_tokens === 0 ? (
      <div className="mt-1 text-[10px] uppercase tracking-wider text-amber-300">
        Brain warmup: history fetch pending — collecting live ticks
      </div>
    ) : warmup.from_history && warmup.warmed_tokens > 0 ? (
      <div className="mt-0.5 text-[10px] uppercase tracking-wider text-slate-500">
        History seed · {warmup.seeded_aggregators}/{warmup.warmed_tokens} tokens
      </div>
    ) : null;

  return (
    <div className="mt-2 space-y-1">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-[10px] uppercase tracking-wider text-slate-400">
          Regime data readiness
        </span>
        {!selective ? (
          <span className="text-[11px] text-slate-500">— selective regime off</span>
        ) : total === 0 ? (
          <span className="text-[11px] text-amber-200/90">Waiting for first scan…</span>
        ) : (
          <>
            <span
              className={`text-xs font-semibold tabular-nums ${
                pct >= 90 ? "text-emerald-300" : pct >= 50 ? "text-amber-200" : "text-rose-200"
              }`}
            >
              {pct}%
            </span>
            <span className="text-[10px] text-slate-500">
              ({ready}/{total} symbols)
            </span>
          </>
        )}
      </div>
      {selective && total > 0 ? (
        <div className="h-1.5 w-full max-w-xs overflow-hidden rounded-full bg-slate-700/80">
          <div
            className={`h-full rounded-full transition-[width] duration-500 ${barColor}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      ) : null}
      {historyNote}
    </div>
  );
}

function KillReportBanner({
  report,
  onDismiss,
}: {
  report: KillSwitchReport;
  onDismiss: () => void;
}) {
  return (
    <div className="card border border-amber-500/30 bg-amber-500/10 p-4">
      <div className="flex items-start justify-between">
        <div className="space-y-1 text-sm">
          <div className="font-semibold text-amber-100">Kill-switch executed</div>
          <div className="text-amber-200/90">
            Bot stopped • switched to dry-run • cancelled {report.cancelled.length} pending order(s)
            • squared off {report.squared_off.length} position(s).
          </div>
          {report.cancel_failures.length || report.squareoff_failures.length ? (
            <div className="text-rose-200">
              Failures: {report.cancel_failures.length} cancel, {report.squareoff_failures.length} square-off.
              Check broker terminal.
            </div>
          ) : null}
        </div>
        <button className="btn-ghost text-xs" onClick={onDismiss}>Dismiss</button>
      </div>
    </div>
  );
}
