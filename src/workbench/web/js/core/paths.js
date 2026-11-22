// 路径显示：项目目录下的路径显示为相对路径。projects_root 由 /api/config 设置。
const state = {root: ''};

export function setProjectsRoot(root) { state.root = (root || '').replace(/\/+$/, ''); }

export function relPath(p) {
  p = p || '';
  return state.root && p.startsWith(state.root + '/') ? p.slice(state.root.length + 1) : p;
}
