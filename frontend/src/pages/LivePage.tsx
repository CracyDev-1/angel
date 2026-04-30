import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiGet,
  apiPost,
  type KillSwitchReport,
  type ScannerBucket,
  type Snapshot,
} from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";
import PositionsPanel from "../components/PositionsPanel";
import DecisionsTable from "../components/DecisionsTable";
import KillSwitchModal from "../components/KillSwitchModal";
import CandidateCard from "../components/CandidateCard";

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

  const data = snap.data;
  const funds = data?.funds;
  const today = data?.bot_today;
  const cepe = data?.ce_pe_summary;
  const positions = data?.positions;
  const scan = data?.last_scan_summary;
  const buckets = data?.scanner_by_kind?.buckets ?? [];

  const isAlive = !!data?.bot_running && !!data?.last_loop_at;
  const totalPnl = today?.net_pnl ?? 0;

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

      {/* Money / P&L hero numbers */}
      <section className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <BigCard
          label="Money in your account"
          value={formatINR(funds?.available_cash ?? 0)}
          hint="Cash available to deploy. Updated from the broker every loop."
          tone="neutral"
        />
        <BigCard
          label="Total profit / loss today"
          value={formatINR(totalPnl)}
          hint={`Realized ${formatINR(today?.realized_pnl ?? 0, { compact: true })} • Unrealized ${formatINR(today?.unrealized_pnl ?? 0, { compact: true })}`}
          tone={totalPnl > 0 ? "good" : totalPnl < 0 ? "bad" : "neutral"}
        />
        <BigCard
          label="Trades placed by bot today"
          value={`${today?.trades_placed ?? 0}`}
          hint={`${today?.filled ?? 0} filled • ${today?.pending ?? 0} pending • ${today?.rejected ?? 0} rejected`}
          tone="neutral"
        />
      </section>

      {/* Scanner buckets — Stocks / Indexes / Commodities */}
      <section>
        <SectionTitle
          title="What the bot is scanning"
          hint="Number of instruments that fit your available cash for at least one lot."
        />
        <div className="mt-3 grid grid-cols-1 gap-4 md:grid-cols-3">
          {["EQUITY", "INDEX", "COMMODITY"].map((k) => (
            <CategoryCard key={k} kind={k} bucket={findBucket(buckets, k)} />
          ))}
        </div>
      </section>

      {/* Candidate cards — the BRAIN output for each instrument */}
      <section>
        <SectionTitle
          title="Brain analysis — what would the bot do?"
          hint={
            scan?.min_score
              ? `Each card shows the multi-factor score and the entry checks. The bot only trades when ALL checks pass and the score ≥ ${Math.round(scan.min_score * 100)}.`
              : "Each card shows the multi-factor score and the entry checks for one instrument."
          }
        />
        <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
          {(data?.scanner ?? []).slice(0, 6).map((row, i) => (
            <CandidateCard key={`${row.exchange}:${row.token}`} row={row} isTop={i === 0} />
          ))}
          {(data?.scanner ?? []).length === 0 ? (
            <div className="col-span-full rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
              {data?.bot_running
                ? "Brain is warming up — needs ≥ 5 five-minute candles and ≥ 2 fifteen-minute candles to grade signals (10–30 minutes typically)."
                : "Click Start trading in the header to begin scanning."}
            </div>
          ) : null}
        </div>
      </section>

      {/* CE / PE made */}
      <section>
        <SectionTitle title="Calls (CE) and Puts (PE) the bot is in" />
        <div className="mt-3 grid grid-cols-1 gap-4 md:grid-cols-2">
          <SidePill
            label="Calls bought (CE)"
            tone="good"
            count={cepe?.ce_open ?? 0}
            capital={cepe?.capital_ce ?? 0}
            pnl={cepe?.pnl_ce ?? 0}
          />
          <SidePill
            label="Puts bought (PE)"
            tone="bad"
            count={cepe?.pe_open ?? 0}
            capital={cepe?.capital_pe ?? 0}
            pnl={cepe?.pnl_pe ?? 0}
          />
        </div>
      </section>

      {/* Open positions with per-row close */}
      <PositionsPanel positions={positions ?? null} />

      {/* Bot reasoning stream — full transparency */}
      <section className="card p-4">
        <SectionTitle
          title="What the bot is thinking (live)"
          hint="One row per scan cycle and per trade decision. Plain English in the Reason column."
        />
        <div className="mt-3">
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

function findBucket(buckets: ScannerBucket[], kind: string): ScannerBucket | null {
  return buckets.find((b) => b.kind.toUpperCase() === kind) || null;
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

function SectionTitle({ title, hint }: { title: string; hint?: string }) {
  return (
    <div>
      <h2 className="text-sm font-semibold tracking-wide text-slate-200">{title}</h2>
      {hint ? <div className="mt-1 text-xs text-slate-400">{hint}</div> : null}
    </div>
  );
}

function BigCard({
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
  const accent =
    tone === "good"
      ? "from-emerald-400/40 to-emerald-400/0"
      : tone === "bad"
      ? "from-rose-400/40 to-rose-400/0"
      : "from-sky-400/30 to-sky-400/0";
  return (
    <div className="card relative overflow-hidden p-5">
      <div className={`absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r ${accent}`} />
      <div className="stat-label">{label}</div>
      <div className={`mt-2 text-3xl font-semibold tracking-tight ${valueClass}`}>{value}</div>
      <div className="mt-2 text-xs text-slate-400">{hint}</div>
    </div>
  );
}

function CategoryCard({ kind, bucket }: { kind: string; bucket: ScannerBucket | null }) {
  const friendly: Record<string, { title: string; emoji: string; help: string }> = {
    EQUITY: { title: "Stocks", emoji: "📈", help: "NSE / BSE equities and stock options" },
    INDEX: { title: "Indexes", emoji: "📊", help: "NIFTY, BANKNIFTY and similar" },
    COMMODITY: { title: "Commodities", emoji: "🛢️", help: "MCX gold, crude oil, etc." },
  };
  const meta = friendly[kind] || { title: kind, emoji: "•", help: "" };
  const total = bucket?.count ?? 0;
  const tradable = bucket?.tradable ?? 0;
  const empty = total === 0;
  return (
    <div className="card relative overflow-hidden p-5">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-xs uppercase tracking-wider text-slate-400">
            {meta.emoji} {meta.title}
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            <div className="text-3xl font-semibold tracking-tight text-slate-100">{tradable}</div>
            <div className="text-xs text-slate-400">/ {total} watched</div>
          </div>
          <div className="mt-1 text-xs text-slate-500">tradable with current cash</div>
        </div>
      </div>
      <div className="mt-3 min-h-[2.5rem]">
        {empty ? (
          <div className="text-xs text-slate-500">
            None in your <code>SCANNER_WATCHLIST_JSON</code>. Add tokens to start tracking.
          </div>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {(bucket?.names ?? []).map((n) => (
              <span key={n} className="pill-blue text-[11px]">{n}</span>
            ))}
            {bucket?.top_name ? (
              <span className="ml-auto text-[11px] text-slate-400">
                strongest: <span className="text-slate-200">{bucket.top_name}</span>
              </span>
            ) : null}
          </div>
        )}
      </div>
      <div className="mt-3 text-[11px] text-slate-500">{meta.help}</div>
    </div>
  );
}

function SidePill({
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
  const accent =
    tone === "good"
      ? "from-emerald-400/40 to-emerald-400/0"
      : "from-rose-400/40 to-rose-400/0";
  return (
    <div className="card relative overflow-hidden p-5">
      <div className={`absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r ${accent}`} />
      <div className={`text-xs uppercase tracking-wider ${headClass}`}>{label}</div>
      <div className="mt-2 flex items-baseline gap-2">
        <div className="text-3xl font-semibold text-slate-100">{count}</div>
        <div className="text-xs text-slate-400">open positions</div>
      </div>
      <div className="mt-2 text-sm text-slate-300">
        Capital used <span className="font-semibold">{formatINR(capital)}</span>
      </div>
      <div className={`text-sm ${classOf(pnl)}`}>
        Open P&amp;L <span className="font-semibold">{formatINR(pnl)}</span>
      </div>
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
