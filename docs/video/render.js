const { chromium } = require('playwright-core');
const fs = require('fs'); const path = require('path');
const EXE = process.env.HOME + '/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell';
const mode = process.argv[2];           // "stills t1,t2,..." or "frames fps workers"
const url = 'file://' + path.join(__dirname, 'video.html') + '#render';
(async () => {
  const exe = fs.existsSync(EXE) ? EXE : require('child_process').execSync(`find ${process.env.HOME}/.cache/ms-playwright/chromium_headless_shell-1228 -name chrome-headless-shell -type f`).toString().trim().split('\n')[0];
  const browser = await chromium.launch({ executablePath: exe, args: ['--force-color-profile=srgb','--font-render-hinting=none'] });
  const mk = async () => { const p = await browser.newPage({ viewport: { width: 1920, height: 1080 } }); await p.goto(url); await p.waitForFunction('window.seek'); return p; };
  if (mode === 'stills') {
    const p = await mk(); fs.mkdirSync(path.join(__dirname,'stills'),{recursive:true});
    const total = await p.evaluate('window.TOTAL'); console.log('TOTAL', total);
    for (const t of process.argv[3].split(',').map(Number)) { await p.evaluate(t => seek(t), t); await p.screenshot({ path: path.join(__dirname,'stills',`s_${String(t).padStart(6,'0')}.png`) }); }
  } else {
    const fps = +process.argv[3], W = +process.argv[4]; const dir = path.join(__dirname,'frames'); fs.mkdirSync(dir,{recursive:true});
    const p0 = await mk(); const total = await p0.evaluate('window.TOTAL'); const N = Math.round(total*fps); console.log('frames', N);
    const pages = [p0, ...await Promise.all(Array.from({length: W-1}, mk))];
    let next = 0, done = 0; const t0 = Date.now();
    await Promise.all(pages.map(async p => { while (true) { const i = next++; if (i >= N) break;
      await p.evaluate(t => seek(t), i/fps); await p.screenshot({ path: path.join(dir, `f_${String(i).padStart(6,'0')}.jpg`), type: 'jpeg', quality: 92 });
      if (++done % 500 === 0) console.log(done, 'frames', ((Date.now()-t0)/1000).toFixed(0)+'s'); } }));
  }
  await browser.close();
})();
