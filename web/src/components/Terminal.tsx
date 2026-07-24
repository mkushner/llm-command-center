import { useEffect, useRef } from "react";
import { FitAddon } from "@xterm/addon-fit";
import { Terminal as XTerm } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { ptyUrl } from "../api";

const THEME = {
  background: "#0a0d12",
  foreground: "#d7dde6",
  cursor: "#8b9eff",
  cursorAccent: "#0a0d12",
  selectionBackground: "#29334a",
  black: "#0a0d12",
  red: "#f87171",
  green: "#4ade80",
  yellow: "#fbbf24",
  blue: "#60a5fa",
  magenta: "#8b9eff",
  cyan: "#5bbdc0",
  white: "#d7dde6",
  brightBlack: "#5d6573",
  brightRed: "#f87171",
  brightGreen: "#4ade80",
  brightYellow: "#fbbf24",
  brightBlue: "#60a5fa",
  brightMagenta: "#8b9eff",
  brightCyan: "#5bbdc0",
  brightWhite: "#ffffff",
};

/** One interactive attach per session — grid uses read-only previews, not this. */
export function Terminal({ sessionId }: { sessionId: string }) {
  const hostRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;

    const term = new XTerm({
      fontFamily: 'ui-monospace, "SF Mono", Menlo, Consolas, monospace',
      fontSize: 12.5,
      lineHeight: 1.15,
      theme: THEME,
      cursorBlink: true,
      scrollback: 5000,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(host);

    let ws: WebSocket | undefined;
    let ro: ResizeObserver | undefined;
    let dataDisp: { dispose(): void } | undefined;
    let cancelled = false;

    const sendResize = () => {
      try {
        fit.fit();
      } catch {
        /* container not measured yet */
      }
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ t: "r", c: term.cols, r: term.rows }));
      }
    };

    // Attach only after the container has a real measured size — a fit() on a
    // 0-sized host makes the PTY attach tiny and the screen render blank until
    // a later resize. Two rAFs guarantees layout has settled.
    const raf = requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (cancelled) return;
        try {
          fit.fit();
        } catch {
          /* ignore */
        }
        ws = new WebSocket(ptyUrl(sessionId, term.cols || 120, term.rows || 32));
        ws.binaryType = "arraybuffer";
        ws.onmessage = (ev) => {
          if (typeof ev.data === "string") term.write(ev.data);
          else term.write(new Uint8Array(ev.data));
        };
        ws.onopen = () => {
          sendResize();
          term.focus();
        };
        dataDisp = term.onData((d) => {
          if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ t: "i", d }));
        });
        ro = new ResizeObserver(() => sendResize());
        ro.observe(host);
      }),
    );

    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
      ro?.disconnect();
      dataDisp?.dispose();
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
      term.dispose();
    };
  }, [sessionId]);

  return <div className="terminal-host" ref={hostRef} />;
}
