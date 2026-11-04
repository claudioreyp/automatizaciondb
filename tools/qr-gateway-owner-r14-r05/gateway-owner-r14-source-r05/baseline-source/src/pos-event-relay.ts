import { POS_NOTICE_TYPES as TYPES, parseNoticeUtc as parseUtc, originalApprovalWithoutRelease, noticeEventKey, noticeRecipient, projectPosNotice, type PosEvent, type OrderStatus } from "./pos-notice-policy.js";
import { closeSync, existsSync, fsyncSync, openSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "fs";
import { join } from "path";
import type { ImpulsaGateway } from "./gateway.js";
import type { GatewayConfig } from "./types.js";


type Delivery = { status: "uncertain" | "sent" | "skipped" | "acked"; createdAt: string; confirmationKey?: string };
type RelayState = { version: 2; businessId: number; branchId: number; activatedAt: string; deliveries: Record<string, Delivery> };






// This event exists only after a staff approval committed a real Yape payment.
// The original cash order already has its kitchen tickets; approval cannot
// announce or generate a second kitchen release.


export class PosEventRelay {
  private timer: ReturnType<typeof setInterval> | null = null;
  private polling = false;
  private running = false;
  private scopeValidated = false;
  private state: RelayState | null = null;
  private ownsLock = false;
  private readonly statePath: string;
  constructor(private readonly gateway: ImpulsaGateway, private readonly config: GatewayConfig) {
    const tenant = config.defaultTenantId.replace(/[^a-zA-Z0-9_-]/g, "_");
    this.statePath = join(config.sessionsDir, ".pos-event-relay-v2-" + tenant + "-" + config.posBusinessId + "-" + config.posBranchId + ".json");
  }
  get enabled(): boolean {
    return Boolean(this.config.posEventApiBase && this.config.posEventToken && this.config.posBusinessId && this.config.posBranchId);
  }
  start(): void {
    if (!this.enabled || this.running) return;
    this.acquireLock();
    try { this.state = this.loadState(); }
    catch (error) { this.releaseLock(); throw error; }
    this.running = true;
    void this.poll();
    this.timer = setInterval(() => void this.poll(), this.config.posEventPollMs);
    this.timer.unref?.();
  }
  stop(): void {
    this.running = false;
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (!this.polling) this.releaseLock();
  }
  private acquireLock(): void {
    const path = this.statePath + ".lock";
    if (existsSync(path)) {
      const pid = Number(readFileSync(path, "utf8"));
      if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error("Relay lock requires manual review");
      try { process.kill(pid, 0); throw new Error("Another relay is already running"); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error; }
      unlinkSync(path);
    }
    writeFileSync(path, String(process.pid), { flag: "wx", mode: 0o600 });
    this.ownsLock = true;
  }
  private releaseLock(): void {
    if (this.ownsLock) { unlinkSync(this.statePath + ".lock"); this.ownsLock = false; }
  }
  private loadState(): RelayState {
    if (existsSync(this.statePath)) {
      const state = JSON.parse(readFileSync(this.statePath, "utf8")) as RelayState;
      if (state.version !== 2 || state.businessId !== this.config.posBusinessId || state.branchId !== this.config.posBranchId
          || !Number.isFinite(parseUtc(state.activatedAt)) || !state.deliveries || Array.isArray(state.deliveries)
          || Object.values(state.deliveries).some(item => !item || !["uncertain", "sent", "skipped", "acked"].includes(item.status)
            || (item.confirmationKey !== undefined && (typeof item.confirmationKey !== "string" || item.confirmationKey.length > 500)))) {
        throw new Error("Relay state requires manual review; delivery stopped");
      }
      return state;
    }
    const state: RelayState = { version: 2, businessId: this.config.posBusinessId!, branchId: this.config.posBranchId!,
      activatedAt: new Date().toISOString(), deliveries: {} };
    this.persist(state);
    return state;
  }
  private persist(state = this.state): void {
    if (!state) throw new Error("Relay state unavailable");
    const temporary = this.statePath + ".tmp";
    const descriptor = openSync(temporary, "w", 0o600);
    try { writeFileSync(descriptor, JSON.stringify(state)); fsyncSync(descriptor); }
    finally { closeSync(descriptor); }
    renameSync(temporary, this.statePath);
  }
  private async request(path: string, method = "GET", key?: string): Promise<unknown> {
    const response = await fetch(this.config.posEventApiBase + path, {
      method, redirect: "error", signal: AbortSignal.timeout(15000),
      headers: { Accept: "application/json", Authorization: "Bearer " + this.config.posEventToken,
        ...(key ? { "Idempotency-Key": key } : {}) },
    });
    if (!response.ok) throw new Error("POS request failed with HTTP " + response.status);
    return response.json();
  }
  private async poll(): Promise<void> {
    if (this.polling || !this.running || !this.state) return;
    if (this.gateway.getTenant(this.config.defaultTenantId)?.status !== "connected") return;
    this.polling = true;
    try {
      if (!this.scopeValidated) {
        const context = await this.request("/integrations/context") as { business?: { id: number }; branch?: { id: number } };
        if (context.business?.id !== this.state.businessId || context.branch?.id !== this.state.branchId) throw new Error("POS scope mismatch");
        this.scopeValidated = true;
      }
      const query = new URLSearchParams({ pending_only: "true", created_after: this.state.activatedAt, limit: "200" });
      TYPES.forEach(type => query.append("event_types", type));
      const response = await this.request("/integrations/events?" + query);
      if (!Array.isArray(response)) throw new Error("Invalid events response");
      const latestByType = new Map<string, string>();
      for (const value of response) {
        if (value && typeof value.id === "string" && TYPES.includes(value.event_type)
            && typeof value.created_at === "string" && parseUtc(value.created_at) >= parseUtc(this.state.activatedAt)
            && (this.recipient(value.whatsapp_chat_id || "") || this.recipient(value.customer_phone || ""))) {
          latestByType.set(this.eventKey(value), value.id);
        }
      }
      for (const value of response) {
        if (!this.running) break;
        if (value && typeof value.id === "string" && typeof value.created_at === "string" && TYPES.includes(value.event_type)) {
          const typeKey = this.eventKey(value);
          try { await this.deliver(value, latestByType.get(typeKey) !== value.id); }
          catch { console.error("[pos-events] delivery pending review id=" + value.id); }
        }
      }
    } catch (error) { console.error("[pos-events] " + (error instanceof Error ? error.message : "poll unavailable")); }
    finally { this.polling = false; if (!this.running) this.releaseLock(); }
  }
  private eventKey(event: PosEvent): string { return noticeEventKey(event); }
  private async deliver(event: PosEvent, superseded: boolean): Promise<void> {
    if (!this.state || !this.running) return;
    const created = parseUtc(event.created_at);
    if (!Number.isFinite(created) || created < parseUtc(this.state.activatedAt)) return;
    const saved = this.state.deliveries[event.id];
    if (saved?.status === "uncertain" || saved?.status === "acked") return;
    if (saved?.status === "sent" || saved?.status === "skipped") { await this.ack(event.id); return; }
    const approvalWithoutRelease = originalApprovalWithoutRelease(event);
    if (event.event_type === "payment.approved" && event.payload?.sent_to_kitchen !== true && !approvalWithoutRelease) return;
    if (event.event_type === "order.addition_committed" && (event.payload?.sent_to_kitchen !== true
      || event.payload?.purpose !== "addition" || event.payload?.payment_method !== "cash"
      || !event.payload?.payment_request_id || !Array.isArray(event.payload?.ticket_ids) || !event.payload.ticket_ids.length)) return;
    const recipient = this.recipient(event.whatsapp_chat_id || "") || this.recipient(event.customer_phone || "");
    if (!recipient) return;
    const orderId = String(event.payload?.order_id || event.aggregate_id || "");
    if (!/^\d+$/.test(orderId) || (event.aggregate_id && event.aggregate_id !== orderId)) throw new Error("Invalid event order identity");
    const order = await this.request("/integrations/orders/" + orderId + "/status") as OrderStatus;
    if (order.id !== Number(orderId) || order.business_id !== this.state.businessId || order.branch_id !== this.state.branchId) {
      throw new Error("POS event scope mismatch");
    }
    const plan = projectPosNotice({ businessId: this.state.businessId, branchId: this.state.branchId,
      activatedAt: this.state.activatedAt, event, order, superseded });
    if (plan.kind === "pending") return;
    if (plan.kind === "skip") {
      this.state.deliveries[event.id] = { status: "skipped", createdAt: event.created_at };
      this.persist();
      await this.ack(event.id);
      return;
    }
    // A second event ID for the same committed operation is not a second
    // customer notice. Preserve ambiguous sends; recover only their ACK.
    const confirmationKey = plan.confirmationKey;
    if (confirmationKey) {
      const prior = Object.values(this.state.deliveries).find(item => item.confirmationKey === confirmationKey
        && ["uncertain", "sent", "acked"].includes(item.status));
      if (prior) {
        this.state.deliveries[event.id] = { status: prior.status === "uncertain" ? "uncertain" : "skipped", createdAt: event.created_at };
        this.persist();
        if (prior.status !== "uncertain") await this.ack(event.id);
        return;
      }
    }
    const message = plan.message;
    if (!this.running || this.gateway.getTenant(this.config.defaultTenantId)?.status !== "connected") return;
    // Persist before WhatsApp: a crash or ambiguous send must never resend automatically.
    this.state.deliveries[event.id] = { status: "uncertain", createdAt: event.created_at, ...(confirmationKey ? { confirmationKey } : {}) };
    this.persist();
    await this.gateway.sendMessage({ tenantId: this.config.defaultTenantId, number: recipient, message });
    this.state.deliveries[event.id].status = "sent";
    this.persist();
    await this.ack(event.id);
  }
  private async ack(id: string): Promise<void> {
    await this.request("/integrations/events/" + encodeURIComponent(id) + "/ack", "POST", "gateway-" + this.config.defaultTenantId + "-" + id);
    this.state!.deliveries[id].status = "acked";
    this.persist();
  }
  private recipient(value: string): string | null { return noticeRecipient(value); }
  
  
}
