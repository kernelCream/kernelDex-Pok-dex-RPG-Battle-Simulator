# kernelDex-Pok-dex-RPG-Battle-Simulator
A mini Pokémon game inside your terminal — browse, battle, collect, and progress like a classic GBA experience, all in pure Python.


> A fully-featured, **curses-based Pokédex and battle simulator** that runs entirely in your terminal. Sprites rendered in 256-colour half-block art, a complete turn-based RPG economy, wild encounters, a shop, and a trainer progression system — all without leaving the command line.


## Features at a Glance

| Category | Highlights |
|---|---|
| **Pokédex** | All 1025 Pokémon, live sprites from PokéAPI, stats, moves, abilities, Pokédex entries |
| **Sprites** | 256-colour half-block rendering with contrast + saturation + sharpness pipeline |
| **Battle** | Full Gen 1–9 type chart, status effects, critical hits, PP system, 18 type animations |
| **Economy** | Shop, items, money penalty (broke = locked moves), lure, item drops |
| **Progression** | Trainer levels, XP, win streaks, achievements/badges, session summary |
| **Audio** | Pokémon cries via mpv / ffplay / cvlc / aplay |

---

## Screenshots

```
┌─ BASE STATS ────────────────┐     ┌─ POKÉDEX ENTRY ─────────────────┐
│  HP   100 ████████████░░░░  │     │  It can melt boulders with       │
│  ATK   76 ██████████░░░░░░  │     │  its fire. Incredibly strong,    │
│  DEF   75 ██████████░░░░░░  │     │  it pushes boulders aside to     │
│  SpA  100 ████████████░░░░  │     │  clear its path.                 │
│  SpD   75 ██████████░░░░░░  │     └─────────────────────────────────┘
│  SPD   80 ██████████░░░░░░  │
│  TOTAL  506                  │     ┌─ ABILITIES ─────────────────────┐
└──────────────────────────────┘     │  ◇ Blaze                        │
                                     │  ◆ Solar Power  (hidden)        │
  ⚔  BATTLE  ·  CHARIZARD vs BLASTOISE  ·  Turn 4        └─────────────────────────────────┘
  ╔════════════════════════╗  ╔═════════════════════════╗
  ║ ★ CHARIZARD ★  ◀ YOU  ║  ║ BLASTOISE          FOE ▶║
  ║  FIRE   FLYING  Lv.50  ║  ║  WATER              Lv.50║
  ║  HP ████████████  148/183║  ║  HP ████████░░░░░   89/170║
  ║  ATK  84  DEF  78  SPD  100║  ║  ATK  83  DEF 100  SPD  78║
  ╚════════════════════════╝  ╚═════════════════════════╝
  ┌─ BATTLE LOG ─────────────────────────────────────────┐
  │  CHARIZARD used FLAMETHROWER!                         │
  │  It's super effective!!                               │
  │  BLASTOISE took 67 dmg  (89/170 HP)                  │
  └──────────────────────────────────────────────────────┘
  ┌─ YOUR TURN  ·  CHARIZARD ────────────────────────────┐
  │  [►]  FIRE    FLAMETHROWER   PWR:90   PP 14/15        │
  │  [2]  FLYING  FLY            PWR:90   PP 14/15        │
  │  [3]  FIRE    FIRE SPIN      PWR:35   PP 14/15        │
  │  [4]  NORMAL  SLASH          PWR:70   PP 19/20        │
  └──────────────────────────────────────────────────────┘
```

---

## Requirements

```bash
pip install Pillow        # for 256-colour half-block sprites

sudo apt install mpv                 # recommended for audio

*For Windows : pip install windows-curses Pillow

**Python 3.8+** required. Runs on any Linux/macOS terminal with 256-colour support.  
**Minimum recommended terminal size:** 120 × 36 characters.

---

## Quick Start

pip install Pillow
sudo apt install mpv

Once in the directory run the python file:
python3 kernelDex.py

---

## Controls


| Key | Action |
|---|---|
| `←` / `→` or `A` / `D` | Navigate Pokémon (±1) |
| `↑` / `↓` or `W` | Navigate Pokémon (±10) |
| `/` or `F` | Search by name or ID |
| `G` | Jump directly to Pokédex number |
| `R` | Refresh / re-download current Pokémon |
| `C` | Play Pokémon cry (requires audio player) |
| `B` | Open trainer battle (choose your opponent) |
| `X` | Trigger a wild encounter |
| `S` | Open the item shop |
| `Q` or `Esc` | Quit and show session summary |

### Battle

| Key | Action |
|---|---|
| `1`–`4` | Select move by number |
| `←` `→` `↑` `↓` | Move cursor across move grid |
| `Enter` / `Space` | Use selected move |
| `I` | Open item bag |
| `R` | Run away |
| `Q` | Quit battle |

---

## Complete Function & Feature Reference

### Core Architecture

**`TrainerState`** — Persistent session object tracking all player data.  
Stores trainer name, level, XP and next-level threshold, win/loss record, active streak, best streak, money, caught and seen Pokémon sets, badge list, and item inventory. Exposes `add_xp()`, `record_win()`, `record_loss()`, `buy()`, `use_item()`, and `check_badges()`.

**`TRAINER`** — Global singleton instance of `TrainerState`, accessible from every screen.

---

### Sprite Rendering

**`_enhance_image(img)`** — PIL quality pipeline applied before quantisation.  
Boosts `Color` ×1.75, `Contrast` ×1.5, `Sharpness` ×1.4, then applies `UnsharpMask` for edge clarity. Compensates for the muddiness of 256-colour terminal palettes.

**`_rgb256(r, g, b)`** — Converts an RGB triple to the nearest xterm-256 colour index.  
Compares the 6×6×6 colour cube error against the 24-step greyscale ramp and picks whichever matches more closely.


---

### Networking & Data

**`fetch_pokemon(pid)`** — Fetches full Pokémon data from `pokeapi.co/api/v2/pokemon/{pid}`. Includes stats, types, moves, sprites, abilities, and cries.

**`fetch_species(pid)`** — Fetches species data including genus, Pokédex flavour text (GBA versions prioritised), generation, gender rate, and capture rate.

**`fetch_sprite(data)`** — Extracts and downloads the sprite PNG. Prefers official artwork; falls back to the default front sprite.

**`get_levelup_moves(poke, max_moves)`** — Extracts level-up moves from Pokémon data, preferring GBA-era game versions. Returns `[(name, level), …]` sorted by learn level.

---

### Pokédex Screen

**`render_frame(...)`** — Main Pokédex renderer. Composes the full screen: title bar with trainer HUD, left panel (sprite + banner + info overlay), vertical divider, right panel (stats, Pokédex entry, abilities, moves), progress bar, and XP bar.

**`draw_type_banner(win, y, x, w, name, gender_sym, primary_type)`** — Renders the Pokémon name banner in the primary type's colour scheme.

**`draw_progress_bar(scr, y, pid, W)`** — Pokédex progress bar at the bottom showing how many of the 1025 Pokémon have been browsed.

**`draw_xp_bar(scr, y, W)`** — Trainer XP bar row. When broke, replaces the XP info with a flashing debt warning.

**`draw_trainer_hud(scr, W)`** — Compact HUD drawn in the title bar: trainer name, level, XP bar, win count, money, broke indicator, lure counter, and streak flame.

---

### Shop Screen

**`show_shop(scr)`** — Full-screen item shop. Displays all four items with name, cost (green if affordable, red if not), held count, and description. Scrolling tip line cycles through economy advice. Supports keyboard navigation and buying.

Items available:

- **Potion** ($150) — Restores 40 HP in battle
- **Full Restore** ($450) — Fully restores HP and clears status in battle
- **PP Restore** ($200) — Refills all PP for one move in battle
- **Lure** ($120) — Doubles prize money for the next 3 wild encounters

---

### Audio

**`play_cry(poke_data)`** — Downloads the Pokémon's `.ogg` cry file and plays it in a background thread. Tries `mpv` → `ffplay` → `cvlc` → `aplay` in order. Cleans up the temp file after playback.

---

#### Setup & Preparation

**`select_battle_pokemon(scr, current_pid, poke_cache)`** — Trainer battle setup screen. Fighter 1 is the current Pokédex Pokémon (locked). The player types any Pokémon ID or name for Fighter 2; the function fetches it from the API if not cached.

**`_make_battle_mon(poke_data)`** — Scales a Pokémon to Level 50 using the official formula: `HP = (2×base + 15) × 50/100 + 60`, `other = (2×base + 15) × 50/100 + 5`. Returns a battle dict including HP, all stats, types, moves list, status, and sleep counter.

**`_load_battle_moves(poke_data, scr, label)`** — Fetches up to 4 damaging moves from the API, falling back to status moves if fewer than 4 damaging options exist. Shows a loading hint on screen during fetching.

**`_fetch_move_detail(raw_name)`** — Fetches a single move's power, type, PP, accuracy, and damage class from the API.

#### Damage Calculation

**`_execute_attack(attacker, defender, move, log)`** — Full Gen 3+ damage pipeline:
1. Status start check (may block the attack)
2. Accuracy roll (modified by paralysis)
3. Critical hit check (6.25% chance, ×1.5 multiplier)
4. Damage class routing (physical → ATK/DEF, special → SpA/SpD)
5. Burn penalty on physical attacks (ATK ×0.5)
6. STAB bonus (×1.5 if move type matches attacker type)
7. Full Gen 1–9 type effectiveness via `TYPE_CHART`
8. Random roll (85–100%)
9. Status application chance (burn/paralyse/poison/freeze/confuse)
10. End-of-turn status damage

**`_type_mult(move_type, def_types)`** — Multiplies effectiveness across both defender types, correctly producing 0× (immunity), 0.25×, 0.5×, 1×, 2×, and 4× results.

#### Status Effects

**`_apply_status(defender, move, log)`** — Chance-based status infliction: Fire→Burn (10%), Electric→Paralysis (10%), Poison→Poison (15%), Ice→Freeze (10%), Psychic→Confusion (10%).

**`_process_status_start(mon, log)`** — Applies sleep (counter), paralysis (25% skip), and freeze (80% skip, thaws on 20%) at the start of a Pokémon's turn.

**`_process_status_end(mon, log)`** — Applies end-of-turn damage: Burn = max_hp/16, Poison = max_hp/8.

#### Battle Screen

**`_draw_battle(...)`** — Full battle screen renderer. Draws two Pokémon panels (name banner in type colour, type badges, status indicator, HP bar with colour thresholds, stat summary, streak display, broke indicator), speed order hint, coloured log, and move picker or item sub-menu.

**`_draw_log_line(...)`** — Token-coloured log renderer. Player's name is rendered in bright green, the foe's name in orange-red, move names in yellow, and damage numbers in orange. All other line types (super effective, critical, status, etc.) have their own colour.

**`_draw_mon(...)`** — Inner function rendering a single Pokémon panel including type-coloured name banner, status symbol, type badges, HP bar, and stat line.

**Move selection with broke penalty:** When `TRAINER.money < 0`, moves at index 1, 2, and 3 are greyed out and labelled `[LOCKED — BROKE]`. Cursor navigation is blocked to those slots. Only the first move fires. The player must win battles to earn money and unlock moves.


#### Battle Main Loop

**`run_battle(scr, p1_data, p2_data, is_wild, ...)`** — Full battle loop. Handles player input, AI move selection (weighted by power with randomness), speed-order resolution, animation sequencing (attacker goes first, then redraw + foe animation), struggle when out of PP, state transitions (player_turn → animating → player_turn, or battle_over), and item sub-menu mid-battle. Returns `"win"`, `"loss"`, or `"run"`.

**`_battle_intro(scr, p1_name, p2_name, is_wild)`** — 5-frame alternating flash intro with coloured background blink.

**AI behaviour:** Selects moves weighted by power (higher power = more likely), with a random element to avoid purely deterministic play.

---

### Wild Encounter System

**`wild_encounter(...)`** — Full wild encounter flow:
1. Selects a random Pokémon scaled to trainer level (higher level → higher Pokédex IDs accessible)
2. Shows grass animation and encounter intro
3. Runs a full battle
4. On win: awards XP, prize money (×2 with Lure), shows optional catch attempt, rolls for item drop
5. On loss: applies money penalty, shows broke warning if balance goes negative

**Item drops:** 12% chance of a Potion drop, 4% chance of a PP Restore on any win.

**Passive encounters:** While browsing the Pokédex, every 15 navigation steps there is a 25% chance of an automatic wild encounter (simulating walking through tall grass).

---

### Economy & Progression

**Money system:**
- Start with $500
- Win prize = `max(50, foe_level × 12)`, doubled with Lure active
- Loss penalty = `min(balance + 200, max(80, trainer_level × 25))`
- Broke threshold is `money < 0`

**Broke penalty in battle:**
- Moves at index 1–3 show `[LOCKED — BROKE]` in muted red
- Cursor navigation is blocked to locked slots
- Number keys 2–4 are rejected with a log message
- Only move index 0 is usable until balance returns to ≥$0

**Trainer progression:**
- XP per win = `max(10, foe_level × 3 + rand(5, 20))`
- Level-up threshold multiplies by ×1.4 each level
- `show_level_up(scr, new_level)` notification on level-up

**`check_badges()`** — Evaluates 13 achievement conditions and returns newly unlocked badge names. Badges include combat milestones (First Blood, Battle Master, Champion), streaks (On a Roll ×3, Hot Streak ×5, Unstoppable ×10), collection (First Catch, Collector, Pokédex Pro), economy (Money Bags, Bankruptcy), and trainer level (Rising Star, Elite Trainer).



## License

MIT — do whatever you like with it.


*Built entirely in the terminal. No frameworks, no web stack, no GPU — just Python, curses, and a lot of Unicode.*

