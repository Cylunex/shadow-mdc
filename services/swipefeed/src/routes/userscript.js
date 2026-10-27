/**
 * 会话采集油猴脚本（由 /api/session/userscript 动态返回）。
 *
 * 作用：在目标站页面上一键把「Cookie + 指定 localStorage 键」POST 回本机 swipefeed，
 * 之后服务端带着这份会话去取源（hstv 的 uu1d、caixiang 的 token、海角的登录态等）。
 *
 * 注意：脚本里不含任何站点逻辑，只做“读本地存储 + 发一个 POST”。
 */
export function buildSessionUserscript({ origin = 'http://127.0.0.1:8787', keys = [] } = {}) {
  const keyList = JSON.stringify(keys.length ? keys : [
    'uu1d', 'uuid', 'theLink', 'deviceInfo', 'token', 'session', 'user', 'userInfo',
    'history', 'vip', 'isVip', 'member', 'access_token', 'refresh_token',
  ]);
  const base = origin.replace(/\/$/, '');
  return `// ==UserScript==
// @name         Swipefeed 会话采集（本机）
// @namespace    swipefeed.session
// @version      1.0.0
// @description  把当前站点的 Cookie / localStorage 会话发回本机 swipefeed，用于服务端取源
// @match        *://*/*
// @grant        GM_cookie
// @grant        GM_xmlhttpRequest
// @grant        GM_registerMenuCommand
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';
  var TARGET = '${base}';
  var KEYS = ${keyList};

  function collectStorage() {
    var out = [];
    for (var i = 0; i < KEYS.length; i++) {
      try {
        var v = localStorage.getItem(KEYS[i]);
        if (v != null) out.push({ key: KEYS[i], value: String(v) });
      } catch (e) {}
    }
    // 兜底：把看起来像会话的键也带上（长度受限）
    try {
      for (var j = 0; j < localStorage.length && out.length < 40; j++) {
        var k = localStorage.key(j);
        if (!k || KEYS.indexOf(k) >= 0) continue;
        if (!/token|uuid|session|user|auth|vip|member|history|device/i.test(k)) continue;
        out.push({ key: k, value: String(localStorage.getItem(k) || '').slice(0, 4000) });
      }
    } catch (e) {}
    return out;
  }

  function post(payload) {
    return new Promise(function (resolve) {
      if (typeof GM_xmlhttpRequest === 'function') {
        GM_xmlhttpRequest({
          method: 'POST',
          url: TARGET + '/api/session',
          headers: { 'Content-Type': 'application/json' },
          data: JSON.stringify(payload),
          onload: function (r) { resolve(r.status); },
          onerror: function () { resolve(0); },
        });
      } else {
        fetch(TARGET + '/api/session', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }).then(function (r) { resolve(r.status); }).catch(function () { resolve(0); });
      }
    });
  }

  function toast(msg) {
    var el = document.createElement('div');
    el.textContent = msg;
    el.style.cssText = 'position:fixed;z-index:2147483647;left:50%;top:16px;transform:translateX(-50%);' +
      'background:rgba(0,0,0,.82);color:#fff;padding:8px 14px;border-radius:8px;font-size:13px;' +
      'font-family:system-ui,sans-serif;pointer-events:none';
    document.documentElement.appendChild(el);
    setTimeout(function () { el.remove(); }, 2600);
  }

  function exportSession() {
    var payload = {
      host: location.hostname,
      cookie: document.cookie || '',
      storage: collectStorage(),
      referer: location.href,
      userAgent: navigator.userAgent,
      source: 'userscript',
      note: 'captured at ' + location.pathname,
    };
    // 有 GM_cookie 就顺便拿 httpOnly（普通 document.cookie 读不到）
    if (typeof GM_cookie === 'function' && GM_cookie.list) {
      GM_cookie.list({ domain: location.hostname }, function (cookies, err) {
        if (!err && Array.isArray(cookies) && cookies.length) {
          var joined = cookies.map(function (c) { return c.name + '=' + c.value; }).join('; ');
          if (joined) payload.cookie = (payload.cookie ? payload.cookie + '; ' : '') + joined;
        }
        post(payload).then(function (code) {
          toast(code >= 200 && code < 300
            ? '已发送会话到 swipefeed：' + location.hostname
            : '发送失败（HTTP ' + code + '），确认本机服务在 ' + TARGET);
        });
      });
    } else {
      post(payload).then(function (code) {
        toast(code >= 200 && code < 300
          ? '已发送会话到 swipefeed：' + location.hostname
          : '发送失败（HTTP ' + code + '），确认本机服务在 ' + TARGET);
      });
    }
  }

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('⬆ 导出会话到 swipefeed', exportSession);
  }
  // 也挂一个快捷键：Ctrl+Alt+S
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey && e.altKey && (e.key === 's' || e.key === 'S')) {
      e.preventDefault();
      exportSession();
    }
  }, true);
})();
`;
}
