# Bundled Halloween Fonts

All fonts in this directory are tagged **"100% Free"** on dafont.com (unrestricted use,
including redistribution) at the time they were added. Filenames match each font's internal
family name exactly — the plugin resolves fonts via `fc-match <name>`, which requires an
exact family-name match to resolve reliably.

| File | Author | Source |
|---|---|---|
| All Eyes On Me.ttf | Dug McUgly | https://www.dafont.com/all-eyes-on-me.font |
| Bone Brigade.otf | Field 2 Design (Ben Dunkle) | https://www.dafont.com/bone-brigade.font |
| Halloween Spider.ttf | Chloe5972 | https://www.dafont.com/halloween-spider.font |
| Mortified Drip.ttf | Walter E Stewart | https://www.dafont.com/mortified-drip.font |
| Mummified.ttf | Haunted House Fonts | https://www.dafont.com/mummified.font |
| October Crow.ttf | Sinister Fonts (Chad Savage) | https://www.dafont.com/october-crow.font |
| Spider Font.ttf | javierq | https://www.dafont.com/spider-font.font |
| Super Midnight.ttf | fsuarez913 | https://www.dafont.com/super-midnight.font |

"Spider Font" is the only rename: its internal family name was `SPIDER font` (inconsistent
casing) — rewritten via fontTools to `Spider Font` for consistency with the rest of the
catalog. Every other file's internal family name already matched its dafont title exactly, no
rewrite needed.

"Bone Brigade" is the only `.otf` in this set. It renders correctly through the plugin's
normal PIL-based path, but is not resolvable by FPP's native overlay-text fallback (used only
when PIL is unavailable or the overlay model's dimensions aren't set), since FPP's own font
scanner does not recognize `.otf` files.
