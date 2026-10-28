import { Plugin } from '@opencode/plugin';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { startGuard } from './runtime.mjs';
const pluginDir=path.dirname(fileURLToPath(import.meta.url));
export default Plugin.define({
  id:'workflow.continuation-guard',
  async setup(ctx){
    const guard=await startGuard(ctx,{pluginDir});
    return await guard.init();
  },
});
