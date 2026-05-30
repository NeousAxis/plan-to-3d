# plan-to-3d — Roadmap vers le rendu photoréaliste

> Plan d'évolution. À exécuter par étapes, en vérifiant chaque étape dans le navigateur.
>
> **AVANCEMENT (2026-05-30) :**
> - ✅ Étape 0 — cases à cocher des calques (Toit/Plafond/Murs/Verre/Sol/Mobilier/Luminaires/Étiquettes)
> - ✅ Étape 1 — UV dans le GLB + textures procédurales (bois, terrazzo, marbre, plâtre, tissu, art abstrait)
> - ✅ Étape 2 — luminaires (sconce/downlight/pendant/floor_lamp), spots dirigés vers le sol pour les downlights (cône qui dessine une flaque), bloom UnrealBloomPass avec auto-détection perf (off sur petit matériel)
> - ✅ Étape 3 — caméra Visite (hauteur d'œil) + déplacement ZQSD/WASD/flèches
> - ✅ Étape 4 — entourage (silhouettes-billboards procédurales), plafond métal perforé (texture alpha avec trous) ; reste : image d'art fournie par utilisateur
> - ✅ Étape 5B (alternative simplifiée) — bouton **« 📸 Rendu photo »** opt-in : rendu studio 1920×1080 super-sampling 4× + shadow map 4096 + bloom, **sans dépendance externe** (path tracer GPU abandonné car trop fragile vs versions). Téléchargement PNG. Vrai GI (5A lightmaps) reste à faire.
> - ✅ Étape 5A (still) — **`bake.py` + `bake_blender.py` : pipeline Blender Cycles headless**. Author-side GI photoréaliste (flaques de lumière au sol, indirect bounces, soft shadows, sky Hosek-Wilkie). Auto-config GPU Metal sur M3. Furniture lights → Cycles lights. Bake d'un lobby = 64-256 samples = 1-5 min sur M3 Max. Sortie `render.png` partageable par tout le monde.
> - 🟡 Étape 5A (lightmap embedded) — **`bake_lightmap.py` + `bake_lightmap_blender.py` : pipeline lightmap UV2 dans le GLB**. Le bake passe (UV unwrap, 1024×1024 par mesh, GI cuit, exporté en `model_lit.glb`). Le viewer convertit les meshes lightmapped en `MeshBasicMaterial` avec map=lightmap×base. **Pipeline fonctionne, plafond/murs/tableau bien visibles**, mais l'exporter glTF Blender perd le node Multiply pour les surfaces à couleur unie (plante, certains meubles) → ces zones sortent noires. **Pour finir proprement il faudrait un 2e pass de bake type EMIT qui pré-multiplie tout dans une image unique** — sprint dédié de ~1-2 h. En attendant, le **rendu still Cycles** reste la voie qui donne le vrai look photoréaliste.
> - ➕ Bonus : arêtes verticales chanfreinées (box_chamfered 12 arêtes), finition de mur (`finish`), comptoir d'accueil, banc, tableau encadré, plafond perforé, calque **Structure** séparé, **sconces murales** correctes (platine + bras + globe), exemple `lobby.json`, calibrage exposure/IBL, **art Rothko-grid paramétrable par hue (UV explicites)**, **2 styles de plafond cohabitant (dark_panels + perforated)**
> - ⬜ Étape 5 — photoréaliste côté créateur : 5A lightmaps cuits / 5B stills Blender
> - ⬜ Étape 6 — paliers de perf, hors-ligne total, replis WebGL2

## Contrainte absolue (décidée 2026-05-30)

**« Je crée ça pour TOUT LE MONDE — tout le monde n'a pas le même ordi. »**

Conséquences sur l'architecture :

- Le fichier livré (`viewer.html`) doit tourner sur **n'importe quelle machine**
  (vieux portable, téléphone) : viewer **temps réel léger** (rasterizer WebGL),
  **aucune installation**, idéalement consultable hors-ligne.
- **PAS de path tracer GPU dans le fichier livré** : ça exigerait un GPU puissant
  (genre M3 Max) → exclu pour un usage universel.
- Le **rendu photoréaliste lourd se fait UNE FOIS côté créateur** (sur sa machine
  puissante) puis est **livré sous forme légère** : textures + éclairage « cuit »
  (baked lightmaps) et/ou images pré-rendues. C'est le modèle standard de l'archi-viz :
  on calcule une fois, on diffuse léger.

Donc deux pipelines distincts :

| | Côté CRÉATEUR (lourd, 1 fois) | Côté SPECTATEUR (léger, partout) |
|---|---|---|
| Rôle | calcul GI / bake / rendu still | naviguer le modèle |
| Coût | OK si lent / GPU costaud | doit être fluide sur petit matériel |
| Sortie | lightmaps, PNG, turntable | `viewer.html` + galerie d'images |

---

## Cible visuelle (références fournies par l'utilisateur)

1. Sketch 3D « dollhouse » meublé (déjà ATTEINT — commit `d869e49`).
2. **Rendu photoréaliste à hauteur d'œil** (hall d'accueil) : illumination globale,
   halos des spots au sol, **textures** (bois, terrazzo, plafond métal perforé),
   **luminaires qui éclairent vraiment** (appliques globe, downlights), tableau
   encadré, **entourage** (silhouettes), profondeur de champ. ← objectif de cette roadmap.

---

## Étape 0 — Cases à cocher des calques (quick win, à faire en premier)

Demandé explicitement. Remplacer la toolbar par un **panneau de calques** :

- ☑ Toit ☑ Murs ☑ Verre ☑ Sol ☑ Mobilier ☑ Plafond ☑ Luminaires ☑ Étiquettes
- Implémentation : ranger chaque mesh dans un « bucket » selon le nom de matériau,
  puis `mesh.visible = checkbox.checked`. Étiquettes via `CSS2DObject.visible`.
- Garder aussi les boutons de vue (Iso / Dessus / Walkthrough).
- **Risque : faible. Effort : ~30 min.** Tourne partout (gratuit en perf).

---

## Étape 1 — Matériaux & textures (universel, léger)

Prérequis bloquant : **le GLB n'a pas encore de coordonnées UV.**

1. **Générer les UV dans le GLB** (`write_glb`) par projection triplanaire en
   coordonnées monde (mètres) : selon l'axe dominant de la normale de chaque face,
   `uv = (deux coords monde)`. Faces déjà séparées → pas de couture.
2. **Textures procédurales côté viewer** (canvas, donc 0 fichier, 0 réseau) :
   bois (veinage), terrazzo (éclats sur fond clair), plâtre (bruit léger), tissu,
   métal, moquette. Assignées par nom de matériau ; `wrapS/T = Repeat` ;
   `repeat` réglé par matériau pour la densité de tuilage.
3. Garder petit : textures 256–512 px, réutilisées. Aucun impact mémoire notable.

**Effort : moyen. Risque : faible.**

---

## Étape 2 — Luminaires & ambiance (modéré, dégradable)

1. Nouveaux types meubles/fixtures : `sconce` (applique globe), `downlight`
   (spot encastré), `pendant` (suspension), `reception_desk`, `bench`, `planter`,
   `artwork` (panneau encadré).
2. Matériau `lamp` **émissif** (les globes/spots brillent).
3. Côté viewer : attacher quelques `PointLight`/`SpotLight` réelles aux meshes
   `lamp` pour créer les **halos au sol** — **plafonné** (ex. 8–12 lumières max,
   les plus proches de la caméra) pour rester fluide sur petit matériel.
4. Post-process léger : **bloom** (les lampes « glow »), **SSAO** doux. Tous deux
   derrière un **réglage de qualité** (Bas/Moyen/Haut) avec auto-détection :
   désactivés par défaut sur faible perf.

**Effort : moyen-élevé. Risque : moyen (perf). Mantra : dégrader proprement.**

---

## Étape 3 — Caméra à hauteur d'œil (walkthrough)

1. Mode « Walk » : caméra à ~1.6 m, regard horizontal, déplacement **ZQSD/flèches**
   + drag souris (style FPS doux), collisions simples optionnelles (rester dans
   l'enveloppe).
2. Boutons de vue : Iso · Dessus · **Walkthrough** + points de vue pré-réglés
   (entrée, etc.).
3. `window.__viewer.walk()` pour scripter/screenshoter une vue intérieure.

**Effort : moyen. Risque : faible.**

---

## Étape 4 — Entourage & œuvres (polish)

1. **Entourage** : silhouettes en **billboard** (plans PNG avec alpha, toujours
   face caméra). 3–5 découpes intégrées en base64. Type meuble `person`.
2. **Tableau encadré** : `artwork` avec image fournie par l'utilisateur (drag&drop
   ou chemin) appliquée en texture sur la toile ; sinon motif abstrait procédural.
3. **Plafond** : type `ceiling` (dalle métal perforée → texture à trous + alpha)
   avec downlights intégrés.

**Effort : moyen. Risque : faible.**

---

## Étape 5 — Le vrai photoréaliste (côté CRÉATEUR, 1 fois)

But : produire la qualité « hall d'accueil » SANS alourdir le fichier livré.
Deux options, **non exclusives** :

### 5A — Bake de l'illumination globale en lightmaps (recommandé « pour tout le monde »)
- Sur la machine du créateur, calculer la GI une fois et la **cuire dans des
  textures** (lightmaps) ; les embarquer dans le GLB.
- Le viewer léger affiche alors un éclairage **qualité GI** pour un coût quasi nul →
  **tout le monde** voit le beau rendu, même sur petit matériel.
- Outils possibles : bake via three.js (gpu-pathtracer **uniquement côté créateur**),
  ou Blender headless → lightmaps → réimport. Demande UV2 non chevauchants.
- **Risque : élevé (unwrap UV2, pipeline de bake). Effort : élevé.** C'est le cœur
  technique du saut de qualité « universel ».

### 5B — Stills / turntable pré-rendus (plus simple, fiable)
- `render.py` optionnel : si **Blender** est dispo (à installer une fois, ~1 Go,
  gratuit, Cycles GPU Metal sur M3), importer le GLB, poser caméra hauteur d'œil,
  éclairage, matériaux, **path tracing → PNG** photoréaliste (+ option turntable MP4).
- Livrable = galerie d'images que **n'importe qui** ouvre (aucune perf requise).
- **Risque : moyen (dépendance Blender). Effort : moyen.** N'impacte pas le viewer.

> Reco : faire **5B** d'abord (résultat photoréaliste fiable rapidement), puis
> **5A** pour que l'interactif lui-même devienne beau partout.

---

## Étape 6 — Performance & compatibilité « pour tout le monde »

- **Paliers de qualité** auto (Bas/Moyen/Haut) selon `devicePixelRatio`,
  nombre de cœurs, taille écran ; bloom/SSAO/ombres modulés.
- **Plafond de triangles** + fusion de géométrie ; instancing pour meubles répétés
  (postes de travail) → moins de draw calls.
- **Option hors-ligne total** : inliner three.js dans le HTML (sinon CDN unpkg).
- **Repli** si pas de WebGL2 : message clair + image statique pré-rendue (lien 5B).
- Tester sur petit gabarit (throttling Chrome / téléphone).

---

## Séquencement proposé

1. **Étape 0** (cases à cocher) — quick win.
2. **Étape 1** (UV + textures) — base de tout le reste.
3. **Étape 3** (walkthrough) — gros gain perçu, peu risqué.
4. **Étape 2** (luminaires + bloom/SSAO dégradables).
5. **Étape 4** (entourage, tableau, plafond).
6. **Étape 5B** (stills Blender) puis **5A** (lightmaps bake).
7. **Étape 6** (paliers perf, hors-ligne, replis) en continu.

## Risques principaux

- UV2 / unwrap pour le bake lightmap (5A) = le point le plus délicat.
- Garder le viewer fluide sur petit matériel tout en ajoutant lumières/post-process
  → tout doit être **dégradable** et plafonné.
- Dépendance Blender (5B) = installation ponctuelle côté créateur uniquement.

## Contenu modèle à ajouter (transverse)

- Finitions : habillage mural bois, plafond métal perforé + downlights, sol terrazzo.
- Fixtures : `reception_desk`, `bench`, `sconce`, `pendant`, `downlight`, `artwork`,
  `planter`, `person` (entourage), `ceiling`.
