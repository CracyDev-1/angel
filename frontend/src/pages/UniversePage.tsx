import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  apiGet,
  apiPost,
  type InstrumentSearchRow,
  type Snapshot,
  type UniverseBlock,
  type UniverseEntry,
} from "../lib/api";
import { formatINR } from "../lib/format";

/**
 * Dynamic universe & instrument-master control panel.
 *
 * Surfaces the data that the bot uses to *resolve* tokens and lot sizes:
 *   • Instrument master file: path, age, bytes, count, refresh button.
 *   • Universe spec (indices / stocks / commodities / atm_for) — read-only here,
 *     editable via /api/universe (Settings page can wire that later).
 *   • Resolved watchlist with all tokens + lot sizes pulled from the master.
 *   • Live ATM CE/PE preview for each underlying with options.
 *   • Instrument search (autocomplete) so the user can verify a symbol
 *     resolves correctly before adding it to the spec.
 */

export default function UniversePage() {
  const qc = useQueryClient();
  const snap = useQuery({
    queryKey: ["status"],
    queryFn: () => apiGet<Snapshot>("/api/status"),
    refetchInterval: 4000,
  });
  const universe: UniverseBlock | undefined = snap.data?.universe;

  const refresh = useMutation({
    mutationFn: () => apiPost("/api/instruments/refresh", { force: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["status"] }),
  });

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold text-slate-100">Universe & Instrument Master</h1>

      <MasterCard
        universe={universe}
        refreshing={refresh.isPending}
        onRefresh={() => refresh.mutate()}
        lastError={(refresh.error as Error | undefined)?.message}
      />

      <SpecCard universe={universe} />

      <WatchlistCard universe={universe} />

      <SearchCard />
    </div>
  );
}

// --------------------------------------------------------------------------- //

function MasterCard(p: {
  universe: UniverseBlock | undefined;
  refreshing: boolean;
  onRefresh: () => void;
  lastError?: string;
}) {
  const m = p.universe?.master;
  const ageHrs =
    m?.age_seconds != null ? (m.age_seconds / 3600).toFixed(1) : "—";
  const sizeMb = m?.bytes ? (m.bytes / (1024 * 1024)).toFixed(1) : "—";
  return (
    <section className="card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-400">
            Instrument master
          </div>
          <div className="mt-1 text-sm text-slate-100">
            {m
              ? `${(m.instruments ?? 0).toLocaleString()} instruments cached`
              : "No master loaded yet"}
          </div>
          <div className="mt-1 text-[11px] text-slate-400">
            Source: <span className="text-slate-200">{m?.source ?? "missing"}</span> •
            Age: <span className="text-slate-200">{ageHrs} h</span> •
            Size: <span className="text-slate-200">{sizeMb} MB</span>
            {m?.last_modified_iso ? (
              <>
                {" "}• Updated{" "}
                <span className="text-slate-200">
                  {new Date(m.last_modified_iso).toLocaleString()}
                </span>
              </>
            ) : null}
          </div>
          <div className="mt-1 text-[11px] text-slate-500 break-all">
            Path: {m?.path ?? "—"}
          </div>
        </div>
        <div className="flex flex-col items-end gap-2">
          <button
            className="btn-primary"
            onClick={p.onRefresh}
            disabled={p.refreshing}
          >
            {p.refreshing ? "Downloading…" : "Force refresh"}
          </button>
          {p.lastError ? (
            <div className="text-[11px] text-rose-400">{p.lastError}</div>
          ) : null}
          <div className="text-[11px] text-slate-500">
            Auto-downloads daily; click to re-pull now.
          </div>
        </div>
      </div>
    </section>
  );
}

// --------------------------------------------------------------------------- //

function SpecCard({ universe }: { universe: UniverseBlock | undefined }) {
  const spec = universe?.spec;
  const r = universe?.report;
  const Item = ({ label, vals, missing }: { label: string; vals: string[]; missing?: string[] }) => (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-1 flex flex-wrap gap-1">
        {(vals ?? []).map((v) => (
          <span
            key={v}
            className="rounded-full bg-slate-800/60 px-2 py-0.5 text-xs text-slate-200"
          >
            {v}
          </span>
        ))}
        {(missing ?? []).map((v) => (
          <span
            key={`miss-${v}`}
            className="rounded-full bg-amber-900/30 px-2 py-0.5 text-xs text-amber-300"
            title="Could not be resolved from the master"
          >
            ⚠ {v}
          </span>
        ))}
      </div>
    </div>
  );
  return (
    <section className="card p-4 space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-400">
            Universe spec (active)
          </div>
          <div className="text-[11px] text-slate-500">
            What the bot is looking at. Edit via UNIVERSE_SPEC_JSON in .env or POST /api/universe.
          </div>
        </div>
        <div className="text-[11px] text-slate-500">
          Last ATM refresh:{" "}
          <span className="text-slate-200">
            {universe?.last_atm_refresh_at
              ? new Date(universe.last_atm_refresh_at).toLocaleTimeString()
              : "—"}
          </span>
        </div>
      </div>
      <div className="grid gap-3 md:grid-cols-2">
        <Item label="Indices" vals={spec?.indices ?? []} missing={r?.indices_missing} />
        <Item label="Stocks (NSE EQ)" vals={spec?.stocks ?? []} missing={r?.stocks_missing} />
        <Item label="Commodities (MCX FUT)" vals={spec?.commodities ?? []} missing={r?.commodities_missing} />
        <Item label="ATM CE/PE for" vals={spec?.atm_for ?? []} missing={r?.atm_missing} />
      </div>
    </section>
  );
}

// --------------------------------------------------------------------------- //

function WatchlistCard({ universe }: { universe: UniverseBlock | undefined }) {
  const wl = universe?.watchlist ?? {};
  const exchanges = useMemo(() => Object.keys(wl).sort(), [wl]);
  return (
    <section className="card p-4 space-y-3">
      <div>
        <div className="text-xs uppercase tracking-wide text-slate-400">
          Resolved watchlist
        </div>
        <div className="text-[11px] text-slate-500">
          Tokens and lot sizes below come straight from the instrument master —
          nothing is hard-coded.
        </div>
      </div>
      {exchanges.length === 0 ? (
        <div className="text-sm text-slate-400">
          Universe is empty. Load the instrument master to populate.
        </div>
      ) : (
        exchanges.map((ex) => (
          <ExchangeBlock key={ex} exchange={ex} entries={wl[ex] ?? []} />
        ))
      )}
    </section>
  );
}

function ExchangeBlock({ exchange, entries }: { exchange: string; entries: UniverseEntry[] }) {
  return (
    <div className="rounded-xl border border-white/5 bg-slate-900/40 p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-semibold text-slate-200">{exchange}</div>
        <div className="text-[11px] text-slate-500">{entries.length} instruments</div>
      </div>
      <div className="overflow-auto">
        <table className="w-full text-xs">
          <thead className="text-[11px] uppercase text-slate-500">
            <tr>
              <th className="px-2 py-1 text-left">Symbol</th>
              <th className="px-2 py-1 text-left">Kind</th>
              <th className="px-2 py-1 text-left">Token</th>
              <th className="px-2 py-1 text-right">Lot size</th>
              <th className="px-2 py-1 text-left">Underlying</th>
              <th className="px-2 py-1 text-left">Expiry</th>
              <th className="px-2 py-1 text-right">Strike</th>
              <th className="px-2 py-1 text-left">Side</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5">
            {entries.map((e) => (
              <tr key={`${exchange}-${e.token}-${e.name}`} className="text-slate-200">
                <td className="px-2 py-1 font-mono">{e.name}</td>
                <td className="px-2 py-1">
                  <span className={kindPillClass(e.kind)}>{e.kind}</span>
                </td>
                <td className="px-2 py-1 font-mono text-slate-400">{e.token}</td>
                <td className="px-2 py-1 text-right">{e.lot_size}</td>
                <td className="px-2 py-1 text-slate-400">{e.underlying ?? "—"}</td>
                <td className="px-2 py-1 text-slate-400">{e.expiry ?? "—"}</td>
                <td className="px-2 py-1 text-right text-slate-400">
                  {e.strike ? formatINR(e.strike) : "—"}
                </td>
                <td className="px-2 py-1">
                  {e.side ? (
                    <span className={sidePillClass(e.side)}>{e.side}</span>
                  ) : (
                    <span className="text-slate-500">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function kindPillClass(kind: string): string {
  const base = "rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase";
  switch (kind.toUpperCase()) {
    case "INDEX":
      return `${base} bg-violet-500/15 text-violet-300`;
    case "EQUITY":
      return `${base} bg-emerald-500/15 text-emerald-300`;
    case "OPTION":
      return `${base} bg-sky-500/15 text-sky-300`;
    case "COMMODITY":
      return `${base} bg-orange-500/15 text-orange-300`;
    default:
      return `${base} bg-slate-700 text-slate-300`;
  }
}

function sidePillClass(side: string): string {
  const base = "rounded-full px-2 py-0.5 text-[10px] font-semibold";
  return side === "CE"
    ? `${base} bg-emerald-500/15 text-emerald-300`
    : `${base} bg-rose-500/15 text-rose-300`;
}

// --------------------------------------------------------------------------- //

function SearchCard() {
  const [q, setQ] = useState("");
  const [exchange, setExchange] = useState("");
  const search = useQuery({
    queryKey: ["instruments-search", q, exchange],
    queryFn: () => {
      const params = new URLSearchParams({ q, limit: "20" });
      if (exchange) params.set("exchange", exchange);
      return apiGet<InstrumentSearchRow[]>(`/api/instruments/search?${params.toString()}`);
    },
    enabled: q.trim().length >= 2,
  });
  const rows = search.data ?? [];

  return (
    <section className="card p-4 space-y-3">
      <div>
        <div className="text-xs uppercase tracking-wide text-slate-400">
          Instrument search
        </div>
        <div className="text-[11px] text-slate-500">
          Verify any symbol resolves to a token + lot size before trading it.
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Type 2+ chars (e.g. RELI, NIFTY24, CRUDE)"
          className="input min-w-64 flex-1"
        />
        <select
          className="input"
          value={exchange}
          onChange={(e) => setExchange(e.target.value)}
        >
          <option value="">All exchanges</option>
          <option value="NSE">NSE</option>
          <option value="BSE">BSE</option>
          <option value="NFO">NFO</option>
          <option value="BFO">BFO</option>
          <option value="MCX">MCX</option>
          <option value="CDS">CDS</option>
        </select>
      </div>
      {q.trim().length < 2 ? (
        <div className="text-xs text-slate-500">
          Type at least 2 characters to search.
        </div>
      ) : search.isFetching ? (
        <div className="text-xs text-slate-500">Searching…</div>
      ) : rows.length === 0 ? (
        <div className="text-xs text-slate-500">No matches.</div>
      ) : (
        <div className="overflow-auto">
          <table className="w-full text-xs">
            <thead className="text-[11px] uppercase text-slate-500">
              <tr>
                <th className="px-2 py-1 text-left">Symbol</th>
                <th className="px-2 py-1 text-left">Exch</th>
                <th className="px-2 py-1 text-left">Type</th>
                <th className="px-2 py-1 text-left">Name</th>
                <th className="px-2 py-1 text-right">Lot</th>
                <th className="px-2 py-1 text-left">Expiry</th>
                <th className="px-2 py-1 text-right">Strike</th>
                <th className="px-2 py-1 text-left">Token</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5">
              {rows.map((r) => (
                <tr key={`${r.exchange}-${r.symboltoken}`} className="text-slate-200">
                  <td className="px-2 py-1 font-mono">{r.tradingsymbol}</td>
                  <td className="px-2 py-1">{r.exchange}</td>
                  <td className="px-2 py-1 text-slate-400">{r.instrument_type || "—"}</td>
                  <td className="px-2 py-1 text-slate-400">{r.name || "—"}</td>
                  <td className="px-2 py-1 text-right">{r.lot_size}</td>
                  <td className="px-2 py-1 text-slate-400">{r.expiry || "—"}</td>
                  <td className="px-2 py-1 text-right text-slate-400">
                    {r.strike ? formatINR(r.strike) : "—"}
                  </td>
                  <td className="px-2 py-1 font-mono text-slate-400">{r.symboltoken}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
