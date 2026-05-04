import { useState } from "react";
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

type Group = {
  underlying: string;
  index: ScannerHit | null;
  ce: ScannerHit[];
  pe: ScannerHit[];
};

function expiryLabel(iso: string | undefined | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-IN", { day: "2-digit", month: "short" });
}

function pickAtm(rows: ScannerHit[]): ScannerHit | null {
  if (rows.length === 0) return null;
  const exact = rows.find((r) => (r.offset ?? 99) === 0);
  if (exact) return exact;
  return rows.slice().sort(
    (a, b) => Math.abs(a.offset ?? 0) - Math.abs(b.offset ?? 0),
  )[0];
}

function groupHits(hits: ScannerHit[]): Group[] {
  const buckets = new Map<string, Group>();
  for (const h of hits) {
    const kind = (h.kind || "").toUpperCase();
    if (kind === "INDEX") {
      const key = (h.underlying || h.name || "").toUpperCase();
      if (!key) continue;
      const g = buckets.get(key) ?? { underlying: key, index: null, ce: [], pe: [] };
      g.index = h;
      buckets.set(key, g);
    } else if (kind === "OPTION") {
      if (h.is_affordable === false) continue;
      const key = (h.underlying || "").toUpperCase();
      if (!key) continue;
      const g = buckets.get(key) ?? { underlying: key, index: null, ce: [], pe: [] };
      if ((h.option_side || "").toUpperCase() === "CE") g.ce.push(h);
      else if ((h.option_side || "").toUpperCase() === "PE") g.pe.push(h);
      buckets.set(key, g);
    }
  }
  for (const g of buckets.values()) {
    g.ce.sort((a, b) => (a.offset ?? 0) - (b.offset ?? 0));
    g.pe.sort((a, b) => (a.offset ?? 0) - (b.offset ?? 0));
  }
  return Array.from(buckets.values()).sort((a, b) =>
    a.underlying.localeCompare(b.underlying),
  );
}

export default function WatchlistPanel({
  hits,
  availableCash,
  market,
  enabled,
  onToggle,
  togglePending,
}: Props) {
  const groups = groupHits(hits);

  return (
    <div className="card overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-white/5 px-3 py-2">
        <div className="flex items-baseline gap-2">
          <span className="text-xs font-semibold uppercase tracking-wider text-slate-200">
            Indexes
          </span>
          <span className="text-[10px] text-slate-500">{groups.length}</span>
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
          <ToggleSwitch
            enabled={enabled}
            pending={!!togglePending}
            onChange={onToggle}
            label="Watch & trade"
          />
        ) : null}
      </div>

      {!enabled ? (
        <div className="p-4 text-center text-[11px] text-slate-500">
          Index option trading paused. Toggle to resume.
        </div>
      ) : groups.length === 0 ? (
        <div className="p-4 text-center text-[11px] text-slate-500">
          No indexes resolved yet.
        </div>
      ) : (
        <ul className="divide-y divide-white/5">
          {groups.map((g) => (
            <IndexRow key={g.underlying} group={g} availableCash={availableCash} />
          ))}
        </ul>
      )}
    </div>
  );
}

function IndexRow({ group, availableCash }: { group: Group; availableCash: number }) {
  const [open, setOpen] = useState(false);
  const idx = group.index;
  const change = idx?.change_pct ?? null;
  const tone = classOf(change ?? 0);
  const atmCe = pickAtm(group.ce);
  const atmPe = pickAtm(group.pe);
  const expiry = atmCe?.expiry || atmPe?.expiry || "";
  const hasOptions = group.ce.length > 0 || group.pe.length > 0;
  const extraCount =
    Math.max(0, group.ce.length - 1) + Math.max(0, group.pe.length - 1);

  return (
    <li>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-white/5"
      >
        <span className="w-16 shrink-0 truncate text-[12px] font-semibold tracking-tight text-slate-100">
          {group.underlying}
        </span>
        <span className={`w-20 shrink-0 text-right text-[12px] font-semibold tabular-nums ${tone}`}>
          {idx?.last_price != null
            ? idx.last_price.toLocaleString("en-IN", { maximumFractionDigits: 0 })
            : "—"}
        </span>
        <span className={`w-12 shrink-0 text-right text-[10px] tabular-nums ${tone}`}>
          {change == null ? "" : (change > 0 ? "+" : "") + formatPct(change)}
        </span>
        <span className="ml-1 flex min-w-0 flex-1 items-center gap-1">
          {atmCe ? (
            <AtmPill side="CE" h={atmCe} availableCash={availableCash} />
          ) : (
            <span className="text-[10px] text-slate-600">no CE</span>
          )}
          {atmPe ? (
            <AtmPill side="PE" h={atmPe} availableCash={availableCash} />
          ) : (
            <span className="text-[10px] text-slate-600">no PE</span>
          )}
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5 text-[10px] text-slate-500">
          {expiry ? (
            <span className="rounded bg-slate-800/80 px-1.5 py-0.5 uppercase tracking-wider">
              {expiryLabel(expiry)}
            </span>
          ) : null}
          {extraCount > 0 ? <span>+{extraCount}</span> : null}
          {hasOptions ? <span>{open ? "▾" : "▸"}</span> : null}
        </span>
      </button>

      {open && hasOptions ? (
        <div className="space-y-1 bg-slate-950/40 px-3 pb-3 pt-1">
          {group.ce.length > 0 ? (
            <StrikeStrip rows={group.ce} side="CE" availableCash={availableCash} />
          ) : null}
          {group.pe.length > 0 ? (
            <StrikeStrip rows={group.pe} side="PE" availableCash={availableCash} />
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

function AtmPill({
  side,
  h,
  availableCash,
}: {
  side: "CE" | "PE";
  h: ScannerHit;
  availableCash: number;
}) {
  const lot = h.notional_per_lot ?? null;
  const aff = h.affordable_lots ?? 0;
  const sideTone = side === "CE" ? "text-emerald-300 border-emerald-500/30" : "text-rose-300 border-rose-500/30";
  const lotTone =
    lot != null && availableCash > 0 && lot > availableCash ? "text-amber-300" : "text-slate-300";
  const score = Math.round((h.score ?? 0) * 100);
  return (
    <span
      className={`group flex min-w-0 items-center gap-1 rounded border ${sideTone} bg-slate-900/40 px-1.5 py-1 text-[11px]`}
      title={`${h.tradingsymbol || h.name}${h.strike ? ` · strike ${h.strike}` : ""} · 1 lot ${lot != null ? formatINR(lot) : "—"} · ${aff} affordable · score ${score}`}
    >
      <span className="font-semibold uppercase tracking-wider">{side}</span>
      <span className="font-semibold tabular-nums text-slate-100">
        {formatINR(h.last_price)}
      </span>
      <span className={`tabular-nums ${lotTone}`}>
        ×{h.lot_size ?? "?"}
      </span>
      {aff > 0 ? null : (
        <span className="text-amber-300">!</span>
      )}
    </span>
  );
}

function StrikeStrip({
  rows,
  side,
  availableCash,
}: {
  rows: ScannerHit[];
  side: "CE" | "PE";
  availableCash: number;
}) {
  return (
    <div className="flex items-center gap-1.5 text-[10px]">
      <span
        className={`shrink-0 font-semibold uppercase tracking-wider ${
          side === "CE" ? "text-emerald-300" : "text-rose-300"
        }`}
      >
        {side}
      </span>
      <div className="flex flex-wrap gap-1">
        {rows.map((o) => {
          const lot = o.notional_per_lot ?? null;
          const lotTone =
            lot != null && availableCash > 0 && lot > availableCash
              ? "text-amber-300"
              : "text-slate-300";
          const off = o.offset ?? 0;
          const offLabel = off === 0 ? "ATM" : off > 0 ? `+${off}` : `${off}`;
          return (
            <span
              key={o.token}
              className="rounded bg-slate-800/70 px-1.5 py-0.5 tabular-nums"
              title={`${o.tradingsymbol || o.name} · 1 lot ${lot != null ? formatINR(lot) : "—"} · ${o.affordable_lots ?? 0} affordable`}
            >
              <span className="text-slate-500">{offLabel}</span>{" "}
              <span className="text-slate-200">
                {o.strike ? o.strike.toLocaleString("en-IN") : "?"}
              </span>{" "}
              <span className="text-slate-300">{formatINR(o.last_price)}</span>
              {lot != null ? (
                <span className={lotTone}> ({formatINR(lot, { compact: true })})</span>
              ) : null}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function ToggleSwitch({
  enabled,
  pending,
  onChange,
  label,
}: {
  enabled: boolean;
  pending: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={enabled}
      title={label}
      disabled={pending}
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-4 w-8 shrink-0 items-center rounded-full transition focus:outline-none focus:ring-2 focus:ring-sky-400/50 ${
        enabled ? "bg-emerald-500/80" : "bg-slate-600/60"
      } ${pending ? "opacity-60" : ""}`}
    >
      <span
        className={`inline-block h-3 w-3 transform rounded-full bg-white shadow transition ${
          enabled ? "translate-x-4" : "translate-x-0.5"
        }`}
      />
      <span className="sr-only">{label}</span>
    </button>
  );
}
