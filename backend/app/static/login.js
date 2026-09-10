/**
 * login.js - MediaDeck 登录逻辑
 * - 捕获表单提交
 * - 向 POST /api/auth/login 发送 JSON {"username", "password"}
 * - 成功 {ok:true, user} 跳转至 location.href = '/'
 * - 失败时在卡片内部友好显示服务端 detail 或默认文案，严禁 alert
 * - 纯独立运行，不依赖 app.js
 */
(function () {
  const form = document.getElementById('login-form');
  const usernameInput = document.getElementById('username');
  const passwordInput = document.getElementById('password');
  const submitBtn = document.getElementById('submit-btn');
  const feedbackEl = document.getElementById('login-feedback');
  const feedbackTextEl = document.getElementById('feedback-text');

  if (!form || !usernameInput || !passwordInput || !submitBtn) return;

  function showError(msg) {
    if (feedbackTextEl) feedbackTextEl.textContent = msg;
    if (feedbackEl) feedbackEl.classList.add('active');
  }

  function clearError() {
    if (feedbackEl) feedbackEl.classList.remove('active');
  }

  function setLoading(loading) {
    submitBtn.disabled = loading;
    submitBtn.classList.toggle('loading', loading);
    usernameInput.disabled = loading;
    passwordInput.disabled = loading;
  }

  // 监听输入实时清除报错
  usernameInput.addEventListener('input', clearError);
  passwordInput.addEventListener('input', clearError);

  form.addEventListener('submit', async function (e) {
    e.preventDefault();
    clearError();

    const username = usernameInput.value.trim();
    const password = passwordInput.value;

    if (!username) {
      showError('请输入用户名');
      usernameInput.focus();
      return;
    }

    if (!password) {
      showError('请输入密码');
      passwordInput.focus();
      return;
    }

    setLoading(true);

    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify({ username, password }),
      });

      let data = null;
      try {
        data = await resp.json();
      } catch (_) {
        // 非 JSON 响应兜底
      }

      if (resp.ok && data && (data.ok === true || data.user)) {
        // 登录成功，跳转根路径面板
        location.href = '/';
        return;
      }

      // 错误处理：提取服务端 detail 或默认文案
      const errorMsg = (data && (data.detail || data.message || data.error)) ||
        (resp.status === 401 ? '用户名或密码错误' : `登录失败 (${resp.status})`);

      showError(errorMsg);
      setLoading(false);
      passwordInput.focus();
      passwordInput.select();
    } catch (err) {
      showError('网络连接失败，请检查网络或稍后再试');
      setLoading(false);
    }
  });
})();
