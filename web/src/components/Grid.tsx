import { statusMeta } from "../helpers";
import type { TaskDTO } from "../types";

interface Props {
  live: TaskDTO[];
  onPick: (id: string) => void;
}

export function Grid({ live, onPick }: Props) {
  if (live.length === 0) {
    return (
      <div className="stage">
        <div className="empty" style={{ margin: "auto" }}>
          No live agents. Advance a task from the board to start one.
        </div>
      </div>
    );
  }
  return (
    <div className="stage">
      <div className="grid">
        {live.map((t) => {
          const m = statusMeta(t.status_kind);
          return (
            <div
              className={`cell ${t.attention ? "attn" : ""}`}
              key={t.id}
              onClick={() => onPick(t.id)}
              title="click to focus this agent"
            >
              <div className="cell-head">
                <span className="dot" style={{ background: m.color }} />
                <span className="ct">{t.title}</span>
                <span className="cm">
                  {t.status_kind && (
                    <span className="cst" style={{ color: m.color }}>
                      {m.label} ·{" "}
                    </span>
                  )}
                  {t.context_remaining != null ? `${t.context_remaining}% · ` : ""}
                  {t.agent} ↳
                </span>
              </div>
              <pre className="preview">{t.preview || "…"}</pre>
            </div>
          );
        })}
      </div>
    </div>
  );
}
