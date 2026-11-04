export function qrImageReply(snapshot: { status: string; qr?: string | null; qrGeneration: number }, generation: string | null) {
  if (generation !== null && !/^(0|[1-9][0-9]{0,8})$/.test(generation)) return { status: 400, bytes: null };
  if (snapshot.status !== 'awaiting_qr' || !snapshot.qr) return { status: 404, bytes: null };
  if (generation !== null && Number(generation) !== snapshot.qrGeneration) return { status: 409, bytes: null };
  const match = /^data:image\/png;base64,([A-Za-z0-9+/=]+)$/.exec(snapshot.qr);
  if (!match) return { status: 503, bytes: null };
  const bytes = Buffer.from(match[1], 'base64');
  if (bytes.length > 512 * 1024 || !bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return { status: 503, bytes: null };
  return { status: 200, bytes };
}
