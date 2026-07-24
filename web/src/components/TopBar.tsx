import type { State } from "../types";

export function TopBar({ state }: { state: State | null }) {
  const agg = state?.aggregate;
  const pills: { c: string; t: string }[] = [];
  if (agg) {
    if (agg.running) pills.push({ c: "var(--ok)", t: `${agg.running} running` });
    if (agg.waiting) pills.push({ c: "var(--warn)", t: `${agg.waiting} waiting` });
    if (agg.ready) pills.push({ c: "var(--orange)", t: `${agg.ready} ready` });
    if (agg.error) pills.push({ c: "var(--err)", t: `${agg.error} error` });
  }
  return (
    <div className="phead">
      <div className="brand">
        <span className="dia">◆</span> LCC <span className="proj">· {state?.project || "…"}</span>
      </div>
      <div className="pills">
        {pills.map((p, i) => (
          <span className="pill" key={i}>
            <span className="dot" style={{ background: p.c }} />
            {p.t}
          </span>
        ))}
      </div>
      <div className="right">
        <span className="branch">{state?.base_branch || "—"}</span>
      </div>
    </div>
  );
}
