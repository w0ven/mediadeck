/**
 * dialog.js - MediaDeck 全局模态弹窗系统
 * 对齐 FoamV2 / 硬玻璃视觉风格：
 * - 遮罩高斯模糊、半透明深色玻璃卡片、微光边框、青色高亮、圆角胶囊按钮
 * - 提供 deckAlert(message)、deckConfirm(message, options)、deckPrompt(message, defaultValue, options)
 * - 全部返回 Promise（confirm->boolean, prompt->string|null, 取消为 false/null）
 * - 支持 Esc 关闭、Tab 陷阱、点击遮罩取消、恢复前序焦点、单例排队/同时仅开一个
 *
 * options（均可省略，省略时与旧调用完全一致）：
 * - title        标题，默认「确认操作」/「请输入」
 * - confirmText  确认按钮文字，默认「确认」
 * - cancelText   取消按钮文字，默认「取消」
 * - danger       危险操作：确认按钮使用警示色，初始焦点落在「取消」上，避免回车误触；
 *                未指定时，内容含「不可恢复 / 无法恢复」的确认按危险操作处理
 * - choices      deckPrompt 专用：[[value, label], ...]，以下拉框代替自由输入
 * - placeholder  deckPrompt 输入框占位文字
 */
(function (root) {
  let activeDialog = null;

  function createDialog(type, message, defaultValue = '', options = {}) {
    options = options || {};
    return new Promise((resolve) => {
      // 若当前已有弹窗打开，先关闭并 resolve 之前的（安全兜底）
      if (activeDialog) {
        activeDialog.close(type === 'confirm' ? false : null);
      }

      const priorFocus = document.activeElement;
      // Callers mark destructive confirmations explicitly; an irreversible
      // message ("不可恢复") is treated as one even from older call sites.
      const danger = options.danger !== undefined ? !!options.danger
        : (type === 'confirm' && /不可恢复|无法恢复/.test(String(message || '')));

      // 遮罩容器
      const overlay = document.createElement('div');
      overlay.className = 'deck-dialog-backdrop';
      overlay.setAttribute('role', 'presentation');

      // 对话框卡片
      const card = document.createElement('div');
      card.className = 'deck-dialog-card' + (danger ? ' is-danger' : '');
      card.setAttribute('role', danger ? 'alertdialog' : 'dialog');
      card.setAttribute('aria-modal', 'true');
      card.tabIndex = -1;

      // 顶部发光微条
      const glowBar = document.createElement('div');
      glowBar.className = 'deck-dialog-glow';

      // 头部装饰
      const head = document.createElement('div');
      head.className = 'deck-dialog-head';
      const icon = document.createElement('span');
      icon.className = 'deck-dialog-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = danger ? '!' : (type === 'alert' ? 'ℹ' : (type === 'confirm' ? '?' : '✎'));

      const title = document.createElement('span');
      title.className = 'deck-dialog-title';
      title.id = 'deck-dialog-title';
      title.textContent = options.title || (type === 'alert' ? '提示' : (type === 'confirm' ? '确认操作' : '请输入'));
      card.setAttribute('aria-labelledby', title.id);

      head.appendChild(icon);
      head.appendChild(title);

      // 消息体
      const body = document.createElement('div');
      body.className = 'deck-dialog-body';
      body.id = 'deck-dialog-body';
      card.setAttribute('aria-describedby', body.id);
      // 将换行符转为段落或 br
      const lines = String(message != null ? message : '').split('\n');
      lines.forEach((line, idx) => {
        if (idx > 0) body.appendChild(document.createElement('br'));
        body.appendChild(document.createTextNode(line));
      });

      // 输入框（prompt 专属）
      let inputEl = null;
      if (type === 'prompt') {
        const inputWrap = document.createElement('div');
        inputWrap.className = 'deck-dialog-input-wrap';
        if (Array.isArray(options.choices) && options.choices.length) {
          inputEl = document.createElement('select');
          options.choices.forEach(([value, label]) => {
            const option = document.createElement('option');
            option.value = String(value);
            option.textContent = String(label == null ? value : label);
            inputEl.appendChild(option);
          });
          if (defaultValue != null && options.choices.some(([value]) => String(value) === String(defaultValue))) {
            inputEl.value = String(defaultValue);
          }
        } else {
          inputEl = document.createElement('input');
          inputEl.type = 'text';
          inputEl.value = defaultValue != null ? String(defaultValue) : '';
          if (options.placeholder) inputEl.placeholder = options.placeholder;
        }
        inputEl.className = 'deck-dialog-input';
        inputEl.setAttribute('aria-label', options.title || String(message || '').split('\n')[0] || '请输入');
        inputWrap.appendChild(inputEl);
        body.appendChild(inputWrap);
      }

      // 底部操作区
      const actions = document.createElement('div');
      actions.className = 'deck-dialog-actions';

      let cancelBtn = null;
      if (type !== 'alert') {
        cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.className = 'deck-dialog-btn deck-dialog-btn-cancel';
        cancelBtn.textContent = options.cancelText || '取消';
        actions.appendChild(cancelBtn);
      }

      const confirmBtn = document.createElement('button');
      confirmBtn.type = 'button';
      confirmBtn.className = 'deck-dialog-btn deck-dialog-btn-confirm' + (danger ? ' is-danger' : '');
      confirmBtn.textContent = options.confirmText || (type === 'alert' ? '我知道了' : '确认');
      actions.appendChild(confirmBtn);

      card.appendChild(glowBar);
      card.appendChild(head);
      card.appendChild(body);
      card.appendChild(actions);
      overlay.appendChild(card);

      document.body.appendChild(overlay);

      let closed = false;
      function cleanup(resultValue) {
        if (closed) return;
        closed = true;
        document.removeEventListener('keydown', onKeyDown, true);
        overlay.classList.add('deck-dialog-closing');
        setTimeout(() => {
          overlay.remove();
          if (priorFocus && typeof priorFocus.focus === 'function' && priorFocus.isConnected) {
            try { priorFocus.focus(); } catch (_) {}
          }
        }, 150);
        if (activeDialog === dialogRecord) {
          activeDialog = null;
        }
        resolve(resultValue);
      }

      const dialogRecord = {
        close: cleanup,
      };
      activeDialog = dialogRecord;

      // 事件绑定
      if (cancelBtn) {
        cancelBtn.addEventListener('click', () => cleanup(type === 'confirm' ? false : null));
      }

      confirmBtn.addEventListener('click', () => {
        if (type === 'prompt') {
          cleanup(inputEl ? inputEl.value : '');
        } else if (type === 'confirm') {
          cleanup(true);
        } else {
          cleanup(undefined);
        }
      });

      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) {
          cleanup(type === 'confirm' ? false : null);
        }
      });

      function getFocusables() {
        return [...card.querySelectorAll('button, input, select, textarea, [tabindex]:not([tabindex="-1"])')]
          .filter(el => !el.disabled && el.offsetParent !== null);
      }

      function onKeyDown(e) {
        if (e.key === 'Escape') {
          e.preventDefault();
          e.stopPropagation();
          cleanup(type === 'confirm' ? false : null);
          return;
        }

        if (e.key === 'Enter' && type === 'prompt' && document.activeElement === inputEl) {
          e.preventDefault();
          e.stopPropagation();
          cleanup(inputEl ? inputEl.value : '');
          return;
        }

        if (e.key === 'Tab') {
          const list = getFocusables();
          if (!list.length) {
            e.preventDefault();
            card.focus();
            return;
          }
          const first = list[0];
          const last = list[list.length - 1];
          if (!card.contains(document.activeElement)) {
            e.preventDefault();
            first.focus();
            return;
          }
          if (e.shiftKey && document.activeElement === first) {
            e.preventDefault();
            last.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }

      document.addEventListener('keydown', onKeyDown, true);

      // 初次焦点分配：危险确认默认停在「取消」，回车不会直接执行
      requestAnimationFrame(() => {
        if (inputEl) {
          inputEl.focus();
          if (typeof inputEl.select === 'function') inputEl.select();
        } else if (danger && cancelBtn) {
          cancelBtn.focus();
        } else if (confirmBtn) {
          confirmBtn.focus();
        } else {
          card.focus();
        }
      });
    });
  }

  root.deckAlert = function (message, options) {
    return createDialog('alert', message, '', options);
  };

  root.deckConfirm = function (message, options) {
    return createDialog('confirm', message, '', options);
  };

  root.deckPrompt = function (message, defaultValue, options) {
    return createDialog('prompt', message, defaultValue, options);
  };
})(typeof window !== 'undefined' ? window : this);
