import type { State } from "./types";

async function asJson(r: Response): Promise<any> {
  try {
    return await r.json();
  } catch {
    return null;
  }
}

const JSON_HEADERS = { "content-type": "application/json" };

export function getJSON(path: string): Promise<any> {
  return fetch(path).then(asJson);
}

export function post(path: string, body?: unknown): Promise<any> {
  return fetch(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: body ? JSON.stringify(body) : undefined,
  }).then(asJson);
}

export function put(path: string, body: unknown): Promise<any> {
  return fetch(path, {
    method: "PUT",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  }).then(asJson);
}

export function del(path: string): Promise<any> {
  return fetch(path, { method: "DELETE" }).then(asJson);
}

function wsBase(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}`;
}

export function ptyUrl(sessionId: string, cols: number, rows: number): string {
  return `${wsBase()}/ws/pty/${encodeURIComponent(sessionId)}?cols=${cols}&rows=${rows}`;
}

/** Subscribe to the board-state stream. Reconnects on drop. Returns a disposer. */
export function connectBoard(onState: (s: State) => void): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const open = () => {
    ws = new WebSocket(`${wsBase()}/ws/board`);
    ws.onmessage = (ev) => {
      try {
        onState(JSON.parse(ev.data));
      } catch {
        /* ignore malformed frame */
      }
    };
    ws.onclose = () => {
      if (!closed) timer = setTimeout(open, 1000);
    };
    ws.onerror = () => {
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
    };
  };
  open();

  return () => {
    closed = true;
    if (timer) clearTimeout(timer);
    try {
      ws?.close();
    } catch {
      /* ignore */
    }
  };
}
