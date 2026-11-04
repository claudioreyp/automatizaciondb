// Harness scheduling only. No server guard, cursor, CAS or API semantics change.
export const ACK_RECOVERY_QUIET_MS = 15000;
const need=(ok,code)=>{if(!ok)throw Error(code);};
export async function recoverAckWithinOriginalLimits({row,limits,spec,control,wait,now,deadline,fence,spacingMs=1000}) {
  need(row&&Array.isArray(spec?.complete_statuses)&&Array.isArray(spec.idle_statuses),'ACK_STATUSES_REQUIRED');
  need(Number.isSafeInteger(limits?.ack_ticks)&&limits.ack_ticks>0&&limits.ack_ticks<=3
    &&Number.isSafeInteger(limits.total_ms)&&limits.total_ms>0&&limits.total_ms<=600000,'ACK_ORIGINAL_LIMITS_REQUIRED');
  need(Number.isSafeInteger(spacingMs)&&spacingMs>=0&&spacingMs<=60000,'ACK_SPACING_BOUND');
  need([control,wait,now,fence].every(fn=>typeof fn==='function')&&Number.isSafeInteger(deadline),'ACK_HARNESS_PORTS_REQUIRED');
  const begin=now();row.ack_ticks=[];row.ack_waits=[];
  let firstIdleReceivedAt=null;
  for(let index=0;index<limits.ack_ticks;index++) {
    fence();
    // ACK1 may itself durably apply Mailbox SENT to the source journal. Its
    // response is therefore the earliest safe baseline for the quiet interval.
    // Starting the interval at socket ACK receipt was insufficient in R08.
    if(index>0) {
      const quietUntil=firstIdleReceivedAt+ACK_RECOVERY_QUIET_MS;
      const delay=index===1?Math.max(spacingMs,quietUntil-now()):spacingMs;
      need(now()+delay<deadline,'ACK_QUIET_WAIT_WOULD_EXCEED_TOTAL_LIMIT');
      const start=now();if(delay>0)await wait(delay);fence();
      row.ack_waits.push({before_tick:index+1,requested_ms:delay,elapsed_ms:now()-start,
        reason:index===1?'journal_quiet_after_bootstrap_ack_response':'bounded_cursor_next_page'});
      if(index===1)need(now()>=quietUntil,'ACK_QUIET_WAIT_INCOMPLETE');
    }
    const tick=await control('ack');row.ack_ticks.push(tick);
    if(spec.complete_statuses.includes(tick.value.status)){row.API_ACK_complete=true;break;}
    need(spec.idle_statuses.includes(tick.value.status),'NONRECOVERABLE_ACK_STATUS');
    if(index===0)firstIdleReceivedAt=now();
    // An empty page is not a terminal failure: the existing server selector
    // resets its per-state cursor to0. A remaining declared tick may inspect
    // the first page; no extra tick or direct cursor mutation is introduced.
  }
  row.latency.api_ack_ms=now()-begin;row.latency.full_turn_ms=now()-row.started_at_ms;
  need(row.API_ACK_complete===true,'API_ACK_NOT_COMPLETE');return row;
}
