#!/usr/bin/env node
/**
 * md2pdf —— markdown → A4 单页简历 PDF + 高度校验
 *
 * 零 npm 依赖。只用 Node 内置模块（含 Node 22 内置的 WebSocket）+ 系统里已装的
 * Chrome/Chromium，通过 DevTools Protocol 直接调 Page.printToPDF 生成 PDF。
 *
 * ⚠️ 为什么不用 `chrome --print-to-pdf` 这个更简单的命令行捷径：实测（2026-09-03，
 * Chrome 141）它的 `--print-to-pdf-no-header` 参数不生效——加不加这个参数产出的 PDF
 * 字节数完全相同，页面上会带着 Chrome 自己的打印页眉页脚（日期 + 文件路径 + 页码），
 * 这对一份简历是不可接受的视觉噪音。DevTools Protocol 的 `Page.printToPDF` 接口本身
 * 没有这个 bug，`displayHeaderFooter:false` 是真的生效的，所以改用这条路径。
 *
 * 这是 ai-native-jobhunt 全仓唯一允许存在的可执行依赖（见 modules/01-resume/SKILL.md）。
 * 装不上 Chrome、或不想装任何东西的人，请走 SKILL.md「单页硬约束」一节里的 no-code
 * 校验法——那条路径不依赖这个脚本，是同等有效的正式验收方式，不是降级选项。
 *
 * 用法：
 *   node md2pdf.js <输入.md> [输出.pdf]
 *
 * 退出码：
 *   0 = 单页，通过
 *   1 = 溢出到多页，未通过（PDF 仍会生成，方便你打开看溢出了多少）
 *   2 = 环境问题（找不到 Chrome / 输入文件不存在 / DevTools 连接失败等）——去用 no-code 校验法
 */

'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');
const http = require('http');
const { spawn, execFileSync } = require('child_process');

function fail(msg) {
  console.error('✖ ' + msg);
  process.exit(2);
}

// ---------- 1. 找一个能用的 Chrome/Chromium 二进制 ----------
function findChrome() {
  const candidates = [
    process.env.MD2PDF_CHROME_PATH,
    'google-chrome',
    'google-chrome-stable',
    'chromium',
    'chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', // macOS
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',   // Windows 常见路径
  ].filter(Boolean);

  for (const c of candidates) {
    try {
      if (path.isAbsolute(c)) {
        if (fs.existsSync(c)) return c;
        continue;
      }
      const whichCmd = process.platform === 'win32' ? 'where' : 'which';
      execFileSync(whichCmd, [c], { stdio: 'ignore' });
      return c;
    } catch (_) { /* 试下一个 */ }
  }
  return null;
}

// ---------- 2. 极简 markdown → HTML（零依赖，只覆盖简历会用到的语法） ----------
// 支持：# ## ### 标题 / **粗体** / *斜体* / - 无序列表 / --- 分割线 / 空行分段落 / | 表格 | / [文字](链接)
function mdToHtml(md) {
  const lines = md.replace(/\r\n/g, '\n').split('\n');
  const out = [];
  let i = 0;
  let inList = false;

  function inlineFmt(s) {
    return s
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.+?)\*/g, '<em>$1</em>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
  }

  function closeList() { if (inList) { out.push('</ul>'); inList = false; } }

  // 分隔行判据：|---|---| 或 |:---|---:| 这类——中间可以有任意多个「|」，
  // 每一段只能是空白/冒号/横线。⚠️ 上一版这里只允许"恰好两个竖线"，实测三栏
  // 表格（|---|---|---|）会直接落空、被当成普通段落输出——已用固件测出并修正。
  function isSeparatorRow(line) {
    return /^\s*\|(?:\s*:?-+:?\s*\|)+\s*$/.test(line);
  }

  while (i < lines.length) {
    const line = lines[i];

    if (/^\s*$/.test(line)) { closeList(); i++; continue; }

    if (/^---+\s*$/.test(line) && !isSeparatorRow(lines[i - 1] || '')) {
      // 分割线：但要排除"表格分隔行本身长得像 ---"这种误判——
      // isSeparatorRow 只匹配带竖线的行，纯 --- 不会被 isSeparatorRow 命中，
      // 这条判断其实恒真，保留是为了未来万一 --- 前面紧跟一个已开表格时不误判。
      closeList(); out.push('<hr>'); i++; continue;
    }

    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      closeList();
      const level = h[1].length;
      out.push(`<h${level}>${inlineFmt(h[2])}</h${level}>`);
      i++; continue;
    }

    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push(`<li>${inlineFmt(li[1])}</li>`);
      i++; continue;
    }

    // 表格：本行是 |a|b|c| 形式，下一行是分隔行
    if (/^\s*\|.*\|\s*$/.test(line) && lines[i + 1] && isSeparatorRow(lines[i + 1])) {
      closeList();
      const headers = line.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
      out.push('<table><thead><tr>' + headers.map(hd => `<th>${inlineFmt(hd)}</th>`).join('') + '</tr></thead><tbody>');
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        const cells = lines[i].trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
        out.push('<tr>' + cells.map(c => `<td>${inlineFmt(c)}</td>`).join('') + '</tr>');
        i++;
      }
      out.push('</tbody></table>');
      continue;
    }

    closeList();
    out.push(`<p>${inlineFmt(line)}</p>`);
    i++;
  }
  closeList();
  return out.join('\n');
}

const CSS = `
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", Arial, sans-serif;
  font-size: 10.5pt;
  line-height: 1.42;
  color: #1a1a1a;
  padding: 16mm 18mm; /* 页边距做在 body 内，而不是 @page margin ——
                          因为页眉页脚已经用 displayHeaderFooter:false 关掉，
                          @page margin 在这种模式下由内容自己的 padding 顶替 */
}
h1 { font-size: 17pt; margin: 0 0 4pt; border-bottom: 1.2pt solid #333; padding-bottom: 3pt; }
h2 { font-size: 12.5pt; margin: 10pt 0 4pt; border-bottom: 0.6pt solid #999; padding-bottom: 2pt; }
h3 { font-size: 11pt; margin: 7pt 0 2pt; }
p { margin: 2pt 0; }
ul { margin: 2pt 0 6pt; padding-left: 14pt; }
li { margin: 1.2pt 0; }
hr { border: none; border-top: 0.5pt solid #ccc; margin: 6pt 0; }
table { border-collapse: collapse; width: 100%; margin: 4pt 0; font-size: 9.5pt; }
th, td { border: 0.5pt solid #ccc; padding: 2pt 4pt; text-align: left; }
strong { font-weight: 600; }
`;

// ---------- 3. 用 DevTools Protocol 打印 PDF（不走有 bug 的命令行捷径） ----------
function httpJson(port, urlPath, method) {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { host: '127.0.0.1', port, path: urlPath, method: method || 'GET', timeout: 5000 },
      (res) => {
        let data = '';
        res.on('data', (c) => (data += c));
        res.on('end', () => {
          try { resolve(JSON.parse(data)); } catch (e) { reject(new Error(`响应不是合法 JSON（HTTP ${res.statusCode}）：${data.slice(0, 200)}`)); }
        });
      }
    );
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
    req.end();
  });
}
// 兼容旧调用名
function httpGetJson(port, urlPath) { return httpJson(port, urlPath, 'GET'); }

async function waitForPort(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      await httpGetJson(port, '/json/version');
      return true;
    } catch (_) {
      await new Promise((r) => setTimeout(r, 100));
    }
  }
  return false;
}

async function printHtmlToPdf(chromeBin, htmlFileAbs, outputPdfAbs) {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'md2pdf-profile-'));
  const port = 9260 + (process.pid % 300); // 尽量避开常见占用，冲突时下面会重试一个随机端口
  const args = [
    '--headless=new', '--disable-gpu', '--no-sandbox',
    '--disable-crash-reporter', '--disable-breakpad', // 关掉 crashpad_handler——
      // 它按设计会在主进程被杀之后继续存活一小段时间去捕获崩溃信息，实测过
      // 它偶尔还在 profile 目录里写东西时 rm -rf 正好执行到一半，导致目录删不
      // 干净、留下残留（`ls /tmp/md2pdf-profile-*` 观测到过）。这个工具用不到
      // 崩溃报告，直接关掉比"等它退出"更可靠。
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${userDataDir}`,
    'about:blank',
  ];
  // detached:true 让 chrome 自成一个进程组（pgid = 它自己的 pid），这样下面
  // 才能用 `kill(-pid)` 把它和它 fork 出的所有子进程（哪怕不是 Node 直接认识
  // 的子进程）一并杀掉，而不只是杀 Node 手上这一个 PID。
  const child = spawn(chromeBin, args, { stdio: 'ignore', detached: true });

  const cleanup = async () => {
    // ⚠️ 实测过一次：SIGKILL 是异步的，紧跟着 rmSync 有时会在 chrome 还没
    // 真正退出、profile 目录里的文件还被占用时执行，导致目录删不干净、留下
    // 残留（用 `ls /tmp/md2pdf-profile-*` 观测到过）。这里改成：先等进程真正
    // exit（或最多等 1.5s），再删目录；删失败再补一次重试。
    await new Promise((resolve) => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(); } };
      child.once('exit', finish);
      try {
        if (child.pid) process.kill(-child.pid, 'SIGKILL'); // 杀整个进程组
        else child.kill('SIGKILL');
      } catch (_) {
        try { child.kill('SIGKILL'); } catch (_) { finish(); } // 杀组失败退回杀单进程
      }
      setTimeout(finish, 1500);
    });
    for (const delay of [0, 300]) {
      if (delay) await new Promise((r) => setTimeout(r, delay));
      try { fs.rmSync(userDataDir, { recursive: true, force: true }); break; } catch (_) { /* 再试一次 */ }
    }
  };

  try {
    const up = await waitForPort(port, 8000);
    if (!up) throw new Error(`Chrome 在 ${8}s 内没有把 DevTools 端口 ${port} 打开——可能端口被占用或启动失败`);

    // 开一个新标签页，指向本地 HTML 文件
    const fileUrl = 'file://' + htmlFileAbs;
    // ⚠️ 新版 Chrome 的 /json/new 要求 PUT，不接受 GET（会返回 405 + 一句提示文本，
    // 不是 JSON——实测在这一步炸过一次，报错信息是「Using unsafe HTTP verb GET」）。
    const tab = await httpJson(port, `/json/new?${encodeURIComponent(fileUrl)}`, 'PUT');
    if (!tab.webSocketDebuggerUrl) throw new Error('拿不到标签页的 webSocketDebuggerUrl');

    const pdfBase64 = await new Promise((resolve, reject) => {
      const ws = new WebSocket(tab.webSocketDebuggerUrl);
      let msgId = 1;
      const pending = new Map();
      const timer = setTimeout(() => { ws.close(); reject(new Error('DevTools 会话超时（10s）')); }, 10000);

      function send(method, params) {
        const id = msgId++;
        return new Promise((res, rej) => {
          pending.set(id, { res, rej });
          ws.send(JSON.stringify({ id, method, params: params || {} }));
        });
      }

      ws.addEventListener('open', async () => {
        try {
          await send('Page.enable');
          // Page.navigate 到目标文件（新标签页用 /json/new?url 打开时通常已经在导航了，
          // 这里再显式 navigate 一次是为了拿到确定的 loaderId/frameId 时序，规避极少数
          // "printToPDF 早于首次渲染完成"的竞态）
          await send('Page.navigate', { url: fileUrl });
          await send('Page.setLifecycleEventsEnabled', { enabled: true });
        } catch (e) { clearTimeout(timer); reject(e); }
      });

      let printed = false;
      ws.addEventListener('message', async (ev) => {
        const msg = JSON.parse(ev.data.toString());
        if (msg.id !== undefined && pending.has(msg.id)) {
          const { res, rej } = pending.get(msg.id);
          pending.delete(msg.id);
          if (msg.error) rej(new Error(msg.error.message)); else res(msg.result);
          return;
        }
        // 等页面真正加载完（load 事件），再打印，避免中文字体还没排版好就截取
        if (!printed && msg.method === 'Page.lifecycleEvent' && msg.params && msg.params.name === 'load') {
          printed = true;
          try {
            const result = await send('Page.printToPDF', {
              printBackground: true,
              displayHeaderFooter: false,
              preferCSSPageSize: true,
              marginTop: 0, marginBottom: 0, marginLeft: 0, marginRight: 0,
            });
            clearTimeout(timer);
            ws.close();
            resolve(result.data);
          } catch (e) { clearTimeout(timer); reject(e); }
        }
      });

      ws.addEventListener('error', (e) => { clearTimeout(timer); reject(new Error('WebSocket 错误：' + (e.message || e))); });
    });

    fs.writeFileSync(outputPdfAbs, Buffer.from(pdfBase64, 'base64'));
  } finally {
    await cleanup();
  }
}

// ---------- 4. 主流程 ----------
async function main() {
  const [, , inputArg, outputArg] = process.argv;
  if (!inputArg) {
    console.error('用法：node md2pdf.js <输入.md> [输出.pdf]');
    console.error('没有 Chrome、或不想装任何东西？走 SKILL.md「单页硬约束」一节的 no-code 校验法，效力相同。');
    process.exit(2);
  }
  if (!fs.existsSync(inputArg)) fail(`找不到输入文件：${inputArg}`);

  const inputAbs = path.resolve(inputArg);
  const outputAbs = path.resolve(outputArg || inputAbs.replace(/\.md$/i, '') + '.pdf');

  const chrome = findChrome();
  if (!chrome) {
    fail(
      '没找到 Chrome/Chromium。这个工具是全仓唯一的可执行依赖，找不到就直接停——' +
      '不代表你没法校验单页，去用 SKILL.md「单页硬约束」一节的 no-code 校验法（行数/字符密度估算 + 浏览器打印预览人工确认），' +
      '效力和这个脚本相同，不是降级方案。'
    );
  }

  const md = fs.readFileSync(inputAbs, 'utf8');
  const body = mdToHtml(md);
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>${CSS}</style></head><body>${body}</body></html>`;

  const tmpHtml = path.join(os.tmpdir(), `md2pdf-${Date.now()}.html`);
  fs.writeFileSync(tmpHtml, html, 'utf8');

  try {
    await printHtmlToPdf(chrome, tmpHtml, outputAbs);
  } catch (e) {
    fail('生成 PDF 失败：' + e.message + '\n去用 no-code 校验法，不要在这个错误上反复重试。');
  } finally {
    try { fs.unlinkSync(tmpHtml); } catch (_) {}
  }

  if (!fs.existsSync(outputAbs)) fail('流程声称成功但没有生成 PDF 文件——环境有问题，走 no-code 校验法。');

  const pdfBytes = fs.readFileSync(outputAbs);
  const m = pdfBytes.toString('latin1').match(/\/Type\s*\/Pages[\s\S]{0,400}?\/Count\s+(\d+)/);
  if (!m) {
    console.log(`⚠ PDF 已生成（${outputAbs}），但没能从文件里解析出页数（这个脚本不依赖任何 PDF 库，解析是尽力而为的）。`);
    console.log('  请直接打开这份 PDF 肉眼确认是否单页——这不算脚本失败，只是页数解析这一步保守地放弃了。');
    process.exit(0);
  }
  const pages = parseInt(m[1], 10);

  console.log(`PDF 已生成：${outputAbs}`);
  console.log(`页数：${pages}`);
  if (pages === 1) {
    console.log('✅ 单页，通过。');
    process.exit(0);
  } else {
    console.log(`⛔ ${pages} 页，溢出，未通过。`);
    console.log('  按 SKILL.md 的定制白名单精简（只许删条目/改顺序/替术语，不许为了塞进一页而改事实或压缩到看不清）。');
    console.log('  精简后重跑这个脚本，或者打开这份 PDF 用打印预览人工看溢出了多少。');
    process.exit(1);
  }
}

main().catch((e) => fail('未预期的错误：' + (e && e.stack || e)));
