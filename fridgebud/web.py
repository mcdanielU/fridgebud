"""The landing page and signup form. One self-contained page, no build step.

Leads with money and Bud, shows the price before the phone number field, and
collects express consent for texts (CASL): an unticked box, who is sending,
how to stop, and how to reach us.
"""
from __future__ import annotations

from html import escape

CONSENT_WORDING = (
    "I agree to get text messages from FridgeBud at this number (about one a day). "
    "I can reply STOP at any time to unsubscribe. Message and data rates may apply."
)

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FridgeBud: Bud texts you before your food dies</title>
<meta name="description" content="One text at 4:30 PM with what's about to go off in your fridge and a dinner that uses it. No app.">
<style>
  :root {{ --ink:#17251c; --paper:#f6f1e7; --leaf:#1f6b46; --leaf-dark:#154d32; --tomato:#d8452e; --line:#dcd3c1; --bubble:#e7e2d6; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--paper); color:var(--ink); font:17px/1.55 ui-rounded,"SF Pro Rounded","Nunito",system-ui,-apple-system,"Segoe UI",sans-serif; }}
  .wrap {{ max-width:1040px; margin:0 auto; padding:28px 22px 64px; }}
  header {{ display:flex; align-items:center; gap:10px; font-weight:800; font-size:20px; letter-spacing:-.01em; }}
  .dot {{ width:30px; height:30px; border-radius:10px 10px 12px 12px; background:var(--leaf); position:relative; }}
  .dot::before, .dot::after {{ content:""; position:absolute; top:10px; width:5px; height:7px; border-radius:50%; background:var(--paper); }}
  .dot::before {{ left:8px; }} .dot::after {{ right:8px; }}
  .hero {{ display:grid; grid-template-columns:1.1fr .9fr; gap:48px; align-items:center; margin-top:44px; }}
  h1 {{ font-size:clamp(38px,6vw,62px); line-height:1.02; letter-spacing:-.03em; margin:0 0 18px; }}
  h1 em {{ font-style:normal; color:var(--tomato); }}
  .lede {{ font-size:20px; max-width:30em; margin:0 0 26px; }}
  .price {{ display:inline-block; background:#fff; border:1.5px solid var(--ink); border-radius:999px; padding:8px 16px; font-weight:700; margin-bottom:20px; box-shadow:3px 3px 0 var(--ink); }}
  form {{ background:#fff; border:1.5px solid var(--ink); border-radius:18px; padding:20px; box-shadow:5px 5px 0 var(--ink); max-width:30em; }}
  label.field {{ display:block; font-weight:700; margin-bottom:6px; }}
  input[type=tel] {{ width:100%; font:inherit; font-size:20px; padding:12px 14px; border:1.5px solid var(--ink); border-radius:12px; background:var(--paper); }}
  input[type=tel]:focus-visible, button:focus-visible, input[type=checkbox]:focus-visible {{ outline:3px solid var(--tomato); outline-offset:2px; }}
  .consent {{ display:flex; gap:10px; align-items:flex-start; font-size:14.5px; margin:14px 0; }}
  .consent input {{ width:20px; height:20px; margin-top:2px; accent-color:var(--leaf); flex:none; }}
  button {{ width:100%; font:inherit; font-weight:800; font-size:19px; color:#fff; background:var(--leaf); border:0; border-radius:12px; padding:14px; cursor:pointer; }}
  button:hover {{ background:var(--leaf-dark); }}
  .error {{ background:#fde7e2; border:1.5px solid var(--tomato); border-radius:12px; padding:10px 14px; margin-bottom:14px; }}
  .small {{ font-size:14px; opacity:.8; }}
  .phone {{ background:#fff; border:1.5px solid var(--ink); border-radius:34px; padding:22px 16px 26px; box-shadow:7px 7px 0 var(--leaf); max-width:360px; margin:0 auto; }}
  .phone .who {{ text-align:center; font-weight:700; font-size:14px; padding-bottom:12px; border-bottom:1px solid var(--line); margin-bottom:14px; }}
  .msg {{ max-width:88%; padding:10px 13px; border-radius:18px; margin:0 0 9px; font-size:15px; line-height:1.4; white-space:pre-line; }}
  .in {{ background:var(--bubble); border-bottom-left-radius:5px; }}
  .out {{ background:var(--leaf); color:#fff; margin-left:auto; border-bottom-right-radius:5px; width:fit-content; }}
  .time {{ text-align:center; font-size:12px; opacity:.6; margin:4px 0 10px; }}
  .steps {{ display:grid; grid-template-columns:repeat(3,1fr); gap:18px; margin-top:72px; }}
  .step {{ border-top:3px solid var(--ink); padding-top:14px; }}
  .step b {{ display:block; font-size:19px; margin-bottom:4px; }}
  .num {{ font-weight:800; color:var(--tomato); }}
  footer {{ margin-top:64px; padding-top:18px; border-top:1px solid var(--line); font-size:14px; opacity:.85; }}
  @media (max-width:800px) {{ .hero {{ grid-template-columns:1fr; gap:36px; }} .steps {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body>
<div class="wrap">
  <header><span class="dot" aria-hidden="true"></span>FridgeBud</header>

  <main>
  <section class="hero">
    <div>
      <h1>Bud texts you <em>before</em> your food dies.</h1>
      <p class="lede">One text at 4:30 PM: what's about to go off in your fridge, and a dinner that uses it. No app to open. Just reply.</p>
      <div class="price">{price_line}</div>
      {error}
      <form method="post" action="/signup">
        <label class="field" for="phone">Your mobile number</label>
        <input id="phone" name="phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="519 555 0100" value="{phone}" required>
        <label class="consent"><input type="checkbox" name="consent" value="yes" required><span>{consent}</span></label>
        <input type="hidden" name="ref" value="{ref}">
        <button type="submit">Text me at 4:30</button>
        <p class="small" style="margin:12px 0 0">You'll get one welcome text. Nothing else is sent until you reply YES.</p>
      </form>
    </div>
    <div class="phone" aria-label="Example of a FridgeBud text conversation">
      <div class="who">Bud</div>
      <div class="time">Today 4:30 PM</div>
      <p class="msg in">🚨 Bud here. Your baby spinach is giving its farewell speech.
Use soon: baby spinach (use today), chicken thighs (use by tomorrow).
Tonight: Quick curry, about 30 min.
Reply MADE when you cook it.</p>
      <p class="msg out">made it</p>
      <p class="msg in">🎉 RESCUED: chicken thighs, baby spinach. I'm doing a little dance in the crisper.</p>
      <div class="time">Sunday 11:00 AM</div>
      <p class="msg in">🧾 BUD'S RESCUE RECEIPT
$15.46 KEPT OUT OF THE BIN</p>
      <div class="time">Example texts</div>
    </div>
  </section>

  <section class="steps" aria-label="How it works">
    <div class="step"><span class="num">1</span><b>Send a photo</b>Your fridge, your grocery haul, a Flashfood or Too Good To Go bag, or a receipt. Bud picks out what won't last.</div>
    <div class="step"><span class="num">2</span><b>Get one text a day</b>At 4:30 PM, only when something needs using. It comes with a dinner idea from what you have.</div>
    <div class="step"><span class="num">3</span><b>See what you kept</b>Every Sunday: a receipt of the food you rescued, in dollars, at the prices you paid.</div>
  </section>
  </main>

  <footer>
    <p>FridgeBud helps you plan what to use first. It never tells you a food is safe to eat: always check food yourself before you eat it. Storage times follow <a href="https://www.canada.ca/en/health-canada/services/general-food-safety-tips/safe-food-storage.html">Health Canada's fridge storage chart</a>.</p>
    <p>{contact}</p>
  </footer>
</div>
</body>
</html>"""

_THANKS = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Check your phone | FridgeBud</title>
<style>body{{margin:0;background:#f6f1e7;color:#17251c;font:19px/1.55 ui-rounded,system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:24px}}
.card{{background:#fff;border:1.5px solid #17251c;border-radius:18px;box-shadow:5px 5px 0 #1f6b46;padding:30px;max-width:26em}}h1{{margin:0 0 10px;letter-spacing:-.02em}}</style></head>
<body><div class="card"><h1>Check your phone.</h1>
<p>Bud just texted {phone}. <b>Reply YES</b> to that text to start. Then send a photo of your fridge or a receipt.</p>
<p style="font-size:15px;opacity:.8">No text within a minute? Check the number and <a href="/">try again</a>.</p></div></body></html>"""


def price_line(price: str, trial_days: int) -> str:
    if price:
        return f"Free for {trial_days} days, then {escape(price)} per household. Stop anytime."
    return f"Free for {trial_days} days. Stop anytime."


def landing(price: str = "", trial_days: int = 14, contact: str = "", error: str = "", phone: str = "", ref: str = "") -> str:
    return _PAGE.format(
        price_line=price_line(price, trial_days),
        error=f'<div class="error" role="alert">{escape(error)}</div>' if error else "",
        phone=escape(phone, quote=True), ref=escape(ref, quote=True),
        consent=escape(CONSENT_WORDING), contact=escape(contact) or "FridgeBud, Kitchener, Ontario, Canada",
    )


def thanks(phone: str) -> str:
    shown = f"({phone[2:5]}) {phone[5:8]}-{phone[8:]}" if len(phone) == 12 else phone
    return _THANKS.format(phone=escape(shown))
