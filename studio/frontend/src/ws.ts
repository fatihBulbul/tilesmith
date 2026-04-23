// Thin WebSocket wrapper with auto-reconnect and typed event dispatch.

import type { WsEvent } from "./types";

type Listener = (ev: WsEvent) => void;

export class StudioWS {
  private url: string;
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private reconnectDelay = 1000;
  private pingTimer: number | null = null;
  private alive = false;

  constructor(path = "/ws") {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this.url = `${proto}//${location.host}${path}`;
  }

  start(): void {
    this.connect();
  }

  on(fn: Listener): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  send(msg: object): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  isAlive(): boolean {
    return this.alive;
  }

  private connect(): void {
    this.setStatus("connecting…");
    const ws = new WebSocket(this.url);
    this.ws = ws;

    ws.onopen = () => {
      this.alive = true;
      this.reconnectDelay = 1000;
      this.setStatus("connected", "ok");
      this.startPing();
    };

    ws.onmessage = (e) => {
      let ev: WsEvent;
      try {
        ev = JSON.parse(e.data) as WsEvent;
      } catch {
        return;
      }
      for (const l of this.listeners) l(ev);
    };

    ws.onclose = () => {
      this.alive = false;
      this.stopPing();
      this.setStatus(`disconnected, retry ${this.reconnectDelay}ms`, "err");
      setTimeout(() => this.connect(), this.reconnectDelay);
      this.reconnectDelay = Math.min(this.reconnectDelay * 2, 10_000);
    };

    ws.onerror = () => {
      this.setStatus("error", "err");
    };
  }

  private startPing(): void {
    this.stopPing();
    this.pingTimer = window.setInterval(() => {
      this.send({ type: "ping" });
    }, 15_000);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  private setStatus(text: string, kind: "ok" | "err" | "" = ""): void {
    const el = document.getElementById("ws-status");
    if (!el) return;
    el.textContent = `WS: ${text}`;
    el.className = kind;
  }
}
