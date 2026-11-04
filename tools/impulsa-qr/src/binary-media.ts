import type { BinaryMediaAction } from "./types.js";

export const MAX_OUTBOUND_IMAGE_BYTES = 8 * 1024 * 1024;

export function validateBinaryMedia(value: Record<string, unknown>): BinaryMediaAction {
  const { data, mimeType } = value;
  if (typeof data !== "string" || data.length > Math.ceil(MAX_OUTBOUND_IMAGE_BYTES / 3) * 4
      || data.length % 4 !== 0 || /[^A-Za-z0-9+/=]/.test(data)) {
    throw new Error("Invalid outbound image data");
  }
  const padding = data.indexOf('=');
  if (padding !== -1 && (padding < data.length - 2 || data.slice(padding) !== '='.repeat(data.length - padding))) {
    throw new Error("Invalid outbound image padding");
  }
  const bytes = Buffer.from(data, "base64");
  const png = bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
  const jpeg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
  const webp = bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP";
  if (!bytes.length || bytes.length > MAX_OUTBOUND_IMAGE_BYTES || bytes.toString('base64') !== data
      || !(mimeType === "image/png" && png || mimeType === "image/jpeg" && jpeg || mimeType === "image/webp" && webp)) {
    throw new Error("Unsupported outbound image format");
  }
  return { type: "media_binary", data, mimeType: mimeType as BinaryMediaAction["mimeType"],
    caption: typeof value.caption === "string" ? value.caption.slice(0, 1024) : undefined };
}
