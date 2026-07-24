import { type FormEvent, useState } from "react";
import { colorVar, fmtTokens, statusMeta } from "../helpers";
import type { TaskDTO } from "../types";
import { Terminal } from "./Terminal";

interface Props {
  task: TaskDTO;
  onAction: (id: string, action: string) => void;
  onDiff: (task: TaskDTO) => void;
  onReply: (id: string, text: string) => void;
}

export function Focus({ task, onAction, onDiff, onReply }: Props) {
  const m = statusMeta(task.status_kind);
  const hb = colorVar(task.health_color);
  const cc = colorVar(task.context_color);
  const [reply, setReply] = useState("");

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = reply.trim();
    if (!text) return;
    onReply(task.id, text);
    setReply("");
  };

  return (
    <div className="stage">
      <div className="focus-head">
        <div className="fh-title">
          <span className="dot" style={{ background: m.color }} />
          <b>{task.agent}</b>
          {task.model && <span className="c-dim">{task.model}</span>}
          {task.branch && <span className="c-accent2">{task.branch}</span>}
          <span className="c-faint">{task.title}</span>
        </div>
        <div className="fh-actions">
          <button onClick={() => onAction(task.id, "advance")} title="advance stage">
            Advance ▸
          </button>
          <button onClick={() => onAction(task.id, "revert")} title="back one stage">
            ◂ Revert
          </button>
          <button onClick={() => onAction(task.id, "restart")} title="restart agent">
            ↻ Restart
          </button>
          <button onClick={() => onDiff(task)} title="diff vs base">
            ± Diff
          </button>
          <button className="danger" onClick={() => onAction(task.id, "stop")} title="stop agent">
            ■ Stop
          </button>
        </div>
      </div>

      <Terminal sessionId={task.session_id!} key={task.session_id!} />

      <form className={`reply ${task.attention ? "hot" : ""}`} onSubmit={submit}>
        <span className="rp">›</span>
        <input
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          placeholder={
            task.attention ? "the agent is waiting — type your reply…" : "send a message to the agent…"
          }
        />
      </form>

      <div className="statusbar">
        <span className="hb" style={{ color: hb }}>
          ●
        </span>
        {task.context_remaining != null && <span style={{ color: cc }}>{task.context_remaining}% ctx</span>}
        {task.tokens != null && (
          <>
            <span className="sep">·</span>
            <span>{fmtTokens(task.tokens)} tok</span>
          </>
        )}
        <span className="sep">·</span>
        <span>
          {task.agent} {task.model}
        </span>
        <span className="hint">type to reply · Ctrl+C interrupt · ⌘G grid</span>
      </div>
    </div>
  );
}
