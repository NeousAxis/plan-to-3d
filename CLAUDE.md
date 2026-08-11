# CLAUDE.md — plan-to-image session handoff

> **Lu automatiquement par Claude au démarrage de toute session dans ce repo.**
> Mis à jour 2026-07-02 **(session #4)**. Direction actée par l'user : **structurer les outils
> existants en « ARES-lite »** autour d'une source de vérité unique (`plan.json`).
> Lis ⚡⚡⚡ (#4) puis les leçons de ⚡⚡ (#3) AVANT tout.

## ⚡⚡⚡⚡ SESSION 2026-08-11 #5 — « Le projet ne fonctionne pas » : crash Visiter réparé + rendu intérieur niveau rendu CAO

L'user est arrivé fâché avec une image de référence (rendu CAO pro : couloir, panneaux bois,
tableau, spots encastrés, flaques de lumière, silhouettes grises). Diagnostic + fix dans `web/cad.html` :

1. **LE bug qui rendait « Visiter » mort** : passer en vue 3D seule (`setView('v3d')`, donc
   Visiter ET `?eye=`) redessinait le 2D avec un canvas de largeur 0 → `T2.s` négatif →
   `roundRect` rayon négatif → RangeError **avalée par le `.catch` du boot** qui affichait le
   toast « Servir via http » trompeur. Fix : guard dans `draw2d()` + le catch loggue et
   n'affiche le hint que si `MODEL` est null. Leçon : ce `.catch` attrape TOUTE la chaîne `.then`.
2. **Upgrade rendu intérieur** (tout procédural, zéro asset externe, distribuable) :
   ACES tone mapping + sRGB ; spots plafonniers encastrés (SpotLight + luminaire visible en FP,
   grille ~2,7 m/pièce + échantillonnage 1,4 m sur la circulation, cap 24) ; **double régime
   lumineux** `fpLights(on)` (dollhouse = soleil ; FP = ambiance basse, spots dominants → les
   flaques de la référence) ; matériaux MeshStandard (terrazzo circulation, parquet repeat
   proportionnel à la pièce, placage bois) ; **mur d'accent** placage + toile abstraite sur le
   plus grand pan de mur PLEIN (living/chambres/hall) ; **silhouettes entourage billboard**
   (toujours face caméra, hall + séjour) ; fenêtres = MeshBasic lumineux ; fermeture du hull
   (murs sur les segments du périmètre non couverts par une pièce) + dalle terrazzo + plafond
   sous tout le hull (le HALL de apartment_2br est label-only, sans rect ni circulation !) ;
   spawn FP : pièce HALL/ENTR → plus grande cellule de circulation → label_at du hall,
   regard initial = azimut le plus dégagé (`freeDirAt`, 16 directions, l'axe du couloir).
   Vérifié dans Chrome sur les 2 plans (`apartment_2br.json` + `appart_auto.json`) :
   entrée/sortie Visiter, dollhouse intact, 2D intact.
   **Reste à faire qualité** : ombres de contact des meubles (spots sans castShadow pour la perf),
   sconces globes muraux, personnes assises, moulures/plinthes. Et toujours le backlog #4 :
   Space segmentation JSON + `rooms[].poly` + édition dans cad.html.
3. **RÈGLE ABSOLUE née de la colère user (« tu feras encore des erreurs sur d'autres plans ») :
   `tools/plan_audit.py` = étape OBLIGATOIRE avant de montrer un 3D.** Superpose le plan.json
   (pièces rouge, portes vert, fenêtres bleu, meubles orange, entrée magenta) sur l'image source,
   calé automatiquement sur les murs (couleur poché bleu nuit ; repli composante sombre).
   `python3 tools/plan_audit.py web/plans/x.json image.jpg -o overlay.png` puis REGARDER l'overlay.
   Sur le plan user, l'audit a attrapé d'un coup : porte d'entrée absente du schéma (→ `MODEL.entry`
   ajouté dans cad.html, 2D+3D), frigo cuisine inventé, 2e fenêtre N du séjour manquante,
   lave-mains changing manquant, 4 portes décalées. Ne JAMAIS transcrire un plan à la main
   sans passer l'audit derrière.

## ⚡⚡⚡ SESSION 2026-07-02 #4 — Restructuration « ARES-lite » : plan.json = source de vérité

### Le recadrage user (à prendre au sérieux)
« Copier ARES Commander n'est pas si difficile — on a déjà une grande partie des outils,
il faut les **structurer correctement**. » → Fini les scripts éparpillés avec la géométrie
**dupliquée en dur** dans chaque HTML. Tout converge vers UN modèle :

```
ENTRÉES                          SOURCE DE VÉRITÉ            SORTIES
plan_raster.py (raster coté) ──┐                     ┌─► web/cad.html   (app 2D+3D synchro)
plan_dxf.py parse (DXF)       ─┼──►  plan.json  ────┼─► plan_dxf.py export → .DXF réel (ARES/AutoCAD)
lecture vision auto (À FAIRE) ─┘   (rects+portes+    └─► plan_to_image.py (habillage FLUX, PAS fidélité)
                                    fenêtres+meubles)
```

### ✅ Construit & VÉRIFIÉ cette session (non commité)
| Fichier | Rôle | Vérif |
|---|---|---|
| `web/plans/apartment_2br.json` | **LE plan user en JSON canonique** : `rooms[{name,rect[x0,y0,x1,y1],area_m2,doors[{wall,at}],windows[…],furniture[{item,rect,h}]}]` (m, y vers le bas) | — |
| `web/cad.html` | **PlanCAD** : app unique — 2D (canvas, murs/portes en arc/fenêtres/meubles/cotes) + 3D (Three.js) **côte à côte, synchronisés**, clic pièce = surlignage dans les 2 vues + panneau infos, boutons 2D/3D/split, charger (drag-drop .json) / exporter JSON | ✅ testé Chrome : rendu + sélection sync OK |
| `plan_dxf.py export` | `plan.json → .dxf` (calques ROOMS/LABELS/WINDOWS/DOORS, mm, arcs de porte orientés) | ✅ **round-trip** : export → re-parse → 8/8 pièces, dims/fenêtres/portes identiques |
| `plan_reader.py` | **LA lecture AUTO** : `read plan.png` → N passes CF Mistral vision fusionnées (prompt strict regex) + **1 passe labels dédiée** (positions des noms, jamais flippées) → solveur (`plan_raster.solve_room`) → **grille des murs** (sommes cumulées des CHAIN) → **solveur de pavage** (candidats sur grille + non-chevauchement + labels en départage, backtracking élagué). `--from-lines` = test offline sans quota | ✅ **RÉUSSI sur le vrai plan de l'user** (`work/_plan/Plan appartement.jpg`, 3 appels vision fusionnés) : **8/8 pièces à leur vraie place**, dims exactes, vérifié dans PlanCAD. Lignes brutes auditées dans `web/plans/appart_auto.json.lines.txt`, sortie `web/plans/appart_auto.json` |

Serveur : `cd web && python3 -m http.server 8777` → `http://localhost:8777/cad.html`
(`?plan=plans/xxx.json` pour charger un autre plan).

### ⚠ Limite VÉRIFIÉE de la lecture vision (test réel sur le plan maison, 2026-07-02)
Le plan maison (`work/_plan/plan_user.png`) n'a **AUCUNE surface ni cote imprimée** → CF Mistral a
**INVENTÉ** surfaces et côtés (et raté WC + DGT) malgré la consigne. Prompt durci depuis
(`area -` / `ENVELOPE: - x -` acceptés par le parseur, régression OK), mais retenir :
- **Plans cotés** (surfaces m² + chaînes imprimées, comme l'appart 2BR) → lecteur fiable, c'est la cible.
- **Plans muets** → ne PAS faire confiance aux nombres vision. Piste : échelle via objet connu
  (lit 140×190, l'astuce scale de la session #2) ou refus propre (label-only).
- Toujours auditer `<out>.lines.txt` (les lignes brutes sont sauvées à côté de la sortie).

### ⚠ Deltas connus dans apartment_2br.json (positions encore posées à la main)
- CHANGING : rect 2.35×2.68 = 6.30 m² vs **5.57 imprimé** (profondeur réelle ~2.37).
- KITCHEN : rect 10.88→14.26 (3.38 m) vs cote **3.05** (devrait finir ~13.93, écart = murs).
→ Se corrige en éditant **UN SEUL fichier** (`web/plans/apartment_2br.json`) maintenant.

### 🎯 NOUVELLE STRATÉGIE LECTURE (actée avec l'user, 2026-07-02 fin de session)
L'user a raison : des **modèles spécialisés** (CubiCasa5K-style) font le spatial en secondes là où
le LLM vision généraliste galère. Architecture cible à 3 étages :
- **Spatial** → modèle spécialisé. **VALIDÉ** : le Space HF public `Viraj2307/Floor-Plan-Detection`
  détecte sur le plan user **9 pièces, 10 portes (dont le WC en-suite !), 4 fenêtres aux bons murs**
  en secondes (`plan_seg.py detect`). Overlays de preuve : `work/_seg/detect_{0,1}.png`.
- **Métrique** → notre solveur chaînes (échelle exacte + cross-check surfaces) — les modèles de
  segmentation sortent des pixels, pas des mètres. Les deux se complètent.
- **Fallback** → heuristiques actuelles de plan_reader + éditeur.
**Bloqueur d'intégration** : le Space renvoie des IMAGES annotées, pas des coordonnées.
L'extraction des cadres rouges (composantes connexes, `plan_seg.red_boxes`) ne récupère que ~80 %
(les cadres qui se touchent fusionnent) ; `red_rects` (appariement de segments) est buggé.
**La solution propre = déployer NOTRE Space HF** (gratuit) qui exécute le même modèle et renvoie
du JSON brut (le repo CubiCasa5k est open source, le Space Viraj est forkable). Nécessite un token
HF write. **C'est LA première tâche de la prochaine session.** Ne PAS brancher les données
bruitées de l'extraction d'overlays dans plan_reader — le plan actuel est enfin juste.

### ⛔ LIMITE STRUCTURELLE identifiée en fin de session (l'user l'a démontrée, crop à l'appui)
Le schéma plan.json ne connaît que des pièces RECTANGULAIRES (`rect`). Or sur son plan la
**CHANGING ROOM est en L : elle ENVELOPPE le WC** (le bras au-dessus du WC porte le lave-mains,
la porte du WC ouvre en haut depuis ce bras). Conséquences irréparables par heuristique :
changing rect + couloir fantôme au-dessus du WC + porte WC au mauvais endroit + glyphe penderie
(l'user n'a RIEN à cet endroit). **Ne PAS re-patcher les heuristiques.** Le fix :
1. **`rooms[].poly`** (polygone rectiligne, fallback `rect`) dans le schéma + rendu 2D/3D ;
2. le **Space de segmentation JSON** qui sort les vraies formes (CubiCasa donne des polygones) ;
3. glyphes mobilier changing : lave-mains seul par défaut (pas de penderie « canapé »).

### 👉 REPRENDRE PAR ICI
0. **Déployer le Space de segmentation JSON** (voir 🎯 ci-dessus) puis brancher dans plan_reader :
   **polygones** de pièces (pas bboxes) + portes/fenêtres → remplacent les heuristiques.
   Puis supporter `poly` dans le paveur, cad.html (2D+3D) et plan_dxf export.
1. ~~Lecture AUTO du raster → plan.json~~ ✅ **FAIT — 9/9 pièces (WC compris) bien placées**
   sur le plan réel. Commande unique : `python3 plan_reader.py read "work/_plan/Plan appartement.jpg"
   -o web/plans/appart_auto.json` (~4 appels quota CF : N passes + labels + complétude).
   Mécanismes clés (tous déterministes, la vision ne fait que LIRE) :
   corroboration des cotes par les chaînes (bathroom 2.26→2.38) · pénalité de taille dans le
   pavage (kitchen exacte) · portes = mur intérieur à contact max avec la circulation ·
   fenêtres filtrées au hull (1/mur) · gaps → pièce manquée (adoption du label le plus proche,
   ex. WC) ou bras du hall · slivers absorbés par la pièce alignée.
   **Restes honnêtes** : fenêtre BEDROOM 1 lue N (réel W) et KITCHEN S (réel E) — faiblesse
   vision non corrigeable sans pixels ; portes CHANGING/KITCHEN/WC posées au coin adjacent
   (~70 cm du réel). → se corrigent dans l'éditeur (brique suivante).
2. **Édition dans cad.html** (drag murs/portes, éditer cotes, ajouter une pièce) → sauver le JSON →
   corrige les restes de lecture EN UI → ARES-lite complet. **C'est la prochaine brique.**
3. Puis : mobilier auto (vision), export DXF du plan auto, re-brancher plan_to_image (habillage FLUX).

## ⚡⚡ SESSION 2026-07-01 #3 — « Je veux MON plan en 3D » (⚠️ session pénible, lire les leçons)

### Ce que l'user veut VRAIMENT (recadré à la dure)
Parti d'une question sur **ARES Commander** (CAO). Il ne veut PAS qu'on clone un noyau CAD.
Il veut : **son plan 2D (une image PNG cotée) transformé en 3D FIDÈLE** — SES murs, SES
proportions, SES pièces, SES portes, SON mobilier. **« un architecte veut voir SON dessin, pas le TIEN ».**
Il n'a **QUE des plans raster** (PNG), **pas de DWG/DXF**.

### ⛔ Erreurs commises cette session (NE PAS refaire — l'user s'est fâché fort, plusieurs fois)
1. **Sur-vendu un rendu FLUX** d'une salle de bain générique en disant « c'est fidèle » → FAUX
   (belle image inventée, pas SA pièce). Le piège de la session #2, refait. **Ne JAMAIS appeler
   « fidèle » un render text→image.**
2. **Trop de questions** au lieu de LIRE le plan. Son plan est **clair et coté** : noms + surfaces
   (m²) + chaînes de cotes imprimées. Claude EST multimodal → **LIRE l'image directement**, ne pas
   demander à l'user de redonner ce qui est écrit dessus.
3. **Reconstruction géométrique à la main** (je tape des rectangles au jugé) → erreurs (portes
   oubliées, Bedroom 2 mal dimensionné). C'est LA cause racine des allers-retours.

### ✅ Construit cette session (fichiers présents, NON commités)
| Fichier | Rôle | État |
|---|---|---|
| `plan_dxf.py` | parseur **DXF→geometry.json** (ezdxf, portes par sens d'ouverture d'arc) | testé sur DXF synthétique 8/8 OK — **mais l'user n'a pas de DXF** |
| `tools/make_test_dxf.py` | génère un DXF de test à vérité-terrain | ok |
| `plan_raster.py` | **solveur déterministe** : `surface imprimée ÷ une cote = l'autre côté`. Zéro dépendance (pas d'OCR/opencv → distribuable) | testé, 8/8 pièces cross-check OK sur le plan user |
| `web/plan2d.html` | **reconstruction 2D top-down** (canvas) depuis un modèle `ROOMS` — cotes, portes en arc, fenêtres. Ressemble au plan de l'user | visuellement proche mais **géométrie tapée à la main** |
| `web/apartment3d.html` | **3D Three.js** depuis le même modèle `ROOMS` : murs+ouvertures, fenêtres verre, **mobilier** (lit/canapé/baignoire/cuisine), orbit, labels | marche, mais mobilier = blocs simples, géométrie à la main |
| `work/_extract_raster/{facts.json,geometry_solved.json}` | le plan user résolu (8 pièces, dims exactes) | ok |

### 🏠 Le plan de l'user (appart 2BR, labels EN) — géométrie LUE (ne pas re-deviner bêtement)
Rectangle **14.26 × 7.04 m**. Cotes imprimées : haut `5.36 | 2.40 | 6.50`, gauche `3.72 | 3.32`,
droite `4.30 | 2.68`, bas `3.01 | 2.20 | 2.40 | 2.35 | 0.92 | 3.05`.
Pièces (m² imprimés) : BEDROOM 1 19.72 (5.36×3.72) · BATHROOM 5.71 · LIVING 27.95 (**6.50×4.30 pile**) ·
BEDROOM 2 12.42 (⚠ area vs cote 3.01 incohérent → ~3.74 large ?) · SHOWER 2.55 · HALL 8.76 (central) ·
CHANGING 5.57 · WC 2.47 · KITCHEN 8.17 (3.05×2.68 pile). Layout : Bed1 haut-G, Bath haut-centre,
Living haut-D ; Bed2 bas-G, Shower, Hall centre, Changing/WC/Kitchen bas-D.

### 👉 REPRENDRE PAR ICI (la vraie solution, pas encore faite)
**Arrêter de retaper la géométrie à la main.** Construire la **lecture AUTOMATIQUE du plan raster** →
`facts.json`/`ROOMS` → 2D → 3D, sans devinette :
- Option distribuable (respecte les contraintes) : **CF Mistral 24B vision** (déjà câblé, gratuit,
  sans carte, sans Google, sans install user) émet le `facts.json` (noms+m²+cotes+murs+portes).
- `plan_raster.py` a déjà le **solveur** (surface÷cote) ; il manque juste le front de lecture auto.
- Puis extruder `web/apartment3d.html` depuis ce modèle lu, pas tapé.
**But final = SON plan raster → 3D fidèle, automatique.** C'est ça qui débloque tout.

## ⚡ SESSION 2026-05-31 #2 — Fidélité au plan : extraction multi-agents + « prompt-juste »

### Le vrai problème (recadré par l'user)
Les renders FLUX text-only sont **beaux mais ne respectent PAS le plan** (taille, forme, fenêtres, placement).
Ex. cellier rendu comme grande buanderie **vitrée** alors qu'il est petit, **borgne**, irrégulier.
→ **Inutile d'ajouter des features tant que la génération ne colle pas au plan.**

### ⛔ Contraintes user — ABSOLUES et SIMULTANÉES (ne JAMAIS en assouplir une seule)
**Gratuit · SANS carte/paiement · SANS aucun produit Google (Gemini/Nano Banana = INTERDIT) ·
qualité frontière · appartement entier · rapide · skill distribuable (pas d'install locale par user).**
Sa thèse : « si c'était faisable en assouplissant, les autres l'auraient déjà fait — donc on doit le craquer. »

### ✅ Construit & validé cette session
1. **`plan_extract.py` — système multi-agents (idée de l'user, ça MARCHE).**
   - 7 agents, **une info chacun** (rooms / dims / openings / fixtures / shape / entry / scale),
     chacun sort un **format strict parsé par REGEX** (toute ligne non conforme rejetée ✗).
   - Runtime : chaque rôle = **un sous-agent Claude** sur l'image du plan (écrit `slice_<role>.txt`).
     **Testé pour de vrai** (7 sous-agents) sur le plan réel → 78 lignes, 100 % parsées. Les agents ont
     **corrigé mes lectures à la main** (fenêtres, cotes, cellier).
   - `python3 plan_extract.py {roles | merge <dir> | spec <dir> --style japandi}` →
     `geometry.json` puis `spec.json` (avec champ `_geom` par pièce).
2. **`plan_to_image.py` — étendu :**
   - `build_control_from_geom(room,_geom)` : croquis perspective **géométrie-aware** (entrée→caméra,
     meubles sur leur **vrai** mur gauche/droite/fond, borgne = pas de fenêtre, irrégulier = mur en biais).
     **Validé visuellement** sur le cellier.
   - `render_geometric` = échelle de moteurs **ZÉRO Google** : `siliconflow`(opt-in payant) → `qwen`(HF
     Space, gratuit, throttlé) → `sd15`(CF, gratuit, plat). Providers : `fetch_qwen_edit`,
     `fetch_siliconflow_edit`, `fetch_cloudflare_img2img`, `build_edit_instruction`.
   - CLI : `--mode geometric --engine {auto,siliconflow,qwen,sd15} --strength --qwen-steps`.

### 📊 Matrice providers (FAITS vérifiés)
| Provider | Modèle | Gratuit / Carte / Google | Verdict |
|---|---|---|---|
| **Cloudflare** | SD1.5 img2img + flux-1-schnell / **flux-2-dev** (text) | gratuit / sans carte (wrangler) / sans Google | ✅ mais SD1.5 **plat** ; FLUX text = pas de contrainte géo. Quota guard 40/j (~29 utilisés). |
| **HF ZeroGPU** | **Qwen-Image-Edit** (frontière, chinois) | gratuit / sans carte / sans Google | ✅ qualité top MAIS ~**1 rendu/jour** (240 s/j), **épuisé** (reset ~23 h). |
| SiliconFlow | Qwen-Image-Edit | **carte requise** | ❌ rejeté (payant) |
| Gemini « Nano Banana » | Gemini 2.5 Flash Image | gratuit 500/j sans carte MAIS **Google** | ❌ **INTERDIT** (retiré du code) |
| **Pollinations** | FLUX | gratuit / anonyme | ✅ **magnifique** mais **text-only** ; renvoie **402** après usage intensif (→ ajouter `referrer` / cooldown) |

**Conclusion honnête :** aucun outil 2026 ne coche TOUTES les contraintes à la fois. L'user refuse d'assouplir → on cherche la voie maligne.

### 👉 DIRECTION EN COURS (dernière consigne user — À POURSUIVRE)
**PAS de force brute best-of-N.** À la place : **prompter JUSTE à partir des éléments des agents.**
- Logique `build_flux_prompt(room,_geom)` **draftée et fonctionnelle, PAS encore commitée** :
  borgne → `"fully enclosed WINDOWLESS room, NO window"` **en tête** ; caméra depuis `entry` ;
  **chaque meuble traduit gauche/droite/fond** selon l'entrée (entrée N → E=gauche, W=droite, S=fond) ;
  négations explicites pilotées par la donnée. (Code dans l'historique de la session #2.)
- **TODO prochaine session :**
  1. Folder `build_flux_prompt` dans `plan_to_image.py` (nouveau `--engine flux-geo` ou `--mode prompt`).
  2. Rendre via **Pollinations FLUX** (`&referrer=plan-to-image` pour contourner le 402) avec **fallback
     CF `black-forest-labs/flux-2-dev`** (gratuit, frontière, sans carte, sans Google).
  3. Vérifier que le **cellier sort borgne + lave-linge à gauche + PAC à droite SANS force brute**.
     Si oui → **c'est LA solution** (beau + fidèle + gratuit + sans Google + distribuable).

### 🏠 Géométrie RÉELLE du plan user (extraite par les agents — NE PAS re-extraire)
Plan : `work/_plan/plan_user.png` (récupéré du transcript). Extraction : `work/_extract_real/geometry.json`.
- Maison plain-pied en L, HSP **2.50 m**. Pièces : CHAMBRE 1/2/3, S.D.B, WC, DGT, HALL,
  SALON/SEJOUR/CUISINE (ouvert, **40 m²**), CELLIER, PORCHE.
- **Fenêtres** : CH2→**W**, CH3→**E** (TVR), CH1→**S**, SALON→**N** (grande baie). **Mur E du salon = PLEIN**.
  Salon côté S = **porte-fenêtre**. Chambres = **PAS** de fenêtre au N (c'était la hachure du mur).
- **CELLIER** (la pièce-test) : **irrégulier** (angle 45° au SE), **BORGNE**, entrée **N** depuis le HALL →
  **ML+SL empilés mur E (= à gauche en entrant)**, **PAC mur W (= à droite)**.
- Lits **140×190** (CH2 = 140×195).

### 🧹 À nettoyer / noter
- **RÉVOQUER le token HF** : `~/.cache/plan_to_image/hf_token` (créé juste pour tester Qwen).
- `gradio_client` installé en `--break-system-packages` (homebrew py3.13) — utilisé par `fetch_qwen_edit`.
- Tests de rendus dans `work/_*` (jetables).

---

## Ce qu'est ce projet

**Skill `plan-to-image`** : convertit un plan 2D d'architecture en **renders intérieurs photoréalistes**, un PNG par pièce, en composant des prompts pro (style, matériaux, lighting, mobilier, palette, vue caméra) et en les envoyant à un modèle FLUX gratuit.

Pivot fait après 4 jours de tentatives sur une version géométrique 3D (Three.js + Cycles) qui plafonnait en qualité visuelle. Le **legacy 3D** est conservé sous `legacy_3d/`.

## Architecture actuelle

```
plan.png ─► plan_analyzer.py ─► spec.json
                   │ (CF Mistral 24B vision, free)
                   │  extrait rooms + dimensions + layout + mobilier
                   ▼
       [USER édite spec.json si besoin]
                   │
                   ▼
        plan_to_image.py ─► 8 PNG + gallery.html
                   │  Pollinations FLUX primary + CF FLUX schnell fallback
                   │  Quota guard CF (hard block à 40/jour, JAMAIS de paid)
```

## Fichiers clés

| Fichier | Rôle |
|---|---|
| `plan_to_image.py` | Génère les images. CLI : `spec.json → PNG/gallery`. Inclut `--mode geometric`, échelle de moteurs `render_geometric` (Qwen-Image-Edit HF / SD1.5, **zéro Google**), `build_control_from_geom`. |
| `plan_extract.py` | **(session #2)** Extraction multi-agents du plan : 7 agents (1 info/regex) → `geometry.json` / `spec.json`. CLI : `roles | merge <dir> | spec <dir>`. |
| `plan_analyzer.py` | Analyse un plan via Mistral Vision (1 passe) — **remplacé** par `plan_extract.py` (plus fiable) |
| `vocabulary.json` | Catalogue pro (styles, matériaux, mobilier, lighting, marques F&B/Vitra/Wegner/etc.) |
| `examples/apartment_2br_image.json` | Spec de référence transcrit du plan apt 2BR de l'user |
| `SKILL.md` | Doc de la skill, à jour avec le pivot |
| `ROADMAP.md` | Roadmap historique (3D → image) |
| `legacy_3d/` | Tout le code 3D Three.js + Cycles conservé pour référence |
| `docs/samples/` | 9 renders de référence (8 pièces apt + 1 CF) |

## Providers d'images (gratuits, ne jamais switcher en payant)

| Provider | Modèle | Auth | Quota | Qualité |
|---|---|---|---|---|
| **Pollinations.ai** | FLUX (≈ dev) | anonyme | non documenté, en pratique illimité | Magazine AD |
| **Cloudflare Workers AI** | `@cf/black-forest-labs/flux-1-schnell` | `wrangler login` (OAuth auto-refresh) | **HARD-BLOCK à 40/jour** | Bon |
| **CF Vision** | `@cf/mistralai/mistral-small-3.1-24b-instruct` | idem | même quota | Excellent pour analyser plans |

**Account ID Cloudflare** : `b2068a4d683aef98e2edcf4662eb0699`
**Token** : lu auto depuis `~/.wrangler/config/default.toml`. Si expiré → `wrangler login`.

### Quota guard
Compteur local `~/.cache/plan_to_image/cf_quota.json`. Warning à 75%, HARD BLOCK à 100%. Override possible avec `P2I_BYPASS_QUOTA=1` (uniquement pour debug — ne JAMAIS le mettre par défaut, l'user a explicitement INTERDIT de basculer en payant).

## Conventions du spec JSON

```jsonc
{
  "project": "<nom du projet>",
  "global": {
    "style":      "japandi",          // clé de vocabulary.styles, ou texte libre
    "atmosphere": "warm",
    "tech":       "architectural photography, AD-style, ..."
  },
  "rooms": [
    {
      "name":       "Séjour",
      // === Géométrie (lue sur le plan, en TÊTE du prompt FLUX) ===
      "dimensions": "rectangular, 6.50 × 4.30 m, 27.95 m², 2.6 m ceiling",
      "layout":     "two windows on north wall, door on west wall",
      "placement":  "L-shaped sofa centred facing north windows, ...",
      "viewpoint":  "view from the SW corner looking NE diagonally",
      // === Design (vocabulaire pro) ===
      "floor":      "smoked oak chevron parquet",
      "walls":      "lime plaster, hand-troweled, warm white",
      "ceiling":    "matte white plaster, recessed downlights",
      "lighting":   "Noguchi Akari pendant, golden hour sun",
      "furniture":  "Camaleonda sofa, Noguchi coffee table, Saarinen Tulip",
      "colors":     "F&B Cornforth White, terracotta accents",
      "extra":      "abstract canvas on south wall, dried branches"
    }
  ]
}
```

## Commandes essentielles

```bash
cd /Users/cyrilleger/planto3dsource

# 1. Analyser un plan → spec auto
python3 plan_analyzer.py ~/Downloads/plan.png --style japandi

# 2. Générer les images depuis un spec
python3 plan_to_image.py examples/apartment_2br_image.json
# → work/<project>/{room1.png, room2.png, ..., gallery.html}

# Options utiles :
#   --only "Séjour"          # 1 seule pièce
#   --seed 333               # changer la composition
#   --provider cloudflare    # forcer CF (sinon auto Pollinations puis CF fallback)
#   --width 1280 --height 720
```

## Règles apprises lors du travail (à appliquer)

1. **Géométrie en TÊTE du prompt** (le code le fait dans `build_prompt`). FLUX donne le plus de poids au début → c'est ce qui fait que le rendu respecte le plan.
2. **Max 3-4 meubles** par petite pièce sinon FLUX produit des hybrides moches (matelas qui flotte, canapé/desk fusionnés). Cf. chambre 2 fix dans commit `cda39f2`.
3. **Négations explicites** marchent : `"NO chair, NO desk, NO double bed"`.
4. **Bed-frame explicite** (`"solid oak frame visible, ~35 cm tall"`) sinon ça fait du matelas posé au sol.
5. **Viewpoint frontal** quand un objet est hero, pas diagonal (qui éparpille).
6. **Cotes précises** : reproduire à 2 décimales (5.36 × 3.72) plutôt qu'arrondir.

## Roadmap restante (ordre validé par l'user)

- ✅ **D** — 10 palettes tableau (legacy, hors scope nouveau pipeline)
- ✅ **A** — calibration bake (legacy)
- ✅ **C** — polish textures (legacy)
- ✅ **B** — lightmap embedded (legacy, partiel)
- ✅ **Pivot** vers plan-to-image
- ✅ **#1** — quota guard CF + hard block
- ✅ **#4** — plan-to-spec auto. **Remplacé/amélioré** par `plan_extract.py` (multi-agents + regex),
  **validé** sur le plan réel de l'user (cf. section ⚡).
- 🟧 **#5 (PRIORITÉ ACTUELLE)** — **fidélité géométrique au plan SANS payant/Google/local.**
  Voie en cours = **prompt-juste** (`build_flux_prompt` depuis `_geom`) → **Pollinations/CF flux-2-dev**.
  Voir « DIRECTION EN COURS » dans la section ⚡. (ControlNet abandonné : pas distribuable / pas gratuit-hébergé.)
- ⬜ **#2** — vue 360° immersive — **bloqué tant que #5 (fidélité) n'est pas réglé** (consigne user).
- ⬜ **#3** — UI HTML pour éditer le spec visuellement (dropdowns vocab, preview live).

## Question architecture en suspens

L'user a demandé : **peut-on mixer plan-to-image avec une vue immersive 3D pour "entrer dans l'image" ?** Trois pistes documentées dans la dernière réponse :
- (A) Pano 360° + sphère Three.js — la voie naturelle, ~2 j (recommandé)
- (B) Multi-vues + transitions, plan cliquable, ~1 j
- (C) 3D auto-généré depuis image (WonderJourney, NeRF…) — expérimental, lourd

## État au moment du handoff (fin session #2, 2026-05-31)

- **Lire la section ⚡ en haut** — c'est l'état réel.
- Construit & validé : `plan_extract.py` (multi-agents + regex) ; `plan_to_image.py` étendu
  (`build_control_from_geom`, échelle moteurs `render_geometric`, `fetch_qwen_edit`, etc., zéro Google).
- Géométrie réelle du plan user extraite dans `work/_extract_real/geometry.json` (plan : `work/_plan/plan_user.png`).
- **Bloqueur** : aucun moteur frontière ne tient TOUTES les contraintes ; quota HF Qwen épuisé du jour.
- **Reprendre par** : folder `build_flux_prompt` (prompt-juste depuis `_geom`) dans `plan_to_image.py`,
  rendre sur **Pollinations FLUX (+referrer)** / **CF flux-2-dev**, et valider le **cellier borgne** sans force brute.
- **Rien n'est commité** cette session — `git status` pour voir les fichiers modifiés/ajoutés.
- Détail complet (matrice providers, géométrie du plan, TODO) : section ⚡ en tête de fichier.

## Préférences user (à respecter)

- **Honnêteté absolue** sur ce qui marche / ne marche pas (l'user déteste les "done" faux).
- **JAMAIS** basculer en plan payant CF sans demande explicite.
- **Verifier visuellement** chaque output avant de dire "done".
- Vocabulaire d'archi/déco pro (pas de termes amateurs).
- L'user est francophone, les noms de pièces / labels en français.

## Repo

- GitHub : `https://github.com/NeousAxis/plan-to-3d` (nom historique, contient plan-to-image)
- Local : `/Users/cyrilleger/planto3dsource`
- Dernier commit : voir `git log -5 --oneline`
