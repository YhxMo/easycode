/**
 * 功能检查脚本（零依赖，node tests/dom-check.mjs）
 *
 * 说明：本脚本不重写业务逻辑，而是
 *   1) 解析项目里真实的 index.html 构建 DOM；
 *   2) 在 Node 的 vm 沙箱中执行未经修改的 app.js；
 *   3) 用最小的 DOM / localStorage 实现模拟浏览器行为（点击、勾选、提交表单、刷新）。
 * 因此断言覆盖的是「真实页面结构 + 真实脚本 + 真实存储读写序列」。
 *
 * 覆盖：新增任务、完成/取消完成、全部/待办/已完成筛选、刷新后 localStorage 持久化、
 *       删除单条、清除已完成、空输入校验、XSS 防护、损坏数据与存储不可用的容错。
 */

import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const STORAGE_KEY = 'personal-task-list.v1';
const INDEX_HTML = await readFile(path.join(ROOT, 'index.html'), 'utf8');
const APP_SOURCE = await readFile(path.join(ROOT, 'app.js'), 'utf8');

/* ============================ 极简 DOM 实现 ============================ */

const VOID_TAGS = new Set(['meta', 'link', 'br', 'input', 'img', 'hr', 'source', 'area', 'base', 'col', 'embed', 'param', 'track', 'wbr']);

function camelToKebab(name) {
  return name.replace(/[A-Z]/g, (c) => '-' + c.toLowerCase());
}

class ClassList {
  constructor(el) { this.el = el; }
  _list() {
    const raw = this.el._attrs.get('class') || '';
    return raw.split(/\s+/).filter(Boolean);
  }
  _write(list) { this.el._attrs.set('class', list.join(' ')); }
  add(...names) { const l = this._list(); names.forEach((n) => { if (!l.includes(n)) l.push(n); }); this._write(l); }
  remove(...names) { this._write(this._list().filter((n) => !names.includes(n))); }
  contains(name) { return this._list().includes(name); }
  toggle(name, force) {
    const has = this.contains(name);
    const next = force === undefined ? !has : Boolean(force);
    if (next && !has) this.add(name);
    if (!next && has) this.remove(name);
    return next;
  }
  get value() { return this._list().join(' '); }
  toString() { return this.value; }
}

class Event {
  constructor(type) {
    this.type = type;
    this.target = null;
    this.cancelable = true;
    this.defaultPrevented = false;
  }
  preventDefault() {
    this.defaultPrevented = true;
  }
}

class TextNode {
  constructor(data) {
    this.nodeType = 3;
    this.data = data;
    this.parentNode = null;
  }
  get textContent() { return this.data; }
  set textContent(value) { this.data = String(value); }
}

class Element {
  constructor(tagName, ownerDocument) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this._attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this._listeners = new Map();
    this._classList = new ClassList(this);
  }

  /* --- 属性 --- */
  get classList() { return this._classList; }
  get className() { return this._attrs.get('class') || ''; }
  set className(value) { this._attrs.set('class', String(value)); }
  get id() { return this._attrs.get('id') || ''; }
  set id(value) { this._attrs.set('id', String(value)); }
  get type() { return this._attrs.get('type') || ''; }
  set type(value) { this._attrs.set('type', String(value)); }
  get value() { return this._attrs.has('value') ? this._attrs.get('value') : ''; }
  set value(v) { this._attrs.set('value', String(v)); }
  get checked() { return this._attrs.has('checked'); }
  set checked(v) { if (v) this._attrs.set('checked', ''); else this._attrs.delete('checked'); }
  get disabled() { return this._attrs.has('disabled'); }
  set disabled(v) { if (v) this._attrs.set('disabled', ''); else this._attrs.delete('disabled'); }
  get hidden() { return this._attrs.has('hidden'); }
  set hidden(v) { if (v) this._attrs.set('hidden', ''); else this._attrs.delete('hidden'); }

  setAttribute(name, value) { this._attrs.set(String(name), value == null ? '' : String(value)); }
  getAttribute(name) { return this._attrs.has(name) ? this._attrs.get(name) : null; }
  hasAttribute(name) { return this._attrs.has(name); }
  removeAttribute(name) { this._attrs.delete(name); }

  get dataset() {
    const el = this;
    return new Proxy({}, {
      get: (_, key) => el._attrs.get('data-' + camelToKebab(String(key))),
      set: (_, key, value) => { el._attrs.set('data-' + camelToKebab(String(key)), String(value)); return true; },
      has: (_, key) => el._attrs.has('data-' + camelToKebab(String(key))),
      ownKeys: () => [...el._attrs.keys()].filter((k) => k.startsWith('data-')),
      getOwnPropertyDescriptor: () => ({ enumerable: true, configurable: true })
    });
  }

  /* --- 文本 --- */
  get textContent() {
    if (this._text !== undefined) return this._text;
    return this.childNodes.map((n) => n.textContent).join('');
  }
  set textContent(value) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    this._text = String(value);
  }

  /* --- innerHTML：真实解析标记，便于检测「用 innerHTML 渲染用户输入」这类回归 --- */
  get innerHTML() {
    return this.childNodes.map((node) => {
      if (node.nodeType === 3) return node.data;
      const attrs = [...node._attrs].map(([k, v]) => (v === '' ? ` ${k}` : ` ${k}="${v}"`)).join('');
      const tag = node.tagName.toLowerCase();
      if (VOID_TAGS.has(tag)) return `<${tag}${attrs}>`;
      return `<${tag}${attrs}>${node.innerHTML}</${tag}>`;
    }).join('');
  }
  set innerHTML(markup) {
    this.childNodes.forEach((n) => { n.parentNode = null; });
    this.childNodes = [];
    this._text = undefined;
    parseMarkup(String(markup), this.ownerDocument, this);
  }

  /* --- 树操作 --- */
  appendChild(node) {
    if (node && node.nodeType === 11) { // DocumentFragment
      node.childNodes.slice().forEach((child) => this.appendChild(child));
      node.childNodes = [];
      return node;
    }
    node.parentNode = this;
    this.childNodes.push(node);
    this._text = undefined;
    return node;
  }
  append(...nodes) { nodes.forEach((n) => this.appendChild(n)); }
  replaceChildren(...nodes) {
    this.childNodes = [];
    this._text = undefined;
    nodes.forEach((n) => this.appendChild(n));
  }
  removeChild(node) {
    this.childNodes = this.childNodes.filter((n) => n !== node);
    node.parentNode = null;
    return node;
  }
  matches(selector) { return matchesComplex(this, selector); }

  /* --- 事件 --- */
  addEventListener(type, handler) {
    if (!this._listeners.has(type)) this._listeners.set(type, []);
    this._listeners.get(type).push(handler);
  }
  removeEventListener(type, handler) {
    const list = this._listeners.get(type) || [];
    this._listeners.set(type, list.filter((h) => h !== handler));
  }
  dispatchEvent(event) {
    event.target = this;
    (this._listeners.get(event.type) || []).slice().forEach((handler) => handler.call(this, event));
    return !event.defaultPrevented;
  }
  /** 模拟浏览器点击：复选框会切换 checked 并派发 change；提交按钮会触发所在表单的 submit。 */
  click() {
    this.dispatchEvent(new Event('click'));
    if (this.tagName === 'INPUT' && this.type === 'checkbox') {
      this.checked = !this.checked;
      this.dispatchEvent(new Event('change'));
      return;
    }
    if (this.tagName === 'BUTTON' && (this.type === 'submit' || this.type === '')) {
      const form = this.closest('form');
      if (form) form.dispatchEvent(new Event('submit'));
    }
  }
  focus() { /* 测试环境中无需实现 */ }

  /* --- 查询 --- */
  closest(selector) {
    let node = this;
    while (node) {
      if (node.nodeType === 1 && matchesComplex(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  _descendants(out = []) {
    this.childNodes.forEach((node) => {
      if (node.nodeType === 1) { out.push(node); node._descendants(out); }
    });
    return out;
  }
  querySelectorAll(selector) { return this.ownerDocument.querySelectorAll(selector, this); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class DocumentFragment extends Element {
  constructor(doc) { super('#document-fragment', doc); this.nodeType = 11; }
}

function matchesCompound(el, compound) {
  if (!el || el.nodeType !== 1) return false;
  const tokens = compound.match(/\[[^\]]+\]|[.#][\w-]+|[\w-]+/g) || [];
  for (const token of tokens) {
    if (token.startsWith('[')) {
      const m = /^\[([\w-]+)(?:([~^$*|]?=)\s*"?([^"\]]*)"?)?\]$/.exec(token);
      if (!m) return false;
      const [, name, op, value] = m;
      if (!el.hasAttribute(name)) return false;
      if (op && el.getAttribute(name) !== value) return false;
    } else if (token.startsWith('.')) {
      if (!el.classList.contains(token.slice(1))) return false;
    } else if (token.startsWith('#')) {
      if (el.getAttribute('id') !== token.slice(1)) return false;
    } else if (el.tagName.toLowerCase() !== token.toLowerCase()) {
      return false;
    }
  }
  return true;
}

function matchesComplex(el, selector) {
  return selector.split(',').some((part) => {
    const compounds = part.trim().split(/\s+/).filter(Boolean);
    if (!compounds.length) return false;
    if (!matchesCompound(el, compounds[compounds.length - 1])) return false;
    let node = el.parentNode;
    let i = compounds.length - 2;
    while (i >= 0 && node) {
      if (matchesCompound(node, compounds[i])) i--;
      node = node.parentNode;
    }
    return i < 0;
  });
}

class Document {
  constructor() {
    this.nodeType = 9;
    this.childNodes = [];
    this.parentNode = null;
    this.documentElement = null;
  }
  createElement(tag) { return new Element(tag, this); }
  createTextNode(text) { return new TextNode(text); }
  createDocumentFragment() { return new DocumentFragment(this); }
  appendChild(node) { node.parentNode = this; this.childNodes.push(node); return node; }
  get textContent() { return this.childNodes.map((n) => n.textContent).join(''); }

  _allElements() {
    const out = [];
    const walk = (node) => {
      node.childNodes.forEach((child) => {
        if (child.nodeType === 1) { out.push(child); walk(child); }
      });
    };
    walk(this);
    return out;
  }
  getElementById(id) { return this._allElements().find((el) => el.getAttribute('id') === id) || null; }
  querySelectorAll(selector, root = null) {
    const scope = root || this;
    const candidates = scope instanceof Document ? scope._allElements() : (scope._descendants());
    return candidates.filter((el) => matchesComplex(el, selector));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get body() { return this.querySelector('body'); }
}

/** 极简 HTML 解析器：足够解析本项目结构良好的 HTML。 */
function parseMarkup(html, doc, root) {
  const source = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<!DOCTYPE[^>]*>/i, '');
  const tagRe = /<\/?([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>/g;
  const stack = [root];
  const addText = (text) => {
    if (!text.trim()) return;
    stack[stack.length - 1].appendChild(doc.createTextNode(text.trim()));
  };
  let lastIndex = 0;
  let m;
  while ((m = tagRe.exec(source))) {
    addText(source.slice(lastIndex, m.index));
    lastIndex = tagRe.lastIndex;
    const [full, name, rawAttrs, selfClose] = m;
    const tag = name.toLowerCase();
    if (full.startsWith('</')) {
      for (let i = stack.length - 1; i > 0; i--) {
        if (stack[i].tagName.toLowerCase() === tag) { stack.length = i; break; }
      }
      continue;
    }
    const el = doc.createElement(tag);
    const attrRe = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s]+)))?/g;
    let a;
    while ((a = attrRe.exec(rawAttrs || ''))) {
      const value = a[2] ?? a[3] ?? a[4] ?? '';
      el.setAttribute(a[1], value);
    }
    stack[stack.length - 1].appendChild(el);
    if (!VOID_TAGS.has(tag) && !selfClose) stack.push(el);
  }
  addText(source.slice(lastIndex));
}

function parseHTML(html) {
  const doc = new Document();
  parseMarkup(html, doc, doc);
  doc.documentElement = doc.querySelector('html');
  if (!doc.documentElement) throw new Error('index.html 解析失败：找不到 <html>');
  return doc;
}

/* ========================= 极简 localStorage ========================= */

class LocalStorageStub {
  constructor({ snapshot = null, throwOnGet = false, throwOnSet = false } = {}) {
    this.map = new Map(snapshot ? JSON.parse(snapshot) : []);
    this.throwOnGet = throwOnGet;
    this.throwOnSet = throwOnSet;
    this.writes = 0;
  }
  _securityError() {
    const err = new Error('The operation is insecure.');
    err.name = 'SecurityError';
    return err;
  }
  getItem(key) {
    if (this.throwOnGet) throw this._securityError();
    return this.map.has(key) ? this.map.get(key) : null;
  }
  setItem(key, value) {
    this.writes++;
    if (this.throwOnSet) throw this._securityError();
    this.map.set(String(key), String(value));
  }
  removeItem(key) { this.map.delete(key); }
  clear() { this.map.clear(); }
  snapshot() { return JSON.stringify([...this.map]); }
}

/* ============================ 会话（页面） ============================ */

function boot({ snapshot = null, throwOnGet = false, throwOnSet = false } = {}) {
  const document = parseHTML(INDEX_HTML);
  const localStorage = new LocalStorageStub({ snapshot, throwOnGet, throwOnSet });
  const logs = [];
  const record = (level) => (...args) => logs.push({ level, text: args.map(String).join(' ') });

  const sandbox = {
    document,
    localStorage,
    crypto: globalThis.crypto,
    setTimeout,
    clearTimeout,
    console: { log: record('log'), warn: record('warn'), error: record('error') }
  };
  const context = vm.createContext(sandbox);
  vm.runInContext('globalThis.window = globalThis;', context);
  vm.runInContext(APP_SOURCE, context, { filename: 'app.js' });

  return {
    document,
    localStorage,
    logs,
    snapshot: () => localStorage.snapshot(),
    storageJson: () => {
      const raw = localStorage.getItem(STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    }
  };
}

/* ============================== 断言工具 ============================== */

let passed = 0;
let failed = 0;
const failures = [];

function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed++;
    console.log(`  ✅ ${name}`);
  } else {
    failed++;
    failures.push(name);
    console.log(`  ❌ ${name}\n       期望: ${JSON.stringify(expected)}\n       实际: ${JSON.stringify(actual)}`);
  }
}

const section = (title) => console.log(`\n${title}`);

/* ================================ 用例 ================================ */

function helpers(page) {
  // 支持 $('task-input')、$('#task-input') 与 $('[data-filter="all"]') 三种写法
  const $ = (selector) => (page.document.getElementById(selector)
    || page.document.querySelector(selector.startsWith('#') || /[.[]/.test(selector) ? selector : '#' + selector));
  const listTexts = () => page.document.querySelectorAll('#task-list .task__text').map((n) => n.textContent);
  const doneFlags = () => page.document.querySelectorAll('#task-list .task').map((li) => li.classList.contains('is-done'));
  const boxes = () => page.document.querySelectorAll('#task-list input[type="checkbox"]');
  const addTask = (text) => {
    $('#task-input').value = text;
    page.document.querySelector('#task-form button[type="submit"]').click();
  };
  const clickFilter = (name) => page.document.querySelector(`[data-filter="${name}"]`).click();
  const pressedState = () => page.document.querySelectorAll('[data-filter]').map((b) => [b.dataset.filter, b.getAttribute('aria-pressed')]);
  return { document: page.document, page, $, listTexts, doneFlags, boxes, addTask, clickFilter, pressedState };
}

/* ---------- 1. 初始状态 ---------- */
section('1. 初始状态（空数据首次打开）');
let page = boot();
let h = helpers(page);
check('列表为空', h.listTexts(), []);
check('计数文案', h.$('task-count').textContent, '共 0 项 · 待办 0 项 · 已完成 0 项');
check('空状态提示可见', h.$('empty-state').hidden, false);
check('空状态提示文案', h.$('empty-state').textContent, '还没有任务，先添加一条吧。');
check('「清除已完成」初始为禁用', h.$('clear-completed').disabled, true);
check('localStorage 中尚无数据', h.$('task-input').value === '' && page.localStorage.getItem(STORAGE_KEY), null);
check('筛选按钮初始状态', h.pressedState(), [['all', 'true'], ['active', 'false'], ['done', 'false']]);

/* ---------- 2. 新增任务 ---------- */
section('2. 新增任务');
h.addTask('买牛奶');
h.addTask('写周报');
h.addTask('健身');
check('新增 3 条，顺序与内容正确', h.listTexts(), ['买牛奶', '写周报', '健身']);
check('计数更新', h.$('task-count').textContent, '共 3 项 · 待办 3 项 · 已完成 0 项');
check('空状态提示已隐藏', h.$('empty-state').hidden, true);
check('输入框已清空', h.$('task-input').value, '');
check('localStorage 已保存 3 条', page.storageJson().map((t) => t.text), ['买牛奶', '写周报', '健身']);
check('保存的数据字段完整', Object.keys(page.storageJson()[0]).sort(), ['createdAt', 'done', 'id', 'text']);
check('任务 id 唯一', new Set(page.storageJson().map((t) => t.id)).size, 3);
check('列表项带 data-id', page.document.querySelector('#task-list .task').dataset.id, page.storageJson()[0].id);

/* ---------- 3. 完成 / 取消完成 ---------- */
section('3. 完成与取消完成');
h.boxes()[1].click();
check('第 2 条标记为已完成', h.doneFlags(), [false, true, false]);
check('复选框 aria-label 同步更新', h.boxes()[1].getAttribute('aria-label'), '标记为未完成');
check('计数变为待办 2 / 已完成 1', h.$('task-count').textContent, '共 3 项 · 待办 2 项 · 已完成 1 项');
check('完成状态已写入 localStorage', page.storageJson().map((t) => t.done), [false, true, false]);
check('「清除已完成」变为可用', h.$('clear-completed').disabled, false);

h.boxes()[1].click();
check('再次点击可取消完成', h.doneFlags(), [false, false, false]);
check('取消后计数回到待办 3', h.$('task-count').textContent, '共 3 项 · 待办 3 项 · 已完成 0 项');
check('取消完成已持久化', page.storageJson().map((t) => t.done), [false, false, false]);

h.boxes()[1].click(); // 恢复「写周报已完成」，用于后续筛选与持久化断言
check('恢复为待办 2 / 已完成 1', h.$('task-count').textContent, '共 3 项 · 待办 2 项 · 已完成 1 项');

/* ---------- 4. 筛选 ---------- */
section('4. 全部 / 待办 / 已完成 筛选');
h.clickFilter('done');
check('「已完成」只显示 1 条', h.listTexts(), ['写周报']);
check('「已完成」按钮为选中态', h.pressedState(), [['all', 'false'], ['active', 'false'], ['done', 'true']]);

h.clickFilter('active');
check('「待办」显示 2 条', h.listTexts(), ['买牛奶', '健身']);
check('「待办」按钮为选中态', h.document.querySelector('[data-filter="active"]').getAttribute('aria-pressed'), 'true');

h.clickFilter('all');
check('「全部」恢复 3 条', h.listTexts(), ['买牛奶', '写周报', '健身']);
check('「全部」按钮为选中态', h.document.querySelector('[data-filter="all"]').getAttribute('aria-pressed'), 'true');

/* ---------- 5. 刷新后持久化 ---------- */
section('5. 刷新页面后数据持久化（重新加载 index.html + app.js）');
const snapshotAfterEdits = page.snapshot();
page = boot({ snapshot: snapshotAfterEdits });
h = helpers(page);
check('刷新后仍有 3 条任务', h.listTexts(), ['买牛奶', '写周报', '健身']);
check('刷新后完成状态保留', h.doneFlags(), [false, true, false]);
check('刷新后计数正确', h.$('task-count').textContent, '共 3 项 · 待办 2 项 · 已完成 1 项');
check('刷新后筛选回到「全部」', page.document.querySelector('[data-filter="all"]').getAttribute('aria-pressed'), 'true');
check('刷新后未破坏本地数据', page.storageJson().map((t) => t.text), ['买牛奶', '写周报', '健身']);

/* ---------- 6. 删除 / 清除已完成 ---------- */
section('6. 删除单条与清除已完成');
page.document.querySelectorAll('#task-list .task__delete')[0].click();
check('删除第 1 条后剩 2 条', h.listTexts(), ['写周报', '健身']);
check('删除结果已持久化', page.storageJson().map((t) => t.text), ['写周报', '健身']);

h.$('clear-completed').click();
check('清除已完成后只剩未完成项', h.listTexts(), ['健身']);
check('清除结果已持久化', page.storageJson().map((t) => t.text), ['健身']);
check('无已完成项时按钮再次禁用', h.$('clear-completed').disabled, true);

h.clickFilter('done');
check('「已完成」筛选为空', h.listTexts(), []);
check('空状态文案随筛选变化', h.$('empty-state').textContent, '还没有已完成的任务。');
check('列表为空时显示空状态', h.$('empty-state').hidden, false);
h.clickFilter('active');
check('「待办」筛选仍有 1 条', h.listTexts(), ['健身']);
h.clickFilter('all');

/* ---------- 7. 输入校验 ---------- */
section('7. 输入校验');
h.addTask('   ');
check('纯空白输入不会新增任务', h.listTexts(), ['健身']);
check('给出错误提示', h.$('form-error').hidden, false);
check('错误提示文案', h.$('form-error').textContent, '任务内容不能为空。');
check('空白输入未写入 localStorage', page.storageJson().length, 1);

h.addTask('  读一本书  ');
check('新增时自动去除首尾空格', h.listTexts(), ['健身', '读一本书']);
check('新增成功后错误提示自动隐藏', h.$('form-error').hidden, true);

/* ---------- 8. XSS 防护 ---------- */
section('8. XSS 防护（危险文本按纯文本渲染）');
const evil = '<img src=x onerror="window.__xss=1">';
h.addTask(evil);
check('危险内容原样显示为文本', h.listTexts(), ['健身', '读一本书', evil]);
check('未创建任何 img/script 元素', page.document.querySelectorAll('#task-list img, #task-list script, #task-list .task img').length, 0);
check('任务数量正常增加', h.$('task-count').textContent, '共 3 项 · 待办 3 项 · 已完成 0 项');
check('本地数据中保存的是纯文本', page.storageJson()[2].text, evil);

/* ---------- 9. 损坏数据容错 ---------- */
section('9. 本地数据异常时的容错');
page = boot({ snapshot: JSON.stringify([[STORAGE_KEY, '这不是 JSON{']]) });
h = helpers(page);
check('损坏 JSON 不会白屏，列表为空', h.listTexts(), []);
check('损坏 JSON 时计数归零', h.$('task-count').textContent, '共 0 项 · 待办 0 项 · 已完成 0 项');
check('损坏 JSON 会给出警告日志', page.logs.some((l) => l.level === 'warn' && l.text.includes('本地数据已损坏')), true);

page = boot({
  snapshot: JSON.stringify([[STORAGE_KEY, JSON.stringify([
    { id: 'a', text: '有效任务', done: false, createdAt: 1 },
    { nope: 1 },
    null,
    { id: 'b', text: '   ' },
    { id: 'c', text: '<b>粗体</b>', done: 'yes' }
  ])]])
});
h = helpers(page);
check('非法条目被过滤', h.listTexts(), ['有效任务', '<b>粗体</b>']);
check('非布尔 done 被规整为 false（复选框未勾选）', h.doneFlags(), [false, false]);
check('缺省 id 会被补齐为非空字符串', h.document.querySelectorAll('#task-list .task').every((li) => typeof li.dataset.id === 'string' && li.dataset.id.length > 0), true);

// 触发一次写回，确认落盘数据被彻底规整（布尔 done + 非空 id）
h.boxes()[1].click();
const rewritten = page.storageJson();
check('写回后 done 严格为布尔值', rewritten.map((t) => t.done), [false, true]);
check('写回后 id 严格为非空字符串', rewritten.every((t) => typeof t.id === 'string' && t.id.length > 0), true);
check('写回后非法条目已从存储中消失', rewritten.map((t) => t.text), ['有效任务', '<b>粗体</b>']);

/* ---------- 10. 存储不可用 ---------- */
section('10. localStorage 不可用时的降级');
page = boot({ throwOnGet: true });
h = helpers(page);
h.addTask('隐私模式下的任务');
check('读取失败时应用照常可用', h.listTexts(), ['隐私模式下的任务']);
check('读取失败会给出警告', page.logs.some((l) => l.level === 'warn' && l.text.includes('无法读取本地存储')), true);

page = boot({ throwOnSet: true });
h = helpers(page);
h.addTask('写入失败的任务');
check('写入失败时任务仍显示在列表', h.listTexts(), ['写入失败的任务']);
check('写入失败会提示用户', h.$('form-error').hidden, false);
check('写入失败提示文案', h.$('form-error').textContent, '保存失败，数据可能无法在刷新后保留。');

/* ---------- 11. 无异常 ---------- */
section('11. 运行期间无脚本报错');
const errored = [];
page = boot();
page.logs.push(); // 触发一次全新会话，确认渲染无异常
check('初始化未产生 error 级日志', page.logs.filter((l) => l.level === 'error'), errored);

/* ================================ 汇总 ================================ */

console.log('\n' + '─'.repeat(56));
console.log(`断言结果：${passed} 项通过，${failed} 项失败`);
if (failed) {
  console.log('失败用例：');
  failures.forEach((name) => console.log(`  - ${name}`));
  process.exitCode = 1;
} else {
  console.log('✅ 全部功能检查通过');
}
