import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiGet,
  apiPost,
  type KillSwitchReport,
  type MarketStatus,
  type PaperPosition,
  type PositionRow,
  type PositionsResponse,
  type RateLimitSummary,
  type ScannerBucket,
  type Snapshot,
  type UniverseBlock,
} from "../lib/api";
import { classOf, formatINR, formatTime } from "../lib/format";
import PositionsPanel from "../components/PositionsPanel";
import DecisionsTable from "../components/DecisionsTable";
import KillSwitchModal from "../components/KillSwitchModal";
import CandidateCard from "../components/CandidateCard";
import DryrunCapital from "../components/DryrunCapital";
import PaperTradesPanel from "../components/PaperTradesPanel";

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
  const buckets = data?.scanner_by_kind?.buckets ?? [];
  const rate = data?.rate_limit;
  const isLive = !!data?.trading_enabled;
  const paper = data?.paper;
  const dryrun = data?.dryrun;

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
            <RateLimitChip rate={rate} />
            <UniverseChip universe={data?.universe} />
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

      {/* Money / P&L hero numbers */}
      <section className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <BigCard
          label="Money in your account"
          value={formatINR(funds?.available_cash ?? 0)}
          hint={
            isLive
              ? "Real broker cash. Updated every loop."
              : dryrun && dryrun.capital_override > 0
              ? `Real cash; sizing uses your dry-run override (${formatINR(dryrun.capital_override)}).`
              : "Real broker cash. Dry-run sim is sizing against the same amount."
          }
          tone="neutral"
        />
        <BigCard
          label={isLive ? "Live profit / loss today" : "Dry-run profit / loss today"}
          value={formatINR(totalPnl)}
          hint={`Realized ${formatINR(today?.realized_pnl ?? 0, { compact: true })} • Unrealized ${formatINR(today?.unrealized_pnl ?? 0, { compact: true })} • from ${pnlSourceLabel}`}
          tone={totalPnl > 0 ? "good" : totalPnl < 0 ? "bad" : "neutral"}
        />
        <BigCard
          label={isLive ? "Trades placed by bot today" : "Paper trades today"}
          value={`${today?.trades_placed ?? 0}`}
          hint={
            isLive
              ? `${today?.filled ?? 0} filled • ${today?.pending ?? 0} pending • ${today?.rejected ?? 0} rejected`
              : `${today?.filled ?? 0} closed • ${paper?.open?.open_positions ?? 0} still open`
          }
          tone="neutral"
        />
      </section>

      {/* Scanner buckets — Stocks / Indexes (commodities intentionally
          excluded: each MCX symbol counts toward Angel's per-request token
          cap and reliably triggers AB1004 once we add the option chain on
          top). */}
      <section>
        <SectionTitle
          title="What the bot is scanning"
          hint="Number of instruments that fit your available cash for at least one lot."
        />
        <div className="mt-3 grid grid-cols-1 gap-4 md:grid-cols-2">
          {["EQUITY", "INDEX"].map((k) => {
            const toggleKey = k === "INDEX" ? "OPTION" : k;
            const enabled = data?.universe?.kind_enabled?.[toggleKey as "EQUITY" | "OPTION" | "COMMODITY"] ?? true;
            const market = data?.market_hours?.[toggleKey] ?? data?.market_hours?.[k];
            return (
              <CategoryCard
                key={k}
                kind={k}
                bucket={findBucket(buckets, k)}
                enabled={enabled}
                pending={toggleKind.isPending}
                onToggle={(v) => toggleKind.mutate({ [toggleKey]: v })}
                market={market}
              />
            );
          })}
        </div>
      </section>

      {/* Active trade(s) hero — always above brain analysis when something is live */}
      <ActiveTradesHero
        isLive={isLive}
        positions={positions}
        paperRows={paper?.open?.rows ?? []}
      />

      {/* Candidate cards — the BRAIN output for each instrument */}
      {(() => {
        const allHits = data?.scanner ?? [];
        // Affordability filter: hide cards whose one-lot cost exceeds available cash.
        // INDEX hits are always kept because the actual trade is an ATM option
        // (~₹2-15k premium per lot), not the index spot itself.
        const visibleHits = allHits.filter((row) => {
          if ((row.kind || "").toUpperCase() === "INDEX") return true;
          if ((row.affordable_lots ?? 0) >= 1) return true;
          return false;
        });
        const hiddenCount = allHits.length - visibleHits.length;
        return (
          <section>
            <SectionTitle
              title="Brain analysis — what would the bot do?"
              hint={
                scan?.min_score
                  ? `Each card shows the multi-factor score and the entry checks. The bot only trades when ALL checks pass and the score ≥ ${Math.round(scan.min_score * 100)}.`
                  : "Each card shows the multi-factor score and the entry checks for one instrument."
              }
            />
            {hiddenCount > 0 ? (
              <div className="mt-2 text-[11px] text-slate-500">
                Hiding{" "}
                <span className="text-slate-300">{hiddenCount}</span> instrument
                {hiddenCount === 1 ? "" : "s"} whose one-lot cost is above your available cash
                of <span className="text-slate-300">{formatINR(funds?.available_cash ?? 0)}</span>.
              </div>
            ) : null}
            <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
              {visibleHits.slice(0, 6).map((row, i) => (
                <CandidateCard key={`${row.exchange}:${row.token}`} row={row} isTop={i === 0} />
              ))}
              {visibleHits.length === 0 ? (
                <div className="col-span-full rounded-lg border border-dashed border-white/10 bg-slate-900/40 p-6 text-center text-sm text-slate-400">
                  {allHits.length > 0
                    ? `All ${allHits.length} scanned instruments need more cash than you have for one lot. Add funds, or adjust SCANNER_WATCHLIST_JSON to include cheaper symbols.`
                    : data?.bot_running
                    ? "Brain is warming up — needs ≥ 5 five-minute candles and ≥ 2 fifteen-minute candles to grade signals (10–30 minutes typically)."
                    : "Click Start trading in the header to begin scanning."}
                </div>
              ) : null}
            </div>
          </section>
        );
      })()}

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

      {/* Paper trades panel — only matters in dry-run */}
      {!isLive ? <PaperTradesPanel paper={paper} /> : null}

      {/* Open broker positions with per-row close */}
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

function CategoryCard({
  kind,
  bucket,
  enabled,
  pending,
  onToggle,
  market,
}: {
  kind: string;
  bucket: ScannerBucket | null;
  enabled: boolean;
  pending: boolean;
  onToggle: (next: boolean) => void;
  market: MarketStatus | undefined;
}) {
  const friendly: Record<string, { title: string; emoji: string; help: string }> = {
    EQUITY: { title: "Stocks", emoji: "📈", help: "NSE / BSE equities and stock options" },
    INDEX: { title: "Indexes", emoji: "📊", help: "NIFTY, BANKNIFTY and similar" },
    COMMODITY: { title: "Commodities", emoji: "🛢️", help: "MCX gold, crude oil, etc." },
  };
  const meta = friendly[kind] || { title: kind, emoji: "•", help: "" };
  const total = bucket?.count ?? 0;
  const tradable = bucket?.tradable ?? 0;
  const empty = total === 0;
  const marketOpen = market?.is_open ?? true;
  // Card is dim when user disabled OR when market closed.
  const dim = !enabled || !marketOpen;
  return (
    <div
      className={`card relative overflow-hidden p-5 transition ${
        dim ? "opacity-60" : ""
      }`}
    >
      {!marketOpen ? (
        <div className="absolute right-0 top-0 rounded-bl-md bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
          Market closed
        </div>
      ) : (
        <div className="absolute right-0 top-0 rounded-bl-md bg-emerald-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-emerald-300">
          Market open
        </div>
      )}

      <div className="flex items-start justify-between gap-3 pt-4">
        <div>
          <div className="text-xs uppercase tracking-wider text-slate-400">
            {meta.emoji} {meta.title}
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            <div className="text-3xl font-semibold tracking-tight text-slate-100">{tradable}</div>
            <div className="text-xs text-slate-400">/ {total} watched</div>
          </div>
          <div className="mt-1 text-xs text-slate-500">
            {enabled ? "tradable with current cash" : "disabled — bot will not watch or trade"}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1">
          <span className="text-[10px] uppercase tracking-wider text-slate-400">
            Watch & trade
          </span>
          <KindToggle
            enabled={enabled}
            pending={pending}
            onChange={(v) => onToggle(v)}
            title={
              kind === "INDEX"
                ? "Watch & trade index ATM options (NIFTY/BANKNIFTY CE/PE)"
                : `Watch & trade ${meta.title.toLowerCase()}`
            }
          />
          <span className={`text-[10px] ${enabled ? "text-emerald-300" : "text-slate-400"}`}>
            {enabled ? "ON" : "OFF"}
          </span>
        </div>
      </div>

      {/* Market hours line — always visible so the user knows the session window. */}
      <div className="mt-3">
        <MarketLine market={market} />
      </div>

      <div className="mt-3 min-h-[2.5rem]">
        {!enabled ? (
          <div className="text-xs text-slate-500">
            Disabled by you. Re-enable to resume polling and trading.
          </div>
        ) : !marketOpen ? (
          <div className="text-xs text-amber-200/80">
            {market?.label ?? "Exchange"} is closed.{" "}
            {market?.reason === "weekend"
              ? "Reopens "
              : market?.reason === "before_open"
              ? "Opens at "
              : "Reopens "}
            <span className="font-semibold text-amber-200">{market?.opens_at_label ?? "soon"}</span>.
          </div>
        ) : empty ? (
          <div className="text-xs text-slate-500">
            None in the active universe.{" "}
            <a href="/dashboard/universe" className="text-sky-300 underline">
              Edit universe
            </a>
            .
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

type HeroTrade = {
  key: string;
  symbol: string;
  side: "CE" | "PE" | "-";
  qty: number;
  entry: number | null;
  ltp: number | null;
  pnl: number | null;
  capital: number;
  source: "live" | "paper";
  opened_at: string | null;
};

function buildHeroTrades(
  isLive: boolean,
  positions: PositionsResponse | undefined | null,
  paperRows: PaperPosition[],
): HeroTrade[] {
  if (isLive) {
    const rows: PositionRow[] = (positions?.rows ?? []).filter((r) => Math.abs(r.net_qty) > 0);
    return rows.map((r) => ({
      key: `live:${r.exchange}:${r.symboltoken}`,
      symbol: r.tradingsymbol,
      side: r.side,
      qty: r.net_qty,
      entry: r.buy_avg ?? r.sell_avg ?? null,
      ltp: r.ltp,
      pnl: r.pnl,
      capital: r.capital_used ?? 0,
      source: "live",
      opened_at: null,
    }));
  }
  return paperRows.map((p) => ({
    key: `paper:${p.exchange}:${p.symboltoken}`,
    symbol: p.tradingsymbol,
    side: p.side,
    qty: p.qty,
    entry: p.entry_price,
    ltp: p.last_price,
    pnl: p.unrealized_pnl,
    capital: p.capital_used,
    source: "paper",
    opened_at: p.opened_at,
  }));
}

function ActiveTradesHero({
  isLive,
  positions,
  paperRows,
}: {
  isLive: boolean;
  positions: PositionsResponse | undefined | null;
  paperRows: PaperPosition[];
}) {
  const trades = buildHeroTrades(isLive, positions, paperRows);
  if (trades.length === 0) {
    return (
      <section className="card border border-white/5 p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold tracking-wide text-slate-200">
              Active trade
            </h2>
            <div className="mt-1 text-xs text-slate-400">
              No open {isLive ? "broker" : "paper"} positions right now. The bot is scanning —
              your active trade will appear here the moment it enters one.
            </div>
          </div>
          <div className="rounded-md bg-slate-800/60 px-3 py-1.5 text-[11px] uppercase tracking-wider text-slate-400">
            idle
          </div>
        </div>
      </section>
    );
  }

  const totalPnl = trades.reduce((s, t) => s + (t.pnl ?? 0), 0);
  const totalCap = trades.reduce((s, t) => s + (t.capital ?? 0), 0);
  const tone = totalPnl > 0 ? "good" : totalPnl < 0 ? "bad" : "neutral";
  const accent =
    tone === "good"
      ? "from-emerald-400/40 to-emerald-400/0"
      : tone === "bad"
      ? "from-rose-400/40 to-rose-400/0"
      : "from-sky-400/40 to-sky-400/0";

  return (
    <section className="card relative overflow-hidden border border-white/10 p-5">
      <div className={`absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r ${accent}`} />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold tracking-wide text-slate-100">
            Active trade{trades.length > 1 ? `s (${trades.length})` : ""}
          </h2>
          <div className="mt-0.5 text-[11px] uppercase tracking-wider text-slate-400">
            {isLive ? "Live broker positions" : "Paper positions (dry-run)"}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-4 text-right">
          <Stat label="Capital deployed" value={formatINR(totalCap)} />
          <Stat
            label={trades.length > 1 ? "Combined P&L" : "P&L"}
            value={formatINR(totalPnl)}
            tone={tone}
          />
        </div>
      </div>
      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-2">
        {trades.map((t) => (
          <ActiveTradeCard key={t.key} trade={t} />
        ))}
      </div>
    </section>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "good" | "bad" | "neutral";
}) {
  const cls =
    tone === "good"
      ? "text-emerald-300"
      : tone === "bad"
      ? "text-rose-300"
      : "text-slate-100";
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-slate-400">{label}</div>
      <div className={`text-lg font-semibold tabular-nums ${cls}`}>{value}</div>
    </div>
  );
}

function ActiveTradeCard({ trade }: { trade: HeroTrade }) {
  const sideTone =
    trade.side === "CE" ? "text-emerald-300" : trade.side === "PE" ? "text-rose-300" : "text-slate-300";
  const pnl = trade.pnl ?? 0;
  const pnlTone =
    pnl > 0 ? "text-emerald-300" : pnl < 0 ? "text-rose-300" : "text-slate-200";
  const move =
    trade.entry && trade.entry > 0 && trade.ltp != null
      ? ((trade.ltp - trade.entry) / trade.entry) * 100
      : null;
  const moveSign = move == null ? "" : move > 0 ? "+" : "";
  return (
    <div className="rounded-lg border border-white/10 bg-slate-900/40 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="font-semibold tracking-tight text-slate-100">{trade.symbol}</div>
          <div className="mt-0.5 flex items-center gap-2 text-[11px]">
            <span className={trade.side === "CE" ? "pill-green" : trade.side === "PE" ? "pill-red" : "pill-blue"}>
              {trade.side}
            </span>
            <span className="text-slate-400">qty {trade.qty}</span>
            {trade.opened_at ? (
              <span className="text-slate-500">since {formatTime(trade.opened_at)}</span>
            ) : null}
          </div>
        </div>
        <div className="text-right">
          <div className={`text-xl font-semibold tabular-nums ${pnlTone}`}>
            {formatINR(pnl)}
          </div>
          <div className="text-[11px] text-slate-400">unrealized P&L</div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-3 text-[11px]">
        <Mini label="Entry" value={trade.entry != null ? formatINR(trade.entry) : "—"} />
        <Mini
          label="LTP"
          value={trade.ltp != null ? formatINR(trade.ltp) : "—"}
          extra={
            move == null
              ? ""
              : `${moveSign}${move.toFixed(2)}%`
          }
          extraTone={
            move == null || Math.abs(move) < 1e-6
              ? "neutral"
              : move > 0
              ? "good"
              : "bad"
          }
        />
        <Mini label="Capital" value={formatINR(trade.capital)} />
      </div>
      <div className={`mt-2 text-[10px] uppercase tracking-wider ${sideTone}`}>
        {trade.source === "live" ? "Real money" : "Simulated (dry-run)"}
      </div>
    </div>
  );
}

function Mini({
  label,
  value,
  extra,
  extraTone,
}: {
  label: string;
  value: string;
  extra?: string;
  extraTone?: "good" | "bad" | "neutral";
}) {
  const cls =
    extraTone === "good"
      ? "text-emerald-300"
      : extraTone === "bad"
      ? "text-rose-300"
      : "text-slate-400";
  return (
    <div className="rounded-md bg-slate-950/40 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="font-semibold tabular-nums text-slate-100">{value}</div>
      {extra ? <div className={`text-[10px] tabular-nums ${cls}`}>{extra}</div> : null}
    </div>
  );
}

function MarketLine({ market }: { market: MarketStatus | undefined }) {
  if (!market) return null;
  if (market.is_open) {
    return (
      <div className="text-[11px] text-emerald-300/90">
        {market.label} open · closes {market.closes_at_label}
      </div>
    );
  }
  return (
    <div className="text-[11px] text-amber-300/90">
      {market.label} closed ·{" "}
      {market.reason === "weekend"
        ? "weekend — reopens "
        : market.reason === "before_open"
        ? "opens "
        : "reopens "}
      {market.opens_at_label}
    </div>
  );
}

function KindToggle({
  enabled,
  pending,
  onChange,
  title,
}: {
  enabled: boolean;
  pending: boolean;
  onChange: (next: boolean) => void;
  title: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      title={title}
      disabled={pending}
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition focus:outline-none focus:ring-2 focus:ring-sky-400/50 ${
        enabled ? "bg-emerald-500/80" : "bg-slate-600/60"
      } ${pending ? "opacity-60" : ""}`}
    >
      <span
        className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition ${
          enabled ? "translate-x-5" : "translate-x-0.5"
        }`}
      />
      <span className="sr-only">{enabled ? "Disable" : "Enable"} {title}</span>
    </button>
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
