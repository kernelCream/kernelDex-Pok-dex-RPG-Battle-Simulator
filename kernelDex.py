#!/usr/bin/env python3
"""
Terminal Pokédex (v7)
Requires: Python 3.8+  |  pip install Pillow
          Linux: mpv / ffplay / cvlc / aplay  for audio.

Controls: ←/→  A/D  navigate  |  ↑/↓  W  jump×10  (S opens Shop)
          /  search  |  G  go to ID  |  R  refresh  |  C  Cry
          B  Battle  |  X  Wild Encounter  |  S  Shop  |  Q  quit

v7 fixes vs v6:
  • Pokédex entry now shown in a second column to the RIGHT of the stats/
    abilities/moves stack when the terminal is wide enough (≥24 spare cols).
    On narrow terminals it moves BEFORE abilities so it is never buried at
    the bottom where it would be cut off when zoomed in.
  • Battle animations: projectile row moved from BY=10 (inside the log) to
    BY=4 (the HP-bar row) so attacks visually hit the opponent's health bar.
  • Battle log: player name highlighted in green, foe name in orange-red —
    makes each turn easy to read at a glance.
"""

import curses, json, urllib.request, subprocess, tempfile, os, threading, time
import textwrap, re, random
from typing import Optional

POKEAPI_BASE = "https://pokeapi.co/api/v2"
MAX_POKEMON  = 1025

# ─────────────────────────────────────────────────────────────────────────────
#  GAME STATE
# ─────────────────────────────────────────────────────────────────────────────
class TrainerState:
    def __init__(self):
        self.name          = "RED"
        self.level         = 1
        self.xp            = 0
        self.xp_next       = 100
        self.battles_won   = 0
        self.battles_lost  = 0
        self.caught        = set()
        self.seen          = set()
        self.streak        = 0
        self.best_streak   = 0
        self.money         = 500
        self.badges        = []
        self.total_battles = 0
        self.items = {
            "potion":       0,
            "full_restore": 0,
            "pp_restore":   0,
            "lure":         0,
        }
        self.lure_active = False
        self.lure_turns  = 0

    SHOP = [
        ("Potion",       "potion",       150, "Restores 40 HP in battle"),
        ("Full Restore", "full_restore", 450, "Fully restores HP in battle"),
        ("PP Restore",   "pp_restore",   200, "Restores all PP for one move"),
        ("Lure",         "lure",         120, "2× prize money next wild encounter"),
    ]

    def is_broke(self): return self.money < 0

    def add_xp(self, amount):
        self.xp += amount; leveled = False
        while self.xp >= self.xp_next:
            self.xp -= self.xp_next; self.level += 1
            self.xp_next = int(self.xp_next * 1.4); leveled = True
        return leveled

    def record_win(self, foe_pid, foe_name, foe_level):
        xp_gain = max(10, foe_level * 3 + random.randint(5, 20))
        leveled = self.add_xp(xp_gain)
        self.battles_won += 1; self.total_battles += 1
        self.streak += 1; self.best_streak = max(self.best_streak, self.streak)
        prize = max(50, foe_level * 12)
        if self.lure_active:
            prize *= 2; self.lure_turns -= 1
            if self.lure_turns <= 0: self.lure_active = False
        self.money += prize; self.seen.add(foe_pid)
        drop = random.random()
        item_drop = None
        if drop < 0.12:   item_drop = "potion";     self.items["potion"]     += 1
        elif drop < 0.16: item_drop = "pp_restore"; self.items["pp_restore"] += 1
        return xp_gain, prize, leveled, item_drop

    def record_loss(self):
        self.battles_lost += 1; self.total_battles += 1; self.streak = 0
        penalty = min(self.money + 200, max(80, self.level * 25))
        self.money -= penalty; return penalty

    def buy(self, key):
        for name, k, cost, _ in self.SHOP:
            if k == key:
                if self.money >= cost:
                    self.money -= cost; self.items[k] += 1; return True
                return False
        return False

    def use_item(self, key, mon, move_idx=0):
        if self.items.get(key, 0) <= 0: return "No items left!"
        self.items[key] -= 1
        if key == "potion":
            heal = min(40, mon["max_hp"] - mon["hp"]); mon["hp"] += heal
            return f"+{heal} HP restored!"
        elif key == "full_restore":
            heal = mon["max_hp"] - mon["hp"]
            mon["hp"] = mon["max_hp"]; mon["status"] = None
            return f"HP fully restored! (+{heal})"
        elif key == "pp_restore":
            mv = mon["moves"][move_idx] if mon["moves"] else None
            if mv: mv["pp"] = mv["max_pp"]; return f"{mv['name']} PP restored!"
            return "No move to restore."
        elif key == "lure":
            self.lure_active = True; self.lure_turns = 3
            return "Lure active! 2× money for 3 battles!"
        return "Nothing happened."

    def check_badges(self):
        new = []
        def _e(n):
            if n not in self.badges: self.badges.append(n); new.append(n)
        if self.battles_won >= 1:  _e("⚔  First Blood")
        if self.battles_won >= 5:  _e("🔥 Battle Veteran")
        if self.battles_won >= 10: _e("💎 Battle Master")
        if self.battles_won >= 25: _e("👑 Champion")
        if self.streak >= 3:       _e("🌊 On a Roll  (×3)")
        if self.streak >= 5:       _e("⚡ Hot Streak  (×5)")
        if self.streak >= 10:      _e("🌟 Unstoppable (×10)")
        if len(self.caught) >= 1:  _e("🎣 First Catch")
        if len(self.caught) >= 5:  _e("🎯 Collector")
        if len(self.caught) >= 10: _e("📖 Pokédex Pro")
        if self.level >= 5:        _e("📈 Rising Star")
        if self.level >= 10:       _e("🏆 Elite Trainer")
        if self.money >= 2000:     _e("💰 Money Bags")
        if self.money < 0:         _e("💸 Bankruptcy (ouch)")
        return new

TRAINER = TrainerState()

# ─────────────────────────────────────────────────────────────────────────────
#  LAYOUT
# ─────────────────────────────────────────────────────────────────────────────
SPRITE_W  = 32
SPRITE_H  = 13
PANEL_L_W = SPRITE_W + 8   # 40

# ─────────────────────────────────────────────────────────────────────────────
#  PALETTE
# ─────────────────────────────────────────────────────────────────────────────
C = dict(
    bg=234, border_dim=238, border_hi=67, gold=179, cyan=74, white=250,
    subtext=243, dex_num=246, stat_hi=71, stat_mid=179, stat_lo=167,
    bar_empty=236, nav_bg=235, nav_key=179, nav_text=243,
    search_bg=67, search_fg=255, error_flag=250, error_bg=131,
    pokeball_r=167, progress_fg=71, progress_bg=236,
    xp_fg=111, xp_bg=236, money=190, crit=220,
    broke=167, item=121, locked=238,
    # v7: separate player / foe name colours for battle log
    log_p1=120,   # bright green  — player
    log_p2=209,   # orange-red    — foe
    log_move=229, log_dmg=209,
)

TYPE_COLORS = {
    "normal":   (250,  59), "fire":     (222, 130),
    "water":    (255,  68), "electric": (235, 185),
    "grass":    (235,  65), "ice":      (235, 109),
    "fighting": (222,  88), "poison":   (222,  97),
    "ground":   (235, 137), "flying":   (235, 103),
    "psychic":  (222, 168), "bug":      (235,  64),
    "rock":     (222, 101), "ghost":    (222,  61),
    "dragon":   (222,  63), "steel":    (235, 102),
    "fairy":    (235, 175), "dark":     (222,  58),
}

P = dict(
    title=1, section=2, border_dim=3, border_hi=4, name=5, dex_num=6,
    white=7, subtext=8, stat_hi=9, stat_mid=10, stat_lo=11, bar_empty=12,
    nav_key=13, nav_text=14, search=15, error=16, ability=17, hidden_ab=18,
    pokeball_r=19, xp_bar=20, money=21, crit=22, broke=23, item=24,
    locked=25,
    log_p1=26,    # player name in log  (bright green)
    log_p2=27,    # foe name in log     (orange-red)
    log_move=28,  # move name in log    (yellow)
    log_dmg=29,   # damage number       (orange)
)

_SPRITE_PAIR_START = 30
_pair_cache: dict = {}
_next_pair: int   = _SPRITE_PAIR_START

def _reset_sprite_pairs():
    global _pair_cache, _next_pair
    _pair_cache = {}; _next_pair = _SPRITE_PAIR_START

def _alloc_pair(fg, bg):
    global _next_pair
    key = (fg, bg)
    if key in _pair_cache: return _pair_cache[key]
    limit = min(curses.COLOR_PAIRS - 1, 230)
    if _next_pair >= limit:
        return _pair_cache.get(next(iter(_pair_cache)), 1) if _pair_cache else 1
    try:
        curses.init_pair(_next_pair, fg, bg)
        _pair_cache[key] = _next_pair; _next_pair += 1
        return _next_pair - 1
    except curses.error: return 1

def init_colors():
    curses.start_color(); curses.use_default_colors()
    pairs = [
        (P["title"],      C["gold"],       C["border_dim"]),
        (P["section"],    C["gold"],       -1),
        (P["border_dim"], C["border_dim"], -1),
        (P["border_hi"],  C["border_hi"],  -1),
        (P["name"],       C["cyan"],       -1),
        (P["dex_num"],    C["dex_num"],    -1),
        (P["white"],      C["white"],      -1),
        (P["subtext"],    C["subtext"],    -1),
        (P["stat_hi"],    C["stat_hi"],    -1),
        (P["stat_mid"],   C["stat_mid"],   -1),
        (P["stat_lo"],    C["stat_lo"],    -1),
        (P["bar_empty"],  C["bar_empty"],  -1),
        (P["nav_key"],    C["nav_key"],    C["nav_bg"]),
        (P["nav_text"],   C["nav_text"],   C["nav_bg"]),
        (P["search"],     C["search_fg"],  C["search_bg"]),
        (P["error"],      C["error_flag"], C["error_bg"]),
        (P["ability"],    C["cyan"],       -1),
        (P["hidden_ab"],  C["subtext"],    -1),
        (P["pokeball_r"], C["pokeball_r"], -1),
        (P["xp_bar"],     C["xp_fg"],      -1),
        (P["money"],      C["money"],      -1),
        (P["crit"],       C["crit"],       -1),
        (P["broke"],      C["broke"],      -1),
        (P["item"],       C["item"],       -1),
        (P["locked"],     C["locked"],     -1),
        (P["log_p1"],     C["log_p1"],     -1),   # v7: green  for player name
        (P["log_p2"],     C["log_p2"],     -1),   # v7: orange for foe name
        (P["log_move"],   C["log_move"],   -1),
        (P["log_dmg"],    C["log_dmg"],    -1),
    ]
    for slot, fg, bg in pairs:
        try: curses.init_pair(slot, fg, bg)
        except curses.error: pass

# ─────────────────────────────────────────────────────────────────────────────
#  SPRITE RENDERING
# ─────────────────────────────────────────────────────────────────────────────
_CUBE = [0, 95, 135, 175, 215, 255]

def _nc(v):
    return min(range(6), key=lambda i: abs(_CUBE[i] - v))

def _rgb256(r, g, b):
    ri, gi, bi = _nc(r), _nc(g), _nc(b)
    cube_idx   = 16 + 36*ri + 6*gi + bi
    cube_err   = (_CUBE[ri]-r)**2 + (_CUBE[gi]-g)**2 + (_CUBE[bi]-b)**2
    luma       = r*0.299 + g*0.587 + b*0.114
    gg         = max(0, min(23, round((luma - 8) / 10)))
    gv         = 8 + 10*gg
    gray_err   = (gv-r)**2 + (gv-g)**2 + (gv-b)**2
    return (232 + gg) if gray_err < cube_err else cube_idx

def _enhance_image(img):
    try:
        from PIL import ImageEnhance, ImageFilter
        r, g, b, a = img.split()
        rgb = img.convert("RGB")
        rgb = ImageEnhance.Color(rgb).enhance(1.75)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.5)
        rgb = ImageEnhance.Sharpness(rgb).enhance(1.4)
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=0.8, percent=80, threshold=3))
        out = rgb.convert("RGBA"); out.putalpha(a); return out
    except Exception: return img

def render_sprite(img_data: bytes, w=SPRITE_W, h=SPRITE_H):
    from PIL import Image
    from io import BytesIO
    img  = Image.open(BytesIO(img_data)).convert("RGBA")
    bbox = img.getbbox()
    if bbox: img = img.crop(bbox)
    img  = _enhance_image(img)
    img  = img.resize((w, h*2), Image.Resampling.LANCZOS)
    *_, a = img.split()
    mask  = a.point(lambda p: 255 if p >= 128 else 0)
    img.putalpha(mask)
    rgb   = Image.new("RGB", img.size)
    rgb.paste(img.convert("RGB"), mask=mask)
    q      = rgb.quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=0).convert("RGB")
    q_rgba = q.convert("RGBA"); q_rgba.putalpha(mask)
    rows = []
    for ry in range(h):
        row = []
        for rx in range(w):
            t  = q_rgba.getpixel((rx, ry*2))
            b_ = q_rgba.getpixel((rx, ry*2+1))
            ta, ba = t[3], b_[3]
            if   ta==0 and ba==0: row.append((' ', 0))
            elif ta==0:           row.append(('▄', _alloc_pair(_rgb256(*b_[:3]), -1)))
            elif ba==0:           row.append(('▀', _alloc_pair(_rgb256(*t[:3]),  -1)))
            else:                 row.append(('▀', _alloc_pair(_rgb256(*t[:3]),  _rgb256(*b_[:3]))))
        rows.append(row)
    return rows

def ascii_sprite(pid):
    lines = [
        f"  ╔{'═'*28}╗  ",
        f"  ║  ✦  No.{pid:04d}{'':>16s}║  ",
        f"  ║{'':30s}║  ",
        f"  ║{'':8s}／￣￣＼{'':8s}║  ",
        f"  ║{'':7s}｜  ˘ω˘  ｜{'':7s}║  ",
        f"  ║{'':8s}＼＿＿／{'':8s}║  ",
        f"  ║{'':30s}║  ",
        f"  ║{'':7s}～～～～～～{'':7s}║  ",
        f"  ║{'':30s}║  ",
        f"  ╚{'═'*28}╝  ",
    ]
    while len(lines) < SPRITE_H: lines.append(f"  {'':30s}  ")
    return lines[:SPRITE_H]

# ─────────────────────────────────────────────────────────────────────────────
#  NETWORK
# ─────────────────────────────────────────────────────────────────────────────
def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "TermPokedex/7.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def fetch_pokemon(pid):
    try: return _get(f"{POKEAPI_BASE}/pokemon/{pid}")
    except Exception: return None

def fetch_species(pid):
    try: return _get(f"{POKEAPI_BASE}/pokemon-species/{pid}")
    except Exception: return None

def fetch_sprite(data):
    try:
        spr = data.get("sprites", {})
        url = (spr.get("other",{}).get("official-artwork",{}).get("front_default")
               or spr.get("front_default"))
        if not url: return None
        req = urllib.request.Request(url, headers={"User-Agent":"TermPokedex/7.0"})
        with urllib.request.urlopen(req, timeout=12) as r: return r.read()
    except Exception: return None

# ─────────────────────────────────────────────────────────────────────────────
#  SPECIES DATA
# ─────────────────────────────────────────────────────────────────────────────
def parse_species(data):
    if not data: return {}
    genus = ""
    for g in data.get("genera", []):
        if g.get("language",{}).get("name") == "en": genus = g.get("genus",""); break
    GBA = {"firered","leafgreen","emerald","ruby","sapphire"}
    flavor = ""
    for e in data.get("flavor_text_entries", []):
        if e.get("language",{}).get("name") != "en": continue
        raw  = e.get("flavor_text","")
        text = re.sub(r"\s+"," ", raw.replace("\n"," ").replace("\f"," ")).strip()
        ver  = e.get("version",{}).get("name","")
        if ver in GBA: flavor = text; break
        if not flavor: flavor = text
    return {
        "genus": genus, "flavor": flavor,
        "gen":   data.get("generation",{}).get("name",""),
        "gender_rate":  data.get("gender_rate", -1),
        "capture_rate": data.get("capture_rate",  0),
        "happiness":    data.get("base_happiness", 0),
    }

# ─────────────────────────────────────────────────────────────────────────────
#  AUDIO
# ─────────────────────────────────────────────────────────────────────────────
def play_cry(poke_data):
    if not poke_data: return
    cries   = poke_data.get("cries",{})
    cry_url = cries.get("latest") or cries.get("legacy")
    if not cry_url: return
    try:
        req = urllib.request.Request(cry_url, headers={"User-Agent":"TermPokedex/7.0"})
        with urllib.request.urlopen(req, timeout=8) as r: audio_data = r.read()
    except Exception: return
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg")
    try: tmp.write(audio_data); tmp.close()
    except Exception: tmp.close(); os.unlink(tmp.name); return
    def _cleanup(p):
        try: os.unlink(p)
        except: pass
    for cmd in [
        ["mpv","--no-video","--really-quiet",tmp.name],
        ["ffplay","-nodisp","-autoexit","-loglevel","quiet",tmp.name],
        ["cvlc","--play-and-exit","--quiet",tmp.name],
        ["aplay",tmp.name],
    ]:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            threading.Thread(target=lambda p=proc,f=tmp.name:(p.wait(),_cleanup(f)),
                             daemon=True).start()
            return
        except (FileNotFoundError,Exception): continue
    _cleanup(tmp.name)

# ─────────────────────────────────────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────────────────────────────────────
GEN_LABELS = {
    "generation-i":"GEN I","generation-ii":"GEN II","generation-iii":"GEN III",
    "generation-iv":"GEN IV","generation-v":"GEN V","generation-vi":"GEN VI",
    "generation-vii":"GEN VII","generation-viii":"GEN VIII","generation-ix":"GEN IX",
}
def gen_label(n): return GEN_LABELS.get(n, n.replace("-"," ").upper() if n else "???")
def gender_symbol(r):
    if r==-1: return "⚲ "
    if r==0:  return "♂ "
    if r==8:  return "♀ "
    return "♂♀"

def get_levelup_moves(poke, max_moves=6):
    GBA = {"firered-leafgreen","emerald","ruby-sapphire"}
    seen = {}
    for m in poke.get("moves",[]):
        name = m["move"]["name"].replace("-"," ").upper()
        best = None; is_gba = False
        for vgd in m.get("version_group_details",[]):
            if vgd["move_learn_method"]["name"] != "level-up": continue
            lv = vgd["level_learned_at"]; vg = vgd["version_group"]["name"]
            if best is None: best=lv; is_gba=vg in GBA
            elif vg in GBA and (not is_gba or lv<best): best=lv; is_gba=True
            elif not is_gba and lv<best: best=lv
        if best is not None:
            if name not in seen or best<seen[name]: seen[name]=best
    return sorted(seen.items(), key=lambda x:(x[1],x[0]))[:max_moves]

# ─────────────────────────────────────────────────────────────────────────────
#  UI PRIMITIVES
# ─────────────────────────────────────────────────────────────────────────────
def _add(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if y<0 or y>=h-1 or x>=w: return
    if x<0: text=text[-x:]; x=0
    ml = w-x-1
    if ml<=0: return
    try: win.addstr(y, x, text[:ml], attr)
    except curses.error: pass

def box(win, y, x, h, w, pair, title=""):
    a=curses.color_pair(pair); ta=a|curses.A_BOLD
    _add(win,y,x,"╔"+"═"*(w-2)+"╗",ta)
    _add(win,y+h-1,x,"╚"+"═"*(w-2)+"╝",ta)
    for r in range(y+1,y+h-1):
        _add(win,r,x,"║",a); _add(win,r,x+w-1,"║",a)
    if title: _add(win,y,x+2,f"  {title}  ",curses.color_pair(P["section"])|curses.A_BOLD)

def thin_box(win, y, x, h, w, pair, title=""):
    a=curses.color_pair(pair); ta=a|curses.A_BOLD
    _add(win,y,x,"┌"+"─"*(w-2)+"┐",ta)
    _add(win,y+h-1,x,"└"+"─"*(w-2)+"┘",ta)
    for r in range(y+1,y+h-1):
        _add(win,r,x,"│",a); _add(win,r,x+w-1,"│",a)
    if title: _add(win,y,x+2,f" {title} ",curses.color_pair(P["section"])|curses.A_BOLD)

def type_badge(win, y, x, ptype):
    fg,bg = TYPE_COLORS.get(ptype.lower(),(250,240))
    pair  = _alloc_pair(fg,bg)
    label = f" {ptype.upper()[:6]:^6s} "
    _add(win,y,x,label,curses.color_pair(pair)|curses.A_BOLD)
    return len(label)

def small_badge(win, y, x, text, fg, bg):
    pair=_alloc_pair(fg,bg); label=f" {text} "
    _add(win,y,x,label,curses.color_pair(pair)|curses.A_BOLD)
    return len(label)

STAT_BAR_W = 20
def stat_bar(win, y, x, label, val, bar_w=STAT_BAR_W):
    cp=P["stat_hi"] if val>=100 else (P["stat_mid"] if val>=60 else P["stat_lo"])
    f=max(1,round(val/255*bar_w)); e=bar_w-f
    _add(win,y,x,f"{label:3s}",curses.color_pair(P["subtext"]))
    _add(win,y,x+4,f"{val:3d}",curses.color_pair(cp)|curses.A_BOLD)
    _add(win,y,x+8,"█"*f,curses.color_pair(cp)|curses.A_BOLD)
    _add(win,y,x+8+f,"░"*e,curses.color_pair(P["bar_empty"]))

# ─────────────────────────────────────────────────────────────────────────────
#  TRAINER HUD
# ─────────────────────────────────────────────────────────────────────────────
def draw_trainer_hud(scr, W):
    xp_pct   = TRAINER.xp / max(1, TRAINER.xp_next)
    bar_w    = 8; filled = round(xp_pct*bar_w)
    streak_s = f"🔥×{TRAINER.streak}" if TRAINER.streak>=2 else ""
    broke_s  = " 💸BROKE!" if TRAINER.is_broke() else ""
    lure_s   = f" 🪝×{TRAINER.lure_turns}" if TRAINER.lure_active else ""
    hud = (f" {TRAINER.name} Lv{TRAINER.level:02d} "
           f"{'█'*filled}{'░'*(bar_w-filled)} "
           f"XP:{TRAINER.xp:>4d}/{TRAINER.xp_next:<4d} "
           f"W:{TRAINER.battles_won} ${TRAINER.money:>5d}"
           f"{broke_s}{lure_s}{streak_s} ")
    x    = max(42, W-len(hud)-1)
    attr = (curses.color_pair(P["broke"]) if TRAINER.is_broke()
            else curses.color_pair(P["title"])) | curses.A_BOLD
    _add(scr, 0, x, hud, attr)

# ─────────────────────────────────────────────────────────────────────────────
#  LOADING SCREEN
# ─────────────────────────────────────────────────────────────────────────────
_SPINNER    = ("◐","◓","◑","◒")
_spinner_idx = 0

def render_loading(scr, pid):
    global _spinner_idx
    H,W=scr.getmaxyx(); scr.erase()
    cx=W//2; cy=H//2-3
    title_a=curses.color_pair(P["title"])|curses.A_BOLD
    scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
    _add(scr,0,1,"  ◈  TERMINAL POKÉDEX  ·  GBA EDITION  ◈",title_a)
    draw_trainer_hud(scr,W)
    sep="─"*min(44,W-4); sep_x=cx-len(sep)//2
    _add(scr,cy,sep_x,sep,curses.color_pair(P["border_dim"]))
    _add(scr,cy+2,sep_x,sep,curses.color_pair(P["border_dim"]))
    spin=_SPINNER[_spinner_idx%len(_SPINNER)]; _spinner_idx+=1
    msg=f"  {spin}  Loading  #{pid:04d} of {MAX_POKEMON}  "
    _add(scr,cy+1,cx-len(msg)//2,msg,curses.color_pair(P["search"])|curses.A_BOLD)
    bar_w=min(44,W-8); filled=round(pid/MAX_POKEMON*bar_w)
    pf=_alloc_pair(C["progress_fg"],C["progress_bg"])
    _add(scr,cy+4,cx-bar_w//2,"█"*filled,curses.color_pair(pf))
    _add(scr,cy+4,cx-bar_w//2+filled,"░"*(bar_w-filled),curses.color_pair(P["bar_empty"]))
    ctr=f"{pid:04d}/{MAX_POKEMON}    {pid/MAX_POKEMON*100:.1f}%"
    _add(scr,cy+5,cx-len(ctr)//2,ctr,curses.color_pair(P["dex_num"]))
    scr.noutrefresh(); curses.doupdate()

# ─────────────────────────────────────────────────────────────────────────────
#  BARS
# ─────────────────────────────────────────────────────────────────────────────
def draw_type_banner(win, y, x, w, name, gender_sym, primary_type):
    fg,bg = TYPE_COLORS.get(primary_type.lower(),(250,240))
    pair  = _alloc_pair(fg,bg); attr=curses.color_pair(pair)|curses.A_BOLD
    inner = w-2; label=f" {name}  {gender_sym}"; pad=max(0,inner-len(label))
    _add(win,y,x+1,(label+" "*pad)[:inner],attr)

def draw_progress_bar(scr, y, pid, W):
    bar_w=max(10,W-22); filled=round(pid/MAX_POKEMON*bar_w)
    pf=_alloc_pair(C["progress_fg"],C["progress_bg"])
    _add(scr,y,1," ◎ ",curses.color_pair(P["pokeball_r"])|curses.A_BOLD)
    _add(scr,y,4,"█"*filled,curses.color_pair(pf))
    _add(scr,y,4+filled,"░"*(bar_w-filled),curses.color_pair(P["bar_empty"]))
    pct=f"{pid/MAX_POKEMON*100:.0f}%"
    _add(scr,y,4+bar_w+1,f" {pid:04d}/{MAX_POKEMON}  {pct} ",curses.color_pair(P["subtext"]))

def draw_xp_bar(scr, y, W):
    xp_pct=TRAINER.xp/max(1,TRAINER.xp_next)
    bar_w=max(10,W-30); filled=round(xp_pct*bar_w)
    pf=_alloc_pair(C["xp_fg"],C["xp_bg"]); label=f" Trainer Lv{TRAINER.level:02d} "
    _add(scr,y,1,label,curses.color_pair(P["xp_bar"])|curses.A_BOLD)
    x0=1+len(label)
    _add(scr,y,x0,"█"*filled,curses.color_pair(pf))
    _add(scr,y,x0+filled,"░"*(bar_w-filled),curses.color_pair(P["bar_empty"]))
    broke_warn="  💸 BROKE — win battles to restore funds!  " if TRAINER.is_broke() else ""
    right=broke_warn or f"  XP {TRAINER.xp}/{TRAINER.xp_next}  W:{TRAINER.battles_won} L:{TRAINER.battles_lost}  ${TRAINER.money}"
    attr=curses.color_pair(P["broke"] if TRAINER.is_broke() else P["subtext"])|curses.A_BOLD
    _add(scr,y,x0+bar_w+1,right,attr)

# ─────────────────────────────────────────────────────────────────────────────
#  SHOP SCREEN
# ─────────────────────────────────────────────────────────────────────────────
def show_shop(scr):
    scr.nodelay(False); scr.timeout(-1)
    cursor=0; message=""; shop=TRAINER.SHOP
    while True:
        H,W=scr.getmaxyx(); scr.erase()
        title_a=curses.color_pair(P["title"])|curses.A_BOLD
        scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
        _add(scr,0,1,"  🛒  ITEM SHOP  ·  POKE MART  ·  GBA EDITION",title_a)
        draw_trainer_hud(scr,W)
        bw=min(64,W-6); bx=W//2-bw//2
        thin_box(scr,2,bx,4+len(shop)+2,bw,P["border_hi"],"SHOP")
        _add(scr,3,bx+3,f"  {'ITEM':<16s}  {'COST':>6s}  {'HELD':>5s}  DESCRIPTION",
             curses.color_pair(P["subtext"])|curses.A_BOLD)
        _add(scr,4,bx+3,"─"*(bw-6),curses.color_pair(P["border_dim"]))
        for i,(name,key,cost,desc) in enumerate(shop):
            sel=i==cursor; held=TRAINER.items.get(key,0)
            attr=(curses.color_pair(P["search"])|curses.A_BOLD) if sel else curses.color_pair(P["white"])
            pfx="►" if sel else " "
            can_buy=TRAINER.money>=cost
            cost_a=(curses.color_pair(P["money"]) if can_buy else curses.color_pair(P["broke"]))|curses.A_BOLD
            _add(scr,5+i,bx+3,f"  {pfx} {name:<16s}",attr)
            _add(scr,5+i,bx+23,f" ${cost:>5d}",cost_a)
            _add(scr,5+i,bx+32,f"  ×{held:<3d}",curses.color_pair(P["item"])|curses.A_BOLD)
            _add(scr,5+i,bx+38,f"  {desc}",curses.color_pair(P["subtext"]))
        ny=5+len(shop)+2
        money_a=(curses.color_pair(P["broke"]) if TRAINER.is_broke() else curses.color_pair(P["money"]))|curses.A_BOLD
        _add(scr,ny,bx+3,
             f"  Wallet: ${TRAINER.money}   "
             f"{'  ⚠ NEGATIVE — win battles to earn money!' if TRAINER.is_broke() else ''}",
             money_a)
        tips=["💡 Potions let you last longer in battle → more XP per fight.",
              "💡 Full Restore turns a losing battle into a win → saves money.",
              "💡 PP Restore keeps your strongest move available every turn.",
              "💡 Lure doubles prize money for 3 encounters — best ROI when winning.",
              "💡 Winning battles is the only way out of negative balance!"]
        _add(scr,ny+2,bx+3,tips[int(time.time())%len(tips)][:bw-6],curses.color_pair(P["subtext"]))
        if message:
            _add(scr,ny+3,bx+3,f"  {message}",curses.color_pair(P["stat_hi"])|curses.A_BOLD)
        scr.hline(H-2,0,' ',W-1,curses.color_pair(P["nav_key"]))
        _add(scr,H-2,1,"  [↑↓] Select  │  [Enter] Buy  │  [Esc/Q] Leave  ",
             curses.color_pair(P["nav_key"])|curses.A_BOLD)
        scr.noutrefresh(); curses.doupdate()
        key=scr.getch()
        if   key==curses.KEY_UP:   cursor=(cursor-1)%len(shop); message=""
        elif key==curses.KEY_DOWN: cursor=(cursor+1)%len(shop); message=""
        elif key in (10,13):
            _,k,cost,_=shop[cursor]
            if TRAINER.money>=cost:
                TRAINER.buy(k); message=f"Bought {shop[cursor][0]}!  Wallet: ${TRAINER.money}"
            else:
                message=f"Not enough money! Need ${cost}, have ${TRAINER.money}."
        elif key in (27,ord('q'),ord('Q')): break
    scr.nodelay(True); scr.timeout(100)

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN RENDER FRAME
#
#  v7 change: Pokédex entry placement logic
#   • Wide terminal  (spare cols after stats ≥ 24): entry drawn in a SECOND
#     COLUMN to the right of the stats / abilities / moves column.
#   • Narrow terminal: entry drawn right after BASE STATS, BEFORE abilities
#     and moves, so it stays near the top and is not cut off when zoomed in.
# ─────────────────────────────────────────────────────────────────────────────
def render_frame(scr, poke, sprite, species_info, pid,
                 search_mode, search_buf, goto_mode, goto_buf, err):
    H,W    = scr.getmaxyx()
    RIGHT_X = PANEL_L_W + 1
    RIGHT_W = W - RIGHT_X - 1
    sp      = species_info or {}

    title_a = curses.color_pair(P["title"]) | curses.A_BOLD
    scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
    _add(scr,0,1,"  ◈  TERMINAL POKÉDEX  ·  GBA EDITION  ◈",title_a)
    draw_trainer_hud(scr,W)

    if pid in TRAINER.caught: _add(scr,0,42,"  ★ CAUGHT ",curses.color_pair(P["money"])|curses.A_BOLD)
    elif pid in TRAINER.seen: _add(scr,0,42,"  👁 SEEN  ",curses.color_pair(P["subtext"])|curses.A_BOLD)

    LP_H = H - 6
    box(scr,1,0,LP_H,PANEL_L_W,P["border_dim"])

    types        = [t["type"]["name"] for t in poke.get("types",[])] if poke else []
    primary_type = types[0] if types else "normal"
    poke_name    = poke.get("name","???").upper() if poke else "LOADING..."
    gsym         = gender_symbol(sp.get("gender_rate",-1))
    draw_type_banner(scr,2,0,PANEL_L_W,poke_name,gsym,primary_type)

    SX,SY = 4,3; max_spr_rows = max(2,LP_H-SY-1)
    if isinstance(sprite,list) and sprite:
        if isinstance(sprite[0],list):
            for ri,row in enumerate(sprite[:max_spr_rows]):
                for ci,(ch,pr) in enumerate(row):
                    _add(scr,SY+ri,SX+ci,ch,curses.color_pair(pr) if pr else 0)
        else:
            for i,ln in enumerate(sprite[:max_spr_rows]):
                _add(scr,SY+i,SX,ln,curses.color_pair(P["border_dim"]))

    if poke:
        panel_bot = 1 + LP_H - 1; info_y = panel_bot - 5
        if info_y >= SY+1:
            _add(scr,info_y,1,"╠"+"─"*(PANEL_L_W-2)+"╣",curses.color_pair(P["border_dim"]))
        info_y += 1
        _add(scr,info_y,SX,f"#{poke.get('id',pid):04d}",curses.color_pair(P["dex_num"])|curses.A_BOLD)
        bx2 = SX + 7
        for pt in types: bx2 += type_badge(scr,info_y,bx2,pt)
        genus = sp.get("genus","")
        if genus and info_y+1 < panel_bot:
            _add(scr,info_y+1,SX,genus[:PANEL_L_W-SX-2],curses.color_pair(P["subtext"])|curses.A_BOLD)
        if info_y+2 < panel_bot:
            hv=poke.get("height",0)/10; wv=poke.get("weight",0)/10
            _add(scr,info_y+2,SX,f"HT {hv:.1f}m  WT {wv:.1f}kg",curses.color_pair(P["subtext"]))
        if info_y+3 < panel_bot:
            gen=sp.get("gen",""); cap=sp.get("capture_rate",0)
            bxp=poke.get("base_experience") or 0; bx3=SX
            if gen:  bx3 += small_badge(scr,info_y+3,bx3,gen_label(gen),C["search_fg"],C["gold"])+1
            if cap:  bx3 += small_badge(scr,info_y+3,bx3,f"CAP {cap}",250,88)+1
            if bxp:  _add(scr,info_y+3,bx3,f"EXP {bxp}",curses.color_pair(P["subtext"]))

    div = curses.color_pair(P["border_dim"])
    for r in range(1,H-5): _add(scr,r,PANEL_L_W,"│",div)
    _add(scr,0,PANEL_L_W,"┬",div); _add(scr,H-6,PANEL_L_W,"┴",div)

    if RIGHT_W >= 20:
        STATS_W = min(RIGHT_W, 50)
        STATS_H = 12

        # ── v7: calculate whether a second column is available ─────────────
        #  Second column starts immediately after the stats column.
        #  We require at least 24 spare columns to draw a useful entry box.
        ENTRY_COL_W = RIGHT_W - STATS_W - 3   # spare cols to the right of stats
        USE_SECOND_COL = ENTRY_COL_W >= 24
        ENTRY_COL_X = RIGHT_X + STATS_W + 2   # col where second column begins

        box(scr,1,RIGHT_X,STATS_H,STATS_W,P["border_hi"],"BASE STATS")
        if poke:
            stats = {s["stat"]["name"]:s["base_stat"] for s in poke.get("stats",[])}
            order = [("hp","HP "),("attack","ATK"),("defense","DEF"),
                     ("special-attack","SpA"),("special-defense","SpD"),("speed","SPD")]
            bw = min(STATS_W-18, STAT_BAR_W)
            for si,(k,l) in enumerate(order):
                stat_bar(scr,3+si,RIGHT_X+2,l,stats.get(k,0),bar_w=bw)
            _add(scr,3+len(order)+1,RIGHT_X+2,f"{'TOTAL':>7s}  {sum(stats.values()):>3d}",
                 curses.color_pair(P["name"])|curses.A_BOLD)

        next_y = 1 + STATS_H + 1   # = 14

        if poke:
            flavor = sp.get("flavor","")

            # ── POKÉDEX ENTRY: second column (wide) or inline (narrow) ──────
            if flavor and USE_SECOND_COL:
                # Wide terminal → entry in its own column to the right of stats
                wrap_w   = max(12, ENTRY_COL_W - 4)
                e_lines  = textwrap.wrap(flavor, wrap_w)
                EV_H     = min(len(e_lines) + 2, H - 7)
                if EV_H >= 3:
                    thin_box(scr,1,ENTRY_COL_X,EV_H,ENTRY_COL_W,P["border_dim"],"POKÉDEX ENTRY")
                    ey = 2
                    for ln in e_lines:
                        if ey >= 1 + EV_H - 1: break
                        _add(scr,ey,ENTRY_COL_X+2,ln,curses.color_pair(P["subtext"]))
                        ey += 1

            elif flavor and not USE_SECOND_COL and H-next_y > 4 and RIGHT_W >= 24:
                # Narrow terminal → entry right after stats, BEFORE abilities/moves
                # so it is not buried at the bottom and cut off when zoomed in
                wrap_w  = max(12, STATS_W - 8)
                e_lines = textwrap.wrap(flavor, wrap_w)
                # Leave at least 8 rows for abilities + moves below
                EV_H    = min(len(e_lines) + 2, max(3, H - next_y - 8))
                if EV_H >= 3:
                    thin_box(scr,next_y,RIGHT_X,EV_H,STATS_W,P["border_dim"],"POKÉDEX ENTRY")
                    ey = next_y + 1
                    for ln in e_lines:
                        if ey >= next_y + EV_H - 1: break
                        _add(scr,ey,RIGHT_X+4,ln,curses.color_pair(P["subtext"]))
                        ey += 1
                    next_y += EV_H + 1

            # ── ABILITIES ──────────────────────────────────────────────────
            abilities = poke.get("abilities",[])
            if abilities:
                AB_H = min(len(abilities)+2, max(3,H-next_y-5))
                if AB_H >= 3:
                    thin_box(scr,next_y,RIGHT_X,AB_H,STATS_W,P["border_dim"],"ABILITIES")
                    ay = next_y + 1
                    for ab in abilities:
                        if ay >= next_y+AB_H-1: break
                        abn = ab["ability"]["name"].replace("-"," ").title()
                        hid = ab.get("is_hidden",False)
                        sym = "◆" if hid else "◇"; tag = "  (hidden)" if hid else ""
                        _add(scr,ay,RIGHT_X+2,f"  {sym} {abn}{tag}",
                             curses.color_pair(P["hidden_ab"] if hid else P["ability"]))
                        ay += 1
                    next_y += AB_H + 1

            # ── MOVES ──────────────────────────────────────────────────────
            if H - next_y > 4:
                moves = get_levelup_moves(poke, max_moves=6)
                MV_H  = min(len(moves)+2, H-next_y-4)
                if MV_H >= 3 and moves:
                    thin_box(scr,next_y,RIGHT_X,MV_H,STATS_W,P["border_dim"],"MOVES (LEVEL-UP)")
                    my = next_y + 1
                    for mv_name,lv in moves:
                        if my >= next_y+MV_H-1: break
                        lv_str = f"Lv.{lv:3d}" if lv>0 else "  ─  "
                        _add(scr,my,RIGHT_X+2,f" {lv_str}  {mv_name:<22s}",
                             curses.color_pair(P["white"]))
                        my += 1
                    next_y += MV_H + 1

            # (Pokédex entry is now either in the second column or placed
            #  before abilities — it is no longer at the bottom of the stack)

    if err and not search_mode and not goto_mode:
        _add(scr,H-6,2,f"  ✖  {err}  ",curses.color_pair(P["error"])|curses.A_BOLD)
    draw_progress_bar(scr,H-5,pid,W)
    draw_xp_bar(scr,H-4,W)
    if search_mode:
        _add(scr,H-6,2,f"  🔍 Search ▶ {search_buf}█  ",curses.color_pair(P["search"])|curses.A_BOLD)
    elif goto_mode:
        _add(scr,H-6,2,f"  # Go to ID ▶ {goto_buf}█  ",curses.color_pair(P["search"])|curses.A_BOLD)

    scr.hline(H-3,0,' ',W-1,curses.color_pair(P["nav_key"]))
    nx=0
    def nav_seg(x,k,d):
        _add(scr,H-3,x,f" {k} ",curses.color_pair(P["nav_key"])|curses.A_BOLD)
        _add(scr,H-3,x+len(k)+2,f"{d} │ ",curses.color_pair(P["nav_text"]))
    for k,d in [("←/→","Nav"),("↑/↓W","×10"),("[/]","Find"),("[G]","ID"),
                ("[R]","Refresh"),("[C]","Cry"),("[B]","Battle"),
                ("[X]","Wild"),("[S]","Shop"),("[Q]","Quit")]:
        nav_seg(nx,k,d); nx+=len(k)+len(d)+6

# ═════════════════════════════════════════════════════════════════════════════
#  GAME LOOP EVENTS
# ═════════════════════════════════════════════════════════════════════════════
def show_notification(scr, lines, color_pair=None, wait=1.8):
    H,W=scr.getmaxyx(); pair=color_pair or P["search"]
    max_w=max(len(l) for l in lines)+6; box_h=len(lines)+6
    bx=max(0,W//2-max_w//2); by=max(0,H//2-box_h//2)
    thin_box(scr,by,bx,box_h,max_w,P["border_hi"])
    for i,line in enumerate(lines):
        _add(scr,by+2+i,bx+3,line,curses.color_pair(pair)|curses.A_BOLD)
    _add(scr,by+box_h-2,bx+2,"  Press any key to continue...  ",curses.color_pair(P["subtext"]))
    scr.noutrefresh(); curses.doupdate()
    scr.nodelay(False); scr.getch()
    scr.nodelay(True); scr.timeout(100)

def show_badge_unlocked(scr, n):
    show_notification(scr,["  🏅 ACHIEVEMENT UNLOCKED!  ",f"    {n}    ","","  Keep battling to earn more!"],P["money"])

def show_level_up(scr, lv):
    show_notification(scr,["  ⬆  TRAINER LEVEL UP!  ",f"    ★  Now Level {lv}  ★    ","","  Your skills are growing!"],P["stat_hi"])

def show_item_drop(scr, item_name):
    show_notification(scr,["  📦 ITEM DROPPED!  ",f"    Found a {item_name}!  ","","  Check your bag at the Shop."],P["item"])

def show_broke_warning(scr):
    show_notification(scr,["  💸  YOU'RE BROKE!  ","",
                            "  In battle, only your first move is available.",
                            "  Win fights to earn prize money.",
                            "  Visit the Shop [S] to spend wisely."],P["broke"])

def show_catch_sequence(scr, mon_name, success):
    H,W=scr.getmaxyx(); cx=W//2; cy=H//2
    frames_throw=[f"  ◎{'':>{i*3}}  " for i in range(1,7)]
    frames_shake=["  ◉  (shake...)  ","  ◉  (shake!!!!) "]
    thin_box(scr,cy-3,cx-20,8,40,P["border_hi"],"POKÉ BALL!")
    for f in frames_throw:
        _add(scr,cy,cx-10,f+" "*20,curses.color_pair(P["pokeball_r"])|curses.A_BOLD)
        scr.noutrefresh(); curses.doupdate(); time.sleep(0.09)
    for f in frames_shake*2:
        _add(scr,cy,cx-10,f+" "*20,curses.color_pair(P["stat_mid"])|curses.A_BOLD)
        scr.noutrefresh(); curses.doupdate(); time.sleep(0.22)
    msg=(f"  ★ Gotcha! {mon_name[:12]} caught!  " if success
         else f"  Oh no! {mon_name[:12]} broke free!  ")
    _add(scr,cy,cx-10,msg,curses.color_pair(P["money"] if success else P["stat_lo"])|curses.A_BOLD)
    _add(scr,cy+2,cx-10,"  Press any key...",curses.color_pair(P["subtext"]))
    scr.noutrefresh(); curses.doupdate()

def wild_encounter(scr, poke_cache, img_cache, sprite_cache, species_cache, current_pid):
    H,W=scr.getmaxyx()
    max_id=min(MAX_POKEMON,151+(TRAINER.level-1)*40)
    wild_pid=random.randint(1,max(1,max_id))
    scr.erase()
    title_a=curses.color_pair(P["title"])|curses.A_BOLD
    scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
    _add(scr,0,1,"  ⚡ WILD ENCOUNTER!",title_a)
    grass="〰"*(W//2)
    _add(scr,H//2-2,4,grass,curses.color_pair(P["stat_hi"]))
    _add(scr,H//2,4,f"  A wild Pokémon appeared!  Loading #{wild_pid:04d}...",
         curses.color_pair(P["search"])|curses.A_BOLD)
    scr.noutrefresh(); curses.doupdate()

    if wild_pid not in poke_cache:
        data=fetch_pokemon(wild_pid)
        if data: poke_cache[wild_pid]=data
    wild_data=poke_cache.get(wild_pid)
    if not wild_data: return current_pid
    TRAINER.seen.add(wild_pid)

    if current_pid not in poke_cache:
        data=fetch_pokemon(current_pid)
        if data: poke_cache[current_pid]=data
    player_data=poke_cache.get(current_pid)
    if not player_data: return current_pid

    wild_name=wild_data.get("name","???").upper()
    scr.erase()
    scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
    _add(scr,0,1,"  ⚡ WILD ENCOUNTER!",title_a)
    _add(scr,H//2-2,4,grass,curses.color_pair(P["stat_hi"]))
    for i in range(5):
        msg=f"  A wild {wild_name} appeared!  "+"!"*i
        _add(scr,H//2,4,msg+" "*10,curses.color_pair(P["search"])|curses.A_BOLD)
        scr.noutrefresh(); curses.doupdate(); time.sleep(0.2)
    time.sleep(0.4)

    result=run_battle(scr,player_data,wild_data,is_wild=True,
                      poke_cache=poke_cache,sprite_cache=sprite_cache)

    if result=="win":
        foe_level=max(5,sum(s["base_stat"] for s in wild_data.get("stats",[]))//30)
        xp_gain,money,leveled,item_drop=TRAINER.record_win(wild_pid,wild_name,foe_level)
        new_badges=TRAINER.check_badges()
        scr.erase()
        scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
        _add(scr,0,1,"  ⚡ WILD ENCOUNTER RESULT!",title_a)
        draw_trainer_hud(scr,W)
        thin_box(scr,H//2-5,W//2-22,12,44,P["border_hi"],"BATTLE RESULT")
        _add(scr,H//2-3,W//2-18,f"  ★ You defeated {wild_name}!",
             curses.color_pair(P["stat_hi"])|curses.A_BOLD)
        _add(scr,H//2-1,W//2-18,f"  +{xp_gain} XP gained!",
             curses.color_pair(P["xp_bar"])|curses.A_BOLD)
        _add(scr,H//2,W//2-18,
             f"  +${money} prize money!"+(" (×2 LURE!)" if TRAINER.lure_active else ""),
             curses.color_pair(P["money"])|curses.A_BOLD)
        if item_drop:
            _add(scr,H//2+1,W//2-18,f"  📦 Item drop: {item_drop}!",
                 curses.color_pair(P["item"])|curses.A_BOLD)
        if TRAINER.streak>=2:
            _add(scr,H//2+2,W//2-18,f"  🔥 Win streak: ×{TRAINER.streak}!",
                 curses.color_pair(P["pokeball_r"])|curses.A_BOLD)
        try:
            sp_data=fetch_species(wild_pid)
            cap_rate=sp_data.get("capture_rate",128) if sp_data else 128
        except Exception: cap_rate=128
        _add(scr,H//2+3,W//2-18,"  [C] Try to catch!   [Any key] Continue",
             curses.color_pair(P["subtext"])|curses.A_BOLD)
        scr.noutrefresh(); curses.doupdate()
        key=scr.getch()
        if key in (ord('c'),ord('C')):
            catch_chance=cap_rate/255*(1+TRAINER.level*0.02)
            caught=random.random()<min(0.95,catch_chance)
            show_catch_sequence(scr,wild_name,caught)
            if caught: TRAINER.caught.add(wild_pid); new_badges=TRAINER.check_badges()
            scr.getch()
        if leveled:   show_level_up(scr,TRAINER.level)
        if item_drop: show_item_drop(scr,item_drop)
        for badge in new_badges: show_badge_unlocked(scr,badge)

    elif result=="loss":
        penalty=TRAINER.record_loss(); new_badges=TRAINER.check_badges()
        scr.erase()
        scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
        _add(scr,0,1,"  ⚡ WILD ENCOUNTER RESULT!",title_a)
        thin_box(scr,H//2-4,W//2-22,10,44,P["border_dim"],"BATTLE RESULT")
        _add(scr,H//2-2,W//2-18,f"  You were defeated by {wild_name}...",
             curses.color_pair(P["stat_lo"])|curses.A_BOLD)
        _add(scr,H//2,W//2-18,f"  -${penalty} lost.  Balance: ${TRAINER.money}",
             curses.color_pair(P["subtext"]))
        if TRAINER.is_broke():
            _add(scr,H//2+1,W//2-18,"  ⚠ BROKE! Moves are locked until balance > $0.",
                 curses.color_pair(P["broke"])|curses.A_BOLD)
        _add(scr,H//2+2,W//2-18,"  Press any key to return...",curses.color_pair(P["dex_num"]))
        scr.noutrefresh(); curses.doupdate(); scr.getch()
        if TRAINER.is_broke(): show_broke_warning(scr)
        for badge in new_badges: show_badge_unlocked(scr,badge)
    return current_pid

# ═════════════════════════════════════════════════════════════════════════════
#  BATTLE SYSTEM
# ═════════════════════════════════════════════════════════════════════════════
TYPE_CHART = {
    "normal":   {"rock":0.5,"ghost":0.0,"steel":0.5},
    "fire":     {"fire":0.5,"water":0.5,"grass":2.0,"ice":2.0,"bug":2.0,"rock":0.5,"dragon":0.5,"steel":2.0},
    "water":    {"fire":2.0,"water":0.5,"grass":0.5,"ground":2.0,"rock":2.0,"dragon":0.5},
    "electric": {"water":2.0,"electric":0.5,"grass":0.5,"ground":0.0,"flying":2.0,"dragon":0.5},
    "grass":    {"fire":0.5,"water":2.0,"grass":0.5,"poison":0.5,"ground":2.0,"flying":0.5,"bug":0.5,"rock":2.0,"dragon":0.5,"steel":0.5},
    "ice":      {"water":0.5,"grass":2.0,"ice":0.5,"ground":2.0,"flying":2.0,"dragon":2.0,"steel":0.5},
    "fighting": {"normal":2.0,"ice":2.0,"poison":0.5,"flying":0.5,"psychic":0.5,"bug":0.5,"rock":2.0,"ghost":0.0,"dark":2.0,"steel":2.0,"fairy":0.5},
    "poison":   {"grass":2.0,"poison":0.5,"ground":0.5,"rock":0.5,"ghost":0.5,"steel":0.0,"fairy":2.0},
    "ground":   {"fire":2.0,"electric":2.0,"grass":0.5,"poison":2.0,"flying":0.0,"bug":0.5,"rock":2.0,"steel":2.0},
    "flying":   {"electric":0.5,"grass":2.0,"fighting":2.0,"bug":2.0,"rock":0.5,"steel":0.5},
    "psychic":  {"fighting":2.0,"poison":2.0,"psychic":0.5,"dark":0.0,"steel":0.5},
    "bug":      {"fire":0.5,"grass":2.0,"fighting":0.5,"poison":0.5,"flying":0.5,"psychic":2.0,"ghost":0.5,"dark":2.0,"steel":0.5,"fairy":0.5},
    "rock":     {"fire":2.0,"ice":2.0,"fighting":0.5,"ground":0.5,"flying":2.0,"bug":2.0,"steel":0.5},
    "ghost":    {"normal":0.0,"psychic":2.0,"ghost":2.0,"dark":0.5},
    "dragon":   {"dragon":2.0,"steel":0.5,"fairy":0.0},
    "dark":     {"fighting":0.5,"psychic":2.0,"ghost":2.0,"dark":0.5,"fairy":0.5},
    "steel":    {"fire":0.5,"water":0.5,"electric":0.5,"ice":2.0,"rock":2.0,"steel":0.5,"fairy":2.0},
    "fairy":    {"fire":0.5,"fighting":2.0,"poison":0.5,"dragon":2.0,"dark":2.0,"steel":0.5},
}

def _type_mult(move_type,def_types):
    mult=1.0; chart=TYPE_CHART.get(move_type,{})
    for dt in def_types: mult*=chart.get(dt,1.0)
    return mult

def _make_battle_mon(poke_data):
    raw={s["stat"]["name"]:s["base_stat"] for s in poke_data.get("stats",[])}
    lv,iv=50,15
    def sc(b): return max(1,int((2*b+iv)*lv/100+5))
    max_hp=int((2*raw.get("hp",45)+iv)*lv/100+lv+10)
    return {
        "id":poke_data["id"],"name":poke_data["name"].upper(),
        "types":[t["type"]["name"] for t in poke_data.get("types",[])],
        "max_hp":max_hp,"hp":max_hp,
        "atk":sc(raw.get("attack",50)),"def_":sc(raw.get("defense",50)),
        "spa":sc(raw.get("special-attack",50)),"spd":sc(raw.get("special-defense",50)),
        "spe":sc(raw.get("speed",50)),"moves":[],"status":None,"sleep_turns":0,
    }

def _fetch_move_detail(raw_name):
    try:
        d=_get(f"{POKEAPI_BASE}/move/{raw_name}",timeout=6)
        return {"name":raw_name.replace("-"," ").upper(),"power":d.get("power") or 0,
                "type":d.get("type",{}).get("name","normal"),"pp":d.get("pp",10),
                "max_pp":d.get("pp",10),"acc":d.get("accuracy") or 100,
                "class":d.get("damage_class",{}).get("name","physical")}
    except Exception:
        return {"name":raw_name.replace("-"," ").upper(),"power":0,"type":"normal",
                "pp":10,"max_pp":10,"acc":100,"class":"physical"}

def _load_battle_moves(poke_data,scr=None,label=""):
    candidates=get_levelup_moves(poke_data,max_moves=20)
    fetched=[]; damaging_n=0
    for mv_name,_ in candidates:
        raw=mv_name.lower().replace(" ","-")
        if scr:
            H,W=scr.getmaxyx()
            _add(scr,H//2+2,4,f"  ⚡ Loading {label}: {mv_name[:24]:<24s} ...",
                 curses.color_pair(P["search"])|curses.A_BOLD)
            scr.noutrefresh(); curses.doupdate()
        md=_fetch_move_detail(raw); fetched.append(md)
        if md["power"]>0: damaging_n+=1
        if damaging_n>=4 and len(fetched)>=4: break
        if len(fetched)>=12: break
    damaging=[m for m in fetched if m["power"]>0][:4]
    status_=[m for m in fetched if m["power"]==0]
    return (damaging+status_[:max(0,4-len(damaging))])[:4]

def _apply_status(defender,move,log):
    if defender["status"]: return
    typ=move.get("type","normal"); roll=random.random()
    if   typ=="fire"     and roll<0.10: defender["status"]="burn";    log.append(f"  {defender['name']} was burned!")
    elif typ=="electric" and roll<0.10: defender["status"]="paralyze";log.append(f"  {defender['name']} is paralyzed!")
    elif typ=="poison"   and roll<0.15: defender["status"]="poison";  log.append(f"  {defender['name']} was poisoned!")
    elif typ=="ice"      and roll<0.10: defender["status"]="freeze";  log.append(f"  {defender['name']} was frozen!")
    elif typ=="psychic"  and roll<0.10: defender["status"]="confuse"; log.append(f"  {defender['name']} is confused!")

def _process_status_start(mon,log):
    if not mon["status"]: return False
    if mon["status"]=="sleep":
        mon["sleep_turns"]-=1
        if mon["sleep_turns"]<=0: mon["status"]=None; log.append(f"  {mon['name']} woke up!"); return False
        log.append(f"  {mon['name']} is fast asleep..."); return True
    elif mon["status"]=="paralyze" and random.random()<0.25:
        log.append(f"  {mon['name']} is paralyzed! Can't move!"); return True
    elif mon["status"]=="freeze":
        if random.random()<0.8: log.append(f"  {mon['name']} is frozen solid!"); return True
        mon["status"]=None; log.append(f"  {mon['name']} thawed out!"); return False
    return False

def _process_status_end(mon,log):
    if mon["status"]=="burn":
        dmg=max(1,mon["max_hp"]//16); mon["hp"]=max(0,mon["hp"]-dmg)
        log.append(f"  {mon['name']} hurt by burn! (-{dmg} HP)")
    elif mon["status"]=="poison":
        dmg=max(1,mon["max_hp"]//8); mon["hp"]=max(0,mon["hp"]-dmg)
        log.append(f"  {mon['name']} hurt by poison! (-{dmg} HP)")

STATUS_SYMBOLS={"burn":"🔥BRN","paralyze":"⚡PAR","poison":"☠PSN",
                "freeze":"❄FRZ","sleep":"💤SLP","confuse":"😵CNF"}

def _execute_attack(attacker,defender,move,log):
    if _process_status_start(attacker,log): return False
    log.append(f"  {attacker['name']} used {move['name']}!")
    if move["power"]==0: log.append("  (Status move — no direct damage.)"); return False
    acc=move["acc"]
    if attacker.get("status")=="paralyze": acc=int(acc*0.75)
    if random.randint(1,100)>acc: log.append(f"  {attacker['name']}'s attack missed!"); return False
    is_crit=random.random()<0.0625; crit_mul=1.5 if is_crit else 1.0
    cls=move["class"]
    if cls=="physical":
        a,d=attacker["atk"],defender["def_"]
        if attacker.get("status")=="burn": a=int(a*0.5)
    elif cls=="special": a,d=attacker["spa"],defender["spd"]
    else: log.append("  (No effect.)"); return False
    base=(2*50/5+2)*move["power"]*a/max(1,d)/50+2
    if move["type"] in attacker["types"]: base*=1.5
    eff=_type_mult(move["type"],defender["types"])
    base*=eff*crit_mul*random.randint(85,100)/100
    dmg=max(1,int(base)); defender["hp"]=max(0,defender["hp"]-dmg)
    if is_crit: log.append("  ★ Critical hit!")
    if   eff==0.0: log.append("  It had no effect!")
    elif eff>=4.0: log.append("  It's super effective!!")
    elif eff>=2.0: log.append("  It's super effective!")
    elif eff< 1.0: log.append("  It's not very effective...")
    log.append(f"  {defender['name']} took {dmg} dmg  ({defender['hp']}/{defender['max_hp']} HP)")
    _apply_status(defender,move,log)
    _process_status_end(attacker,log)
    return defender["hp"]<=0

# ─────────────────────────────────────────────────────────────────────────────
#  BATTLE ANIMATIONS
#
#  v7 fix: BY changed from 10 → 4.
#   Row 10 was inside the battle log area, so projectiles flew through the
#   log text instead of the battle panels.
#   BY=4 is the HP-bar row of the 7-row mon panels (rows 1-7), so projectiles
#   now visually travel across both HP bars before impacting the opponent.
# ─────────────────────────────────────────────────────────────────────────────
_IMPACTS = {
    "normal":  [(0," ╳ ╳ ╳ ")],
    "fire":    [(-1,"  ▲▲▲  "),(0," ▲███▲ "),(1,"  ▲▲▲  ")],
    "water":   [(-1,"  ~~~  "),(0," ≈≈≈≈≈ "),(1,"  ~~~  ")],
    "electric":[(-1," ╲★╱★╲ "),(0,"★╱  ╲★"),(1," ╲★╱★╲ ")],
    "grass":   [(-1," ✿ ♣ ✿ "),(0," ♣✿✿✿♣ "),(1," ✿ ♣ ✿ ")],
    "ice":     [(-1,"  ✦·✦  "),(0," ✦❄❄❄✦ "),(1,"  ✦·✦  ")],
    "fighting":[(0," ╲╱╲╱╲ ")],
    "poison":  [(-1,"  ○●○  "),(0," ●○○○● "),(1,"  ○●○  ")],
    "ground":  [(0," ░▒▓▒░ "),(1," ▒▓██▓ ")],
    "flying":  [(-1," ∿∿∿∿∿ "),(0,"~~~~~~~"),(1," ∿∿∿∿∿ ")],
    "psychic": [(-1,"  ◇○◇  "),(0," ◉○◉○◉ "),(1,"  ◇○◇  ")],
    "bug":     [(-1," · • · "),(0," •···• "),(1," · • · ")],
    "rock":    [(-1," ◈ · ◈ "),(0," ◇◈◈◈◇ "),(1," ◈ · ◈ ")],
    "ghost":   [(-1,"  ▒░▒  "),(0," ▓░▒░▓ "),(1,"  ▒░▒  ")],
    "dragon":  [(-1," ═════ "),(0,"►══════"),(1," ═════ ")],
    "dark":    [(-1,"  ▓█▓  "),(0," █████ "),(1,"  ▓█▓  ")],
    "steel":   [(-1," ─◇─◇─ "),(0," ◆───◆ "),(1," ─◇─◇─ ")],
    "fairy":   [(-1," ✧✦✧✦✧ "),(0," ✦✧✦✧✦ "),(1," ✧✦✧✦✧ ")],
}

def _animate_attack(scr, attacker, defender, move, is_player):
    H,W = scr.getmaxyx()
    if H<8 or W<20: return
    half = max(22,(W-3)//2)

    # ── v7: BY=4 aligns animation with the HP-bar row in the 7-row panels ──
    BY  = 4
    TOP = BY - 1   # row 3 — type-badge row
    BOT = BY + 1   # row 5 — stats row

    mv_type = move.get("type","normal")
    fg,_    = TYPE_COLORS.get(mv_type,(250,240))
    tp      = _alloc_pair(fg,-1)
    HI  = curses.color_pair(tp) | curses.A_BOLD
    DIM = curses.color_pair(tp)

    if is_player: sx,ex,step,imp_x = 5, half+half//2, 2, half+4
    else:         sx,ex,step,imp_x = W-7, half//2, -2, 4
    positions = list(range(sx,ex,step))

    def ref(): scr.noutrefresh(); curses.doupdate()
    def clr(y,x,n=1):
        n=max(0,min(n,W-x-1))
        if n>0: _add(scr,y,max(1,x)," "*n,0)
    def clamp(x): return max(1,min(x,W-2))
    def safe(y):  return 2<=y<=H-2

    lbl=f"  {move['name'][:20]}  "; lx=max(1,W//2-len(lbl)//2)
    _add(scr,BY,lx,lbl,HI); ref(); time.sleep(0.24)
    clr(BY,lx,len(lbl)); ref(); time.sleep(0.04)

    if move["power"]==0:
        cx=W//2
        for sym,dy in [("·",0),("○",0),("◉",0),("○",-1),("◉",-1),("·",0),(" ",0)]:
            ry=BY+dy
            if safe(ry): clr(ry,cx-2,5); _add(scr,ry,cx-1,sym,HI)
            ref(); time.sleep(0.10)
        for dy in (0,-1):
            if safe(BY+dy): clr(BY+dy,cx-2,5)
        ref(); return

    drawn=[]
    def erase_drawn(keep=0):
        for ry,rx in drawn[:max(0,len(drawn)-keep)]: clr(ry,rx)
        drawn.clear()

    if mv_type=="electric":
        bolt=[]
        for i,pos in enumerate(positions):
            px=clamp(pos); ry=TOP if (i%4<2) else BY
            ch=("╱" if is_player else "╲") if ry==TOP else ("╲" if is_player else "╱")
            if safe(ry): _add(scr,ry,px,ch,HI); bolt.append((ry,px,ch))
            ref(); time.sleep(0.011)
        for _ in range(3):
            for ry,px,_ in bolt: _add(scr,ry,px,"★",HI)
            ref(); time.sleep(0.065)
            for ry,px,ch in bolt: _add(scr,ry,px,ch,DIM)
            ref(); time.sleep(0.045)
        for ry,px,_ in bolt: clr(ry,px)
        ref()
    elif mv_type=="dragon":
        beam=[]
        for i,pos in enumerate(positions):
            px=clamp(pos); _add(scr,BY,px,"►" if i==len(positions)-1 else "═",HI); beam.append(px)
            ref(); time.sleep(0.011)
        for _ in range(3):
            for bpx in beam: _add(scr,BY,bpx,"▶",HI)
            ref(); time.sleep(0.065)
            for bpx in beam: _add(scr,BY,bpx,"═",DIM)
            ref(); time.sleep(0.045)
        for bpx in beam: clr(BY,bpx)
        ref()
    else:
        CHARS={"fire":["▲","▲","△"],"water":["≈","~"],"psychic":["◉","○"],
               "ghost":["░","▒","▓"],"grass":["♣","✿"],"ice":["✦","❄"],
               "dark":["▌","█","▐"],"fairy":["✦","✧"],"steel":["◇","◆","►"],
               "rock":["◈","◇"],"ground":["░","▒","▓"],"flying":["~","∿"],
               "fighting":["●","◉"],"bug":["•","·"],"poison":["○","●"]}
        OFFS={"water":[0,-1,0,1],"flying":[0,-1,0,1],"poison":[0,-1,0,0]}
        ch_list=CHARS.get(mv_type,["●","─"])
        offs=OFFS.get(mv_type,[0]*100)
        for i,pos in enumerate(positions):
            px=clamp(pos); dy=offs[i%len(offs)] if offs else 0
            ry=BY+dy
            if not safe(ry): ry=BY
            _add(scr,ry,px,ch_list[i%len(ch_list)],HI); drawn.append((ry,px))
            if len(drawn)>=2: pry,ppx=drawn[-2]; _add(scr,pry,ppx,"─",DIM)
            if len(drawn)>8:  ry2,rx2=drawn[-9]; clr(ry2,rx2)
            ref(); time.sleep(0.018)
        erase_drawn(); ref()

    impact_rows=_IMPACTS.get(mv_type,_IMPACTS["normal"])
    ix=max(1,min(imp_x,W-10))
    for flash in range(3):
        for dy,text in impact_rows:
            ry=BY+dy
            if safe(ry): _add(scr,ry,ix,text,HI if flash%2==0 else DIM)
        ref(); time.sleep(0.085)
        for dy,text in impact_rows:
            ry=BY+dy
            if safe(ry): clr(ry,ix,len(text))
        ref(); time.sleep(0.038)
    hit_x=max(1,(half+3) if is_player else 3)
    _add(scr,BY,hit_x,"  ─ HIT ─  ",curses.color_pair(P["stat_lo"])|curses.A_BOLD)
    ref(); time.sleep(0.115)
    clr(BY,hit_x,11); ref()

# ─────────────────────────────────────────────────────────────────────────────
#  COLOURED BATTLE LOG LINE
#
#  v7 change: player (p1) name shown in bright green (P["log_p1"]),
#             foe (p2) name shown in orange-red (P["log_p2"]).
#             Move names stay yellow; damage numbers stay orange-red.
# ─────────────────────────────────────────────────────────────────────────────
def _log_base_attr(line):
    lo=line.lower()
    if "fainted" in lo or "battle start" in lo: return curses.color_pair(P["title"])|curses.A_BOLD
    if "super effective!!" in lo: return curses.color_pair(P["stat_hi"])|curses.A_BOLD
    if "super effective!" in lo:  return curses.color_pair(P["stat_hi"])|curses.A_BOLD
    if "not very effective" in lo: return curses.color_pair(P["subtext"])
    if "no effect" in lo or "missed" in lo: return curses.color_pair(P["stat_lo"])
    if "critical" in lo: return curses.color_pair(P["crit"])|curses.A_BOLD
    if any(s in lo for s in ("burned","poisoned","paralyzed","frozen","asleep","confused")):
        return curses.color_pair(P["stat_mid"])|curses.A_BOLD
    if "hurt by" in lo: return curses.color_pair(P["stat_mid"])
    if "woke up" in lo or "thawed" in lo: return curses.color_pair(P["stat_hi"])
    if "restored" in lo: return curses.color_pair(P["item"])|curses.A_BOLD
    return curses.color_pair(P["white"])

def _draw_log_line(scr, y, x_start, max_w, line, p1_name, p2_name):
    """Render one log line with per-token colour highlighting.

    Player (p1) name: bright green   P["log_p1"]
    Foe    (p2) name: orange-red     P["log_p2"]
    Move name:        yellow         P["log_move"]
    Damage number:    orange         P["log_dmg"]
    """
    base      = _log_base_attr(line)
    text      = line[:max_w]

    # Colour attr per name — different for player vs foe
    p1_attr   = curses.color_pair(P["log_p1"]) | curses.A_BOLD
    p2_attr   = curses.color_pair(P["log_p2"]) | curses.A_BOLD
    move_attr = curses.color_pair(P["log_move"]) | curses.A_BOLD
    dmg_attr  = curses.color_pair(P["log_dmg"])  | curses.A_BOLD

    segs = []

    # Highlight each name with its own colour
    for name, attr in [(p1_name, p1_attr), (p2_name, p2_attr)]:
        if not name: continue
        idx = 0
        while True:
            pos = text.find(name, idx)
            if pos == -1: break
            segs.append((pos, pos+len(name), attr))
            idx = pos + len(name)

    # Highlight move name (between "used " and "!")
    used_idx = text.find(" used ")
    if used_idx != -1:
        ms = used_idx + 6; me = text.find("!", ms)
        if me == -1: me = min(len(text), ms+24)
        segs.append((ms, me, move_attr))

    # Highlight damage number (digits after "took ")
    took_idx = text.find(" took ")
    if took_idx != -1:
        ns = took_idx + 6; ne = ns
        while ne < len(text) and text[ne].isdigit(): ne += 1
        segs.append((ns, ne, dmg_attr))

    segs.sort(key=lambda s: s[0]); merged = []
    for seg in segs:
        if merged and seg[0] < merged[-1][1]: continue
        merged.append(seg)

    cursor = 0; cx = x_start
    for ss, se, sa in merged:
        ss = max(0, min(ss, len(text))); se = max(ss, min(se, len(text)))
        if cursor < ss:
            chunk = text[cursor:ss]; _add(scr, y, cx, chunk, base); cx += len(chunk)
        chunk = text[ss:se]; _add(scr, y, cx, chunk, sa); cx += len(chunk)
        cursor = se
    if cursor < len(text): _add(scr, y, cx, text[cursor:], base)

# ─────────────────────────────────────────────────────────────────────────────
#  BATTLE SCREEN RENDERER  — v6 layout preserved
# ─────────────────────────────────────────────────────────────────────────────
MON_PANEL_H = 7
LOG_H_MAX   = 8

def _draw_battle(scr, p1, p2, log, cursor, state, turn,
                 item_mode=False, item_cursor=0, item_msg=""):
    H,W=scr.getmaxyx(); scr.erase()
    title_a=curses.color_pair(P["title"])|curses.A_BOLD
    scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
    _add(scr,0,1,f"  ⚔  BATTLE  ·  {p1['name']} vs {p2['name']}  ·  Turn {turn}",title_a)
    draw_trainer_hud(scr,W)
    half=max(22,(W-3)//2)
    broke=TRAINER.is_broke()

    def _draw_mon(mon, px, pw, is_player):
        pair_b=P["border_hi"] if is_player else P["border_dim"]
        box(scr,1,px,MON_PANEL_H,pw,pair_b)

        primary=mon["types"][0] if mon["types"] else "normal"
        fg,bg=TYPE_COLORS.get(primary,(250,240)); np_=_alloc_pair(fg,bg)
        inner=pw-2
        side="◀ YOU" if is_player else "FOE ▶"
        name_str=f" ★ {mon['name']} ★ " if is_player else f" {mon['name']} "
        name_str=name_str[:inner-len(side)-1]
        pad=max(0,inner-len(name_str)-len(side))
        banner=(name_str+" "*pad+side)[:inner]
        _add(scr,2,px+1,banner,curses.color_pair(np_)|curses.A_BOLD)

        if mon.get("status"):
            sym=STATUS_SYMBOLS.get(mon["status"],"?")
            _add(scr,2,px+1,f" {sym} ",curses.color_pair(P["stat_lo"])|curses.A_BOLD)

        bx=px+2
        for pt in mon["types"][:2]: bx+=type_badge(scr,3,bx,pt)
        _add(scr,3,px+pw-9," Lv.50 ",curses.color_pair(P["dex_num"]))
        if is_player and broke:
            _add(scr,3,px+pw-17," 💸BROKE ",curses.color_pair(P["broke"])|curses.A_BOLD)
        if is_player and TRAINER.streak>=2:
            _add(scr,3,bx+1,f"🔥×{TRAINER.streak}",curses.color_pair(P["pokeball_r"])|curses.A_BOLD)

        hp_pct=mon["hp"]/max(1,mon["max_hp"])
        bar_w=max(4,pw-18); filled=max(0,round(hp_pct*bar_w))
        hp_p=(P["stat_hi"] if hp_pct>0.5 else P["stat_mid"] if hp_pct>0.2 else P["stat_lo"])
        _add(scr,4,px+2,"HP ",curses.color_pair(P["subtext"])|curses.A_BOLD)
        _add(scr,4,px+5,"█"*filled,curses.color_pair(hp_p)|curses.A_BOLD)
        _add(scr,4,px+5+filled,"░"*max(0,bar_w-filled),curses.color_pair(P["bar_empty"]))
        _add(scr,4,px+5+bar_w+1,f" {mon['hp']:3d}/{mon['max_hp']:3d}",
             curses.color_pair(hp_p)|curses.A_BOLD)

        _add(scr,5,px+2,
             f"ATK {mon['atk']:3d}  DEF {mon['def_']:3d}  SPD {mon['spe']:3d}",
             curses.color_pair(P["subtext"]))

        if is_player and TRAINER.streak>=2:
            _add(scr,6,px+2,
                 f"  🔥 Streak ×{TRAINER.streak}  Best ×{TRAINER.best_streak}",
                 curses.color_pair(P["pokeball_r"])|curses.A_BOLD)

    _draw_mon(p1,0,half,True)
    _draw_mon(p2,half+1,W-half-2,False)

    SPD_ROW=MON_PANEL_H+1
    spd_s,spd_p=(("⚡ YOU move first",P["stat_hi"]) if p1["spe"]>=p2["spe"]
                 else ("⚡ FOE moves first",P["stat_lo"]))
    _add(scr,SPD_ROW,W//2-len(spd_s)//2,spd_s,curses.color_pair(spd_p)|curses.A_BOLD)

    LOG_Y=SPD_ROW+1
    LOG_H=max(4,min(LOG_H_MAX+2, H-LOG_Y-10))
    thin_box(scr,LOG_Y,0,LOG_H,W-1,P["border_dim"],"BATTLE LOG")
    visible=log[-(LOG_H-2):]
    for i,line in enumerate(visible):
        _draw_log_line(scr,LOG_Y+1+i,2,W-4,line,p1["name"],p2["name"])

    BOT_Y=LOG_Y+LOG_H+1
    BOT_H=max(4,H-BOT_Y-3)

    if item_mode:
        item_keys=[k for k,v in TRAINER.items.items() if v>0]
        item_names={"potion":"Potion (+40 HP)","full_restore":"Full Restore",
                    "pp_restore":"PP Restore","lure":"Lure (2× money)"}
        thin_box(scr,BOT_Y,0,BOT_H,W-1,P["item"],"USE ITEM")
        if item_keys:
            for i,k in enumerate(item_keys[:4]):
                sel=i==item_cursor
                attr=(curses.color_pair(P["search"])|curses.A_BOLD) if sel else curses.color_pair(P["item"])|curses.A_BOLD
                pfx="►" if sel else " "
                _add(scr,BOT_Y+1+i,4,f"  {pfx} {item_names.get(k,k):<24s}  ×{TRAINER.items[k]}",attr)
            if item_msg:
                _add(scr,BOT_Y+len(item_keys)+2,4,f"  {item_msg}",
                     curses.color_pair(P["stat_hi"])|curses.A_BOLD)
        else:
            _add(scr,BOT_Y+2,4,"  No items in bag!  Visit the Shop [S].",
                 curses.color_pair(P["subtext"]))

    elif state=="player_turn" and p1.get("moves"):
        title_str="YOUR TURN  ·  "+p1["name"]
        if broke: title_str+="  💸 BROKE — only move 1 available"
        thin_box(scr,BOT_Y,0,BOT_H,W-1,P["border_hi"],title_str)
        moves=p1["moves"]; col_w=max(30,(W-4)//2)

        items_total=sum(TRAINER.items.values())
        if items_total>0:
            item_summary="  BAG: "+"  ".join(
                f"{k[:3].upper()}×{v}" for k,v in TRAINER.items.items() if v>0)
            _add(scr,BOT_Y+1,W-len(item_summary)-4,item_summary,
                 curses.color_pair(P["item"])|curses.A_BOLD)

        for i,mv in enumerate(moves[:4]):
            row=i//2; col=i%2
            my=BOT_Y+2+row; mx=2+col*col_w
            if my>=BOT_Y+BOT_H-1: break
            locked_by_broke=broke and i>0
            sel=(i==cursor) and not locked_by_broke
            if locked_by_broke:
                attr=curses.color_pair(P["locked"])
                mv_t=mv.get("type","normal"); mfg,mbg=TYPE_COLORS.get(mv_t,(250,240))
                mcp=_alloc_pair(mfg,mbg)
                _add(scr,my,mx,f"[{i+1}]",attr)
                _add(scr,my,mx+4,f" {mv_t.upper()[:6]:^6s} ",curses.color_pair(mcp))
                _add(scr,my,mx+13,f"{mv['name'][:16]:<16s}  [LOCKED — BROKE]",
                     curses.color_pair(P["broke"]))
            else:
                attr=(curses.color_pair(P["search"])|curses.A_BOLD) if sel else curses.color_pair(P["white"])
                mv_t=mv.get("type","normal"); mfg,mbg=TYPE_COLORS.get(mv_t,(250,240))
                mcp=_alloc_pair(mfg,mbg)
                pwr_s=f"PWR:{mv['power']:3d}" if mv["power"]>0 else "Status  "
                pp_r=mv["pp"]/max(1,mv["max_pp"])
                pp_attr=curses.color_pair(P["stat_hi"] if pp_r>0.5 else (P["stat_mid"] if pp_r>0.2 else P["stat_lo"]))
                pfx="►" if sel else str(i+1)
                _add(scr,my,mx,f"[{pfx}]",attr)
                _add(scr,my,mx+4,f" {mv_t.upper()[:6]:^6s} ",curses.color_pair(mcp)|curses.A_BOLD)
                _add(scr,my,mx+13,f"{mv['name'][:16]:<16s}  {pwr_s}  ",attr)
                _add(scr,my,mx+43,f"PP {mv['pp']}/{mv['max_pp']}",pp_attr|curses.A_BOLD)

    elif state in ("animating","waiting"):
        thin_box(scr,BOT_Y,0,min(4,BOT_H),W-1,P["border_dim"])
        _add(scr,BOT_Y+1,4,"  Press any key to continue...  ",
             curses.color_pair(P["dex_num"])|curses.A_BOLD)

    elif state=="battle_over":
        winner=p1["name"] if p2["hp"]<=0 else p2["name"]
        loser =p2["name"] if p2["hp"]<=0 else p1["name"]
        thin_box(scr,BOT_Y,0,min(5,BOT_H),W-1,P["border_hi"])
        _add(scr,BOT_Y+1,4,f"  ★  {loser} fainted!   {winner} wins!  ★",
             curses.color_pair(P["title"])|curses.A_BOLD)
        _add(scr,BOT_Y+2,4,"  Press any key to return...",curses.color_pair(P["dex_num"]))

    scr.hline(H-2,0,' ',W-1,curses.color_pair(P["nav_key"]))
    _add(scr,H-2,1,
         " [1-4] Select  │  [Enter/Spc] Use  │  [I] Items  │  [R] Run  │  [Q] Quit ",
         curses.color_pair(P["nav_key"])|curses.A_BOLD)
    scr.noutrefresh(); curses.doupdate()

# ─────────────────────────────────────────────────────────────────────────────
#  BATTLE INTRO
# ─────────────────────────────────────────────────────────────────────────────
def _battle_intro(scr,p1_name,p2_name,is_wild):
    H,W=scr.getmaxyx()
    title_a=curses.color_pair(P["title"])|curses.A_BOLD
    phrase=f"A wild {p2_name} appeared!" if is_wild else f"{p2_name} wants to battle!"
    for flash in range(5):
        scr.erase()
        if flash%2==0: scr.bkgd(' ',curses.color_pair(P["border_hi"]))
        scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
        _add(scr,0,1,"  ⚔  BATTLE START!",title_a)
        _add(scr,H//2-1,W//2-len(phrase)//2,phrase,
             curses.color_pair(P["search"] if flash%2==0 else P["title"])|curses.A_BOLD)
        _add(scr,H//2+1,W//2-10,f"  {p1_name}  VS  {p2_name}  ",
             curses.color_pair(P["stat_hi"])|curses.A_BOLD)
        scr.noutrefresh(); curses.doupdate(); time.sleep(0.13)
    scr.bkgd(' ',0); time.sleep(0.2)

# ─────────────────────────────────────────────────────────────────────────────
#  BATTLE MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────
def run_battle(scr,p1_data,p2_data,is_wild=False,poke_cache=None,sprite_cache=None):
    H,W=scr.getmaxyx()
    p1=_make_battle_mon(p1_data); p2=_make_battle_mon(p2_data)
    scr.erase()
    title_a=curses.color_pair(P["title"])|curses.A_BOLD
    scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
    _add(scr,0,1,"  ⚔  BATTLE SETUP — Loading moves...  ⚔",title_a)
    _add(scr,H//2-1,4,f"  Preparing: {p1['name']} vs {p2['name']}  ",
         curses.color_pair(P["search"])|curses.A_BOLD)
    scr.noutrefresh(); curses.doupdate()
    p1["moves"]=_load_battle_moves(p1_data,scr,p1["name"])
    p2["moves"]=_load_battle_moves(p2_data,scr,p2["name"])

    _battle_intro(scr,p1["name"],p2["name"],is_wild)

    log=[f"  ⚔  {p1['name']} vs {p2['name']}  —  Battle start!"]
    cursor=0; state="player_turn"; turn=1; result="run"
    item_mode=False; item_cursor=0; item_msg=""

    scr.nodelay(False); scr.timeout(200)

    while True:
        broke=TRAINER.is_broke()
        _draw_battle(scr,p1,p2,log,cursor,state,turn,item_mode,item_cursor,item_msg)
        try: key=scr.getch()
        except Exception: continue

        if item_mode:
            item_keys=[k for k,v in TRAINER.items.items() if v>0]
            if key==27: item_mode=False; item_msg=""
            elif key in (curses.KEY_UP,ord('w'),ord('W')):
                item_cursor=max(0,item_cursor-1); item_msg=""
            elif key==curses.KEY_DOWN:
                item_cursor=min(len(item_keys)-1,item_cursor+1); item_msg=""
            elif key in (10,13) and item_keys:
                k=item_keys[item_cursor]; msg=TRAINER.use_item(k,p1)
                item_msg=msg; log.append(f"  Used {k}: {msg}")
                if not any(v>0 for v in TRAINER.items.values()): item_mode=False
            continue

        if state=="player_turn":
            if key in (ord('q'),ord('Q')): result="run"; break
            elif key in (ord('r'),ord('R')):
                log.append("  You ran away safely!")
                _draw_battle(scr,p1,p2,log,cursor,"waiting",turn)
                scr.getch(); result="run"; break
            elif key in (ord('i'),ord('I')):
                item_mode=True; item_cursor=0; item_msg=""; continue

            if   key==curses.KEY_LEFT:
                if cursor%2==1: cursor-=1
            elif key==curses.KEY_RIGHT:
                if cursor%2==0 and cursor+1<len(p1["moves"]) and not broke: cursor+=1
            elif key==curses.KEY_UP:
                if cursor>=2 and not broke: cursor-=2
            elif key==curses.KEY_DOWN:
                if cursor+2<len(p1["moves"]) and not broke: cursor+=2
            elif ord('1')<=key<=ord('4'):
                idx=key-ord('1')
                if idx==0 or not broke:
                    if idx<len(p1["moves"]): cursor=idx; key=10
                else:
                    log.append("  ⚠ Move locked! Win battles to get out of debt.")

            if key in (10,13,ord(' ')):
                if not p1["moves"]: continue
                if broke and cursor!=0:
                    log.append("  ⚠ Broke! Only your first move is available.")
                    cursor=0; continue
                mv1=p1["moves"][cursor]
                if mv1["pp"]<=0: log.append(f"  {mv1['name']} has no PP left!"); continue

                ai_pool=[m for m in p2["moves"] if m["pp"]>0]
                if ai_pool:
                    ai_w=[max(1,m["power"]) for m in ai_pool]; r=random.random()*sum(ai_w)
                    cum=0; mv2=ai_pool[-1]
                    for m,w in zip(ai_pool,ai_w):
                        cum+=w
                        if r<=cum: mv2=m; break
                else: mv2=None
                mv1["pp"]-=1
                if mv2: mv2["pp"]-=1

                if p1["spe"]>=p2["spe"]:
                    _animate_attack(scr,p1,p2,mv1,True)
                    p2_f=_execute_attack(p1,p2,mv1,log)
                    if not p2_f:
                        if mv2:
                            _draw_battle(scr,p1,p2,log,cursor,"animating",turn)
                            _animate_attack(scr,p2,p1,mv2,False)
                            _execute_attack(p2,p1,mv2,log)
                        else:
                            sdmg=max(1,p1["max_hp"]//8); p1["hp"]=max(0,p1["hp"]-sdmg)
                            log.append(f"  {p2['name']} struggled! -{sdmg} to {p1['name']}")
                else:
                    if mv2:
                        _animate_attack(scr,p2,p1,mv2,False)
                        _execute_attack(p2,p1,mv2,log)
                    else:
                        sdmg=max(1,p1["max_hp"]//8); p1["hp"]=max(0,p1["hp"]-sdmg)
                        log.append(f"  {p2['name']} struggled! -{sdmg} to {p1['name']}")
                    if p1["hp"]>0:
                        _draw_battle(scr,p1,p2,log,cursor,"animating",turn)
                        _animate_attack(scr,p1,p2,mv1,True)
                        _execute_attack(p1,p2,mv1,log)

                if   p2["hp"]<=0: log.append(f"  {p2['name']} fainted!"); state="battle_over"; result="win"
                elif p1["hp"]<=0: log.append(f"  {p1['name']} fainted!"); state="battle_over"; result="loss"
                else:             turn+=1; state="animating"

        elif state=="animating":
            if key!=-1: state="player_turn"
        elif state=="battle_over":
            if key!=-1: break

    scr.nodelay(True); scr.timeout(100)
    return result

# ─────────────────────────────────────────────────────────────────────────────
#  BATTLE SETUP
# ─────────────────────────────────────────────────────────────────────────────
def select_battle_pokemon(scr,current_pid,poke_cache):
    scr.nodelay(False); scr.timeout(200)
    p2_buf=""; err=""
    while True:
        H,W=scr.getmaxyx(); scr.erase()
        title_a=curses.color_pair(P["title"])|curses.A_BOLD
        scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
        _add(scr,0,1,"  ⚔  BATTLE SETUP  ·  Choose Your Opponent",title_a)
        draw_trainer_hud(scr,W)
        box(scr,2,2,15,W-4,P["border_hi"],"SELECT FIGHTERS")
        p1_name=poke_cache[current_pid]["name"].upper() if current_pid in poke_cache else f"#{current_pid:04d}"
        p1_types=[t["type"]["name"] for t in poke_cache[current_pid].get("types",[])] if current_pid in poke_cache else []
        fg1,bg1=TYPE_COLORS.get(p1_types[0] if p1_types else "normal",(250,240))
        cp1=_alloc_pair(fg1,bg1)
        _add(scr,4,6,"Fighter 1  (YOU):",curses.color_pair(P["subtext"])|curses.A_BOLD)
        _add(scr,4,26,f" #{current_pid:04d}  {p1_name} ",curses.color_pair(cp1)|curses.A_BOLD)
        bx=26+8+len(p1_name)
        for pt in p1_types[:2]: bx+=type_badge(scr,4,bx,pt)
        _add(scr,5,6,"  (current Pokémon — confirmed)",curses.color_pair(P["subtext"]))
        _add(scr,6,6,f"  Trainer Lv{TRAINER.level}  ·  W:{TRAINER.battles_won} L:{TRAINER.battles_lost}  ·  Streak: ×{TRAINER.streak}",
             curses.color_pair(P["xp_bar"])|curses.A_BOLD)
        if TRAINER.is_broke():
            _add(scr,7,6,"  💸 BROKE — moves 2-4 will be locked until balance > $0.",
                 curses.color_pair(P["broke"])|curses.A_BOLD)
        _add(scr,9,4,"─"*(W-10),curses.color_pair(P["border_dim"]))
        _add(scr,10,6,"Fighter 2  (FOE):",curses.color_pair(P["subtext"])|curses.A_BOLD)
        _add(scr,10,26," Enter Pokémon ID or name:",curses.color_pair(P["white"]))
        _add(scr,11,26,f"  {p2_buf}█"+" "*max(0,34-len(p2_buf)),
             curses.color_pair(P["search"])|curses.A_BOLD)
        _add(scr,13,6,"  Examples:  6  ·  charizard  ·  150  ·  mewtwo",
             curses.color_pair(P["subtext"]))
        if err: _add(scr,14,6,f"  ✖  {err}  ",curses.color_pair(P["error"])|curses.A_BOLD)
        scr.hline(H-2,0,' ',W-1,curses.color_pair(P["nav_key"]))
        _add(scr,H-2,1,"  [Enter] Start Battle  │  [Esc] Cancel  ",
             curses.color_pair(P["nav_key"])|curses.A_BOLD)
        scr.noutrefresh(); curses.doupdate()
        key=scr.getch()
        if key==27: scr.nodelay(True); scr.timeout(100); return None,None
        elif key in (10,13):
            raw=p2_buf.strip()
            if not raw: err="Please enter a Pokémon ID or name."; continue
            p2_id=None
            if raw.isdigit(): p2_id=max(1,min(int(raw),MAX_POKEMON))
            else:
                lc=raw.lower()
                for i,d in poke_cache.items():
                    if d.get("name","").lower()==lc: p2_id=i; break
                if p2_id is None:
                    _add(scr,14,6,f"  Searching for '{raw}'...",
                         curses.color_pair(P["search"])|curses.A_BOLD)
                    scr.noutrefresh(); curses.doupdate()
                    d2=fetch_pokemon(lc)
                    if d2: p2_id=d2["id"]; poke_cache[p2_id]=d2
                    else:  err=f"'{raw}' not found."; p2_buf=""; continue
            if p2_id not in poke_cache:
                _add(scr,14,6,f"  Loading #{p2_id:04d}...",
                     curses.color_pair(P["search"])|curses.A_BOLD)
                scr.noutrefresh(); curses.doupdate()
                d2=fetch_pokemon(p2_id)
                if d2: poke_cache[p2_id]=d2
                else:  err=f"Failed to load #{p2_id:04d}."; p2_buf=""; continue
            scr.nodelay(True); scr.timeout(100)
            return poke_cache[current_pid],poke_cache[p2_id]
        elif key in (8,127,curses.KEY_BACKSPACE): p2_buf=p2_buf[:-1]; err=""
        elif 32<=key<=126: p2_buf+=chr(key); err=""

# ─────────────────────────────────────────────────────────────────────────────
#  POST-BATTLE RESET / TRAINER SETUP / FAREWELL
# ─────────────────────────────────────────────────────────────────────────────
def _post_battle_reset(scr, sprite_cache):
    init_colors(); _reset_sprite_pairs(); sprite_cache.clear()

def trainer_setup(scr):
    H,W=scr.getmaxyx(); buf=""
    while True:
        scr.erase()
        title_a=curses.color_pair(P["title"])|curses.A_BOLD
        scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
        _add(scr,0,1,"  ◈  TERMINAL POKÉDEX  ·  GBA EDITION  ◈",title_a)
        cx=W//2
        thin_box(scr,H//2-8,cx-25,16,50,P["border_hi"],"NEW GAME")
        _add(scr,H//2-6,cx-20,"  Welcome to the world of Pokémon!",
             curses.color_pair(P["stat_hi"])|curses.A_BOLD)
        _add(scr,H//2-4,cx-20,"  Battle wild Pokémon to earn XP and money.",
             curses.color_pair(P["white"]))
        _add(scr,H//2-3,cx-20,"  Buy items at the Shop [S] to gain an edge.",
             curses.color_pair(P["subtext"]))
        _add(scr,H//2-2,cx-20,"  Go broke and your moves get locked — be careful!",
             curses.color_pair(P["broke"]))
        _add(scr,H//2,cx-20,"  What is your name?",
             curses.color_pair(P["subtext"])|curses.A_BOLD)
        _add(scr,H//2+1,cx-20,f"  ▶  {buf}█"+" "*20,
             curses.color_pair(P["search"])|curses.A_BOLD)
        _add(scr,H//2+3,cx-20,"  [Enter] confirm  (blank = RED)",
             curses.color_pair(P["subtext"]))
        scr.noutrefresh(); curses.doupdate()
        key=scr.getch()
        if key in (10,13): TRAINER.name=buf.strip().upper() or "RED"; break
        elif key in (8,127): buf=buf[:-1]
        elif 32<=key<=126 and len(buf)<10: buf+=chr(key)

def _show_farewell(scr):
    H,W=scr.getmaxyx(); scr.erase()
    title_a=curses.color_pair(P["title"])|curses.A_BOLD
    scr.hline(0,0,' ',W-1,curses.color_pair(P["title"]))
    _add(scr,0,1,"  ◈  SESSION SUMMARY  ◈",title_a)
    cx=W//2
    thin_box(scr,H//2-9,cx-28,19,56,P["border_hi"],f"TRAINER  {TRAINER.name}  —  FAREWELL!")
    stats=[("Trainer Level",str(TRAINER.level)),("Battles Won",str(TRAINER.battles_won)),
           ("Battles Lost",str(TRAINER.battles_lost)),("Best Win Streak",f"×{TRAINER.best_streak}"),
           ("Pokémon Seen",str(len(TRAINER.seen))),("Pokémon Caught",str(len(TRAINER.caught))),
           ("Final Balance",f"${TRAINER.money}"),("Badges Earned",str(len(TRAINER.badges)))]
    for i,(label,val) in enumerate(stats):
        _add(scr,H//2-7+i,cx-24,f"  {label:<20s}  {val}",
             curses.color_pair(P["white"] if i%2==0 else P["subtext"])|curses.A_BOLD)
    if TRAINER.badges:
        _add(scr,H//2+3,cx-24,
             "  Badges: "+"  ".join(TRAINER.badges[:3])[:50],
             curses.color_pair(P["money"])|curses.A_BOLD)
    items_str="  ".join(f"{k}×{v}" for k,v in TRAINER.items.items() if v>0) or "none"
    _add(scr,H//2+5,cx-24,f"  Items in bag: {items_str}",
         curses.color_pair(P["item"])|curses.A_BOLD)
    _add(scr,H//2+7,cx-24,"  Thank you for playing!  Press any key to exit.",
         curses.color_pair(P["dex_num"]))
    scr.noutrefresh(); curses.doupdate()
    scr.nodelay(False); scr.getch()

# ─────────────────────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────
def main(scr):
    curses.curs_set(0); curses.noecho()
    scr.nodelay(True); scr.timeout(100)
    init_colors()
    trainer_setup(scr)

    pid=1
    search_mode=False; search_buf=""
    goto_mode=False;   goto_buf=""
    err=None
    poke_cache={}; img_cache={}; sprite_cache={}; species_cache={}
    dirty=True; last_H=last_W=0
    steps_since_wild=0; WILD_STEP_THRESHOLD=15

    while True:
        H,W=scr.getmaxyx()
        if H!=last_H or W!=last_W: dirty=True; last_H=H; last_W=W

        if pid not in poke_cache:
            render_loading(scr,pid)
            data=fetch_pokemon(pid)
            if data: poke_cache[pid]=data; err=None
            else:    err=f"Failed to load #{pid:04d}"
            dirty=True

        poke=poke_cache.get(pid)
        if poke and pid not in img_cache:
            img_cache[pid]=fetch_sprite(poke); dirty=True
        if poke and pid not in sprite_cache:
            sprite_cache.clear(); _reset_sprite_pairs(); init_colors()
            raw_img=img_cache.get(pid)
            try:    sprite_cache[pid]=render_sprite(raw_img) if raw_img else ascii_sprite(pid)
            except: sprite_cache[pid]=ascii_sprite(pid)
            dirty=True
        if poke and pid not in species_cache:
            raw=fetch_species(poke.get("id",pid))
            species_cache[pid]=parse_species(raw); dirty=True

        sprite=sprite_cache.get(pid); species_info=species_cache.get(pid,{})

        if dirty:
            scr.erase()
            render_frame(scr,poke,sprite,species_info,pid,
                         search_mode,search_buf,goto_mode,goto_buf,err)
            scr.noutrefresh(); curses.doupdate(); dirty=False

        try:   key=scr.getch()
        except: continue
        if key==-1: continue

        if search_mode:
            if key==27: search_mode=False; search_buf=""; dirty=True
            elif key in (10,13):
                if search_buf.isdigit(): pid=max(1,min(int(search_buf),MAX_POKEMON))
                else:
                    lc=search_buf.lower(); found=False
                    for i,d in poke_cache.items():
                        if d.get("name","").lower()==lc: pid=i; found=True; break
                    if not found:
                        d2=fetch_pokemon(lc)
                        if d2: poke_cache[d2["id"]]=d2; pid=d2["id"]
                        else:  err=f"'{search_buf}' not found."
                search_mode=False; search_buf=""; dirty=True
            elif key in (8,127): search_buf=search_buf[:-1]; dirty=True
            elif 32<=key<=126:   search_buf+=chr(key);        dirty=True
            continue

        if goto_mode:
            if key==27: goto_mode=False; goto_buf=""; dirty=True
            elif key in (10,13):
                if goto_buf.isdigit(): pid=max(1,min(int(goto_buf),MAX_POKEMON))
                goto_mode=False; goto_buf=""; dirty=True
            elif key in (8,127): goto_buf=goto_buf[:-1]; dirty=True
            elif 48<=key<=57:    goto_buf+=chr(key);    dirty=True
            continue

        prev=pid; moved=False

        if   key in (curses.KEY_LEFT, ord('a'),ord('A')): pid=max(1,pid-1);           moved=True
        elif key in (curses.KEY_RIGHT,ord('d'),ord('D')): pid=min(MAX_POKEMON,pid+1); moved=True
        elif key in (curses.KEY_UP,   ord('w'),ord('W')): pid=max(1,pid-10);          moved=True
        elif key == curses.KEY_DOWN:                       pid=min(MAX_POKEMON,pid+10);moved=True
        elif key in (ord('/'),ord('f'),ord('F')): search_mode=True; search_buf=""; dirty=True
        elif key in (ord('g'),ord('G')):          goto_mode=True;   goto_buf="";   dirty=True
        elif key in (ord('r'),ord('R')):
            poke_cache.pop(pid,None); img_cache.pop(pid,None)
            sprite_cache.pop(pid,None); species_cache.pop(pid,None); dirty=True
        elif key in (ord('c'),ord('C')): play_cry(poke)
        elif key in (ord('s'),ord('S')): show_shop(scr); dirty=True
        elif key in (ord('b'),ord('B')):
            if poke:
                p1,p2=select_battle_pokemon(scr,pid,poke_cache)
                if p1 and p2:
                    result=run_battle(scr,p1,p2,is_wild=False,
                                      poke_cache=poke_cache,sprite_cache=sprite_cache)
                    if result=="win":
                        foe_level=max(5,sum(s["base_stat"] for s in p2.get("stats",[]))//30)
                        xp,money,leveled,item_drop=TRAINER.record_win(
                            p2.get("id",0),p2.get("name","???").upper(),foe_level)
                        new_badges=TRAINER.check_badges()
                        if leveled:   show_level_up(scr,TRAINER.level)
                        if item_drop: show_item_drop(scr,item_drop)
                        for b in new_badges: show_badge_unlocked(scr,b)
                    elif result=="loss":
                        TRAINER.record_loss(); TRAINER.check_badges()
                        if TRAINER.is_broke(): show_broke_warning(scr)
                _post_battle_reset(scr,sprite_cache); dirty=True
        elif key in (ord('x'),ord('X')):
            if poke:
                wild_encounter(scr,poke_cache,img_cache,sprite_cache,species_cache,pid)
                _post_battle_reset(scr,sprite_cache); dirty=True
        elif key in (ord('q'),ord('Q'),27):
            _show_farewell(scr); break

        if pid!=prev:
            dirty=True
            if moved:
                steps_since_wild+=abs(pid-prev)
                if steps_since_wild>=WILD_STEP_THRESHOLD:
                    steps_since_wild=0
                    if random.random()<0.25 and poke:
                        wild_encounter(scr,poke_cache,img_cache,sprite_cache,species_cache,pid)
                        _post_battle_reset(scr,sprite_cache); dirty=True


if __name__=="__main__":
    print("Terminal Pokédex — GBA Edition  (v7)")
    print("Requires Python 3.8+  |  pip install Pillow  for colour sprites")
    print()
    print("v7 fixes:")
    print("  ✔ Pokédex entry: second column to the RIGHT of stats on wide terminals;")
    print("    placed BEFORE abilities/moves on narrow terminals (never buried at bottom)")
    print("  ✔ Battle animations now travel across the HP-bar row (BY=4), not the log")
    print("  ✔ Battle log: player name = green, foe name = orange-red")
    print()
    print("Controls: ←/→ or A/D  navigate  |  B  Battle  |  X  Wild  |  S  Shop  |  Q  Quit")
    print()
    print("Press Enter to launch...")
    input()
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
