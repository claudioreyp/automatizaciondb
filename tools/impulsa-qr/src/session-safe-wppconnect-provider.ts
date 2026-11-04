import { writeFileSync } from "fs";
import { resolve } from "path";

import * as BuilderbotWppConnect from "@builderbot/provider-wppconnect";
import * as wppconnect from "@wppconnect-team/wppconnect";

type CreateVendor = (options: Record<string, unknown>) => Promise<any>;
type SaveQr = (name: string, image: string) => void;

const ProviderBase = (BuilderbotWppConnect as any).WPPConnectProvider as new (
  args: Record<string, unknown>
) => any;

const saveQrImage: SaveQr = (name, image) => {
  const match = /^data:image\/png;base64,([A-Za-z0-9+/=]+)$/.exec(image);
  if (!match) return;
  writeFileSync(resolve(process.cwd(), `${name}.qr.png`), Buffer.from(match[1], "base64"));
};

// The upstream provider empties <name>_sessions and retries forever when
// initVendor fails. A detached browser frame must leave the linked session on
// disk so the gateway can make bounded, observable recovery attempts.
export function sessionSafeProviderClass(
  createVendor: CreateVendor = wppconnect.create as unknown as CreateVendor,
  saveQr: SaveQr = saveQrImage,
) {
  return class SessionSafeWPPConnectProvider extends ProviderBase {
    listenOnEvents(vendor: any): void {
      // BuilderBot's initAll calls this even when initVendor could not start.
      // Keep its HTTP server available for health and QR status inspection.
      if (vendor) super.listenOnEvents(vendor);
    }

    async initVendor(): Promise<any> {
      const args = this.globalVendorArgs as Record<string, unknown>;
      const name = String(args.name || "bot");
      const session = `${name}_sessions`;
      try {
        const vendor = await createVendor({
          session,
          catchQR: (qr: string) => {
            this.emit("require_action", {
              title: "Vincular WhatsApp",
              instructions: ["Escanea el código QR con WhatsApp"],
              payload: { qr },
            });
            try { saveQr(name, qr); }
            catch (error) { console.error("[gateway] Could not save QR image", error); }
          },
          puppeteerOptions: { headless: true, args: ["--no-sandbox"] },
          ...args,
          folderNameToken: session,
        });
        this.vendor = vendor;
        const wid = await vendor.getWid();
        this.emit("ready", true);
        this.emit("host", { phone: String(wid).split("@")[0] });
        return vendor;
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        this.vendor = undefined;
        this.emit("auth_failure", { instructions: ["No se pudo iniciar WhatsApp"], reason });
        return undefined;
      }
    }
  };
}

export const SessionSafeWPPConnectProvider = sessionSafeProviderClass();
