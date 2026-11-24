import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// Import the tool contract from the installed runtime, so Cordis identities and
// the plugin API always match that installation. No copied runtime dependencies.
const runtime = process.env.CODEX_VOICE_DSH_RUNTIME;
if (!runtime) throw new Error('CODEX_VOICE_DSH_RUNTIME is required');
const { defineTool } = await import(pathToFileURL(path.join(runtime, 'node_modules/@deepseek-ai/dsh-tools/lib/index.js')).href);

export const name = 'codex-voice-notification';
export const inject = ['tools'];
export function apply(ctx) {
  ctx.tools.register(defineTool({
    name: 'speak_notification',
    description: '为给定的 Codex 事件提交一句中文语音提醒。调用一次后结束；本机执行器会再次核实事件，并调用火山 TTS 播放。',
    parameters: {
      event_id: {type: 'string', required: true, description: '用户消息中提供的 notification_id，必须原样使用'},
      text: {type: 'string', required: true, description: '4到100字符的中文自然语言，目标30到60字，包含称呼、任务和需要用户处理的事项'},
    },
    output: {schema: {type: 'string'}, render: (_args, value) => [{type: 'text', text: value}]},
    async execute(args) {
      if (args.event_id !== process.env.CODEX_VOICE_NOTIFICATION_ID) throw new Error('Unknown notification ID');
      const text = args.text.trim();
      if (text.length < 4 || text.length > 100 || /https?:|[<>\x00]|```/.test(text)) throw new Error('Invalid plain-language speech');
      const output = process.env.CODEX_VOICE_OUTPUT;
      if (!output) throw new Error('No notification output destination');
      // Exclusive creation prevents duplicate model calls from duplicating speech.
      try { fs.writeFileSync(output, JSON.stringify({event_id: args.event_id, text}), {flag:'wx', mode:0o600}); }
      catch (error) { if (error.code !== 'EEXIST') throw error; }
      return '语音提醒已提交本机队列。请结束本轮，不要重复调用。';
    },
  }));
  ctx.on('agent/created', ({agent}) => {
    // Scope the actual Agent, including profiles with home-level additions.
    // Future runtime tools cannot silently become capabilities of this agent.
    agent.ctx.tools.restrict({allow: ['speak_notification']});
    const tools = ctx.tools.definitions(agent).map(tool => tool.name).sort();
    if (tools.length !== 1 || tools[0] !== 'speak_notification') throw new Error('Unexpected notification capabilities');
    fs.writeFileSync(process.env.CODEX_VOICE_OUTPUT + '.tools.json', JSON.stringify(tools), {mode:0o600});
  });
}
