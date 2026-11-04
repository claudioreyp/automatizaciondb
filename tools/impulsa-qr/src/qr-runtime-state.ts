/** Public metadata only: configured activation is distinct from a live transport. */
export function qrRuntimeState(pairingStatus: string, transport: { enabled: boolean; status: string }) {
  const attention = transport.enabled && ["REVIEW_REQUIRED", "CLOSE_UNCONFIRMED"].includes(transport.status);
  const connected = pairingStatus === "connected";
  const active = transport.enabled && connected && ["BOUND", "IDLE", "ACK_DURABLE"].includes(transport.status);
  return {
    mode: transport.enabled ? "native_agent" : "direct_pairing_only",
    consumerMessaging: active ? "active" : attention ? "attention_required" : "paused",
    n8nForwarding: active,
    connected,
    requiresAttention: attention,
  };
}
