/** 变异测试：故意在 app.js 副本里引入 bug，确认检查脚本会失败（证明断言有效）。
 *  仅在 .mutation-scratch/ 下操作，结束后自动删除。 */
import { readFile, writeFile, mkdir, rm, cp } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const SCRATCH = path.join(ROOT, '.mutation-scratch');
const APP = await readFile(path.join(ROOT, 'app.js'), 'utf8');
const HTML = await readFile(path.join(ROOT, 'index.html'), 'utf8');
const CHECK = await readFile(path.join(ROOT, 'tests', 'dom-check.mjs'), 'utf8');

const mutations = [
  ['新增任务后不写 localStorage', APP.replace('tasks.push({ id: createId(), text: value, done: false, createdAt: Date.now() });\n    hideError();\n    saveTasks();', 'tasks.push({ id: createId(), text: value, done: false, createdAt: Date.now() });\n    hideError();\n    // saveTasks();')],
  ['切换完成状态不生效', APP.replace('task.done = !task.done;', 'task.done = task.done;')],
  ['切换完成状态后不保存', APP.replace('task.done = !task.done;\n    saveTasks();', 'task.done = !task.done;')],
  ['「待办」筛选失效（返回全部）', APP.replace("if (filter === 'active') {\n      return tasks.filter(function (task) { return !task.done; });\n    }", '')],
  ['删除任务不生效', APP.replace('tasks = next;\n    saveTasks();\n    render();\n  }\n\n  function clearCompleted()', 'tasks = tasks;\n    render();\n  }\n\n  function clearCompleted()')],
  ['启动时忽略 localStorage', APP.replace('tasks = loadTasks();', 'tasks = [];')],
  ['用 innerHTML 渲染任务文本（XSS 回归）', APP.replace('label.textContent = task.text;', 'label.innerHTML = task.text;')],
  ['空输入不做校验', APP.replace('if (!value) {\n      showError(\'任务内容不能为空。\');\n      return false;\n    }', '')],
  ['筛选按钮不更新 aria-pressed', APP.replace("button.setAttribute('aria-pressed', isActive ? 'true' : 'false');", '')],
];

await rm(SCRATCH, { recursive: true, force: true });
await mkdir(SCRATCH, { recursive: true });

const results = [];
for (const [index, [name, mutated]] of mutations.entries()) {
  if (mutated === APP) {
    results.push({ name, status: 'SKIP(未匹配到代码)' });
    continue;
  }
  const dir = path.join(SCRATCH, `m${index}`);
  await mkdir(path.join(dir, 'tests'), { recursive: true });
  await writeFile(path.join(dir, 'app.js'), mutated);
  await writeFile(path.join(dir, 'index.html'), HTML);
  const { writeFile: _w } = await import('node:fs/promises');
  await cp(path.join(ROOT, 'styles.css'), path.join(dir, 'styles.css'));
  // 复用真实检查脚本（它按自身位置推算项目根目录）
  await writeFile(path.join(dir, 'tests', 'dom-check.mjs'), CHECK);

  const { status, stdout } = await new Promise((resolve) => {
    const child = spawn(process.execPath, [path.join(dir, 'tests', 'dom-check.mjs')], { stdio: ['ignore', 'pipe', 'pipe'] });
    let out = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { out += d; });
    child.on('close', (code) => resolve({ status: code, stdout: out }));
  });

  const failedLine = (stdout.match(/断言结果：(\d+) 项通过，(\d+) 项失败/) || [])[1];
  const caught = status !== 0;
  results.push({
    name,
    status: caught ? `已捕获（${failedLine} 项通过后失败，exit=${status}）` : `❌ 未被捕获（exit=${status}）`,
    caught,
    firstFail: (stdout.split('\n').find((l) => l.includes('❌')) || '').trim()
  });
}

await rm(SCRATCH, { recursive: true, force: true });

console.log('变异测试结果（期望：每个变异都被检查脚本捕获）\n' + '─'.repeat(56));
let missed = 0;
for (const r of results) {
  if (r.status.startsWith('SKIP')) { console.log(`⚠️  ${r.name}: ${r.status}`); continue; }
  if (r.caught) {
    console.log(`✅ ${r.name}\n     ${r.status}\n     首个失败断言: ${r.firstFail.replace('❌ ', '')}`);
  } else {
    missed++;
    console.log(`❌ ${r.name}: ${r.status}`);
  }
}
console.log('─'.repeat(56));
console.log(missed === 0 ? `✅ ${results.filter((r) => r.caught).length} 个变异全部被捕获，检查脚本有效` : `⚠️ 有 ${missed} 个变异未被捕获`);
process.exitCode = missed === 0 ? 0 : 1;
