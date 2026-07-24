import { useEffect, useMemo, useState } from "react";
import { connectBoard, del, getJSON, post, put } from "./api";
import { byAttention } from "./helpers";
import type { State, TaskDTO } from "./types";
import { Board } from "./components/Board";
import { ConfirmModal } from "./components/ConfirmModal";
import { DiffModal } from "./components/DiffModal";
import { Focus } from "./components/Focus";
import { Grid } from "./components/Grid";
import { Sidebar } from "./components/Sidebar";
import { Tabs } from "./components/Tabs";
import { TaskDialog } from "./components/TaskDialog";
import { TopBar } from "./components/TopBar";

const BOARD = "board";

function isTypingTarget(e: KeyboardEvent): boolean {
  const el = e.target as HTMLElement | null;
  if (!el) return false;
  return (
    el.tagName === "INPUT" ||
    el.tagName === "TEXTAREA" ||
    el.isContentEditable ||
    !!el.closest?.(".terminal-host")
  );
}

export default function App() {
  const [state, setState] = useState<State | null>(null);
  const [view, setView] = useState<"grid" | "focus">("grid");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [dialog, setDialog] = useState<{ task?: TaskDTO } | null>(null);
  const [diff, setDiff] = useState<{ title: string; text: string } | null>(null);
  const [confirm, setConfirm] = useState<{
    prompt: string;
    detail?: string;
    confirmLabel?: string;
    danger?: boolean;
    onConfirm: () => void;
  } | null>(null);

  useEffect(() => connectBoard(setState), []);

  const live = useMemo(
    () => (state?.tasks ?? []).filter((t) => t.session_id).sort(byAttention),
    [state],
  );
  const activeFocusTask =
    activeId && activeId !== BOARD ? live.find((t) => t.id === activeId) : undefined;

  const drillTo = (id: string) => {
    setActiveId(id);
    setView("focus");
  };
  const toGrid = () => {
    setActiveId((cur) => (cur === BOARD ? null : cur));
    setView("grid");
  };
  const toFocus = () => {
    if (activeFocusTask) {
      setView("focus");
    } else if (live.length) {
      setActiveId(live[0].id);
      setView("focus");
    }
  };
  const onView = (v: "grid" | "focus") => (v === "grid" ? toGrid() : toFocus());

  const runAction = (id: string, action: string) => {
    const doIt = async () => {
      const r = await post(`/api/task/${id}/${action}`);
      if (r && r.error) window.alert(r.error);
    };
    if (action === "stop") {
      setConfirm({
        prompt: "Stop this agent?",
        detail: "The running tmux session will be killed.",
        confirmLabel: "Stop",
        danger: true,
        onConfirm: () => void doIt(),
      });
      return;
    }
    void doIt();
  };

  const deleteTask = (task: TaskDTO) => {
    setConfirm({
      prompt: `Delete “${task.title}”?`,
      detail: "This removes the task and stops its agent.",
      confirmLabel: "Delete",
      danger: true,
      onConfirm: () =>
        void (async () => {
          await del(`/api/task/${task.id}`);
          if (activeId === task.id) toGrid();
        })(),
    });
  };

  const saveTask = async (fields: Record<string, string>) => {
    if (dialog?.task) await put(`/api/task/${dialog.task.id}`, fields);
    else await post("/api/task", fields);
    setDialog(null);
  };

  const openDiff = async (task: TaskDTO) => {
    const r = await getJSON(`/api/task/${task.id}/diff`);
    setDiff({ title: task.title, text: (r && r.diff) || "" });
  };

  // Global shortcuts (kept off the terminal / inputs where relevant).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "g") {
        e.preventDefault();
        view === "grid" ? toFocus() : toGrid();
        return;
      }
      if (e.key === "Escape") {
        if (confirm) return setConfirm(null);
        if (dialog) return setDialog(null);
        if (diff) return setDiff(null);
        if (!isTypingTarget(e) && view === "focus" && activeId !== BOARD) toGrid();
        return;
      }
      if (e.key === "o" && !dialog && !diff && !confirm && !isTypingTarget(e)) {
        e.preventDefault();
        setDialog({});
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, activeId, dialog, diff, confirm, live]);

  let mode: "board" | "grid" | "focus";
  if (activeId === BOARD) mode = "board";
  else if (view === "focus" && activeFocusTask) mode = "focus";
  else mode = "grid";

  return (
    <div className="app">
      <TopBar state={state} />
      <div className="body">
        <Sidebar live={live} activeId={activeId} onPick={drillTo} onNew={() => setDialog({})} />
        <div className="main">
          <Tabs
            live={live}
            activeId={activeId}
            view={view}
            onBoard={() => setActiveId(BOARD)}
            onPick={drillTo}
            onView={onView}
          />
          {mode === "board" && state && (
            <Board
              state={state}
              onOpen={drillTo}
              onEdit={(t) => setDialog({ task: t })}
              onDelete={deleteTask}
              onAction={runAction}
            />
          )}
          {mode === "grid" && <Grid live={live} onPick={drillTo} />}
          {mode === "focus" && activeFocusTask && (
            <Focus task={activeFocusTask} onAction={runAction} onDiff={openDiff} />
          )}
        </div>
      </div>

      {dialog && <TaskDialog task={dialog.task} onSave={saveTask} onClose={() => setDialog(null)} />}
      {diff && <DiffModal title={diff.title} text={diff.text} onClose={() => setDiff(null)} />}
      {confirm && (
        <ConfirmModal
          prompt={confirm.prompt}
          detail={confirm.detail}
          confirmLabel={confirm.confirmLabel}
          danger={confirm.danger}
          onConfirm={confirm.onConfirm}
          onClose={() => setConfirm(null)}
        />
      )}
    </div>
  );
}
