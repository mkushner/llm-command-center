import { colorVar, fmtTokens, statusMeta } from "../helpers";
import type { State, TaskDTO } from "../types";

const HOT = new Set(["planning", "execute", "review"]);

interface Props {
  state: State;
  onOpen: (id: string) => void;
  onEdit: (task: TaskDTO) => void;
  onDelete: (task: TaskDTO) => void;
  onAction: (id: string, action: string) => void;
}

export function Board({ state, onOpen, onEdit, onDelete, onAction }: Props) {
  return (
    <div className="stage">
      <div className="kanban">
        {state.stages.map((st) => {
          const items = state.tasks.filter((t) => t.status === st.key);
          return (
            <div className={`col ${HOT.has(st.key) ? "hot" : ""}`} key={st.key}>
              <div className="col-head">
                <span className="cn">{st.label}</span>
                <span className="cc">{items.length}</span>
                <span className="cm">
                  {st.agent ? `${st.agent}${st.model ? ` · ${st.model}` : ""}` : ""}
                </span>
              </div>
              <div className="col-list">
                {items.length === 0 && (
                  <div className="empty">{st.key === "backlog" ? "press o to add" : "—"}</div>
                )}
                {items.map((t) => (
                  <Card
                    key={t.id}
                    task={t}
                    onOpen={onOpen}
                    onEdit={onEdit}
                    onDelete={onDelete}
                    onAction={onAction}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

type CardProps = Omit<Props, "state"> & { task: TaskDTO };

function Card({ task, onOpen, onEdit, onDelete, onAction }: CardProps) {
  const live = !!task.session_id;
  const m = statusMeta(task.status_kind);
  const cc = colorVar(task.context_color);
  const cls = ["card", task.attention ? "attn" : "", task.stale ? "stale" : ""].filter(Boolean).join(" ");
  return (
    <div className={cls} onClick={() => live && onOpen(task.id)}>
      <div className="ct">{task.title}</div>
      <div className="meta">
        {task.status_kind && (
          <span className="chip" style={{ color: m.color }}>
            <span
              className="dot"
              style={{ width: 6, height: 6, borderRadius: "50%", background: m.color }}
            />
            {m.label}
          </span>
        )}
        {!task.status_kind && task.status === "done" && <span style={{ color: "var(--ok)" }}>✓ done</span>}
        {!task.status_kind && task.status === "backlog" && <span className="c-faint">backlog</span>}
      </div>
      {live && (
        <div className="ctxrow">
          <span style={{ fontFamily: "var(--mono)", fontSize: 10, color: "var(--faint)" }}>{task.agent}</span>
          {task.context_remaining != null && (
            <div className="ctxbar">
              <i style={{ width: `${task.context_remaining}%`, background: cc }} />
            </div>
          )}
          {task.tokens != null && <span className="tok">{fmtTokens(task.tokens)}</span>}
        </div>
      )}
      <div className="card-actions" onClick={(e) => e.stopPropagation()}>
        {task.status !== "backlog" && task.status !== "done" && (
          <button onClick={() => onAction(task.id, "revert")} title="revert">
            ◂
          </button>
        )}
        {task.status !== "done" && (
          <button onClick={() => onAction(task.id, "advance")} title="advance">
            ▸
          </button>
        )}
        <button onClick={() => onEdit(task)} title="edit">
          ✎
        </button>
        <button className="danger" onClick={() => onDelete(task)} title="delete">
          ✕
        </button>
      </div>
    </div>
  );
}
