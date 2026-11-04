import { execFile as execFileCallback } from "child_process";
import { existsSync } from "fs";
import { readFile, unlink } from "fs/promises";
import { basename, extname, resolve } from "path";
import { promisify } from "util";

import { BotManager } from "@builderbot/manager";
import { SessionSafeWPPConnectProvider } from "./session-safe-wppconnect-provider.js";

import { N8nClient } from "./n8n-client.js";
import { validateBinaryMedia } from "./binary-media.js";
import { acceptedProviderReceipt, individualRecipient, providerReceipt, restaurantAction, type ProviderReceipt, type RestaurantAction } from "./restaurant-transport.js";
import { whileTyping } from "./typing.js";
import { ConversationQueue, InboundMessageIds, OrderedPreparation, obsoleteInformation, withSingleEmoji } from "./conversation-turn.js";
import { currentMenuImages } from "./pos-menu.js";
import { providerContactDisplayName } from "./contact-profile.js";
import type {
  CreateTenantInput,
  GatewayConfig,
  GatewayAction,
  N8nInboundPayload,
  ProviderStatus,
  RawBotContext,
  RestartTenantInput,
  SendMessageInput,
  TenantState,
  TenantStatus,
} from "./types.js";
import {
  classifyInboundEvent,
  extractProviderEventType,
  extractProviderSubtype,
  extractMessageId,
  extractProviderTimestampInfo,
  extractProviderTimestampMs,
  extractTimestamp,
  hasServerMessageTimestamp,
  isEmptyTextInbound,
  isGroupMessage,
  isPlaceholderInbound,
  isStickerInbound,
  isSyncNotificationEvent,
  normalizeQrImage,
  parseInboundMessage,
} from "./utils.js";

const execFile = promisify(execFileCallback);

const WINDOWS_BROWSER_CANDIDATES = [
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
];

const STARTUP_VERIFICATION_DELAY_MS = 10_000;
const STARTUP_NO_MESSAGE_RECOVERY_MS = 120_000;
const WATCHDOG_INTERVAL_MS = 30_000;
const PROVIDER_HEALTH_TIMEOUT_MS = 15_000;
const MESSAGE_CUTOFF_SKEW_MS = 10_000;
const SYNC_BARRIER_SETTLE_MS = 4_000;
const QR_RECOVERY_WAIT_MS = 12_000;
const QR_RECOVERY_MAX_ATTEMPTS = 4;
const VENDOR_MESSAGE_POLL_MS = 1_500;
const VENDOR_MESSAGE_POLL_OVERLAP_MS = 5_000;
// Give WPPConnect more than one polling overlap to attach delayed media bytes.
// Text turns still use the configured 3.5-second silence window.
const INCOMPLETE_MEDIA_NOTICE_GRACE_MS = 10_000;
const VENDOR_MESSAGE_POLL_FAILURE_THRESHOLD = 3;
const VENDOR_RUNTIME_RECOVERY_COOLDOWN_MS = 30_000;
const VENDOR_DISCONNECT_GRACE_MS = 12_000;
const MAX_INBOUND_MEDIA_BYTES = 8 * 1024 * 1024;

type ProviderLike = {
  on: (event: string, cb: (payload: any) => void) => void;
  sendMessage: (number: string, message: string, options: Record<string, unknown>) => Promise<unknown>;
  saveFile?: (ctx: unknown, options?: { path?: string }) => Promise<string>;
  vendor?: {
    getWid?: () => Promise<unknown>;
    getPnLidEntry?: (phoneOrLid: string) => Promise<{
      phoneNumber?: unknown;
      lid?: unknown;
      contact?: Record<string, unknown>;
    }>;
    onMessage?: (cb: (payload: unknown) => void) => unknown;
    onAnyMessage?: (cb: (payload: unknown) => void) => unknown;
    page?: {
      evaluate: <T>(pageFunction: (...args: any[]) => T | Promise<T>, ...args: any[]) => Promise<T>;
    };
    sendText?: unknown;
    startTyping?: (to: string, duration?: number) => Promise<unknown>;
    stopTyping?: (to: string) => Promise<unknown>;
    sendImageFromBase64?: (to: string, data: string, filename: string, caption?: string) => Promise<any>;
    listenerEmitter?: {
      listenerCount?: (event: string) => number;
    };
    onStateChange?: (cb: (payload: unknown) => void) => unknown;
    onStreamChange?: (cb: (payload: unknown) => void) => unknown;
    getConnectionState?: () => Promise<unknown>;
    isConnected?: () => Promise<boolean>;
    logout?: () => Promise<unknown>;
    close?: () => Promise<unknown>;
  };
  initVendor?: () => Promise<unknown>;
  listenOnEvents?: (vendor: unknown) => void;
  stop?: () => Promise<void>;
  server?: { server?: { listening?: boolean } };
};

const TERMINAL_VENDOR_STATES = new Set(["UNPAIRED", "UNPAIRED_IDLE", "TIMEOUT"]);
const TERMINAL_VENDOR_STREAMS = new Set(["DISCONNECTED"]);


export class ImpulsaGateway {
  private readonly manager: BotManager;
  private readonly n8n: N8nClient;
  private readonly states = new Map<string, TenantState>();
  private readonly processedMessageIds = new InboundMessageIds();
  private readonly tenantConfigs = new Map<
    string,
    { name: string; providerOptions: Record<string, unknown>; port: number }
  >();
  private readonly startupVerificationTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly recoveryLocks = new Set<string>();
  private readonly recoveryStats = new Map<string, { count: number; lastAt: string | null }>();
  private readonly attachedProviders = new WeakSet<object>();
  private readonly attachedVendors = new WeakSet<object>();
  private readonly disconnectRestartLocks = new Set<string>();
  private readonly qrRecoveryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly qrRecoveryAttempts = new Map<string, number>();
  private readonly syncBarrierTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly vendorMessagePollTimers = new Map<string, ReturnType<typeof setInterval>>();
  private readonly vendorMessagePollProviders = new Map<string, ProviderLike>();
  private readonly vendorMessagePollCursors = new Map<string, number>();
  private readonly vendorMessagePollBusy = new Set<string>();
  private readonly vendorMessagePollFailures = new Map<string, number>();
  private readonly vendorRuntimeRecoveryLocks = new Set<string>();
  private readonly vendorRuntimeRecoveryLastAt = new Map<string, number>();
  private readonly vendorDisconnectTimers = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly turnQueue: ConversationQueue;
  private readonly inboundReception = new OrderedPreparation();
  private readonly inboundPreparation = new OrderedPreparation();
  private readonly incompleteMediaNotices = new Map<string, ReturnType<typeof setTimeout>>();
  private readonly incompleteMediaNoticeIds = new InboundMessageIds();
  private readonly contactPhoneByWid = new Map<string, string>();
  private readonly contactPhoneResolutions = new Map<string, Promise<string>>();
  private readonly watchdogTimer: ReturnType<typeof setInterval>;
  private shuttingDown = false;

  constructor(private readonly config: GatewayConfig, pairingProviderClass?: typeof SessionSafeWPPConnectProvider) {
    if (pairingProviderClass && config.pairingOnly !== true) throw new Error("Pairing connector requires paused preview mode");
    this.manager = new BotManager({
      sessionsDir: config.sessionsDir,
      defaultProviderClass: pairingProviderClass || SessionSafeWPPConnectProvider,
      autoReconnect: {
        enabled: false,
      },
    });
    this.n8n = new N8nClient(config);
    this.turnQueue = new ConversationQueue(config.messageBatchWindowMs,
      (payload, superseded) => this.processInboundPayload(payload.tenantId, payload, superseded),
      (_error, payload) => console.error(`[gateway] inbound turn delivery uncertain tenant=${payload.tenantId} turn=${payload.context.turnId || 'unknown'}; not retried`));
    this.watchdogTimer = setInterval(() => {
      void this.runWatchdog();
    }, WATCHDOG_INTERVAL_MS);
  }

  async bootstrap(): Promise<void> {
    await this.createTenant({
      tenantId: this.config.defaultTenantId,
      name: "Impulsa",
    });
  }

  async createTenant(input: CreateTenantInput): Promise<TenantState> {
    const tenantId = input.tenantId.trim();
    if (!tenantId) {
      throw new Error("tenantId is required");
    }

    if (this.manager.hasBot(tenantId)) {
      throw new Error(`Bot "${tenantId}" already exists`);
    }

    const executablePath = this.resolveBrowserExecutablePath(input.providerOptions);
    const providerOptions = {
      headless: true,
      useChrome: !executablePath,
      disableWelcome: true,
      updatesLog: false,
      autoClose: 0,
      puppeteerOptions: {
        headless: true,
        args: ["--no-sandbox", "--disable-setuid-sandbox"],
        ...(executablePath ? { executablePath } : {}),
      },
      ...(input.providerOptions || {}),
    };
    // Pairing preview exposes only its read-only localhost page, never the
    // upstream provider HTTP server (whose routes bypass this gateway).
    const port = this.config.pairingOnly === true ? 0 : this.allocateTenantPort();

    this.tenantConfigs.set(tenantId, {
      name: input.name?.trim() || tenantId,
      providerOptions,
      port,
    });

    this.states.set(tenantId, this.deriveState({
      tenantId,
      name: input.name?.trim() || tenantId,
      status: "initializing",
      effectiveStatus: "initializing",
      providerStatus: "initializing",
      hasLiveSessionEvidence: false,
      isOperational: false,
      providerType: "WPPConnectProvider",
      port,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      qr: null,
      lastError: null,
      lastInboundAt: null,
      lastInboundType: null,
      lastInboundTextPreview: null,
      lastN8nAt: null,
      lastN8nStatus: null,
      lastOutboundAt: null,
      lastOutboundPreview: null,
      lastTransportError: null,
      sessionReady: false,
      messageBusReady: false,
      messageBusListenerCount: 0,
      fallbackMessageBusReady: false,
      fallbackMessageBusListenerCount: 0,
      pollingMessageBusReady: false,
      pollingMessageBusLastRunAt: null,
      pollingMessageBusCursorAt: null,
      pollingMessageBusError: null,
      pollingMessageBusConsecutiveErrors: 0,
      pollingMessageBusRecoveryAt: null,
      pollingMessageBusRecoveryReason: null,
      listenerBoundAt: null,
      startupVerifiedAt: null,
      startupVerificationStatus: "idle",
      startupVerificationError: null,
      lastMessageEventAt: null,
      lastProviderEventAt: null,
      lastProviderEventSource: null,
      lastAcceptedInboundAt: null,
      pendingInboundBatchCount: 0,
      lastInboundBatchSize: null,
      lastInboundBatchAt: null,
      acceptMessagesAfter: null,
      syncBarrierStartedAt: null,
      syncLastNoiseAt: null,
      syncSettledAt: null,
      syncBarrierActive: false,
      lastIgnoredInboundAt: null,
      lastIgnoredReason: null,
      ignoredInboundCount: 0,
      recoveryAttemptCount: 0,
      lastRecoveryAt: null,
      lastDisconnectAt: null,
      lastDisconnectReason: null,
      lastVendorState: null,
      lastVendorStream: null,
      autoRestartReason: null,
      disconnectPendingQr: false,
      disconnectGeneration: 0,
      connectionInvalidatedAt: null,
      disconnectInProgress: false,
      restartInProgress: false,
      qrUpdatedAt: null,
      qrGeneration: 0,
      qrInvalidatedAt: null,
      qrRecoveryInProgress: false,
      lastAuthFailureAt: null,
      lastAuthFailureReason: null,
    }));

    const bot = await this.manager.createBot({
      tenantId,
      name: input.name?.trim() || tenantId,
      flows: [],
      port,
      providerOptions,
    });

    this.attachProviderListeners(tenantId, bot.provider as ProviderLike);
    if (this.config.pairingOnly === true) {
      // BuilderBot normally initializes the vendor from its HTTP server.
      // Start only WhatsApp pairing here, with no provider HTTP listener.
      const pairingProvider = bot.provider as unknown as { initVendor(): Promise<unknown> };
      void pairingProvider.initVendor().catch(() => {
        this.patchState(tenantId, { providerStatus: "error", lastError: "QR pairing could not start" });
      });
    }
    return this.requireState(tenantId);
  }

  async restartTenant(tenantId: string, input: RestartTenantInput = {}): Promise<TenantState> {
    const currentConfig = this.tenantConfigs.get(tenantId);
    const previousState = this.states.get(tenantId);
    if (!currentConfig) {
      throw new Error(`Bot "${tenantId}" not found`);
    }

    this.clearStartupVerification(tenantId);
    this.clearSyncBarrierSettlement(tenantId);
    this.clearVendorMessagePolling(tenantId);
    this.clearVendorDisconnectVerification(tenantId);
    this.clearInboundBatches(tenantId);
    const oldProvider = this.manager.getBot(tenantId)?.provider as ProviderLike | undefined;
    await this.stopProviderHttpServer(oldProvider);
    try { await oldProvider?.vendor?.close?.(); }
    catch (error) { console.warn(`[gateway] old vendor close failed ${tenantId}: ${error instanceof Error ? error.message : String(error)}`); }
    await this.manager.removeBot(tenantId);
    this.states.delete(tenantId);

    const restarted = await this.createTenant({
      tenantId,
      name: input.name?.trim() || currentConfig.name,
      providerOptions: {
        ...currentConfig.providerOptions,
        ...(input.providerOptions || {}),
      },
    });

    const carryover = this.buildRestartStateCarryover(previousState);
    if (Object.keys(carryover).length > 0) {
      this.patchState(tenantId, carryover);
    }

    return this.requireState(tenantId);
  }

  private async stopProviderHttpServer(provider?: ProviderLike): Promise<void> {
    if (!provider?.server?.server?.listening) return;
    if (typeof provider.stop !== "function") throw new Error("Provider HTTP server cannot stop safely");
    // BuilderBot's removeBot() does not close the provider's HTTP listener.
    // Wait for port release before creating another provider on the same port.
    await provider.stop();
  }

  async removeTenant(tenantId: string): Promise<boolean> {
    const removed = await this.manager.removeBot(tenantId);
    if (removed) {
      this.clearStartupVerification(tenantId);
      this.clearQrRecovery(tenantId);
      this.clearSyncBarrierSettlement(tenantId);
      this.clearVendorMessagePolling(tenantId);
      this.clearVendorDisconnectVerification(tenantId);
      this.clearInboundBatches(tenantId);
      this.states.delete(tenantId);
      this.tenantConfigs.delete(tenantId);
      this.recoveryStats.delete(tenantId);
      this.disconnectRestartLocks.delete(tenantId);
    }
    return removed;
  }

  async shutdown(): Promise<void> {
    this.shuttingDown = true;
    clearInterval(this.watchdogTimer);
    for (const tenantId of this.startupVerificationTimers.keys()) {
      this.clearStartupVerification(tenantId);
    }
    for (const tenantId of this.qrRecoveryTimers.keys()) {
      this.clearQrRecovery(tenantId);
    }
    for (const tenantId of this.syncBarrierTimers.keys()) {
      this.clearSyncBarrierSettlement(tenantId);
    }
    for (const tenantId of this.vendorMessagePollTimers.keys()) {
      this.clearVendorMessagePolling(tenantId);
    }
    for (const tenantId of this.vendorDisconnectTimers.keys()) {
      this.clearVendorDisconnectVerification(tenantId);
    }
    for (const tenantId of this.states.keys()) {
      this.clearInboundBatches(tenantId);
    }
    await this.manager.shutdown();
  }

  listTenants(): TenantState[] {
    return Array.from(this.states.values()).sort((a, b) => a.tenantId.localeCompare(b.tenantId));
  }

  getTenant(tenantId: string): TenantState | undefined {
    return this.states.get(tenantId);
  }

  getHealth() {
    const tenants = this.listTenants();
    const counts = tenants.reduce(
      (acc, tenant) => {
        acc.total += 1;
        acc[tenant.status] += 1;
        return acc;
      },
      {
        total: 0,
        initializing: 0,
        awaiting_qr: 0,
        connected: 0,
        degraded: 0,
        disconnected: 0,
        error: 0,
      } as Record<TenantStatus | "total", number>
    );

    return {
      status: counts.error > 0 || counts.degraded > 0 ? "degraded" : "healthy",
      uptime: process.uptime(),
      memory: process.memoryUsage(),
      tenants: counts,
      n8nWebhookUrl: this.config.n8nWebhookUrl,
    };
  }

  async sendMessage(input: SendMessageInput): Promise<void> {
    const bot = this.manager.getBot(input.tenantId);
    if (!bot) {
      throw new Error(`Bot "${input.tenantId}" not found`);
    }

    await this.sendProviderMessage(bot.provider as ProviderLike, input.number, input.media
      ? { type: "media", url: input.media, caption: input.message }
      : { type: "text", text: input.message });
  }

  /** Opt-in durable sink uses confirmed provider IDs; legacy sends retain their contract. */
  async sendRestaurantAction(tenant: string, sender: string, action: RestaurantAction): Promise<ProviderReceipt> {
    const recipient = individualRecipient(sender);
    const validated = restaurantAction(action);
    const bot = this.manager.getBot(tenant);
    if (!bot) throw new Error('Restaurant transport unavailable');
    const receipt = await this.sendProviderMessage(bot.provider as ProviderLike, recipient, validated);
    if (!receipt) throw new Error('Provider receipt missing');
    return acceptedProviderReceipt(receipt);
  }

  async disconnectTenant(tenantId: string): Promise<TenantState> {
    return this.startQrRegeneration(tenantId, {
      reason: "manual disconnect from qr page",
      logoutVendor: true,
      closeVendor: true,
      source: "manual_disconnect",
    });
  }

  private attachProviderListeners(tenantId: string, provider: ProviderLike) {
    if (this.attachedProviders.has(provider as object)) {
      return;
    }

    this.attachedProviders.add(provider as object);
    this.patchState(tenantId, {
      listenerBoundAt: new Date().toISOString(),
      messageBusReady: false,
      messageBusListenerCount: 0,
      fallbackMessageBusReady: false,
      fallbackMessageBusListenerCount: 0,
      pollingMessageBusReady: false,
      pollingMessageBusLastRunAt: null,
      pollingMessageBusCursorAt: null,
      pollingMessageBusError: null,
      pollingMessageBusConsecutiveErrors: 0,
      providerStatus: "initializing",
    });

    provider.on("message", (payload: RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown }) => {
      this.receiveProviderMessage(tenantId, provider, payload, "builderbot_message");
    });

    provider.on("require_action", (payload: { payload?: { qr?: string } }) => {
      if (!this.isCurrentProvider(tenantId, provider)) {
        return;
      }
      const state = this.getTenant(tenantId);
      if (state && this.shouldIgnoreRequireAction(state)) {
        const messageBusReady = provider.vendor ? this.hasMessageBus(provider.vendor) : false;
        this.patchState(tenantId, {
          providerStatus: "host",
          qr: null,
          sessionReady: true,
          messageBusReady,
          messageBusListenerCount: this.getMessageBusListenerCount(provider.vendor),
          startupVerificationStatus: state.startupVerificationStatus === "idle" ? "success" : state.startupVerificationStatus,
          startupVerificationError: null,
        });
        this.scheduleProviderMessageBusCheck(tenantId, provider);
        return;
      }

      const qrText = payload?.payload?.qr;
      const now = new Date().toISOString();
      this.patchState(tenantId, {
        providerStatus: "require_action",
        qr: qrText ? normalizeQrImage(qrText) : null,
        qrUpdatedAt: qrText ? now : null,
        qrGeneration: qrText ? this.nextQrGeneration(tenantId, qrText) : this.getTenant(tenantId)?.qrGeneration ?? 0,
        qrInvalidatedAt: null,
        qrRecoveryInProgress: false,
        lastError: null,
        lastTransportError: null,
        sessionReady: false,
        messageBusReady: false,
        messageBusListenerCount: 0,
        startupVerificationStatus: "idle",
        startupVerificationError: null,
        disconnectPendingQr: false,
        disconnectInProgress: false,
        restartInProgress: false,
        syncBarrierActive: false,
        syncBarrierStartedAt: null,
        syncLastNoiseAt: null,
        syncSettledAt: null,
      });
      this.clearQrRecovery(tenantId);
    });

    provider.on("ready", () => {
      if (!this.isCurrentProvider(tenantId, provider)) {
        return;
      }
      const state = this.getTenant(tenantId);
      const now = new Date().toISOString();
      this.patchState(tenantId, {
        providerStatus: "ready",
        qr: null,
        qrRecoveryInProgress: false,
        lastError: null,
        lastTransportError: null,
        messageBusReady: false,
        messageBusListenerCount: this.getMessageBusListenerCount(provider.vendor),
        startupVerificationStatus: "pending",
        startupVerificationError: null,
        startupVerifiedAt: null,
        acceptMessagesAfter: state?.acceptMessagesAfter ?? null,
        disconnectInProgress: false,
      });
      this.attachVendorListeners(tenantId, provider);
      this.scheduleProviderMessageBusCheck(tenantId, provider);
    });

    provider.on("host", (host: Record<string, unknown>) => {
      if (!this.isCurrentProvider(tenantId, provider)) {
        return;
      }
      const now = new Date().toISOString();
      this.patchState(tenantId, {
        providerStatus: "host",
        qr: null,
        qrRecoveryInProgress: false,
        sessionReady: true,
        messageBusReady: false,
        messageBusListenerCount: this.getMessageBusListenerCount(provider.vendor),
        startupVerificationStatus: "success",
        startupVerificationError: null,
        startupVerifiedAt: now,
        acceptMessagesAfter: this.bumpAcceptMessagesAfter(tenantId, now),
        lastError: null,
        host,
        disconnectPendingQr: false,
        connectionInvalidatedAt: null,
        disconnectInProgress: false,
        restartInProgress: false,
        qrInvalidatedAt: null,
        syncBarrierActive: true,
        syncBarrierStartedAt: now,
        syncLastNoiseAt: now,
        syncSettledAt: null,
        lastAcceptedInboundAt: null,
      });
      this.clearQrRecovery(tenantId);
      this.attachVendorListeners(tenantId, provider);
      this.scheduleProviderMessageBusCheck(tenantId, provider);
      this.scheduleSyncBarrierSettlement(tenantId);
    });

    provider.on("auth_failure", (payload: unknown) => {
      if (!this.isCurrentProvider(tenantId, provider)) {
        return;
      }
      const message = this.stringifyPayload(payload);
      const state = this.getTenant(tenantId);
      const now = new Date().toISOString();
      const inQrRecovery = Boolean(
        state?.disconnectPendingQr ||
          state?.disconnectInProgress ||
          state?.restartInProgress ||
          (state?.autoRestartReason && state?.lastDisconnectAt && !this.hasLiveSessionEvidence(state))
      );
      const inQrLinkFlow = Boolean(
        !this.hasLiveSessionEvidence(state ?? ({} as TenantState)) ||
          state?.effectiveStatus === "awaiting_qr" ||
          this.hasCurrentQr(state)
      );
      this.patchState(tenantId, {
        providerStatus: "auth_failure",
        lastError: message,
        sessionReady: inQrRecovery || inQrLinkFlow ? false : state?.sessionReady ?? false,
        messageBusReady: inQrRecovery || inQrLinkFlow ? false : state?.messageBusReady ?? false,
        startupVerificationStatus: inQrRecovery || inQrLinkFlow ? "recovering" : "failed",
        startupVerificationError: message,
        disconnectPendingQr: inQrRecovery || inQrLinkFlow ? true : state?.disconnectPendingQr ?? false,
        disconnectInProgress: inQrRecovery || inQrLinkFlow ? false : state?.disconnectInProgress ?? false,
        restartInProgress: inQrRecovery || inQrLinkFlow ? true : state?.restartInProgress ?? false,
        qr: inQrLinkFlow ? null : state?.qr ?? null,
        qrInvalidatedAt: inQrLinkFlow ? now : state?.qrInvalidatedAt ?? null,
        qrRecoveryInProgress: inQrRecovery || inQrLinkFlow,
        lastAuthFailureAt: now,
        lastAuthFailureReason: message,
      });

      if (inQrRecovery || inQrLinkFlow) {
        this.scheduleQrRecovery(tenantId, state?.autoRestartReason ?? "auth_failure during qr recovery");
      }
    });

    provider.on("notice", (payload: unknown) => {
      if (!this.isCurrentProvider(tenantId, provider)) {
        return;
      }
      this.patchState(tenantId, {
        providerStatus: "notice",
        lastError: this.stringifyPayload(payload),
      });
    });
  }

  private attachVendorListeners(tenantId: string, provider: ProviderLike): void {
    const vendor = provider.vendor;
    if (!vendor) {
      return;
    }

    // The vendor may expose its page after the first listener attachment, or a
    // reconnect may clear the poll timer while retaining the same vendor object.
    // Recheck polling on every attachment without registering duplicate listeners.
    this.startVendorMessagePolling(tenantId, provider);
    if (this.attachedVendors.has(vendor as object)) {
      return;
    }

    this.attachedVendors.add(vendor as object);

    if (typeof vendor.onAnyMessage === "function") {
      try {
        vendor.onAnyMessage((payload: unknown) => {
          const normalized = this.normalizeVendorInbound(payload);
          this.receiveProviderMessage(tenantId, provider, normalized, "vendor_any_message");
        });
        const fallbackListenerCount = this.getVendorListenerCount(vendor, "onAnyMessage");
        this.patchState(tenantId, {
          fallbackMessageBusReady: true,
          fallbackMessageBusListenerCount: fallbackListenerCount,
        });
        console.log(
          `[gateway] fallback message bus ${tenantId} listeners=${fallbackListenerCount ?? "unknown"}`
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        this.patchState(tenantId, {
          fallbackMessageBusReady: false,
          fallbackMessageBusListenerCount: this.getVendorListenerCount(vendor, "onAnyMessage"),
          startupVerificationError: `Fallback message listener failed: ${message}`,
        });
      }
    }

    if (typeof vendor.onStateChange === "function") {
      vendor.onStateChange((payload: unknown) => {
        if (this.shuttingDown || !this.isCurrentProvider(tenantId, provider)) {
          return;
        }
        const vendorState = this.normalizeVendorSignal(payload);
        this.patchState(tenantId, {
          providerStatus: "state_change",
          lastVendorState: vendorState,
        });

        if (vendorState === "CONNECTED") {
          this.clearVendorDisconnectVerification(tenantId);
        }

        if (vendorState && TERMINAL_VENDOR_STATES.has(vendorState)) {
          const reason = `${vendorState.toLowerCase()} via onStateChange`;
          if (vendorState === "TIMEOUT") {
            this.scheduleVendorDisconnectVerification(tenantId, provider, reason);
          } else {
            void this.handleVendorDisconnect(tenantId, reason).catch((error) => {
              const message = error instanceof Error ? error.message : String(error);
              this.patchState(tenantId, {
                providerStatus: "error",
                lastError: message,
                lastTransportError: message,
                disconnectInProgress: false,
                restartInProgress: false,
              });
            });
          }
        }
      });
    }

    if (typeof vendor.onStreamChange === "function") {
      vendor.onStreamChange((payload: unknown) => {
        if (this.shuttingDown || !this.isCurrentProvider(tenantId, provider)) {
          return;
        }
        const vendorStream = this.normalizeVendorSignal(payload);
        this.patchState(tenantId, {
          providerStatus: "stream_change",
          lastVendorStream: vendorStream,
        });

        if (vendorStream === "CONNECTED") {
          this.clearVendorDisconnectVerification(tenantId);
        }

        if (vendorStream && TERMINAL_VENDOR_STREAMS.has(vendorStream)) {
          this.scheduleVendorDisconnectVerification(
            tenantId,
            provider,
            `${vendorStream.toLowerCase()} via onStreamChange`
          );
        }
      });
    }
  }

  private allocateTenantPort(): number {
    return 4100 + this.states.size;
  }

  private async handleProviderMessage(
    tenantId: string,
    provider: ProviderLike,
    ctx: RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown }
  ): Promise<void> {
    if (this.config.pairingOnly === true) return;
    if (this.isOwnMessage(ctx)) {
      return;
    }
    if (this.ignoreStickerInbound(tenantId, ctx)) return;

    let payload = this.toInboundPayload(tenantId, ctx);
    const audienceRejection = this.getInboundAudienceRejection(payload);
    if (audienceRejection) {
      this.recordIgnoredInbound(tenantId, audienceRejection, payload, ctx, extractProviderTimestampMs(ctx));
      return;
    }

    const freshnessDecision = this.shouldProcessInboundMessage(tenantId, ctx, payload);
    if (!freshnessDecision.accepted) {
      this.recordIgnoredInbound(tenantId, freshnessDecision.reason, payload, ctx, freshnessDecision.timestampMs);
      return;
    }

    // WPPConnect can emit the same message through multiple transports. Only an
    // admissible copy may consume the deduplication keys; otherwise an incomplete
    // listener event can block the complete polling copy before it reaches n8n.
    const messageId = payload.message.id;
    if (!messageId) {
      this.recordIgnoredInbound(tenantId, "missing_message_id", payload, ctx, freshnessDecision.timestampMs);
      return;
    }
    if (this.processedMessageIds.has(tenantId, messageId)) {
      this.recordIgnoredInbound(tenantId, "duplicate", payload, ctx, freshnessDecision.timestampMs);
      return;
    }
    const key = this.getConversationKey(tenantId, this.extractRealContactPhone(ctx) || payload.from);
    void this.inboundPreparation.run(key, async () => {
    payload = await this.attachInboundMedia(provider, ctx, payload);
    if (payload.context.hasMedia && !payload.message.media?.data) {
      // The listener may arrive before its bytes; leave the original ID free
      // for a later complete polling copy and never expose empty evidence to n8n.
      // A complete copy may have won while this download was pending.
      if (this.processedMessageIds.has(tenantId, messageId)) {
        this.clearIncompleteMediaNotice(tenantId, messageId);
        this.recordIgnoredInbound(tenantId, "duplicate", payload, ctx, freshnessDecision.timestampMs);
        return;
      }
      this.scheduleIncompleteMediaNotice(tenantId, messageId, payload.from, provider, payload.message.type);
      this.recordIgnoredInbound(tenantId, "media_not_materialized", payload, ctx, freshnessDecision.timestampMs);
      return;
    }
    this.clearIncompleteMediaNotice(tenantId, messageId);
    if (this.processedMessageIds.isDuplicate(tenantId, messageId)) {
      this.recordIgnoredInbound(tenantId, "duplicate", payload, ctx, freshnessDecision.timestampMs);
      return;
    }

    this.patchState(tenantId, {
      lastInboundAt: new Date().toISOString(),
      lastAcceptedInboundAt: new Date().toISOString(),
      lastInboundType: payload.message.type,
      lastInboundTextPreview: this.previewText(payload.message.text),
      lastTransportError: null,
    });

    console.log(`[gateway] inbound ${tenantId} type=${payload.message.type}`);

    this.turnQueue.push(key, payload);
    this.patchState(tenantId, { pendingInboundBatchCount: this.turnQueue.count(tenantId) });
    }).catch(error => {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`[gateway] inbound preparation failed ${tenantId}: ${message}`);
    });
  }

  private async processInboundPayload(tenantId: string, payload: N8nInboundPayload, superseded = () => false): Promise<void> {
    if (this.config.pairingOnly === true) return;
    const provider = this.manager.getBot(tenantId)?.provider as ProviderLike | undefined;
    try {
      // This turn passed audience, freshness, sticker, media and ID checks before
      // entering the queue. The client may use this fact for a cautious response
      // when n8n returns HTTP 200 with no body at all.
      const result = await whileTyping(provider?.vendor, this.toProviderRecipient(payload.from),
        () => this.n8n.execute(payload, { acceptedInbound: true }));
      this.patchState(tenantId, {
        lastN8nAt: new Date().toISOString(),
        lastN8nStatus: result.meta.status,
        sessionReady: true,
      });

      if (!result.response.actions.length || obsoleteInformation(result.response, payload, superseded())) {
        return;
      }

      const bot = this.manager.getBot(tenantId);
      if (!bot) {
        throw new Error(`Bot "${tenantId}" not found`);
      }

      const outboundPreview: string[] = [];
      for (const action of result.response.actions) {
        await this.sendProviderMessage(bot.provider, payload.from, action);
        outboundPreview.push(
          action.type === "text"
            ? this.previewText(action.text) ?? "text"
            : this.previewText(action.caption || (action.type === "media" ? action.url : "Imagen privada")) ?? "media"
        );
      }

      this.patchState(tenantId, {
        lastOutboundAt: new Date().toISOString(),
        lastOutboundPreview: outboundPreview.join(" | ") || null,
        lastTransportError: null,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown transport error";
      this.patchState(tenantId, {
        lastTransportError: message,
      });
      throw error;
    }
  }


  private receiveProviderMessage(
    tenantId: string,
    provider: ProviderLike,
    payload: RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown },
    source: "builderbot_message" | "vendor_any_message" | "vendor_message_poll"
  ): void {
    if (this.config.pairingOnly === true) return;
    if (!this.isCurrentProvider(tenantId, provider)) {
      return;
    }
    if (this.isOwnMessage(payload) || this.ignoreStickerInbound(tenantId, payload)) return;

    const now = new Date().toISOString();
    this.patchState(tenantId, {
      providerStatus: "message",
      qr: null,
      lastMessageEventAt: now,
      lastProviderEventAt: now,
      lastProviderEventSource: source,
      sessionReady: true,
      messageBusReady: true,
      messageBusListenerCount: this.getMessageBusListenerCount(provider.vendor),
      fallbackMessageBusReady: typeof provider.vendor?.onAnyMessage === "function",
      fallbackMessageBusListenerCount: this.getVendorListenerCount(provider.vendor, "onAnyMessage"),
      startupVerificationStatus: "success",
      startupVerificationError: null,
      startupVerifiedAt: now,
      lastError: null,
      disconnectPendingQr: false,
      connectionInvalidatedAt: null,
      disconnectInProgress: false,
      restartInProgress: false,
    });
    // Some QR relinks emit live messages without another "host" event. In that
    // case the new session still needs a fresh cutoff and a settled sync barrier.
    this.startSyncBarrierIfMissing(tenantId);
    this.clearQrRecovery(tenantId);
    this.attachVendorListeners(tenantId, provider);
    void this.inboundReception.run(tenantId, () => this.handleProviderMessageWithContact(tenantId, provider, payload)).catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      console.error(`[gateway] inbound processing failed ${tenantId}: ${message}`);
    });
  }

  private async handleProviderMessageWithContact(
    tenantId: string,
    provider: ProviderLike,
    payload: RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown }
  ): Promise<void> {
    const enriched = await this.enrichProviderContactPhone(tenantId, provider, payload);
    await this.handleProviderMessage(tenantId, provider, enriched);
  }

  private async attachInboundMedia(
    provider: ProviderLike,
    ctx: RawBotContext,
    payload: N8nInboundPayload
  ): Promise<N8nInboundPayload> {
    if (!payload.context.hasMedia || typeof provider.saveFile !== "function") {
      return payload;
    }

    let temporaryPath = "";
    try {
      temporaryPath = await provider.saveFile(ctx);
      if (!temporaryPath) return payload;

      const buffer = await readFile(temporaryPath);
      if (!buffer.length || buffer.length > MAX_INBOUND_MEDIA_BYTES) {
        console.warn(
          `[gateway] inbound media omitted from webhook size=${buffer.length} max=${MAX_INBOUND_MEDIA_BYTES}`
        );
        return payload;
      }

      const raw = ctx as Record<string, unknown>;
      const mimeType = String(raw.mimetype || raw.mimeType || this.mimeTypeFromPath(temporaryPath));
      const originalName = String(raw.filename || raw.fileName || basename(temporaryPath));
      return {
        ...payload,
        message: {
          ...payload.message,
          media: {
            data: buffer.toString("base64"),
            mimeType,
            fileName: originalName.slice(0, 160) || "whatsapp-media",
            size: buffer.length,
          },
        },
      };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.warn(`[gateway] could not materialize inbound media: ${message}`);
      return payload;
    } finally {
      if (temporaryPath) {
        await unlink(temporaryPath).catch(() => undefined);
      }
    }
  }

  private mimeTypeFromPath(filePath: string): string {
    const extension = extname(filePath).toLowerCase();
    const byExtension: Record<string, string> = {
      ".jpg": "image/jpeg",
      ".jpeg": "image/jpeg",
      ".png": "image/png",
      ".webp": "image/webp",
      ".gif": "image/gif",
      ".pdf": "application/pdf",
      ".ogg": "audio/ogg",
      ".opus": "audio/opus",
      ".mp3": "audio/mpeg",
      ".mp4": "video/mp4",
    };
    return byExtension[extension] || "application/octet-stream";
  }

  private async enrichProviderContactPhone(
    tenantId: string,
    provider: ProviderLike,
    payload: RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown }
  ): Promise<RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown }> {
    const directPhone = this.extractRealContactPhone(payload);
    if (directPhone) {
      this.rememberContactPhone(tenantId, String(payload.from || directPhone), directPhone);
      return this.withContactPhone(payload, directPhone);
    }

    const senderWid = String(payload.from || "").trim().toLowerCase();
    if (!senderWid.endsWith("@lid")) {
      return payload;
    }

    const cacheKey = `${tenantId}:${senderWid}`;
    const cachedPhone = this.contactPhoneByWid.get(cacheKey);
    if (cachedPhone) {
      return this.withContactPhone(payload, cachedPhone);
    }

    let resolution = this.contactPhoneResolutions.get(cacheKey);
    if (!resolution) {
      resolution = this.resolveLidContactPhone(provider, senderWid)
        .catch(() => "")
        .finally(() => {
          this.contactPhoneResolutions.delete(cacheKey);
        });
      this.contactPhoneResolutions.set(cacheKey, resolution);
    }

    const phone = await resolution;
    if (!phone) {
      return payload;
    }

    this.rememberContactPhone(tenantId, senderWid, phone);
    return this.withContactPhone(payload, phone);
  }

  private async resolveLidContactPhone(provider: ProviderLike, lid: string): Promise<string> {
    const resolveMapping = async (): Promise<unknown> => {
      if (typeof provider.vendor?.getPnLidEntry === "function") {
        return provider.vendor.getPnLidEntry(lid);
      }

      const page = provider.vendor?.page;
      if (!page || typeof page.evaluate !== "function") {
        return null;
      }

      return page.evaluate(async (contactId: string) => {
        const runtime = globalThis as unknown as {
          WPP?: {
            contact?: {
              getPnLidEntry?: (id: string) => Promise<unknown>;
            };
          };
        };
        return runtime.WPP?.contact?.getPnLidEntry
          ? runtime.WPP.contact.getPnLidEntry(contactId)
          : null;
      }, lid);
    };

    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const mapping = await Promise.race([
        resolveMapping(),
        new Promise<null>((resolve) => {
          timeout = setTimeout(() => resolve(null), 1_500);
          timeout.unref?.();
        }),
      ]);
      const phoneNumber = mapping && typeof mapping === "object"
        ? (mapping as Record<string, unknown>).phoneNumber
        : null;
      return this.normalizeContactPhone(phoneNumber);
    } finally {
      if (timeout) clearTimeout(timeout);
    }
  }

  private withContactPhone(
    payload: RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown },
    phone: string
  ): RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown } {
    const sender = payload.sender && typeof payload.sender === "object"
      ? payload.sender as Record<string, unknown>
      : {};
    return {
      ...payload,
      phone,
      contactPhone: phone,
      sender: { ...sender, phone },
    };
  }

  private extractRealContactPhone(payload: RawBotContext): string {
    const sender = payload.sender && typeof payload.sender === "object"
      ? payload.sender as Record<string, unknown>
      : {};
    const contact = payload.contact && typeof payload.contact === "object"
      ? payload.contact as Record<string, unknown>
      : {};
    const candidates = [
      payload.contactPhone,
      payload.phone,
      sender.phone,
      sender.phoneNumber,
      contact.phone,
      contact.phoneNumber,
      payload.from,
    ];
    for (const candidate of candidates) {
      const phone = this.normalizeContactPhone(candidate);
      if (phone) return phone;
    }
    return "";
  }

  private normalizeContactPhone(value: unknown): string {
    let serialized = "";
    if (typeof value === "string") {
      serialized = value;
    } else if (value && typeof value === "object") {
      const record = value as Record<string, unknown>;
      if (typeof record._serialized === "string") {
        serialized = record._serialized;
      } else if (typeof record.id === "string" && typeof record.server === "string") {
        serialized = `${record.id}@${record.server}`;
      } else if (typeof record.user === "string") {
        serialized = record.user;
      }
    }

    const normalized = serialized.trim().toLowerCase();
    if (!normalized || normalized.endsWith("@lid") || normalized.endsWith("@g.us")) {
      return "";
    }
    const digits = normalized
      .replace(/@(c\.us|s\.whatsapp\.net)$/i, "")
      .replace(/\D/g, "")
      .slice(0, 15);
    return /^\d{8,15}$/.test(digits) ? digits : "";
  }

  private rememberContactPhone(tenantId: string, wid: string, phone: string): void {
    const normalizedWid = wid.trim().toLowerCase();
    if (normalizedWid) {
      this.contactPhoneByWid.set(`${tenantId}:${normalizedWid}`, phone);
    }
    this.contactPhoneByWid.set(`${tenantId}:${phone}`, phone);
    if (this.contactPhoneByWid.size > 5_000) {
      const oldest = this.contactPhoneByWid.keys().next().value;
      if (oldest) this.contactPhoneByWid.delete(oldest);
    }
  }

  private normalizeVendorInbound(payload: unknown): RawBotContext & {
    fromMe?: boolean;
    id?: unknown;
    key?: unknown;
  } {
    const raw = payload && typeof payload === "object" ? { ...(payload as Record<string, unknown>) } : {};
    const providerType = typeof raw.type === "string" ? raw.type.toLowerCase() : "";
    const rawFrom = typeof raw.from === "string" ? raw.from : "";
    const from = rawFrom.replace(/@c\.us$/i, "").replace(/^\+/, "").replace(/\s/g, "");
    let body = typeof raw.body === "string" ? raw.body : "";

    if (["image", "video"].includes(providerType)) {
      body = "_event_media_";
    } else if (providerType === "document") {
      body = "_event_document_";
    } else if (["ptt", "audio"].includes(providerType)) {
      body = "_event_voice_note_";
    } else if (raw.lat !== undefined && raw.lng !== undefined) {
      body = "_event_location_";
    }

    return {
      ...raw,
      body,
      from,
      name:
        typeof raw.notifyName === "string"
          ? raw.notifyName
          : typeof raw.name === "string"
            ? raw.name
            : typeof raw.author === "string"
              ? raw.author
              : "",
    } as RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown };
  }

  private startVendorMessagePolling(tenantId: string, provider: ProviderLike): void {
    const vendor = provider.vendor;
    if (!vendor?.page || typeof vendor.page.evaluate !== "function") {
      this.patchState(tenantId, {
        pollingMessageBusReady: false,
        pollingMessageBusError: "Vendor page is not available for message polling",
      });
      return;
    }

    const currentProvider = this.vendorMessagePollProviders.get(tenantId);
    if (currentProvider === provider && this.vendorMessagePollTimers.has(tenantId)) {
      return;
    }

    this.clearVendorMessagePolling(tenantId);
    this.vendorMessagePollProviders.set(tenantId, provider);
    this.vendorMessagePollCursors.set(tenantId, Date.now());
    this.vendorMessagePollFailures.set(tenantId, 0);
    this.patchState(tenantId, {
      pollingMessageBusReady: true,
      pollingMessageBusLastRunAt: null,
      pollingMessageBusCursorAt: new Date().toISOString(),
      pollingMessageBusError: null,
      pollingMessageBusConsecutiveErrors: 0,
    });

    const run = () => {
      void this.pollVendorMessages(tenantId, provider);
    };
    const timer = setInterval(run, VENDOR_MESSAGE_POLL_MS);
    timer.unref?.();
    this.vendorMessagePollTimers.set(tenantId, timer);
    run();
    console.log(`[gateway] polling message bus ${tenantId} interval=${VENDOR_MESSAGE_POLL_MS}ms`);
  }

  private clearVendorMessagePolling(tenantId: string): void {
    const timer = this.vendorMessagePollTimers.get(tenantId);
    if (timer) {
      clearInterval(timer);
    }
    this.vendorMessagePollTimers.delete(tenantId);
    this.vendorMessagePollProviders.delete(tenantId);
    this.vendorMessagePollCursors.delete(tenantId);
    this.vendorMessagePollBusy.delete(tenantId);
    this.vendorMessagePollFailures.delete(tenantId);
  }

  private async pollVendorMessages(tenantId: string, provider: ProviderLike): Promise<void> {
    if (this.vendorMessagePollBusy.has(tenantId) || !this.isCurrentProvider(tenantId, provider)) {
      return;
    }

    const state = this.getTenant(tenantId);
    const vendorPage = provider.vendor?.page;
    if (!state || !vendorPage || typeof vendorPage.evaluate !== "function") {
      return;
    }

    if (!state.sessionReady || state.syncBarrierActive || !state.syncSettledAt) {
      const cursorMs = Date.now();
      this.vendorMessagePollCursors.set(tenantId, cursorMs);
      this.patchState(tenantId, {
        pollingMessageBusReady: true,
        pollingMessageBusCursorAt: new Date(cursorMs).toISOString(),
        pollingMessageBusError: null,
      });
      return;
    }

    const settledAtMs = this.isoToMs(state.syncSettledAt) ?? Date.now();
    const cursorMs = Math.max(this.vendorMessagePollCursors.get(tenantId) ?? settledAtMs, settledAtMs);
    this.vendorMessagePollBusy.add(tenantId);

    try {
      const result = await vendorPage.evaluate(
        async (input: { sinceMs: number }) => {
          type BrowserWid = { _serialized?: string; toString?: () => string } | string | null | undefined;
          type BrowserMessage = Record<string, any>;
          type BrowserChat = Record<string, any>;
          const runtime = globalThis as unknown as {
            WPP?: { chat?: { list?: (options: Record<string, unknown>) => Promise<BrowserChat[]> } };
          };
          const serializeWid = (value: BrowserWid): string => {
            if (typeof value === "string") return value;
            if (typeof value?._serialized === "string") return value._serialized;
            return typeof value?.toString === "function" ? value.toString() : "";
          };
          const chatApi = runtime.WPP?.chat;
          if (!chatApi || typeof chatApi.list !== "function") {
            return { ready: false, messages: [] as Array<Record<string, unknown>> };
          }

          const chats = await chatApi.list({ onlyUsers: true, count: 120 });
          const messages: Array<Record<string, unknown>> = [];
          const emittedIds = new Set<string>();

          for (const chat of chats) {
            const models: BrowserMessage[] = chat?.msgs?._models ?? chat?.msgs?.models ?? [];
            const contactPhone = serializeWid(chat?.contact?.phoneNumber);
            const chatName = String(chat?.name ?? chat?.formattedTitle ?? chat?.contact?.pushname ?? "");

            for (const message of models.slice(-16)) {
              const rawTimestamp = Number(message?.t ?? message?.timestamp ?? 0);
              const timestampMs = rawTimestamp >= 1_000_000_000_000 ? rawTimestamp : rawTimestamp * 1000;
              if (!Number.isFinite(timestampMs) || timestampMs < input.sinceMs) continue;

              const fromMe = Boolean(message?.id?.fromMe ?? message?.fromMe ?? message?.isSentByMe);
              if (fromMe) continue;

              const id = serializeWid(message?.id);
              if (!id || emittedIds.has(id)) continue;
              emittedIds.add(id);

              const rawFrom = serializeWid(message?.from);
              const from = contactPhone.endsWith("@c.us") ? contactPhone : rawFrom;
              if (!from || from === "status@broadcast" || from.endsWith("@g.us")) continue;

              const type = String(message?.type ?? "chat").toLowerCase();
              let body = typeof message?.body === "string" ? message.body : "";
              if (["image", "video"].includes(type)) {
                body = "_event_media_";
              } else if (type === "document") {
                body = "_event_document_";
              } else if (["ptt", "audio"].includes(type)) {
                body = "_event_voice_note_";
              } else if (message?.lat !== undefined && message?.lng !== undefined) {
                body = "_event_location_";
              }

              messages.push({
                id,
                key: { id, fromMe: false, remoteJid: from },
                from,
                to: serializeWid(message?.to),
                fromMe: false,
                isSentByMe: false,
                body,
                type,
                subtype: message?.subtype ?? null,
                name: chatName,
                notifyName: chatName,
                // A chat/agenda title is not necessarily the sender's profile.
                contact_display_name: typeof chat?.contact?.pushname === "string" ? chat.contact.pushname : null,
                author: chatName,
                t: rawTimestamp,
                timestamp: rawTimestamp,
                isNewMsg: message?.isNewMsg ?? null,
                recvFresh: message?.recvFresh ?? null,
                isMdHistoryMsg: message?.isMdHistoryMsg ?? message?.__x_isMdHistoryMsg ?? null,
              });
            }
          }

          messages.sort((left, right) => Number(left.t ?? 0) - Number(right.t ?? 0));
          return { ready: true, messages: messages.slice(-50) };
        },
        { sinceMs: cursorMs }
      );

      const nextCursorMs = Math.max(cursorMs, Date.now() - VENDOR_MESSAGE_POLL_OVERLAP_MS);
      this.vendorMessagePollCursors.set(tenantId, nextCursorMs);
      this.patchState(tenantId, {
        pollingMessageBusReady: result.ready,
        pollingMessageBusLastRunAt: new Date().toISOString(),
        pollingMessageBusCursorAt: new Date(nextCursorMs).toISOString(),
        pollingMessageBusError: result.ready ? null : "WPP chat API is not ready",
        pollingMessageBusConsecutiveErrors: 0,
      });
      this.vendorMessagePollFailures.set(tenantId, 0);

      if (result.messages.length > 0) {
        console.log(`[gateway] polling message bus ${tenantId} candidates=${result.messages.length}`);
      }
      for (const rawMessage of result.messages) {
        const normalized = this.normalizeVendorInbound(rawMessage);
        this.receiveProviderMessage(tenantId, provider, normalized, "vendor_message_poll");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const consecutiveErrors = (this.vendorMessagePollFailures.get(tenantId) ?? 0) + 1;
      this.vendorMessagePollFailures.set(tenantId, consecutiveErrors);
      this.patchState(tenantId, {
        pollingMessageBusReady: false,
        pollingMessageBusLastRunAt: new Date().toISOString(),
        pollingMessageBusError: message,
        pollingMessageBusConsecutiveErrors: consecutiveErrors,
      });
      if (
        consecutiveErrors >= VENDOR_MESSAGE_POLL_FAILURE_THRESHOLD &&
        this.isDetachedVendorRuntimeError(message)
      ) {
        void this.recoverDetachedVendorRuntime(tenantId, provider, message);
      }
    } finally {
      this.vendorMessagePollBusy.delete(tenantId);
    }
  }

  private isDetachedVendorRuntimeError(message: string): boolean {
    return /detached\s+frame|frame\s+was\s+detached|execution context was destroyed|cannot find context|target closed|session closed|page has been closed|protocol error.*(?:target|session)/i.test(
      message
    );
  }

  private async recoverDetachedVendorRuntime(
    tenantId: string,
    provider: ProviderLike,
    reason: string
  ): Promise<void> {
    if (
      this.shuttingDown ||
      !this.isCurrentProvider(tenantId, provider) ||
      this.vendorRuntimeRecoveryLocks.has(tenantId)
    ) {
      return;
    }

    const nowMs = Date.now();
    const lastRecoveryMs = this.vendorRuntimeRecoveryLastAt.get(tenantId) ?? 0;
    if (nowMs - lastRecoveryMs < VENDOR_RUNTIME_RECOVERY_COOLDOWN_MS) {
      return;
    }

    this.vendorRuntimeRecoveryLocks.add(tenantId);
    this.vendorRuntimeRecoveryLastAt.set(tenantId, nowMs);
    const recoveryAt = new Date(nowMs).toISOString();
    const state = this.getTenant(tenantId);
    this.patchState(tenantId, {
      messageBusReady: false,
      pollingMessageBusReady: false,
      pollingMessageBusRecoveryAt: recoveryAt,
      pollingMessageBusRecoveryReason: reason,
      recoveryAttemptCount: (state?.recoveryAttemptCount ?? 0) + 1,
      lastRecoveryAt: recoveryAt,
      startupVerificationStatus: "recovering",
      startupVerificationError: `Recovering detached WPPConnect runtime: ${reason}`,
    });
    this.clearVendorMessagePolling(tenantId);
    console.warn(`[gateway] recovering detached WPPConnect runtime ${tenantId}: ${reason}`);

    try {
      await this.restartTenant(tenantId);
      console.log(`[gateway] WPPConnect runtime recreated ${tenantId}; preserved stored session`);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.patchState(tenantId, {
        providerStatus: "error",
        lastError: message,
        lastTransportError: message,
        startupVerificationStatus: "failed",
        startupVerificationError: message,
      });
      console.error(`[gateway] WPPConnect runtime recovery failed ${tenantId}: ${message}`);
    } finally {
      this.vendorRuntimeRecoveryLocks.delete(tenantId);
    }
  }

  private async sendProviderMessage(
    provider: ProviderLike,
    number: string,
    action: GatewayAction
  ): Promise<ProviderReceipt | void> {
    if (this.config.pairingOnly === true) throw new Error("QR preview has consumer messaging paused");
    const vendor = provider.vendor;
    if (!vendor || typeof vendor.sendText !== "function") {
      throw new Error("Provider vendor is not ready to send messages");
    }

    const recipient = this.toProviderRecipient(number);

    if (action.type === "pos_menu") {
      for await (const image of currentMenuImages(this.config)) {
        await this.sendProviderMessage(provider, number, image);
      }
      return;
    }

    if (action.type === "media_binary") {
      const image = validateBinaryMedia({ ...action });
      if (typeof vendor.sendImageFromBase64 !== "function") throw new Error("Provider cannot send private images");
      const extension = image.mimeType === "image/jpeg" ? "jpg" : image.mimeType.split("/")[1];
      const result = await vendor.sendImageFromBase64(recipient, `data:${image.mimeType};base64,${image.data}`, `restaurante.${extension}`, image.caption ? withSingleEmoji(image.caption) : "");
      if (!result?.id || result?.erro === true || result?.error === true) throw new Error("Private image delivery could not be confirmed");
      return providerReceipt(result.id, result.ack);
    }

    if (action.type === "text") {
      const vendorPage = vendor.page;
      if (!vendorPage || typeof vendorPage.evaluate !== "function") {
        throw new Error("Provider page is not ready to send text messages");
      }

      const result = await vendorPage.evaluate(
        async (input: { to: string; text: string }) => {
          const runtime = globalThis as unknown as {
            WPP?: {
              chat?: {
                sendTextMessage?: (
                  to: string,
                  text: string,
                  options?: Record<string, unknown>
                ) => Promise<Record<string, any>>;
              };
            };
          };
          const sendTextMessage = runtime.WPP?.chat?.sendTextMessage;
          if (typeof sendTextMessage !== "function") {
            throw new Error("WPP text sender is not available");
          }

          const response = await sendTextMessage(input.to, input.text, { waitForAck: true });
          return {
            id: typeof response?.id === "string" ? response.id : response?.id?._serialized ?? null,
            ack: typeof response?.ack === "number" ? response.ack : null,
            messageSendResult:
              typeof response?.sendMsgResult?.messageSendResult === "string"
                ? response.sendMsgResult.messageSendResult
                : null,
          };
        },
        { to: recipient, text: withSingleEmoji(action.text) }
      );

      if (result.messageSendResult?.startsWith("ERROR")) {
        throw new Error(`WhatsApp rejected the outbound message: ${result.messageSendResult}`);
      }
      if (!result.id) {
        throw new Error("WhatsApp did not return an outbound message id");
      }
      return providerReceipt(result.id, result.ack);
    }

    await provider.sendMessage(recipient, action.caption ? withSingleEmoji(action.caption) : "", {
      media: action.url,
    });
  }

  private toProviderRecipient(number: string): string {
    const normalized = number.trim().replace(/^\+/, "").replace(/\s/g, "");
    if (!normalized) {
      throw new Error("Outbound recipient is empty");
    }
    if (/@(?:c\.us|lid|g\.us)$/i.test(normalized)) {
      return normalized;
    }
    return `${normalized}@c.us`;
  }

  private async handleVendorDisconnect(tenantId: string, reason: string): Promise<void> {
    if (this.shuttingDown) {
      return;
    }

    this.clearVendorDisconnectVerification(tenantId);
    this.startQrRegeneration(tenantId, {
      reason,
      logoutVendor: false,
      closeVendor: true,
      source: "vendor_disconnect",
    });
  }

  private scheduleVendorDisconnectVerification(
    tenantId: string,
    provider: ProviderLike,
    reason: string
  ): void {
    this.clearVendorDisconnectVerification(tenantId);
    const timer = setTimeout(() => {
      this.vendorDisconnectTimers.delete(tenantId);
      void this.verifyVendorDisconnect(tenantId, provider, reason);
    }, VENDOR_DISCONNECT_GRACE_MS);
    timer.unref?.();
    this.vendorDisconnectTimers.set(tenantId, timer);
    console.log(`[gateway] vendor disconnect pending ${tenantId} reason=${reason}`);
  }

  private clearVendorDisconnectVerification(tenantId: string): void {
    const timer = this.vendorDisconnectTimers.get(tenantId);
    if (timer) {
      clearTimeout(timer);
    }
    this.vendorDisconnectTimers.delete(tenantId);
  }

  private async verifyVendorDisconnect(
    tenantId: string,
    provider: ProviderLike,
    reason: string
  ): Promise<void> {
    if (this.shuttingDown || !this.isCurrentProvider(tenantId, provider)) {
      return;
    }

    const vendor = provider.vendor;
    if (!vendor) {
      await this.handleVendorDisconnect(tenantId, reason);
      return;
    }

    let connected: boolean | null = null;
    let connectionState: string | null = null;
    try {
      if (typeof vendor.isConnected === "function") {
        connected = await this.withTimeout(vendor.isConnected(), 5_000, "Vendor connection check timed out");
      }
      if (typeof vendor.getConnectionState === "function") {
        connectionState = this.normalizeVendorSignal(
          await this.withTimeout(vendor.getConnectionState(), 5_000, "Vendor state check timed out")
        );
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.patchState(tenantId, { lastError: `Disconnect verification: ${message}` });
    }

    if (connected === true || connectionState === "CONNECTED") {
      this.patchState(tenantId, {
        providerStatus: "host",
        lastVendorStream: "CONNECTED",
        lastVendorState: connectionState ?? this.getTenant(tenantId)?.lastVendorState ?? null,
        sessionReady: true,
        lastError: null,
        lastTransportError: null,
      });
      console.log(`[gateway] transient vendor disconnect ignored ${tenantId}`);
      return;
    }

    if (connected === null && connectionState === null) {
      this.patchState(tenantId, {
        providerStatus: "stream_change",
        lastError: `Provider reported ${reason}, but no terminal state could be verified`,
      });
      return;
    }

    await this.handleVendorDisconnect(tenantId, reason);
  }

  private startQrRegeneration(
    tenantId: string,
    options: { reason: string; logoutVendor: boolean; closeVendor: boolean; source: string }
  ): TenantState {
    const currentConfig = this.tenantConfigs.get(tenantId);
    if (!currentConfig) {
      throw new Error(`Bot "${tenantId}" not found`);
    }

    if (this.disconnectRestartLocks.has(tenantId)) {
      return this.requireState(tenantId);
    }

    this.clearStartupVerification(tenantId);
    this.clearQrRecovery(tenantId);
    this.clearVendorMessagePolling(tenantId);
    this.clearVendorDisconnectVerification(tenantId);

    const disconnectedAt = new Date().toISOString();
    const previousState = this.states.get(tenantId);
    const nextGeneration = (previousState?.disconnectGeneration ?? 0) + 1;

    this.markTenantDisconnected(tenantId, options.reason, disconnectedAt, {
      source: options.source,
      generation: nextGeneration,
    });

    void this.resetTenantSession(tenantId, options, {
      disconnectedAt,
      nextGeneration,
      previousState,
      preMarkedDisconnected: true,
    }).catch((error) => {
      const message = error instanceof Error ? error.message : "Unknown session reset error";
      this.patchState(tenantId, {
        providerStatus: "error",
        lastError: message,
        lastTransportError: message,
        startupVerificationStatus: "failed",
        startupVerificationError: message,
        disconnectPendingQr: false,
        disconnectInProgress: false,
        restartInProgress: false,
      });
      console.error(`[gateway] qr regeneration failed for ${tenantId}: ${message}`);
    });

    return this.requireState(tenantId);
  }

  private markTenantDisconnected(
    tenantId: string,
    reason: string,
    disconnectedAt = new Date().toISOString(),
    options: { source: string; generation?: number; keepQr?: boolean } = { source: "unknown" }
  ): void {
    const current = this.getTenant(tenantId);
    const nextGeneration = options.generation ?? ((current?.disconnectGeneration ?? 0) + 1);
    this.patchState(tenantId, {
      providerStatus: "disconnected",
      sessionReady: false,
      messageBusReady: false,
      pollingMessageBusReady: false,
      pollingMessageBusCursorAt: null,
      pollingMessageBusError: null,
      host: undefined,
      qr: options.keepQr ? current?.qr ?? null : null,
      lastMessageEventAt: null,
      lastProviderEventAt: null,
      lastAcceptedInboundAt: null,
      lastInboundAt: null,
      lastInboundType: null,
      lastInboundTextPreview: null,
      lastOutboundAt: null,
      lastOutboundPreview: null,
      lastDisconnectAt: disconnectedAt,
      lastDisconnectReason: reason,
      autoRestartReason: reason,
      startupVerificationStatus: "failed",
      startupVerificationError: reason,
      lastError: reason,
      lastTransportError: null,
      disconnectPendingQr: true,
      disconnectGeneration: nextGeneration,
      connectionInvalidatedAt: disconnectedAt,
      disconnectInProgress: true,
      restartInProgress: true,
      qrInvalidatedAt: disconnectedAt,
      qrRecoveryInProgress: true,
      syncBarrierActive: false,
      syncBarrierStartedAt: null,
      syncLastNoiseAt: null,
      syncSettledAt: null,
    });
  }

  private buildRestartStateCarryover(previousState?: TenantState): Partial<TenantState> {
    if (!previousState) {
      return {};
    }

    return {
      lastDisconnectAt: previousState.lastDisconnectAt ?? null,
      lastDisconnectReason: previousState.lastDisconnectReason ?? null,
      lastVendorState: previousState.lastVendorState ?? null,
      lastVendorStream: previousState.lastVendorStream ?? null,
      autoRestartReason: previousState.autoRestartReason ?? null,
      recoveryAttemptCount: previousState.recoveryAttemptCount ?? 0,
      lastRecoveryAt: previousState.lastRecoveryAt ?? null,
      pollingMessageBusRecoveryAt: previousState.pollingMessageBusRecoveryAt ?? null,
      pollingMessageBusRecoveryReason: previousState.pollingMessageBusRecoveryReason ?? null,
      disconnectPendingQr: false,
      disconnectGeneration: previousState.disconnectGeneration ?? 0,
      connectionInvalidatedAt: previousState.connectionInvalidatedAt ?? null,
      disconnectInProgress: false,
      restartInProgress: false,
      qrUpdatedAt: previousState.qrUpdatedAt ?? null,
      qrGeneration: previousState.qrGeneration ?? 0,
      qrInvalidatedAt: previousState.qrInvalidatedAt ?? null,
      qrRecoveryInProgress: false,
      lastAuthFailureAt: previousState.lastAuthFailureAt ?? null,
      lastAuthFailureReason: previousState.lastAuthFailureReason ?? null,
    };
  }

  private async resetTenantSession(
    tenantId: string,
    options: { reason: string; logoutVendor: boolean; closeVendor: boolean; source: string },
    transition?: {
      disconnectedAt: string;
      nextGeneration: number;
      previousState?: TenantState;
      preMarkedDisconnected?: boolean;
    }
  ): Promise<TenantState> {
    const currentConfig = this.tenantConfigs.get(tenantId);
    const previousState = transition?.previousState ?? this.states.get(tenantId);
    if (!currentConfig) {
      throw new Error(`Bot "${tenantId}" not found`);
    }

    if (this.disconnectRestartLocks.has(tenantId)) {
      return this.requireState(tenantId);
    }

    this.disconnectRestartLocks.add(tenantId);
    const disconnectedAt = transition?.disconnectedAt ?? new Date().toISOString();
    const nextGeneration = transition?.nextGeneration ?? ((previousState?.disconnectGeneration ?? 0) + 1);
    const bot = this.manager.getBot(tenantId);
    const provider = bot?.provider as ProviderLike | undefined;

    if (!transition?.preMarkedDisconnected) {
      this.clearStartupVerification(tenantId);
      this.markTenantDisconnected(tenantId, options.reason, disconnectedAt, {
        source: options.source,
        generation: nextGeneration,
      });
    }

    try {
      this.clearVendorMessagePolling(tenantId);
      this.clearVendorDisconnectVerification(tenantId);
      try {
        if (provider?.vendor && options.logoutVendor && typeof provider.vendor.logout === "function") {
          await provider.vendor.logout();
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "Unknown vendor logout error";
        this.patchState(tenantId, { lastError: message });
      }

      try {
        if (provider?.vendor && options.closeVendor && typeof provider.vendor.close === "function") {
          await provider.vendor.close();
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "Unknown vendor close error";
        this.patchState(tenantId, { lastError: message });
      }

      await this.stopProviderHttpServer(provider);
      await this.manager.removeBot(tenantId);

      await this.killTenantBrowserProcesses(tenantId);

      await this.createTenant({
        tenantId,
        name: currentConfig.name,
        providerOptions: currentConfig.providerOptions,
      });

      const restartedState = this.requireState(tenantId);
      const shouldPreserveProviderState =
        restartedState.providerStatus === "require_action" ||
        restartedState.providerStatus === "host" ||
        restartedState.providerStatus === "message";
      this.patchState(tenantId, {
        ...this.buildRestartStateCarryover(previousState),
        providerStatus: shouldPreserveProviderState ? restartedState.providerStatus : "restarting",
        lastDisconnectAt: disconnectedAt,
        lastDisconnectReason: options.reason,
        autoRestartReason: options.reason,
        disconnectPendingQr: true,
        disconnectGeneration: nextGeneration,
        connectionInvalidatedAt: disconnectedAt,
        disconnectInProgress: false,
        restartInProgress: !shouldPreserveProviderState,
        qrRecoveryInProgress: !shouldPreserveProviderState,
        sessionReady: shouldPreserveProviderState ? restartedState.sessionReady : false,
        messageBusReady: shouldPreserveProviderState ? restartedState.messageBusReady : false,
        qr: shouldPreserveProviderState ? restartedState.qr ?? null : null,
        host: shouldPreserveProviderState ? restartedState.host : undefined,
      });
      this.scheduleQrRecovery(tenantId, options.reason);

      return this.requireState(tenantId);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown session reset error";
      this.patchState(tenantId, {
        providerStatus: "disconnected",
        lastError: message,
        lastTransportError: message,
        disconnectPendingQr: true,
        disconnectInProgress: false,
        restartInProgress: false,
        qrRecoveryInProgress: false,
      });
      throw error;
    } finally {
      this.disconnectRestartLocks.delete(tenantId);
    }
  }

  private getTenantSessionPaths(tenantId: string): string[] {
    return [
      resolve(this.config.sessionsDir, tenantId),
      resolve(this.config.sessionsDir, `${tenantId}_sessions`),
    ];
  }

  private async killTenantBrowserProcesses(tenantId: string): Promise<number> {
    if (process.platform !== "win32") {
      return 0;
    }

    const processIds = await this.findTenantBrowserProcessIds(tenantId);
    if (!processIds.length) {
      return 0;
    }

    let killed = 0;
    for (const processId of processIds) {
      try {
        await execFile("taskkill", ["/PID", String(processId), "/T", "/F"], {
          windowsHide: true,
        });
        killed += 1;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (!/not found|no running instance|cannot find|no se encontr/i.test(message)) {
          this.patchState(tenantId, {
            lastError: `taskkill ${processId}: ${message}`,
          });
        }
      }
    }

    if (killed > 0) {
      console.log(`[gateway] killed ${killed} browser process(es) for ${tenantId}`);
      await this.sleep(1_000);
    }

    return killed;
  }

  private async findTenantBrowserProcessIds(tenantId: string): Promise<number[]> {
    if (process.platform !== "win32") {
      return [];
    }

    const patterns = this.getTenantSessionPaths(tenantId)
      .flatMap((sessionPath) => [sessionPath.toLowerCase(), sessionPath.replaceAll("\\", "/").toLowerCase()])
      .filter((pattern, index, values) => values.indexOf(pattern) === index);

    const escapedPatterns = patterns
      .map((pattern) => `'${pattern.replaceAll("'", "''")}'`)
      .join(", ");
    const script = `
$patterns = @(${escapedPatterns})
$processes = Get-CimInstance Win32_Process | Where-Object {
  $name = [string]$_.Name
  $exe = [string]$_.ExecutablePath
  $cmd = [string]$_.CommandLine
  if (-not $cmd) { return $false }
  if (($name -notmatch 'msedge|chrome') -and ($exe -notmatch 'msedge|chrome')) { return $false }
  $cmdLower = $cmd.ToLower()
  foreach ($pattern in $patterns) {
    if ($pattern -and $cmdLower.Contains($pattern)) {
      return $true
    }
  }
  return $false
} | Select-Object -ExpandProperty ProcessId
if ($processes) {
  $processes | ConvertTo-Json -Compress
}
`;

    try {
      const { stdout } = await execFile(
        "powershell.exe",
        ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        {
          windowsHide: true,
          maxBuffer: 1024 * 1024,
        }
      );
      const trimmed = stdout.trim();
      if (!trimmed) {
        return [];
      }

      const parsed = JSON.parse(trimmed) as number | number[];
      const processIds = Array.isArray(parsed) ? parsed : [parsed];
      return processIds.filter((value): value is number => Number.isInteger(value) && value > 0);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.patchState(tenantId, {
        lastError: `find browser processes failed: ${message}`,
      });
      return [];
    }
  }

  private normalizeVendorSignal(payload: unknown): string | null {
    if (typeof payload === "string") {
      const normalized = payload.trim().toUpperCase();
      return normalized || null;
    }

    if (payload && typeof payload === "object") {
      for (const key of ["state", "stream", "status", "socket", "type"]) {
        const value = (payload as Record<string, unknown>)[key];
        if (typeof value === "string" && value.trim()) {
          return value.trim().toUpperCase();
        }
      }
    }

    return null;
  }

  private toSerializableContext(ctx: RawBotContext): Record<string, unknown> {
    try {
      return JSON.parse(JSON.stringify(ctx)) as Record<string, unknown>;
    } catch {
      return {
        body: ctx.body,
        from: ctx.from,
        name: ctx.name,
      };
    }
  }

  private requireState(tenantId: string): TenantState {
    const state = this.states.get(tenantId);
    if (!state) {
      throw new Error(`Bot "${tenantId}" not found`);
    }
    return state;
  }

  private patchState(tenantId: string, patch: Partial<TenantState>): void {
    const current = this.states.get(tenantId);
    if (!current) return;

    this.states.set(tenantId, this.deriveState({
      ...current,
      ...patch,
      updatedAt: new Date().toISOString(),
    }));
  }

  private scheduleStartupVerification(tenantId: string): void {
    this.clearStartupVerification(tenantId);
    const timer = setTimeout(() => {
      void this.verifyStartupSession(tenantId);
    }, STARTUP_VERIFICATION_DELAY_MS);
    this.startupVerificationTimers.set(tenantId, timer);
  }

  private clearStartupVerification(tenantId: string): void {
    const timer = this.startupVerificationTimers.get(tenantId);
    if (timer) {
      clearTimeout(timer);
      this.startupVerificationTimers.delete(tenantId);
    }
  }

  private scheduleSyncBarrierSettlement(tenantId: string): void {
    this.clearSyncBarrierSettlement(tenantId);
    const state = this.getTenant(tenantId);
    if (!state?.syncBarrierActive) {
      return;
    }

    const referenceMs = this.isoToMs(state.syncLastNoiseAt) ?? this.isoToMs(state.syncBarrierStartedAt) ?? Date.now();
    const elapsedMs = Math.max(0, Date.now() - referenceMs);
    const delayMs = Math.max(0, SYNC_BARRIER_SETTLE_MS - elapsedMs);
    const timer = setTimeout(() => {
      this.syncBarrierTimers.delete(tenantId);
      this.maybeSettleSyncBarrier(tenantId);
      if (this.getTenant(tenantId)?.syncBarrierActive) {
        this.scheduleSyncBarrierSettlement(tenantId);
      }
    }, delayMs + 25);
    this.syncBarrierTimers.set(tenantId, timer);
  }

  private clearSyncBarrierSettlement(tenantId: string): void {
    const timer = this.syncBarrierTimers.get(tenantId);
    if (timer) {
      clearTimeout(timer);
    }
    this.syncBarrierTimers.delete(tenantId);
  }

  private clearQrRecovery(tenantId: string): void {
    const timer = this.qrRecoveryTimers.get(tenantId);
    if (timer) {
      clearTimeout(timer);
      this.qrRecoveryTimers.delete(tenantId);
    }
    this.qrRecoveryAttempts.delete(tenantId);
  }

  private scheduleQrRecovery(tenantId: string, reason: string): void {
    const state = this.getTenant(tenantId);
    if (!state) {
      return;
    }

    if (this.hasCurrentQr(state) || (state.providerStatus === "require_action" && !state.qrRecoveryInProgress)) {
      this.clearQrRecovery(tenantId);
      return;
    }

    const attempts = this.qrRecoveryAttempts.get(tenantId) ?? 0;
    if (attempts >= QR_RECOVERY_MAX_ATTEMPTS) {
      this.clearQrRecovery(tenantId);
      this.patchState(tenantId, {
        providerStatus: "error",
        lastError: "No se pudo regenerar el QR automaticamente.",
        startupVerificationStatus: "failed",
        startupVerificationError: "No se pudo regenerar el QR automaticamente.",
        disconnectPendingQr: false,
        disconnectInProgress: false,
        restartInProgress: false,
        qrRecoveryInProgress: false,
      });
      return;
    }

    const nextAttempt = attempts + 1;
    this.qrRecoveryAttempts.set(tenantId, nextAttempt);

    const existingTimer = this.qrRecoveryTimers.get(tenantId);
    if (existingTimer) {
      clearTimeout(existingTimer);
    }

    const timer = setTimeout(() => {
      void this.runQrRecovery(tenantId, reason, nextAttempt);
    }, QR_RECOVERY_WAIT_MS);
    this.qrRecoveryTimers.set(tenantId, timer);
  }

  private async runQrRecovery(tenantId: string, reason: string, expectedAttempt: number): Promise<void> {
    const currentAttempt = this.qrRecoveryAttempts.get(tenantId);
    if (currentAttempt !== expectedAttempt) {
      return;
    }

    this.qrRecoveryTimers.delete(tenantId);
    const state = this.getTenant(tenantId);
    if (!state) {
      this.clearQrRecovery(tenantId);
      return;
    }

    if (this.hasCurrentQr(state) || (state.providerStatus === "require_action" && !state.qrRecoveryInProgress)) {
      this.clearQrRecovery(tenantId);
      return;
    }

    if (state.effectiveStatus === "connected") {
      this.clearQrRecovery(tenantId);
      return;
    }

    this.patchState(tenantId, {
      providerStatus: "disconnecting",
      startupVerificationStatus: "recovering",
      startupVerificationError: `Reintentando regenerar QR (${expectedAttempt}/${QR_RECOVERY_MAX_ATTEMPTS})`,
      disconnectPendingQr: true,
      disconnectInProgress: false,
      restartInProgress: true,
      qrRecoveryInProgress: true,
    });

    try {
      await this.resetTenantSession(tenantId, {
        reason,
        logoutVendor: false,
        closeVendor: true,
        source: "qr_recovery",
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.patchState(tenantId, {
        providerStatus: "error",
        lastError: message,
        startupVerificationStatus: "failed",
        startupVerificationError: message,
        disconnectPendingQr: false,
        disconnectInProgress: false,
        restartInProgress: false,
        qrRecoveryInProgress: false,
      });
    }
  }

  private async verifyStartupSession(tenantId: string): Promise<void> {
    this.clearStartupVerification(tenantId);

    const state = this.getTenant(tenantId);
    if (!state || state.status !== "connected") {
      return;
    }

    try {
      const provider = this.getProvider(tenantId);
      const vendor = await this.ensureHealthyVendor(provider);
      const wid = typeof vendor?.getWid === "function"
        ? await this.withTimeout(vendor.getWid(), PROVIDER_HEALTH_TIMEOUT_MS, "Provider getWid timed out")
        : null;
      const messageBusReady = this.ensureProviderMessageBus(tenantId, provider, vendor);
      const verifiedAt = new Date().toISOString();

      this.patchState(tenantId, {
        sessionReady: Boolean(wid),
        messageBusReady,
        messageBusListenerCount: this.getMessageBusListenerCount(vendor),
        startupVerifiedAt: verifiedAt,
        startupVerificationStatus: messageBusReady ? "success" : "failed",
        startupVerificationError: messageBusReady ? null : "Vendor message bus is not available",
        lastError: messageBusReady ? null : state.lastError,
      });

      if (!messageBusReady) {
        await this.recoverTenant(tenantId, "startup verification failed: message bus unavailable");
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown startup verification error";
      this.patchState(tenantId, {
        sessionReady: false,
        messageBusReady: false,
        startupVerifiedAt: new Date().toISOString(),
        startupVerificationStatus: "failed",
        startupVerificationError: message,
        lastTransportError: message,
      });
      await this.recoverTenant(tenantId, `startup verification failed: ${message}`);
    }
  }

  private async runWatchdog(): Promise<void> {
    if (this.config.pairingOnly === true) return;
    for (const state of this.listTenants()) {
      this.maybeSettleSyncBarrier(state.tenantId);
      if (state.status === "connected" && state.sessionReady) {
        this.startSyncBarrierIfMissing(state.tenantId);
        const provider = this.manager.getBot(state.tenantId)?.provider as ProviderLike | undefined;
        if (provider?.vendor && !this.vendorMessagePollTimers.has(state.tenantId)) {
          this.attachVendorListeners(state.tenantId, provider);
        }
      }
      this.patchState(state.tenantId, {});
    }
  }

  private async recoverTenant(tenantId: string, reason: string): Promise<void> {
    if (this.recoveryLocks.has(tenantId)) {
      return;
    }

    this.recoveryLocks.add(tenantId);
    const stats = this.recoveryStats.get(tenantId) ?? { count: 0, lastAt: null };
    const nextCount = stats.count + 1;
    const now = new Date().toISOString();
    this.recoveryStats.set(tenantId, { count: nextCount, lastAt: now });
    this.patchState(tenantId, {
      recoveryAttemptCount: nextCount,
      lastRecoveryAt: now,
      startupVerificationStatus: "recovering",
      startupVerificationError: reason,
      lastTransportError: reason,
      messageBusReady: false,
    });

    try {
      if (nextCount === 1) {
        await this.softRecoverProvider(tenantId);
      } else if (nextCount === 2) {
        await this.restartTenant(tenantId);
      } else {
        // A bad browser frame must never erase a linked WhatsApp session.
        // Leave the process available for an operator to inspect or relink.
        const message = "Automatic recovery stopped; stored WhatsApp session preserved for manual review";
        this.clearVendorMessagePolling(tenantId);
        this.clearVendorDisconnectVerification(tenantId);
        this.clearStartupVerification(tenantId);
        this.patchState(tenantId, {
          providerStatus: "error",
          sessionReady: false,
          messageBusReady: false,
          restartInProgress: false,
          qrRecoveryInProgress: false,
          startupVerificationStatus: "failed",
          startupVerificationError: message,
          lastError: message,
          lastTransportError: message,
        });
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown recovery error";
      this.patchState(tenantId, {
        startupVerificationStatus: "failed",
        startupVerificationError: message,
        lastTransportError: message,
      });
    } finally {
      this.recoveryLocks.delete(tenantId);
    }
  }

  private async softRecoverProvider(tenantId: string): Promise<void> {
    const provider = this.getProvider(tenantId);
    if (typeof provider.initVendor !== "function" || typeof provider.listenOnEvents !== "function") {
      throw new Error("Provider does not support soft recovery");
    }

    const vendor = await this.withTimeout(
      provider.initVendor(),
      PROVIDER_HEALTH_TIMEOUT_MS * 2,
      "Provider initVendor timed out during recovery"
    );
    const activeVendor = vendor as NonNullable<ProviderLike["vendor"]>;
    const messageBusReady = this.ensureProviderMessageBus(tenantId, provider, activeVendor);
    if (!messageBusReady) {
      throw new Error("Provider message bus could not be restored");
    }

    const verifiedAt = new Date().toISOString();
    this.patchState(tenantId, {
      status: "connected",
      sessionReady: true,
      messageBusReady,
      messageBusListenerCount: this.getMessageBusListenerCount(activeVendor),
      startupVerifiedAt: verifiedAt,
      startupVerificationStatus: "success",
      startupVerificationError: null,
      lastError: null,
      lastTransportError: null,
    });
  }

  private getProvider(tenantId: string): ProviderLike {
    const bot = this.manager.getBot(tenantId);
    const provider = bot?.provider as ProviderLike | undefined;

    if (!provider) {
      throw new Error(`Provider for "${tenantId}" is not available`);
    }

    return provider;
  }

  private isCurrentProvider(tenantId: string, provider: ProviderLike): boolean {
    const bot = this.manager.getBot(tenantId);
    return Boolean(bot?.provider && bot.provider === provider);
  }

  private async ensureHealthyVendor(provider: ProviderLike): Promise<NonNullable<ProviderLike["vendor"]>> {
    if (provider.vendor) {
      return provider.vendor;
    }

    if (typeof provider.initVendor !== "function") {
      throw new Error("Provider vendor is not initialized");
    }

    const vendor = await this.withTimeout(
      provider.initVendor(),
      PROVIDER_HEALTH_TIMEOUT_MS,
      "Provider vendor initialization timed out"
    );
    return vendor as NonNullable<ProviderLike["vendor"]>;
  }

  private getMessageBusListenerCount(vendor: ProviderLike["vendor"] | null | undefined): number | null {
    return this.getVendorListenerCount(vendor, "onMessage");
  }

  private getVendorListenerCount(
    vendor: ProviderLike["vendor"] | null | undefined,
    eventName: "onMessage" | "onAnyMessage"
  ): number | null {
    const listenerEmitter = vendor?.listenerEmitter;
    const listenerCount = listenerEmitter?.listenerCount;
    if (typeof listenerCount !== "function") {
      return null;
    }

    const count = Number(listenerCount.call(listenerEmitter, eventName));
    return Number.isFinite(count) ? Math.max(0, count) : null;
  }

  private hasMessageBus(vendor: NonNullable<ProviderLike["vendor"]>): boolean {
    if (typeof vendor.onMessage !== "function") {
      return false;
    }

    const listenerCount = this.getMessageBusListenerCount(vendor);
    return listenerCount === null || listenerCount > 0;
  }

  private ensureProviderMessageBus(
    tenantId: string,
    provider: ProviderLike,
    vendor: NonNullable<ProviderLike["vendor"]>
  ): boolean {
    let listenerCount = this.getMessageBusListenerCount(vendor);
    let rebound = false;

    if (listenerCount === 0 && typeof provider.listenOnEvents === "function") {
      provider.listenOnEvents(vendor);
      rebound = true;
      listenerCount = this.getMessageBusListenerCount(vendor);
    }

    const ready = typeof vendor.onMessage === "function" && (listenerCount === null || listenerCount > 0);
    this.patchState(tenantId, {
      messageBusReady: ready,
      messageBusListenerCount: listenerCount,
      startupVerificationError: ready ? null : "Vendor message listener is not bound",
    });
    console.log(
      `[gateway] message bus ${tenantId} ready=${ready} listeners=${listenerCount ?? "unknown"} rebound=${rebound}`
    );
    return ready;
  }

  private scheduleProviderMessageBusCheck(tenantId: string, provider: ProviderLike): void {
    setTimeout(() => {
      if (!this.isCurrentProvider(tenantId, provider)) {
        return;
      }

      const vendor = provider.vendor;
      if (!vendor) {
        this.patchState(tenantId, {
          messageBusReady: false,
          messageBusListenerCount: 0,
          startupVerificationError: "Provider vendor is not initialized",
        });
        return;
      }

      this.ensureProviderMessageBus(tenantId, provider, vendor);
    }, 0);
  }

  private async withTimeout<T>(promise: Promise<T>, timeoutMs: number, timeoutMessage: string): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | null = null;

    try {
      return await Promise.race([
        promise,
        new Promise<T>((_, reject) => {
          timer = setTimeout(() => reject(new Error(timeoutMessage)), timeoutMs);
        }),
      ]);
    } finally {
      if (timer) {
        clearTimeout(timer);
      }
    }
  }

  private async sleep(ms: number): Promise<void> {
    await new Promise((resolvePromise) => setTimeout(resolvePromise, ms));
  }

  private stringifyPayload(payload: unknown): string {
    if (typeof payload === "string") {
      return payload;
    }

    try {
      return JSON.stringify(payload);
    } catch {
      return "Unknown provider payload";
    }
  }

  private resolveBrowserExecutablePath(providerOptions?: Record<string, unknown>): string | undefined {
    const explicitOptions = providerOptions?.puppeteerOptions;
    if (explicitOptions && typeof explicitOptions === "object") {
      const explicitPath = (explicitOptions as Record<string, unknown>).executablePath;
      if (typeof explicitPath === "string" && explicitPath.trim()) {
        return explicitPath.trim();
      }
    }

    if (this.config.wppExecutablePath?.trim()) {
      return this.config.wppExecutablePath.trim();
    }

    for (const candidate of WINDOWS_BROWSER_CANDIDATES) {
      if (existsSync(candidate)) {
        return candidate;
      }
    }

    return undefined;
  }

  private toInboundPayload(tenantId: string, ctx: RawBotContext): N8nInboundPayload {
    const parsed = parseInboundMessage(ctx);
    // Only the provider's structured fields are trusted. Body/name/quoted text
    // cannot supply coordinates or resolve an opaque LID into a phone number.
    const latitude = ctx.lat, longitude = ctx.lng;
    const location = parsed.type === 'location' && typeof latitude === 'number' && Number.isFinite(latitude)
      && typeof longitude === 'number' && Number.isFinite(longitude) && Math.abs(latitude) <= 90 && Math.abs(longitude) <= 180
      ? { latitude, longitude } : undefined;
    const phone = this.extractRealContactPhone(ctx);
    return {
      tenantId,
      channel: "whatsapp",
      provider: "wppconnect",
      from: typeof ctx.from === "string" ? ctx.from : "",
      name: typeof ctx.name === "string" ? ctx.name : "",
      contact_display_name: providerContactDisplayName(ctx),
      ...(location ? { location } : {}),
      ...(phone ? { verified_phone: phone } : {}),
      message: {
        id: extractMessageId(ctx),
        type: parsed.type,
        text: parsed.text,
        timestamp: extractTimestamp(ctx),
      },
      context: {
        isGroup: isGroupMessage(ctx),
        hasMedia: parsed.hasMedia,
      },
      raw: this.toSerializableContext(ctx),
    };
  }

  private ignoreStickerInbound(tenantId: string, ctx: RawBotContext): boolean {
    if (!isStickerInbound(ctx)) return false;
    const messageId = extractMessageId(ctx);
    if (messageId) {
      // A listener may first report the same ID as incomplete generic media.
      // Cancel its notice and reserve the ID so later copies cannot send one.
      this.clearIncompleteMediaNotice(tenantId, messageId);
      this.processedMessageIds.isDuplicate(tenantId, messageId);
    }
    const payload = this.toInboundPayload(tenantId, ctx);
    this.recordIgnoredInbound(tenantId, "sticker", payload, ctx, extractProviderTimestampMs(ctx));
    return true;
  }

  private getInboundAudienceRejection(payload: N8nInboundPayload): string | null {
    const from = payload.from.trim().toLowerCase();
    if (!from) {
      return "missing_sender";
    }
    if (payload.context.isGroup || from.endsWith("@g.us")) {
      return "group_message";
    }
    if (from === "status@broadcast") {
      return "status_broadcast";
    }
    if (from.endsWith("@newsletter")) {
      return "newsletter_message";
    }
    if (from.endsWith("@broadcast")) {
      return "broadcast_message";
    }
    if (/^\d{6,20}(?:@(c\.us|lid|s\.whatsapp\.net))?$/.test(from)) {
      return null;
    }
    return "unsupported_sender";
  }

  private getConversationKey(tenantId: string, from: string): string {
    const phone = this.normalizeContactPhone(from);
    return `${tenantId}:${phone ? `phone:${phone}` : `wid:${from.trim().toLowerCase()}`}`;
  }

  private clearInboundBatches(tenantId: string): void {
    this.turnQueue.clear(tenantId);
    for (const [key, timer] of this.incompleteMediaNotices) {
      if (key.startsWith(tenantId + ':')) {
        clearTimeout(timer);
        this.incompleteMediaNotices.delete(key);
      }
    }
    this.patchState(tenantId, { pendingInboundBatchCount: 0 });
  }

  private clearIncompleteMediaNotice(tenantId: string, messageId: string): void {
    const key = `${tenantId}:${messageId}`;
    const timer = this.incompleteMediaNotices.get(key);
    if (timer) clearTimeout(timer);
    this.incompleteMediaNotices.delete(key);
  }

  private incompleteMediaNoticeDelayMs(): number {
    return Math.max(INCOMPLETE_MEDIA_NOTICE_GRACE_MS, this.config.messageBatchWindowMs);
  }

  private scheduleIncompleteMediaNotice(
    tenantId: string,
    messageId: string,
    from: string,
    provider: ProviderLike,
    type: N8nInboundPayload["message"]["type"]
  ): void {
    const key = `${tenantId}:${messageId}`;
    if (this.incompleteMediaNotices.has(key) || this.incompleteMediaNoticeIds.has(tenantId, messageId)) return;
    const timer = setTimeout(() => {
      this.incompleteMediaNotices.delete(key);
      if (this.shuttingDown || !this.isCurrentProvider(tenantId, provider)) return;
      if (this.processedMessageIds.has(tenantId, messageId)) return;
      // A failed or uncertain WhatsApp send must not trigger another notice on
      // the next polling copy of the same original message.
      if (this.incompleteMediaNoticeIds.isDuplicate(tenantId, messageId)) return;
      const text = type === "voice_note"
        ? "No pude abrir el audio. ¿Puedes enviarlo otra vez? 🙏"
        : type === "document"
          ? "No pude abrir el archivo. ¿Puedes enviarlo otra vez? 🙏"
          : "No pude abrir la imagen. ¿Puedes enviármela otra vez? 🙏";
      void this.sendProviderMessage(provider, from,
        { type: "text", text })
        .catch(error => console.error(`[gateway] media retry notice failed ${tenantId}: ${String(error)}`));
    }, this.incompleteMediaNoticeDelayMs());
    timer.unref?.();
    this.incompleteMediaNotices.set(key, timer);
  }

  private isOwnMessage(ctx: RawBotContext & { fromMe?: boolean; id?: unknown; key?: unknown }): boolean {
    if (ctx.fromMe === true || ctx.isSentByMe === true) {
      return true;
    }

    const key = typeof ctx.key === "object" && ctx.key !== null ? (ctx.key as Record<string, unknown>) : undefined;
    const id = typeof ctx.id === "object" && ctx.id !== null ? (ctx.id as Record<string, unknown>) : undefined;
    return key?.fromMe === true || id?.fromMe === true;
  }

  private previewText(value: string, maxLength = 140): string | null {
    const normalized = value.replace(/\s+/g, " ").trim();
    if (!normalized) {
      return null;
    }

    if (normalized.length <= maxLength) {
      return normalized;
    }

    return `${normalized.slice(0, Math.max(0, maxLength - 3))}...`;
  }

  private shouldIgnoreRequireAction(state: TenantState): boolean {
    return this.hasLiveSessionEvidence(state);
  }

  private shouldProcessInboundMessage(
    tenantId: string,
    ctx: RawBotContext,
    payload: N8nInboundPayload
  ): { accepted: true; timestampMs: number | null } | { accepted: false; reason: string; timestampMs: number | null } {
    this.maybeSettleSyncBarrier(tenantId);
    const state = this.requireState(tenantId);
    const { timestampMs, source: timestampSource } = extractProviderTimestampInfo(ctx);
    const acceptMessagesAfterMs = this.isoToMs(state.acceptMessagesAfter);
    const parsedMessage = {
      type: payload.message.type,
      text: payload.message.text,
      hasMedia: payload.context.hasMedia,
    };
    const isPlaceholder = isPlaceholderInbound(parsedMessage);
    const hasServerTimestamp = hasServerMessageTimestamp(ctx);
    const classification = classifyInboundEvent(ctx, parsedMessage);
    const hasTimestampAfterCutoff =
      hasServerTimestamp &&
      timestampMs !== null &&
      acceptMessagesAfterMs !== null &&
      timestampMs >= acceptMessagesAfterMs - MESSAGE_CUTOFF_SKEW_MS;

    if (classification === "sync_notification_event") {
      return { accepted: false, reason: "sync_notification_event", timestampMs };
    }

    if (classification === "empty_text_sync_event") {
      return { accepted: false, reason: "empty_text_sync_event", timestampMs };
    }

    if (acceptMessagesAfterMs === null) {
      return { accepted: false, reason: "missing_accept_cutoff", timestampMs };
    }

    if (hasServerTimestamp && timestampMs !== null && timestampMs < acceptMessagesAfterMs - MESSAGE_CUTOFF_SKEW_MS) {
      return {
        accepted: false,
        reason: isPlaceholder ? "historical_media_placeholder" : "timestamp_before_cutoff",
        timestampMs,
      };
    }

    if (state.syncBarrierActive) {
      if (this.isSyncNoiseClassification(classification)) {
        return { accepted: false, reason: classification, timestampMs };
      }

      if (classification === "fresh_text_candidate" && hasTimestampAfterCutoff) {
        return { accepted: true, timestampMs };
      }

      if (classification === "fresh_media_candidate" && hasTimestampAfterCutoff) {
        return { accepted: true, timestampMs };
      }

      if (timestampSource === "clientReceivedTsMillis") {
        return { accepted: false, reason: "client_received_only_timestamp", timestampMs };
      }

      if (isPlaceholder && !hasTimestampAfterCutoff) {
        return { accepted: false, reason: "missing_fresh_signal_for_media", timestampMs };
      }

      return { accepted: false, reason: "sync_barrier_active", timestampMs };
    }

    if (classification === "ambiguous_sync_event") {
      return { accepted: false, reason: "ambiguous_sync_event", timestampMs };
    }

    if (!hasServerTimestamp) {
      if (timestampSource === "clientReceivedTsMillis") {
        return { accepted: false, reason: "client_received_only_timestamp", timestampMs };
      }

      return { accepted: false, reason: "missing_server_timestamp", timestampMs };
    }

    if (!hasTimestampAfterCutoff) {
      return {
        accepted: false,
        reason: isPlaceholder ? "historical_media_placeholder" : "timestamp_before_cutoff",
        timestampMs,
      };
    }

    return { accepted: true, timestampMs };
  }

  private recordIgnoredInbound(
    tenantId: string,
    reason: string,
    payload: N8nInboundPayload,
    ctx: RawBotContext,
    timestampMs: number | null
  ): void {
    const state = this.getTenant(tenantId);
    const now = new Date().toISOString();
    const cutoff = state?.acceptMessagesAfter ?? null;
    const nextCount = (state?.ignoredInboundCount ?? 0) + 1;
    const { source: timestampSource } = extractProviderTimestampInfo(ctx);
    const eventType = extractProviderEventType(ctx);
    const subtype = extractProviderSubtype(ctx);
    const shouldExtendSyncBarrier = Boolean(state?.syncBarrierActive && this.isSyncNoiseReason(reason));
    this.patchState(tenantId, {
      lastIgnoredInboundAt: now,
      lastIgnoredReason: reason,
      ignoredInboundCount: nextCount,
      ...(shouldExtendSyncBarrier
        ? {
            syncBarrierStartedAt: state?.syncBarrierStartedAt ?? now,
            syncLastNoiseAt: now,
            syncSettledAt: null,
          }
        : {}),
    });
    if (shouldExtendSyncBarrier) {
      this.scheduleSyncBarrierSettlement(tenantId);
    }
    console.log(
      `[gateway] ignored inbound ${tenantId} type=${payload.message.type} providerType=${eventType ?? "null"} subtype=${subtype ?? "null"} reason=${reason} timestampMs=${timestampMs ?? "null"} timestampSource=${timestampSource ?? "null"} cutoff=${cutoff ?? "null"}`
    );
  }

  private bumpAcceptMessagesAfter(tenantId: string, isoTimestamp: string): string {
    const state = this.getTenant(tenantId);
    const current = this.isoToMs(state?.acceptMessagesAfter ?? null);
    const next = this.isoToMs(isoTimestamp);
    if (current !== null && next !== null && current > next) {
      return state?.acceptMessagesAfter ?? isoTimestamp;
    }
    return isoTimestamp;
  }

  private isSyncNoiseClassification(classification: string): boolean {
    return classification === "sync_notification_event" || classification === "empty_text_sync_event";
  }

  private isSyncNoiseReason(reason: string): boolean {
    return new Set([
      "sync_notification_event",
      "empty_text_sync_event",
      "historical_media_placeholder",
      "ambiguous_sync_event",
      "client_received_only_timestamp",
      "missing_server_timestamp",
      "missing_server_timestamp_for_media",
      "missing_fresh_signal_for_media",
      "timestamp_before_cutoff",
    ]).has(reason);
  }

  private maybeSettleSyncBarrier(tenantId: string): void {
    const state = this.getTenant(tenantId);
    if (!state?.syncBarrierActive) {
      return;
    }

    const referenceMs = this.isoToMs(state.syncLastNoiseAt) ?? this.isoToMs(state.syncBarrierStartedAt);
    if (referenceMs === null) {
      return;
    }

    if (Date.now() - referenceMs < SYNC_BARRIER_SETTLE_MS) {
      return;
    }

    const settledAt = new Date().toISOString();
    const settledAtMs = this.isoToMs(settledAt) ?? Date.now();
    this.clearSyncBarrierSettlement(tenantId);
    this.vendorMessagePollCursors.set(tenantId, settledAtMs);
    this.patchState(tenantId, {
      syncBarrierActive: false,
      syncSettledAt: settledAt,
      pollingMessageBusCursorAt: new Date(settledAtMs).toISOString(),
      lastIgnoredReason: "sync_settled",
    });
    console.log(`[gateway] sync settled ${tenantId} at=${settledAt}`);
  }

  private startSyncBarrierIfMissing(tenantId: string): void {
    const state = this.getTenant(tenantId);
    if (!state?.sessionReady || state.syncBarrierActive || state.syncSettledAt) return;
    const now = new Date().toISOString();
    this.patchState(tenantId, {
      acceptMessagesAfter: this.bumpAcceptMessagesAfter(tenantId, now),
      syncBarrierActive: true,
      syncBarrierStartedAt: now,
      syncLastNoiseAt: now,
      syncSettledAt: null,
    });
    this.scheduleSyncBarrierSettlement(tenantId);
  }

  private isoToMs(value: string | null | undefined): number | null {
    if (!value) {
      return null;
    }

    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  private hasLiveSessionEvidence(state: TenantState): boolean {
    if (
      state.providerStatus === "disconnected" ||
      state.providerStatus === "restarting" ||
      state.providerStatus === "disconnecting" ||
      state.disconnectPendingQr ||
      state.disconnectInProgress ||
      state.restartInProgress ||
      state.qrRecoveryInProgress
    ) {
      return false;
    }

    const hostPhone = typeof state.host?.phone === "string" ? state.host.phone.trim() : "";
    return Boolean(
      state.sessionReady ||
      state.lastMessageEventAt ||
      state.lastInboundAt ||
      state.lastOutboundAt ||
      hostPhone
    );
  }

  private deriveState(state: TenantState): TenantState {
    const hasLiveSessionEvidence = this.hasLiveSessionEvidence(state);
    const providerStatus = state.providerStatus ?? "unknown";
    const hasInitError = typeof state.lastError === "string" && state.lastError.includes("WPP initialization");
    const hasTransportError = Boolean(state.lastTransportError);
    const hasDetachedRuntimeFailure =
      (state.pollingMessageBusConsecutiveErrors ?? 0) >= VENDOR_MESSAGE_POLL_FAILURE_THRESHOLD &&
      this.isDetachedVendorRuntimeError(state.pollingMessageBusError ?? "");
    const hasCurrentQr = this.hasCurrentQr(state);
    const isQrRecovery = Boolean(
      state.disconnectPendingQr ||
        state.disconnectInProgress ||
        state.qrRecoveryInProgress ||
        state.restartInProgress ||
        (!hasLiveSessionEvidence &&
          providerStatus === "auth_failure" &&
          state.autoRestartReason &&
          state.lastDisconnectAt)
    );

    let effectiveStatus: TenantStatus;
    if (hasCurrentQr) {
      effectiveStatus = "awaiting_qr";
    } else if (isQrRecovery || providerStatus === "restarting" || providerStatus === "disconnecting") {
      effectiveStatus = "disconnected";
    } else if (providerStatus === "disconnected") {
      effectiveStatus = hasCurrentQr ? "awaiting_qr" : "disconnected";
    } else if (hasLiveSessionEvidence) {
      effectiveStatus =
        providerStatus === "auth_failure" ||
        hasInitError ||
        hasTransportError ||
        state.messageBusReady === false ||
        hasDetachedRuntimeFailure
          ? "degraded"
          : "connected";
    } else if (providerStatus === "auth_failure") {
      effectiveStatus = "error";
    } else if (providerStatus === "ready" || providerStatus === "initializing") {
      effectiveStatus = "initializing";
    } else {
      effectiveStatus = state.status ?? "initializing";
    }

    return {
      ...state,
      status: effectiveStatus,
      effectiveStatus,
      hasLiveSessionEvidence,
      isOperational:
        effectiveStatus === "connected" &&
        state.messageBusReady === true &&
        !hasDetachedRuntimeFailure &&
        !state.disconnectPendingQr &&
        !state.restartInProgress,
      qr: effectiveStatus === "awaiting_qr" && hasCurrentQr ? state.qr ?? null : null,
    };
  }

  private hasCurrentQr(state?: TenantState | null): boolean {
    if (!state?.qr || state.qrRecoveryInProgress) {
      return false;
    }

    const qrUpdatedAtMs = this.isoToMs(state.qrUpdatedAt);
    const qrInvalidatedAtMs = this.isoToMs(state.qrInvalidatedAt);
    if (qrUpdatedAtMs === null) {
      return false;
    }

    return qrInvalidatedAtMs === null || qrUpdatedAtMs > qrInvalidatedAtMs;
  }

  private nextQrGeneration(tenantId: string, nextQr: string): number {
    const state = this.getTenant(tenantId);
    if (!state) {
      return 1;
    }

    if (state.qr === nextQr && state.qrGeneration) {
      return state.qrGeneration;
    }

    return (state.qrGeneration ?? 0) + 1;
  }
}
