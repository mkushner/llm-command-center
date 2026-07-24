import type { TaskDTO } from "./types";

export interface StatusMeta {
  color: string;
  label: string;
  attn: boolean;
}

export function statusMeta(kind: string | null): StatusMeta {
  switch (kind) {
    case "error":
      return { color: "var(--err)", label: "error", attn: true };
    case "waiting":
      return { color: "var(--warn)", label: "waiting for input", attn: true };
    case "ready":
      return { color: "var(--orange)", label: "stage complete", attn: true };
    case "running":
      return { color: "var(--ok)", label: "running", attn: false };
    case "stale":
      return { color: "var(--faint)", label: "needs restart", attn: false };
    default:
      return { color: "var(--faint)", label: "idle", attn: false };
  }
}

const attnRank: Record<string, number> = {
  error: 0,
  waiting: 1,
  ready: 2,
  running: 3,
  stale: 4,
};

export function byAttention(a: TaskDTO, b: TaskDTO): number {
  return (attnRank[a.status_kind ?? ""] ?? 5) - (attnRank[b.status_kind ?? ""] ?? 5);
}

/** Map a health/context color name from the backend to a CSS var. */
export function colorVar(name: string | null): string {
  switch (name) {
    case "green":
      return "var(--ok)";
    case "yellow":
      return "var(--warn)";
    case "dark_orange":
      return "var(--orange)";
    case "red":
      return "var(--err)";
    default:
      return "var(--faint)";
  }
}

export function fmtTokens(t: number | null): string {
  return t ? `${(t / 1000).toFixed(1)}k` : "";
}

export function shortTitle(title: string, words = 3): string {
  return title.split(/\s+/).slice(0, words).join(" ");
}
