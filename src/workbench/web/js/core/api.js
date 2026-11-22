// 本地 API 客户端。会话 Cookie 由窗口首次加载时写入，这里不需要额外处理鉴权。

async function request(url, opt = {}) {
  const r = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...opt,
                              body: opt.body ? JSON.stringify(opt.body) : undefined});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}

// 出错时弹窗提示（用户操作）
export async function api(url, opt = {}) {
  try { return await request(url, opt); }
  catch (e) { alert(e.message); throw e; }
}

// 出错时只抛异常，由调用方显示（弹窗里的表单、后台刷新）
export const apiq = request;

// 上传图片：请求体为文件原始字节，返回 {path, url, name, size}
export async function upload(file) {
  const r = await fetch('/api/uploads?name=' + encodeURIComponent(file.name || 'image.png'),
                        {method: 'POST', headers: {'Content-Type': file.type}, body: file});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || r.statusText);
  return j;
}
