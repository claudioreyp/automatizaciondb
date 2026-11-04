type TypingVendor = {
  startTyping?: (to: string, duration?: number) => Promise<unknown>;
  stopTyping?: (to: string) => Promise<unknown>;
};

export function beginTyping(vendor: TypingVendor | undefined, recipient: string): () => Promise<void> {
  let stopped = false;
  let pending: Promise<unknown> = Promise.resolve();
  let stopping: Promise<void> | undefined;
  const refresh = () => {
    pending = pending.then(async () => {
      // WPPConnect awaits the full duration when one is supplied. We stop explicitly.
      if (!stopped) await vendor?.startTyping?.(recipient);
    }).catch(() => undefined);
  };
  refresh();
  const timer = setInterval(refresh, 10000);
  timer.unref?.();
  return () => {
    if (stopping) return stopping;
    stopped = true;
    clearInterval(timer);
    stopping = (async () => {
      await pending;
      try { await vendor?.stopTyping?.(recipient); } catch { /* Presence must not fail a real reply. */ }
    })();
    return stopping;
  };
}

export async function whileTyping<T>(vendor: TypingVendor | undefined, recipient: string, work: () => Promise<T>): Promise<T> {
  const stop = beginTyping(vendor, recipient);
  try {
    return await work();
  } finally {
    // No refresh survives into the outbound send/ACK phase or a failed/suppressed reply.
    await stop();
  }
}
