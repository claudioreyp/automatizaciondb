// Shared POS notice rules. Pure: no credentials, I/O, timers or provider.
export type PosEvent = { id: string; event_type: string; aggregate_id?: string | null; customer_phone?: string | null;
  whatsapp_chat_id?: string | null; payload?: Record<string, unknown> | null; created_at: string };
export type DeliveryFeeNotification = { quote_id: string | null; delivery_fee: number; currency: string; cash_available: boolean;
  payment_request: { id: string; purpose: string; method: string; status: string; amount: number;
    payable_amount: number; version: number; quote_id: string | null } | null };
export type OrderStatus = { id?: number; business_id?: number; branch_id?: number; folio?: number; channel?: string; status?: string;
  customer_phone?: string | null; whatsapp_chat_id?: string | null; delivery_fee?: number; delivery_fee_status?: string;
  delivery_quote_id?: string | null; delivery_fee_notification?: DeliveryFeeNotification };
const FEE_EVENT = "order.delivery_fee_confirmed";
const TYPES = ["payment.approved", "order.ready", "order.dispatched", "order.addition_committed", FEE_EVENT];
const parseUtc = (value: string) => Date.parse(/(?:Z|[+-]\d\d:\d\d)$/i.test(value) ? value : value + "Z");
const amount = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value) && value >= 0;
const originalApprovalWithoutRelease = (event: PosEvent): boolean => {
  const payload = event.payload;
  return event.event_type === "payment.approved" && payload?.purpose === "initial"
    && payload.payment_method === "yape" && payload.payment_approved === true
    && payload.sent_to_kitchen === false && payload.kitchen_release === "previously_released"
    && payload.original_payment_method_change === true
    && Number.isSafeInteger(payload.payment_id) && Number(payload.payment_id) > 0
    && Number.isSafeInteger(payload.evidence_id) && Number(payload.evidence_id) > 0
    && Array.isArray(payload.ticket_ids) && payload.ticket_ids.length === 0;
};
export { TYPES as POS_NOTICE_TYPES, parseUtc as parseNoticeUtc, originalApprovalWithoutRelease };
export function noticeEventKey(event: PosEvent): string {
    const key = `${event.aggregate_id || event.payload?.order_id}:${event.event_type}`;
    if (event.event_type === FEE_EVENT) return key + ":" + JSON.stringify([event.payload?.quote_id, event.payload?.payment_request_id]);
    // Independent additional payments share an order, but each approved request
    // commits a different product batch and needs its own notice.
    return ["payment.approved", "order.addition_committed"].includes(event.event_type) && event.payload?.purpose === "addition"
      ? key + ":addition:" + String(event.payload.payment_request_id || event.id)
      : key;
  }
export function noticeRecipient(value: string): string | null {
    if (/^\d{8,20}@lid$/.test(value)) return value;
    if (/@(?:g\.us|newsletter|broadcast)/i.test(value)) return null;
    const phone = value.replace(/@(?:c\.us|s\.whatsapp\.net)$/i, "").replace(/^\+/, "");
    return /^\d{8,15}$/.test(phone) ? phone : null;
  }
function currentDeliveryFee(event: PosEvent, order: OrderStatus, recipient: string): boolean {
    const payload = event.payload || {}, fee = order.delivery_fee_notification, request = fee?.payment_request;
    if (order.channel !== "delivery" || ["delivered", "closed", "cancelled"].includes(order.status || "")
        || order.delivery_fee_status !== "final") return false;
    // Missing/malformed projections must remain pending for investigation. They
    // are not evidence that a valid notice became stale.
    if (!fee || !request || typeof fee.cash_available !== "boolean" || !/^[A-Z]{3}$/.test(fee.currency)
        || !amount(fee.delivery_fee) || !amount(request.amount) || !amount(request.payable_amount)
        || request.payable_amount > request.amount || !request.id || request.purpose !== "delivery"
        || !Number.isSafeInteger(request.version) || request.version < 1
        || !["cash", "yape", "unselected"].includes(request.method)
        || !["pending", "under_review", "paid", "waived", "rejected", "requires_review", "superseded", "cancelled", "declined"].includes(request.status)
        || !amount(payload.delivery_fee) || !amount(payload.request_amount) || typeof payload.payment_request_id !== "string"
        || !Number.isSafeInteger(payload.request_version) || Number(payload.request_version) < 1
        || !(payload.quote_id === null || typeof payload.quote_id === "string")
        || !(fee.quote_id === null || typeof fee.quote_id === "string")
        || !(request.quote_id === null || typeof request.quote_id === "string")) throw Error("Invalid delivery fee notification");
    const currentRecipient = noticeRecipient(order.whatsapp_chat_id || "") || noticeRecipient(order.customer_phone || "");
    if (currentRecipient !== recipient) throw Error("Delivery fee recipient changed");
    if (["superseded", "cancelled", "declined"].includes(request.status)) return false;
    return payload.quote_id === fee.quote_id && fee.quote_id === order.delivery_quote_id
      && request.quote_id === fee.quote_id && payload.payment_request_id === request.id
      && payload.currency === fee.currency && payload.delivery_fee === fee.delivery_fee
      && order.delivery_fee === fee.delivery_fee && payload.request_amount === request.amount
      && Number(payload.request_version) <= request.version;
  }
function noticeMessage(event: PosEvent, order: OrderStatus): string {
    const reference = Number.isSafeInteger(order.folio) && order.folio! > 0 ? " #" + order.folio : "";
    const subject = "Tu pedido" + reference;
    if (event.event_type === FEE_EVENT) {
      const fee = order.delivery_fee_notification!, request = fee.payment_request!;
      const money = (value: number) => value.toFixed(2) + " " + fee.currency;
      const quoted = "El envío de tu pedido" + reference + " quedó cotizado en " + money(fee.delivery_fee) + ".";
      if (request.status === "under_review") return quoted + " El comprobante del envío sigue en revisión; no repitas el pago.";
      if (["rejected", "requires_review"].includes(request.status)) return quoted + " El pago del envío requiere revisión del encargado; no repitas un pago sin verificarlo.";
      if (request.status === "paid" || request.status === "waived" || request.payable_amount === 0) {
        return quoted + " No hay un importe adicional del envío pendiente de pago.";
      }
      const balance = request.payable_amount !== fee.delivery_fee
        ? " El importe pendiente del envío es " + money(request.payable_amount) + "; se conservan los pagos anteriores." : "";
      if (request.method === "yape") return quoted + balance + " El pago del envío por Yape sigue pendiente de comprobante y revisión del encargado.";
      return quoted + balance + (fee.cash_available ? " Puedes pagarlo directamente al repartidor al llegar."
        : " Falta coordinar el pago del envío con el encargado.");
    }
    if (event.event_type === "order.addition_committed") return "Los productos adicionales de tu pedido" + reference
      + " ya se incorporaron y su comanda fue enviada a cocina. El importe adicional se cobrará en efectivo.";
    if (originalApprovalWithoutRelease(event)) return "¡Pago confirmado! El pago de tu pedido" + reference + " fue aprobado por el encargado.";
    if (event.event_type === "payment.approved") return event.payload?.purpose === "addition"
      ? "¡Pago confirmado! Agregamos los productos adicionales a tu pedido" + reference + " y enviamos su nueva comanda a cocina. "
        + (order.status === "ready"
            ? order.channel === "delivery" ? "Todo está listo; estamos coordinando el reparto." : "Todo está listo para recoger en el local."
            : "Te avisaremos cuando todo esté listo.")
        : "¡Pago confirmado! La comanda de tu pedido" + reference + " fue enviada a cocina. "
          + (order.status === "preparing" ? "Ya está en preparación. " : "")
          + (order.channel === "delivery" ? "Te avisaremos cuando esté listo." : "Te avisaremos cuando esté listo para recoger.");
    const messages: Record<string, string> = {
      "order.ready": order.channel === "delivery"
          ? subject + " ya está preparado. Estamos coordinando el reparto; te avisaremos cuando salga."
          : subject + " ya está listo para recoger en el local.",
      "order.dispatched": subject + " ya salió con nuestro repartidor y va en camino. 🛵",
    };
    return messages[event.event_type];
  }
export type PosNoticePlan =
  | { kind: 'pending'; reason: string }
  | { kind: 'skip'; eventId: string }
  | { kind: 'notice'; eventId: string; orderId: number; recipient: string; message: string; confirmationKey?: string };

// Called only with the configured scope and API-authenticated event/status.
// This projection does not authorize delivery or acknowledge the source event.
export function projectPosNotice(input: {
  businessId: number; branchId: number; activatedAt: string;
  event: PosEvent; order: OrderStatus; superseded: boolean;
}): PosNoticePlan {
  const { businessId, branchId, activatedAt, event, order, superseded } = input;
  if (![businessId, branchId].every(x => Number.isSafeInteger(x) && x > 0)
      || !Number.isFinite(parseUtc(activatedAt)) || typeof superseded !== 'boolean') throw Error('Invalid POS notice boundary');
  if (!event || typeof event.id !== 'string' || !/^[A-Za-z0-9_.:-]{1,200}$/.test(event.id)
      || !TYPES.includes(event.event_type) || typeof event.created_at !== 'string'
      || !Number.isFinite(parseUtc(event.created_at)) || parseUtc(event.created_at) < parseUtc(activatedAt)) {
    return { kind: 'pending', reason: 'unsupported_or_historical_event' };
  }
  const recipient = noticeRecipient(event.whatsapp_chat_id || '') || noticeRecipient(event.customer_phone || '');
  if (!recipient) return { kind: 'pending', reason: 'direct_recipient_required' };
  const orderId = String(event.payload?.order_id || event.aggregate_id || '');
  if (!/^[1-9]\d*$/.test(orderId) || !Number.isSafeInteger(Number(orderId))
      || (event.aggregate_id && event.aggregate_id !== orderId)) throw Error('Invalid event order identity');
  if (!order || order.id !== Number(orderId) || order.business_id !== businessId || order.branch_id !== branchId) throw Error('POS event scope mismatch');
  const currentRecipient = noticeRecipient(order.whatsapp_chat_id || '') || noticeRecipient(order.customer_phone || '');
  if (currentRecipient !== recipient) throw Error('POS notice recipient changed');
  const approvalWithoutRelease = originalApprovalWithoutRelease(event);
  if (event.event_type === 'payment.approved' && event.payload?.sent_to_kitchen !== true && !approvalWithoutRelease) {
    return { kind: 'pending', reason: 'kitchen_release_or_original_approval_required' };
  }
  if (event.event_type === 'order.addition_committed' && (event.payload?.sent_to_kitchen !== true
      || event.payload?.purpose !== 'addition' || event.payload?.payment_method !== 'cash'
      || !event.payload?.payment_request_id || !Array.isArray(event.payload?.ticket_ids) || !event.payload.ticket_ids.length)) {
    return { kind: 'pending', reason: 'committed_cash_addition_required' };
  }
  const current = event.event_type === FEE_EVENT ? currentDeliveryFee(event, order, recipient)
      : approvalWithoutRelease ? ["confirmed", "sent_to_kitchen", "preparing", "ready", "dispatched", "delivered"].includes(order.status || "")
      : ["payment.approved", "order.addition_committed"].includes(event.event_type)
      ? ["confirmed", "sent_to_kitchen", "preparing", ...(event.payload?.purpose === "addition" ? ["ready"] : [])].includes(order.status || "")
      : event.event_type === "order.ready"
        ? order.status === "ready"
        : order.status === "dispatched" && order.channel === "delivery";
  if (superseded || !current) return { kind: 'skip', eventId: event.id };
  const confirmationKey = approvalWithoutRelease
      ? JSON.stringify(["payment.approved", order.id, event.payload!.payment_id])
      : event.event_type === FEE_EVENT
        ? JSON.stringify([order.id, event.payload?.quote_id, event.payload?.payment_request_id,
          event.payload?.delivery_fee, event.payload?.request_amount]) : undefined;
  return { kind: 'notice', eventId: event.id, orderId: Number(orderId), recipient,
    message: noticeMessage(event, order), ...(confirmationKey ? { confirmationKey } : {}) };
}
