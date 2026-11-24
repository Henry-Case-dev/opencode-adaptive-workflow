/** MCP Events V2 bus adapter. It accepts a notification as an untrusted *signal* only.
 * No payload is ever executed, forwarded as a prompt, or accepted as an approval.
 */
import crypto from 'node:crypto';
const MAX_SEEN=96;
const SAFE_TOKEN=/^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$/;
export function safeMcpEventSources(rows){
  if(!Array.isArray(rows))return [];
  const seen=new Set(), accepted=[];
  for(const row of rows.slice(0,32)){
    if(!row || typeof row !== 'object' ||
       typeof row.server !== 'string' || typeof row.name !== 'string' ||
       !SAFE_TOKEN.test(row.server) || !SAFE_TOKEN.test(row.name))continue;
    const key=`${row.server}\0${row.name}`;
    if(seen.has(key))continue;
    seen.add(key);accepted.push({server:row.server,name:row.name});
  }
  return accepted;
}
export function acceptMcpEventSignal(event, sources, observed=[]) {
  if(event?.type!=='experimental.mcp.event')return {ok:false,reason:'wrong-type'};
  const p=event.properties??event.data??{};
  if(!p || typeof p!=='object' || Array.isArray(p) ||
     typeof p.server!=='string'||typeof p.name!=='string'||
     typeof p.eventId!=='string'||!p.eventId||p.eventId.length>1024)
    return {ok:false,reason:'invalid-event'};
  if(!Array.isArray(sources) || !sources.some(x=>x.server===p.server && x.name===p.name))
    return {ok:false,reason:'not-allowlisted'};
  // Event IDs are upstream-controlled. Hash them rather than persisting payloads
  // or raw identifiers potentially containing private resource information.
  const key=crypto.createHash('sha256').update(JSON.stringify([p.server,p.name,p.eventId])).digest('hex');
  const history=Array.isArray(observed)?observed.filter(x=>typeof x==='string'&&/^[a-f0-9]{64}$/.test(x)).slice(-MAX_SEEN):[];
  if(history.includes(key))return {ok:false,reason:'duplicate'};
  return {ok:true,seen:[...history,key].slice(-MAX_SEEN),server:p.server,name:p.name,truncated:p.truncated===true};
}
