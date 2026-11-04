import { loadConfig } from "./config.js";
import { GatewayApi } from "./api.js";
import { ImpulsaGateway } from "./gateway.js";
import { PosEventRelay } from "./pos-event-relay.js";
import { startRestaurantSink, restaurantSinkConfiguration } from "./restaurant-qr-sink.js";

const main = async () => {
  const config = loadConfig();
  // Validate explicit opt-in before starting a provider. Default starts no sink.
  restaurantSinkConfiguration(process.env);
  const gateway = new ImpulsaGateway(config);
  const posEventRelay = new PosEventRelay(gateway, config);

  await gateway.bootstrap();
  posEventRelay.start();

  const api = new GatewayApi(gateway, config.apiKey);
  api.start(config.port);
  const restaurantSink = await startRestaurantSink(gateway, process.env);

  const shutdown = async () => {
    posEventRelay.stop();
    api.stop();
    await restaurantSink?.close();
    await gateway.shutdown();
  };

  process.on("SIGINT", () => {
    shutdown()
      .catch((error) => console.error("Shutdown failed", error))
      .finally(() => process.exit(0));
  });

  process.on("SIGTERM", () => {
    shutdown()
      .catch((error) => console.error("Shutdown failed", error))
      .finally(() => process.exit(0));
  });

  console.log(`Default tenant: ${config.defaultTenantId}`);
  console.log(`n8n webhook: ${config.n8nWebhookUrl}`);
  console.log(`POS event relay: ${posEventRelay.enabled ? "enabled" : "disabled"}`);
};

main().catch((error) => {
  console.error("Failed to start Impulsa gateway", error);
  process.exit(1);
});
