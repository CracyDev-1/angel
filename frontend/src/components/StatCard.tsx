type Accent = "default" | "emerald" | "rose" | "amber" | "sky";

type Props = {
  label: string;
  value: string;
  hint?: string;
  accent?: Accent;
  valueClass?: string;
};

const accentBars: Record<Accent, string> = {
  default: "from-slate-500/30 to-slate-500/0",
  emerald: "from-emerald-400/40 to-emerald-400/0",
  rose: "from-rose-400/40 to-rose-400/0",
  amber: "from-amber-400/40 to-amber-400/0",
  sky: "from-sky-400/40 to-sky-400/0",
};

export default function StatCard({ label, value, hint, accent = "default", valueClass }: Props) {
  return (
    <div className="card relative overflow-hidden p-4">
      <div className={`absolute inset-x-0 top-0 h-0.5 bg-gradient-to-r ${accentBars[accent]}`} />
      <div className="stat-label">{label}</div>
      <div className={`stat-num mt-1 ${valueClass || "text-slate-100"}`}>{value}</div>
      {hint ? <div className="mt-1 text-xs text-slate-400">{hint}</div> : null}
    </div>
  );
}
