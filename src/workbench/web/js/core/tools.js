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

// 工具调用的完整内容（展开后显示）：改文件显示改前 / 改后的全文，其余显示完整参数
export function toolDetail(name, inp = {}) {
  const file = inp.file_path || inp.notebook_path || '';
  if (name === 'Edit') return `${file}${inp.replace_all ? '（全部替换）' : ''}\n\n改前：\n${inp.old_string ?? ''}\n\n改后：\n${inp.new_string ?? ''}`;
  if (name === 'MultiEdit') return file + '\n\n' + (inp.edits || []).map((e, i) => `#${i + 1} 改前：\n${e.old_string ?? ''}\n\n#${i + 1} 改后：\n${e.new_string ?? ''}`).join('\n\n');
  if (name === 'Write') return `${file}\n\n${inp.content ?? ''}`;
  if (name === 'NotebookEdit') return `${file}${inp.cell_id ? ' · ' + inp.cell_id : ''}\n\n${inp.new_source ?? ''}`;
  if (name === 'Bash') return (inp.description ? inp.description + '\n\n' : '') + (inp.command || '');
  return JSON.stringify(inp, null, 2);
}
