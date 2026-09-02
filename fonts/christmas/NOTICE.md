# Bundled Christmas Fonts

All fonts in this directory are tagged **"100% Free"** on dafont.com (unrestricted use,
including redistribution) at the time they were added. Filenames intentionally match each
font's internal family name exactly — the plugin resolves fonts via `fc-match <name>`, which
requires an exact family-name match to resolve reliably. Several were renamed from their
original dafont title for a cleaner dropdown; renaming rewrote the font's own internal name
table (via fontTools), not just the filename, so fc-match still resolves them correctly.

| File | Original dafont title | Author | Source |
|---|---|---|---|
| Christmas Garland.otf | (same) | Mozatype | https://www.dafont.com/christmas-garland.font |
| Kingthings Christmas.ttf | Kingthings Christmas 2 | Kingthings | https://www.dafont.com/kingthings-christmas.font |
| PW Joyeux Noel.otf | New Born Christmas | Keithzo | https://www.dafont.com/new-born-christmas.font |
| PW Tinsel Letters.ttf | PWTinselLetters | Peax Webdesign | https://www.dafont.com/pwtinselletters.font |
| Present Snow.ttf | Santa Christmas | Keithzo | https://www.dafont.com/santa-christmas.font |
| Toy Train.ttf | (same) | West Wind Fonts | https://www.dafont.com/toy-train.font |
| Xmas Lights.ttf | Xmas Lights (BRK) | AEnigma | https://www.dafont.com/xmas-lights.font |

"PW Joyeux Noel.otf" and "Present Snow.ttf" are renamed for the dropdown label only — they
are not the still-unresolved "PW Joyeux Noel" font by Peax Webdesign referenced earlier; that
one's download link was never found.

"Christmas Garland" and "PW Joyeux Noel" are `.otf` files. They render correctly through the
plugin's normal PIL-based path, but are not resolvable by FPP's native overlay-text fallback
(used only when PIL is unavailable or the overlay model's dimensions aren't set), since FPP's
own font scanner does not recognize `.otf` files.
