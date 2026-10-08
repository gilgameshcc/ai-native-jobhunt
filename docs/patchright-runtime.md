# 本机 Patchright 与持久 profile 接入

适用于 [02-apply](../modules/02-apply/SKILL.md) 的运行环境检查；本仓库仍只维护判断规则，不提供真实平台投递实现。以下命令和校验脚本在**用户私人工作区**运行，不在公开仓库保存浏览器资料。

## 先判断运行时能力

本地 CLI agent 可通过本机 Node 脚本兼任浏览器侧，并按文件交接：先将页面资料写进 jobs/，再读取文件定级。是否叫 Codex、Claude Code 或浏览器 agent 不构成能力证明。无现成 adapter 可以只读探索，不能据此宣称已适配上传或发送。

有常驻 Host 时先读其状态，确认已加载后端与唯一 profile owner；不要另起进程占用它的资料目录。普通 Playwright、泛用浏览器工具与 Patchright API 相似，但不能替代后端核实。已出现安全验证时遵守 [风险停止](../modules/02-apply/GOVERNANCE.md)，修复本机依赖不自动解除平台暂停。

## 安装与实际导入是两件事

在私人工作区根目录执行：

```bash
npm view patchright dist-tags.latest
npm install --prefix .claude/browser-runtime --save-exact patchright@latest
npx --prefix .claude/browser-runtime patchright install chromium
npx --prefix .claude/browser-runtime patchright --version
```

`npx patchright` 提供安装、open、codegen 等 CLI 命令，不是执行任意 Node 脚本的命令。脚本放在安装目录中，用 `node .claude/browser-runtime/verify.cjs` 运行，并从 **patchright** 导入。不要假定 `npx --package=patchright node script.cjs` 会让脚本自动解析到该包。记录查询日期与稳定版；npm 锁文件记录安装版本，实际运行仍需输出解析路径与加载版本。

## 私人持久 profile

在私人工作区根目录运行时，约定 `.claude/browser-profile` 为持久资料目录。已有同目录的登录态可以复用，但新建空目录不能叫“已保留登录态”；登录有效性须在目标网站由可见身份核实。profile 放哪由用户决定，不复制正在运行的浏览器资料，不导出 cookie、token 或密码到 Markdown。

脚本用绝对路径或相对脚本位置解析 profile，不能随着 cwd 改变悄悄建另一套目录。同一 profile 一次只允许一个浏览器进程；占用时复用已有 Host 或等待本人关闭，不删除 Chromium 锁、不杀他人的进程。停止本次脚本时只关闭自己创建的 context。

公开仓库中不要创建或提交 profile；若私人工作区本身由 Git 管理，在本机 `info/exclude` 排除 `.claude/browser-profile/` 和 `.claude/browser-runtime/`，并用 `git check-ignore` 验证。

本机已有 Google Chrome 时，可使用 `channel: 'chrome'`，不必等待 Chromium 下载；没有 Chrome 则完成上面的安装，并在脚本中去掉 channel 字段以使用配套 Chromium。两条路径都必须由 Patchright 驱动。

## 不访问招聘平台的本机验证

将以下脚本存为私人工作区 `.claude/browser-runtime/verify.cjs`。它在本机 HTTP 页面上验证读取、填写回读，以及 context 重启后同一 profile 的存储保留；标记只用于这个本机页面，不接触真实网站的登录数据。

```javascript
const { chromium } = require('patchright');
const { version } = require('patchright/package.json');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');

const profile = path.resolve(__dirname, '../browser-profile');
const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end('<h1>本机验证</h1><input aria-label="验证输入">');
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  console.log(JSON.stringify({ backend: 'patchright', version,
    modulePath: require.resolve('patchright'), profile, pid: process.pid }));
  let context;
  try {
    context = await chromium.launchPersistentContext(profile, { channel: 'chrome', headless: false });
    let page = await context.newPage();
    await page.goto(url);
    assert.equal(await page.locator('h1').innerText(), '本机验证');
    await page.getByLabel('验证输入').fill('读取填写回读');
    assert.equal(await page.getByLabel('验证输入').inputValue(), '读取填写回读');
    await page.evaluate(() => localStorage.setItem('local-runtime-check', 'ok'));
    await context.close();
    context = undefined;
    context = await chromium.launchPersistentContext(profile, { channel: 'chrome', headless: false });
    page = await context.newPage();
    await page.goto(url);
    assert.equal(await page.evaluate(() => localStorage.getItem('local-runtime-check')), 'ok');
    await page.evaluate(() => localStorage.removeItem('local-runtime-check'));
    console.log('本机读取、填写回读与持久 profile 验证通过');
  } finally {
    if (context) await context.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
```

仅在带桌面环境且该 profile 未被占用时运行。检查失败就记录症状并修复环境，不静默改用普通 Playwright。脚本不会测试真实网站登录、风险停止或发送权限，不能以它通过为由声明网站验证通过。

**常驻 Host 不直接用这份短脚本替换。** 安装版本核对只是磁盘证据；还须读其进程状态或启动记录，按已有管理入口重载后再核实加载版本、解析路径与动作能力。

使用方式依据：[Patchright NodeJS 官方说明](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-nodejs)。不固定推荐版本，运行时按 npm 稳定标签查询。
