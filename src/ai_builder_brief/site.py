"""Static public site renderer for AI Builder Brief."""

from __future__ import annotations

from pathlib import Path


SITE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="Three to five source-linked AI developments before stand-up.">
  <title>AI Builder Brief</title>
  <style>
    :root { color-scheme: dark; --bg:#090d12; --panel:#111923; --text:#eef6ff; --muted:#98a9bb; --accent:#60e6a8; --line:#263443; }
    * { box-sizing:border-box; } body { margin:0; background:radial-gradient(circle at 20% 0,#153b3377,transparent 34rem),var(--bg); color:var(--text); font:16px/1.6 Inter,ui-sans-serif,system-ui,sans-serif; }
    main { width:min(980px,calc(100% - 2rem)); margin:auto; } a { color:inherit; text-underline-offset:.2em; }
    header { min-height:54vh; display:grid; align-content:center; padding:4rem 0 3rem; border-bottom:1px solid var(--line); }
    .kicker { color:var(--accent); font-weight:800; letter-spacing:.09em; text-transform:uppercase; }
    h1 { margin:.15rem 0 .7rem; max-width:12ch; font-size:clamp(3.2rem,10vw,7rem); line-height:.88; letter-spacing:-.065em; }
    .lede { max-width:45rem; color:var(--muted); font-size:1.15rem; } nav { display:flex; flex-wrap:wrap; gap:.7rem; margin-top:1.3rem; }
    .button { padding:.65rem 1rem; border:1px solid var(--line); border-radius:999px; text-decoration:none; font-weight:750; background:var(--panel); }
    .primary { color:#07110d; background:var(--accent); border-color:var(--accent); }
    section { padding:2.2rem 0; } .section-head { display:flex; justify-content:space-between; align-items:end; border-bottom:1px solid var(--line); }
    h2 { margin:0 0 .7rem; } #episodes { display:grid; gap:1rem; padding-top:1.2rem; }
    article { padding:1.2rem; border:1px solid var(--line); border-radius:18px; background:#111923dd; }
    article h3 { margin:0; } article p,time { color:var(--muted); } audio { width:100%; margin:.8rem 0; }
    .links { display:flex; flex-wrap:wrap; gap:1rem; font-size:.92rem; } footer { padding:2rem 0 3rem; color:var(--muted); border-top:1px solid var(--line); }
  </style>
</head>
<body><main>
  <header>
    <div class="kicker">Daily · source transparent · ~6 minutes</div>
    <h1>AI Builder Brief</h1>
    <p class="lede">The AI developments that matter to people building with models, agents, and open-source tools. Every story carries its primary source or independent corroboration.</p>
    <nav><a class="button primary" href="feed.xml">Subscribe via RSS</a><a class="button" href="https://github.com/lifan-builds/ai-builder-brief">Source &amp; manifests</a><a class="button" href="https://github.com/lifan-builds/castforge">Powered by CastForge</a></nav>
  </header>
  <section><div class="section-head"><h2>Episodes</h2><p id="count">Loading feed…</p></div><div id="episodes"></div></section>
  <footer><p>Automated briefing. Claims should be verified against the linked sources. Reddit and newsletter summaries are not used as factual inputs.</p></footer>
</main>
<script>
const episodes=document.querySelector('#episodes'),count=document.querySelector('#count');
fetch('feed.xml').then(r=>{if(!r.ok)throw Error(`Feed returned ${r.status}`);return r.text()}).then(x=>new DOMParser().parseFromString(x,'application/xml')).then(feed=>{
  const items=[...feed.querySelectorAll('channel > item')]; count.textContent=`${items.length} episodes`;
  for(const item of items){const guid=item.querySelector('guid')?.textContent||'',date=guid.slice(-10),card=document.createElement('article'),title=document.createElement('h3'),time=document.createElement('time'),audio=document.createElement('audio'),summary=document.createElement('p'),links=document.createElement('div');
    title.textContent=item.querySelector('title')?.textContent||'AI Builder Brief'; time.textContent=new Date(item.querySelector('pubDate')?.textContent||'').toLocaleDateString(undefined,{dateStyle:'long'}); audio.controls=true;audio.preload='none';audio.src=item.querySelector('enclosure')?.getAttribute('url')||''; summary.textContent=item.querySelector('description')?.textContent||'';links.className='links';
    for(const [label,url] of [['Source manifest',`manifests/${date}.json`],['Transcript',`transcripts/${date}.vtt`],['Chapters',`chapters/${date}.json`]]){const a=document.createElement('a');a.textContent=label;a.href=url;links.append(a)} card.append(title,time,audio,summary,links);episodes.append(card)}
}).catch(e=>{count.textContent='Feed unavailable';episodes.textContent=e.message});
</script></body></html>
"""


def render_site(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SITE_HTML, encoding="utf-8")
    return path
