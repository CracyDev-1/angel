import type { MarketStatus, ScannerHit } from "../lib/api";
import { classOf, formatINR, formatPct } from "../lib/format";

type Props = {
  hits: ScannerHit[];
  availableCash: number;
  market?: MarketStatus;
  enabled: boolean;
  onToggle?: (next: boolean) => void;
  togglePending?: boolean;
};

export default function StocksPanel({
  hits,
  availableCash,
  market,
  enabled,
  onToggle,
  togglePending,
}: Props) {
  const stocks = hits
    .filter((h) => (h.kind || "").toUpperCase() === "EQUITY")
    .slice()
    .sort((a, b) => (b.score ?? 0) - (a.score ?? 0));

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-white/5 px-3 py-2">
        <div className="flex items-baseline gap-2">
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-200">
            Stocks
          </span>
          <span className="text-[10px] text-slate-500">{stocks.length}</span>
          {market ? (
            <span
              className={`text-[10px] uppercase tracking-wider ${
                market.is_open ? "text-emerald-300" : "text-amber-300"
              }`}
            >
              {market.is_open ? "open" : market.opens_at_label ? `opens ${market.opens_at_label}` : "closed"}
            </span>
          ) : null}
        </div>
        {onToggle ? (
          <button
            type="button"
            role="switch"
            aria-checked={enabled}
            disabled={!!togglePending}
            onClick={() => onToggle(!enabled)}
            className={`relative inline-flex h-4 w-8 shrink-0 items-center rounded-full transition focus:outline-none focus:ring-2 focus:ring-sky-400/50 ${
              enabled ? "bg-emerald-500/80" : "bg-slate-600/60"
            } ${togglePending ? "opacity-60" : ""}`}
            title="Watch & trade equities"
          >
            <span
              className={`inline-block h-3 w-3 transform rounded-full bg-white shadow transition ${
                enabled ? "translate-x-4" : "translate-x-0.5"
              }`}
            />
            <span className="sr-only">Watch & trade equities</span>
          </button>
        ) : null}
      </div>

      {!enabled ? (
        <div className="p-4 text-center text-[11px] text-slate-500">
          Equity trading paused. Toggle to resume.
        </div>
      ) : stocks.length === 0 ? (
        <div className="p-4 text-center text-[11px] text-slate-500">
          No stocks resolved.
        </div>
      ) : (
        <ul className="divide-y divide-white/5">
          {stocks.map((s) => {
            const lot = s.notional_per_lot ?? null;
            const aff = s.affordable_lots ?? 0;
            const change = s.change_pct ?? null;
            const tone = classOf(change ?? 0);
            const lotTone =
              lot != null && availableCash > 0 && lot > availableCash
                ? "text-amber-300"
                : "text-slate-300";
            return (
              <li
                key={`${s.exchange}:${s.token}`}
                className="flex items-center gap-2 px-3 py-1.5 text-[12px]"
                title={`lot ×${s.lot_size ?? "?"} · 1 lot ${lot != null ? formatINR(lot) : "—"} · ${aff} affordable`}
              >
                <span className="min-w-0 flex-1 truncate font-medium tracking-tight text-slate-100">
                  {s.name}
                </span>
                <span className={`w-20 shrink-0 text-right tabular-nums ${tone}`}>
                  {s.last_price != null
                    ? s.last_price.toLocaleString("en-IN", { maximumFractionDigits: 2 })
                    : "—"}
                </span>
                <span className={`w-12 shrink-0 text-right text-[10px] tabular-nums ${tone}`}>
                  {change == null ? "" : (change > 0 ? "+" : "") + formatPct(change)}
                </span>
                <span className={`w-20 shrink-0 text-right text-[10px] tabular-nums ${lotTone}`}>
                  {lot != null ? formatINR(lot, { compact: true }) : "—"}
                </span>
                <span
                  className={`w-10 shrink-0 text-right text-[10px] tabular-nums ${
                    aff > 0 ? "text-slate-400" : "text-amber-300"
                  }`}
                >
                  {aff > 0 ? `${aff} lot` : "0"}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
