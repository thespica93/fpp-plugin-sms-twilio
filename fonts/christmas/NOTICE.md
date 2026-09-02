# Bundled Christmas Fonts

All fonts in this directory are tagged **"100% Free"** on dafont.com (unrestricted use,
including redistribution) at the time they were added. Filenames intentionally match each
font's internal family name exactly — the plugin resolves fonts via `fc-match <name>`, which
requires an exact family-name match to resolve reliably.

| File | Author | Source |
|---|---|---|
| Christmas Garland.otf | Mozatype | https://www.dafont.com/christmas-garland.font |
| Kingthings Christmas 2.ttf | Kingthings | https://www.dafont.com/kingthings-christmas.font |
| New Born Christmas.otf | Keithzo | https://www.dafont.com/new-born-christmas.font |
| PWTinselLetters.ttf | Peax Webdesign | https://www.dafont.com/pwtinselletters.font |
| Santa Christmas.ttf | Keithzo | https://www.dafont.com/santa-christmas.font |
| Santa Chrismast Display.ttf | Keithzo | https://www.dafont.com/santa-christmas.font |
| Santa christmas start.ttf | Keithzo | https://www.dafont.com/santa-christmas.font |
| Toy Train.ttf | West Wind Fonts | https://www.dafont.com/toy-train.font |
| Xmas Lights (BRK).ttf | AEnigma | https://www.dafont.com/xmas-lights.font |

The three "Santa Christmas" files are separate weights/variants from the same download that
each ship with a different internal family name (including two typos from the original
author: "Chrismast" and "start"/"Star") — the odd names are preserved as-is rather than
corrected, since correcting them would break font resolution.

"Christmas Garland" and "New Born Christmas" are `.otf` files. They render correctly through
the plugin's normal PIL-based path, but are not resolvable by FPP's native overlay-text
fallback (used only when PIL is unavailable or the overlay model's dimensions aren't set),
since FPP's own font scanner does not recognize `.otf` files.
