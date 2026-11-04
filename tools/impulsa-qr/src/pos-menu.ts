import { MAX_OUTBOUND_IMAGE_BYTES, validateBinaryMedia } from "./binary-media.js";
import type { GatewayAction, GatewayConfig } from "./types.js";

export async function* currentMenuImages(config: GatewayConfig): AsyncGenerator<GatewayAction> {
  const headers = { Authorization: "Bearer " + config.posEventToken };
  const request = (path: string) => fetch(config.posEventApiBase + path, {
    headers, redirect: "error", signal: AbortSignal.timeout(15000),
  });
  let images: { id: string }[];
  try {
    if (!config.posEventApiBase || !config.posEventToken) throw Error("Missing POS configuration");
    const response = await request("/integrations/context");
    if (!response.ok) throw Error("Context unavailable");
    const context = await response.json() as { business?: { id: number }; branch?: { id: number; menu_images: { id: string }[] } };
    if (context.business?.id !== config.posBusinessId || context.branch?.id !== config.posBranchId) throw Error("Scope mismatch");
    images = context.branch!.menu_images;
    if (!Array.isArray(images) || images.length > 10 || images.some(image => !/^[\w-]{1,80}$/.test(image.id))) throw Error("Invalid gallery");
  } catch {
    yield { type: "text", text: "No pude consultar la carta en este momento. Puedes pedirme los platos disponibles mientras lo revisamos. 🙏" };
    return;
  }
  if (!images.length) {
    yield { type: "text", text: "Todavía no tenemos imágenes de la carta cargadas. Puedo ayudarte a elegir entre los platos disponibles. 🍕" };
    return;
  }
  for (const [index, image] of images.entries()) {
    let action: GatewayAction;
    try {
      const response = await request("/integrations/context/menu-cards/" + encodeURIComponent(image.id));
      if (!response.ok || !response.body) throw Error("Image unavailable");
      const parts: Uint8Array[] = [];
      const reader = response.body.getReader();
      let length = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        length += value.length;
        if (length > MAX_OUTBOUND_IMAGE_BYTES) { await reader.cancel(); throw Error("Oversized image"); }
        parts.push(value);
      }
      action = validateBinaryMedia({ data: Buffer.concat(parts).toString("base64"),
        mimeType: response.headers.get("content-type")?.split(";")[0],
        caption: index === 0 ? "Aquí está nuestra carta 🙌" : undefined });
    } catch {
      yield { type: "text", text: "No pude cargar todas las imágenes de la carta en este momento. Puedo ayudarte a consultar los productos. 🙏" };
      return;
    }
    // The next image is fetched only after the consumer confirms this send.
    yield action;
  }
}
