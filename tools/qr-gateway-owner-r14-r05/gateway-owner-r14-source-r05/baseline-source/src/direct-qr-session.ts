// Pairing state only. No application messages, credentials or n8n client.
export class DirectQrSession {
  private status = "initializing";
  private qr: string | null = null;
  private qrExpiresAt = 0;
  private generation = 0;

  constructor(private encodePng: (raw: string) => Buffer, private now = Date.now) {}

  action(event: unknown) {
    const raw = (event as { payload?: { qr?: unknown } })?.payload?.qr;
    if (typeof raw !== "string" || !raw.length || raw.length > 8192) return false;
    const png = this.encodePng(raw);
    if (!png.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
      throw new Error("QR encoder did not produce PNG");
    }
    this.qr = `data:image/png;base64,${png.toString("base64")}`;
    this.qrExpiresAt = this.now() + 40_000;
    this.generation++;
    this.status = "awaiting_qr";
    return true;
  }

  connection(update: { connection?: string }) {
    if (update.connection === "open") this.clear("connected");
    if (update.connection === "close") this.clear("disconnected");
    if (update.connection === "connecting") this.clear("initializing");
  }

  failed() { this.clear("error"); }
  stopped() { this.clear("disconnected"); }

  snapshot(includeQr = false) {
    const current = this.now() < this.qrExpiresAt ? this.qr : null;
    return {
      status: this.status === "awaiting_qr" && !current ? "initializing" : this.status,
      qr: includeQr ? current : undefined,
      qrGeneration: this.generation,
    };
  }

  private clear(status: string) {
    this.status = status;
    this.qr = null;
    this.qrExpiresAt = 0;
  }
}

// Call initVendor directly: BuilderBot's Core/message bus is intentionally absent.
// Install the observer before init so every reconnect is observed as well.
export interface DirectPairingObserver {
  observeSocket(socket: any): void;
  observeConnection(socket: any, update: { connection?: string }): void;
}

export function bindDirectPairing(provider: any, session: DirectQrSession, observer?: DirectPairingObserver) {
  // BuilderBot calls this again from reconnectVendor after phone pairing.
  // Keep this entrypoint limited to socket authentication on every connection.
  provider.listenOnEvents = () => {};
  const attach = provider.attachSocketListeners.bind(provider);
  provider.attachSocketListeners = (socket: any, saveCreds: unknown) => {
    observer?.observeSocket(socket);
    socket.ev.on("connection.update", (update: { connection?: string }) => {
      if (provider.vendor === socket && !provider.isCleaned) {
        observer?.observeConnection(socket, update);
        session.connection(update);
      }
    });
    attach(socket, saveCreds);
  };
  provider.on("require_action", (event: unknown) => {
    try {
      if (session.action(event)) {
        // A fresh, successfully encoded QR is authentication progress. Count
        // consecutive failures, not normal expiry while waiting for the phone.
        // Genuine failures without a new QR retain BuilderBot's retry limit.
        provider.reconnectAttempts = 0;
        provider.reconnectDelay = 1000;
      }
    } catch { session.failed(); }
  });
  provider.on("auth_failure", () => session.failed());
}
