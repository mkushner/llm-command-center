import type { CSSProperties } from "react";
import { statusMeta } from "../helpers";
import type { TaskDTO } from "../types";

interface Props {
  live: TaskDTO[];
  activeId: string | null;
  onPick: (id: string) => void;
  onNew: () => void;
}

export function Sidebar({ live, activeId, onPick, onNew }: Props) {
  return (
    <div className="sidebar">
      <div className="side-head">
        <span>Sessions</span>
        <span style={{ color: "var(--faint)" }}>{live.length}</span>
      </div>
      <div className="side-list">
        {live.length === 0 && <div className="empty">no live agents</div>}
        {live.map((t) => {
          const m = statusMeta(t.status_kind);
          const cls = ["srow", activeId === t.id ? "active" : "", t.attention ? "attn" : ""]
            .filter(Boolean)
            .join(" ");
          return (
            <button
              className={cls}
              key={t.id}
              style={{ "--ring": m.color } as CSSProperties}
              onClick={() => onPick(t.id)}
            >
              <span className="dot" style={{ background: m.color }} />
              <span className="st">
                <span className="t">{t.title}</span>
                <span className="sub">
                  {t.agent}
                  {t.model ? ` · ${t.model}` : ""} · {t.status}
                </span>
              </span>
              <span className="pin">{t.context_remaining != null ? `${t.context_remaining}%` : ""}</span>
            </button>
          );
        })}
      </div>
      <div className="side-foot">
        <button className="newbtn" onClick={onNew}>
          ＋ new task
        </button>
      </div>
    </div>
  );
}
