/* 我的任务清单 —— 原生 JavaScript 实现（无任何依赖）
 *
 * 数据流：
 *   localStorage --loadTasks()--> tasks(内存) --render()--> DOM
 *   DOM 事件 --> 修改 tasks --> saveTasks() --> localStorage
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'personal-task-list.v1';
  var FILTERS = ['all', 'active', 'done'];
  var ERROR_TIMEOUT = 3000;

  var els = {
    form: document.getElementById('task-form'),
    input: document.getElementById('task-input'),
    error: document.getElementById('form-error'),
    list: document.getElementById('task-list'),
    empty: document.getElementById('empty-state'),
    count: document.getElementById('task-count'),
    clear: document.getElementById('clear-completed'),
    filters: document.querySelectorAll('[data-filter]')
  };

  /** @type {{id: string, text: string, done: boolean, createdAt: number}[]} */
  var tasks = [];
  var filter = 'all';
  var errorTimer = 0;

  /* ---------------------------- 数据层 ---------------------------- */

  function createId() {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
    return 't-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  }

  /** 把任意来源的一条记录整理成合法任务，非法则返回 null。 */
  function normalizeTask(raw) {
    if (!raw || typeof raw !== 'object') return null;
    var text = typeof raw.text === 'string' ? raw.text.trim() : '';
    if (!text) return null;
    return {
      id: typeof raw.id === 'string' && raw.id ? raw.id : createId(),
      text: text,
      done: raw.done === true,
      createdAt: Number.isFinite(raw.createdAt) ? raw.createdAt : Date.now()
    };
  }

  function loadTasks() {
    var raw = null;
    try {
      raw = window.localStorage.getItem(STORAGE_KEY);
    } catch (err) {
      // 隐私模式等场景下 localStorage 可能不可用，此时降级为「仅当前页面有效」。
      console.warn('[任务清单] 无法读取本地存储，本次改动不会被保存：', err);
      return [];
    }
    if (!raw) return [];

    try {
      var parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed.map(normalizeTask).filter(Boolean);
    } catch (err) {
      console.warn('[任务清单] 本地数据已损坏，已忽略：', err);
      return [];
    }
  }

  function saveTasks() {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(tasks));
    } catch (err) {
      console.warn('[任务清单] 保存失败：', err);
      showError('保存失败，数据可能无法在刷新后保留。');
    }
  }

  /* ---------------------------- 操作层 ---------------------------- */

  function addTask(text) {
    var value = String(text == null ? '' : text).trim();
    if (!value) {
      showError('任务内容不能为空。');
      return false;
    }
    tasks.push({ id: createId(), text: value, done: false, createdAt: Date.now() });
    hideError();
    saveTasks();
    render();
    return true;
  }

  function toggleTask(id) {
    var task = findTask(id);
    if (!task) return;
    task.done = !task.done;
    saveTasks();
    render();
  }

  function removeTask(id) {
    var next = tasks.filter(function (task) { return task.id !== id; });
    if (next.length === tasks.length) return;
    tasks = next;
    saveTasks();
    render();
  }

  function clearCompleted() {
    var next = tasks.filter(function (task) { return !task.done; });
    if (next.length === tasks.length) return;
    tasks = next;
    saveTasks();
    render();
  }

  function setFilter(next) {
    if (FILTERS.indexOf(next) === -1) return;
    filter = next;
    render();
  }

  function findTask(id) {
    for (var i = 0; i < tasks.length; i++) {
      if (tasks[i].id === id) return tasks[i];
    }
    return null;
  }

  function visibleTasks() {
    if (filter === 'active') {
      return tasks.filter(function (task) { return !task.done; });
    }
    if (filter === 'done') {
      return tasks.filter(function (task) { return task.done; });
    }
    return tasks.slice();
  }

  /* ---------------------------- 视图层 ---------------------------- */

  function createTaskNode(task) {
    var li = document.createElement('li');
    li.className = 'task';
    li.dataset.id = task.id;
    if (task.done) li.classList.add('is-done');

    var checkboxId = 'task-check-' + task.id;

    var checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'task__checkbox';
    checkbox.id = checkboxId;
    checkbox.checked = task.done;
    checkbox.setAttribute('aria-label', task.done ? '标记为未完成' : '标记为已完成');
    checkbox.addEventListener('change', function () { toggleTask(task.id); });

    var label = document.createElement('label');
    label.className = 'task__text';
    label.setAttribute('for', checkboxId);
    label.textContent = task.text; // 使用 textContent，天然避免 XSS

    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'task__delete';
    remove.textContent = '删除';
    remove.setAttribute('aria-label', '删除任务：' + task.text);
    remove.addEventListener('click', function () { removeTask(task.id); });

    li.appendChild(checkbox);
    li.appendChild(label);
    li.appendChild(remove);
    return li;
  }

  function emptyMessage() {
    if (tasks.length === 0) return '还没有任务，先添加一条吧。';
    if (filter === 'active') return '没有待办任务，全部完成啦 🎉';
    if (filter === 'done') return '还没有已完成的任务。';
    return '没有可显示的任务。';
  }

  function render() {
    var items = visibleTasks();

    // 用文档片段重建列表，保证 DOM 与数据完全一致。
    var fragment = document.createDocumentFragment();
    items.forEach(function (task) { fragment.appendChild(createTaskNode(task)); });
    els.list.replaceChildren(fragment);

    els.empty.hidden = items.length > 0;
    els.empty.textContent = emptyMessage();

    var remaining = 0;
    tasks.forEach(function (task) { if (!task.done) remaining++; });
    var completed = tasks.length - remaining;
    els.count.textContent = '共 ' + tasks.length + ' 项 · 待办 ' + remaining + ' 项 · 已完成 ' + completed + ' 项';

    els.clear.disabled = completed === 0;

    els.filters.forEach(function (button) {
      var isActive = button.dataset.filter === filter;
      button.classList.toggle('is-active', isActive);
      button.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });
  }

  function showError(message) {
    els.error.textContent = message;
    els.error.hidden = false;
    window.clearTimeout(errorTimer);
    errorTimer = window.setTimeout(hideError, ERROR_TIMEOUT);
  }

  function hideError() {
    window.clearTimeout(errorTimer);
    els.error.hidden = true;
    els.error.textContent = '';
  }

  /* ---------------------------- 事件绑定 ---------------------------- */

  els.form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (addTask(els.input.value)) {
      els.input.value = '';
    }
    els.input.focus();
  });

  els.filters.forEach(function (button) {
    button.addEventListener('click', function () { setFilter(button.dataset.filter); });
  });

  els.clear.addEventListener('click', clearCompleted);

  /* ---------------------------- 启动 ---------------------------- */

  tasks = loadTasks();
  render();
})();
