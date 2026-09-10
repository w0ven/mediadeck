/**
 * dialog.js - MediaDeck 全局模态弹窗系统
 * 对齐 FoamV2 / 硬玻璃视觉风格：
 * - 遮罩高斯模糊、半透明深色玻璃卡片、微光边框、青色高亮、圆角胶囊按钮
 * - 提供 deckAlert(message)、deckConfirm(message)、deckPrompt(message, defaultValue)
 * - 全部返回 Promise（confirm->boolean, prompt->string|null, 取消为 false/null）
 * - 支持 Esc 关闭、Tab 陷阱、点击遮罩取消、恢复前序焦点、单例排队/同时仅开一个
 */
(function (root) {
  let activeDialog = null;

  function createDialog(type, message, defaultValue = '') {
    return new Promise((resolve) => {
      // 若当前已有弹窗打开，先关闭并 resolve 之前的（安全兜底）
      if (activeDialog) {
        activeDialog.close(type === 'confirm' ? false : null);
      }

      const priorFocus = document.activeElement;

      // 遮罩容器
      const overlay = document.createElement('div');
      overlay.className = 'deck-dialog-backdrop';
      overlay.setAttribute('role', 'presentation');

      // 对话框卡片
      const card = document.createElement('div');
      card.className = 'deck-dialog-card';
      card.setAttribute('role', 'dialog');
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
      icon.textContent = type === 'alert' ? 'ℹ' : (type === 'confirm' ? '?' : '✎');
      
      const title = document.createElement('span');
      title.className = 'deck-dialog-title';
      title.textContent = type === 'alert' ? '提示' : (type === 'confirm' ? '确认操作' : '请输入');

      head.appendChild(icon);
      head.appendChild(title);

      // 消息体
      const body = document.createElement('div');
      body.className = 'deck-dialog-body';
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
        inputEl = document.createElement('input');
        inputEl.type = 'text';
        inputEl.className = 'deck-dialog-input';
        inputEl.value = defaultValue != null ? String(defaultValue) : '';
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
        cancelBtn.textContent = '取消';
        actions.appendChild(cancelBtn);
      }

      const confirmBtn = document.createElement('button');
      confirmBtn.type = 'button';
      confirmBtn.className = 'deck-dialog-btn deck-dialog-btn-confirm';
      confirmBtn.textContent = type === 'alert' ? '我知道了' : '确认';
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

      // 初次焦点分配
      requestAnimationFrame(() => {
        if (inputEl) {
          inputEl.focus();
          inputEl.select();
        } else if (confirmBtn) {
          confirmBtn.focus();
        } else {
          card.focus();
        }
      });
    });
  }

  root.deckAlert = function (message) {
    return createDialog('alert', message);
  };

  root.deckConfirm = function (message) {
    return createDialog('confirm', message);
  };

  root.deckPrompt = function (message, defaultValue) {
    return createDialog('prompt', message, defaultValue);
  };
})(typeof window !== 'undefined' ? window : this);
