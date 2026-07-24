import { shortTitle, statusMeta } from "../helpers";
import type { TaskDTO } from "../types";

interface Props {
  live: TaskDTO[];
  activeId: string | null;
  view: "grid" | "focus";
  onBoard: () => void;
  onPick: (id: string) => void;
  onView: (v: "grid" | "focus") => void;
}

export function Tabs({ live, activeId, view, onBoard, onPick, onView }: Props) {
  const onBoardTab = activeId === "board";
  return (
    <div className="tabbar">
      <div className="tabs">
        <button className={`tab ${onBoardTab ? "on" : ""}`} onClick={onBoard}>
          ▤ Board
        </button>
        {live.map((t) => {
          const m = statusMeta(t.status_kind);
          const cls = ["tab", activeId === t.id ? "on" : "", t.attention ? "attn" : ""]
            .filter(Boolean)
            .join(" ");
          return (
            <button className={cls} key={t.id} onClick={() => onPick(t.id)}>
              <span className="dot" style={{ background: m.color }} />
              {shortTitle(t.title)}
            </button>
          );
        })}
      </div>
      <div className="viewsw">
        <button className={view === "focus" && !onBoardTab ? "on" : ""} onClick={() => onView("focus")}>
          focus
        </button>
        <button className={view === "grid" && !onBoardTab ? "on" : ""} onClick={() => onView("grid")}>
          grid
        </button>
      </div>
    </div>
  );
}
