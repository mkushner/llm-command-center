import { type KeyboardEvent, useState } from "react";
import type { TaskDTO } from "../types";

interface Props {
  task?: TaskDTO; // present = edit
  onSave: (fields: Record<string, string>) => void;
  onClose: () => void;
}

export function TaskDialog({ task, onSave, onClose }: Props) {
  const [title, setTitle] = useState(task?.title ?? "");
  const [description, setDescription] = useState(task?.description ?? "");
  const [branch, setBranch] = useState(task?.branch ?? "");
  const [verify, setVerify] = useState("");
  const [done, setDone] = useState("");
  const [err, setErr] = useState("");

  const save = () => {
    if (!title.trim()) {
      setErr("Title is required");
      return;
    }
    onSave({
      title: title.trim(),
      description,
      checkout_branch: branch,
      verify,
      done,
    });
  };

  const onKey = (e: KeyboardEvent) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") save();
  };

  return (
    <div className="scrim" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(e) => e.stopPropagation()} onKeyDown={onKey}>
        <div className="modal-hd">
          {task ? "Edit task" : "New task"}
          <span className="hint">⌘↵ save · Esc cancel</span>
        </div>
        <div className="modal-bd">
          <div className="field">
            <label>Title</label>
            <input
              autoFocus
              value={title}
              placeholder="Short imperative summary (e.g. 'add OAuth login')"
              onChange={(e) => setTitle(e.target.value)}
            />
            {err && <span className="form-err">{err}</span>}
          </div>
          <div className="field">
            <label>Branch (optional — review/work an existing branch)</label>
            <input
              value={branch}
              placeholder="e.g. feature/oauth"
              onChange={(e) => setBranch(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Spec</label>
            <textarea value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          {!task && (
            <>
              <div className="field">
                <label>How to verify (optional)</label>
                <textarea value={verify} onChange={(e) => setVerify(e.target.value)} />
              </div>
              <div className="field">
                <label>Done when (optional)</label>
                <textarea value={done} onChange={(e) => setDone(e.target.value)} />
              </div>
            </>
          )}
        </div>
        <div className="modal-ft">
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save}>
            Save
          </button>
        </div>
      </div>
    </div>
  );
}
