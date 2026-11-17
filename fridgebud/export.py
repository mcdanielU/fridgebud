"""The shopping list as a small web page: something to shop from in the store, not just a text to scroll.

Items are grouped by aisle and tick off with a tap (ticks are kept on that phone only). Underneath, "Share"
opens the phone's share sheet (Notes, Notion, Reminders, WhatsApp...) and the copy buttons put the list on the
clipboard in the shape each app understands: one item per line for Apple Notes, headings plus "- [ ] item" for
Notion, which pastes as to-dos. The page lives at an unguessable link per household; nothing else is on it.
"""
from __future__ import annotations

import html
import json
import re

from .aisles import EMOJI, aisle, grouped  # noqa: F401  (re-exported)

def page(items: list[str], title: str = "Shopping list") -> str:
    groups = grouped(items)
    sections = "\n".join(
        f'<section><h2><span aria-hidden="true">{EMOJI[name]}</span> {name}<small>{len(rows)}</small></h2>'
        + "".join(f'<label><input type="checkbox" data-item="{html.escape(i)}"><span>{html.escape(i)}</span></label>'
                  for i in rows) + "</section>"
        for name, rows in groups)
    plain = "\n".join(items)
    notion = "\n\n".join(f"## {name}\n" + "\n".join(f"- [ ] {i}" for i in rows) for name, rows in groups)
    count = f"{len(items)} item{'' if len(items) == 1 else 's'}"
    empty = '<p class="empty">Your list is empty. Reply SHOP under a recipe and Bud fills it in.</p>' if not items else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><meta name="theme-color" content="#0c1f1c"><title>{html.escape(title)} · FridgeBud</title>
<style>
  :root {{ --paper:#f4f6f2; --ink:#0c1f1c; --muted:#5b6b67; --line:#e1e7e3; --card:#fff; --yolk:#ffc533; --leaf:#2f8f5b; }}
  @media (prefers-color-scheme: dark) {{ :root {{ --paper:#0a1614; --ink:#e9f1ef; --muted:#93a7a2; --line:#1f3330; --card:#11211e; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--ink); font:400 17px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
         -webkit-font-smoothing:antialiased; }}
  main {{ max-width:480px; margin:0 auto; padding:0 16px 48px; }}
  header {{ position:sticky; top:0; z-index:2; background:var(--paper); padding:22px 4px 14px; }}
  .brand {{ font-size:13px; font-weight:600; color:var(--muted); letter-spacing:.04em; text-transform:uppercase; }}
  h1 {{ font-size:30px; line-height:1.1; margin:4px 0 12px; letter-spacing:-.025em; }}
  .bar {{ height:8px; background:var(--line); border-radius:99px; overflow:hidden; }}
  .bar i {{ display:block; height:100%; width:0; background:var(--leaf); border-radius:99px; transition:width .25s; }}
  .progress {{ display:flex; justify-content:space-between; font-size:14px; color:var(--muted); margin-top:8px; }}
  .progress button {{ all:unset; cursor:pointer; color:var(--muted); text-decoration:underline; }}
  section {{ background:var(--card); border:1px solid var(--line); border-radius:18px; margin:14px 0; overflow:hidden; }}
  h2 {{ display:flex; align-items:center; gap:8px; font-size:15px; margin:0; padding:13px 16px 9px; }}
  h2 small {{ margin-left:auto; color:var(--muted); font-weight:500; }}
  label {{ display:flex; align-items:center; gap:13px; padding:12px 16px; border-top:1px solid var(--line); cursor:pointer;
          -webkit-tap-highlight-color:transparent; }}
  input {{ appearance:none; -webkit-appearance:none; width:24px; height:24px; flex:none; margin:0; border:2px solid var(--muted);
          border-radius:50%; display:grid; place-content:center; transition:background .15s, border-color .15s; }}
  input:checked {{ background:var(--leaf); border-color:var(--leaf); }}
  input:checked::after {{ content:""; width:6px; height:11px; border:solid #fff; border-width:0 2.5px 2.5px 0; transform:rotate(45deg) translate(-1px,-1px); }}
  input:checked + span {{ color:var(--muted); text-decoration:line-through; }}
  .send {{ margin-top:28px; }}
  .send h3 {{ font-size:14px; color:var(--muted); font-weight:600; margin:0 4px 10px; }}
  .send button {{ width:100%; font:600 16px/1 inherit; padding:15px; border-radius:999px; border:1.5px solid var(--line);
                 background:var(--card); color:var(--ink); margin-bottom:10px; cursor:pointer; }}
  .send button.primary {{ background:var(--yolk); border-color:var(--yolk); color:#2a1d00; }}
  .done {{ text-align:center; color:var(--muted); font-size:14px; min-height:20px; }}
  .empty {{ text-align:center; color:var(--muted); padding:40px 10px; }}
</style></head>
<body><main>
  <header>
    <div class="brand">FridgeBud</div>
    <h1>{html.escape(title)}</h1>
    <div class="bar"><i id="fill"></i></div>
    <div class="progress"><span id="count">0 of {count} in the cart</span><button id="reset" hidden>Untick all</button></div>
  </header>
  {sections}{empty}
  <div class="send">
    <h3>Send to your notes app</h3>
    <button class="primary" id="share">Share to Notes, Notion…</button>
    <button id="apple">Copy for Apple Notes</button>
    <button id="notion">Copy for Notion</button>
    <p class="done" id="done" role="status"></p>
  </div>
</main>
<script>
  const plain = {json.dumps(plain)}, notion = {json.dumps(notion)}, done = document.getElementById("done");
  const boxes = [...document.querySelectorAll("input[data-item]")], key = "fridgebud" + location.pathname;
  let ticked = {{}};
  try {{ ticked = JSON.parse(localStorage.getItem(key) || "{{}}"); }} catch (e) {{}}
  function update() {{
    const n = boxes.filter(b => b.checked).length;
    document.getElementById("count").textContent = n === boxes.length && n
      ? "All done. Send Bud the receipt and it updates your fridge 🎉" : n + " of {count} in the cart";
    document.getElementById("fill").style.width = (boxes.length ? 100 * n / boxes.length : 0) + "%";
    document.getElementById("reset").hidden = !n;
  }}
  boxes.forEach(b => {{
    b.checked = !!ticked[b.dataset.item];
    b.onchange = () => {{
      if (b.checked) ticked[b.dataset.item] = 1; else delete ticked[b.dataset.item];
      try {{ localStorage.setItem(key, JSON.stringify(ticked)); }} catch (e) {{}}
      update();
    }};
  }});
  document.getElementById("reset").onclick = () => {{
    ticked = {{}}; boxes.forEach(b => b.checked = false);
    try {{ localStorage.removeItem(key); }} catch (e) {{}}
    update();
  }};
  update();
  async function copy(text, tip) {{
    try {{ await navigator.clipboard.writeText(text); done.textContent = tip; }}
    catch (e) {{ done.textContent = "Couldn't copy here. Press and hold the list to copy it."; }}
  }}
  const share = document.getElementById("share");
  if (navigator.share) {{
    share.onclick = () => navigator.share({{ title: {json.dumps(title)}, text: plain }}).catch(() => {{}});
  }} else {{ share.style.display = "none"; }}
  document.getElementById("apple").onclick = () => copy(plain, "Copied. In Notes: paste, select it all, then tap the checklist button.");
  document.getElementById("notion").onclick = () => copy(notion, "Copied. Paste into Notion and it becomes a to-do list.");
</script>
</body></html>"""
