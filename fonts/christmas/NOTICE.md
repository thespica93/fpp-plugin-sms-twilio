# Bundled Christmas Fonts

All fonts in this directory are tagged **"100% Free"** on dafont.com (unrestricted use,
including redistribution) at the time they were added. Filenames intentionally match each
font's internal family name exactly — the plugin resolves fonts via `fc-match <name>`, which
requires an exact family-name match to resolve reliably. A few carry small filename tweaks
from their dafont title (extra spacing, a trailing "2" dropped) or, where the embedded family
name itself had no spaces, a rewritten internal name table (via fontTools) rather than just a
renamed file — so fc-match still resolves them correctly either way.

| File | dafont title | dafont slug (if different) | Author | Source |
|---|---|---|---|---|
| Christmas Garland.otf | Christmas Garland | | Mozatype | https://www.dafont.com/christmas-garland.font |
| Kingthings Christmas.ttf | Kingthings Christmas 2 | | Kingthings | https://www.dafont.com/kingthings-christmas.font |
| PW Joyeux Noel.ttf | PW Joyeux Noel | pwhappychristmas | Peax Webdesign | https://www.dafont.com/pwhappychristmas.font |
| PW Tinsel Letters.ttf | PWTinselLetters | pwtinselletters | Peax Webdesign | https://www.dafont.com/pwtinselletters.font |
| Present Snow.ttf | Present Snow | | Siti Nurjanah | https://www.dafont.com/present-snow.font |
| Toy Train.ttf | Toy Train | | West Wind Fonts | https://www.dafont.com/toy-train.font |
| Xmas Lights.ttf | Xmas Lights (BRK) | | AEnigma | https://www.dafont.com/xmas-lights.font |

Earlier drafts of this catalog had "PW Joyeux Noel" and "Present Snow" as relabeled copies of
two other Keithzo fonts (New Born Christmas, Santa Christmas) — a mistake, since those are
real, distinct fonts on dafont with their own look. Both have since been replaced with the
actual fonts of those names.

PW Joyeux Noel's dafont slug (`pwhappychristmas`) differs from both its display title and its
internal family name (`PWJoyeuxNoel`, no spaces before the rewrite) — worth knowing if this
font is ever re-sourced from dafont directly, since searching dafont for "joyeux noel" won't
find it.

"Christmas Garland" is the only `.otf` in this set. It renders correctly through the plugin's
normal PIL-based path, but is not resolvable by FPP's native overlay-text fallback (used only
when PIL is unavailable or the overlay model's dimensions aren't set), since FPP's own font
scanner does not recognize `.otf` files.
