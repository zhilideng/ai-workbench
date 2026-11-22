// macOS 原生能力（pywebview 注入的 window.pywebview.api）。在普通浏览器里调试时不存在，调用方需降级。

export const isNative = () => !!(window.pywebview && window.pywebview.api);

// pywebview 的 API 在页面加载后异步注入
export function nativeReady() {
  if (isNative()) return Promise.resolve(true);
  return new Promise(resolve => {
    const done = () => resolve(isNative());
    window.addEventListener('pywebviewready', done, {once: true});
    setTimeout(done, 3000);
  });
}

export async function call(method, ...args) {
  if (!(await nativeReady())) return null;
  return window.pywebview.api[method](...args);
}
