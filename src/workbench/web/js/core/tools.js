// 工具调用的一行摘要（对话里折叠显示）
import {relPath} from './paths.js';

export function toolSummary(name, inp = {}) {
  if (inp.command) return inp.command.slice(0, 200);
  if (inp.file_path) return relPath(inp.file_path);
  if (inp.pattern) return `"${inp.pattern}"${inp.path ? ' in ' + inp.path : ''}`;
  if (inp.url) return inp.url;
  if (inp.description) return inp.description;
  if (inp.todos) return inp.todos.map(t => (t.status === 'completed' ? '☑' : '☐') + ' ' + t.content).join('  ');
  return JSON.stringify(inp).slice(0, 160);
}
