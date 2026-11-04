import {createHash} from 'node:crypto';
import {buildAckTargetFromMetadata,canonicalJSON} from './ack-target-tools.mjs';

/** The client snapshot comes from the private in-memory owner, never a request. */
export function buildAckReferenceFromClientSnapshot({snapshot,config,delivery_id}){
  if(snapshot?.status!=='OPEN'||!snapshot.metadata||!Number.isSafeInteger(snapshot.epoch))throw Error('ACK_REFERENCE_CLIENT_SNAPSHOT');
  const body=JSON.parse(canonicalJSON(snapshot.metadata));
  const matches=body.deliveries?.filter(r=>r.delivery_id===delivery_id);
  if(!Array.isArray(matches)||matches.length!==1)throw Error('ACK_REFERENCE_CLIENT_DELIVERY');
  const r=matches[0];
  return buildAckTargetFromMetadata({metadata:{...body,checksum:createHash('sha256').update(canonicalJSON(body)).digest('hex')},
    config,sender:r.sender,message_id:r.message_id,delivery_id});
}
